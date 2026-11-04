import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.room import Room


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    # На PostgreSQL — BIGINT с Identity, на SQLite — INTEGER с autoincrement
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
        autoincrement=True,
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)

    room: Mapped["Room"] = relationship(back_populates="messages")
    agent: Mapped["Agent"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message #{self.id} in {self.room_id}>"