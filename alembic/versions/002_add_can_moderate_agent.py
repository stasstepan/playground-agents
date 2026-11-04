"""Add can_moderate field to agents table

Revision ID: 002_add_can_moderate
Revises: 001_initial
Create Date: 2026-11-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_can_moderate"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("can_moderate", sa.Boolean, default=False, nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("agents", "can_moderate")