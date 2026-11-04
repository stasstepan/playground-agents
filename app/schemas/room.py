import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ParticipantInfo(BaseModel):
    agent_id: str
    role: str
    last_key_prefix: str | None = None


class RoomCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Room title")
    description: str | None = None
    rules: dict | None = None
    participant_keys: list[str] = Field(
        default_factory=list,
        description="List of key_hashes or key_prefixes to identify participants",
    )
    max_messages: int = 1000


class RoomResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    rules: dict | None
    status: str
    created_by: uuid.UUID
    message_count: int
    participants: list[ParticipantInfo] = []
    joined: bool = False
    created_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}


class RoomListResponse(BaseModel):
    rooms: list[RoomResponse]