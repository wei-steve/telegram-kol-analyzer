"""Runtime trading settings persisted in the local database."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.models import TradingSetting


TRADING_SETTINGS_KEY = "global"
ENTRY_REVISION_ACTIVATION_KEY = "entry_revision_v2_activation"
MAX_FLOAT_DECIMAL = Decimal(str(sys.float_info.max))

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
    telegram_source_deletion_exit_enabled: bool = False
    management_execution_mode: Literal["disabled", "shadow", "live"] = "disabled"
    composite_management_v2_mode: Literal[
        "disabled", "shadow", "live"
    ] = "disabled"
    trigger_protection_stop_rescue_mode: Literal[
        "disabled", "shadow", "live"
    ] = "disabled"
    position_management_liveness_v2_mode: Literal[
        "disabled", "shadow", "live"
    ] = "disabled"
    entry_preamble_mode: Literal["disabled", "shadow", "live"] = "disabled"
    entry_message_assembly_v2_mode: Literal[
        "disabled", "shadow", "live"
    ] = "disabled"
    entry_revision_v2_mode: Literal["disabled", "shadow", "live"] = "disabled"
    multi_instruction_mode: Literal["disabled", "shadow", "live"] = "disabled"
    multi_instruction_activation_after_raw_message_id: int = 0
    instruction_execution_contract_mode: Literal[
        "disabled", "shadow", "live"
    ] = "disabled"
    instruction_execution_entry_after_item_id: int = 0
    instruction_execution_management_after_item_id: int = 0
    deepcoin_contract_specs_mode: Literal["static", "shadow", "live"] = "static"
    mimo_contract_mode: Literal["v1", "v2_live_adapter"] = "v1"
    message_lock_mode: Literal["global", "per_chat"] = "global"
    message_processing_max_parallel_chats: int = 20
    message_pipeline_mode: Literal["inline", "shadow", "queue"] = "inline"
    worker_command_mode: Literal["inline", "shadow", "queue"] = "inline"
    semantic_review_enabled: bool = False
    authoritative_gap_recovery_max_age_minutes: float = 15.0
    mimo_v2_activation_after_raw_message_id: int = 0
    default_max_loss_usdt: float = 20.0
    daily_max_loss_usdt: float = 500.0
    max_concurrent_positions: int = 4
    max_market_entry_deviation_pct: float = 0.15
    nearby_entry_market_deviation_pct: float = 0.15
    min_ai_confidence: float = 0.75
    revision_target_min_confidence: float = 0.70
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

    @property
    def effective_composite_management_v2_mode(
        self,
    ) -> Literal["disabled", "shadow", "live"]:
        if self.composite_management_v2_mode == "shadow":
            return "shadow"
        if (
            self.composite_management_v2_mode == "live"
            and self.auto_trade_enabled
            and self.management_execution_mode == "live"
        ):
            return "live"
        return "disabled"

    @property
    def effective_trigger_protection_stop_rescue_mode(
        self,
    ) -> Literal["disabled", "shadow", "live"]:
        if self.trigger_protection_stop_rescue_mode == "shadow":
            return "shadow"
        if (
            self.trigger_protection_stop_rescue_mode == "live"
            and self.auto_trade_enabled
            and self.management_execution_mode == "live"
        ):
            return "live"
        return "disabled"

    @property
    def effective_position_management_liveness_v2_mode(
        self,
    ) -> Literal["disabled", "shadow", "live"]:
        if self.position_management_liveness_v2_mode == "shadow":
            return "shadow"
        if (
            self.position_management_liveness_v2_mode == "live"
            and self.auto_trade_enabled
            and self.management_execution_mode == "live"
        ):
            return "live"
        return "disabled"

    def context_resolution_enabled_for_chat(self, chat_id: int) -> bool:
        return (
            self.context_resolution_enabled
            and self.live_management_execution_enabled
            and int(chat_id) in self.context_resolution_live_chat_ids
        )


class TradingSettingsConcurrencyConflict(ValueError):
    """The persisted concurrency tuple no longer matches the caller's view."""


def _settings_row_and_payload_in_session(session):
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == TRADING_SETTINGS_KEY)
        .one_or_none()
    )
    if row is None:
        return None, {}
    try:
        payload = json.loads(row.value_json)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return row, payload


def _load_trading_settings_in_session(session) -> TradingSettings:
    _row, payload = _settings_row_and_payload_in_session(session)
    try:
        return trading_settings_from_payload(payload)
    except ValueError:
        return TradingSettings()


