"""Dependency-light validation for the entry-revision authority document."""

from __future__ import annotations

from datetime import UTC, datetime
import json


ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY = "entry_revision_exchange_authority"
_IDLE_KEYS = frozenset(
    {"schema_version", "state", "generation", "released_at"}
)


def is_canonical_idle_entry_revision_exchange_authority(
    value_json: str,
) -> bool:
    """Return whether a stored document is the exact parseable idle schema."""

    try:
        document = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        not isinstance(document, dict)
        or frozenset(document) != _IDLE_KEYS
        or document.get("schema_version") != 2
        or document.get("state") != "idle"
    ):
        return False
    generation = document.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return False
    released_at = document.get("released_at")
    if not isinstance(released_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(released_at)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.astimezone(UTC) is not None
