"""Typed YAML configuration loader for target Telegram groups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class TrackedSenderConfig:
    display_name: str
    username: str | None = None
    telegram_sender_id: int | None = None
    custom_label: str | None = None


@dataclass(slots=True)
class TargetGroupConfig:
    chat_title: str
    enabled: bool = True
    tracked_senders: list[TrackedSenderConfig] = field(default_factory=list)
    custom_group_label: str | None = None
    sync_start_date: date | None = None
    sync_end_date: date | None = None


@dataclass(slots=True)
class GroupConfig:
    groups: list[TargetGroupConfig] = field(default_factory=list)


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_group_config(config_path: str | Path) -> GroupConfig:
    """Load target group configuration from YAML."""

    raw_data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    groups: list[TargetGroupConfig] = []

    for group_data in raw_data.get("groups", []):
        tracked_senders = [
            TrackedSenderConfig(
                display_name=sender_data["display_name"],
                username=sender_data.get("username"),
                telegram_sender_id=sender_data.get("telegram_sender_id"),
                custom_label=sender_data.get("custom_label"),
            )
            for sender_data in group_data.get("tracked_senders", [])
        ]
        groups.append(
            TargetGroupConfig(
                chat_title=group_data["chat_title"],
                enabled=group_data.get("enabled", True),
                tracked_senders=tracked_senders,
                custom_group_label=group_data.get("custom_group_label"),
                sync_start_date=_parse_optional_date(group_data.get("sync_start_date")),
                sync_end_date=_parse_optional_date(group_data.get("sync_end_date")),
            )
        )

    return GroupConfig(groups=groups)
