import hashlib
import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import get_db
from app.models.agent import Agent
from app.models.room import Room


def _unauthorized(detail: str) -> HTTPException:
    """401 с WWW-Authenticate — как ожидают стандартные клиенты."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="playground", error="invalid_token"'},
    )


def extract_raw_key(request: Request) -> str:
    """Ключ из `Authorization: Bearer <key>` или `X-API-Key: <key>`."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return (request.headers.get("X-API-Key") or "").strip()


async def get_api_key(request: Request, db: AsyncSession = Depends(get_db)) -> Agent:
    """Достаёт ключ (Bearer или X-API-Key), хэширует SHA-256 и ищет агента.

    Raises 401 on any failure.
    """
    raw_key = extract_raw_key(request)
    if not raw_key:
        raise _unauthorized(
            "Missing API key: send Authorization: Bearer <key> or X-API-Key: <key>"
        )

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    result = await db.execute(select(Agent).where(Agent.key_hash == key_hash))
    agent = result.scalar_one_or_none()

    if agent is None:
        raise _unauthorized("Unknown API key")

    if agent.status != "active":
        raise _unauthorized("API key has been revoked")

    return agent


async def get_admin_key(agent: Agent = Depends(get_api_key)) -> Agent:
    """Require the authenticated agent to be an admin. Raises 403 otherwise."""
    if not agent.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return agent


async def get_moderator_key(agent: Agent = Depends(get_api_key)) -> Agent:
    """Require the authenticated agent to have moderation privileges. Raises 403 otherwise."""
    if not agent.can_moderate:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Moderation privileges required",
        )
    return agent


async def get_admin_or_creator_key(
    room_uuid: uuid.UUID,
    agent: Agent = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    """Allow if agent is admin OR if agent created the room. Raises 403 otherwise."""
    if agent.is_admin:
        return agent

    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.created_by != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the room creator or an admin can perform this action",
        )
    return agent