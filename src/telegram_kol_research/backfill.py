"""Historical backfill planning helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig, load_group_config


def compute_backfill_start(*, now: datetime, days: int = 90) -> datetime:
    """Return the start of the historical backfill window."""

    return now - timedelta(days=days)


@dataclass(slots=True)
class BackfillWindow:
    chat_title: str
    start_at: datetime
    end_at: datetime
    checkpoint_message_id: int | None = None
    checkpoint_message_at: datetime | None = None


def _coerce_group_start(group: TargetGroupConfig, default_start: datetime) -> datetime:
    if group.sync_start_date is None:
        return default_start
    return datetime.combine(group.sync_start_date, datetime.min.time(), tzinfo=timezone.utc)


def _coerce_group_end(group: TargetGroupConfig, default_end: datetime) -> datetime:
    if group.sync_end_date is None:
        return default_end
    return datetime.combine(group.sync_end_date, datetime.max.time(), tzinfo=timezone.utc)


def build_backfill_windows(
    config: GroupConfig,
    *,
    now: datetime,
    days: int = 90,
    checkpoints: dict[str, dict[str, Any]] | None = None,
) -> list[BackfillWindow]:
    """Plan backfill windows for enabled target groups."""

    default_start = compute_backfill_start(now=now, days=days)
    checkpoint_map = checkpoints or {}
    windows: list[BackfillWindow] = []

    for group in config.groups:
        if not group.enabled:
            continue
        checkpoint = checkpoint_map.get(group.chat_title, {})
        windows.append(
            BackfillWindow(
                chat_title=group.chat_title,
                start_at=_coerce_group_start(group, default_start),
                end_at=_coerce_group_end(group, now),
                checkpoint_message_id=checkpoint.get("last_message_id"),
                checkpoint_message_at=checkpoint.get("last_message_at"),
            )
        )

    return windows


def load_target_groups(config_path: str | Path) -> GroupConfig:
    """Load the target group config used by the backfill pipeline."""

    return load_group_config(config_path)


def run_backfill_plan(
    config_path: str | Path,
    *,
    now: datetime | None = None,
    days: int = 90,
    checkpoints: dict[str, dict[str, Any]] | None = None,
) -> list[BackfillWindow]:
    """Build a minimal backfill plan for the configured target groups."""

    effective_now = now or datetime.now(timezone.utc)
    group_config = load_target_groups(config_path)
    return build_backfill_windows(
        group_config,
        now=effective_now,
        days=days,
        checkpoints=checkpoints,
    )
