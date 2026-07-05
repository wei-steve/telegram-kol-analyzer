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
    trading_mode: str = "notify_only"
    max_loss_usdt: float | None = None
    symbol_whitelist: list[str] | None = None


@dataclass(slots=True)
class TargetGroupConfig:
    chat_title: str
    chat_id: int | None = None
    enabled: bool = True
    tracked_senders: list[TrackedSenderConfig] = field(default_factory=list)
    custom_group_label: str | None = None
    sync_start_date: date | None = None
    sync_end_date: date | None = None
    ai_strategy_enabled: bool = False
    trading_mode: str = "notify_only"
    max_loss_usdt: float = 20.0
    symbol_whitelist: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    symbol_max_loss_usdt: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class GroupConfig:
    groups: list[TargetGroupConfig] = field(default_factory=list)


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_symbol_whitelist(value: Any) -> list[str] | None:
    if value in (None, ""):
        return None
    return [str(symbol).upper() for symbol in value]


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
                trading_mode=sender_data.get("trading_mode", "notify_only"),
                max_loss_usdt=(
                    float(sender_data["max_loss_usdt"])
                    if sender_data.get("max_loss_usdt") is not None
                    else None
                ),
                symbol_whitelist=_parse_symbol_whitelist(sender_data.get("symbol_whitelist")),
            )
            for sender_data in group_data.get("tracked_senders", [])
        ]
        groups.append(
            TargetGroupConfig(
                chat_title=group_data["chat_title"],
                chat_id=group_data.get("chat_id"),
                enabled=group_data.get("enabled", True),
                tracked_senders=tracked_senders,
                custom_group_label=group_data.get("custom_group_label"),
                sync_start_date=_parse_optional_date(group_data.get("sync_start_date")),
                sync_end_date=_parse_optional_date(group_data.get("sync_end_date")),
                ai_strategy_enabled=bool(group_data.get("ai_strategy_enabled", False)),
                trading_mode=group_data.get("trading_mode", "notify_only"),
                max_loss_usdt=float(group_data.get("max_loss_usdt", 20.0)),
                symbol_whitelist=(
                    _parse_symbol_whitelist(group_data.get("symbol_whitelist"))
                    or ["BTC", "ETH"]
                ),
                symbol_max_loss_usdt=_parse_symbol_max_loss_usdt(
                    group_data.get("symbol_max_loss_usdt")
                ),
            )
        )

    return GroupConfig(groups=groups)


def update_group_automation_settings(
    config_path: str | Path,
    *,
    chat_id: int,
    chat_title: str | None = None,
    ai_strategy_enabled: bool | None = None,
    auto_trade_enabled: bool | None = None,
) -> GroupConfig:
    """Persist per-group AI strategy and auto-trade switches."""

    path = Path(config_path)
    config = load_group_config(path)
    group = next((item for item in config.groups if item.chat_id == chat_id), None)
    if group is None:
        group = TargetGroupConfig(
            chat_title=chat_title or str(chat_id),
            chat_id=chat_id,
            enabled=True,
        )
        config.groups.insert(0, group)
    elif chat_title and group.chat_title == str(chat_id):
        group.chat_title = chat_title

    if ai_strategy_enabled is not None:
        group.ai_strategy_enabled = bool(ai_strategy_enabled)
    if auto_trade_enabled is not None:
        group.trading_mode = "auto_trade" if auto_trade_enabled else "notify_only"

    _write_group_config(path, config)
    return config


def _write_group_config(path: Path, config: GroupConfig) -> None:
    payload = {"groups": [_group_to_yaml_dict(group) for group in config.groups]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _group_to_yaml_dict(group: TargetGroupConfig) -> dict[str, Any]:
    data: dict[str, Any] = {
        "chat_title": group.chat_title,
        "chat_id": group.chat_id,
        "enabled": group.enabled,
        "ai_strategy_enabled": group.ai_strategy_enabled,
        "trading_mode": group.trading_mode,
        "max_loss_usdt": group.max_loss_usdt,
        "symbol_whitelist": group.symbol_whitelist,
    }
    if group.symbol_max_loss_usdt:
        data["symbol_max_loss_usdt"] = group.symbol_max_loss_usdt
    if group.custom_group_label is not None:
        data["custom_group_label"] = group.custom_group_label
    if group.sync_start_date is not None:
        data["sync_start_date"] = group.sync_start_date.isoformat()
    if group.sync_end_date is not None:
        data["sync_end_date"] = group.sync_end_date.isoformat()
    if group.tracked_senders:
        data["tracked_senders"] = [
            _sender_to_yaml_dict(sender) for sender in group.tracked_senders
        ]
    return data


def _parse_symbol_max_loss_usdt(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, float] = {}
    for raw_symbol, raw_loss in value.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol:
            continue
        try:
            loss = float(raw_loss)
        except (TypeError, ValueError):
            continue
        if loss > 0:
            parsed[symbol] = loss
    return parsed


def _sender_to_yaml_dict(sender: TrackedSenderConfig) -> dict[str, Any]:
    data: dict[str, Any] = {
        "display_name": sender.display_name,
        "trading_mode": sender.trading_mode,
    }
    if sender.username is not None:
        data["username"] = sender.username
    if sender.telegram_sender_id is not None:
        data["telegram_sender_id"] = sender.telegram_sender_id
    if sender.custom_label is not None:
        data["custom_label"] = sender.custom_label
    if sender.max_loss_usdt is not None:
        data["max_loss_usdt"] = sender.max_loss_usdt
    if sender.symbol_whitelist is not None:
        data["symbol_whitelist"] = sender.symbol_whitelist
    return data
