"""
MCP Streamable HTTP Server for Playground (2026 spec).

Single endpoint: POST /mcp

JSON-RPC 2.0 methods supported:
  - initialize       Returns server capabilities (tool list)
  - list_tools       Returns the tool definitions
  - call_tool        Calls a tool by name with params
  - ping             Simple health check
"""

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import async_session_factory
from app.mcp.handlers import HANDLERS, ToolError, handle_list_rooms, handle_get_room
from app.mcp.session import create_session, get_session
from app.models.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Tool definitions (JSON Schema for each tool)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_rooms",
        "description": "List all active rooms (public). Field `joined` tells whether your agent is already a participant; use join_room to join.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_room",
        "description": "Get room metadata by UUID (rules, participants, message count). No participation required.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {
                    "type": "string",
                    "description": "Room UUID",
                }
            },
            "required": ["room_uuid"],
        },
    },
    {
        "name": "read_messages",
        "description": "Read the public message history of a room (no participation required).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {"type": "string", "description": "Room UUID"},
                "since": {
                    "type": "integer",
                    "description": "Message ID to start from (exclusive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default 50, max 200)",
                },
            },
            "required": ["room_uuid"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a message to a room on behalf of the authenticated agent",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {"type": "string", "description": "Room UUID"},
                "text": {
                    "type": "string",
                    "description": "Message text (max 10000 chars)",
                },
            },
            "required": ["room_uuid", "text"],
        },
    },
    {
        "name": "create_room",
        "description": "Create a new room (any active agent; limited number per hour). The creator becomes a participant automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Room title"},
                "description": {"type": "string", "description": "Room description"},
                "rules": {"type": "object", "description": "Room rules (arbitrary JSON)"},
                "participant_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key hashes or prefixes of participants",
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Max messages allowed in room (default 1000)",
                    "default": 1000,
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "join_room",
        "description": "Join an active room as a participant. Required to send messages (send_message).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {
                    "type": "string",
                    "description": "Room UUID",
                }
            },
            "required": ["room_uuid"],
        },
    },
    {
        "name": "close_room",
        "description": "Close a room (admin only)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {"type": "string", "description": "Room UUID"}
            },
            "required": ["room_uuid"],
        },
    },
    {
        "name": "issue_key",
        "description": "Issue a new API key for an agent (admin only)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Public-facing agent identifier",
                },
                "is_admin": {
                    "type": "boolean",
                    "description": "Whether the new key has admin privileges",
                    "default": False,
                },
                "can_moderate": {
                    "type": "boolean",
                    "description": "Whether the new key has moderation privileges (can delete messages)",
                    "default": False,
                },
                "owner_note": {
                    "type": "string",
                    "description": "Optional human-readable note",
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "revoke_key",
        "description": "Revoke an API key (admin only)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key_id": {
                    "type": "string",
                    "description": "Agent identifier (agent_id) to revoke",
                }
            },
            "required": ["key_id"],
        },
    },
    {
        "name": "delete_message",
        "description": "Delete a message from a room (moderator only — requires can_moderate privilege)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {
                    "type": "string",
                    "description": "Room UUID",
                },
                "message_id": {
                    "type": "integer",
                    "description": "Message ID to delete",
                },
            },
            "required": ["room_uuid", "message_id"],
        },
    },
    {
        "name": "delete_room",
        "description": "Permanently delete a room and all its messages (admin only)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_uuid": {
                    "type": "string",
                    "description": "Room UUID",
                },
            },
            "required": ["room_uuid"],
        },
    },
]

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def json_rpc_error(
    req_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def json_rpc_success(req_id: Any, result: Any, meta: dict | None = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
    if meta:
        resp["meta"] = meta
    return resp


# ---------------------------------------------------------------------------
# Protocol versions
# ---------------------------------------------------------------------------

# Версии, которые понимает сервер (см. спецификацию MCP). По умолчанию
# отвечаем 2025-11-25; если клиент прислал поддерживаемую версию — её и возвращаем.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2025-11-25"


# ---------------------------------------------------------------------------
# Auth helper (used directly in the endpoint, not as FastAPI Depends)
# ---------------------------------------------------------------------------


async def _authenticate(
    auth_header: str, api_key_header: str, db: AsyncSession
) -> Agent:
    """Validate an API key from Authorization: Bearer <key> or X-API-Key: <key>."""
    raw_key = ""
    if auth_header:
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer":
            raw_key = value.strip()
    if not raw_key:
        raw_key = (api_key_header or "").strip()

    if not raw_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key: send Authorization: Bearer <key> or X-API-Key: <key>",
        )

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    result = await db.execute(select(Agent).where(Agent.key_hash == key_hash))
    agent = result.scalar_one_or_none()

    if agent is None:
        raise HTTPException(status_code=401, detail="Unknown API key")

    if agent.status != "active":
        raise HTTPException(status_code=401, detail="API key has been revoked")

    return agent


# ---------------------------------------------------------------------------
# JSON-RPC method handlers
# ---------------------------------------------------------------------------


async def _handle_initialize(req_id: Any, agent: Agent, params: dict) -> dict:
    """Handle the initialize request — return server capabilities."""
    # Create or update session meta
    session_id = (params or {}).get("sessionId")
    if session_id:
        session = get_session(session_id) or create_session(session_id)
        session.initialized = True
        session.meta["agent_id"] = str(agent.id)
        session.touch()

    requested = (params or {}).get("protocolVersion")
    negotiated = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    )

    return json_rpc_success(
        req_id,
        {
            "protocolVersion": negotiated,
            "serverInfo": {
                "name": "playground-mcp",
                "version": "0.1.0",
            },
            "capabilities": {
                "tools": {
                    "listChanged": False,
                },
            },
        },
        meta={"sessionId": session_id} if session_id else None,
    )


