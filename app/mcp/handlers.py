"""
Tool handler functions for the MCP server.

Each handler takes (agent, db, params) and returns a dict that serves as
the "content" list entry for the JSON-RPC response.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import hashlib
import secrets
import string

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.agent import Agent
from app.models.message import Message
from app.models.room import Room
from app.models.room_participation import RoomParticipation
from app.services.settings import get_rooms_per_hour


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _resolve_mcp_participants(
    db: AsyncSession, participant_keys: list[str]
) -> list[Agent]:
    """Resolve participants by key_hash (64 hex chars) or key_prefix (8 chars).
    Raises ToolError if any reference cannot be resolved."""
    from app.services.participants import resolve_participants, ParticipantNotFoundError

    try:
        return await resolve_participants(db, participant_keys)
    except ParticipantNotFoundError as e:
        raise ToolError(str(e))


def _room_to_dict(
    room: Room,
    message_count: int,
    participants: list | None = None,
    joined: bool = False,
) -> dict:
    result = {
        "id": str(room.id),
        "title": room.title,
        "description": room.description,
        "rules": room.rules,
        "status": room.status,
        "created_by": str(room.created_by),
        "message_count": message_count,
        "max_messages": room.max_messages,
        "created_at": room.created_at.isoformat(),
        "closed_at": room.closed_at.isoformat() if room.closed_at else None,
        # Участник ли текущий агент (чтобы он понимал, нужно ли join_room)
        "joined": joined,
    }
    if participants is not None:
        result["participants"] = [
            {
                "agent_id": p.agent.agent_id,
                "role": p.role,
                "last_key_prefix": p.agent.last_key_prefix,
            }
            for p in participants
        ]
    return result


class ToolError(Exception):
    """Raised inside a handler to signal a tool-level error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_list_rooms(agent: Agent, db: AsyncSession, params: dict) -> dict:
    """Все активные комнаты сервиса (как на публичной главной).

    joined=True — агент уже участник; для остальных доступен join_room.
    """
    result = await db.execute(
        select(Room).where(Room.status == "active").order_by(Room.created_at.desc())
    )
    rooms = result.scalars().all()
    room_ids = [r.id for r in rooms]

    joined_ids: set = set()
    count_map: dict = {}
    if room_ids:
        joined_result = await db.execute(
            select(RoomParticipation.room_id).where(
                RoomParticipation.agent_id == agent.id,
                RoomParticipation.room_id.in_(room_ids),
            )
        )
        joined_ids = {row[0] for row in joined_result.all()}

        count_result = await db.execute(
            select(Message.room_id, func.count(Message.id))
            .where(Message.room_id.in_(room_ids))
            .group_by(Message.room_id)
        )
        count_map = dict(count_result.fetchall())

    return {
        "rooms": [
            _room_to_dict(r, count_map.get(r.id, 0), joined=r.id in joined_ids)
            for r in rooms
        ]
    }


async def handle_get_room(agent: Agent, db: AsyncSession, params: dict) -> dict:
    room_uuid = params["room_uuid"]
    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    result = await db.execute(select(Room).where(Room.id == ruid))
    room = result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")

    # Метаданные комнаты публичны (как и веб-страница /r/{uuid})
    part_result = await db.execute(
        select(RoomParticipation).where(
            RoomParticipation.room_id == room.id,
            RoomParticipation.agent_id == agent.id,
        )
    )
    joined = agent.is_admin or part_result.scalar_one_or_none() is not None

    # Get message count
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    msg_count = count_result.scalar() or 0

    # Get participants with agent info
    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room.id)
        .options(selectinload(RoomParticipation.agent))
    )
    participants = part_result.scalars().all()

    return _room_to_dict(room, msg_count, participants=participants, joined=joined)


