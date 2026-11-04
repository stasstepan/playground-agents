from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_admin_key, get_api_key
from app.db.database import get_db
from app.models.agent import Agent
from app.schemas.agent import AgentCreate, AgentResponse, AgentWithKey
from app.schemas.error import ErrorResponse
from app.services.keys import generate_raw_key, hash_key, key_prefix

router = APIRouter(prefix="/api/keys", tags=["keys"])


@router.post(
    "",
    response_model=AgentWithKey,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def create_key(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_admin_key),
):
    """Create a new API key. Admin only. Returns the raw key once."""
    result = await db.execute(select(Agent).where(Agent.agent_id == body.agent_id))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent with agent_id '{body.agent_id}' already exists",
        )

    raw_key = generate_raw_key()
    new_agent = Agent(
        key_hash=hash_key(raw_key),
        agent_id=body.agent_id,
        is_admin=body.is_admin,
        can_moderate=body.can_moderate,
        owner_note=body.owner_note,
        status="active",
        last_key_prefix=key_prefix(raw_key),
    )
    db.add(new_agent)
    await db.commit()
    await db.refresh(new_agent)

    return AgentWithKey(
        id=new_agent.id,
        agent_id=new_agent.agent_id,
        is_admin=new_agent.is_admin,
        can_moderate=new_agent.can_moderate,
        status=new_agent.status,
        raw_key=raw_key,
        last_key_prefix=key_prefix(raw_key),
        created_at=new_agent.created_at,
    )


@router.get(
    "/me",
    response_model=AgentResponse,
    responses={
        401: {"model": ErrorResponse},
    },
)
async def get_my_key(
    agent: Agent = Depends(get_api_key),
):
    """Return information about the currently authenticated API key."""
    return AgentResponse(
        id=agent.id,
        agent_id=agent.agent_id,
        is_admin=agent.is_admin,
        can_moderate=agent.can_moderate,
        status=agent.status,
        last_key_prefix=agent.last_key_prefix,
        created_at=agent.created_at,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def revoke_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(get_admin_key),
):
    """Revoke an API key by setting its status to 'revoked'. Admin only."""
    result = await db.execute(select(Agent).where(Agent.agent_id == key_id))
    target = result.scalar_one_or_none()

    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    if target.status == "revoked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Key is already revoked",
        )

    target.status = "revoked"
    await db.commit()

    return {"detail": f"Key for agent '{key_id}' has been revoked"}