import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    text: str = Field(..., description="Message text content")
    # NOTE: agent_id is extracted from the authenticated API key, not from the request body


class MessageResponse(BaseModel):
    id: int
    room_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    text: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MessagePage(BaseModel):
    messages: list[MessageResponse]
    total: int


class MessageDeleteResponse(BaseModel):
    detail: str
    message_id: int
    room_id: uuid.UUID