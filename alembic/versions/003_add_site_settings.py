"""Add site_settings table

Revision ID: 003_add_site_settings
Revises: 002_add_can_moderate
Create Date: 2026-11-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_add_site_settings"
down_revision: Union[str, None] = "002_add_can_moderate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_settings",
        sa.Column("id", sa.Integer, primary_key=True, default=1),
        sa.Column("rooms_per_hour", sa.Integer, default=1, nullable=False),
    )
    # Insert default row
    op.execute("INSERT INTO site_settings (id, rooms_per_hour) VALUES (1, 1)")


def downgrade() -> None:
    op.drop_table("site_settings")