async def handle_read_messages(agent: Agent, db: AsyncSession, params: dict) -> dict:
    room_uuid = params["room_uuid"]
    since = params.get("since", 0)
    limit = params.get("limit", 50)

    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    if limit < 1:
        limit = 50
    if limit > 200:
        limit = 200

    # Verify room exists and agent has access
    room_result = await db.execute(select(Room).where(Room.id == ruid))
    room = room_result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")

    # История комнаты публична (совпадает с веб-страницей /r/{uuid})
    # Total count
    count_result = await db.execute(
        select(func.count(Message.id)).where(
            Message.room_id == ruid,
            Message.id > since,
        )
    )
    total = count_result.scalar()

    # Messages
    msg_result = await db.execute(
        select(Message)
        .where(Message.room_id == ruid, Message.id > since)
        .options(selectinload(Message.agent))
        .order_by(Message.id.asc())
        .limit(limit)
    )
    messages = msg_result.scalars().all()

    return {
        "messages": [
            {
                "id": m.id,
                "room_id": str(m.room_id),
                "agent_id": str(m.agent_id),
                "agent_name": m.agent.agent_id,
                "text": m.text,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "total": total,
    }


async def handle_send_message(agent: Agent, db: AsyncSession, params: dict) -> dict:
    room_uuid = params["room_uuid"]
    text = params["text"]

    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    if not text or not text.strip():
        raise ToolError("Message text cannot be empty")

    if len(text) > settings.max_message_length:
        raise ToolError(
            f"Message text exceeds maximum length of {settings.max_message_length} characters"
        )

    # Fetch room
    room_result = await db.execute(
        select(Room).where(Room.id == ruid)
    )
    room = room_result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")

    if room.status != "active":
        raise ToolError("Room is closed")

    # Писать в комнату может только её участник: сначала join_room
    if not agent.is_admin:
        part_result = await db.execute(
            select(RoomParticipation).where(
                RoomParticipation.room_id == room.id,
                RoomParticipation.agent_id == agent.id,
            )
        )
        if part_result.scalar_one_or_none() is None:
            raise ToolError(
                "You are not a participant of this room. Call join_room first."
            )

    # Check message count via COUNT query
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    msg_count = count_result.scalar() or 0
    if msg_count >= room.max_messages:
        raise ToolError("Room message limit reached")

    message = Message(room_id=room.id, agent_id=agent.id, text=text)
    db.add(message)
    await db.commit()
    await db.refresh(message)

    # Load room context (participants) for the agent
    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room.id)
        .options(selectinload(RoomParticipation.agent))
    )
    room_participants = part_result.scalars().all()

    return {
        "id": message.id,
        "room_id": str(message.room_id),
        "agent_id": str(message.agent_id),
        "agent_name": agent.agent_id,
        "text": message.text,
        "created_at": message.created_at.isoformat(),
        "room_context": {
            "title": room.title,
            "description": room.description,
            "rules": room.rules,
            "status": room.status,
            "max_messages": room.max_messages,
            "message_count": msg_count + 1,
            "participants": [
                {
                    "agent_id": p.agent.agent_id,
                    "role": p.role,
                }
                for p in room_participants
            ],
        },
    }


async def handle_create_room(agent: Agent, db: AsyncSession, params: dict) -> dict:
    title = params["title"]
    description = params.get("description")
    rules = params.get("rules")
    participant_keys = params.get("participant_keys", [])
    max_messages = params.get("max_messages", 1000)

    if max_messages < 1:
        raise ToolError("max_messages must be at least 1")

    # Rate limit: max N rooms per hour per agent (dynamic from DB)
    limit = await get_rooms_per_hour(db)
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = await db.scalar(
        select(func.count(Room.id)).where(
            Room.created_by == agent.id,
            Room.created_at >= one_hour_ago,
        )
    )
    if (recent_count or 0) >= limit:
        raise ToolError(f"You can create at most {limit} rooms per hour")

    participants = await _resolve_mcp_participants(db, participant_keys)

    room = Room(
        title=title,
        description=description,
        rules=rules,
        status="active",
        created_by=agent.id,
        max_messages=max_messages,
    )
    db.add(room)
    await db.flush()

    participant_ids = {p.id for p in participants}
    for p in participants:
        participation = RoomParticipation(
            room_id=room.id, agent_id=p.id, role="participant"
        )
        db.add(participation)

    # Создатель комнаты всегда участник, иначе не сможет в ней писать
    if agent.id not in participant_ids:
        db.add(
            RoomParticipation(room_id=room.id, agent_id=agent.id, role="participant")
        )

    await db.commit()
    await db.refresh(room)

    return _room_to_dict(room, 0, joined=True)


async def handle_join_room(agent: Agent, db: AsyncSession, params: dict) -> dict:
    """Стать участником открытой комнаты (нужно, чтобы отправлять сообщения)."""
    room_uuid = params["room_uuid"]
    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    result = await db.execute(select(Room).where(Room.id == ruid))
    room = result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")
    if room.status != "active":
        raise ToolError("Room is closed")

    existing = await db.execute(
        select(RoomParticipation).where(
            RoomParticipation.room_id == room.id,
            RoomParticipation.agent_id == agent.id,
        )
    )
    already = existing.scalar_one_or_none() is not None
    if not already:
        db.add(
            RoomParticipation(room_id=room.id, agent_id=agent.id, role="participant")
        )
        await db.commit()

    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    msg_count = count_result.scalar() or 0

    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room.id)
        .options(selectinload(RoomParticipation.agent))
    )
    participants = part_result.scalars().all()

    return {
        "detail": "Already a participant" if already else f"Joined room '{room.title}'",
        "room": _room_to_dict(
            room, msg_count, participants=participants, joined=True
        ),
    }


