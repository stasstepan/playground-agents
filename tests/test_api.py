"""
Core API tests for Playground portal.

Uses SQLite in-memory with aiosqlite for async tests.
Overrides the database dependency to use the test DB.
"""
import hashlib
import secrets
import string
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import get_db
from app.main import app
from app.models import Agent, Base, Message, Room, RoomParticipation
from app.core.config import settings

# ── Test database setup ──────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite://"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with test_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test, drop after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── Helpers ──────────────────────────────────────────────────────────────


def generate_raw_key() -> str:
    """Generate a pgk_ key matching the production format."""
    rand = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
    return f"pgk_{rand}"


async def create_agent(
    db: AsyncSession,
    agent_id: str = "test-agent",
    is_admin: bool = False,
    status: str = "active",
) -> tuple[Agent, str]:
    """Create an agent in the DB and return (agent, raw_key)."""
    raw_key = generate_raw_key()
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    agent = Agent(
        id=uuid.uuid4(),
        key_hash=key_hash,
        agent_id=agent_id,
        is_admin=is_admin,
        status=status,
        last_key_prefix=raw_key[:8],
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent, raw_key


async def create_room(
    db: AsyncSession,
    created_by: Agent,
    title: str = "Test Room",
    status: str = "active",
    max_messages: int = 100,
) -> Room:
    """Create a room."""
    room = Room(
        id=uuid.uuid4(),
        title=title,
        description="Test description",
        rules={"roles": "seller/buyer", "format": "RUB/ton", "completion": "agreement"},
        status=status,
        created_by=created_by.id,
        max_messages=max_messages,
    )
    db.add(room)
    await db.commit()
    await db.refresh(room)
    return room


async def add_participant(db: AsyncSession, room: Room, agent: Agent, role: str = "seller"):
    """Add agent as participant to room."""
    rp = RoomParticipation(room_id=room.id, agent_id=agent.id, role=role)
    db.add(rp)
    await db.commit()


async def send_message(db: AsyncSession, room: Room, agent: Agent, text: str = "Hello") -> Message:
    """Send a message in a room."""
    msg = Message(room_id=room.id, agent_id=agent.id, text=text)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


def auth_header(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


# ── Fixtures for common test objects ─────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def admin_agent(db):
    agent, key = await create_agent(db, agent_id="admin", is_admin=True)
    return agent, key


@pytest_asyncio.fixture
async def regular_agent(db):
    agent, key = await create_agent(db, agent_id="seller", is_admin=False)
    return agent, key


@pytest_asyncio.fixture
async def sample_room(db, admin_agent):
    agent, _ = admin_agent
    room = await create_room(db, created_by=agent)
    return room


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ═══════════════════════════════════════════════════════════════════════
#  1. Health
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"


# ═══════════════════════════════════════════════════════════════════════
#  2. Auth
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_auth_missing_header(client):
    resp = await client.get("/api/keys/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_invalid_key(client):
    resp = await client.get(
        "/api/keys/me",
        headers=auth_header("pgk_invalidkey123456789012345678901234567890123456"),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_revoked_key(client, db):
    agent, raw_key = await create_agent(db, agent_id="revoked-agent", status="revoked")
    resp = await client.get("/api/keys/me", headers=auth_header(raw_key))
    assert resp.status_code == 401
    assert "revoked" in resp.text.lower() or "revoked" in str(resp.json()).lower()


@pytest.mark.asyncio
async def test_auth_valid_key(client, regular_agent):
    _, raw_key = regular_agent
    resp = await client.get("/api/keys/me", headers=auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "seller"


# ═══════════════════════════════════════════════════════════════════════
#  3. Key management (/api/keys)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_key_admin_only(client, regular_agent):
    _, raw_key = regular_agent  # non-admin
    resp = await client.post(
        "/api/keys",
        headers=auth_header(raw_key),
        json={"agent_id": "new-agent"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_key_success(client, admin_agent):
    _, raw_key = admin_agent
    resp = await client.post(
        "/api/keys",
        headers=auth_header(raw_key),
        json={"agent_id": "new-agent", "is_admin": False, "owner_note": "test"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] == "new-agent"
    assert data["is_admin"] is False
    assert data["can_moderate"] is False
    assert data["status"] == "active"
    # raw_key should be returned once
    assert "raw_key" in data
    assert data["raw_key"].startswith("pgk_")


@pytest.mark.asyncio
async def test_create_key_duplicate_agent_id(client, admin_agent, db):
    _, raw_key = admin_agent
    agent, _ = await create_agent(db, agent_id="existing-agent")
    resp = await client.post(
        "/api/keys",
        headers=auth_header(raw_key),
        json={"agent_id": "existing-agent"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_revoke_key(client, admin_agent, db):
    _, admin_key = admin_agent
    agent, agent_raw_key = await create_agent(db, agent_id="to-revoke")

    # Agent can authenticate
    resp = await client.get("/api/keys/me", headers=auth_header(agent_raw_key))
    assert resp.status_code == 200

    # Revoke by admin (uses agent_id string, not UUID)
    resp = await client.delete(
        f"/api/keys/{agent.agent_id}",
        headers=auth_header(admin_key),
    )
    assert resp.status_code == 200

    # Agent can no longer authenticate
    resp = await client.get("/api/keys/me", headers=auth_header(agent_raw_key))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_keys_me(client, regular_agent):
    agent, raw_key = regular_agent
    resp = await client.get("/api/keys/me", headers=auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == agent.agent_id
    assert data["status"] == "active"


# ═══════════════════════════════════════════════════════════════════════
#  4. Room CRUD (/api/rooms)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_room_any_agent_can_create(client, regular_agent):
    _, raw_key = regular_agent
    resp = await client.post(
        "/api/rooms",
        headers=auth_header(raw_key),
        json={"title": "My Room"},
    )
    # Any active agent can create a room (max 1 per hour)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_room_rate_limit(client, regular_agent, db):
    agent, raw_key = regular_agent
    # Create first room
    resp = await client.post(
        "/api/rooms",
        headers=auth_header(raw_key),
        json={"title": "First Room"},
    )
    assert resp.status_code == 201

    # Try to create second room within same hour
    resp = await client.post(
        "/api/rooms",
        headers=auth_header(raw_key),
        json={"title": "Second Room"},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_create_room_success(client, admin_agent):
    _, raw_key = admin_agent
    resp = await client.post(
        "/api/rooms",
        headers=auth_header(raw_key),
        json={
            "title": "Test Room",
            "description": "A test room",
            "rules": {"roles": "seller/buyer"},
            "max_messages": 500,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Test Room"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_create_room_with_participants(client, admin_agent, db):
    agent, raw_key = admin_agent
    participant_agent, p_key = await create_agent(db, agent_id="participant-1")
    await db.commit()

    resp = await client.post(
        "/api/rooms",
        headers=auth_header(raw_key),
        json={
            "title": "Room with participants",
            "description": "Has participants",
            "participant_keys": [p_key[:8]],  # prefix match
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_list_rooms_admin_sees_all(client, admin_agent, db):
    agent, raw_key = admin_agent
    # Create a room
    await create_room(db, created_by=agent)
    resp = await client.get("/api/rooms", headers=auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rooms"]) == 1


@pytest.mark.asyncio
async def test_list_rooms_regular_sees_only_own(client, admin_agent, regular_agent, db):
    admin, admin_key = admin_agent
    regular, regular_key = regular_agent

    # Create a room by admin, add regular as participant
    room = await create_room(db, created_by=admin)
    await add_participant(db, room, regular, role="buyer")

    # Create another room by admin only
    await create_room(db, created_by=admin, title="Admin-only Room")

    # Regular agent should see only 1 room
    resp = await client.get("/api/rooms", headers=auth_header(regular_key))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rooms"]) == 1
    assert data["rooms"][0]["title"] == "Test Room"


@pytest.mark.asyncio
async def test_get_room_detail(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent)
    resp = await client.get(f"/api/rooms/{room.id}", headers=auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test Room"
    assert data["status"] == "active"
    assert "message_count" in data


@pytest.mark.asyncio
async def test_get_room_not_found(client, admin_agent):
    _, raw_key = admin_agent
    resp = await client.get(
        f"/api/rooms/{uuid.uuid4()}",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_close_room_admin_only(client, regular_agent, sample_room):
    _, raw_key = regular_agent
    resp = await client.post(
        f"/api/rooms/{sample_room.id}/close",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_close_room_success(client, admin_agent, sample_room):
    _, raw_key = admin_agent
    resp = await client.post(
        f"/api/rooms/{sample_room.id}/close",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "closed" in data.get("detail", "").lower()


@pytest.mark.asyncio
async def test_delete_room_admin_only(client, regular_agent, sample_room):
    _, raw_key = regular_agent
    resp = await client.delete(
        f"/api/rooms/{sample_room.id}",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_room_success(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent)

    resp = await client.delete(
        f"/api/rooms/{room.id}",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "deleted" in data.get("detail", "").lower()

    # Verify room no longer exists
    resp = await client.get(
        f"/api/rooms/{room.id}",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_room_not_found(client, admin_agent):
    _, raw_key = admin_agent
    resp = await client.delete(
        f"/api/rooms/{uuid.uuid4()}",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  5. Messages
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_send_message(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent)
    await add_participant(db, room, agent, role="seller")

    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(raw_key),
        json={"text": "Hello, this is a test message"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["text"] == "Hello, this is a test message"
    assert data["agent_id"] == str(agent.id)
    assert data["id"] is not None


@pytest.mark.asyncio
async def test_send_message_any_active_key_can_write(client, admin_agent, regular_agent, db):
    admin, admin_key = admin_agent
    regular, regular_key = regular_agent
    room = await create_room(db, created_by=admin)
    # Any active key can write to any active room (no participation check)

    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(regular_key),
        json={"text": "I'm not a participant but can write"},
    )
    # All active keys can write to any active room
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_send_to_closed_room(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent, status="closed")
    await add_participant(db, room, agent, role="seller")

    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(raw_key),
        json={"text": "Should fail, room closed"},
    )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_send_to_full_room(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent, max_messages=1)
    await add_participant(db, room, agent, role="seller")

    # First message should succeed
    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(raw_key),
        json={"text": "First message"},
    )
    assert resp.status_code == 201

    # Second message should exceed limit
    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(raw_key),
        json={"text": "Should fail, room full"},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_send_message_too_long(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent)
    await add_participant(db, room, agent, role="seller")

    long_text = "x" * (settings.max_message_length + 1)
    resp = await client.post(
        f"/api/rooms/{room.id}/messages",
        headers=auth_header(raw_key),
        json={"text": long_text},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_read_messages_pagination(client, admin_agent, db):
    agent, raw_key = admin_agent
    room = await create_room(db, created_by=agent)
    await add_participant(db, room, agent, role="seller")

    # Send 3 messages
    for i in range(3):
        resp = await client.post(
            f"/api/rooms/{room.id}/messages",
            headers=auth_header(raw_key),
            json={"text": f"Message {i}"},
        )
        assert resp.status_code == 201

    # Read with since=1
    resp = await client.get(
        f"/api/rooms/{room.id}/messages?since=1",
        headers=auth_header(raw_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) >= 2  # messages with id > 1


# ═══════════════════════════════════════════════════════════════════════
#  6. Admin web panel
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_admin_login_page(client):
    resp = await client.get("/admin/login")
    assert resp.status_code in (200, 302, 303, 307)
    assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_admin_dashboard_requires_auth(client):
    resp = await client.get("/admin")
    # Should redirect to login
    assert resp.status_code in (302, 307, 303, 200)
    # If 200, should be login page
    if resp.status_code == 200:
        assert "login" in resp.text.lower()


# ═══════════════════════════════════════════════════════════════════════
#  7. Read-only room view
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_readonly_room_public(client, sample_room):
    """Public read-only room view should be accessible without auth."""
    resp = await client.get(f"/r/{sample_room.id}")
    # Public page — should return 200 and render HTML
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


# ═══════════════════════════════════════════════════════════════════════
#  8. AGENTS.md
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_agents_md_endpoint(client):
    resp = await client.get("/agents.md")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers.get("content-type", "") or "text/plain" in resp.headers.get("content-type", "")
    assert "pgk_" in resp.text or "MCP" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  9. Moderation (delete message)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_message_moderator_required(client, admin_agent, regular_agent, db):
    admin, admin_key = admin_agent
    regular, regular_key = regular_agent
    room = await create_room(db, created_by=admin)

    # Send a message as regular agent
    msg = await send_message(db, room, regular, text="Bad message")
    await db.commit()

    # Regular agent (non-moderator) cannot delete
    resp = await client.delete(
        f"/api/rooms/{room.id}/messages/{msg.id}",
        headers=auth_header(regular_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_message_success(client, admin_agent, regular_agent, db):
    admin, admin_key = admin_agent
    regular, regular_key = regular_agent

    # Promote admin agent to moderator
    admin.can_moderate = True
    await db.commit()

    room = await create_room(db, created_by=admin)
    msg = await send_message(db, room, regular, text="Bad message")
    await db.commit()

    resp = await client.delete(
        f"/api/rooms/{room.id}/messages/{msg.id}",
        headers=auth_header(admin_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message_id"] == msg.id
    assert "deleted" in data["detail"].lower()


@pytest.mark.asyncio
async def test_delete_message_not_found(client, admin_agent, db):
    admin, admin_key = admin_agent
    admin.can_moderate = True
    await db.commit()

    room = await create_room(db, created_by=admin)
    resp = await client.delete(
        f"/api/rooms/{room.id}/messages/99999",
        headers=auth_header(admin_key),
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  10. Error schemas
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_404_error_schema(client):
    resp = await client.get("/api/rooms/nonexistent-uuid")
    assert resp.status_code in (401, 404, 422)


@pytest.mark.asyncio
async def test_malformed_uuid(client, admin_agent):
    _, raw_key = admin_agent
    resp = await client.get(
        "/api/rooms/not-a-uuid",
        headers=auth_header(raw_key),
    )
    # FastAPI may return 422 for path param validation
    assert resp.status_code in (400, 422, 404)