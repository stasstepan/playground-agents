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


async def get_api_key(request: Request, db: AsyncSession = Depends(get_db)) -> Agent:
    """Extract Bearer token from Authorization header, hash it with SHA-256,
    and look up the agent by key_hash. Raises 401 on any failure."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )

    raw_key = auth_header.removeprefix("Bearer ").strip()
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty API key",
        )

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    result = await db.execute(select(Agent).where(Agent.key_hash == key_hash))
    agent = result.scalar_one_or_none()

    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown API key",
        )

    if agent.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has been revoked",
        )

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