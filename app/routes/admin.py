"""
Admin web panel routes (Jinja2 + HTMX).

Session auth via signed httpOnly cookie using HMAC.
"""

import hashlib
import hmac
import json
import logging
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.timefmt import msk
from app.db.database import get_db
from app.models.agent import Agent
from app.models.message import Message
from app.models.room import Room
from app.models.room_participation import RoomParticipation
from app.models.site_settings import SiteSettings
from app.services.keys import generate_raw_key, hash_key, key_prefix
from app.services.settings import get_rooms_per_hour, set_rooms_per_hour

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.cache_size = 0
# явно инициализируем globals пустым словарём для hashable cache key
templates.env.globals = {}
# Все даты в интерфейсе — в московском времени
templates.env.filters["msk"] = msk

# ---------------------------------------------------------------------------
# Session helpers (HMAC-signed cookie)
# ---------------------------------------------------------------------------

COOKIE_NAME = "playground_admin"
SESSION_TTL = timedelta(hours=24)


def _sign_data(data: str) -> str:
    """Sign data with HMAC-SHA256 using settings.secret_key."""
    return hmac.new(
        settings.secret_key.encode(), data.encode(), hashlib.sha256
    ).hexdigest()


def _create_session_token(username: str) -> str:
    """Create a signed session token: timestamp|username|signature."""
    expires = (datetime.now(timezone.utc) + SESSION_TTL).timestamp()
    payload = f"{expires}|{username}"
    sig = _sign_data(payload)
    return f"{payload}|{sig}"


def _verify_session_token(token: str) -> Optional[str]:
    """Verify session token. Returns username if valid, None otherwise."""
    try:
        parts = token.split("|")
        if len(parts) != 3:
            return None
        expires_str, username, sig = parts
        payload = f"{expires_str}|{username}"
        expected_sig = _sign_data(payload)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        expires = float(expires_str)
        if datetime.now(timezone.utc).timestamp() > expires:
            return None
        return username
    except (ValueError, IndexError):
        return None


def _get_admin_username(request: Request) -> Optional[str]:
    """Extract admin username from cookie, or None if not authenticated."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _verify_session_token(token)


def _redirect_login(flash: str = "") -> RedirectResponse:
    """Redirect to login page, optionally with a flash message."""
    url = "/admin/login"
    if flash:
        url += f"?flash={flash}"
    return RedirectResponse(url=url, status_code=303)


# ---------------------------------------------------------------------------
# Auth middleware helper
# ---------------------------------------------------------------------------

async def get_current_admin(request: Request) -> str:
    """Dependency: validate admin session. Returns admin username or raises 303 redirect."""
    username = _get_admin_username(request)
    if username is None:
        raise HTTPException(status_code=303, detail="", headers={"Location": "/admin/login"})
    # Expose for logging middleware
    request.state.admin_user = username
    return username


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, flash: str = Query("")):
    """Render admin login page."""
    # If already logged in, redirect to dashboard
    if _get_admin_username(request) is not None:
        return RedirectResponse(url="/admin", status_code=303)

    error = flash if flash else ""
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": error},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Verify credentials and set session cookie."""
    # Сверяем с settings.admin_user и bcrypt-хэшем settings.admin_hash (plain text не поддерживается)
    if username != settings.admin_user:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Неверное имя пользователя или пароль"},
            status_code=401,
        )

    logger = logging.getLogger(__name__)

    # Пароль администратора хранится ТОЛЬКО как bcrypt-хэш (ADMIN_HASH).
    # Plain-text пароль не поддерживается: .env с паролем в открытом виде — дыра.
    password_valid = False
    admin_hash = (settings.admin_hash or "").strip()

    if not admin_hash:
        logger.warning("ADMIN_HASH is empty — admin login disabled.")
    elif admin_hash.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            import bcrypt as bcrypt_lib

            password_valid = bcrypt_lib.checkpw(
                password.encode("utf-8"), admin_hash.encode("utf-8")
            )
        except (ImportError, ValueError, TypeError):
            logger.warning("ADMIN_HASH is not a valid bcrypt hash — admin login disabled.")
            password_valid = False
    else:
        logger.warning("ADMIN_HASH is not a bcrypt hash (must start with $2b$) — admin login disabled.")
        password_valid = False

    if not password_valid:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Неверное имя пользователя или пароль"},
            status_code=401,
        )

    # Create session
    token = _create_session_token(username)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        path="/",
        max_age=int(SESSION_TTL.total_seconds()),
        secure=settings.session_secure,
        samesite="strict",
    )
    return response