def _persist_trading_settings_in_session(
    session,
    settings: TradingSettings,
    *,
    updated_at: datetime,
    row=None,
    persisted_payload: dict[str, Any] | None = None,
) -> None:
    if row is None or persisted_payload is None:
        row, persisted_payload = _settings_row_and_payload_in_session(session)
    prior_revision_mode = str(
        persisted_payload.get("entry_revision_v2_mode") or "disabled"
    )
    stored_payload = settings.to_dict()
    if "entry_preamble_live_chat_ids" in persisted_payload:
        stored_payload["entry_preamble_live_chat_ids"] = persisted_payload[
            "entry_preamble_live_chat_ids"
        ]
    value_json = json.dumps(stored_payload, ensure_ascii=False, sort_keys=True)
    if row is None:
        row = TradingSetting(key=TRADING_SETTINGS_KEY, value_json=value_json)
        session.add(row)
    else:
        row.value_json = value_json
    row.updated_at = updated_at
    if settings.entry_revision_v2_mode != prior_revision_mode:
        activation = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_ACTIVATION_KEY)
            .one_or_none()
        )
        if activation is None:
            activation = TradingSetting(key=ENTRY_REVISION_ACTIVATION_KEY)
            session.add(activation)
        activation.value_json = json.dumps(
            {"mode": settings.entry_revision_v2_mode}, sort_keys=True
        )
        activation.updated_at = updated_at


def load_trading_settings(session_factory: sessionmaker) -> TradingSettings:
    """Load global trading settings, returning safe defaults when absent."""

    with session_factory() as session:
        return _load_trading_settings_in_session(session)


def save_trading_settings(
    session_factory: sessionmaker,
    payload: dict[str, Any],
    *,
    updated_at: datetime | None = None,
) -> TradingSettings:
    """Validate and persist global trading settings."""

    with session_factory() as session:
        row, persisted_payload = _settings_row_and_payload_in_session(session)
        merged_payload = {**persisted_payload, **payload}
        settings = trading_settings_from_payload(merged_payload)
        _persist_trading_settings_in_session(
            session,
            settings,
            updated_at=updated_at or datetime.now(UTC),
            row=row,
            persisted_payload=persisted_payload,
        )
        session.commit()
    return settings


