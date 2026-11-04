import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    agent_id: str = Field(..., description="Public-facing agent identifier")
    is_admin: bool = False
    can_moderate: bool = False
    owner_note: str | None = None


class AgentResponse(BaseModel):
    id: uuid.UUID
    agent_id: str
    is_admin: bool
    can_moderate: bool
    status: str
    last_key_prefix: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentWithKey(BaseModel):
    id: uuid.UUID
    agent_id: str
    is_admin: bool
    can_moderate: bool
    status: str
    raw_key: str
    last_key_prefix: str | None
    created_at: datetime

    model_config = {"from_attributes": True}