@router.get("/logout")
async def logout():
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    q: str = Query("", description="Search/filter query"),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Render admin dashboard with stats and room list."""
    # Stats
    total_rooms_result = await db.execute(select(func.count(Room.id)))
    total_rooms = total_rooms_result.scalar() or 0

    active_rooms_result = await db.execute(
        select(func.count(Room.id)).where(Room.status == "active")
    )
    active_rooms = active_rooms_result.scalar() or 0

    active_keys_result = await db.execute(
        select(func.count(Agent.id)).where(Agent.status == "active")
    )
    active_keys = active_keys_result.scalar() or 0

    # Room list (без eager-load сообщений — используем подзапрос для message_count)
    msg_count_subq = (
        select(Message.room_id, func.count(Message.id).label("cnt"))
        .group_by(Message.room_id)
        .subquery()
    )
    rooms_query = (
        select(Room, func.coalesce(msg_count_subq.c.cnt, 0).label("msg_count"))
        .outerjoin(msg_count_subq, Room.id == msg_count_subq.c.room_id)
        .order_by(Room.created_at.desc())
    )

    if q:
        rooms_query = rooms_query.where(Room.title.ilike(f"%{q}%"))

    rooms_result = await db.execute(rooms_query)
    rows = rooms_result.all()
    rooms_with_count = [{"room": r, "msg_count": cnt} for r, cnt in rows]

    # Flash message from query param
    flash = None
    flash_param = request.query_params.get("flash")
    if flash_param:
        flash = {"type": "success", "message": flash_param}

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "stats": {
                "active_rooms": active_rooms,
                "total_rooms": total_rooms,
                "active_keys": active_keys,
            },
            "rooms": rooms_with_count,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Room management
# ---------------------------------------------------------------------------

@router.get("/rooms/new", response_class=HTMLResponse)
async def room_new_form(
    request: Request,
    admin_username: str = Depends(get_current_admin),
):
    """Render create room form."""
    flash = None
    flash_param = request.query_params.get("flash")
    if flash_param:
        flash = {"type": "error", "message": flash_param}

    return templates.TemplateResponse(
        "admin/room_new.html",
        {"request": request, "flash": flash},
    )


async def _resolve_participant_by_prefix(
    db: AsyncSession, prefix: str
) -> Optional[Agent]:
    """Resolve a participant by key prefix (8 chars) or full key_hash (64 hex)."""
    ref = (prefix or "").strip()
    if not ref:
        return None
    if len(ref) == 64:
        stmt = select(Agent).where(Agent.key_hash == ref, Agent.status == "active")
    else:
        stmt = select(Agent).where(
            Agent.last_key_prefix == ref, Agent.status == "active"
        )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@router.post("/rooms/new")
async def room_new_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    roles: str = Form(""),
    format: str = Form(""),
    completion_criteria: str = Form(""),
    max_messages: int = Form(1000),
    participant_keys: str = Form(""),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Create a new room."""
    # Get the admin agent (creator)
    admin_result = await db.execute(
        select(Agent).where(Agent.agent_id == admin_username, Agent.status == "active")
    )
    admin_agent = admin_result.scalar_one_or_none()

    # If the admin agent doesn't exist yet, we'll use the first admin agent found
    if admin_agent is None:
        admin_result = await db.execute(
            select(Agent).where(Agent.is_admin.is_(True), Agent.status == "active").limit(1)
        )
        admin_agent = admin_result.scalar_one_or_none()

    if admin_agent is None:
        # Create a dummy admin agent for the panel user
        import hashlib as hl
        random_key = f"pgk_{secrets.token_hex(20)}"
        key_hash = hl.sha256(random_key.encode()).hexdigest()
        admin_agent = Agent(
            key_hash=key_hash,
            agent_id=admin_username,
            is_admin=True,
            status="active",
            owner_note="Admin panel auto-created",
            last_key_prefix=random_key[:8],
        )
        db.add(admin_agent)
        await db.flush()

    # Build rules dict
    rules = {}
    if roles:
        rules["roles"] = roles
    if format:
        rules["format"] = format
    if completion_criteria:
        rules["completion_criteria"] = completion_criteria

    # Create room (no participant restrictions — all keys have access by default)
    room = Room(
        title=title,
        description=description or None,
        rules=rules or None,
        status="active",
        created_by=admin_agent.id,
        max_messages=max_messages or 1000,
    )
    db.add(room)
    await db.flush()

    # Add the admin agent as a participant
    admin_participation = RoomParticipation(
        room_id=room.id, agent_id=admin_agent.id, role="admin"
    )
    db.add(admin_participation)

    # Опционально: сразу добавить участников по префиксам/хэшам ключей
    added = 0
    missing: list[str] = []
    seen_ids = {admin_agent.id}
    for ref in participant_keys.replace(";", " ").replace(",", " ").split():
        ref = ref.strip()
        if not ref:
            continue
        member = await _resolve_participant_by_prefix(db, ref)
        if member is None:
            missing.append(ref)
            continue
        if member.id in seen_ids:
            continue
        seen_ids.add(member.id)
        db.add(
            RoomParticipation(room_id=room.id, agent_id=member.id, role="participant")
        )
        added += 1

    await db.commit()

    message = "Комната создана"
    if added:
        message += f" (+{added} участн.)"
    if missing:
        message += "; ключи не найдены: " + ", ".join(missing)

    return RedirectResponse(
        url=f"/admin/rooms/{room.id}?flash={quote(message)}",
        status_code=303,
    )


