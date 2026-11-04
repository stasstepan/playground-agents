import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.room import Room


class RoomParticipation(Base):
    __tablename__ = "room_participants"

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)

    room: Mapped["Room"] = relationship(back_populates="participants")
    agent: Mapped["Agent"] = relationship(back_populates="room_participations")

    def __repr__(self) -> str:
        return f"<RoomParticipation room={self.room_id} agent={self.agent_id} role={self.role}>"