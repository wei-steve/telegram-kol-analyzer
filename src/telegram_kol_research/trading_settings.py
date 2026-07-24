"""Runtime trading settings persisted in the local database."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.models import TradingSetting


TRADING_SETTINGS_KEY = "global"


@dataclass(slots=True)
class TradingSettings:
    auto_trade_enabled: bool = False
    management_execution_mode: Literal["disabled", "shadow", "live"] = "disabled"
    default_max_loss_usdt: float = 20.0
    daily_max_loss_usdt: float = 500.0
    max_concurrent_positions: int = 4
    max_market_entry_deviation_pct: float = 0.15
    nearby_entry_market_deviation_pct: float = 0.15
    min_ai_confidence: float = 0.75
    allowed_symbols: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    symbol_max_loss_usdt: dict[str, float] = field(default_factory=dict)
    entry_range_order_style: str = "eager"
    take_profit_allocations: list[float] = field(default_factory=lambda: [40.0, 30.0, 30.0])
    trigger_backup_stop_buffer_bps: float = 50.0
    move_stop_to_breakeven_after_tp1: bool = True
    allow_vision_auto_trade: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def max_loss_for_symbol(self, symbol: str | None) -> float:
        return resolve_symbol_max_loss_usdt(
            default_max_loss_usdt=self.default_max_loss_usdt,
            symbol_max_loss_usdt=self.symbol_max_loss_usdt,
            symbol=symbol,
        )

    @property
    def management_planning_enabled(self) -> bool:
        return self.management_execution_mode == "shadow" or (
            self.management_execution_mode == "live" and self.auto_trade_enabled
        )

    @property
    def live_management_execution_enabled(self) -> bool:
        return (
            self.management_execution_mode == "live" and self.auto_trade_enabled
        )


def load_trading_settings(session_factory: sessionmaker) -> TradingSettings:
    """Load global trading settings, returning safe defaults when absent."""

    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == TRADING_SETTINGS_KEY)
            .one_or_none()
        )
        if row is None:
            return TradingSettings()
        try:
            payload = json.loads(row.value_json)
        except json.JSONDecodeError:
            return TradingSettings()
    return trading_settings_from_payload(payload)


def save_trading_settings(
    session_factory: sessionmaker,
    payload: dict[str, Any],
    *,
    updated_at: datetime | None = None,
) -> TradingSettings:
    """Validate and persist global trading settings."""

    settings = trading_settings_from_payload(payload)
    value_json = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == TRADING_SETTINGS_KEY)
            .one_or_none()
        )
        if row is None:
            row = TradingSetting(key=TRADING_SETTINGS_KEY, value_json=value_json)
            session.add(row)
        else:
            row.value_json = value_json
        row.updated_at = updated_at or datetime.now(UTC)
        session.commit()
    return settings


def apply_trading_settings_to_group_config(
    group_config: GroupConfig,
    settings: TradingSettings,
) -> GroupConfig:
    """Apply global Web risk defaults while preserving sender-level overrides."""

    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title=group.chat_title,
                chat_id=group.chat_id,
                enabled=group.enabled,
                tracked_senders=group.tracked_senders,
                custom_group_label=group.custom_group_label,
                sync_start_date=group.sync_start_date,
                sync_end_date=group.sync_end_date,
                ai_strategy_enabled=group.ai_strategy_enabled,
                trading_mode=group.trading_mode,
                max_loss_usdt=settings.default_max_loss_usdt,
                symbol_whitelist=settings.allowed_symbols.copy(),
                symbol_max_loss_usdt=settings.symbol_max_loss_usdt.copy(),
            )
            for group in group_config.groups
        ]
    )


def trading_settings_from_payload(payload: dict[str, Any] | None) -> TradingSettings:
    raw = payload or {}
    defaults = TradingSettings()
    allowed_symbols = _parse_symbol_list(raw.get("allowed_symbols"), defaults.allowed_symbols)
    symbol_max_loss_usdt = _parse_symbol_max_loss_usdt(raw.get("symbol_max_loss_usdt"))
    take_profit_allocations = _parse_allocations(
        raw.get("take_profit_allocations"),
        defaults.take_profit_allocations,
    )
    style = str(raw.get("entry_range_order_style") or defaults.entry_range_order_style)
    if style not in {"conservative", "eager"}:
        style = defaults.entry_range_order_style
    management_execution_mode = _management_execution_mode(
        raw.get("management_execution_mode", defaults.management_execution_mode)
    )
    return TradingSettings(
        auto_trade_enabled=_boolean_setting(
            raw,
            "auto_trade_enabled",
            defaults.auto_trade_enabled,
        ),
        management_execution_mode=management_execution_mode,
        default_max_loss_usdt=_positive_float(
            raw.get("default_max_loss_usdt"),
            defaults.default_max_loss_usdt,
        ),
        daily_max_loss_usdt=_positive_float(
            raw.get("daily_max_loss_usdt"),
            defaults.daily_max_loss_usdt,
        ),
        max_concurrent_positions=max(
            1,
            int(_positive_float(raw.get("max_concurrent_positions"), defaults.max_concurrent_positions)),
        ),
        max_market_entry_deviation_pct=_positive_float(
            raw.get("max_market_entry_deviation_pct"),
            defaults.max_market_entry_deviation_pct,
        ),
        nearby_entry_market_deviation_pct=_positive_float(
            raw.get("nearby_entry_market_deviation_pct"),
            defaults.nearby_entry_market_deviation_pct,
        ),
        min_ai_confidence=max(
            0.0,
            min(1.0, _positive_float(raw.get("min_ai_confidence"), defaults.min_ai_confidence)),
        ),
        allowed_symbols=allowed_symbols,
        symbol_max_loss_usdt=symbol_max_loss_usdt,
        entry_range_order_style=style,
        take_profit_allocations=take_profit_allocations,
        trigger_backup_stop_buffer_bps=_positive_float(
            raw.get("trigger_backup_stop_buffer_bps"),
            defaults.trigger_backup_stop_buffer_bps,
        ),
        move_stop_to_breakeven_after_tp1=_boolean_setting(
            raw,
            "move_stop_to_breakeven_after_tp1",
            defaults.move_stop_to_breakeven_after_tp1,
        ),
        allow_vision_auto_trade=_boolean_setting(
            raw,
            "allow_vision_auto_trade",
            defaults.allow_vision_auto_trade,
        ),
    )


def _management_execution_mode(
    value: Any,
) -> Literal["disabled", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError(
            "management_execution_mode must be disabled, shadow, or live"
        )
    normalized = value.strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError(
            "management_execution_mode must be disabled, shadow, or live"
        )
    return normalized


def _boolean_setting(
    payload: dict[str, Any],
    field_name: str,
    default: bool,
) -> bool:
    if field_name not in payload:
        return default
    value = payload[field_name]
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if parsed <= 0:
        return float(fallback)
    return parsed


def _parse_symbol_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = fallback
    symbols = []
    for item in raw_items:
        symbol = str(item).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or fallback.copy()


def resolve_symbol_max_loss_usdt(
    *,
    default_max_loss_usdt: float,
    symbol_max_loss_usdt: dict[str, float] | None,
    symbol: str | None,
) -> float:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol and symbol_max_loss_usdt:
        value = symbol_max_loss_usdt.get(normalized_symbol)
        if value is not None:
            return _positive_float(value, default_max_loss_usdt)
    return float(default_max_loss_usdt)


def _parse_symbol_max_loss_usdt(value: Any) -> dict[str, float]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                return _parse_symbol_max_loss_usdt(loaded)
        raw_items: list[tuple[Any, Any]] = []
        for item in stripped.replace(";", ",").split(","):
            if ":" not in item:
                continue
            symbol, loss = item.split(":", 1)
            raw_items.append((symbol, loss))
    elif isinstance(value, dict):
        raw_items = list(value.items())
    elif isinstance(value, list):
        raw_items = [
            (
                item.get("symbol"),
                item.get("max_loss_usdt"),
            )
            for item in value
            if isinstance(item, dict)
        ]
    else:
        raw_items = []

    parsed: dict[str, float] = {}
    for raw_symbol, raw_loss in raw_items:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        try:
            loss = float(raw_loss)
        except (TypeError, ValueError):
            continue
        if loss > 0:
            parsed[symbol] = loss
    return parsed


def _parse_allocations(value: Any, fallback: list[float]) -> list[float]:
    if isinstance(value, str):
        raw_items = value.replace("/", ",").replace("-", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = fallback
    allocations: list[float] = []
    for item in raw_items:
        try:
            parsed = float(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            allocations.append(parsed)
    if not allocations:
        return fallback.copy()
    total = sum(allocations)
    return [round(item * 100 / total, 8) for item in allocations]
