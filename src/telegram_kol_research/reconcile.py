"""Checkpoint-based reconcile helpers for listener recovery."""

from __future__ import annotations

from datetime import datetime, timedelta


def build_reconcile_window(
    *,
    checkpoint_message_at: datetime | None,
    now: datetime,
    safety_minutes: int = 15,
) -> tuple[datetime, datetime]:
    """Build a replay window that overlaps recent history after a checkpoint."""

    if checkpoint_message_at is None:
        return now - timedelta(minutes=safety_minutes), now
    return checkpoint_message_at - timedelta(minutes=safety_minutes), now
