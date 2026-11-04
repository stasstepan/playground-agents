"""Initial schema: agents, rooms, room_participants, messages

Supports both PostgreSQL (with UUID, JSONB, BIGINT Identity)
and SQLite (with String GUID, JSON fallback, INTEGER autoincrement).

Revision ID: 001_initial
Revises:
Create Date: 2026-11-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.engine import Connection

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    """Check if the backend is SQLite."""
    bind = op.get_bind()
    return isinstance(bind, Connection) and bind.dialect.name == "sqlite"


def _uuid_type():
    """Return appropriate UUID type for the current dialect."""
    return sa.String(36) if _is_sqlite() else UUID(as_uuid=True)


def _json_type():
    """Return appropriate JSON type for the current dialect."""
    return sa.JSON if _is_sqlite() else JSONB


def _messages_id_type():
    """Return appropriate ID column for messages."""
    if _is_sqlite():
        return sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)
    return sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True, autoincrement=True)


def upgrade() -> None:
    uuid_t = _uuid_type()
    json_t = _json_type()

    op.create_table(
        "agents",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("key_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("agent_id", sa.String(100), unique=True, nullable=False),
        sa.Column("is_admin", sa.Boolean, default=False, nullable=False),
        sa.Column("owner_note", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), default="active", nullable=False),
        sa.Column("last_key_prefix", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "rooms",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("rules", json_t, nullable=True),
        sa.Column("status", sa.String(20), default="active", nullable=False, index=True),
        sa.Column("created_by", uuid_t, sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("max_messages", sa.Integer, default=1000, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "room_participants",
        sa.Column("room_id", uuid_t, sa.ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", uuid_t, sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(50), nullable=False),
    )

    messages_id_col = _messages_id_type()
    op.create_table(
        "messages",
        messages_id_col,
        sa.Column("room_id", uuid_t, sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("agent_id", uuid_t, sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("room_participants")
    op.drop_table("rooms")
    op.drop_table("agents")