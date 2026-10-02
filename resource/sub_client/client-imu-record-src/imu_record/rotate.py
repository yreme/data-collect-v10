"""Clock-aligned rotate windows (e.g. local 00:00 / 06:00 / 12:00 / 18:00)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def resolve_tz(name: str = "") -> timezone | ZoneInfo:
    raw = (name or "").strip()
    if not raw:
        # Use system local timezone.
        return datetime.now().astimezone().tzinfo or timezone.utc
    return ZoneInfo(raw)


def window_start(
    now: Optional[datetime] = None,
    *,
    period_hours: int = 6,
    tz_name: str = "",
) -> datetime:
    """Return the start of the current rotate window in *tz*."""
    if period_hours <= 0:
        raise ValueError("period_hours must be > 0")
    tz = resolve_tz(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    start_hour = (now.hour // period_hours) * period_hours
    return now.replace(hour=start_hour, minute=0, second=0, microsecond=0)


def window_id(
    now: Optional[datetime] = None,
    *,
    period_hours: int = 6,
    tz_name: str = "",
) -> int:
    """Integer id for the current window (epoch seconds of window start)."""
    return int(window_start(now, period_hours=period_hours, tz_name=tz_name).timestamp())


def next_window_start(
    now: Optional[datetime] = None,
    *,
    period_hours: int = 6,
    tz_name: str = "",
) -> datetime:
    start = window_start(now, period_hours=period_hours, tz_name=tz_name)
    return start + timedelta(hours=period_hours)


def format_window_filename_ts(
    now: Optional[datetime] = None,
    *,
    period_hours: int = 6,
    tz_name: str = "",
) -> str:
    """``YYYY-MM-DD_HH-MM-SS`` for the current window start (local/tz)."""
    start = window_start(now, period_hours=period_hours, tz_name=tz_name)
    return start.strftime("%Y-%m-%d_%H-%M-%S")