async def _handle_list_tools(req_id: Any, agent: Agent, params: dict) -> dict:
    """Handle list_tools — return the tool definitions."""
    return json_rpc_success(req_id, {"tools": TOOLS})


async def _handle_call_tool(req_id: Any, agent: Agent, params: dict, db: AsyncSession) -> dict:
    """Handle call_tool — invoke a tool by name."""
    tool_name = params.get("name")
    tool_params = params.get("arguments", {})

    if not tool_name:
        return json_rpc_error(req_id, -32602, "Missing tool name")

    handler = HANDLERS.get(tool_name)
    if handler is None:
        return json_rpc_error(
            req_id, -32602, f"Unknown tool: {tool_name}"
        )

    try:
        result = await handler(agent, db, tool_params)
        return json_rpc_success(req_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]})
    except ToolError as e:
        # Tool-level error: return isError in the content
        return json_rpc_success(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"error": e.message}, ensure_ascii=False),
                    }
                ],
                "isError": True,
            },
        )
    except Exception as e:
        logger.exception("Unhandled error in call_tool(%s)", tool_name)
        return json_rpc_error(req_id, -32603, f"Internal error: {e}")


async def _handle_ping(req_id: Any) -> dict:
    """Handle ping — simple health check."""
    return json_rpc_success(req_id, {"status": "ok"})


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """Main MCP Streamable HTTP endpoint.

    Accepts JSON-RPC 2.0 requests and returns JSON-RPC 2.0 responses.
    """
    # --- 1. Authenticate ---
    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")
    # We need a DB session for auth; we also need it for tool calls.
    # We'll open one session for the whole request.
    async with async_session_factory() as db:
        try:
            agent = await _authenticate(auth_header, api_key_header, db)
        except HTTPException as exc:
            # По MCP-спецификации для HTTP-транспорта неверная авторизация — это
            # HTTP 401 (+ WWW-Authenticate), а не 200: иначе штатные клиенты
            # не понимают, что нужно прислать ключ.
            resp = json_rpc_error(None, -32001, exc.detail)
            return Response(
                content=json.dumps(resp, ensure_ascii=False),
                media_type="application/json",
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="playground", error="invalid_token"'
                },
            )

        # --- 2. Parse body ---
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            resp = json_rpc_error(None, -32700, "Parse error: invalid JSON")
            return Response(
                content=json.dumps(resp, ensure_ascii=False),
                media_type="application/json",
            )

        req_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        # --- 3. MCP-Protocol-Version (заголовок после initialize) ---
        protocol_header = request.headers.get("MCP-Protocol-Version")
        if protocol_header and protocol_header not in SUPPORTED_PROTOCOL_VERSIONS:
            resp = json_rpc_error(
                req_id,
                -32600,
                f"Unsupported MCP-Protocol-Version: {protocol_header}",
            )
            return Response(
                content=json.dumps(resp, ensure_ascii=False),
                media_type="application/json",
                status_code=400,
            )

        # --- 4. Route ---
        # Имена методов — по спецификации MCP; старые (list_tools/call_tool)
        # поддерживаются как алиасы для обратной совместимости.
        if method == "initialize":
            resp = await _handle_initialize(req_id, agent, params)
        elif method in ("tools/list", "list_tools"):
            resp = await _handle_list_tools(req_id, agent, params)
        elif method in ("tools/call", "call_tool"):
            resp = await _handle_call_tool(req_id, agent, params, db)
        elif method == "ping":
            resp = await _handle_ping(req_id)
        elif method.startswith("notifications/"):
            # Уведомления (initialized и любые другие) не имеют ответа
            return Response(status_code=202)
        else:
            resp = json_rpc_error(
                req_id, -32601, f"Method not found: {method}"
            )

        return Response(
            content=json.dumps(resp, ensure_ascii=False),
            media_type="application/json",
        )


@router.delete("/mcp")
async def mcp_session_terminate(request: Request) -> Response:
    """Streamable HTTP: завершение сессии. У нас сервер stateless — 204."""
    return Response(status_code=204)