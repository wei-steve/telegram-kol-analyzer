"""Timezone normalization helpers for storage and local UI inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


DEFAULT_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def normalize_to_utc_naive(
    value: datetime | None,
    *,
    default_timezone: ZoneInfo = DEFAULT_LOCAL_TIMEZONE,
) -> datetime | None:
    """Convert a datetime to the project's SQLite storage convention: naive UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_timezone)
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_datetime_to_utc_naive(
    value: str | datetime | None,
    *,
    default_timezone: ZoneInfo = DEFAULT_LOCAL_TIMEZONE,
) -> datetime | None:
    """Parse a datetime-like value and normalize it to naive UTC."""

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    return normalize_to_utc_naive(parsed, default_timezone=default_timezone)


def parse_local_datetime_to_utc_naive(value: str | datetime | None) -> datetime | None:
    """Parse UI/local operator input as Asia/Shanghai when it lacks timezone info."""

    return parse_datetime_to_utc_naive(value, default_timezone=DEFAULT_LOCAL_TIMEZONE)


def utc_naive_to_local(
    value: datetime | None,
    *,
    display_timezone: ZoneInfo = DEFAULT_LOCAL_TIMEZONE,
) -> datetime | None:
    """Convert a stored naive UTC datetime to a timezone-aware local display time."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(display_timezone)