@router.get("/rooms/{room_uuid}", response_class=HTMLResponse)
async def room_detail(
    request: Request,
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Render room detail page with messages."""
    result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = result.scalar_one_or_none()

    if room is None:
        return _redirect_login("Комната не найдена")

    # Get participants
    participants_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room_uuid)
        .options(selectinload(RoomParticipation.agent))
    )
    participants = participants_result.scalars().all()

    # Get messages with agent names
    msg_result = await db.execute(
        select(Message)
        .where(Message.room_id == room_uuid)
        .options(selectinload(Message.agent))
        .order_by(Message.id.asc())
    )
    messages = msg_result.scalars().all()

    # Build messages list
    msg_list = []
    for msg in messages:
        msg_list.append({
            "id": msg.id,
            "text": msg.text,
            "agent_name": msg.agent.agent_id if msg.agent else "unknown",
            "created_at": msg.created_at,
        })

    flash = None
    flash_param = request.query_params.get("flash")
    if flash_param:
        flash = {"type": "success", "message": unquote(flash_param)}

    return templates.TemplateResponse(
        "admin/room_detail.html",
        {
            "request": request,
            "room": room,
            "participants": participants,
            "messages": msg_list,
            "message_count": len(msg_list),
            "flash": flash,
        },
    )


@router.post("/rooms/{room_uuid}/participants")
async def room_add_participant(
    room_uuid: uuid.UUID,
    prefix: str = Form(...),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Добавить участника в существующую комнату по префиксу ключа/хэшу."""
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()
    if room is None:
        return RedirectResponse(url="/admin?flash=Комната+не+найдена", status_code=303)

    target = await _resolve_participant_by_prefix(db, prefix)
    if target is None:
        message = f"Агент с ключом '{prefix.strip()}' не найден"
        return RedirectResponse(
            url=f"/admin/rooms/{room_uuid}?flash={quote(message)}", status_code=303
        )

    existing = await db.execute(
        select(RoomParticipation).where(
            RoomParticipation.room_id == room_uuid,
            RoomParticipation.agent_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            RoomParticipation(
                room_id=room_uuid, agent_id=target.id, role="participant"
            )
        )
        await db.commit()
        message = f"Участник {target.agent_id} добавлен"
    else:
        message = f"{target.agent_id} уже является участником"

    return RedirectResponse(
        url=f"/admin/rooms/{room_uuid}?flash={quote(message)}", status_code=303
    )


@router.get("/rooms/{room_uuid}/edit-page", response_class=HTMLResponse)
async def room_edit_form(
    request: Request,
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Render page with edit form for room metadata."""
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()
    if room is None:
        return _redirect_login("Комната не найдена")

    return templates.TemplateResponse(
        "admin/room_edit.html",
        {
            "request": request,
            "room": room,
        },
    )


@router.post("/rooms/{room_uuid}/edit")
async def room_edit(
    request: Request,
    room_uuid: uuid.UUID,
    title: str = Form(...),
    description: str = Form(""),
    roles: str = Form(""),
    format: str = Form(""),
    completion_criteria: str = Form(""),
    max_messages: int = Form(1000),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Update room metadata."""
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()
    if room is None:
        return _redirect_login("Комната не найдена")

    room.title = title
    room.description = description or None
    room.max_messages = max_messages or 1000

    rules = {}
    if roles:
        rules["roles"] = roles
    if format:
        rules["format"] = format
    if completion_criteria:
        rules["completion_criteria"] = completion_criteria
    room.rules = rules or None

    await db.commit()

    return RedirectResponse(
        url=f"/admin/rooms/{room.id}?flash=Комната+обновлена",
        status_code=303,
    )


@router.post("/rooms/{room_uuid}/close")
async def room_close(
    request: Request,
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Close a room. HTMX endpoint: returns updated row or redirects."""
    result = await db.execute(select(Room).where(Room.id == room_uuid))
    room = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.status == "closed":
        raise HTTPException(status_code=409, detail="Room is already closed")

    room.status = "closed"
    room.closed_at = datetime.now(timezone.utc)
    await db.commit()

    # Check if this is an HTMX request
    hx_request = request.headers.get("HX-Request") == "true"
    if hx_request:
        # Return a response that HTMX will follow
        return HTMLResponse(
            content="",
            status_code=303,
            headers={"HX-Location": f"/admin/rooms/{room_uuid}?flash=Комната+закрыта"},
        )

    return RedirectResponse(
        url=f"/admin/rooms/{room_uuid}?flash=Комната+закрыта",
        status_code=303,
    )


@router.post("/rooms/{room_uuid}/delete")
async def room_delete(
    request: Request,
    room_uuid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Permanently delete a room and all its messages. Admin only.

    Uses cascade delete — messages and participants are removed automatically.
    """
    result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = result.scalar_one_or_none()

    if room is None:
        flash_msg = "Комната не найдена"
        return RedirectResponse(url=f"/admin?flash={flash_msg}", status_code=303)

    title = room.title
    await db.delete(room)
    await db.commit()

    return RedirectResponse(
        url=f"/admin?flash=Комната+«{title}»+удалена",
        status_code=303,
    )


@router.get("/rooms/{room_uuid}/export")
async def room_export(
    request: Request,
    room_uuid: uuid.UUID,
    format: str = Query("json"),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Export room messages as JSON or Markdown."""
    result = await db.execute(
        select(Room).where(Room.id == room_uuid)
    )
    room = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    # Get messages
    msg_result = await db.execute(
        select(Message)
        .where(Message.room_id == room_uuid)
        .options(selectinload(Message.agent))
        .order_by(Message.id.asc())
    )
    messages = msg_result.scalars().all()

    # Get participants
    part_result = await db.execute(
        select(RoomParticipation)
        .where(RoomParticipation.room_id == room_uuid)
        .options(selectinload(RoomParticipation.agent))
    )
    participants = part_result.scalars().all()

    if format == "md":
        lines = []
        lines.append(f"# {room.title}")
        lines.append("")
        if room.description:
            lines.append(f"{room.description}")
            lines.append("")
        lines.append(f"**Статус:** {room.status}")
        lines.append(f"**Создана:** {msk(room.created_at)}")
        if room.closed_at:
            lines.append(f"**Закрыта:** {msk(room.closed_at)}")
        lines.append(f"**Сообщений:** {len(messages)}")
        lines.append("")
        lines.append("---")
        lines.append("")

        for msg in messages:
            agent_name = msg.agent.agent_id if msg.agent else "unknown"
            timestamp = msk(msg.created_at, "%d.%m.%Y %H:%M:%S")
            lines.append(f"### #{msg.id} — {agent_name} ({timestamp})")
            lines.append("")
            lines.append(msg.text)
            lines.append("")
            lines.append("---")
            lines.append("")

        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="room-{room_uuid}.md"'},
        )
    else:
        # JSON export
        data = {
            "id": str(room.id),
            "title": room.title,
            "description": room.description,
            "rules": room.rules,
            "status": room.status,
            "created_at": room.created_at.isoformat(),
            "closed_at": room.closed_at.isoformat() if room.closed_at else None,
            "max_messages": room.max_messages,
            "messages": [
                {
                    "id": msg.id,
                    "agent_id": str(msg.agent_id),
                    "agent_name": msg.agent.agent_id if msg.agent else "unknown",
                    "text": msg.text,
                    "created_at": msg.created_at.isoformat(),
                }
                for msg in messages
            ],
            "participants": [
                {
                    "agent_id": p.agent.agent_id,
                    "role": p.role,
                }
                for p in participants
            ],
        }
        return PlainTextResponse(
            content=json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="room-{room_uuid}.json"'},
        )


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

@router.get("/keys", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Render key management page."""
    result = await db.execute(
        select(Agent).order_by(Agent.created_at.desc())
    )
    keys = result.scalars().all()

    flash = None
    flash_param = request.query_params.get("flash")
    if flash_param:
        flash = {"type": "error" if "ошиб" in flash_param.lower() else "success", "message": unquote(flash_param)}

    return templates.TemplateResponse(
        "admin/keys.html",
        {
            "request": request,
            "keys": keys,
            "new_key": None,
            "flash": flash,
        },
    )


@router.post("/keys/new")
async def key_create(
    request: Request,
    agent_id: str = Form(...),
    is_admin: str = Form(""),
    can_moderate: str = Form(""),
    owner_note: str = Form(""),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Create a new API key and show it once."""
    # Check if agent_id already exists
    existing = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    if existing.scalar_one_or_none() is not None:
        flash_msg = f"Агент с ID «{agent_id}» уже существует"
        # Re-render keys page with error
        result = await db.execute(select(Agent).order_by(Agent.created_at.desc()))
        keys = result.scalars().all()
        return templates.TemplateResponse(
            "admin/keys.html",
            {
                "request": request,
                "keys": keys,
                "new_key": None,
                "flash": {"type": "error", "message": flash_msg},
            },
            status_code=400,
        )

    raw_key = generate_raw_key()
    key_hash = hash_key(raw_key)
    prefix = key_prefix(raw_key)

    new_agent = Agent(
        key_hash=key_hash,
        agent_id=agent_id,
        is_admin=is_admin.lower() in ("on", "true", "1"),
        can_moderate=can_moderate.lower() in ("on", "true", "1"),
        owner_note=owner_note or None,
        status="active",
        last_key_prefix=prefix,
    )
    db.add(new_agent)
    await db.commit()
    await db.refresh(new_agent)

    from app.schemas.agent import AgentWithKey

    new_key_data = AgentWithKey(
        id=new_agent.id,
        agent_id=new_agent.agent_id,
        is_admin=new_agent.is_admin,
        can_moderate=new_agent.can_moderate,
        status=new_agent.status,
        raw_key=raw_key,
        last_key_prefix=prefix,
        created_at=new_agent.created_at,
    )

    # Re-fetch all keys for the table
    result = await db.execute(select(Agent).order_by(Agent.created_at.desc()))
    keys = result.scalars().all()

    return templates.TemplateResponse(
        "admin/keys.html",
        {
            "request": request,
            "keys": keys,
            "new_key": new_key_data,
            "flash": {"type": "success", "message": "Ключ успешно создан!"},
        },
    )


@router.post("/keys/{agent_id}/revoke")
async def key_revoke(
    request: Request,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Revoke a key. HTMX endpoint: returns updated table row."""
    result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    target = result.scalar_one_or_none()

    if target is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if target.status == "revoked":
        raise HTTPException(status_code=409, detail="Key is already revoked")

    target.status = "revoked"
    await db.commit()

    hx_request = request.headers.get("HX-Request") == "true"
    if hx_request:
        return HTMLResponse(
            content=f"""<tr>
                <td><code>{target.last_key_prefix or '—'}</code></td>
                <td><strong>{target.agent_id}</strong></td>
                <td>{'<span class="badge badge-admin">Админ</span>' if target.is_admin else ''}</td>
                <td><span class="badge badge-revoked">Отозван</span></td>
                <td><small>{msk(target.created_at)}</small></td>
                <td><small class="text-muted">{target.owner_note or '—'}</small></td>
                <td class="cell-actions">—</td>
            </tr>"""
        )

    return RedirectResponse(url="/admin/keys?flash=Ключ+отозван", status_code=303)


@router.post("/keys/{agent_id}/delete")
async def key_delete(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Удалить агента вместе с ключом.

    Разрешено только если у агента нет созданных комнат и отправленных
    сообщений: иначе сначала нужно удалить его комнаты (сообщения уходят
    каскадом вместе с комнатой).
    """
    result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    target = result.scalar_one_or_none()
    if target is None:
        return RedirectResponse(
            url="/admin/keys?flash=Агент+не+найден", status_code=303
        )

    rooms_count = await db.scalar(
        select(func.count(Room.id)).where(Room.created_by == target.id)
    )
    messages_count = await db.scalar(
        select(func.count(Message.id)).where(Message.agent_id == target.id)
    )
    if rooms_count or messages_count:
        message = (
            f"Ошибка: нельзя удалить «{agent_id}» — у него {rooms_count} "
            f"комнат(ы) и {messages_count} сообщений. Сначала удалите его комнаты."
        )
        return RedirectResponse(
            url=f"/admin/keys?flash={quote(message)}", status_code=303
        )

    # Участия убираем явно (FK), затем удаляем агента
    await db.execute(
        delete(RoomParticipation).where(RoomParticipation.agent_id == target.id)
    )
    await db.execute(delete(Agent).where(Agent.id == target.id))
    await db.commit()

    message = f"Агент «{agent_id}» удалён вместе с ключом"
    return RedirectResponse(url=f"/admin/keys?flash={quote(message)}", status_code=303)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Render settings page with rooms_per_hour control."""
    current_value = await get_rooms_per_hour(db)

    flash = None
    flash_param = request.query_params.get("flash")
    if flash_param:
        flash = {"type": "success", "message": unquote(flash_param)}

    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "rooms_per_hour": current_value,
            "flash": flash,
        },
    )


@router.post("/settings")
async def settings_update(
    request: Request,
    rooms_per_hour: int = Form(1, ge=1, le=100, description="Max rooms per hour per agent"),
    db: AsyncSession = Depends(get_db),
    admin_username: str = Depends(get_current_admin),
):
    """Update rooms_per_hour setting."""
    await set_rooms_per_hour(db, rooms_per_hour)
    return RedirectResponse(
        url="/admin/settings?flash=Настройки+сохранены",
        status_code=303,
    )