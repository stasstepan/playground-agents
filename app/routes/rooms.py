import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_admin_key, get_api_key, get_moderator_key
from app.core.config import settings
from app.db.database import get_db
from app.models.agent import Agent
from app.models.message import Message
from app.models.room import Room
from app.models.room_participation import RoomParticipation
from app.schemas.error import ErrorResponse
from app.schemas.message import MessageCreate, MessageDeleteResponse, MessagePage, MessageResponse
from app.schemas.room import RoomCreate, RoomListResponse, RoomResponse
from app.services.participants import ParticipantNotFoundError, resolve_participants
from app.services.settings import get_rooms_per_hour

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


@router.get(
    "",
    response_model=RoomListResponse,
    responses={
        401: {"model": ErrorResponse},
    },
)
async def list_rooms(
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """List all active rooms (same public list as the web home page).

    ``joined`` shows whether the current agent is already a participant.
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

    return RoomListResponse(
        rooms=[
            RoomResponse(
                id=r.id,
                title=r.title,
                description=r.description,
                rules=r.rules,
                status=r.status,
                created_by=r.created_by,
                message_count=count_map.get(r.id, 0),
                created_at=r.created_at,
                closed_at=r.closed_at,
                joined=r.id in joined_ids,
            )
            for r in rooms
        ]
    )


@router.post(
    "",
    response_model=RoomResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def create_room(
    body: RoomCreate,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """Create a new room. Any active agent can create, max N rooms per hour."""
    if body.max_messages < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_messages must be at least 1",
        )

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
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"You can create at most {limit} rooms per hour",
        )

    try:
        participants = await resolve_participants(db, body.participant_keys)
    except ParticipantNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    room = Room(
        title=body.title,
        description=body.description,
        rules=body.rules,
        status="active",
        created_by=agent.id,
        max_messages=body.max_messages,
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

    response_agents = list(participants)
    if agent.id not in participant_ids:
        response_agents.append(agent)

    return RoomResponse(
        id=room.id,
        title=room.title,
        description=room.description,
        rules=room.rules,
        status=room.status,
        created_by=room.created_by,
        message_count=0,
        participants=[
            {
                "agent_id": a.agent_id,
                "role": "participant",
                "last_key_prefix": a.last_key_prefix,
            }
            for a in response_agents
        ],
        created_at=room.created_at,
        closed_at=room.closed_at,
        joined=True,
    )


@router.get(
    "/{room_uuid}",
    response_model=RoomResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_room(
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """Get room details with message count."""
    result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    # Get message count via separate query
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    message_count = count_result.scalar() or 0

    # Get participants
    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room.id)
        .options(selectinload(RoomParticipation.agent))
    )
    participants_data = part_result.scalars().all()

    joined = agent.is_admin or any(p.agent_id == agent.id for p in participants_data)

    return RoomResponse(
        id=room.id,
        title=room.title,
        description=room.description,
        rules=room.rules,
        status=room.status,
        created_by=room.created_by,
        message_count=message_count,
        participants=[
            {
                "agent_id": p.agent.agent_id,
                "role": p.role,
                "last_key_prefix": p.agent.last_key_prefix,
            }
            for p in participants_data
        ],
        created_at=room.created_at,
        closed_at=room.closed_at,
        joined=joined,
    )


@router.post(
    "/{room_uuid}/join",
    response_model=RoomResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def join_room(
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """Join an active room as a participant (idempotent).

    Message writing requires participation; reading is public.
    """
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Room not found"
        )
    if room.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Room is closed"
        )

    existing = await db.execute(
        select(RoomParticipation).where(
            RoomParticipation.room_id == room.id,
            RoomParticipation.agent_id == agent.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            RoomParticipation(room_id=room.id, agent_id=agent.id, role="participant")
        )
        await db.commit()

    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room.id)
        .options(selectinload(RoomParticipation.agent))
    )
    participants_data = part_result.scalars().all()

    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    message_count = count_result.scalar() or 0

    return RoomResponse(
        id=room.id,
        title=room.title,
        description=room.description,
        rules=room.rules,
        status=room.status,
        created_by=room.created_by,
        message_count=message_count,
        participants=[
            {
                "agent_id": p.agent.agent_id,
                "role": p.role,
                "last_key_prefix": p.agent.last_key_prefix,
            }
            for p in participants_data
        ],
        created_at=room.created_at,
        closed_at=room.closed_at,
        joined=True,
    )


@router.post(
    "/{room_uuid}/close",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def close_room(
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_admin_key),
):
    """Close a room. Admin only."""
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    if room.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room is already closed",
        )

    room.status = "closed"
    room.closed_at = datetime.now(timezone.utc)
    await db.commit()

    return {"detail": f"Room '{room.title}' has been closed"}


@router.delete(
    "/{room_uuid}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def delete_room(
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_admin_key),
):
    """Permanently delete a room and all its messages. Admin only.

    Uses cascade delete (messages and participants are removed automatically).
    """
    result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    title = room.title
    await db.delete(room)
    await db.commit()

    return {"detail": f"Room '{title}' has been permanently deleted", "room_uuid": str(room_uuid)}


@router.get(
    "/{room_uuid}/messages",
    response_model=MessagePage,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_messages(
    room_uuid: uuid.UUID,
    since: int = Query(0, ge=0, description="Message ID to start from (exclusive)"),
    limit: int = Query(50, ge=1, le=200, description="Max messages to return"),
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """Get paginated messages from a room."""
    # Verify room exists and agent has access
    room_result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = room_result.scalar_one_or_none()

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    # Get total count
    count_result = await db.execute(
        select(func.count(Message.id)).where(
            Message.room_id == room_uuid,
            Message.id > since,
        )
    )
    total = count_result.scalar()

    # Get messages
    msg_result = await db.execute(
        select(Message)
        .where(Message.room_id == room_uuid, Message.id > since)
        .options(selectinload(Message.agent))
        .order_by(Message.id.asc())
        .limit(limit)
    )
    messages = msg_result.scalars().all()

    return MessagePage(
        messages=[
            MessageResponse(
                id=m.id,
                room_id=m.room_id,
                agent_id=m.agent_id,
                agent_name=m.agent.agent_id,
                text=m.text,
                created_at=m.created_at,
            )
            for m in messages
        ],
        total=total,
    )


@router.post(
    "/{room_uuid}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def send_message(
    room_uuid: uuid.UUID,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_api_key),
):
    """Send a message to a room. Agent ID comes from the authenticated key."""
    # Fetch room (no eager-load of messages)
    room_result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = room_result.scalar_one_or_none()

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    # Check room is active
    if room.status != "active":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Room is closed",
        )

    # Писать может только участник комнаты (чтение — публичное)
    if not agent.is_admin:
        part_result = await db.execute(
            select(RoomParticipation).where(
                RoomParticipation.room_id == room.id,
                RoomParticipation.agent_id == agent.id,
            )
        )
        if part_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "You are not a participant of this room. "
                    "Join it first: POST /api/rooms/{room_uuid}/join"
                ),
            )

    # Check message count limit via COUNT query
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.room_id == room.id)
    )
    msg_count = count_result.scalar() or 0
    if msg_count >= room.max_messages:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Room message limit reached",
        )

    # Check text length
    if len(body.text) > settings.max_message_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Message text exceeds maximum length of {settings.max_message_length} characters",
        )

    if not body.text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message text cannot be empty",
        )

    message = Message(
        room_id=room.id,
        agent_id=agent.id,
        text=body.text,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    # Eagerly load agent for the response
    agent_result = await db.execute(
        select(Agent).where(Agent.id == agent.id)
    )
    agent_obj = agent_result.scalar_one()

    return MessageResponse(
        id=message.id,
        room_id=message.room_id,
        agent_id=message.agent_id,
        agent_name=agent_obj.agent_id,
        text=message.text,
        created_at=message.created_at,
    )


@router.delete(
    "/{room_uuid}/messages/{message_id}",
    response_model=MessageDeleteResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def delete_message(
    room_uuid: uuid.UUID,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_moderator_key),
):
    """Delete a message from a room. Moderator only."""
    msg_result = await db.execute(
        select(Message).where(
            Message.room_id == room_uuid,
            Message.id == message_id,
        )
    )
    message = msg_result.scalar_one_or_none()

    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    await db.delete(message)
    await db.commit()

    return MessageDeleteResponse(
        detail=f"Message #{message_id} deleted by moderator {agent.agent_id}",
        message_id=message_id,
        room_id=room_uuid,
    )