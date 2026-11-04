import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.room import Room
    from app.models.message import Message


class Agent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_moderate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    owner_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | revoked
    last_key_prefix: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # first 8 chars of raw key for display

    # relationships
    messages: Mapped[list["Message"]] = relationship(back_populates="agent")
    room_participations: Mapped[list["RoomParticipation"]] = relationship(back_populates="agent")
    created_rooms: Mapped[list["Room"]] = relationship(back_populates="created_by_agent")

    def __repr__(self) -> str:
        return f"<Agent {self.agent_id} admin={self.is_admin} mod={self.can_moderate}>"