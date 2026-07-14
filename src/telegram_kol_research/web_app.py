"""FastAPI app for the Telegram web workbench."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio
import json
import logging
import re
import time

import httpx
from sqlalchemy import func

try:
    from fastapi import FastAPI, Request
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
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
from telegram_kol_research.authoritative_recognition import process_authoritative_message
from telegram_kol_research.app_logging import (
    configure_application_logging,
    read_log_page,
)
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
from telegram_kol_research.deepcoin_execution_actions import DeepcoinExecutionActionError
from telegram_kol_research.deepcoin_execution_actions import close_bound_position_market
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import update_group_automation_settings
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.message_recognition import recognize_message_now
from telegram_kol_research.models import (
    AiPromptTestRun,
    ExecutionBinding,
    ExecutionOrderLeg,
    RecognitionDecision,
)
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.prompt_composition import (
    render_registered_prompt,
    validate_prompt_content,
)
from telegram_kol_research.prompt_defaults import (
    GROUP_RESEARCH_PROMPT,
    MIMO_VISION_PROMPT,
    RESEARCH_CHAT_SYSTEM_PROMPT,
    SHARED_TRADING_PROMPT,
    seed_default_prompt_registry,
    seed_group_research_prompt,
)
from telegram_kol_research.prompt_registry import (
    PromptDetail,
    PromptInvocationRecord,
    PromptRegistryConflict,
    PromptRegistryError,
    PromptRegistryNotFound,
    get_prompt_detail,
    list_prompt_definitions,
    publish_prompt_draft,
    record_prompt_invocation,
    record_prompt_validation,
    rollback_prompt,
    save_prompt_draft,
)
from telegram_kol_research.prompt_testing import run_prompt_draft_test
from telegram_kol_research.recognition_profiles import list_recognition_profiles
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
from telegram_kol_research.position_attribution import PositionAttributionError
from telegram_kol_research.position_attribution import require_manual_position_attribution_allowed
from telegram_kol_research.protection_attribution import match_position_protection
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_runner import run_recovery_dry_run
from telegram_kol_research.semantic_disagreement_review import run_semantic_review_loop
from telegram_kol_research.strategy_alerts import (
    StrategyAlertConfig,
    load_strategy_alert_config,
    strategy_alerts_enabled,
)
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    deliver_pending_position_attribution_incidents,
    load_system_operator_bot_config,
    send_ai_recognition_conflict_review,
    send_pending_entry_expiry_review,
    send_semantic_disagreement_notification,
    system_operator_bot_enabled,
)
from telegram_kol_research.time_utils import DEFAULT_LOCAL_TIMEZONE
from telegram_kol_research.trading_settings import (
    load_trading_settings,
    save_trading_settings,
)
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.web_queries import (
    load_database_freshness,
    load_group_messages,
    load_group_rows,
    load_home_event_rows,
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
from telegram_kol_research.telegram_live_listener import (
    _build_authoritative_notification_payload,
    _filter_callable_kwargs,
    _schedule_authoritative_notification,
    launch_live_listener_task,
    run_live_listener,
)
from telegram_kol_research.telegram_live_listener import run_periodic_reconcile, run_reconcile_once
from telegram_kol_research.telegram_bot_commands import (
    run_system_operator_bot_command_loop,
    run_telegram_bot_command_loop,
)
from telegram_kol_research.telegram_client import create_telegram_client, load_telegram_auth_config, maybe_await
from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
)


REFRESH_TIMEOUT_SECONDS = 180
SESSION_LOCK_OWNER_PID_PATTERN = re.compile(r"owner pid=(\d+)")
logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 1)


def _log_background_task_result(task_name: str):
    def _callback(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.exception("Background task %s exited with error", task_name, exc_info=exc)

    return _callback


def _build_semantic_review_notifier(app: FastAPI):
    config = app.state.system_operator_bot_config
    if not isinstance(config, SystemOperatorBotConfig):
        return None

    async def notify(*, raw_message_id: int, payload: dict[str, Any]) -> None:
        await send_semantic_disagreement_notification(config=config, payload=payload)

    return notify


async def _supervise_semantic_review_runner(app: FastAPI) -> None:
    while True:
        try:
            await app.state.semantic_review_runner(
                session_factory=app.state.session_factory,
                config_path=app.state.ai_recognition_config_path,
                notifier=_build_semantic_review_notifier(app),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Semantic review runner exited with error; restarting")
        else:
            logger.error("Semantic review runner exited unexpectedly; restarting")
        await asyncio.sleep(app.state.semantic_review_restart_delay_seconds)


async def _stop_semantic_review_task(app: FastAPI) -> None:
    task = app.state.semantic_review_task
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    app.state.semantic_review_task = None


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
    should_use_lifecycle_holding_counts = holding_positions is None and not active_positions
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
        group_holding_count = (
            group_lifecycle_counts.get("entered", 0)
            if group_lifecycle_counts is not None and should_use_lifecycle_holding_counts
            else holding_counts_by_chat_id.get(group_chat_id, 0)
        )
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
    error_state: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    try:
        deepcoin_client = deepcoin_client_factory()
        positions = deepcoin_client.list_positions()
    except Exception as exc:
        logger.exception("Deepcoin live position load failed")
        if error_state is not None:
            error_state["error"] = str(exc)
        return []

    active_positions = [
        position for position in positions if _deepcoin_position_has_size(position)
    ]
    if not active_positions:
        return []
    tpsl_orders, tpsl_evidence_available = _load_deepcoin_pending_tpsl_orders(
        deepcoin_client,
        active_positions,
    )
    protection_match = match_position_protection(
        active_positions,
        tpsl_orders,
        evidence_available=tpsl_evidence_available,
    )

    with session_factory() as session:
        active_pos_ids = {
            pos_id
            for position in active_positions
            if (pos_id := _first_position_string(position, "posId", "pos_id", "id"))
        }
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.venue == "deepcoin")
            .filter(ExecutionOrderLeg.pos_id.in_(active_pos_ids))
            .all()
            if active_pos_ids
            else []
        )
        legs_by_pos_id = {str(leg.pos_id): leg for leg in legs if leg.pos_id}
        binding_ids = {int(leg.execution_binding_id) for leg in legs}
        bindings_by_id = {
            int(binding.id): binding
            for binding in (
                session.query(ExecutionBinding)
                .filter(ExecutionBinding.id.in_(binding_ids))
                .all()
                if binding_ids
                else []
            )
        }

        rows: list[dict[str, object]] = []
        for position in active_positions:
            pos_id = _first_position_string(position, "posId", "pos_id", "id")
            protection = protection_match.by_pos_id.get(pos_id or "")
            stop_loss_value = (
                protection.stop_loss
                if protection is not None and protection.status == "verified"
                else None
            )
            take_profit_values = (
                protection.take_profits
                if protection is not None and protection.status == "verified"
                else []
            )
            take_profit_value = take_profit_values[0] if take_profit_values else None
            has_protection = stop_loss_value is not None or take_profit_value is not None
            protection_status = protection.status if protection is not None else "absent"
            if (
                protection_status == "present_but_ambiguous"
                and protection is not None
                and protection.evidence.get("has_stop_loss")
            ):
                stop_loss_state_text = "止损存在，归属待确认"
            elif protection_status == "evidence_unavailable":
                stop_loss_state_text = "止损证据暂不可用"
            elif stop_loss_value is None:
                stop_loss_state_text = "无止损"
            else:
                stop_loss_state_text = None
            ownership_leg = legs_by_pos_id.get(pos_id or "")
            ownership_state = (
                str(ownership_leg.attribution_status or "unassigned")
                if ownership_leg is not None
                else "unassigned"
            )
            ownership_verified = ownership_state == "verified"
            binding = (
                bindings_by_id.get(int(ownership_leg.execution_binding_id))
                if ownership_leg is not None
                else None
            )
            binding_is_live = bool(
                ownership_verified
                and binding is not None
                and str(binding.status or "").lower() in {"open", "active"}
            )
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
                    "stop_loss_text": _position_text_value(stop_loss_value),
                    "stop_loss_state_text": stop_loss_state_text,
                    "take_profit_text": "/".join(
                        _position_text_value(value) or "" for value in take_profit_values
                    ) or None,
                    "protection_status": (
                        "protected"
                        if protection_status == "verified" and has_protection
                        else "unprotected" if protection_status == "absent" else protection_status
                    ),
                    "protection_mutation_allowed": bool(
                        protection is not None and protection.can_mutate
                    ),
                    "execution_status": (
                        binding.status
                        if binding_is_live
                        else (
                            "system_attribution_conflict"
                            if binding is not None
                            else "unbound_live_position"
                        )
                    ),
                    "exchange_status": (
                        binding.last_exchange_status
                        if binding is not None and not binding_is_live
                        else "position_active"
                    ),
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
                            stop_loss=_float_or_none(stop_loss_value),
                            take_profit=_float_or_none(take_profit_value),
                            group_label_by_chat_id=group_label_by_chat_id,
                        )
                        if binding is None
                        else []
                    ),
                    "persisted_attribution": _persisted_position_attribution(
                        leg=ownership_leg,
                        binding=binding,
                        group_label_by_chat_id=group_label_by_chat_id,
                    ),
                }
            )
        return rows


def _load_exchange_position_snapshot(
    session_factory,
    *,
    deepcoin_client_factory,
    group_label_by_chat_id: dict[int, str],
    pending_entry_signals: list[dict[str, Any]],
    trading_settings,
    order_limit: int = 100,
) -> dict[str, Any]:
    """Load the exchange-style dashboard snapshot from Deepcoin read APIs."""

    positions = _load_deepcoin_live_position_rows(
        session_factory,
        deepcoin_client_factory=deepcoin_client_factory,
        group_label_by_chat_id=group_label_by_chat_id,
    )
    snapshot: dict[str, Any] = {
        "positions": positions,
        "open_orders": [],
        "order_history": [],
        "position_history": [],
        "error": None,
    }
    try:
        client = deepcoin_client_factory()
    except Exception as exc:
        logger.exception("Deepcoin exchange snapshot client creation failed")
        snapshot["error"] = str(exc)
        return snapshot

    raw_open_orders = _safe_deepcoin_list(client, "list_open_orders")
    raw_order_history = _safe_deepcoin_list(client, "list_order_history")
    instruments = _exchange_snapshot_instrument_ids(
        positions=positions,
        open_orders=raw_open_orders,
        order_history=raw_order_history,
        pending_entry_signals=pending_entry_signals,
        allowed_symbols=getattr(trading_settings, "allowed_symbols", []),
    )
    raw_trigger_orders: list[dict[str, Any]] = []
    raw_trigger_history: list[dict[str, Any]] = []
    for inst_id in sorted(instruments):
        raw_trigger_orders.extend(
            _safe_deepcoin_list(client, "list_trigger_orders_pending", inst_id=inst_id)
        )
        raw_trigger_history.extend(
            _safe_deepcoin_list(client, "list_trigger_order_history", inst_id=inst_id)
        )

    snapshot["open_orders"] = _dedupe_exchange_rows(
        [
            *(_exchange_order_row(order, source="普通委托") for order in raw_open_orders),
            *(_exchange_order_row(order, source="触发委托") for order in raw_trigger_orders),
        ],
        limit=order_limit,
    )
    snapshot["order_history"] = _dedupe_exchange_rows(
        [
            *(_exchange_order_row(order, source="历史委托") for order in raw_order_history),
            *(
                _exchange_order_row(order, source="触发历史")
                for order in raw_trigger_history
            ),
        ],
        limit=order_limit,
    )
    _attach_exchange_order_bindings(
        session_factory,
        [*snapshot["open_orders"], *snapshot["order_history"]],
        group_label_by_chat_id=group_label_by_chat_id,
    )
    return snapshot


def _annotate_exchange_snapshot_attribution(
    snapshot: dict[str, Any],
    *,
    holding_positions: list[dict[str, Any]],
    pending_entry_signals: list[dict[str, Any]],
    exited_positions: list[dict[str, Any]],
    group_label_by_chat_id: dict[int, str],
) -> dict[str, Any]:
    for item in snapshot.get("positions", []):
        item["attribution"] = item.get("persisted_attribution") or {
            "state": "unassigned",
            "label": "归属待确认",
            "chat_id": None,
            "group_name": "未归属",
            "strategy_id": None,
            "strategy_summary": "自动管理已冻结",
            "source_excerpt": "",
            "score": 0,
            "reasons": [],
            "order_role": None,
        }
    for item in snapshot.get("open_orders", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=[*pending_entry_signals, *holding_positions],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=_infer_exchange_order_role(item),
        )
    for item in snapshot.get("order_history", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=[*exited_positions, *holding_positions, *pending_entry_signals],
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=_infer_exchange_order_role(item),
        )
    for item in snapshot.get("position_history", []):
        item["attribution"] = _exchange_item_attribution(
            item,
            candidates=exited_positions,
            group_label_by_chat_id=group_label_by_chat_id,
            default_order_role=None,
        )
    snapshot["grouped"] = {
        "positions": _group_exchange_items(snapshot.get("positions", [])),
        "open_orders": _group_exchange_items(snapshot.get("open_orders", [])),
        "order_history": _group_exchange_items(snapshot.get("order_history", [])),
        "position_history": _group_exchange_items(snapshot.get("position_history", [])),
    }
    return snapshot


def _persisted_position_attribution(
    *,
    leg: ExecutionOrderLeg | None,
    binding: ExecutionBinding | None,
    group_label_by_chat_id: dict[int, str],
) -> dict[str, Any] | None:
    if leg is None:
        return None
    try:
        evidence = json.loads(leg.attribution_evidence_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}
    state = str(leg.attribution_status or "unassigned")
    terminal_leg_states = {
        "cancelled",
        "canceled",
        "manually_cancelled",
        "exchange_cancelled",
        "rejected",
        "expired",
        "failed",
        "closed",
        "manually_closed",
    }
    verified = (
        state == "verified"
        and binding is not None
        and str(leg.status or "").lower() not in terminal_leg_states
    )
    chat_id = binding.chat_id if binding is not None else None
    group_name = (
        group_label_by_chat_id.get(int(chat_id))
        if chat_id is not None
        else None
    )
    if verified:
        label = "已验证归属"
        rendered_state = "bound"
        strategy_instance_id = str(
            leg.strategy_instance_id or binding.strategy_instance_id or ""
        )
        strategy_summary = " · ".join(
            item
            for item in (
                strategy_instance_id,
                f"{binding.symbol} {binding.side}",
            )
            if item
        )
        reasons = ["持久化 entry-leg 证据"]
    else:
        label = "归属待确认"
        rendered_state = "conflict"
        strategy_summary = "归属冲突 · 自动管理已冻结"
        reasons = [state]
    last_verified_at = leg.last_verified_at
    if last_verified_at is not None:
        if last_verified_at.tzinfo is None:
            last_verified_at = last_verified_at.replace(tzinfo=UTC)
        last_verified_display = last_verified_at.astimezone(
            DEFAULT_LOCAL_TIMEZONE
        ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        last_verified_display = None
    return {
        "state": rendered_state,
        "label": label,
        "chat_id": chat_id,
        "group_name": group_name or ("未确认群组" if not verified else str(chat_id)),
        "strategy_id": leg.strategy_instance_id
        or (binding.strategy_instance_id if binding is not None else None),
        "strategy_summary": strategy_summary,
        "source_excerpt": "",
        "score": 100 if verified else 0,
        "reasons": reasons,
        "order_role": f"entry leg #{leg.leg_index}",
        "evidence_type": evidence.get("evidence_type")
        or evidence.get("evidence_source"),
        "pos_id": leg.pos_id,
        "last_verified_at": last_verified_display,
        "ownership_state": state,
        "automatic_management_frozen": not verified,
    }


def _exchange_item_attribution(
    item: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    group_label_by_chat_id: dict[int, str],
    default_order_role: str | None,
) -> dict[str, Any]:
    bound = _bound_exchange_attribution(
        item,
        group_label_by_chat_id=group_label_by_chat_id,
        default_order_role=default_order_role,
    )
    if bound is not None:
        return bound
    candidate = _candidate_exchange_attribution(
        item,
        candidates=candidates,
        group_label_by_chat_id=group_label_by_chat_id,
        default_order_role=default_order_role,
    )
    if candidate is not None:
        return candidate
    return {
        "state": "unassigned",
        "label": "未归属",
        "chat_id": None,
        "group_name": "未归属",
        "strategy_id": None,
        "strategy_summary": "",
        "source_excerpt": "",
        "score": 0,
        "reasons": [],
        "order_role": default_order_role,
    }


def _bound_exchange_attribution(
    item: dict[str, Any],
    *,
    group_label_by_chat_id: dict[int, str],
    default_order_role: str | None,
) -> dict[str, Any] | None:
    chat_id = item.get("chat_id")
    if chat_id in (None, ""):
        return None
    try:
        chat_id_int = int(chat_id)
    except (TypeError, ValueError):
        chat_id_int = None
    group_name = (
        group_label_by_chat_id.get(chat_id_int)
        if chat_id_int is not None
        else None
    ) or str(item.get("group_label") or item.get("sender_name") or f"群组 {chat_id}")
    return {
        "state": "bound",
        "label": "已绑定",
        "chat_id": chat_id,
        "group_name": group_name,
        "strategy_id": item.get("lifecycle_id") or item.get("message_id"),
        "strategy_summary": _strategy_summary(item),
        "source_excerpt": _strategy_excerpt(item),
        "score": 100,
        "reasons": ["已有绑定"],
        "order_role": default_order_role,
    }


def _candidate_exchange_attribution(
    item: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    group_label_by_chat_id: dict[int, str],
    default_order_role: str | None,
) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for candidate in candidates:
        score, reasons = _score_exchange_candidate(item, candidate)
        if score > 0:
            scored.append((score, candidate, reasons))
    if not scored:
        return None
    scored.sort(key=lambda entry: entry[0], reverse=True)
    best_score, best_candidate, reasons = scored[0]
    is_tied = len(scored) > 1 and scored[1][0] == best_score
    if best_score < 75 or is_tied:
        return None
    chat_id = best_candidate.get("chat_id")
    try:
        chat_id_int = int(chat_id) if chat_id not in (None, "") else None
    except (TypeError, ValueError):
        chat_id_int = None
    group_name = (
        group_label_by_chat_id.get(chat_id_int)
        if chat_id_int is not None
        else None
    ) or str(best_candidate.get("sender_name") or f"群组 {chat_id}")
    return {
        "state": "candidate",
        "label": "可能归属",
        "chat_id": chat_id,
        "group_name": group_name,
        "strategy_id": best_candidate.get("lifecycle_id")
        or best_candidate.get("message_id"),
        "strategy_summary": _strategy_summary(best_candidate),
        "source_excerpt": _strategy_excerpt(best_candidate),
        "score": best_score,
        "reasons": reasons,
        "order_role": default_order_role,
    }


def _score_exchange_candidate(
    item: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[int, list[str]]:
    item_symbol = str(item.get("symbol") or _symbol_from_deepcoin_inst_id(item.get("inst_id")) or "").upper()
    candidate_symbol = str(candidate.get("symbol") or "").upper()
    item_side = str(item.get("side") or "")
    candidate_side = str(candidate.get("side") or "")
    score = 0
    reasons: list[str] = []
    if item_symbol and candidate_symbol and item_symbol == candidate_symbol:
        score += 45
        reasons.append("币种一致")
    else:
        return 0, []
    if item_side and candidate_side and item_side == candidate_side:
        score += 25
        reasons.append("方向一致")
    else:
        return 0, []
    item_price = _exchange_item_price(item)
    candidate_prices = _strategy_price_values(candidate)
    if item_price is not None and candidate_prices:
        closest = min(abs(item_price - price) for price in candidate_prices)
        denominator = max(abs(item_price), 1.0)
        if closest / denominator <= 0.002:
            score += 30
            reasons.append("价格接近")
        elif closest / denominator <= 0.01:
            score += 15
            reasons.append("价格相近")
    return score, reasons


def _strategy_summary(item: dict[str, Any]) -> str:
    symbol = str(item.get("symbol") or _symbol_from_deepcoin_inst_id(item.get("inst_id")) or "").upper()
    side = str(item.get("side") or "")
    parts = [part for part in [symbol, side] if part]
    entry = item.get("entry_range_text") or item.get("entry_text")
    if entry:
        parts.append(f"entry {entry}")
    stop_loss = item.get("stop_loss_text") or item.get("stop_loss")
    if stop_loss:
        parts.append(f"SL {stop_loss}")
    take_profit = item.get("take_profit_text") or item.get("take_profit")
    if take_profit:
        parts.append(f"TP {take_profit}")
    return " ".join(str(part) for part in parts)


def _strategy_excerpt(item: dict[str, Any]) -> str:
    text = str(item.get("original_text") or item.get("text") or "")
    return text[:90]


def _exchange_item_price(item: dict[str, Any]) -> float | None:
    for key in ("entry_price_actual", "price_text", "entry_text"):
        value = _float_or_none(item.get(key))
        if value is not None:
            return value
    return None


def _strategy_price_values(item: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for key in (
        "entry_price_actual",
        "entry_range_low",
        "entry_range_high",
        "stop_loss",
        "take_profit",
        "entry_text",
        "entry_range_text",
        "stop_loss_text",
        "take_profit_text",
    ):
        raw_value = item.get(key)
        if raw_value in (None, ""):
            continue
        if isinstance(raw_value, int | float):
            values.append(float(raw_value))
            continue
        values.extend(float(match) for match in re.findall(r"\d+(?:\.\d+)?", str(raw_value)))
    return values


def _group_exchange_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        attr = item.get("attribution") or {}
        key = str(attr.get("chat_id") or "unassigned")
        if key not in groups:
            group_name = str(attr.get("group_name") or "未归属")
            groups[key] = {
                "key": key,
                "group_name": group_name,
                "state": attr.get("state") or "unassigned",
                "items": [],
            }
            order.append(key)
        groups[key]["items"].append(item)
    return [groups[key] for key in order]


def _infer_exchange_order_role(item: dict[str, Any]) -> str | None:
    source = str(item.get("source") or "").lower()
    order_type = str(item.get("order_type") or "").lower()
    price = str(item.get("price_text") or "")
    if "tpsl" in order_type or "trigger" in source or "触发" in str(item.get("source") or ""):
        if price:
            return "触发委托"
        return "条件委托"
    if "market" in order_type:
        return "市价委托"
    if "limit" in order_type:
        return "限价委托"
    return None


def _safe_deepcoin_list(client, method_name: str, *, inst_id: str | None = None) -> list[dict[str, Any]]:
    method = getattr(client, method_name, None)
    if method is None:
        return []
    try:
        rows = method(inst_id=inst_id) if inst_id else method()
    except TypeError:
        try:
            rows = method()
        except Exception:
            logger.exception("Deepcoin %s load failed", method_name)
            return []
    except Exception:
        logger.exception("Deepcoin %s load failed", method_name)
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _exchange_snapshot_instrument_ids(
    *,
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    order_history: list[dict[str, Any]],
    pending_entry_signals: list[dict[str, Any]],
    allowed_symbols: list[str],
) -> set[str]:
    instruments: set[str] = set()
    for row in [*positions, *open_orders, *order_history]:
        inst_id = _exchange_inst_id(row)
        if inst_id:
            instruments.add(inst_id)
    for item in pending_entry_signals:
        symbol = str(item.get("symbol") or "").upper().strip()
        if symbol:
            instruments.add(_symbol_to_deepcoin_inst_id(symbol))
    for symbol in allowed_symbols or []:
        symbol_text = str(symbol or "").upper().strip()
        if symbol_text:
            instruments.add(_symbol_to_deepcoin_inst_id(symbol_text))
    return {inst_id for inst_id in instruments if inst_id}


def _exchange_order_row(order: dict[str, Any], *, source: str) -> dict[str, Any]:
    inst_id = _exchange_inst_id(order)
    order_type = _first_position_string(
        order,
        "ordType",
        "orderType",
        "triggerOrderType",
        "type",
    )
    position_side = _normalize_deepcoin_position_side(order.get("posSide") or order.get("side"))
    direction_label, direction_side = _exchange_order_direction(
        order_type=order_type,
        position_side=position_side,
        source=source,
    )
    return {
        "source": source,
        "inst_id": inst_id,
        "symbol": _symbol_from_deepcoin_inst_id(inst_id),
        "side": position_side,
        "order_direction_label": direction_label,
        "order_direction_side": direction_side,
        "order_id": _first_position_string(
            order,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        ),
        "client_order_id": _first_position_string(
            order,
            "clOrdId",
            "clientOrderId",
            "client_order_id",
        ),
        "order_type": order_type,
        "status": _first_position_string(order, "state", "status", "orderStatus"),
        "price_text": _position_text_value(
            _first_non_zero_exchange_price(
                order,
                "px",
                "price",
                "ordPx",
                "triggerPx",
                "triggerPrice",
                "slTriggerPrice",
                "tpTriggerPrice",
                "closeSLTriggerPrice",
                "closeTPTriggerPrice",
            )
        ),
        "size_text": _position_text_value(
            _first_exchange_value(order, "sz", "size", "qty", "quantity")
        ),
        "created_at": _format_deepcoin_timestamp(
            _first_position_string(order, "cTime", "createdAt", "createTime")
        ),
        "updated_at": _format_deepcoin_timestamp(
            _first_position_string(order, "uTime", "updatedAt", "updateTime")
        ),
    }


def _dedupe_exchange_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        identity = (
            str(row.get("source") or ""),
            str(row.get("order_id") or ""),
            str(row.get("client_order_id") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _attach_exchange_order_bindings(
    session_factory,
    rows: list[dict[str, Any]],
    *,
    group_label_by_chat_id: dict[int, str],
) -> None:
    wanted_ids = {
        str(value)
        for row in rows
        for value in (row.get("order_id"), row.get("client_order_id"))
        if value not in (None, "")
    }
    if not wanted_ids:
        return
    with session_factory() as session:
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .order_by(ExecutionBinding.updated_at.desc(), ExecutionBinding.id.desc())
            .all()
        )
    bindings_by_order_id: dict[str, ExecutionBinding] = {}
    for binding in bindings:
        binding_ids = [
            *_split_exchange_binding_ids(binding.order_id),
            *_split_exchange_binding_ids(binding.client_order_id),
        ]
        for binding_id in binding_ids:
            if binding_id in wanted_ids and binding_id not in bindings_by_order_id:
                bindings_by_order_id[binding_id] = binding
    for row in rows:
        binding = bindings_by_order_id.get(str(row.get("order_id") or ""))
        if binding is None:
            binding = bindings_by_order_id.get(str(row.get("client_order_id") or ""))
        if binding is None:
            continue
        row["chat_id"] = binding.chat_id
        row["message_id"] = binding.message_id
        row["group_label"] = group_label_by_chat_id.get(binding.chat_id, str(binding.chat_id))
        row["symbol"] = binding.symbol or row.get("symbol")
        row["side"] = binding.side or row.get("side")
        row["execution_status"] = binding.status


def _split_exchange_binding_ids(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _exchange_order_direction(
    *,
    order_type: str | None,
    position_side: str,
    source: str,
) -> tuple[str | None, str]:
    side_action = {
        "long": ("开多", "平多", "long", "short"),
        "short": ("开空", "平空", "short", "long"),
    }.get(position_side)
    if side_action is None:
        return None, position_side
    open_label, close_label, open_side, close_side = side_action
    normalized_type = str(order_type or "").lower()
    source_text = str(source or "")
    if normalized_type == "tpsl":
        return f"止盈止损/{close_label}", close_side
    if normalized_type == "conditional" or "触发" in source_text:
        return f"条件/{open_label}", open_side
    return open_label, open_side


def _format_deepcoin_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    numeric_value = _float_or_none(text)
    if numeric_value is None:
        return text
    timestamp_seconds = (
        numeric_value / 1000 if abs(numeric_value) >= 10_000_000_000 else numeric_value
    )
    try:
        local_time = datetime.fromtimestamp(timestamp_seconds, tz=UTC).astimezone(
            DEFAULT_LOCAL_TIMEZONE
        )
    except (OSError, OverflowError, ValueError):
        return text
    return local_time.strftime("%Y-%m-%d %H:%M:%S")


def _exchange_inst_id(row: dict[str, Any]) -> str:
    inst_id = str(row.get("instId") or row.get("instrument_id") or "").upper().strip()
    if inst_id:
        return _symbol_to_deepcoin_inst_id(inst_id)
    symbol = str(row.get("symbol") or "").upper().strip()
    return _symbol_to_deepcoin_inst_id(symbol) if symbol else ""


def _symbol_to_deepcoin_inst_id(symbol: str) -> str:
    text = str(symbol or "").upper().strip()
    if not text:
        return ""
    if text.endswith("-USDT-SWAP"):
        return text
    if text.endswith("USDT"):
        return f"{text[:-4]}-USDT-SWAP"
    return f"{text}-USDT-SWAP"


def _first_exchange_value(order: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_non_zero_exchange_price(order: dict[str, Any], *keys: str) -> Any:
    fallback = None
    for key in keys:
        value = order.get(key)
        if value in (None, ""):
            continue
        if fallback is None:
            fallback = value
        numeric_value = _float_or_none(value)
        if numeric_value is None or numeric_value != 0:
            return value
    return fallback


def _load_deepcoin_tpsl_orders_by_position_key(
    deepcoin_client,
    positions: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    orders, _evidence_available = _load_deepcoin_pending_tpsl_orders(
        deepcoin_client,
        positions,
    )
    orders_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for order in orders:
        for key in _deepcoin_tpsl_order_position_keys(order):
            current = orders_by_key.get(key)
            orders_by_key[key] = _merge_deepcoin_tpsl_orders(current, order)
    return orders_by_key


def _load_deepcoin_pending_tpsl_orders(
    deepcoin_client,
    positions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if not hasattr(deepcoin_client, "list_trigger_orders_pending"):
        return [], False
    result: list[dict[str, Any]] = []
    evidence_available = True
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
            evidence_available = False
            continue
        for order in orders:
            if str(order.get("triggerOrderType") or "").upper() != "TPSL":
                continue
            result.append(order)
    return result, evidence_available


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


def _deepcoin_position_tpsl_price(position: dict[str, Any], kind: str) -> Any:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if kind == "sl"
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = position.get(key)
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


def _load_existing_bound_lifecycle_attribution_candidate(
    session,
    *,
    lifecycle_id: int,
    pos_id: str,
    position_symbol: str,
    position_side: str,
    entry_price_actual: float | None,
    stop_loss: float | None,
    take_profit: float | None,
) -> dict[str, object] | None:
    lifecycle = session.get(StrategyLifecycle, lifecycle_id)
    if lifecycle is None or lifecycle.execution_binding_id is None:
        return None
    if lifecycle.symbol != position_symbol or lifecycle.side != position_side:
        return None
    binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
    if binding is None or binding.venue != "deepcoin":
        return None
    if binding.status not in {"open", "active"}:
        return None
    if pos_id in _split_binding_ids(binding.pos_id):
        return None
    score, reasons = _score_live_position_attribution(
        lifecycle,
        entry_price_actual=entry_price_actual,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    if score <= 0:
        return None
    return {
        "lifecycle_id": lifecycle.id,
        "match_score": score,
        "match_reasons": reasons,
        "bindable": score >= 70,
    }


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

    if (
        stop_loss is not None
        and lifecycle.stop_loss is not None
        and _lifecycle_stop_loss_is_plausible(lifecycle)
    ):
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
    scores: list[int] = []
    if lifecycle.entry_price_actual is not None:
        if _prices_close(entry_price_actual, lifecycle.entry_price_actual, tolerance_pct=0.006):
            scores.append(65)
        elif _prices_close(entry_price_actual, lifecycle.entry_price_actual, tolerance_pct=0.015):
            scores.append(35)
        else:
            scores.append(0)

    low = lifecycle.entry_range_low
    high = lifecycle.entry_range_high
    if low is None and high is None:
        return max(scores, default=0)
    if low is None:
        low = high
    if high is None:
        high = low
    assert low is not None and high is not None
    lower, upper = sorted((float(low), float(high)))
    padding = max(abs(entry_price_actual) * 0.004, 1.0)
    if lower - padding <= entry_price_actual <= upper + padding:
        scores.append(65)
        return max(scores)
    nearest = lower if entry_price_actual < lower else upper
    if _prices_close(entry_price_actual, nearest, tolerance_pct=0.012):
        scores.append(35)
    else:
        scores.append(0)
    return max(scores)


def _lifecycle_stop_loss_is_plausible(lifecycle: StrategyLifecycle) -> bool:
    stop_loss = _float_or_none(lifecycle.stop_loss)
    if stop_loss is None or stop_loss <= 0:
        return False
    references = [
        value
        for value in (
            _float_or_none(lifecycle.entry_price_actual),
            _float_or_none(lifecycle.entry_range_low),
            _float_or_none(lifecycle.entry_range_high),
        )
        if value is not None and value > 0
    ]
    if not references:
        return True
    reference = max(references)
    return reference * 0.2 <= stop_loss <= reference * 5


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


def _is_preferred_live_position_binding(candidate: ExecutionBinding, existing: ExecutionBinding) -> bool:
    live_statuses = {"open", "active"}
    candidate_is_live = str(candidate.status or "").lower() in live_statuses
    existing_is_live = str(existing.status or "").lower() in live_statuses
    if candidate_is_live != existing_is_live:
        return candidate_is_live
    candidate_updated = candidate.updated_at or candidate.created_at
    existing_updated = existing.updated_at or existing.created_at
    if candidate_updated is None:
        return False
    if existing_updated is None:
        return True
    return candidate_updated > existing_updated


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


def _to_deepcoin_swap_instrument(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if text.endswith("-USDT-SWAP"):
        return text
    return f"{text}-USDT-SWAP"


def _build_trading_symbol_rows(
    exchange_symbols: list[dict[str, Any]],
    *,
    selected_symbols: list[str],
    symbol_max_loss_usdt: dict[str, float],
) -> list[dict[str, Any]]:
    selected = {str(symbol).upper() for symbol in selected_symbols}
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for item in exchange_symbols:
        symbol = str(item.get("symbol") or "").strip().upper()
        instrument_id = str(item.get("instrument_id") or "").strip().upper()
        if not symbol:
            instrument_id = str(item.get("instId") or "").strip().upper()
            symbol = _symbol_from_deepcoin_inst_id(instrument_id)
        if not symbol or symbol == "?":
            continue
        rows_by_symbol[symbol] = {
            "symbol": symbol,
            "instrument_id": instrument_id or _to_deepcoin_swap_instrument(symbol),
            "selected": symbol in selected,
            "max_loss_usdt": symbol_max_loss_usdt.get(symbol),
        }
    for symbol in selected:
        rows_by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "instrument_id": _to_deepcoin_swap_instrument(symbol),
                "selected": True,
                "max_loss_usdt": symbol_max_loss_usdt.get(symbol),
            },
        )
    return sorted(rows_by_symbol.values(), key=lambda item: item["symbol"])


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


def _run_authoritative_processor(app: FastAPI, *, raw_message_id: int):
    ai_config = load_ai_recognition_config(app.state.ai_recognition_config_path)
    return process_authoritative_message(
        app.state.session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_config,
        media_root=app.state.media_root,
        auto_trade_executor=app.state.auto_trade_executor,
    )


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
    deepcoin_reconcile_runner=None,
    deepcoin_reconcile_interval_seconds: int = 30,
    deepcoin_reconcile_startup_delay_seconds: int = 5,
    message_recognizer=None,
    ai_recognition_config_path: str | Path | None = None,
    semantic_review_runner=None,
    semantic_review_restart_delay_seconds: float = 1.0,
) -> FastAPI:
    """Create the minimal FastAPI app used by the web command."""

    resolved_database_path = Path(database_path)
    log_directory = resolved_database_path.parent / "logs"
    configure_application_logging(log_directory)
    resolved_media_root = Path(media_root) if media_root is not None else resolved_database_path.parent / "media"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if app.state.semantic_review_task is None:
                app.state.semantic_review_task = asyncio.create_task(
                    _supervise_semantic_review_runner(app)
                )
                app.state.semantic_review_task.add_done_callback(
                    _log_background_task_result("semantic_review_task")
                )
            # ── lifecycle monitor (no dependency on Telegram client) ──
            app.state.lifecycle_monitor_http = httpx.AsyncClient(timeout=10.0)
            expiry_review_notifier = None
            if isinstance(app.state.system_operator_bot_config, SystemOperatorBotConfig):
                async def expiry_review_notifier(payload):
                    group_labels = _group_label_by_chat_id(app.state.group_config)
                    payload = dict(payload)
                    payload["chat_title"] = group_labels.get(int(payload.get("chat_id") or 0))
                    await send_pending_entry_expiry_review(
                        config=app.state.system_operator_bot_config,
                        payload=payload,
                    )
            app.state.lifecycle_monitor = LifecycleMonitor(
                session_factory=app.state.session_factory,
                broker=app.state.live_update_broker,
                config=LifecycleMonitorConfig(),
                http_client=app.state.lifecycle_monitor_http,
                now_provider=app.state.now_provider,
                expiry_review_notifier=expiry_review_notifier,
            )
            app.state.lifecycle_monitor_task = asyncio.create_task(
                app.state.lifecycle_monitor.run_loop()
            )
            app.state.deepcoin_reconcile_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.deepcoin_reconcile_runner,
                    startup_delay_seconds=app.state.deepcoin_reconcile_startup_delay_seconds,
                    session_factory=app.state.session_factory,
                    deepcoin_client_factory=app.state.deepcoin_client_factory,
                    interval_seconds=app.state.deepcoin_reconcile_interval_seconds,
                    now_provider=app.state.now_provider,
                    system_operator_bot_config=app.state.system_operator_bot_config,
                )
            )
            if isinstance(app.state.strategy_alert_config, StrategyAlertConfig):
                app.state.telegram_bot_command_task = asyncio.create_task(
                    run_telegram_bot_command_loop(
                        config=app.state.strategy_alert_config,
                        session_factory=app.state.session_factory,
                        group_config=app.state.group_config,
                    )
                )
            if isinstance(app.state.system_operator_bot_config, SystemOperatorBotConfig):
                app.state.system_operator_bot_command_task = asyncio.create_task(
                    run_system_operator_bot_command_loop(
                        config=app.state.system_operator_bot_config,
                        session_factory=app.state.session_factory,
                        deepcoin_client_factory=app.state.deepcoin_client_factory,
                    )
                )
                app.state.system_operator_bot_command_task.add_done_callback(
                    _log_background_task_result("system_operator_bot_command_task")
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
                    authoritative_processor=app.state.authoritative_processor,
                    system_operator_bot_config=app.state.system_operator_bot_config,
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
                        authoritative_processor=app.state.authoritative_processor,
                        system_operator_bot_config=app.state.system_operator_bot_config,
                        startup_delay_seconds=app.state.reconcile_startup_delay_seconds,
                    )
                )
            yield
        finally:
            await _stop_semantic_review_task(app)
            # ── lifecycle monitor shutdown ──
            lcm_task = getattr(app.state, "lifecycle_monitor_task", None)
            if lcm_task is not None:
                lcm_task.cancel()
                try:
                    await lcm_task
                except asyncio.CancelledError:
                    pass
                app.state.lifecycle_monitor_task = None
            lcm_http = getattr(app.state, "lifecycle_monitor_http", None)
            if lcm_http is not None:
                await lcm_http.aclose()
            # ── live listener shutdown ──
            deepcoin_reconcile_task = app.state.deepcoin_reconcile_task
            if deepcoin_reconcile_task is not None:
                deepcoin_reconcile_task.cancel()
                try:
                    await deepcoin_reconcile_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.deepcoin_reconcile_task = None
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
            system_bot_command_task = app.state.system_operator_bot_command_task
            if system_bot_command_task is not None:
                system_bot_command_task.cancel()
                try:
                    await system_bot_command_task
                except asyncio.CancelledError:
                    pass
                app.state.system_operator_bot_command_task = None
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
    app.state.log_directory = log_directory
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
    loaded_system_operator_bot_config = load_system_operator_bot_config()
    app.state.system_operator_bot_config = (
        loaded_system_operator_bot_config
        if system_operator_bot_enabled(loaded_system_operator_bot_config)
        else None
    )
    app.state.chat_requester = request_grounded_chat_answer
    app.state.prompt_test_runner = run_prompt_draft_test
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
    app.state.deepcoin_reconcile_runner = (
        deepcoin_reconcile_runner or run_deepcoin_execution_reconcile_loop
    )
    app.state.deepcoin_reconcile_interval_seconds = deepcoin_reconcile_interval_seconds
    app.state.deepcoin_reconcile_startup_delay_seconds = deepcoin_reconcile_startup_delay_seconds
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
    seed_default_prompt_registry(
        app.state.session_factory,
        load_ai_recognition_config(app.state.ai_recognition_config_path),
    )
    app.state.semantic_review_runner = semantic_review_runner or run_semantic_review_loop
    app.state.semantic_review_restart_delay_seconds = max(
        0.0,
        min(float(semantic_review_restart_delay_seconds), 60.0),
    )
    app.state.semantic_review_task = None
    app.state.authoritative_processor = lambda raw_message_id: _run_authoritative_processor(
        app,
        raw_message_id=raw_message_id,
    )
    app.state.reconcile_interval_seconds = reconcile_interval_seconds
    app.state.reconcile_startup_delay_seconds = (
        15
        if reconcile_startup_delay_seconds is None
        else reconcile_startup_delay_seconds
    )
    app.state.reconcile_task = None
    app.state.deepcoin_reconcile_task = None
    app.state.telegram_bot_command_task = None
    app.state.system_operator_bot_command_task = None
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
                authoritative_processor=app.state.authoritative_processor,
                system_operator_bot_config=app.state.system_operator_bot_config,
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
                    authoritative_processor=app.state.authoritative_processor,
                    system_operator_bot_config=app.state.system_operator_bot_config,
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

    @app.get("/logs")
    def logs_page(request: Request):
        return templates.TemplateResponse(
            request,
            "logs.html",
            {"asset_version": app.state.asset_version},
        )

    @app.get("/api/logs")
    def api_logs(offset: int = 0, limit: int = 100, level: str | None = None):
        if offset < 0 or not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="invalid log pagination")
        normalized_level = (level or "").upper() or None
        if normalized_level not in {None, "INFO", "WARNING", "ERROR"}:
            raise HTTPException(status_code=422, detail="invalid log level")
        return read_log_page(
            app.state.log_directory,
            offset=offset,
            limit=limit,
            level=normalized_level,
        )

    def build_home_dashboard_context() -> dict[str, Any]:
        symbol_whitelist_by_chat_id = _symbol_whitelist_by_chat_id(app.state.group_config)
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        group_label_by_chat_id = _group_label_by_chat_id(app.state.group_config)
        exchange_error: dict[str, str] = {}
        exchange_positions = _load_deepcoin_live_position_rows(
            app.state.session_factory,
            deepcoin_client_factory=app.state.deepcoin_client_factory,
            group_label_by_chat_id=group_label_by_chat_id,
            error_state=exchange_error,
        )
        exchange_snapshot = _annotate_exchange_snapshot_attribution(
            {
                "positions": exchange_positions,
                "open_orders": [],
                "order_history": [],
                "position_history": [],
                "error": None,
            },
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=[],
            group_label_by_chat_id=group_label_by_chat_id,
        )
        exchange_positions = exchange_snapshot.get("positions") or []
        missing_stop_positions = [
            row for row in holding_positions
            if not (row.get("stop_loss") or row.get("stop_loss_text"))
        ]
        unassigned_positions = [
            row for row in exchange_positions
            if (row.get("attribution") or {}).get("state") in {"unassigned", "candidate"}
        ]
        risk_rows: list[dict[str, Any]] = []
        for row in missing_stop_positions:
            risk_rows.append({
                "id": f"risk:missing-stop:{row.get('id') or row.get('message_id')}",
                "kind": "risk",
                "occurred_at": row.get("latest_event_at") or row.get("signal_at") or app.state.now_provider(),
                "source_label": row.get("sender_name") or str(row.get("chat_id") or "策略"),
                "title": "持仓缺少止损",
                "summary": f"{row.get('symbol') or '-'} {row.get('side') or ''}",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "status": "risk",
                "destination": {"view": "positions", "pos_id": row.get("pos_id")},
            })
        for row in unassigned_positions:
            risk_rows.append({
                "id": f"risk:unassigned:{row.get('pos_id') or row.get('symbol')}",
                "kind": "risk",
                "occurred_at": app.state.now_provider(),
                "source_label": "Deepcoin",
                "title": "交易所仓位归属异常",
                "summary": f"{row.get('symbol') or '-'} {row.get('side') or ''}",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "status": "risk",
                "destination": {"view": "positions", "pos_id": row.get("pos_id")},
            })
        home_events = sorted(
            [*risk_rows, *load_home_event_rows(app.state.session_factory, limit=50)],
            key=lambda row: (
                row["occurred_at"].astimezone(UTC).replace(tzinfo=None)
                if row["occurred_at"].tzinfo is not None
                else row["occurred_at"]
            ),
            reverse=True,
        )[:50]
        monitor_status = build_monitor_status()
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        return {
            "home_events": home_events,
            "home_summary": {
                "position_count": len(exchange_positions),
                "unrealized_pnl": sum(
                    float(row.get("unrealized_pnl") or row.get("upl") or 0)
                    for row in exchange_positions
                ),
                "risk_count": len(missing_stop_positions) + len(unassigned_positions),
                "pending_count": len(pending_entry_signals),
                "telegram_state": monitor_status["state"],
                "database_state": (
                    "stale"
                    if freshness["stale_hours"] is not None and freshness["stale_hours"] > 1
                    else "current"
                ),
                "exchange_state": "error" if exchange_error else "current",
            },
            "database_latest_message_at": freshness["latest_message_at"],
        }

    def build_positions_panel_context() -> dict[str, Any]:
        symbol_whitelist_by_chat_id = _symbol_whitelist_by_chat_id(app.state.group_config)
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        exited_positions = list_exited_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=50,
        )
        group_label_by_chat_id = _group_label_by_chat_id(app.state.group_config)
        exchange_snapshot = _load_exchange_position_snapshot(
            app.state.session_factory,
            deepcoin_client_factory=app.state.deepcoin_client_factory,
            group_label_by_chat_id=group_label_by_chat_id,
            pending_entry_signals=pending_entry_signals,
            trading_settings=load_trading_settings(app.state.session_factory),
        )
        exchange_snapshot["position_history"] = exited_positions
        exchange_snapshot = _annotate_exchange_snapshot_attribution(
            exchange_snapshot,
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=exited_positions,
            group_label_by_chat_id=group_label_by_chat_id,
        )
        return {
            "exchange_snapshot": exchange_snapshot,
            "holding_positions": holding_positions,
            "pending_entry_signals": pending_entry_signals,
            "exited_positions": exited_positions,
        }

    @app.get("/home-dashboard")
    def home_dashboard_partial(request: Request):
        return templates.TemplateResponse(
            request,
            "_home_dashboard.html",
            build_home_dashboard_context(),
        )

    @app.get("/positions-panel")
    def positions_panel_partial(request: Request):
        return templates.TemplateResponse(
            request,
            "_exchange_positions_panel.html",
            build_positions_panel_context(),
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
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
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
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
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
            active_positions=[],
            pending_entry_signals=pending_entry_signals,
            holding_positions=holding_positions,
            lifecycle_counts_by_chat_id=lifecycle_counts_by_chat_id,
            live_listener_enabled=live_listener_enabled,
            refresh_mode_label=refresh_mode_label,
        )
        trading_settings = load_trading_settings(app.state.session_factory)
        ai_recognition_config = load_ai_recognition_config(
            app.state.ai_recognition_config_path
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": groups,
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
                "refresh_mode_label": refresh_mode_label,
                "trader_dashboard": trader_dashboard,
                "ai_recognition_config": ai_recognition_config,
                "ai_prompt_views": build_ai_prompt_views(ai_recognition_config),
                "recognition_profiles": list_recognition_profiles(),
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
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=500,
        )
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=500,
            symbol_whitelist_by_chat_id=symbol_whitelist_by_chat_id,
        )
        trader_dashboard = _build_trader_dashboard_state(
            groups=groups,
            group_config=app.state.group_config,
            active_positions=[],
            pending_entry_signals=pending_entry_signals,
            holding_positions=holding_positions,
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
            if isinstance(app.state.system_operator_bot_config, SystemOperatorBotConfig):
                await deliver_pending_position_attribution_incidents(
                    app.state.session_factory,
                    config=app.state.system_operator_bot_config,
                    delivered_at=app.state.now_provider(),
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
        }

    @app.post("/api/execution/close-bound-position")
    async def close_bound_position(payload: dict[str, Any] | None = None):
        data = payload or {}
        if set(data) != {"pos_id"}:
            raise HTTPException(status_code=400, detail="only pos_id is accepted")
        pos_id = str(data.get("pos_id") or "").strip()
        if not pos_id:
            raise HTTPException(status_code=400, detail="pos_id is required")
        try:
            result = close_bound_position_market(
                app.state.session_factory,
                pos_id=pos_id,
                deepcoin_client=app.state.deepcoin_client_factory(),
                executed_at=app.state.now_provider(),
            )
        except DeepcoinExecutionActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeepcoinClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("bound Deepcoin position close failed")
            raise HTTPException(status_code=500, detail="bound position close failed") from exc
        return result

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
            try:
                require_manual_position_attribution_allowed(
                    session,
                    venue="deepcoin",
                    pos_id=pos_id,
                )
            except PositionAttributionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
                matched_candidate = _load_existing_bound_lifecycle_attribution_candidate(
                    session,
                    lifecycle_id=lifecycle_id_int,
                    pos_id=pos_id,
                    position_symbol=position_symbol,
                    position_side=position_side,
                    entry_price_actual=_float_or_none(active_position.get("avgPx")),
                    stop_loss=_float_or_none(
                        _deepcoin_tpsl_price(tpsl_order, "sl") if tpsl_order else None
                    ),
                    take_profit=_float_or_none(
                        _deepcoin_tpsl_price(tpsl_order, "tp") if tpsl_order else None
                    ),
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

    @app.get("/api/trading-settings/symbols")
    def list_trading_setting_symbols():
        settings = load_trading_settings(app.state.session_factory)
        saved_symbols = [str(symbol).upper() for symbol in settings.allowed_symbols]
        try:
            deepcoin_client = app.state.deepcoin_client_factory()
            if not hasattr(deepcoin_client, "list_swap_symbols"):
                raise DeepcoinClientError("Deepcoin client cannot list symbols")
            exchange_symbols = deepcoin_client.list_swap_symbols()
        except Exception:
            exchange_symbols = [
                {
                    "symbol": symbol,
                    "instrument_id": _to_deepcoin_swap_instrument(symbol),
                }
                for symbol in saved_symbols
            ]
        return {
            "symbols": _build_trading_symbol_rows(
                exchange_symbols,
                selected_symbols=saved_symbols,
                symbol_max_loss_usdt=settings.symbol_max_loss_usdt,
            )
        }

    @app.post("/api/trading-settings")
    def update_trading_settings(payload: dict[str, Any]):
        return save_trading_settings(
            app.state.session_factory,
            payload,
            updated_at=app.state.now_provider(),
        ).to_dict()

    @app.post("/api/messages/{raw_message_id}/recognize")
    async def recognize_message(raw_message_id: int):
        try:
            processing_result = await asyncio.to_thread(
                app.state.authoritative_processor,
                raw_message_id,
            )
            with app.state.session_factory() as session:
                raw_message = session.get(RawMessage, raw_message_id)
                if raw_message is None:
                    raise LookupError(f"Raw message {raw_message_id} not found")
                conflict_payload = None
                if processing_result.assessment.agreement_status == "authoritative_failed":
                    conflict_payload = _build_authoritative_notification_payload(
                        raw_message=raw_message,
                        chat_title=raw_message.sender_name,
                        processing_result=processing_result,
                    )
            if (
                conflict_payload is not None
                and system_operator_bot_enabled(app.state.system_operator_bot_config)
            ):
                _schedule_authoritative_notification(
                    session_factory=app.state.session_factory,
                    raw_message_id=raw_message_id,
                    sender=send_ai_recognition_conflict_review,
                    config=app.state.system_operator_bot_config,
                    payload=conflict_payload,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        result = processing_result.recognition
        semantic_review_status = getattr(
            processing_result.assessment,
            "semantic_review_status",
            None,
        )
        if semantic_review_status not in {
            "pending",
            "execution_pending",
            "execution_running",
        }:
            semantic_review_status = "pending"
        return {
            "raw_message_id": result.raw_message_id,
            "status": result.status,
            "summary": result.summary,
            "reason": result.reason,
            "auto_trade": processing_result.automation,
            "ai_conflict": False,
            "authoritative_model": processing_result.assessment.mimo.model,
            "agreement_status": "pending",
            "semantic_review_status": semantic_review_status,
            "differences": [],
            "notification_scheduled": (
                conflict_payload is not None
                and system_operator_bot_enabled(app.state.system_operator_bot_config)
            ),
        }

    @app.get("/api/ai-prompts")
    def list_ai_prompts(chat_id: int | None = None):
        details = list_prompt_definitions(
            app.state.session_factory,
            chat_id=chat_id,
        )
        return {"items": [_prompt_detail_response(item) for item in details]}

    @app.get("/api/ai-prompts/{prompt_key}")
    def get_ai_prompt(prompt_key: str, chat_id: int | None = None):
        try:
            detail = get_prompt_detail(
                app.state.session_factory,
                prompt_key,
                chat_id=chat_id,
            )
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _prompt_detail_response(detail)

    @app.get("/api/ai-prompts/{prompt_key}/history")
    def get_ai_prompt_history(prompt_key: str, chat_id: int | None = None):
        try:
            detail = get_prompt_detail(
                app.state.session_factory,
                prompt_key,
                chat_id=chat_id,
            )
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "prompt_key": detail.prompt_key,
            "chat_id": detail.scope_chat_id,
            "items": [_prompt_version_response(item) for item in detail.history],
        }

    @app.put("/api/ai-prompts/{prompt_key}/draft")
    def update_ai_prompt_draft(
        prompt_key: str,
        payload: dict[str, Any],
        chat_id: int | None = None,
    ):
        if prompt_key == GROUP_RESEARCH_PROMPT and chat_id is None:
            raise HTTPException(
                status_code=422,
                detail="chat_id is required for group research prompts",
            )
        try:
            detail = save_prompt_draft(
                app.state.session_factory,
                prompt_key,
                content=str(payload.get("content") or ""),
                change_note=str(payload.get("change_note") or ""),
                chat_id=chat_id,
                expected_active_version_id=_int_or_none(
                    payload.get("expected_active_version_id")
                ),
                expected_draft_updated_at=_parse_optional_datetime(
                    payload.get("expected_draft_updated_at")
                ),
            )
        except PromptRegistryNotFound as exc:
            if prompt_key != GROUP_RESEARCH_PROMPT or chat_id is None:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            seed_group_research_prompt(
                app.state.session_factory,
                chat_id=chat_id,
            )
            detail = save_prompt_draft(
                app.state.session_factory,
                prompt_key,
                content=str(payload.get("content") or ""),
                change_note=str(payload.get("change_note") or ""),
                chat_id=chat_id,
            )
        except PromptRegistryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PromptRegistryError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _prompt_detail_response(detail)

    @app.post("/api/ai-prompts/{prompt_key}/validate")
    def validate_ai_prompt_draft(
        prompt_key: str,
        payload: dict[str, Any],
        chat_id: int | None = None,
    ):
        try:
            detail = get_prompt_detail(
                app.state.session_factory,
                prompt_key,
                chat_id=chat_id,
            )
            expected_draft_id = int(payload.get("expected_draft_version_id"))
            if (
                detail.draft_version is None
                or detail.draft_version.id != expected_draft_id
            ):
                raise PromptRegistryConflict("draft version changed")
            result = validate_prompt_content(
                prompt_key,
                detail.draft_version.content,
                validation_profile=detail.validation_profile,
                required_variables=detail.required_variables,
            )
            record_prompt_validation(
                app.state.session_factory,
                prompt_key,
                expected_draft_version_id=expected_draft_id,
                success=result.success,
                errors=result.errors,
                chat_id=chat_id,
            )
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PromptRegistryConflict, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PromptRegistryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"success": result.success, "errors": list(result.errors)}

    @app.post("/api/ai-prompts/{prompt_key}/publish")
    def publish_ai_prompt(
        prompt_key: str,
        payload: dict[str, Any],
        chat_id: int | None = None,
    ):
        try:
            current = get_prompt_detail(
                app.state.session_factory,
                prompt_key,
                chat_id=chat_id,
            )
            expected_draft_id = int(payload.get("expected_draft_version_id"))
            expected_active_id = int(payload.get("expected_active_version_id"))
            if current.category == "trading":
                shared = get_prompt_detail(
                    app.state.session_factory, SHARED_TRADING_PROMPT
                )
                vision = get_prompt_detail(
                    app.state.session_factory, MIMO_VISION_PROMPT
                )
                expected_by_model = {
                    "deepseek": {SHARED_TRADING_PROMPT: shared.active_version.id},
                    "mimo": {
                        SHARED_TRADING_PROMPT: shared.active_version.id,
                        MIMO_VISION_PROMPT: vision.active_version.id,
                    },
                }
                required_models = (
                    {"mimo", "deepseek"}
                    if prompt_key == SHARED_TRADING_PROMPT
                    else {"mimo"}
                )
                with app.state.session_factory() as session:
                    completed_tests = (
                        session.query(AiPromptTestRun)
                        .filter(AiPromptTestRun.draft_version_id == expected_draft_id)
                        .filter(AiPromptTestRun.status == "completed")
                        .all()
                    )
                covered_models = {
                    row.model_kind
                    for row in completed_tests
                    if row.model_kind in required_models
                    and json.loads(row.active_prompt_versions_json or "{}")
                    == expected_by_model[row.model_kind]
                }
                if not required_models.issubset(covered_models):
                    raise PromptRegistryConflict(
                        "trading prompt draft requires current historical tests "
                        f"for: {', '.join(sorted(required_models - covered_models))}"
                    )
            detail = publish_prompt_draft(
                app.state.session_factory,
                prompt_key,
                expected_draft_version_id=expected_draft_id,
                expected_active_version_id=expected_active_id,
                chat_id=chat_id,
            )
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PromptRegistryConflict, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PromptRegistryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _prompt_detail_response(detail)

    @app.post("/api/ai-prompts/{prompt_key}/test")
    def test_ai_prompt_draft(
        prompt_key: str,
        payload: dict[str, Any],
    ):
        try:
            draft_version_id = int(payload.get("draft_version_id"))
            raw_message_ids = [
                int(value) for value in (payload.get("raw_message_ids") or [])
            ]
            model_kinds = [
                str(value) for value in (payload.get("model_kinds") or ["mimo"])
            ]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not raw_message_ids:
            raise HTTPException(status_code=422, detail="raw_message_ids is required")
        if len(raw_message_ids) > 20:
            raise HTTPException(status_code=422, detail="at most 20 messages per test")
        try:
            detail = get_prompt_detail(app.state.session_factory, prompt_key)
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if detail.category != "trading":
            raise HTTPException(
                status_code=422, detail="historical tests support trading prompts only"
            )
        if detail.draft_version is None or detail.draft_version.id != draft_version_id:
            raise HTTPException(status_code=409, detail="draft version changed")
        if not model_kinds or any(kind not in {"mimo", "deepseek"} for kind in model_kinds):
            raise HTTPException(status_code=422, detail="unsupported model kind")
        if prompt_key == MIMO_VISION_PROMPT and set(model_kinds) != {"mimo"}:
            raise HTTPException(
                status_code=422, detail="MiMo vision prompt can only be tested with MiMo"
            )
        if len(raw_message_ids) * len(model_kinds) > 20:
            raise HTTPException(status_code=422, detail="at most 20 model calls per test")

        config = load_ai_recognition_config(app.state.ai_recognition_config_path)
        items: list[dict[str, Any]] = []
        for raw_message_id in raw_message_ids:
            for model_kind in model_kinds:
                try:
                    result = app.state.prompt_test_runner(
                        app.state.session_factory,
                        prompt_key=prompt_key,
                        draft_version_id=draft_version_id,
                        raw_message_id=raw_message_id,
                        model_kind=model_kind,
                        ai_recognition_config=config,
                        media_root=app.state.media_root,
                    )
                except (LookupError, ValueError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                items.append(
                    {
                        "test_run_id": result.test_run_id,
                        "raw_message_id": raw_message_id,
                        "model_kind": model_kind,
                        "active_payload": result.active_payload,
                        "draft_payload": result.draft_payload,
                        "differences": result.differences,
                        "duration_ms": result.duration_ms,
                        "error_message": result.error_message,
                    }
                )
        return {"items": items}

    @app.post("/api/ai-prompts/{prompt_key}/rollback")
    def rollback_ai_prompt(
        prompt_key: str,
        payload: dict[str, Any],
        chat_id: int | None = None,
    ):
        try:
            detail = rollback_prompt(
                app.state.session_factory,
                prompt_key,
                source_version_id=int(payload.get("source_version_id")),
                change_note=str(payload.get("change_note") or ""),
                expected_active_version_id=int(
                    payload.get("expected_active_version_id")
                ),
                chat_id=chat_id,
            )
        except PromptRegistryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PromptRegistryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PromptRegistryError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _prompt_detail_response(detail)

    @app.post("/api/ai-recognition-config")
    def update_ai_recognition_config(payload: dict[str, Any]):
        existing_config = load_ai_recognition_config(app.state.ai_recognition_config_path)
        config = save_ai_recognition_config(
            app.state.ai_recognition_config_path,
            AiRecognitionConfig(
                recognition_prompt=existing_config.recognition_prompt,
                lifecycle_event_prompt=existing_config.lifecycle_event_prompt,
                mimo_direct_prompt=existing_config.mimo_direct_prompt,
                mode=str(payload.get("mode") or "ai_provider"),
                text_provider=_provider_config_from_payload(
                    payload.get("text_provider"), existing_config.text_provider
                ),
                image_provider=_provider_config_from_payload(
                    payload.get("image_provider"), existing_config.image_provider
                ),
                ai_models=_model_configs_from_payload(
                    payload.get("ai_models"), existing_config.ai_models
                ),
                active_text_model_id=str(payload.get("active_text_model_id") or ""),
                active_image_model_id=str(payload.get("active_image_model_id") or ""),
            ),
        )
        return {
            "mode": config.mode,
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
        message_limit = extract_recent_message_limit(question) or 50
        messages = load_group_messages(
            app.state.session_factory,
            chat_id=chat_id,
            limit=message_limit,
        )
        scope_context = build_scope_context(list(reversed(messages)))
        config = app.state.llm_proxy_config
        system = render_registered_prompt(
            app.state.session_factory,
            RESEARCH_CHAT_SYSTEM_PROMPT,
        )
        group = None
        try:
            group = render_registered_prompt(
                app.state.session_factory,
                GROUP_RESEARCH_PROMPT,
                chat_id=chat_id,
            )
        except PromptRegistryNotFound:
            pass
        group_prompt = group.content if group is not None else None
        version_map = dict(system.version_map)
        if group is not None:
            version_map.update(group.version_map)
        invocation_status = "success"
        invocation_error = None
        try:
            answer = app.state.chat_requester(
                config=config,
                question=question,
                scope_context=scope_context,
                system_prompt=system.content,
                group_prompt=group_prompt,
            )
        except httpx.HTTPError as exc:
            invocation_status = "error"
            invocation_error = str(exc)
            raise HTTPException(
                status_code=502,
                detail=_build_chat_proxy_error_detail(exc),
            ) from exc
        finally:
            record_prompt_invocation(
                app.state.session_factory,
                PromptInvocationRecord(
                    feature="research_chat",
                    correlation_key=f"research_chat:{chat_id}:{time.time_ns()}",
                    chat_id=chat_id,
                    model=config.model,
                    prompt_versions=version_map,
                    status=invocation_status,
                    error_message=invocation_error,
                ),
            )
        return {
            "answer": answer,
            "scope_mode": "current_group",
            "scope_message_count": len(messages),
            "proxy_payload": build_proxy_chat_payload(
                question=question,
                scope_context=scope_context,
                model=config.model,
                system_prompt=system.content,
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
                    reconcile_kwargs = _filter_callable_kwargs(
                        app.state.reconcile_once_runner,
                        {
                            "client": telegram_client,
                            "session_factory": app.state.session_factory,
                            "broker": app.state.live_update_broker,
                            "target_titles": set(app.state.live_target_titles),
                            "media_root": app.state.media_root,
                            "strategy_alert_config": app.state.strategy_alert_config,
                            "strategy_alert_enabled_for_title": app.state.strategy_alert_enabled_for_title,
                            "authoritative_processor": app.state.authoritative_processor,
                            "system_operator_bot_config": app.state.system_operator_bot_config,
                        },
                    )
                    return await asyncio.wait_for(
                        app.state.reconcile_once_runner(
                            **reconcile_kwargs,
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


async def run_deepcoin_execution_reconcile_loop(
    *,
    session_factory,
    deepcoin_client_factory,
    interval_seconds: int = 30,
    now_provider=None,
    system_operator_bot_config: SystemOperatorBotConfig | None = None,
) -> None:
    while True:
        try:
            client = deepcoin_client_factory()
            synced_at = now_provider() if now_provider is not None else datetime.now(UTC)
            if hasattr(client, "list_open_orders"):
                reconcile_deepcoin_execution_bindings(
                    session_factory,
                    client=client,
                    recovered_at=synced_at,
                )
                if system_operator_bot_enabled(system_operator_bot_config):
                    await deliver_pending_position_attribution_incidents(
                        session_factory,
                        config=system_operator_bot_config,
                        delivered_at=synced_at,
                    )
            sync_manual_closed_deepcoin_positions(
                session_factory,
                client=client,
                synced_at=synced_at,
            )
        except DeepcoinClientError as exc:
            logger.warning("Deepcoin execution reconcile skipped: %s", exc)
        except Exception:
            logger.exception("Deepcoin execution reconcile failed")
        await asyncio.sleep(interval_seconds)


async def _run_reconcile_after_startup_delay(
    *,
    runner,
    startup_delay_seconds: int,
    **kwargs,
):
    if startup_delay_seconds > 0:
        await asyncio.sleep(startup_delay_seconds)
    await runner(**_filter_callable_kwargs(runner, kwargs))


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


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _prompt_version_response(version: Any) -> dict[str, Any]:
    return {
        "id": version.id,
        "version_number": version.version_number,
        "content": version.content,
        "status": version.status,
        "change_note": version.change_note,
        "source_version_id": version.source_version_id,
        "validated_at": _datetime_to_iso(version.validated_at),
        "validation_result": version.validation_result,
        "created_at": _datetime_to_iso(version.created_at),
        "updated_at": _datetime_to_iso(version.updated_at),
        "published_at": _datetime_to_iso(version.published_at),
    }


def _prompt_detail_response(detail: PromptDetail) -> dict[str, Any]:
    return {
        "definition_id": detail.definition_id,
        "prompt_key": detail.prompt_key,
        "display_name": detail.display_name,
        "description": detail.description,
        "category": detail.category,
        "scope_key": detail.scope_key,
        "scope_chat_id": detail.scope_chat_id,
        "consumers": list(detail.consumers),
        "required_variables": list(detail.required_variables),
        "validation_profile": detail.validation_profile,
        "enabled": detail.enabled,
        "active_version": _prompt_version_response(detail.active_version),
        "draft_version": (
            _prompt_version_response(detail.draft_version)
            if detail.draft_version is not None
            else None
        ),
        "history": [_prompt_version_response(item) for item in detail.history],
    }


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _provider_config_from_payload(
    payload: Any,
    existing: AiProviderConfig | None = None,
) -> AiProviderConfig:
    if not isinstance(payload, dict):
        return existing or AiProviderConfig()
    return AiProviderConfig(
        base_url=str(payload.get("base_url") or ""),
        api_key=str(payload.get("api_key") or (existing.api_key if existing else "")),
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
        "api_key": "",
        "api_key_configured": bool(config.api_key),
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
    }


def _model_configs_from_payload(
    payload: Any,
    existing: list[AiModelConfig] | None = None,
) -> list[AiModelConfig]:
    if not isinstance(payload, list):
        return list(existing or [])
    models: list[AiModelConfig] = []
    existing_by_id = {model.id: model for model in (existing or [])}
    for item in payload:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("model") or "")
        prior = existing_by_id.get(model_id)
        models.append(
            AiModelConfig(
                id=model_id,
                label=str(item.get("label") or item.get("model") or item.get("id") or ""),
                base_url=str(item.get("base_url") or ""),
                api_key=str(item.get("api_key") or (prior.api_key if prior else "")),
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
        "api_key": "",
        "api_key_configured": bool(config.api_key),
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
