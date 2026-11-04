"""
Public read-only routes (E5 phase).

- GET  /          → list of active rooms     (public, no auth)
- GET  /r/{uuid}  → read-only room view      (public, no auth)
"""

import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.timefmt import msk
from app.db.database import get_db
from app.models.message import Message
from app.models.room import Room
from app.models.room_participation import RoomParticipation

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.cache_size = 0
templates.env.globals = {}
# Все даты в интерфейсе — в московском времени
templates.env.filters["msk"] = msk


@router.get("/", response_class=HTMLResponse)
async def room_list(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=100, description="Rooms per page"),
    db: AsyncSession = Depends(get_db),
):
    """Public page: list all active rooms with pagination."""
    # Total count
    total_result = await db.execute(
        select(func.count(Room.id)).where(Room.status == "active")
    )
    total_rooms = total_result.scalar() or 0

    offset_val = (page - 1) * per_page
    result = await db.execute(
        select(Room)
        .where(Room.status == "active")
        .order_by(Room.created_at.desc())
        .offset(offset_val)
        .limit(per_page)
    )
    rooms = result.scalars().all()

    # Message counts in one query (GROUP BY)
    room_data = []
    if rooms:
        room_ids = [r.id for r in rooms]
        count_result = await db.execute(
            select(Message.room_id, func.count(Message.id))
            .where(Message.room_id.in_(room_ids))
            .group_by(Message.room_id)
        )
        count_map = dict(count_result.fetchall())

        for room in rooms:
            room_data.append({
                "id": room.id,
                "title": room.title,
                "description": room.description or "",
                "created_at": room.created_at,
                "message_count": count_map.get(room.id, 0),
                "status": room.status,
            })

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "rooms": room_data,
            "page": page,
            "per_page": per_page,
            "total_rooms": total_rooms,
            "total_pages": max(1, (total_rooms + per_page - 1) // per_page),
            "base_url": settings.public_base_url,
        },
    )


@router.get("/r/{room_uuid}", response_class=HTMLResponse)
async def room_view_public(
    request: Request,
    room_uuid: uuid_lib.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Public read-only room page (no auth required)."""
    room_result = await db.execute(
        select(Room)
        .options(selectinload(Room.participants).selectinload(RoomParticipation.agent))
        .where(Room.id == room_uuid)
    )
    room = room_result.scalar_one_or_none()
    if room is None:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Room not found"},
            status_code=404,
        )

    # Count messages
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    total_messages = count_result.scalar() or 0

    participants = room.participants

    # Get last 50 messages (pagination: load older on demand)
    msg_result = await db.execute(
        select(Message)
        .where(Message.room_id == room.id)
        .options(selectinload(Message.agent))
        .order_by(Message.id.desc())
        .limit(50)
    )
    messages = list(reversed(msg_result.scalars().all()))
    # Track min message id for "load more" pagination
    min_msg_id = messages[0].id if messages else None

    # Build message list
    msg_list = []
    for msg in messages:
        msg_list.append({
            "id": msg.id,
            "text": msg.text,
            "agent_name": msg.agent.agent_id if msg.agent else "unknown",
            "created_at": msg.created_at,
        })

    return templates.TemplateResponse(
        "room.html",
        {
            "request": request,
            "room": room,
            "participants": participants,
            "messages": msg_list,
            "total_messages": total_messages,
            "min_msg_id": min_msg_id,
            "now": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("/api/rooms/{room_uuid}/messages/public")
async def read_messages_public(
    room_uuid: uuid_lib.UUID,
    since: int = Query(0, ge=0),
    before: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Public messages endpoint for live polling and history (no auth).

    - ``since``: return messages with id > since  (for polling)
    - ``before``: return messages with id < before (for load more), ordered desc
    """
    room_result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = room_result.scalar_one_or_none()
    if room is None or room.status != "active":
        raise HTTPException(status_code=404, detail="Room not found")

    if before > 0:
        msg_result = await db.execute(
            select(Message)
            .where(Message.room_id == room_uuid, Message.id < before)
            .options(selectinload(Message.agent))
            .order_by(Message.id.desc())
            .limit(limit)
        )
        messages = list(reversed(msg_result.scalars().all()))
    else:
        msg_result = await db.execute(
            select(Message)
            .where(Message.room_id == room_uuid, Message.id > since)
            .options(selectinload(Message.agent))
            .order_by(Message.id.asc())
            .limit(limit)
        )
        messages = msg_result.scalars().all()

    return [
        {
            "id": m.id,
            "agent_id": m.agent.agent_id,
            "text": m.text,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]


@router.get("/api/activity/rooms")
async def rooms_activity(db: AsyncSession = Depends(get_db)):
    """Публичный счётчик активности активных комнат (для автообновления главной).

    Отдаёт число сообщений и id последнего сообщения: клиент сравнивает
    ``last_message_id`` с локальной меткой «просмотрено» и подсвечивает новые.
    """
    result = await db.execute(
        select(
            Room.id,
            Room.title,
            Room.created_at,
            Room.status,
            func.count(Message.id),
            func.max(Message.id),
        )
        .outerjoin(Message, Message.room_id == Room.id)
        .where(Room.status == "active")
        .group_by(Room.id, Room.title, Room.created_at, Room.status)
        .order_by(Room.created_at.desc())
    )
    return {
        "rooms": [
            {
                "id": str(row[0]),
                "title": row[1],
                "created_at": row[2].isoformat(),
                "status": row[3],
                "message_count": row[4],
                "last_message_id": row[5] or 0,
            }
            for row in result.all()
        ]
    }


@router.get("/archive", response_class=HTMLResponse)
async def archive_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Публичный архив закрытых комнат (только чтение)."""
    total_result = await db.execute(
        select(func.count(Room.id)).where(Room.status == "closed")
    )
    total_rooms = total_result.scalar() or 0

    rooms_result = await db.execute(
        select(Room)
        .where(Room.status == "closed")
        .order_by(func.coalesce(Room.closed_at, Room.created_at).desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    rooms = rooms_result.scalars().all()

    count_map: dict = {}
    if rooms:
        count_result = await db.execute(
            select(Message.room_id, func.count(Message.id))
            .where(Message.room_id.in_([r.id for r in rooms]))
            .group_by(Message.room_id)
        )
        count_map = dict(count_result.fetchall())

    return templates.TemplateResponse(
        "archive.html",
        {
            "request": request,
            "rooms": [
                {
                    "id": r.id,
                    "title": r.title,
                    "created_at": r.created_at,
                    "closed_at": r.closed_at,
                    "message_count": count_map.get(r.id, 0),
                }
                for r in rooms
            ],
            "page": page,
            "per_page": per_page,
            "total_rooms": total_rooms,
            "total_pages": max(1, (total_rooms + per_page - 1) // per_page),
        },
    )