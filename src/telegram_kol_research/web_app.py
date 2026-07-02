"""FastAPI app for the Telegram web workbench."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio
import logging
import re
import time

import httpx
from sqlalchemy import func

try:
    from fastapi import FastAPI, Request
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
except (
    ModuleNotFoundError
) as exc:  # pragma: no cover - import guard for missing optional deps
    raise RuntimeError(
        "FastAPI is not installed in the current environment. Install project dependencies first."
    ) from exc

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.auto_trade_execution import auto_process_message_trade_signal
from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiProviderConfig,
    AiRecognitionConfig,
    build_ai_prompt_views,
    load_ai_recognition_config,
    save_ai_recognition_config,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_execution_actions import recover_missing_position_protections
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import update_group_automation_settings
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.message_recognition import recognize_message_now
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.recognition_experiments import run_mimo_direct_for_message
from telegram_kol_research.llm_chat import (
    build_proxy_chat_payload,
    build_scope_context,
    build_source_reference_map,
    extract_recent_message_limit,
    load_llm_proxy_config,
    request_grounded_chat_answer,
)
from telegram_kol_research.execution_bindings import bind_deepcoin_position_to_lifecycle
from telegram_kol_research.execution_bindings import list_active_positions
from telegram_kol_research.execution_bindings import reconcile_deepcoin_execution_bindings
from telegram_kol_research.execution_bindings import sync_manual_closed_deepcoin_positions
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_runner import run_recovery_dry_run
from telegram_kol_research.strategy_alerts import (
    StrategyAlertConfig,
    load_strategy_alert_config,
    strategy_alerts_enabled,
)
from telegram_kol_research.trading_settings import (
    load_trading_settings,
    save_trading_settings,
)
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.web_queries import (
    load_database_freshness,
    load_group_messages,
    load_group_rows,
    list_execution_strategy_overview,
    load_lifecycle_counts,
    load_lifecycle_counts_by_chat_id,
    list_exited_strategies,
    list_holding_strategies,
    mark_strategy_lifecycle_manual_close,
    list_pending_strategies,
    load_messages_in_time_window,
    load_selected_messages,
)
from telegram_kol_research.lifecycle_monitor import (
    LifecycleMonitor,
    LifecycleMonitorConfig,
)
from telegram_kol_research.telegram_live_listener import launch_live_listener_task, run_live_listener
from telegram_kol_research.telegram_live_listener import run_periodic_reconcile, run_reconcile_once
from telegram_kol_research.telegram_bot_commands import run_telegram_bot_command_loop
from telegram_kol_research.telegram_client import create_telegram_client, load_telegram_auth_config, maybe_await
from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
)


REFRESH_TIMEOUT_SECONDS = 180
SESSION_LOCK_OWNER_PID_PATTERN = re.compile(r"owner pid=(\d+)")
logger = logging.getLogger("uvicorn.error")


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 1)


def _build_trader_dashboard_state(
    *,
    groups: list[dict[str, Any]],
    group_config: GroupConfig,
    active_positions: list[dict[str, Any]],
    pending_entry_signals: list[dict[str, Any]],
    holding_positions: list[dict[str, Any]] | None = None,
    exited_positions: list[dict[str, Any]] | None = None,
    lifecycle_counts: dict[str, int] | None = None,
    lifecycle_counts_by_chat_id: dict[int, dict[str, int]] | None = None,
    live_listener_enabled: bool = False,
    refresh_mode_label: str = "",
) -> dict[str, Any]:
    entered_count = len(active_positions)
    pending_count = len(pending_entry_signals)
    holding_count = (
        len(holding_positions)
        if holding_positions is not None
        else len(active_positions)
    )
    exited_count = (
        (lifecycle_counts or {}).get("exited", 0)
        + (lifecycle_counts or {}).get("expired", 0)
        if lifecycle_counts is not None or exited_positions is not None
        else len(exited_positions) if exited_positions is not None else 0
    )
    ready_count = sum(
        1
        for item in pending_entry_signals
        if not item.get("deepcoin_order_draft", {}).get("blocking_reason_codes")
    )
    blocked_count = sum(
        1
        for item in pending_entry_signals
        if item.get("deepcoin_order_draft", {}).get("blocking_reason_codes")
    )

    holding_counts_by_chat_id: dict[int, int] = {}
    holding_count_source = holding_positions if holding_positions is not None else active_positions
    for h in holding_count_source:
        chat_id = h.get("chat_id")
        if chat_id is not None:
            holding_counts_by_chat_id[int(chat_id)] = (
                holding_counts_by_chat_id.get(int(chat_id), 0) + 1
            )
    pending_counts_by_chat_id: dict[int, int] = {}
    for item in pending_entry_signals:
        chat_id = item.get("chat_id")
        if chat_id is not None:
            pending_counts_by_chat_id[int(chat_id)] = (
                pending_counts_by_chat_id.get(int(chat_id), 0) + 1
            )
    work_counts_by_chat_id: dict[int, int] = {}
    for chat_id, count in holding_counts_by_chat_id.items():
        work_counts_by_chat_id[chat_id] = count
    for chat_id, count in pending_counts_by_chat_id.items():
        work_counts_by_chat_id[chat_id] = (
            work_counts_by_chat_id.get(chat_id, 0) + count
        )

    config_by_chat_id = {
        int(item.chat_id): item for item in group_config.groups if item.chat_id is not None
    }
    config_by_title = {item.chat_title: item for item in group_config.groups}
    group_rows = []
    for group in groups:
        group_chat_id = int(group["chat_id"])
        group_lifecycle_counts = (
            lifecycle_counts_by_chat_id or {}
        ).get(group_chat_id)
        group_holding_count = holding_counts_by_chat_id.get(group_chat_id, 0)
        group_pending_count = (
            group_lifecycle_counts.get("pending_entry", 0)
            if group_lifecycle_counts is not None
            else pending_counts_by_chat_id.get(group_chat_id, 0)
        )
        config_item = config_by_chat_id.get(int(group["chat_id"])) or config_by_title.get(
            str(group.get("raw_title") or group.get("title") or "")
        )
        group_rows.append(
            {
                **group,
                "strategy_work_count": group_holding_count + group_pending_count,
                "holding_count": group_holding_count,
                "pending_count": group_pending_count,
                "ai_strategy_enabled": (
                    bool(config_item.ai_strategy_enabled) if config_item is not None else False
                ),
                "auto_trade_enabled": (
                    config_item.trading_mode == "auto_trade"
                    if config_item is not None
                    else False
                ),
            }
        )

    return {
        "entered_count": entered_count,
        "holding_count": holding_count,
        "pending_count": pending_count,
        "exited_count": exited_count,
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "group_rows": group_rows,
        "live_listener_enabled": live_listener_enabled,
        "refresh_mode_label": refresh_mode_label,
    }


def _build_strategy_kpi_counts(
    *,
    selected_chat_id: int | None,
    holding_positions: list[dict[str, Any]],
    pending_entry_signals: list[dict[str, Any]],
    exited_positions: list[dict[str, Any]] | None = None,
    lifecycle_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    def belongs_to_selected(item: dict[str, Any]) -> bool:
        if selected_chat_id is None:
            return True
        chat_id = item.get("chat_id")
        return chat_id is not None and int(chat_id) == int(selected_chat_id)

    selected_holding = [item for item in holding_positions if belongs_to_selected(item)]
    selected_pending = [item for item in pending_entry_signals if belongs_to_selected(item)]
    if lifecycle_counts is not None:
        holding_count = len(selected_holding)
        pending_count = lifecycle_counts.get("pending_entry", len(selected_pending))
        exited_count = lifecycle_counts.get("exited", 0) + lifecycle_counts.get("expired", 0)
    else:
        holding_count = len(selected_holding)
        pending_count = len(selected_pending)
        selected_exited = [
            item for item in (exited_positions or []) if belongs_to_selected(item)
        ]
        exited_count = len(selected_exited)
    ready_count = sum(
        1
        for item in selected_pending
        if not item.get("deepcoin_order_draft", {}).get("blocking_reason_codes")
    )
    return {
        "holding_count": holding_count,
        "pending_count": pending_count,
        "exited_count": exited_count,
        "ready_count": ready_count,
    }


def _symbol_whitelist_by_chat_id(group_config: GroupConfig) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for item in group_config.groups:
        if item.chat_id is None:
            continue
        symbols = {
            str(symbol).upper()
            for symbol in (item.symbol_whitelist or [])
            if str(symbol).strip()
        }
        if symbols:
            result[int(item.chat_id)] = symbols
    return result


def _group_label_by_chat_id(group_config: GroupConfig) -> dict[int, str]:
    return {
        int(item.chat_id): item.custom_group_label or item.chat_title
        for item in group_config.groups
        if item.chat_id is not None
    }


def _load_deepcoin_live_position_rows(
    session_factory,
    *,
    deepcoin_client_factory,
    group_label_by_chat_id: dict[int, str],
) -> list[dict[str, object]]:
    try:
        deepcoin_client = deepcoin_client_factory()
        positions = deepcoin_client.list_positions()
    except Exception:
        logger.exception("Deepcoin live position load failed")
        return []

    active_positions = [
        position for position in positions if _deepcoin_position_has_size(position)
    ]
    if not active_positions:
        return []
    tpsl_orders_by_position_key = _load_deepcoin_tpsl_orders_by_position_key(
        deepcoin_client,
        active_positions,
    )

    with session_factory() as session:
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .all()
        )
        bindings_by_pos_id: dict[str, ExecutionBinding] = {}
        for binding in bindings:
            for pos_id in _split_binding_ids(binding.pos_id):
                bindings_by_pos_id[pos_id] = binding

        rows: list[dict[str, object]] = []
        for position in active_positions:
            pos_id = _first_position_string(position, "posId", "pos_id", "id")
            protection_key = _deepcoin_position_protection_key(position)
            tpsl_order = tpsl_orders_by_position_key.get(
                protection_key
            ) or tpsl_orders_by_position_key.get(
                (
                    protection_key[0],
                    protection_key[1],
                    "*",
                    protection_key[3],
                )
            )
            binding = bindings_by_pos_id.get(pos_id or "")
            lifecycle = None
            if binding is not None:
                lifecycle = (
                    session.query(StrategyLifecycle)
                    .filter(StrategyLifecycle.chat_id == binding.chat_id)
                    .filter(StrategyLifecycle.message_id == binding.message_id)
                    .filter(StrategyLifecycle.symbol == binding.symbol)
                    .filter(StrategyLifecycle.side == binding.side)
                    .order_by(StrategyLifecycle.id.desc())
                    .first()
                )
            symbol = (
                binding.symbol
                if binding is not None
                else _symbol_from_deepcoin_inst_id(position.get("instId"))
            )
            side = (
                binding.side
                if binding is not None
                else _normalize_deepcoin_position_side(
                    position.get("posSide") or position.get("side")
                )
            )
            rows.append(
                {
                    "lifecycle_id": lifecycle.id if lifecycle is not None else None,
                    "chat_id": binding.chat_id if binding is not None else None,
                    "group_label": (
                        group_label_by_chat_id.get(binding.chat_id, str(binding.chat_id))
                        if binding is not None
                        else "未绑定实盘仓位"
                    ),
                    "message_id": binding.message_id if binding is not None else None,
                    "symbol": symbol,
                    "side": side,
                    "status": "live",
                    "lifecycle_status": lifecycle.lifecycle_status if lifecycle is not None else None,
                    "entry_price_actual": _float_or_none(position.get("avgPx")),
                    "stop_loss_text": _position_text_value(
                        _deepcoin_tpsl_price(tpsl_order, "sl") if tpsl_order else None
                    ),
                    "take_profit_text": _position_text_value(
                        _deepcoin_tpsl_price(tpsl_order, "tp") if tpsl_order else None
                    ),
                    "protection_status": "protected" if tpsl_order else "unprotected",
                    "execution_status": binding.status if binding is not None else "unbound_live_position",
                    "exchange_status": "position_active",
                    "pos_id": pos_id,
                    "order_id": binding.order_id if binding is not None else None,
                    "position_size_text": _position_size_label(position),
                    "original_text": (
                        "这个交易所仓位没有本地 KOL 绑定，请人工确认归因。"
                        if binding is None
                        else None
                    ),
                    "attribution_candidates": (
                        _load_live_position_attribution_candidates(
                            session,
                            symbol=symbol,
                            side=side,
                            entry_price_actual=_float_or_none(position.get("avgPx")),
                            stop_loss=_float_or_none(
                                _deepcoin_tpsl_price(tpsl_order, "sl") if tpsl_order else None
                            ),
                            take_profit=_float_or_none(
                                _deepcoin_tpsl_price(tpsl_order, "tp") if tpsl_order else None
                            ),
                            group_label_by_chat_id=group_label_by_chat_id,
                        )
                        if binding is None
                        else []
                    ),
                }
            )
        return rows


def _load_deepcoin_tpsl_orders_by_position_key(
    deepcoin_client,
    positions: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not hasattr(deepcoin_client, "list_trigger_orders_pending"):
        return {}
    orders_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    instrument_ids = {
        str(position.get("instId") or "")
        for position in positions
        if str(position.get("instId") or "")
    }
    for inst_id in instrument_ids:
        try:
            orders = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
        except Exception:
            logger.exception("Deepcoin pending trigger order load failed for %s", inst_id)
            continue
        for order in orders:
            if str(order.get("triggerOrderType") or "").upper() != "TPSL":
                continue
            for key in _deepcoin_tpsl_order_position_keys(order):
                current = orders_by_key.get(key)
                orders_by_key[key] = _merge_deepcoin_tpsl_orders(current, order)
    return orders_by_key


def _deepcoin_position_protection_key(position: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(position.get("instId") or "").upper(),
        _normalize_deepcoin_position_side(position.get("posSide") or position.get("side")),
        _normalize_position_amount(position.get("pos")),
        str(position.get("cTime") or position.get("uTime") or ""),
    )


def _deepcoin_tpsl_order_position_keys(order: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    ctime = str(order.get("cTime") or order.get("uTime") or "")
    if not ctime:
        return []
    base = (
        str(order.get("instId") or "").upper(),
        _normalize_deepcoin_position_side(order.get("posSide") or order.get("side")),
    )
    size = _normalize_position_amount(order.get("sz"))
    keys = [(*base, size, ctime)]
    if size == "0":
        keys.append((*base, "*", ctime))
    return keys


def _merge_deepcoin_tpsl_orders(
    first: dict[str, Any] | None,
    second: dict[str, Any],
) -> dict[str, Any]:
    if first is None:
        return dict(second)
    merged = dict(first)
    for key in ("slTriggerPrice", "tpTriggerPrice", "closeSLTriggerPrice", "closeTPTriggerPrice"):
        if _is_nonzero_price(second.get(key)):
            merged[key] = second.get(key)
    return merged


def _is_nonzero_price(value: Any) -> bool:
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return bool(str(value or "").strip())


def _normalize_position_amount(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value or "").strip()


def _deepcoin_tpsl_price(order: dict[str, Any] | None, kind: str) -> Any:
    if not order:
        return None
    keys = (
        ("slTriggerPrice", "closeSLTriggerPrice")
        if kind == "sl"
        else ("tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = order.get(key)
        if _is_nonzero_price(value):
            return value
    return None


def _load_live_position_attribution_candidates(
    session,
    *,
    symbol: str,
    side: str,
    entry_price_actual: float | None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    group_label_by_chat_id: dict[int, str],
    limit: int = 6,
) -> list[dict[str, object]]:
    rows = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.symbol == symbol)
        .filter(StrategyLifecycle.side == side)
        .filter(StrategyLifecycle.lifecycle_status.in_(["entered", "pending_entry"]))
        .filter(StrategyLifecycle.execution_binding_id.is_(None))
        .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
        .limit(max(limit * 4, 24))
        .all()
    )
    result: list[dict[str, object]] = []
    for lifecycle in rows:
        score, reasons = _score_live_position_attribution(
            lifecycle,
            entry_price_actual=entry_price_actual,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if score <= 0:
            continue
        result.append(
            {
                "lifecycle_id": lifecycle.id,
                "group_label": group_label_by_chat_id.get(
                    lifecycle.chat_id,
                    str(lifecycle.chat_id),
                ),
                "message_id": lifecycle.message_id,
                "lifecycle_status": lifecycle.lifecycle_status,
                "entry": lifecycle.entry_price_actual
                or _format_range(lifecycle.entry_range_low, lifecycle.entry_range_high),
                "stop_loss": lifecycle.stop_loss,
                "take_profit": lifecycle.take_profit,
                "match_score": score,
                "match_reasons": reasons,
                "bindable": False,
            }
        )
    result.sort(key=lambda item: (-int(item["match_score"]), str(item["group_label"])))
    if result and _has_unique_confident_attribution(result):
        result[0]["bindable"] = True
    return result[:limit]


def _has_unique_confident_attribution(candidates: list[dict[str, object]]) -> bool:
    if not candidates:
        return False
    top_score = int(candidates[0].get("match_score") or 0)
    second_score = int(candidates[1].get("match_score") or 0) if len(candidates) > 1 else 0
    return top_score >= 70 and second_score < 70


def _score_live_position_attribution(
    lifecycle: StrategyLifecycle,
    *,
    entry_price_actual: float | None,
    stop_loss: float | None,
    take_profit: float | None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    entry_score = _score_entry_price_match(lifecycle, entry_price_actual)
    if entry_score:
        score += entry_score
        reasons.append("入场价匹配")
    elif entry_price_actual is not None:
        return 0, []

    if stop_loss is not None and lifecycle.stop_loss is not None:
        if _prices_close(stop_loss, lifecycle.stop_loss, tolerance_pct=0.006):
            score += 25
            reasons.append("止损匹配")
        else:
            score -= 20

    if take_profit is not None and _take_profit_matches(take_profit, lifecycle.take_profit):
        score += 15
        reasons.append("止盈匹配")

    if lifecycle.lifecycle_status == "entered":
        score += 10
        reasons.append("策略已入场")

    return max(score, 0), reasons


def _score_entry_price_match(
    lifecycle: StrategyLifecycle,
    entry_price_actual: float | None,
) -> int:
    if entry_price_actual is None or entry_price_actual <= 0:
        return 0
    if lifecycle.entry_price_actual is not None:
        if _prices_close(entry_price_actual, lifecycle.entry_price_actual, tolerance_pct=0.006):
            return 65
        if _prices_close(entry_price_actual, lifecycle.entry_price_actual, tolerance_pct=0.015):
            return 35
        return 0

    low = lifecycle.entry_range_low
    high = lifecycle.entry_range_high
    if low is None and high is None:
        return 0
    if low is None:
        low = high
    if high is None:
        high = low
    assert low is not None and high is not None
    lower, upper = sorted((float(low), float(high)))
    padding = max(abs(entry_price_actual) * 0.004, 1.0)
    if lower - padding <= entry_price_actual <= upper + padding:
        return 65
    nearest = lower if entry_price_actual < lower else upper
    if _prices_close(entry_price_actual, nearest, tolerance_pct=0.012):
        return 35
    return 0


def _prices_close(actual: float, expected: float, *, tolerance_pct: float) -> bool:
    baseline = max(abs(expected), 1.0)
    return abs(actual - expected) / baseline <= tolerance_pct


def _take_profit_matches(actual: float, take_profit_text: str | None) -> bool:
    for value in _extract_price_numbers(take_profit_text):
        if _prices_close(actual, value, tolerance_pct=0.008):
            return True
    return False


def _extract_price_numbers(value: str | None) -> list[float]:
    if not value:
        return []
    numbers: list[float] = []
    for match in re.findall(r"\d+(?:\.\d+)?", str(value)):
        try:
            numbers.append(float(match))
        except ValueError:
            continue
    return numbers


def _format_range(low: float | None, high: float | None) -> str | None:
    if low is None and high is None:
        return None
    if high is None or low == high:
        return str(low)
    if low is None:
        return str(high)
    return f"{low:g}-{high:g}"


def _deepcoin_position_has_size(position: dict[str, Any]) -> bool:
    try:
        return abs(float(position.get("pos") or position.get("size") or 0)) > 0
    except (TypeError, ValueError):
        return False


def _first_position_string(position: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = position.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _split_binding_ids(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _normalize_deepcoin_position_side(value: Any) -> str:
    side = str(value or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _symbol_from_deepcoin_inst_id(value: Any) -> str:
    text = str(value or "").upper()
    if text.endswith("-USDT-SWAP"):
        return text[: -len("-USDT-SWAP")]
    return text.split("-")[0] if text else "?"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_text_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _position_size_label(position: dict[str, Any]) -> str | None:
    size = _position_text_value(position.get("pos") or position.get("size"))
    inst_id = str(position.get("instId") or "")
    if not size:
        return None
    return f"{size} contracts {inst_id}".strip()


def _extract_session_lock_owner_pid(reason: str | None) -> int | None:
    if not reason or "already in use" not in reason:
        return None
    match = SESSION_LOCK_OWNER_PID_PATTERN.search(reason)
    if match is None:
        return None
    return int(match.group(1))


def _group_ai_strategy_enabled(group_config: GroupConfig, chat_title: str) -> bool:
    return any(
        group.enabled and group.ai_strategy_enabled and group.chat_title == chat_title
        for group in group_config.groups
    )


def _run_auto_trade_executor(app: FastAPI, *, raw_message_id: int) -> dict[str, Any]:
    try:
        deepcoin_client = app.state.deepcoin_client_factory()
        return auto_process_message_trade_signal(
            app.state.session_factory,
            raw_message_id=raw_message_id,
            group_config=app.state.group_config,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            processed_at=app.state.now_provider(),
        )
    except Exception:
        logger.exception("automatic Deepcoin trade execution failed")
        return {"status": "failed", "reason": "auto_trade_executor_error"}


def create_web_app(
    database_path: str | Path,
    media_root: str | Path | None = None,
    live_target_titles: set[str] | None = None,
    live_listener_runner=None,
    telegram_client: Any | None = None,
    live_listener_status_reason: str | None = None,
    group_labels_by_title: dict[str, str] | None = None,
    now_provider=None,
    reconcile_runner=None,
    reconcile_interval_seconds: int = 300,
    reconcile_startup_delay_seconds: int | None = None,
    group_config: GroupConfig | None = None,
    group_config_path: str | Path | None = None,
    recovery_runner=None,
    recovery_market_data_factory=None,
    deepcoin_contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    deepcoin_client_factory=None,
    message_recognizer=None,
    ai_recognition_config_path: str | Path | None = None,
) -> FastAPI:
    """Create the minimal FastAPI app used by the web command."""

    resolved_database_path = Path(database_path)
    resolved_media_root = Path(media_root) if media_root is not None else resolved_database_path.parent / "media"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ── lifecycle monitor (started first; no dependency on Telegram client) ──
        app.state.lifecycle_monitor_http = httpx.AsyncClient(timeout=10.0)
        app.state.lifecycle_monitor = LifecycleMonitor(
            session_factory=app.state.session_factory,
            broker=app.state.live_update_broker,
            config=LifecycleMonitorConfig(),
            http_client=app.state.lifecycle_monitor_http,
            now_provider=app.state.now_provider,
        )
        app.state.lifecycle_monitor_task = asyncio.create_task(
            app.state.lifecycle_monitor.run_loop()
        )
        if isinstance(app.state.strategy_alert_config, StrategyAlertConfig):
            app.state.telegram_bot_command_task = asyncio.create_task(
                run_telegram_bot_command_loop(
                    config=app.state.strategy_alert_config,
                    session_factory=app.state.session_factory,
                    group_config=app.state.group_config,
                )
            )
        if (
            app.state.live_target_titles
            and app.state.telegram_client is not None
            and app.state.live_listener_task is None
        ):
            app.state.live_listener_task = launch_live_listener_task(
                runner=app.state.live_listener_runner,
                client=app.state.telegram_client,
                session_factory=app.state.session_factory,
                broker=app.state.live_update_broker,
                target_titles=app.state.live_target_titles,
                media_root=app.state.media_root,
                strategy_alert_config=app.state.strategy_alert_config,
                strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                ai_recognition_config_path=app.state.ai_recognition_config_path,
                lifecycle_monitor=app.state.lifecycle_monitor,
                auto_trade_executor=app.state.auto_trade_executor,
            )
            app.state.reconcile_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.reconcile_runner,
                    client=app.state.telegram_client,
                    session_factory=app.state.session_factory,
                    broker=app.state.live_update_broker,
                    target_titles=app.state.live_target_titles,
                    media_root=app.state.media_root,
                    interval_seconds=app.state.reconcile_interval_seconds,
                    operation_lock=app.state.telegram_operation_lock,
                    strategy_alert_config=app.state.strategy_alert_config,
                    strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                    startup_delay_seconds=app.state.reconcile_startup_delay_seconds,
                )
            )
        try:
            yield
        finally:
            # ── lifecycle monitor shutdown ──
            lcm_task = app.state.lifecycle_monitor_task
            if lcm_task is not None:
                lcm_task.cancel()
                try:
                    await lcm_task
                except asyncio.CancelledError:
                    pass
                app.state.lifecycle_monitor_task = None
            lcm_http = app.state.lifecycle_monitor_http
            if lcm_http is not None:
                await lcm_http.aclose()
            # ── live listener shutdown ──
            task = app.state.live_listener_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                app.state.live_listener_task = None
            bot_command_task = app.state.telegram_bot_command_task
            if bot_command_task is not None:
                bot_command_task.cancel()
                try:
                    await bot_command_task
                except asyncio.CancelledError:
                    pass
                app.state.telegram_bot_command_task = None
            reconcile_task = app.state.reconcile_task
            if reconcile_task is not None:
                reconcile_task.cancel()
                try:
                    await reconcile_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.reconcile_task = None
            app.state.live_update_broker.close()

    app = FastAPI(title="Telegram KOL Research Web", lifespan=lifespan)
    app.state.database_path = Path(database_path)
    app.state.session_factory = create_session_factory(database_path)
    app.state.media_root = resolved_media_root.resolve()
    app.state.live_update_broker = LiveUpdateBroker()
    app.state.llm_proxy_config = load_llm_proxy_config()
    loaded_strategy_alert_config = load_strategy_alert_config()
    app.state.strategy_alert_config = (
        loaded_strategy_alert_config
        if strategy_alerts_enabled(loaded_strategy_alert_config)
        else None
    )
    app.state.chat_requester = request_grounded_chat_answer
    app.state.live_target_titles = live_target_titles or set()
    app.state.live_listener_runner = live_listener_runner or run_live_listener
    app.state.live_listener_task = None
    app.state.telegram_client = telegram_client
    app.state.live_listener_status_reason = live_listener_status_reason
    app.state.group_labels_by_title = group_labels_by_title or {}
    app.state.group_config = group_config or GroupConfig()
    app.state.group_config_path = Path(group_config_path) if group_config_path else None
    app.state.strategy_alert_enabled_for_title = lambda title: _group_ai_strategy_enabled(
        app.state.group_config,
        title,
    )
    app.state.now_provider = now_provider or (lambda: datetime.now(UTC))
    app.state.reconcile_runner = reconcile_runner or run_periodic_reconcile
    app.state.recovery_runner = recovery_runner or run_recovery_dry_run
    app.state.recovery_market_data_factory = (
        recovery_market_data_factory or GateMarketDataProvider
    )
    app.state.deepcoin_contract_spec_provider = deepcoin_contract_spec_provider
    app.state.deepcoin_client_factory = (
        deepcoin_client_factory or build_deepcoin_client_from_env
    )
    app.state.auto_trade_executor = lambda raw_message_id: _run_auto_trade_executor(
        app,
        raw_message_id=raw_message_id,
    )
    app.state.message_recognizer = message_recognizer or recognize_message_now
    app.state.ai_recognition_config_path = (
        Path(ai_recognition_config_path)
        if ai_recognition_config_path is not None
        else Path("config/ai_recognition.yaml")
    )
    app.state.reconcile_interval_seconds = reconcile_interval_seconds
    app.state.reconcile_startup_delay_seconds = (
        15
        if reconcile_startup_delay_seconds is None
        else reconcile_startup_delay_seconds
    )
    app.state.reconcile_task = None
    app.state.telegram_bot_command_task = None
    app.state.telegram_auth_loader = load_telegram_auth_config
    app.state.telegram_client_factory = create_telegram_client
    app.state.reconcile_once_runner = run_reconcile_once
    app.state.telegram_session_lock_factory = acquire_telegram_session_lock
    app.state.telegram_operation_lock = asyncio.Lock()
    app.state.asset_version = _static_asset_version()

    async def ensure_live_tasks_match_targets() -> None:
        if not app.state.live_target_titles:
            for task_name in ("live_listener_task", "reconcile_task"):
                task = getattr(app.state, task_name)
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    setattr(app.state, task_name, None)
            return
        if app.state.telegram_client is None:
            return
        live_task = app.state.live_listener_task
        if live_task is None or live_task.done():
            app.state.live_listener_task = launch_live_listener_task(
                runner=app.state.live_listener_runner,
                client=app.state.telegram_client,
                session_factory=app.state.session_factory,
                broker=app.state.live_update_broker,
                target_titles=app.state.live_target_titles,
                media_root=app.state.media_root,
                strategy_alert_config=app.state.strategy_alert_config,
                strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                ai_recognition_config_path=app.state.ai_recognition_config_path,
                lifecycle_monitor=app.state.lifecycle_monitor,
                auto_trade_executor=app.state.auto_trade_executor,
            )
        reconcile_task = app.state.reconcile_task
        if reconcile_task is None or reconcile_task.done():
            app.state.reconcile_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.reconcile_runner,
                    client=app.state.telegram_client,
                    session_factory=app.state.session_factory,
                    broker=app.state.live_update_broker,
                    target_titles=app.state.live_target_titles,
                    media_root=app.state.media_root,
                    interval_seconds=app.state.reconcile_interval_seconds,
                    operation_lock=app.state.telegram_operation_lock,
                    strategy_alert_config=app.state.strategy_alert_config,
                    strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                    startup_delay_seconds=0,
                )
            )

    def build_monitor_status() -> dict[str, Any]:
        synced_group_count = len(app.state.live_target_titles)
        task = app.state.live_listener_task
        reconcile_task = app.state.reconcile_task
        if synced_group_count == 0:
            return {
                "state": "idle",
                "label": "未配置同步群",
                "detail": "当前没有启用的 Telegram 群组可同步",
                "monitored_group_count": synced_group_count,
            }
        if app.state.telegram_client is None:
            return {
                "state": "disconnected",
                "label": "已断开",
                "detail": app.state.live_listener_status_reason or "Telegram 连接未建立",
                "monitored_group_count": synced_group_count,
            }
        if task is None:
            return {
                "state": "disconnected",
                "label": "已断开",
                "detail": "Telegram 同步监听任务未启动",
                "monitored_group_count": synced_group_count,
            }
        if task.done():
            detail = _task_failure_detail(task, default="Telegram 同步监听任务已停止")
            return {
                "state": "disconnected",
                "label": "已断开",
                "detail": detail,
                "monitored_group_count": synced_group_count,
            }
        if reconcile_task is not None and reconcile_task.done():
            detail = _task_failure_detail(
                reconcile_task,
                default="Telegram 周期同步任务已停止",
            )
            return {
                "state": "disconnected",
                "label": "已断开",
                "detail": detail,
                "monitored_group_count": synced_group_count,
            }
        return {
            "state": "monitoring",
            "label": "监控中",
            "detail": f"Telegram 正在同步监听 {synced_group_count} 个启用群组",
            "monitored_group_count": synced_group_count,
        }


    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.get("/")
    def index(request: Request):
        groups = load_group_rows(
            app.state.session_factory,
            group_labels_by_title=app.state.group_labels_by_title,
            configured_groups=app.state.group_config.groups,
        )
        selected_chat_id = groups[0]["chat_id"] if groups else None
        selected_group = next(
            (group for group in groups if group["chat_id"] == selected_chat_id),
            None,
        )
        messages = (
            load_group_messages(
                app.state.session_factory, chat_id=int(selected_chat_id), limit=50
            )
            if selected_chat_id is not None
            else []
        )
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        recovery_decisions = list_recovery_decisions(
            app.state.session_factory,
            limit=20,
        )
        active_positions = list_active_positions(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        symbol_whitelist_by_chat_id = _symbol_whitelist_by_chat_id(app.state.group_config)
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        # ── lifecycle data ──
        lifecycle_counts_by_chat_id = load_lifecycle_counts_by_chat_id(
            app.state.session_factory,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        lifecycle_counts = load_lifecycle_counts(
            app.state.session_factory,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        exited_positions = list_exited_strategies(
            app.state.session_factory,
            chat_id=selected_chat_id,
            limit=50,
        )
        monitor_status = build_monitor_status()
        live_listener_enabled = monitor_status["state"] == "monitoring"
        refresh_mode_label = (
            "实时监听 + SSE"
            if live_listener_enabled
            else "仅本地快照"
        )
        trader_dashboard = _build_trader_dashboard_state(
            groups=groups,
            group_config=app.state.group_config,
            active_positions=active_positions,
            pending_entry_signals=pending_entry_signals,
            holding_positions=holding_positions,
            exited_positions=exited_positions,
            lifecycle_counts=lifecycle_counts,
            lifecycle_counts_by_chat_id=lifecycle_counts_by_chat_id,
            live_listener_enabled=live_listener_enabled,
            refresh_mode_label=refresh_mode_label,
        )
        strategy_kpi = _build_strategy_kpi_counts(
            selected_chat_id=selected_chat_id,
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=exited_positions,
        )
        ai_recognition_config = load_ai_recognition_config(
            app.state.ai_recognition_config_path
        )
        trading_settings = load_trading_settings(app.state.session_factory)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": groups,
                "messages": messages,
                "selected_chat_id": selected_chat_id,
                "selected_group": selected_group,
                "live_listener_enabled": live_listener_enabled,
                "monitor_status": monitor_status,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "session_lock_owner_pid": _extract_session_lock_owner_pid(
                    app.state.live_listener_status_reason
                ),
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "asset_version": app.state.asset_version,
                "active_positions": active_positions,
                "pending_entry_signals": pending_entry_signals,
                "holding_positions": holding_positions,
                "exited_positions": exited_positions,
                "refresh_mode_label": refresh_mode_label,
                "trader_dashboard": trader_dashboard,
                "strategy_kpi": strategy_kpi,
                "ai_recognition_config": ai_recognition_config,
                "ai_prompt_views": build_ai_prompt_views(ai_recognition_config),
                "trading_settings": trading_settings,
            },
        )

    @app.get("/execution")
    def execution_dashboard(request: Request, status: str = "live"):
        monitor_status = build_monitor_status()
        live_listener_enabled = monitor_status["state"] == "monitoring"
        selected_status = status if status in {"live", "holding", "pending", "exited"} else "live"
        lifecycle_status = "holding" if selected_status == "live" else selected_status
        overview = list_execution_strategy_overview(
            app.state.session_factory,
            status=lifecycle_status,
            limit=200,
            group_label_by_chat_id=_group_label_by_chat_id(app.state.group_config),
            symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(app.state.group_config),
        )
        live_positions = _load_deepcoin_live_position_rows(
            app.state.session_factory,
            deepcoin_client_factory=app.state.deepcoin_client_factory,
            group_label_by_chat_id=_group_label_by_chat_id(app.state.group_config),
        )
        overview["counts"]["live"] = len(live_positions)
        if selected_status == "live":
            overview["status"] = "live"
            overview["items"] = live_positions
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        return templates.TemplateResponse(
            request,
            "execution.html",
            {
                "asset_version": app.state.asset_version,
                "monitor_status": monitor_status,
                "live_listener_enabled": live_listener_enabled,
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "overview": overview,
                "selected_status": selected_status,
            },
        )

    @app.get("/groups")
    def groups_partial(request: Request, selected_chat_id: int | None = None):
        groups = load_group_rows(
            app.state.session_factory,
            group_labels_by_title=app.state.group_labels_by_title,
            configured_groups=app.state.group_config.groups,
        )
        monitor_status = build_monitor_status()
        symbol_whitelist_by_chat_id = _symbol_whitelist_by_chat_id(app.state.group_config)
        lifecycle_counts_by_chat_id = load_lifecycle_counts_by_chat_id(
            app.state.session_factory,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        trader_dashboard = _build_trader_dashboard_state(
            groups=groups,
            group_config=app.state.group_config,
            active_positions=[],
            pending_entry_signals=[],
            lifecycle_counts_by_chat_id=lifecycle_counts_by_chat_id,
            live_listener_enabled=monitor_status["state"] == "monitoring",
            refresh_mode_label=(
                "实时监听 + SSE"
                if monitor_status["state"] == "monitoring"
                else "仅本地快照"
            ),
        )
        return templates.TemplateResponse(
            request,
            "_kol_strategy_list.html",
            {
                "groups": trader_dashboard["group_rows"],
                "selected_chat_id": selected_chat_id,
            },
        )

    @app.get("/groups/{chat_id}/detail")
    def group_detail(request: Request, chat_id: int):
        """Return the right-panel message detail fragment for a group."""
        route_started_at = time.perf_counter()
        step_started_at = time.perf_counter()
        selected_group = _lookup_single_group(
            app.state.session_factory,
            chat_id=chat_id,
            group_labels_by_title=app.state.group_labels_by_title,
            configured_groups=app.state.group_config.groups,
        )
        lookup_group_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        messages = load_group_messages(
            app.state.session_factory, chat_id=chat_id, limit=50
        )
        messages_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        monitor_status = build_monitor_status()
        monitor_status_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        freshness_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        response = templates.TemplateResponse(
            request,
            "_strategy_detail.html",
            {
                "selected_group": selected_group,
                "selected_chat_id": chat_id,
                "messages": messages,
                "monitor_status": monitor_status,
                "live_listener_enabled": monitor_status["state"] == "monitoring",
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "refresh_mode_label": (
                    "实时监听 + SSE"
                    if monitor_status["state"] == "monitoring"
                    else "仅本地快照"
                ),
                "search_text": "",
                "sender_name": "",
            },
        )
        template_ms = _elapsed_ms(step_started_at)
        logger.info(
            "web_perf route=/groups/{chat_id}/detail chat_id=%s total_ms=%.1f "
            "lookup_group_ms=%.1f messages_ms=%.1f monitor_status_ms=%.1f "
            "freshness_ms=%.1f template_ms=%.1f message_count=%s",
            chat_id,
            _elapsed_ms(route_started_at),
            lookup_group_ms,
            messages_ms,
            monitor_status_ms,
            freshness_ms,
            template_ms,
            len(messages),
        )
        return response

    @app.get("/groups/{chat_id}/detail/tab/pending")
    def group_detail_tab_pending(request: Request, chat_id: int):
        """Return only the pending tab content."""
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=chat_id,
            limit=50,
            symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(app.state.group_config),
        )
        return templates.TemplateResponse(
            request,
            "_detail_pending.html",
            {
                "pending_entry_signals": pending_entry_signals,
                "selected_chat_id": chat_id,
            },
        )

    @app.get("/groups/{chat_id}/detail/tab/exited")
    def group_detail_tab_exited(request: Request, chat_id: int):
        """Return only the exited tab content."""
        exited_positions = list_exited_strategies(
            app.state.session_factory, chat_id=chat_id, limit=50
        )
        return templates.TemplateResponse(
            request,
            "_detail_exited.html",
            {
                "exited_positions": exited_positions,
                "selected_chat_id": chat_id,
            },
        )

    @app.get("/groups/{chat_id}/detail/tab/messages")
    def group_detail_tab_messages(request: Request, chat_id: int):
        """Return only the messages tab content."""
        messages = load_group_messages(
            app.state.session_factory, chat_id=chat_id, limit=50
        )
        monitor_status = build_monitor_status()
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        selected_group = _lookup_single_group(
            app.state.session_factory,
            chat_id=chat_id,
            group_labels_by_title=app.state.group_labels_by_title,
            configured_groups=app.state.group_config.groups,
        )
        return templates.TemplateResponse(
            request,
            "_messages.html",
            {
                "messages": messages,
                "selected_chat_id": chat_id,
                "selected_group": selected_group,
                "search_text": "",
                "sender_name": "",
                "before_message_id": None,
                "live_listener_enabled": monitor_status["state"] == "monitoring",
                "monitor_status": monitor_status,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "refresh_mode_label": (
                    "实时监听 + SSE"
                    if monitor_status["state"] == "monitoring"
                    else "仅本地快照"
                ),
            },
        )

    @app.get("/groups/{chat_id}/strategy-mid-panel")
    def group_strategy_mid_panel(request: Request, chat_id: int, filter: str = "holding"):
        """Return the middle strategy panel fragment for a group."""
        route_started_at = time.perf_counter()
        filter = filter if filter in {"holding", "pending", "exited"} else "holding"
        step_started_at = time.perf_counter()
        lifecycle_counts = load_lifecycle_counts(
            app.state.session_factory,
            chat_id=chat_id,
            symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(
                app.state.group_config
            ),
        )
        lifecycle_counts_ms = _elapsed_ms(step_started_at)

        holding_ms = 0.0
        pending_ms = 0.0
        exited_ms = 0.0
        step_started_at = time.perf_counter()
        holding_positions = (
            list_holding_strategies(app.state.session_factory, chat_id=chat_id, limit=50)
            if filter == "holding"
            else []
        )
        if filter == "holding":
            holding_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        pending_entry_signals = (
            list_pending_strategies(
                app.state.session_factory,
                chat_id=chat_id,
                limit=50,
                symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(app.state.group_config),
            )
            if filter == "pending"
            else []
        )
        if filter == "pending":
            pending_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        exited_positions = (
            list_exited_strategies(app.state.session_factory, chat_id=chat_id, limit=50)
            if filter == "exited"
            else []
        )
        if filter == "exited":
            exited_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        strategy_kpi = _build_strategy_kpi_counts(
            selected_chat_id=chat_id,
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=exited_positions,
            lifecycle_counts=lifecycle_counts,
        )
        items = {
            "holding": holding_positions,
            "pending": pending_entry_signals,
            "exited": exited_positions,
        }[filter]
        kpi_ms = _elapsed_ms(step_started_at)
        step_started_at = time.perf_counter()
        response = templates.TemplateResponse(
            request,
            "_strategy_mid_panel.html",
            {
                "filter": filter,
                "selected_chat_id": chat_id,
                "strategy_kpi": strategy_kpi,
                "holding_positions": holding_positions,
                "pending_entry_signals": pending_entry_signals,
                "exited_positions": exited_positions,
                "items": items,
            },
        )
        template_ms = _elapsed_ms(step_started_at)
        logger.info(
            "web_perf route=/groups/{chat_id}/strategy-mid-panel chat_id=%s "
            "filter=%s total_ms=%.1f lifecycle_counts_ms=%.1f holding_ms=%.1f "
            "pending_ms=%.1f exited_ms=%.1f kpi_ms=%.1f template_ms=%.1f "
            "holding_count=%s pending_count=%s exited_count=%s item_count=%s",
            chat_id,
            filter,
            _elapsed_ms(route_started_at),
            lifecycle_counts_ms,
            holding_ms,
            pending_ms,
            exited_ms,
            kpi_ms,
            template_ms,
            len(holding_positions),
            len(pending_entry_signals),
            len(exited_positions),
            len(items),
        )
        return response

    @app.post("/api/strategy-lifecycles/{lifecycle_id}/manual-close")
    async def manual_close_strategy_lifecycle(
        lifecycle_id: int,
        payload: dict[str, Any] | None = None,
    ):
        data = payload or {}
        exit_price = data.get("exit_price")
        parsed_exit_price = None
        if exit_price not in (None, ""):
            try:
                parsed_exit_price = float(exit_price)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid exit_price") from exc
        try:
            result = mark_strategy_lifecycle_manual_close(
                app.state.session_factory,
                lifecycle_id=lifecycle_id,
                exit_price=parsed_exit_price,
                note=str(data.get("note") or "").strip() or None,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    @app.post("/api/execution/sync-deepcoin")
    async def sync_deepcoin_execution_state():
        try:
            client = app.state.deepcoin_client_factory()
            reconcile_result = (
                reconcile_deepcoin_execution_bindings(
                    app.state.session_factory,
                    client=client,
                    recovered_at=app.state.now_provider(),
                )
                if hasattr(client, "list_open_orders")
                else None
            )
            result = sync_manual_closed_deepcoin_positions(
                app.state.session_factory,
                client=client,
                synced_at=app.state.now_provider(),
            )
            protection_result = (
                recover_missing_position_protections(
                    app.state.session_factory,
                    deepcoin_client=client,
                    recovered_at=app.state.now_provider(),
                )
                if hasattr(client, "set_position_sltp")
                and hasattr(client, "list_trigger_orders_pending")
                else None
            )
        except DeepcoinClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Deepcoin execution sync failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "checked": result.checked,
            "manually_closed": result.manually_closed,
            "skipped_without_pos_id": result.skipped_without_pos_id,
            "reconciled_active": reconcile_result.active if reconcile_result else 0,
            "reconciled_open": reconcile_result.open if reconcile_result else 0,
            "reconciled_stale": reconcile_result.stale if reconcile_result else 0,
            "protection_checked": protection_result.checked if protection_result else 0,
            "protection_recovered": protection_result.protected if protection_result else 0,
            "protection_skipped_existing": protection_result.skipped_existing if protection_result else 0,
            "protection_skipped_missing_prices": (
                protection_result.skipped_missing_prices if protection_result else 0
            ),
        }

    @app.post("/api/execution/bind-live-position")
    async def bind_live_position(payload: dict[str, Any]):
        pos_id = str(payload.get("pos_id") or "").strip()
        lifecycle_id = payload.get("lifecycle_id")
        if not pos_id:
            raise HTTPException(status_code=400, detail="pos_id is required")
        try:
            lifecycle_id_int = int(lifecycle_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid lifecycle_id") from exc
        try:
            client = app.state.deepcoin_client_factory()
            positions = client.list_positions()
        except DeepcoinClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        active_position = next(
            (
                position
                for position in positions
                if _first_position_string(position, "posId", "pos_id", "id") == pos_id
                and _deepcoin_position_has_size(position)
            ),
            None,
        )
        if active_position is None:
            raise HTTPException(status_code=404, detail="live position not found")
        with app.state.session_factory() as session:
            existing_binding = (
                session.query(ExecutionBinding)
                .filter(ExecutionBinding.venue == "deepcoin")
                .filter(ExecutionBinding.status.in_(["open", "active"]))
                .all()
            )
            if any(pos_id in _split_binding_ids(row.pos_id) for row in existing_binding):
                raise HTTPException(status_code=409, detail="live position is already bound")

            protection_key = _deepcoin_position_protection_key(active_position)
            tpsl_order = _load_deepcoin_tpsl_orders_by_position_key(
                client,
                [active_position],
            ).get(protection_key)
            position_symbol = _symbol_from_deepcoin_inst_id(active_position.get("instId"))
            position_side = _normalize_deepcoin_position_side(
                active_position.get("posSide") or active_position.get("side")
            )
            candidates = _load_live_position_attribution_candidates(
                session,
                symbol=position_symbol,
                side=position_side,
                entry_price_actual=_float_or_none(active_position.get("avgPx")),
                stop_loss=_float_or_none(
                    _deepcoin_tpsl_price(tpsl_order, "sl") if tpsl_order else None
                ),
                take_profit=_float_or_none(
                    _deepcoin_tpsl_price(tpsl_order, "tp") if tpsl_order else None
                ),
                group_label_by_chat_id={},
                limit=24,
            )
            matched_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if int(candidate["lifecycle_id"]) == lifecycle_id_int
                ),
                None,
            )
            if matched_candidate is None:
                raise HTTPException(
                    status_code=409,
                    detail="live position does not match this KOL strategy",
                )
            if not matched_candidate.get("bindable"):
                raise HTTPException(
                    status_code=409,
                    detail="live position attribution is ambiguous; bind from a unique high-confidence candidate",
                )
        try:
            binding_id = bind_deepcoin_position_to_lifecycle(
                app.state.session_factory,
                lifecycle_id=lifecycle_id_int,
                pos_id=pos_id,
                position_payload=active_position,
                bound_at=app.state.now_provider(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"binding_id": binding_id, "lifecycle_id": lifecycle_id_int, "pos_id": pos_id}

    @app.post("/api/groups/{chat_id}/automation")
    async def update_group_automation(chat_id: int, payload: dict[str, Any]):
        config_path = app.state.group_config_path
        if config_path is None:
            raise HTTPException(
                status_code=503,
                detail="group config path is not configured",
            )
        app.state.group_config = update_group_automation_settings(
            config_path,
            chat_id=chat_id,
            chat_title=str(payload.get("chat_title") or chat_id),
            ai_strategy_enabled=payload.get("ai_strategy_enabled"),
            auto_trade_enabled=payload.get("auto_trade_enabled"),
        )
        group = next(
            item for item in app.state.group_config.groups if item.chat_id == chat_id
        )
        live_target_titles = {
            item.chat_title
            for item in app.state.group_config.groups
            if item.enabled
        }
        app.state.live_target_titles.clear()
        app.state.live_target_titles.update(live_target_titles)
        await ensure_live_tasks_match_targets()
        return {
            "chat_id": chat_id,
            "chat_title": group.chat_title,
            "ai_strategy_enabled": group.ai_strategy_enabled,
            "auto_trade_enabled": group.trading_mode == "auto_trade",
        }

    @app.get("/api/trading-settings")
    def get_trading_settings():
        return load_trading_settings(app.state.session_factory).to_dict()

    @app.post("/api/trading-settings")
    def update_trading_settings(payload: dict[str, Any]):
        return save_trading_settings(
            app.state.session_factory,
            payload,
            updated_at=app.state.now_provider(),
        ).to_dict()

    @app.post("/api/messages/{raw_message_id}/recognize")
    def recognize_message(raw_message_id: int):
        try:
            ai_config = load_ai_recognition_config(app.state.ai_recognition_config_path)
            result = app.state.message_recognizer(
                app.state.session_factory,
                raw_message_id=raw_message_id,
                ai_recognition_config=ai_config,
            )
            run_mimo_direct_for_message(
                app.state.session_factory,
                raw_message_id=raw_message_id,
                ai_recognition_config=ai_config,
                media_root=app.state.media_root,
            )
            auto_trade_result = app.state.auto_trade_executor(raw_message_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "raw_message_id": result.raw_message_id,
            "status": result.status,
            "summary": result.summary,
            "reason": result.reason,
            "auto_trade": auto_trade_result,
        }

    @app.post("/api/ai-recognition-config")
    def update_ai_recognition_config(payload: dict[str, Any]):
        existing_config = load_ai_recognition_config(app.state.ai_recognition_config_path)
        recognition_prompt = str(
            _ai_prompt_payload_value(payload, "recognition_prompt")
            or existing_config.recognition_prompt
        ).strip()
        if not recognition_prompt:
            raise HTTPException(status_code=422, detail="recognition_prompt is required")
        config = save_ai_recognition_config(
            app.state.ai_recognition_config_path,
            AiRecognitionConfig(
                recognition_prompt=recognition_prompt,
                lifecycle_event_prompt=str(
                    _ai_prompt_payload_value(payload, "lifecycle_event_prompt")
                    or existing_config.lifecycle_event_prompt
                ),
                mimo_direct_prompt=str(
                    _ai_prompt_payload_value(payload, "mimo_direct_prompt")
                    or existing_config.mimo_direct_prompt
                ),
                mode=str(payload.get("mode") or "ai_provider"),
                text_provider=_provider_config_from_payload(payload.get("text_provider")),
                image_provider=_provider_config_from_payload(payload.get("image_provider")),
                ai_models=_model_configs_from_payload(payload.get("ai_models")),
                active_text_model_id=str(payload.get("active_text_model_id") or ""),
                active_image_model_id=str(payload.get("active_image_model_id") or ""),
            ),
        )
        return {
            "mode": config.mode,
            "recognition_prompt": config.recognition_prompt,
            "lifecycle_event_prompt": config.lifecycle_event_prompt,
            "mimo_direct_prompt": config.mimo_direct_prompt,
            "prompts": build_ai_prompt_views(config),
            "active_text_model_id": config.active_text_model_id,
            "active_image_model_id": config.active_image_model_id,
            "ai_models": [_model_config_response(model) for model in config.ai_models],
            "text_provider": _provider_config_response(config.text_provider),
            "image_provider": _provider_config_response(config.image_provider),
        }

    @app.get("/groups/{chat_id}/messages")
    def group_messages(
        request: Request,
        chat_id: int,
        before_message_id: int | None = None,
        search_text: str | None = None,
        sender_name: str | None = None,
    ):
        monitor_status = build_monitor_status()
        messages = load_group_messages(
            app.state.session_factory,
            chat_id=chat_id,
            limit=50,
            before_message_id=before_message_id,
            search_text=search_text,
            sender_name=sender_name,
        )
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        selected_group = _lookup_single_group(
            app.state.session_factory,
            chat_id=chat_id,
            group_labels_by_title=app.state.group_labels_by_title,
            configured_groups=app.state.group_config.groups,
        )
        return templates.TemplateResponse(
            request,
            "_messages.html",
            {
                "messages": messages,
                "selected_chat_id": chat_id,
                "selected_group": selected_group,
                "search_text": search_text or "",
                "sender_name": sender_name or "",
                "before_message_id": before_message_id,
                "live_listener_enabled": monitor_status["state"] == "monitoring",
                "monitor_status": monitor_status,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "refresh_mode_label": (
                    "实时监听 + SSE"
                    if monitor_status["state"] == "monitoring"
                    else "仅本地快照"
                ),
            },
        )

    @app.get("/local-media/{requested_path:path}")
    def local_media(requested_path: str):
        # Strip any leading data/media/ prefix so we can safely join with media_root
        normalized = requested_path.replace("\\", "/")
        media_prefix = "data/media/"
        while normalized.startswith(media_prefix):
            normalized = normalized[len(media_prefix):]
        # Also handle legacy double-prefix like media/media/ inside the path
        media_root_name = app.state.media_root.name
        double_prefix = f"{media_root_name}/{media_root_name}/"
        while double_prefix in normalized:
            normalized = normalized.replace(double_prefix, f"{media_root_name}/", 1)
        candidate = (app.state.media_root / normalized).resolve()
        try:
            candidate.relative_to(app.state.media_root)
        except ValueError as exc:
            raise RuntimeError("Invalid media path") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="media file not found")
        return FileResponse(candidate)

    @app.get("/api/freshness")
    def api_freshness(chat_id: int | None = None):
        with app.state.session_factory() as session:
            global_latest = (
                session.query(
                    func.max(RawMessage.id).label("raw_message_id"),
                    func.max(RawMessage.created_at).label("created_at"),
                    func.max(RawMessage.posted_at).label("posted_at"),
                )
                .one()
            )
            selected_latest = None
            selected_count = 0
            if chat_id is not None:
                selected_latest = (
                    session.query(
                        func.max(RawMessage.id).label("raw_message_id"),
                        func.max(RawMessage.message_id).label("message_id"),
                        func.max(RawMessage.created_at).label("created_at"),
                        func.max(RawMessage.posted_at).label("posted_at"),
                    )
                    .filter(RawMessage.chat_id == chat_id)
                    .one()
                )
                selected_count = int(
                    session.query(func.count(RawMessage.id))
                    .filter(RawMessage.chat_id == chat_id)
                    .scalar()
                    or 0
                )

        return {
            "global": {
                "raw_message_id": global_latest.raw_message_id or 0,
                "created_at": _datetime_to_iso(global_latest.created_at),
                "posted_at": _datetime_to_iso(global_latest.posted_at),
            },
            "selected": {
                "chat_id": chat_id,
                "raw_message_id": (
                    selected_latest.raw_message_id if selected_latest is not None else 0
                )
                or 0,
                "message_id": (
                    selected_latest.message_id if selected_latest is not None else 0
                )
                or 0,
                "message_count": selected_count,
                "created_at": _datetime_to_iso(
                    selected_latest.created_at if selected_latest is not None else None
                ),
                "posted_at": _datetime_to_iso(
                    selected_latest.posted_at if selected_latest is not None else None
                ),
            },
        }

    @app.get("/api/monitor-status")
    async def api_monitor_status():
        status = build_monitor_status()
        if (
            status["state"] == "disconnected"
            and app.state.live_target_titles
            and not _is_telegram_auth_duplicated_error(status.get("detail"))
        ):
            await ensure_live_tasks_match_targets()
            status = build_monitor_status()
        return status

    @app.post("/api/chat")
    def chat(payload: dict[str, Any]):
        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=422, detail="question is required")

        chat_id_value = payload.get("chat_id")
        if chat_id_value is None:
            raise HTTPException(status_code=422, detail="chat_id is required")

        chat_id = int(chat_id_value)
        group_prompt = str(payload.get("group_prompt") or "").strip() or None
        message_limit = extract_recent_message_limit(question) or 50
        messages = load_group_messages(
            app.state.session_factory,
            chat_id=chat_id,
            limit=message_limit,
        )
        scope_context = build_scope_context(list(reversed(messages)))
        config = app.state.llm_proxy_config
        try:
            answer = app.state.chat_requester(
                config=config,
                question=question,
                scope_context=scope_context,
                group_prompt=group_prompt,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=_build_chat_proxy_error_detail(exc),
            ) from exc
        return {
            "answer": answer,
            "scope_mode": "current_group",
            "scope_message_count": len(messages),
            "proxy_payload": build_proxy_chat_payload(
                question=question,
                scope_context=scope_context,
                model=config.model,
                group_prompt=group_prompt,
            ),
            "sources": build_source_reference_map(messages),
        }

    @app.post("/api/refresh")
    async def refresh():
        shared_client = app.state.telegram_client is not None
        session_lock = None
        session_lock_entered = False
        try:
            if shared_client:
                telegram_client = app.state.telegram_client
            else:
                auth_config = app.state.telegram_auth_loader()
                session_lock = app.state.telegram_session_lock_factory(auth_config.session_path)
                session_lock.__enter__()
                session_lock_entered = True
                telegram_client = app.state.telegram_client_factory(auth_config)
        except TelegramSessionLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            if session_lock_entered and session_lock is not None:
                session_lock.__exit__(None, None, None)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def run_refresh():
            async with app.state.telegram_operation_lock:
                await maybe_await(getattr(telegram_client, "connect", lambda: None)())
                try:
                    return await asyncio.wait_for(
                        app.state.reconcile_once_runner(
                            client=telegram_client,
                            session_factory=app.state.session_factory,
                            broker=app.state.live_update_broker,
                            target_titles=set(app.state.live_target_titles),
                            media_root=app.state.media_root,
                            strategy_alert_config=app.state.strategy_alert_config,
                            strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                        ),
                        timeout=REFRESH_TIMEOUT_SECONDS,
                    )
                finally:
                    disconnect = getattr(telegram_client, "disconnect", None)
                    if callable(disconnect) and not shared_client:
                        await maybe_await(disconnect())

        try:
            return await run_refresh()
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Telegram refresh timed out after "
                    f"{REFRESH_TIMEOUT_SECONDS} seconds. Please try again."
                ),
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=_build_refresh_error_detail(exc)) from exc
        finally:
            if session_lock_entered and session_lock is not None:
                session_lock.__exit__(None, None, None)

    @app.post("/api/recovery-dry-run")
    def recovery_dry_run():
        market_data = app.state.recovery_market_data_factory()
        try:
            result = app.state.recovery_runner(
                session_factory=app.state.session_factory,
                group_config=app.state.group_config,
                now=app.state.now_provider(),
                market_data=market_data,
                account_state=None,
                lookback_hours=48,
                persist=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            close = getattr(market_data, "close", None)
            if callable(close):
                close()
        return {
            "total_candidates": result.total_candidates,
            "persisted_decisions": len(result.evaluations),
            "action_counts": result.action_counts,
        }

    @app.post("/api/recovery-decisions/review")
    def review_recovery_decision(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side", "review_status"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return apply_recovery_review_decision(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                review_status=str(payload["review_status"]),
                note=str(payload.get("note") or "").strip() or None,
                reviewed_at=app.state.now_provider(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/strategy-panels")
    def api_strategy_panels(chat_id: int):
        """Return strategy panel data for a specific group as JSON."""
        active_positions = list_active_positions(
            app.state.session_factory,
            chat_id=chat_id,
            limit=50,
        )
        pending_entry_signals = list_recovery_execution_previews(
            app.state.session_factory,
            limit=20,
            contract_spec_provider=app.state.deepcoin_contract_spec_provider,
        )
        dashboard = _build_trader_dashboard_state(
            groups=[],
            group_config=app.state.group_config,
            active_positions=active_positions,
            pending_entry_signals=pending_entry_signals,
            live_listener_enabled=True,
            refresh_mode_label="",
        )
        serialized_active = []
        for a in active_positions:
            item = dict(a)
            if item.get("posted_at"):
                item["posted_at"] = str(item["posted_at"])
            if item.get("opened_at"):
                item["opened_at"] = str(item["opened_at"])
            serialized_active.append(item)
        return {
            "entered_count": dashboard["entered_count"],
            "pending_count": dashboard["pending_count"],
            "active_positions": serialized_active,
            "pending_entry_signals": [dict(p) for p in pending_entry_signals],
        }

    @app.get("/api/recovery-execution-queue")
    def recovery_execution_queue():
        return {
            "items": list_recovery_execution_previews(
                app.state.session_factory,
                limit=100,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
        }

    @app.post("/api/recovery-order-confirm-dry-run")
    def recovery_order_confirm_dry_run(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return confirm_recovery_order_dry_run(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                persist_ready_confirmation=True,
                confirmed_at=app.state.now_provider(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/recovery-live-submit-gate")
    def recovery_live_submit_gate(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return validate_recovery_live_submit_gate(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/recovery-live-submit")
    def recovery_live_submit(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            deepcoin_client = app.state.deepcoin_client_factory()
            return submit_recovery_order_live(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                deepcoin_client=deepcoin_client,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                submitted_at=app.state.now_provider(),
            )
        except RecoveryLiveSubmitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeepcoinClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/trade-signals")
    def trade_signals(limit: int = 50):
        items = list_pending_trade_signals(
            app.state.session_factory,
            venue="deepcoin",
            limit=max(1, min(int(limit), 200)),
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "signal_uid": item.signal_uid,
                    "strategy_instance_id": item.strategy_instance_id,
                    "source_type": item.source_type,
                    "venue": item.venue,
                    "kol_id": item.kol_id,
                    "chat_id": item.chat_id,
                    "message_id": item.message_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "action": item.action,
                    "status": item.status,
                    "attempts": item.attempts,
                    "last_error": item.last_error,
                    "payload": item.payload,
                }
                for item in items
            ]
        }

    @app.post("/api/trade-signals/process-next")
    def trade_signals_process_next():
        try:
            deepcoin_client = app.state.deepcoin_client_factory()
            result = process_next_trade_signal_live(
                app.state.session_factory,
                deepcoin_client=deepcoin_client,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                processed_at=app.state.now_provider(),
            )
            return {"processed": result is not None, "result": result}
        except RecoveryLiveSubmitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeepcoinClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/events")
    def events():
        broker = app.state.live_update_broker
        return StreamingResponse(
            broker.stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return app


async def _run_reconcile_after_startup_delay(
    *,
    runner,
    startup_delay_seconds: int,
    **kwargs,
):
    if startup_delay_seconds > 0:
        await asyncio.sleep(startup_delay_seconds)
    await runner(**kwargs)


def _task_failure_detail(task: asyncio.Task, *, default: str) -> str:
    if task.cancelled():
        return default.replace("已停止", "已取消")
    try:
        exception = task.exception()
    except asyncio.CancelledError:
        return default.replace("已停止", "已取消")
    if exception is None:
        return default
    return str(exception)


def _is_telegram_auth_duplicated_error(message: Any) -> bool:
    lowered = str(message or "").lower()
    return "authorization key" in lowered and "used under two different" in lowered


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _static_asset_version() -> int:
    static_dir = Path(__file__).parent / "static"
    app_js = static_dir / "app.js"
    app_css = static_dir / "app.css"
    # Combine mtime + size so any file change produces a new version,
    # avoiding browser 304 cache hits that keep stale JS.
    return (
        hash(
            (
                app_js.stat().st_mtime,
                app_js.stat().st_size,
                app_css.stat().st_mtime,
                app_css.stat().st_size,
            )
        )
        & 0x7FFFFFFF
    )


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _provider_config_from_payload(payload: Any) -> AiProviderConfig:
    if not isinstance(payload, dict):
        return AiProviderConfig()
    return AiProviderConfig(
        base_url=str(payload.get("base_url") or ""),
        api_key=str(payload.get("api_key") or ""),
        model=str(payload.get("model") or ""),
        timeout_seconds=float(payload.get("timeout_seconds") or 60),
    )


def _ai_prompt_payload_value(payload: dict[str, Any], prompt_id: str) -> str | None:
    if prompt_id in payload:
        return str(payload.get(prompt_id) or "")
    prompts = payload.get("prompts")
    if isinstance(prompts, dict) and prompt_id in prompts:
        return str(prompts.get(prompt_id) or "")
    if isinstance(prompts, list):
        for item in prompts:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("field_name") or "")
            if item_id == prompt_id:
                return str(item.get("value") or "")
    return None


def _provider_config_response(config: AiProviderConfig) -> dict[str, Any]:
    return {
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
    }


def _model_configs_from_payload(payload: Any) -> list[AiModelConfig]:
    if not isinstance(payload, list):
        return []
    models: list[AiModelConfig] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        models.append(
            AiModelConfig(
                id=str(item.get("id") or item.get("model") or ""),
                label=str(item.get("label") or item.get("model") or item.get("id") or ""),
                base_url=str(item.get("base_url") or ""),
                api_key=str(item.get("api_key") or ""),
                model=str(item.get("model") or ""),
                timeout_seconds=float(item.get("timeout_seconds") or 60),
                supports_text=bool(item.get("supports_text", True)),
                supports_image=bool(item.get("supports_image", False)),
            )
        )
    return models


def _model_config_response(config: AiModelConfig) -> dict[str, Any]:
    return {
        "id": config.id,
        "label": config.label,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "supports_text": config.supports_text,
        "supports_image": config.supports_image,
    }


def _build_chat_proxy_error_detail(exc: httpx.HTTPError) -> str:
    message = _extract_proxy_error_message(exc)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return "AI 代理鉴权失败。请检查 TELEGRAM_KOL_LLM_API_KEY 是否已设置且有效。"
    lowered = message.lower()
    if "does not support image input" in lowered or (
        "image" in lowered and "not support" in lowered
    ):
        return "当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。"
    return "AI proxy request failed. Check CLIProxyAPI connectivity and credentials."


def _build_refresh_error_detail(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "authorization key" in lowered and "used under two different" in lowered:
        return (
            "Telegram 登录会话已失效：同一个 session 曾被多个客户端同时使用。"
            "请重新登录生成新的 Telegram session 后再刷新。"
        )
    return message


def _extract_proxy_error_message(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            payload = response.json()
        except ValueError:
            return response.text
        error_payload = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str):
                return message
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str):
            return detail
    return str(exc)


def _lookup_single_group(
    session_factory,
    *,
    chat_id: int,
    group_labels_by_title: dict[str, str] | None = None,
    configured_groups: list | None = None,
) -> dict | None:
    """Fast single-group lookup — avoids the N+1 scan of load_group_rows()."""
    from sqlalchemy import func
    from telegram_kol_research.models import RawMessage

    label_map = group_labels_by_title or {}
    configured_by_chat_id = {
        int(getattr(g, "chat_id")): g
        for g in (configured_groups or [])
        if getattr(g, "chat_id", None) is not None
    }
    with session_factory() as session:
        row = (
            session.query(
                func.max(RawMessage.posted_at).label("last_posted_at"),
            )
            .filter(RawMessage.chat_id == chat_id)
            .first()
        )
        last_posted_at = row.last_posted_at if row else None

        latest = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id == chat_id)
            .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
            .first()
        )
        raw_title = str(chat_id)
        if latest and latest.sender_name:
            raw_title = latest.sender_name
        cfg = configured_by_chat_id.get(chat_id)
        if cfg is not None:
            raw_title = str(getattr(cfg, "chat_title", raw_title))

    from telegram_kol_research.web_queries import utc_naive_to_local
    return {
        "chat_id": chat_id,
        "title": label_map.get(raw_title, raw_title),
        "raw_title": raw_title,
        "last_posted_at": utc_naive_to_local(last_posted_at) if last_posted_at else None,
        "message_count": 0,
        "has_media": False,
    }
