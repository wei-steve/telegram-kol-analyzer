"""Runtime trading settings persisted in the local database."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.models import TradingSetting


TRADING_SETTINGS_KEY = "global"

ENTRY_THRESHOLD_KEYS = (
    "market_leg_threshold",
    "first_limit_offset",
    "second_limit_offset",
)

LEGACY_SYMBOL_ENTRY_THRESHOLD_DEFAULTS = {
    "BTC": {
        "market_leg_threshold": "200",
        "first_limit_offset": "90",
        "second_limit_offset": "90",
    },
    "ETH": {
        "market_leg_threshold": "4",
        "first_limit_offset": "2",
        "second_limit_offset": "2",
    },
}


@dataclass(frozen=True, slots=True)
class SymbolEntryThresholds:
    market_leg_threshold: Decimal
    first_limit_offset: Decimal
    second_limit_offset: Decimal

    @classmethod
    def zero(cls) -> "SymbolEntryThresholds":
        return cls(Decimal("0"), Decimal("0"), Decimal("0"))

    def to_dict(self) -> dict[str, str]:
        return {
            "market_leg_threshold": _canonical_decimal(self.market_leg_threshold),
            "first_limit_offset": _canonical_decimal(self.first_limit_offset),
            "second_limit_offset": _canonical_decimal(self.second_limit_offset),
        }


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
    symbol_entry_thresholds: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            symbol: values.copy()
            for symbol, values in LEGACY_SYMBOL_ENTRY_THRESHOLD_DEFAULTS.items()
        }
    )
    entry_range_order_style: str = "eager"
    take_profit_allocations: list[float] = field(default_factory=lambda: [40.0, 30.0, 30.0])
    trigger_backup_stop_buffer_bps: float = 20.0
    move_stop_to_breakeven_after_tp1: bool = True
    allow_vision_auto_trade: bool = True
    context_resolution_enabled: bool = False
    context_resolution_live_chat_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def max_loss_for_symbol(self, symbol: str | None) -> float:
        return resolve_symbol_max_loss_usdt(
            default_max_loss_usdt=self.default_max_loss_usdt,
            symbol_max_loss_usdt=self.symbol_max_loss_usdt,
            symbol=symbol,
        )

    def entry_thresholds_for_symbol(
        self,
        symbol: str | None,
    ) -> SymbolEntryThresholds:
        normalized_symbol = str(symbol or "").strip().upper()
        values = self.symbol_entry_thresholds.get(normalized_symbol)
        if not values:
            return SymbolEntryThresholds.zero()
        return SymbolEntryThresholds(
            **{
                key: Decimal(values.get(key, "0"))
                for key in ENTRY_THRESHOLD_KEYS
            }
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

    def context_resolution_enabled_for_chat(self, chat_id: int) -> bool:
        return (
            self.context_resolution_enabled
            and self.live_management_execution_enabled
            and int(chat_id) in self.context_resolution_live_chat_ids
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
    try:
        return trading_settings_from_payload(payload)
    except ValueError:
        return TradingSettings()


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
    symbol_entry_thresholds = _parse_symbol_entry_thresholds(
        raw.get("symbol_entry_thresholds")
        if "symbol_entry_thresholds" in raw
        else LEGACY_SYMBOL_ENTRY_THRESHOLD_DEFAULTS
    )
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
        symbol_entry_thresholds=symbol_entry_thresholds,
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
        context_resolution_enabled=_boolean_setting(
            raw,
            "context_resolution_enabled",
            defaults.context_resolution_enabled,
        ),
        context_resolution_live_chat_ids=_parse_context_resolution_chat_ids(
            raw.get(
                "context_resolution_live_chat_ids",
                defaults.context_resolution_live_chat_ids,
            )
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


def _parse_context_resolution_chat_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("context_resolution_live_chat_ids must be a list of nonzero integers")
    if any(isinstance(item, bool) or not isinstance(item, int) or item == 0 for item in value):
        raise ValueError("context_resolution_live_chat_ids must be a list of nonzero integers")
    if len(set(value)) != len(value):
        raise ValueError("context_resolution_live_chat_ids must not contain duplicates")
    return list(value)


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


def _canonical_decimal(value: Decimal) -> str:
    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _parse_entry_threshold_value(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise ValueError(f"{field_name} must be a non-negative finite decimal")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{field_name} must be a non-negative finite decimal")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a non-negative finite decimal"
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name} must be a non-negative finite decimal")
    return parsed


def _parse_symbol_entry_thresholds(
    value: Any,
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("symbol_entry_thresholds must be an object")

    parsed: dict[str, dict[str, str]] = {}
    for raw_symbol, raw_thresholds in value.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol_entry_thresholds symbol must not be empty")
        if not isinstance(raw_thresholds, dict):
            raise ValueError(
                f"symbol_entry_thresholds.{symbol} must be an object"
            )
        thresholds = SymbolEntryThresholds(
            **{
                key: _parse_entry_threshold_value(
                    raw_thresholds.get(key, "0"),
                    f"symbol_entry_thresholds.{symbol}.{key}",
                )
                for key in ENTRY_THRESHOLD_KEYS
            }
        )
        parsed[symbol] = thresholds.to_dict()
    return parsed


def _parse_allocations(value: Any, fallback: list[float]) -> list[float]:
    if value is None:
        raw_items = fallback
    elif isinstance(value, str):
        raw_items = value.replace("/", ",").replace("-", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ValueError("take_profit_allocations must be a comma-separated list or array")
    if not 1 <= len(raw_items) <= 5:
        raise ValueError("take_profit_allocations must contain one through five positive values")
    allocations: list[float] = []
    for item in raw_items:
        try:
            parsed = float(item)
        except (TypeError, ValueError):
            raise ValueError("take_profit_allocations must contain positive numbers") from None
        if parsed <= 0:
            raise ValueError("take_profit_allocations must contain positive numbers")
        allocations.append(parsed)
    total = sum(allocations)
    return [round(item * 100 / total, 8) for item in allocations]
