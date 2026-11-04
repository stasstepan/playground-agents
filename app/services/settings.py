"""
Dynamic site settings service.

Reads/writes the site_settings DB row (single-row table keyed by id=1).
Falls back to ``app.core.config.settings`` defaults when the row doesn't exist.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.models.site_settings import SiteSettings


async def get_rooms_per_hour(db: AsyncSession) -> int:
    """Return the current rooms_per_hour limit from DB (or config default)."""
    result = await db.execute(select(SiteSettings).where(SiteSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        return app_settings.rooms_per_hour
    return row.rooms_per_hour


async def set_rooms_per_hour(db: AsyncSession, value: int) -> SiteSettings:
    """Update or create the rooms_per_hour setting."""
    result = await db.execute(select(SiteSettings).where(SiteSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        row = SiteSettings(id=1, rooms_per_hour=value)
        db.add(row)
    else:
        row.rooms_per_hour = value
    await db.commit()
    await db.refresh(row)
    return row