"""Форматирование времени в часовом поясе площадки (Москва).

В БД время хранится в UTC (datetime с tzinfo). Для UI все даты показываем
в московском времени. Москва с 2014 года — фиксированный UTC+3, без перехода
на летнее время, поэтому используем фиксированный offset: не зависим от
наличия tzdata в контейнере.
"""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), name="MSK")


def msk(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Отформатировать дату в московском времени (naive-даты считаем UTC)."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MSK).strftime(fmt)