def transition_message_concurrency_settings(
    session_factory: sessionmaker,
    payload: dict[str, Any],
    *,
    updated_at: datetime | None = None,
) -> TradingSettings:
    """Atomically compare and replace the message concurrency tuple."""

    expected_mode_key = "message_lock_expected_mode"
    expected_cap_key = "message_processing_expected_max_parallel_chats"
    target_mode_key = "message_lock_mode"
    target_cap_key = "message_processing_max_parallel_chats"
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row, persisted_payload = _settings_row_and_payload_in_session(session)
        current = trading_settings_from_payload(persisted_payload)

        if (
            current.message_lock_mode == "global"
            and payload.get(target_mode_key) == "per_chat"
        ):
            required = {
                expected_mode_key,
                expected_cap_key,
                target_mode_key,
                target_cap_key,
            }
            if not required.issubset(payload):
                raise ValueError(
                    "global to per_chat requires both target and expected fields"
                )

        candidate_payload = dict(payload)
        candidate_payload.pop(expected_mode_key, None)
        candidate_payload.pop(expected_cap_key, None)
        candidate = trading_settings_from_payload(
            {**current.to_dict(), **candidate_payload}
        )
        if target_mode_key in payload:
            if expected_mode_key not in payload:
                raise ValueError(
                    "message lock changes require the expected message lock mode"
                )
            expected_mode = _message_lock_mode(payload[expected_mode_key])
            if expected_mode != current.message_lock_mode:
                raise TradingSettingsConcurrencyConflict(
                    "expected message lock mode does not match persisted settings"
                )
        if target_cap_key in payload:
            if expected_cap_key not in payload:
                raise ValueError(
                    "parallel chat limit changes require the expected parallel chat limit"
                )
            expected_cap = _bounded_int_setting(
                payload[expected_cap_key],
                field_name=expected_cap_key,
                minimum=1,
                maximum=20,
            )
            if expected_cap != current.message_processing_max_parallel_chats:
                raise TradingSettingsConcurrencyConflict(
                    "expected parallel chat limit does not match persisted settings"
                )

        _persist_trading_settings_in_session(
            session,
            candidate,
            updated_at=updated_at or datetime.now(UTC),
            row=row,
            persisted_payload=persisted_payload,
        )
        session.commit()
    return candidate


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
    composite_management_v2_mode = _composite_management_v2_mode(
        raw.get(
            "composite_management_v2_mode",
            defaults.composite_management_v2_mode,
        )
    )
    trigger_protection_stop_rescue_mode = _trigger_protection_stop_rescue_mode(
        raw.get(
            "trigger_protection_stop_rescue_mode",
            defaults.trigger_protection_stop_rescue_mode,
        )
    )
    position_management_liveness_v2_mode = _rollout_mode(
        raw.get(
            "position_management_liveness_v2_mode",
            defaults.position_management_liveness_v2_mode,
        ),
        field_name="position_management_liveness_v2_mode",
    )
    entry_preamble_mode = _entry_preamble_mode(
        raw.get("entry_preamble_mode", defaults.entry_preamble_mode)
    )
    entry_message_assembly_v2_mode = _rollout_mode(
        raw.get(
            "entry_message_assembly_v2_mode",
            defaults.entry_message_assembly_v2_mode,
        ),
        field_name="entry_message_assembly_v2_mode",
    )
    entry_revision_v2_mode = _rollout_mode(
        raw.get("entry_revision_v2_mode", defaults.entry_revision_v2_mode),
        field_name="entry_revision_v2_mode",
    )
    multi_instruction_mode = _rollout_mode(
        raw.get("multi_instruction_mode", defaults.multi_instruction_mode),
        field_name="multi_instruction_mode",
    )
    multi_instruction_activation_after_raw_message_id = _nonnegative_int_setting(
        raw.get(
            "multi_instruction_activation_after_raw_message_id",
            defaults.multi_instruction_activation_after_raw_message_id,
        ),
        field_name="multi_instruction_activation_after_raw_message_id",
    )
    instruction_execution_contract_mode = _rollout_mode(
        raw.get(
            "instruction_execution_contract_mode",
            defaults.instruction_execution_contract_mode,
        ),
        field_name="instruction_execution_contract_mode",
    )
    instruction_execution_entry_after_item_id = _nonnegative_int_setting(
        raw.get(
            "instruction_execution_entry_after_item_id",
            defaults.instruction_execution_entry_after_item_id,
        ),
        field_name="instruction_execution_entry_after_item_id",
    )
    instruction_execution_management_after_item_id = _nonnegative_int_setting(
        raw.get(
            "instruction_execution_management_after_item_id",
            defaults.instruction_execution_management_after_item_id,
        ),
        field_name="instruction_execution_management_after_item_id",
    )
    deepcoin_contract_specs_mode = _deepcoin_contract_specs_mode(
        raw.get(
            "deepcoin_contract_specs_mode",
            defaults.deepcoin_contract_specs_mode,
        )
    )
    mimo_contract_mode = _mimo_contract_mode(
        raw.get("mimo_contract_mode", defaults.mimo_contract_mode)
    )
    message_lock_mode = _message_lock_mode(
        raw.get("message_lock_mode", defaults.message_lock_mode)
    )
    message_processing_max_parallel_chats = _bounded_int_setting(
        raw.get(
            "message_processing_max_parallel_chats",
            defaults.message_processing_max_parallel_chats,
        ),
        field_name="message_processing_max_parallel_chats",
        minimum=1,
        maximum=20,
    )
    message_pipeline_mode = _message_pipeline_mode(
        raw.get("message_pipeline_mode", defaults.message_pipeline_mode)
    )
    worker_command_mode = _worker_command_mode(
        raw.get("worker_command_mode", defaults.worker_command_mode)
    )
    authoritative_gap_recovery_max_age_minutes = _positive_float(
        raw.get("authoritative_gap_recovery_max_age_minutes"),
        defaults.authoritative_gap_recovery_max_age_minutes,
    )
    mimo_v2_activation_after_raw_message_id = _nonnegative_int_setting(
        raw.get(
            "mimo_v2_activation_after_raw_message_id",
            defaults.mimo_v2_activation_after_raw_message_id,
        ),
        field_name="mimo_v2_activation_after_raw_message_id",
    )
    return TradingSettings(
        auto_trade_enabled=_boolean_setting(
            raw,
            "auto_trade_enabled",
            defaults.auto_trade_enabled,
        ),
        telegram_source_deletion_exit_enabled=_boolean_setting(
            raw,
            "telegram_source_deletion_exit_enabled",
            defaults.telegram_source_deletion_exit_enabled,
        ),
        management_execution_mode=management_execution_mode,
        composite_management_v2_mode=composite_management_v2_mode,
        trigger_protection_stop_rescue_mode=trigger_protection_stop_rescue_mode,
        position_management_liveness_v2_mode=position_management_liveness_v2_mode,
        entry_preamble_mode=entry_preamble_mode,
        entry_message_assembly_v2_mode=entry_message_assembly_v2_mode,
        entry_revision_v2_mode=entry_revision_v2_mode,
        multi_instruction_mode=multi_instruction_mode,
        multi_instruction_activation_after_raw_message_id=(
            multi_instruction_activation_after_raw_message_id
        ),
        instruction_execution_contract_mode=instruction_execution_contract_mode,
        instruction_execution_entry_after_item_id=(
            instruction_execution_entry_after_item_id
        ),
        instruction_execution_management_after_item_id=(
            instruction_execution_management_after_item_id
        ),
        deepcoin_contract_specs_mode=deepcoin_contract_specs_mode,
        mimo_contract_mode=mimo_contract_mode,
        mimo_v2_activation_after_raw_message_id=(
            mimo_v2_activation_after_raw_message_id
        ),
        message_lock_mode=message_lock_mode,
        message_processing_max_parallel_chats=(
            message_processing_max_parallel_chats
        ),
        message_pipeline_mode=message_pipeline_mode,
        worker_command_mode=worker_command_mode,
        semantic_review_enabled=_boolean_setting(
            raw,
            "semantic_review_enabled",
            defaults.semantic_review_enabled,
        ),
        authoritative_gap_recovery_max_age_minutes=(
            authoritative_gap_recovery_max_age_minutes
        ),
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
        revision_target_min_confidence=max(
            0.0,
            min(
                1.0,
                _positive_float(
                    raw.get("revision_target_min_confidence"),
                    defaults.revision_target_min_confidence,
                ),
            ),
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


def _composite_management_v2_mode(
    value: Any,
) -> Literal["disabled", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError(
            "composite_management_v2_mode must be disabled, shadow, or live"
        )
    normalized = value.strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError(
            "composite_management_v2_mode must be disabled, shadow, or live"
        )
    return normalized


def _trigger_protection_stop_rescue_mode(
    value: Any,
) -> Literal["disabled", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError(
            "trigger_protection_stop_rescue_mode must be disabled, shadow, or live"
        )
    normalized = value.strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError(
            "trigger_protection_stop_rescue_mode must be disabled, shadow, or live"
        )
    return normalized


def _rollout_mode(
    value: Any,
    *,
    field_name: str,
) -> Literal["disabled", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be disabled, shadow, or live")
    normalized = value.strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError(f"{field_name} must be disabled, shadow, or live")
    return normalized


def _deepcoin_contract_specs_mode(
    value: Any,
) -> Literal["static", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError(
            "deepcoin_contract_specs_mode must be static, shadow, or live"
        )
    normalized = value.strip().lower()
    if normalized not in {"static", "shadow", "live"}:
        raise ValueError(
            "deepcoin_contract_specs_mode must be static, shadow, or live"
        )
    return normalized


def _mimo_contract_mode(
    value: Any,
) -> Literal["v1", "v2_live_adapter"]:
    if not isinstance(value, str):
        raise ValueError("mimo_contract_mode must be v1 or v2_live_adapter")
    normalized = value.strip().lower()
    if normalized not in {"v1", "v2_live_adapter"}:
        raise ValueError("mimo_contract_mode must be v1 or v2_live_adapter")
    return normalized


def _message_lock_mode(value: Any) -> Literal["global", "per_chat"]:
    if not isinstance(value, str):
        raise ValueError("message_lock_mode must be global or per_chat")
    normalized = value.strip().lower()
    if normalized not in {"global", "per_chat"}:
        raise ValueError("message_lock_mode must be global or per_chat")
    return normalized


def _message_pipeline_mode(value: Any) -> Literal["inline", "shadow", "queue"]:
    if not isinstance(value, str):
        raise ValueError("message_pipeline_mode must be inline, shadow, or queue")
    normalized = value.strip().lower()
    if normalized not in {"inline", "shadow", "queue"}:
        raise ValueError("message_pipeline_mode must be inline, shadow, or queue")
    return normalized


def _worker_command_mode(value: Any) -> Literal["inline", "shadow", "queue"]:
    if not isinstance(value, str):
        raise ValueError("worker_command_mode must be inline, shadow, or queue")
    normalized = value.strip().lower()
    if normalized not in {"inline", "shadow", "queue"}:
        raise ValueError("worker_command_mode must be inline, shadow, or queue")
    return normalized


def _entry_preamble_mode(
    value: Any,
) -> Literal["disabled", "shadow", "live"]:
    if not isinstance(value, str):
        raise ValueError("entry_preamble_mode must be disabled, shadow, or live")
    normalized = value.strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError("entry_preamble_mode must be disabled, shadow, or live")
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


def _nonnegative_int_setting(value: Any, *, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _bounded_int_setting(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f"{field_name} must be an integer from {minimum} to {maximum}"
        )
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
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_FLOAT_DECIMAL:
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
