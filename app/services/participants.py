"""
Shared participant resolution logic.

Used by REST routes and MCP handlers to resolve participant references
by key_hash (64 hex chars) or key_prefix (first 8 chars).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent


class ParticipantNotFoundError(ValueError):
    """Raised when a participant key reference cannot be resolved."""


async def resolve_participants(
    db: AsyncSession, participant_keys: list[str]
) -> list[Agent]:
    """Resolve participants by key_hash (64 hex chars) or key_prefix (8 chars).

    Raises ParticipantNotFoundError if any reference cannot be resolved.
    """
    agents: list[Agent] = []
    for key_ref in participant_keys:
        if len(key_ref) == 64 and all(c in "0123456789abcdef" for c in key_ref.lower()):
            stmt = select(Agent).where(Agent.key_hash == key_ref)
        else:
            stmt = select(Agent).where(Agent.last_key_prefix == key_ref)

        result = await db.execute(stmt)
        agent = result.scalar_one_or_none()
        if agent is None:
            raise ParticipantNotFoundError(f"Participant with key '{key_ref}' not found")
        agents.append(agent)
    return agents