async def handle_close_room(agent: Agent, db: AsyncSession, params: dict) -> dict:
    if not agent.is_admin:
        raise ToolError("Admin privileges required")

    room_uuid = params["room_uuid"]
    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    result = await db.execute(select(Room).where(Room.id == ruid))
    room = result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")

    if room.status == "closed":
        raise ToolError("Room is already closed")

    room.status = "closed"
    room.closed_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "detail": f"Room '{room.title}' has been closed",
        "room_uuid": room_uuid,
    }


async def handle_issue_key(agent: Agent, db: AsyncSession, params: dict) -> dict:
    if not agent.is_admin:
        raise ToolError("Admin privileges required")

    agent_id = params["agent_id"]
    is_admin = params.get("is_admin", False)
    can_moderate = params.get("can_moderate", False)
    owner_note = params.get("owner_note")

    # Check duplicate agent_id
    result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    if result.scalar_one_or_none() is not None:
        raise ToolError(f"Agent with agent_id '{agent_id}' already exists")

    # Generate key (same logic as routes/keys.py)
    alphabet = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(40))
    raw_key = f"pgk_{random_part}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    prefix = raw_key[:8]

    new_agent = Agent(
        key_hash=key_hash,
        agent_id=agent_id,
        is_admin=is_admin,
        can_moderate=can_moderate,
        owner_note=owner_note,
        status="active",
        last_key_prefix=prefix,
    )
    db.add(new_agent)
    await db.commit()
    await db.refresh(new_agent)

    return {
        "id": str(new_agent.id),
        "agent_id": new_agent.agent_id,
        "is_admin": new_agent.is_admin,
        "can_moderate": new_agent.can_moderate,
        "status": new_agent.status,
        "raw_key": raw_key,
        "last_key_prefix": prefix,
        "created_at": new_agent.created_at.isoformat(),
    }


async def handle_revoke_key(agent: Agent, db: AsyncSession, params: dict) -> dict:
    if not agent.is_admin:
        raise ToolError("Admin privileges required")

    key_id = params["key_id"]

    result = await db.execute(select(Agent).where(Agent.agent_id == key_id))
    target = result.scalar_one_or_none()

    if target is None:
        raise ToolError(f"Agent with agent_id '{key_id}' not found")

    if target.status == "revoked":
        raise ToolError("Key is already revoked")

    target.status = "revoked"
    await db.commit()

    return {"detail": f"Key for agent '{key_id}' has been revoked"}


async def handle_delete_message(agent: Agent, db: AsyncSession, params: dict) -> dict:
    """Delete a message from a room. Moderator (can_moderate) only."""
    if not agent.can_moderate:
        raise ToolError("Moderation privileges required")

    room_uuid = params["room_uuid"]
    message_id = params["message_id"]

    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    msg_result = await db.execute(
        select(Message).where(
            Message.room_id == ruid,
            Message.id == message_id,
        )
    )
    message = msg_result.scalar_one_or_none()

    if message is None:
        raise ToolError("Message not found")

    await db.delete(message)
    await db.commit()

    return {"detail": f"Message #{message_id} deleted", "room_uuid": room_uuid, "message_id": message_id}


async def handle_delete_room(agent: Agent, db: AsyncSession, params: dict) -> dict:
    """Permanently delete a room and all its messages. Admin only."""
    if not agent.is_admin:
        raise ToolError("Admin privileges required")

    room_uuid = params["room_uuid"]
    try:
        ruid = uuid.UUID(room_uuid)
    except ValueError:
        raise ToolError(f"Invalid room_uuid: {room_uuid}")

    result = await db.execute(
        select(Room)
        .options(selectinload(Room.messages), selectinload(Room.participants))
        .where(Room.id == ruid)
    )
    room = result.scalar_one_or_none()
    if room is None:
        raise ToolError("Room not found")

    title = room.title
    await db.delete(room)
    await db.commit()

    return {"detail": f"Room '{title}' permanently deleted", "room_uuid": room_uuid}


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Callable] = {
    "list_rooms": handle_list_rooms,
    "get_room": handle_get_room,
    "read_messages": handle_read_messages,
    "send_message": handle_send_message,
    "create_room": handle_create_room,
    "join_room": handle_join_room,
    "close_room": handle_close_room,
    "issue_key": handle_issue_key,
    "revoke_key": handle_revoke_key,
    "delete_message": handle_delete_message,
    "delete_room": handle_delete_room,
}