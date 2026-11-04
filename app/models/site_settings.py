"""
Dynamic site settings stored in DB.

A single-row table keyed by id=1. Values are updated via the admin panel
without requiring a server restart. Falls back to ``app.core.config.settings``
for defaults when the row doesn't exist yet.
"""

from typing import Optional

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SiteSettings(Base):
    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    rooms_per_hour: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    def __repr__(self) -> str:
        return f"<SiteSettings rooms_per_hour={self.rooms_per_hour}>"