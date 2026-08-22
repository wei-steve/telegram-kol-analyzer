"""FastAPI app for the Telegram web workbench."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select

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
from telegram_kol_research.auto_trade_execution import disabled_management_message_needs_no_client
from telegram_kol_research.authoritative_recognition import (
    assess_message_authoritatively,
    process_authoritative_message,
)
from telegram_kol_research.message_evidence import build_message_input_fingerprint
from telegram_kol_research.mimo_contract_circuit import load_mimo_contract_circuit
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
from telegram_kol_research.deepcoin_contract_spec_cache import (
    DeepcoinContractSpecRefreshOrchestrator,
)
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_execution_actions import DeepcoinExecutionActionError
from telegram_kol_research.deepcoin_execution_actions import close_bound_position_market
from telegram_kol_research.runtime_agent_production_audit import (
    project_bounded_production_audit,
    run_bounded_production_audit_command,
)
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.message_lock_provider import MessageLockProvider
from telegram_kol_research.message_processing_worker import (
    run_message_processing_worker_loop,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_monitor_state,
    capture_notification_failure,
)
from telegram_kol_research.runtime_loop_health import LoopLagMonitor
from telegram_kol_research.runtime_worker_executor import (
    run_on_management_worker,
    shutdown_management_worker_executor,
)
from telegram_kol_research.production_safety_monitor import (
    MONITOR_ADAPTER_NAMES,
    capture_uncaptured_runtime_incident_sources,
)
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import update_group_automation_settings
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.live_position_snapshot import LivePositionSnapshotStore
from telegram_kol_research.models import (
    AiPromptTestRun,
    ExecutionBinding,
    ExecutionOrderLeg,
    MediaAsset,
    MessageEvidenceVersion,
    MessageProcessingJob,
    PositionBackupStopOrder,
    PositionProtectionLedger,
    RecognitionDecision,
    StrategyManagementBatch,
    StrategyManagementLeg,
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
from telegram_kol_research.position_attribution import has_authoritative_persisted_position
from telegram_kol_research.position_attribution import require_manual_position_attribution_allowed
from telegram_kol_research.protection_attribution import match_position_protection
from telegram_kol_research.protection_ledger import (
    build_account_protection_ownership,
)
from telegram_kol_research.protection_snapshot import build_position_protection_audit
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_runner import run_recovery_dry_run
from telegram_kol_research.runtime_agent_exchange_snapshot import (
    build_read_only_exchange_snapshot,
    incomplete_read_only_exchange_snapshot,
)
from telegram_kol_research.semantic_disagreement_review import run_semantic_review_loop
from telegram_kol_research.strategy_management_worker import (
    run_strategy_management_worker_loop,
)
from telegram_kol_research.break_even_convergence_worker import (
    run_break_even_convergence_worker_loop,
)
from telegram_kol_research.source_message_deletion_worker import (
    run_source_message_deletion_worker_loop,
)
from telegram_kol_research.strategy_records import (
    count_strategy_records,
    enrich_strategy_records_with_exchange,
    load_live_bindings_without_lifecycle,
    load_strategy_record_detail,
    load_strategy_record_summaries,
    management_execution_drift_reason,
)
from telegram_kol_research.strategy_alerts import (
    StrategyAlertConfig,
    load_strategy_alert_config,
    process_strategy_alert_for_record,
    strategy_alerts_enabled,
)
from telegram_kol_research.config import (
    MessageOperationSupervisorConfig,
    RuntimeIncidentConfig,
    load_message_operation_supervisor_config,
    load_runtime_incident_config,
    message_operation_supervisor_policy_status,
)
from telegram_kol_research.message_operation_supervisor import (
    build_message_operation_coverage_snapshot,
    run_message_operation_supervisor_cycle,
)
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    canonical_management_error_summary,
    deliver_terminal_entry_cleanup_notifications,
    deliver_pending_position_attribution_incidents,
    deliver_pending_position_protection_incidents,
    load_notification_bot_config,
    load_system_operator_bot_config,
    probe_system_operator_bot_evidence,
    send_ai_recognition_conflict_review,
    send_pending_entry_expiry_review,
    send_semantic_disagreement_notification,
    send_system_operator_bot_message,
    run_runtime_incident_notification_loop,
    run_strategy_management_notification_loop,
    system_operator_bot_enabled,
)
from telegram_kol_research.runtime_agent_telegram_evidence import (
    project_bounded_telegram_evidence,
)
from telegram_kol_research.time_utils import DEFAULT_LOCAL_TIMEZONE
from telegram_kol_research.trading_settings import (
    SymbolEntryThresholds,
    load_trading_settings,
    save_trading_settings,
    trading_settings_from_payload,
)
from telegram_kol_research.context_resolution import resolve_contextual_strategy
from telegram_kol_research.context_resolution_worker import (
    build_redacted_exchange_state,
    build_context_state_fingerprint,
    run_context_resolution_once,
    schedule_context_reanalysis,
)
from telegram_kol_research.entry_revision_executor import (
    run_entry_revision_worker_once,
)
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.web_queries import (
    load_database_freshness,
    load_group_message_page,
    load_group_messages,
    load_group_rows,
    load_home_event_rows,
    list_execution_strategy_overview,
    load_lifecycle_counts,
    load_lifecycle_counts_by_chat_id,
    list_exited_strategies,
    list_verified_deepcoin_history_positions,
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
    _deliver_authoritative_instruction_summary,
    _filter_callable_kwargs,
    _handle_authoritative_failure_notification,
    _schedule_authoritative_notification,
    launch_live_listener_task,
    run_live_listener,
)
from telegram_kol_research.telegram_live_listener import (
    run_authoritative_gap_recovery_loop,
    run_periodic_reconcile,
    run_reconcile_once,
)
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
MESSAGE_PAGE_SIZE = 20
SESSION_LOCK_OWNER_PID_PATTERN = re.compile(r"owner pid=(\d+)")
logger = logging.getLogger(__name__)


async def _run_monitor_capture_writer(writer: Callable[[], int]) -> int:
    """Run one writer outside the saturated shared executor and drain on cancel."""

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="monitor-incident-capture",
    )
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, writer)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
_TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS = 5.0

_MANAGEMENT_SECRET_MARKERS = (
    "dc-access", "authorization", "api-key", "api_key", "passphrase",
    "secret", "raw_header", "raw_payload", "raw_response",
)


def _bounded_management_text(value: Any, *, limit: int = 300) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if any(marker in text.lower() for marker in _MANAGEMENT_SECRET_MARKERS):
        return "[redacted]"
    return text[:limit]


def _json_value(value: Any, default):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded


def _safe_protection_rows(value: Any) -> list[dict[str, Any]]:
    decoded = _json_value(value, [])
    if isinstance(decoded, dict):
        decoded = decoded.get("rows") or decoded.get("orders") or [decoded]
    if not isinstance(decoded, list):
        return []
    safe = []
    allowed = (
        "purpose", "order_id", "ordId", "tpsl_id", "type", "side",
        "trigger_price", "triggerPx", "price", "size", "status",
    )
    for row in decoded[:20]:
        if not isinstance(row, dict):
            continue
        safe.append(
            {
                key: _bounded_management_text(row.get(key), limit=120)
                for key in allowed if row.get(key) is not None
            }
        )
    return safe


def _management_batch_mode(batch: StrategyManagementBatch, snapshot: dict[str, Any]) -> str:
    del snapshot
    mode = str(batch.execution_mode or "disabled")
    return mode if mode in {"disabled", "shadow", "live"} else "disabled"


def _management_protection_recovery_bypass(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    marker = snapshot.get("protection_recovery_bypass")
    if not isinstance(marker, dict) or marker.get("version") != 1:
        return None
    pos_ids = marker.get("target_pos_ids")
    if not isinstance(pos_ids, list):
        return None
    return {
        "reason": _bounded_management_text(marker.get("reason"), limit=80),
        "allowed_action": _bounded_management_text(
            marker.get("allowed_action"), limit=64
        ),
        "target_pos_ids": [
            _bounded_management_text(pos_id, limit=120)
            for pos_id in pos_ids[:20]
            if pos_id not in (None, "")
        ],
    }


def _management_batch_api_rows(
    session_factory, *, chat_id: int | None, limit: int, group_labels=None
):
    labels = group_labels or {}
    with session_factory() as session:
        query = (
            session.query(StrategyManagementBatch, RawMessage)
            .join(RawMessage, RawMessage.id == StrategyManagementBatch.raw_message_id)
        )
        if chat_id is not None:
            query = query.filter(RawMessage.chat_id == chat_id)
        rows = query.order_by(
            StrategyManagementBatch.planned_at.desc(), StrategyManagementBatch.id.desc()
        ).limit(limit).all()
        result = []
        for batch, raw in rows:
            snapshot = _json_value(batch.target_snapshot_json, {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            mode = _management_batch_mode(batch, snapshot)
            legs = []
            for leg in (
                session.query(StrategyManagementLeg)
                .filter(StrategyManagementLeg.management_batch_id == batch.id)
                .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
                .limit(20)
            ):
                legs.append(
                    {
                        "id": leg.id,
                        "execution_order_leg_id": leg.execution_order_leg_id,
                        "pos_id": _bounded_management_text(leg.pos_id, limit=120),
                        "leg_index": leg.leg_index,
                        "status": _bounded_management_text(leg.status, limit=64),
                        "preflight_size": _bounded_management_text(leg.preflight_size, limit=64),
                        "planned_close_size": _bounded_management_text(leg.planned_close_size, limit=64),
                        "avg_entry_price": _bounded_management_text(leg.avg_entry_price, limit=64),
                        "client_order_id": _bounded_management_text(leg.client_order_id, limit=120),
                        "exchange_order_id": _bounded_management_text(leg.exchange_order_id, limit=120),
                        "old_protection": _safe_protection_rows(leg.old_tpsl_json),
                        "planned_protection": _safe_protection_rows(leg.planned_tpsl_json),
                        "error_summary": canonical_management_error_summary(
                            leg.last_error
                        ),
                    }
                )
            positions = snapshot.get("positions") or snapshot.get("targets")
            targets = []
            if isinstance(positions, list):
                for position in positions[:20]:
                    if isinstance(position, dict):
                        targets.append(
                            {
                                key: _bounded_management_text(position.get(key), limit=120)
                                for key in ("pos_id", "size", "avg_entry_price")
                                if position.get(key) is not None
                            }
                        )
            result.append(
                {
                    "batch_id": batch.id,
                    "mode": mode,
                    "mode_label": (
                        "实盘执行" if mode == "live" else "未调用交易 API"
                    ),
                    "intent": batch.intent,
                    "effective_action": batch.effective_action,
                    "requested_fraction": batch.requested_fraction,
                    "effective_fraction": batch.effective_fraction,
                    "partial_round_before": batch.partial_round_before,
                    "status": batch.status,
                    "reason": _bounded_management_text(batch.reason_code, limit=240),
                    "protection_recovery_bypass": _management_protection_recovery_bypass(
                        snapshot
                    ),
                    "safety_label": "禁止自动重试" if batch.status == "recovery_required" else None,
                    "source": {
                        "chat_id": raw.chat_id,
                        "chat_title": _bounded_management_text(labels.get(raw.chat_id), limit=120),
                        "message_id": raw.message_id,
                        "raw_message_id": raw.id,
                    },
                    "lifecycle_id": batch.target_lifecycle_id,
                    "strategy_instance_id": batch.strategy_instance_id,
                    "execution_binding_id": batch.execution_binding_id,
                    "targets": targets,
                    "legs": legs,
                    "planned_at": batch.planned_at.isoformat() if batch.planned_at else None,
                    "started_at": batch.started_at.isoformat() if batch.started_at else None,
                    "reconciled_at": batch.reconciled_at.isoformat() if batch.reconciled_at else None,
                    "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
                    "updated_at": batch.updated_at.isoformat() if batch.updated_at else None,
                }
            )
        return result


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
    config = app.state.notification_bot_config
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


async def _disconnect_shared_telegram_client(app: FastAPI) -> None:
    client = app.state.telegram_client
    disconnect = getattr(client, "disconnect", None)
    if not callable(disconnect):
        return
    disconnect_task = asyncio.ensure_future(maybe_await(disconnect()))
    done, _pending = await asyncio.wait(
        {disconnect_task}, timeout=_TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS
    )
    if disconnect_task not in done:
        disconnect_task.cancel()
        logger.warning("Telegram client disconnect exceeded shutdown timeout")
        return
    try:
        disconnect_task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Telegram client disconnect failed during shutdown")


async def _stop_live_listener_task(app: FastAPI) -> None:
    task = app.state.live_listener_task
    if task is None:
        return
    await _disconnect_shared_telegram_client(app)
    task.cancel()
    done, _pending = await asyncio.wait(
        {task}, timeout=_TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS
    )
    if task not in done:
        logger.warning("Telegram live listener exceeded shutdown timeout")
    else:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Telegram live listener failed during shutdown")
    app.state.live_listener_task = None


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


def _strategy_record_api_sort_key(record: dict[str, object]) -> tuple[int, float, int]:
    attention = record.get("attention")
    severity = (
        str(attention.get("severity") or "")
        if isinstance(attention, dict)
        else ""
    )
    severity_rank = {"critical": 0, "warning": 1, "review": 2}.get(severity, 3)
    latest_changed_at = record.get("latest_changed_at")
    if isinstance(latest_changed_at, datetime):
        if latest_changed_at.tzinfo is None:
            latest_changed_at = latest_changed_at.replace(tzinfo=UTC)
        timestamp = latest_changed_at.timestamp()
    else:
        timestamp = 0.0
    lifecycle_id = record.get("lifecycle_id")
    return (
        severity_rank,
        -timestamp,
        -int(lifecycle_id) if lifecycle_id is not None else 0,
    )


STRATEGY_RECORD_FILTERS = {
    "needs_attention",
    "all",
    "executing",
    "pending_entry",
    "finished",
}


def _strategy_record_matches_filter(
    record: dict[str, object],
    *,
    filter_name: str,
) -> bool:
    """Apply mobile record filters only after exchange enrichment."""

    if filter_name == "all":
        return True
    if filter_name == "needs_attention":
        return record.get("attention") is not None

    lifecycle_state = str(record.get("lifecycle_state") or "").strip().lower()
    if filter_name == "pending_entry":
        return lifecycle_state == "pending_entry"
    if filter_name == "finished":
        return lifecycle_state in {
            "cancelled",
            "exited",
            "expired",
            "finished",
            "invalidated",
            "rejected",
        }
    if filter_name == "executing":
        return (
            lifecycle_state == "entered"
            or record.get("real_position") is not None
            or bool(record.get("real_positions"))
        )
    return False


def _strategy_detail_position_ownership(
    *,
    detail: dict[str, object],
    binding: dict[str, object],
    position: dict[str, object],
    expected_pos_id: object | None = None,
) -> tuple[str, str, list[str]]:
    """Fail closed unless exchange attribution proves the requested owner."""

    attribution = position.get("attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    binding_venue = str(binding.get("venue") or "").strip().lower()
    if binding_venue != "deepcoin":
        return (
            "conflict",
            "执行绑定不是 Deepcoin，禁止确认 Deepcoin 仓位归属",
            [f"binding.venue={binding_venue or 'unknown'}"],
        )

    terminal_states = {
        "cancelled",
        "canceled",
        "closed",
        "exchange_cancelled",
        "exchange_closed",
        "expired",
        "failed",
        "manually_cancelled",
        "manually_closed",
        "rejected",
        "stale",
        "terminal",
    }
    binding_status = str(binding.get("status") or "unknown").strip().lower()
    if binding_status not in {"open", "active"}:
        return (
            "conflict" if binding_status in terminal_states else "unconfirmed",
            "本地执行绑定不是可确认的活跃状态",
            [f"binding.status={binding_status}"],
        )

    attribution_state = str(attribution.get("state") or "").strip().lower()
    execution_status = str(position.get("execution_status") or "").strip().lower()
    exchange_status = str(position.get("exchange_status") or "").strip().lower()
    binding_exchange_status = str(
        binding.get("last_exchange_status") or ""
    ).strip().lower()
    if attribution_state == "conflict" or execution_status == "system_attribution_conflict":
        return (
            "conflict",
            "Deepcoin 快照明确标记了归属冲突",
            [
                item
                for item in (
                    f"attribution.state={attribution_state}" if attribution_state else None,
                    f"execution_status={execution_status}" if execution_status else None,
                )
                if item is not None
            ],
        )
    terminal_snapshot_states = terminal_states.intersection(
        {execution_status, exchange_status, binding_exchange_status}
    )
    if terminal_snapshot_states:
        return (
            "conflict",
            "Deepcoin 快照或绑定包含终态执行证据",
            sorted(terminal_snapshot_states),
        )
    if attribution_state != "bound":
        return (
            "unconfirmed",
            "Deepcoin 仓位存在，但策略归属尚未唯一确认",
            ["交易所归属状态不是 bound"],
        )

    identity = detail.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    mismatches: list[str] = []
    missing: list[str] = []

    expected_strategy_id = binding.get("strategy_instance_id")
    actual_strategy_id = attribution.get("strategy_id")
    if expected_strategy_id in {None, ""} or actual_strategy_id in {None, ""}:
        missing.append("strategy_instance_id")
    elif str(actual_strategy_id) != str(expected_strategy_id):
        mismatches.append("strategy_instance_id")

    expected_pos_id = expected_pos_id or binding.get("pos_id")
    actual_attribution_pos_id = attribution.get("pos_id")
    actual_position_pos_id = position.get("pos_id") or position.get("posId")
    if actual_attribution_pos_id in {None, ""}:
        missing.append("attribution.pos_id")
    elif str(actual_attribution_pos_id) != str(expected_pos_id):
        mismatches.append("attribution.pos_id")
    if str(actual_position_pos_id or "") != str(expected_pos_id or ""):
        mismatches.append("position.pos_id")

    expected_chat_id = binding.get("chat_id")
    expected_message_id = binding.get("message_id")
    for label, actual, expected in (
        ("attribution.chat_id", attribution.get("chat_id"), expected_chat_id),
        ("position.chat_id", position.get("chat_id"), expected_chat_id),
        ("position.message_id", position.get("message_id"), expected_message_id),
        ("record.chat_id", identity.get("chat_id"), expected_chat_id),
        ("record.message_id", identity.get("message_id"), expected_message_id),
    ):
        if actual not in {None, ""} and str(actual) != str(expected):
            mismatches.append(label)

    if mismatches:
        return (
            "conflict",
            "Deepcoin 仓位已匹配，但策略归属身份不一致",
            mismatches,
        )
    if missing:
        return (
            "unconfirmed",
            "Deepcoin 仓位已匹配，但策略归属身份证据不完整",
            missing,
        )
    return (
        "confirmed",
        "Deepcoin 实时仓位及策略归属已确认",
        [],
    )


def _load_deepcoin_live_position_rows(
    session_factory,
    *,
    deepcoin_client_factory,
    group_label_by_chat_id: dict[int, str],
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    error_state: dict[str, str] | None = None,
    deepcoin_client=None,
    unattributed_protection_rows: list[dict[str, str]] | None = None,
    source_capture: dict[str, Any] | None = None,
) -> list[dict[str, object]]:
    try:
        if deepcoin_client is None:
            deepcoin_client = deepcoin_client_factory()
        positions = deepcoin_client.list_positions()
    except Exception:
        logger.exception("Deepcoin live position load failed")
        if error_state is not None:
            error_state["error"] = "unavailable"
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
    if source_capture is not None:
        source_capture.update(
            {
                "positions": positions,
                "tpsl_orders": tpsl_orders,
                "tpsl_evidence_available": tpsl_evidence_available,
            }
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
        verified_live_leg_ids: set[int] = set()
        for leg in legs:
            binding = bindings_by_id.get(int(leg.execution_binding_id))
            if (
                leg.id is not None
                and str(leg.attribution_status or "").lower() == "verified"
                and str(leg.status or "").lower() == "active"
                and str(leg.pos_id or "") in active_pos_ids
                and binding is not None
                and str(binding.status or "").lower() == "active"
                and has_authoritative_persisted_position(leg, session=session)
            ):
                verified_live_leg_ids.add(int(leg.id))
        ledger_rows = (
            (
                session.query(PositionProtectionLedger)
                .filter(PositionProtectionLedger.venue == "deepcoin")
                .filter(
                    PositionProtectionLedger.execution_order_leg_id.in_(
                        verified_live_leg_ids
                    )
                )
                .filter(PositionProtectionLedger.status == "verified")
                .all()
            )
            if verified_live_leg_ids
            else []
        )
        account_ownership = build_account_protection_ownership(
            ledger_rows,
            venue="deepcoin",
            live_pos_ids=active_pos_ids,
        )
        exact_order_position_ids = {
            order_id: owner.pos_id
            for order_id, owner in account_ownership.by_order_id.items()
        }
        direct_protection_rows, pending_unattributed_rows = (
            _split_exchange_protection_display_rows(
                positions=active_positions,
                pending_orders=tpsl_orders,
                account_ownership=account_ownership,
                contract_spec_provider=contract_spec_provider,
            )
        )
        if unattributed_protection_rows is not None:
            unattributed_protection_rows.extend(pending_unattributed_rows)
        backup_by_leg_id = {
            int(row.execution_order_leg_id): row
            for row in (
                session.query(PositionBackupStopOrder)
                .filter(PositionBackupStopOrder.venue == "deepcoin")
                .filter(
                    PositionBackupStopOrder.execution_order_leg_id.in_(verified_live_leg_ids)
                )
                .order_by(PositionBackupStopOrder.id.asc())
                .all()
                if verified_live_leg_ids
                else []
            )
            if (
                (leg := legs_by_pos_id.get(str(row.pos_id))) is not None
                and int(leg.id) == int(row.execution_order_leg_id)
            )
        }
        protection_match = match_position_protection(
            active_positions,
            tpsl_orders,
            evidence_available=tpsl_evidence_available,
            exact_order_position_ids=exact_order_position_ids,
        )
        lifecycle_candidates_by_binding_id: dict[int, list[StrategyLifecycle]] = {}
        if binding_ids:
            bound_lifecycles = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.execution_binding_id.in_(binding_ids))
                .order_by(
                    StrategyLifecycle.execution_binding_id.asc(),
                    StrategyLifecycle.id.asc(),
                )
                .all()
            )
            for lifecycle_candidate in bound_lifecycles:
                lifecycle_candidates_by_binding_id.setdefault(
                    int(lifecycle_candidate.execution_binding_id), []
                ).append(lifecycle_candidate)

        rows: list[dict[str, object]] = []
        for position in active_positions:
            pos_id = _first_position_string(position, "posId", "pos_id", "id")
            exchange_protection_orders = direct_protection_rows.get(pos_id or "", [])
            position_side = _normalize_deepcoin_position_side(
                position.get("posSide") or position.get("side")
            )
            (
                exchange_stop_loss,
                exchange_backup_stop,
                exchange_take_profits,
            ) = _summarize_verified_exchange_protection_rows(
                exchange_protection_orders, side=position_side
            )
            protection = protection_match.by_pos_id.get(pos_id or "")
            stop_loss_value = exchange_stop_loss
            take_profit_values = exchange_take_profits
            take_profit_value = take_profit_values[0] if take_profit_values else None
            has_protection = stop_loss_value is not None or take_profit_value is not None
            protection_status = protection.status if protection is not None else "absent"
            if (
                protection_status == "present_but_ambiguous"
                and protection is not None
                and stop_loss_value is None
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
            lifecycle_candidates = (
                lifecycle_candidates_by_binding_id.get(int(binding.id), [])
                if binding is not None
                else []
            )
            lifecycle = (
                lifecycle_candidates[0]
                if len(lifecycle_candidates) == 1
                else None
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
                    "backup_stop_text": exchange_backup_stop,
                    "backup_stop_state_text": "第二止损未设置" if ownership_verified else None,
                    "take_profit_text": "/".join(
                        _position_text_value(value) or "" for value in take_profit_values
                    ) or None,
                    "exchange_protection_orders": exchange_protection_orders,
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
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    order_limit: int = 100,
) -> dict[str, Any]:
    """Load the exchange-style dashboard snapshot from Deepcoin read APIs."""
    snapshot: dict[str, Any] = {
        "positions": [],
        "unattributed_protection_orders": [],
        "open_orders": [],
        "order_history": [],
        "position_history": [],
        "error": None,
    }
    try:
        client = deepcoin_client_factory()
    except Exception:
        logger.exception("Deepcoin exchange snapshot client creation failed")
        snapshot["error"] = "unavailable"
        return snapshot

    position_error: dict[str, str] = {}
    unattributed_protection_orders: list[dict[str, str]] = []
    positions = _load_deepcoin_live_position_rows(
        session_factory,
        deepcoin_client_factory=deepcoin_client_factory,
        group_label_by_chat_id=group_label_by_chat_id,
        contract_spec_provider=contract_spec_provider,
        error_state=position_error,
        deepcoin_client=client,
        unattributed_protection_rows=unattributed_protection_orders,
    )
    snapshot["positions"] = positions
    snapshot["unattributed_protection_orders"] = unattributed_protection_orders
    if position_error:
        snapshot["error"] = position_error["error"]
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
    raw_position_history: list[dict[str, Any]] = []
    for inst_id in sorted(instruments):
        raw_trigger_orders.extend(
            _safe_deepcoin_list(client, "list_trigger_orders_pending", inst_id=inst_id)
        )
        raw_trigger_history.extend(
            _safe_deepcoin_list(client, "list_trigger_order_history", inst_id=inst_id)
        )
        raw_position_history.extend(
            _safe_deepcoin_list(client, "list_position_history", inst_id=inst_id)
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
    snapshot["position_history"] = sorted(
        (
            _exchange_position_history_row(
                position,
                contract_spec_provider=contract_spec_provider,
            )
            for position in raw_position_history
            if _float_or_none(position.get("avgPx")) is not None
            and _float_or_none(position.get("closeAvgPx")) is not None
            and _float_or_none(position.get("closePos")) not in (None, 0.0)
        ),
        key=lambda row: (row.get("exited_at") or datetime.min.replace(tzinfo=UTC), row["history_sort_id"]),
        reverse=True,
    )[:order_limit]
    _attach_exchange_history_position_bindings(
        session_factory,
        snapshot["position_history"],
        group_label_by_chat_id=group_label_by_chat_id,
    )
    return snapshot


def _empty_exchange_snapshot() -> dict[str, Any]:
    return {
        "positions": [],
        "unattributed_protection_orders": [],
        "open_orders": [],
        "order_history": [],
        "position_history": [],
        "error": None,
    }


def _load_exchange_live_snapshot(
    session_factory,
    *,
    deepcoin_client_factory,
    group_label_by_chat_id: dict[int, str],
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> dict[str, Any]:
    """Load only live positions and their exact pending TPSL evidence."""
    snapshot = _empty_exchange_snapshot()
    try:
        client = deepcoin_client_factory()
    except Exception:
        logger.exception("Deepcoin live snapshot client creation failed")
        snapshot["error"] = "unavailable"
        return snapshot
    position_error: dict[str, str] = {}
    unattributed_protection_orders: list[dict[str, str]] = []
    source_capture: dict[str, Any] = {}
    scope = client if hasattr(client, "__enter__") else nullcontext(client)
    with scope:
        snapshot["positions"] = _load_deepcoin_live_position_rows(
            session_factory,
            deepcoin_client_factory=deepcoin_client_factory,
            group_label_by_chat_id=group_label_by_chat_id,
            contract_spec_provider=contract_spec_provider,
            error_state=position_error,
            deepcoin_client=client,
            unattributed_protection_rows=unattributed_protection_orders,
            source_capture=source_capture,
        )
    snapshot["unattributed_protection_orders"] = unattributed_protection_orders
    if position_error:
        snapshot["error"] = position_error["error"]
    else:
        snapshot["_live_source"] = source_capture
    return snapshot


@dataclass(frozen=True)
class HistoryPositionBrowsePage:
    """One immutable page from a short-lived history browse snapshot."""

    rows: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool
    total_count: int


@dataclass(frozen=True)
class _HistoryPositionBrowseSnapshot:
    rows: tuple[dict[str, Any], ...]
    filter_key: tuple[str | None, str | None]
    expires_at: datetime


class HistoryPositionBrowseSnapshotStore:
    """Keep one browser's bounded, read-only history browse result stable."""

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime],
        token_factory: Callable[[], str] | None = None,
        ttl: timedelta = timedelta(minutes=5),
        max_snapshots: int = 64,
        max_page_size: int = 20,
    ) -> None:
        self._now_provider = now_provider
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._ttl = max(timedelta(seconds=1), ttl)
        self._max_snapshots = max(1, int(max_snapshots))
        self._max_page_size = max(1, int(max_page_size))
        self._snapshots: dict[str, _HistoryPositionBrowseSnapshot] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        rows: tuple[dict[str, Any], ...],
        filter_key: tuple[str | None, str | None],
    ) -> str:
        now = self._utc_now()
        with self._lock:
            self._discard_expired(now)
            while len(self._snapshots) >= self._max_snapshots:
                self._snapshots.pop(next(iter(self._snapshots)))
            token = self._token_factory()
            if not token:
                raise ValueError("browse token unavailable")
            self._snapshots[token] = _HistoryPositionBrowseSnapshot(
                rows=tuple(rows),
                filter_key=filter_key,
                expires_at=now + self._ttl,
            )
            return token

    def page(
        self,
        *,
        token: str,
        cursor: str | None,
        page_size: int,
        filter_key: tuple[str | None, str | None],
    ) -> HistoryPositionBrowsePage:
        if not 1 <= page_size <= self._max_page_size:
            raise ValueError("page size")
        now = self._utc_now()
        with self._lock:
            self._discard_expired(now)
            snapshot = self._snapshots.get(token)
            if snapshot is None:
                raise ValueError("browse snapshot expired")
            if snapshot.filter_key != filter_key:
                raise ValueError("browse snapshot filter mismatch")
            start = 0
            if cursor is not None:
                for index, row in enumerate(snapshot.rows):
                    if row.get("history_sort_id") == cursor:
                        start = index + 1
                        break
                else:
                    raise ValueError("browse snapshot cursor is unknown")
            rows = snapshot.rows[start : start + page_size]
            has_more = start + len(rows) < len(snapshot.rows)
            return HistoryPositionBrowsePage(
                rows=rows,
                next_cursor=(str(rows[-1]["history_sort_id"]) if has_more and rows else None),
                has_more=has_more,
                total_count=len(snapshot.rows),
            )

    def _utc_now(self) -> datetime:
        now = self._now_provider()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)

    def _discard_expired(self, now: datetime) -> None:
        for token, snapshot in tuple(self._snapshots.items()):
            if snapshot.expires_at <= now:
                del self._snapshots[token]


class _CachedLivePositionClient:
    def __init__(self, source: dict[str, Any]) -> None:
        self._positions = list(source.get("positions") or [])
        self._tpsl_orders = list(source.get("tpsl_orders") or [])
        self._tpsl_evidence_available = bool(
            source.get("tpsl_evidence_available", True)
        )

    def list_positions(self):
        return self._positions

    def list_trigger_orders_pending(self, *, inst_id: str):
        if not self._tpsl_evidence_available:
            raise DeepcoinClientError("cached TPSL evidence unavailable")
        normalized_inst_id = str(inst_id or "").upper()
        return [
            order
            for order in self._tpsl_orders
            if not str(order.get("instId") or "")
            or str(order.get("instId") or "").upper() == normalized_inst_id
        ]


def _materialize_cached_live_position_snapshot(
    session_factory,
    *,
    payload: dict[str, Any],
    group_label_by_chat_id: dict[int, str],
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> dict[str, Any]:
    source = payload.get("_live_source")
    if not isinstance(source, dict):
        return payload
    snapshot = _empty_exchange_snapshot()
    unattributed_protection_orders: list[dict[str, str]] = []
    client = _CachedLivePositionClient(source)
    snapshot["positions"] = _load_deepcoin_live_position_rows(
        session_factory,
        deepcoin_client_factory=lambda: client,
        group_label_by_chat_id=group_label_by_chat_id,
        contract_spec_provider=contract_spec_provider,
        deepcoin_client=client,
        unattributed_protection_rows=unattributed_protection_orders,
    )
    snapshot["unattributed_protection_orders"] = unattributed_protection_orders
    return snapshot


def _load_exchange_tab_snapshot(
    session_factory,
    *,
    tab_name: str,
    deepcoin_client_factory,
    group_label_by_chat_id: dict[int, str],
    pending_entry_signals: list[dict[str, Any]],
    trading_settings,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    order_limit: int = 20,
    open_order_limit: int = 100,
    known_history_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Load exactly one non-live exchange tab."""
    if tab_name not in {"open-orders", "order-history", "position-history"}:
        raise ValueError("unsupported exchange position tab")

    snapshot = _empty_exchange_snapshot()
    try:
        client = deepcoin_client_factory()
    except Exception:
        logger.exception("Deepcoin exchange tab client creation failed")
        snapshot["error"] = "unavailable"
        return snapshot

    scope = client if hasattr(client, "__enter__") else nullcontext(client)
    with scope:
        return _load_exchange_tab_snapshot_from_client(
            session_factory,
            tab_name=tab_name,
            client=client,
            snapshot=snapshot,
            group_label_by_chat_id=group_label_by_chat_id,
            pending_entry_signals=pending_entry_signals,
            trading_settings=trading_settings,
            contract_spec_provider=contract_spec_provider,
            order_limit=order_limit,
            open_order_limit=open_order_limit,
            known_history_symbols=known_history_symbols or [],
        )


def _load_exchange_tab_snapshot_from_client(
    session_factory,
    *,
    tab_name: str,
    client,
    snapshot: dict[str, Any],
    group_label_by_chat_id: dict[int, str],
    pending_entry_signals: list[dict[str, Any]],
    trading_settings,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
    order_limit: int,
    open_order_limit: int,
    known_history_symbols: list[str],
) -> dict[str, Any]:
    error_state: dict[str, str] = {}
    if tab_name == "open-orders":
        raw_open_orders = _safe_deepcoin_list(
            client,
            "list_open_orders",
            error_state=error_state,
        )
        if error_state:
            snapshot["error"] = "unavailable"
            return snapshot
        instruments = _exchange_snapshot_instrument_ids(
            positions=[],
            open_orders=raw_open_orders,
            order_history=[],
            pending_entry_signals=pending_entry_signals,
            allowed_symbols=getattr(trading_settings, "allowed_symbols", []),
        )
        raw_trigger_orders: list[dict[str, Any]] = []
        for inst_id in sorted(instruments):
            raw_trigger_orders.extend(
                _safe_deepcoin_list(
                    client,
                    "list_trigger_orders_pending",
                    inst_id=inst_id,
                    error_state=error_state,
                )
            )
        if error_state:
            snapshot["error"] = "unavailable"
            return snapshot
        snapshot["open_orders"] = _dedupe_exchange_rows(
            [
                *(
                    _exchange_order_row(order, source="普通委托")
                    for order in raw_open_orders
                ),
                *(
                    _exchange_order_row(order, source="触发委托")
                    for order in raw_trigger_orders
                ),
            ],
            limit=open_order_limit,
        )
        _attach_exchange_order_bindings(
            session_factory,
            snapshot["open_orders"],
            group_label_by_chat_id=group_label_by_chat_id,
        )
        return snapshot

    if tab_name == "order-history":
        raw_order_history = _safe_deepcoin_list(
            client,
            "list_order_history",
            error_state=error_state,
        )
        if error_state:
            snapshot["error"] = "unavailable"
            return snapshot
        instruments = _exchange_snapshot_instrument_ids(
            positions=[],
            open_orders=[],
            order_history=raw_order_history,
            pending_entry_signals=pending_entry_signals,
            allowed_symbols=getattr(trading_settings, "allowed_symbols", []),
        )
        raw_trigger_history: list[dict[str, Any]] = []
        for inst_id in sorted(instruments):
            raw_trigger_history.extend(
                _safe_deepcoin_list(
                    client,
                    "list_trigger_order_history",
                    inst_id=inst_id,
                    error_state=error_state,
                )
            )
        if error_state:
            snapshot["error"] = "unavailable"
            return snapshot
        snapshot["order_history"] = _dedupe_exchange_rows(
            [
                *(
                    _exchange_order_row(order, source="历史委托")
                    for order in raw_order_history
                ),
                *(
                    _exchange_order_row(order, source="触发历史")
                    for order in raw_trigger_history
                ),
            ],
            limit=order_limit,
        )
        _attach_exchange_order_bindings(
            session_factory,
            snapshot["order_history"],
            group_label_by_chat_id=group_label_by_chat_id,
        )
        return snapshot

    instruments = _exchange_snapshot_instrument_ids(
        positions=[],
        open_orders=[],
        order_history=[],
        pending_entry_signals=pending_entry_signals,
        allowed_symbols=getattr(trading_settings, "allowed_symbols", []),
    )
    instruments.update(
        _symbol_to_deepcoin_inst_id(symbol)
        for symbol in known_history_symbols
        if str(symbol or "").strip()
    )
    raw_position_history: list[dict[str, Any]] = []
    for inst_id in sorted(instruments):
        raw_position_history.extend(
            _safe_deepcoin_list(
                client,
                "list_position_history",
                inst_id=inst_id,
                error_state=error_state,
            )
        )
    if error_state:
        snapshot["error"] = "unavailable"
        return snapshot
    snapshot["position_history"] = sorted(
        (
            _exchange_position_history_row(
                position,
                contract_spec_provider=contract_spec_provider,
            )
            for position in raw_position_history
            if _float_or_none(position.get("avgPx")) is not None
            and _float_or_none(position.get("closeAvgPx")) is not None
            and _float_or_none(position.get("closePos")) not in (None, 0.0)
        ),
        key=lambda row: (
            row.get("exited_at") or datetime.min.replace(tzinfo=UTC),
            row["history_sort_id"],
        ),
        reverse=True,
    )[:order_limit]
    _attach_exchange_history_position_bindings(
        session_factory,
        snapshot["position_history"],
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
        item["attribution"] = _order_item_attribution(
            item,
            candidates=[*pending_entry_signals, *holding_positions],
            group_label_by_chat_id=group_label_by_chat_id,
        )
    for item in snapshot.get("order_history", []):
        item["attribution"] = _order_item_attribution(
            item,
            candidates=[*exited_positions, *holding_positions, *pending_entry_signals],
            group_label_by_chat_id=group_label_by_chat_id,
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


def _attach_exchange_history_position_bindings(
    session_factory,
    rows: list[dict[str, Any]],
    *,
    group_label_by_chat_id: dict[int, str],
) -> None:
    """Attach exact local ownership to historical rows by DeepCoin split posId."""
    pos_ids = {str(row.get("pos_id") or "") for row in rows if row.get("pos_id")}
    if not pos_ids:
        return
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.venue == "deepcoin")
            .filter(ExecutionOrderLeg.pos_id.in_(pos_ids))
            .order_by(ExecutionOrderLeg.id.desc())
            .all()
        )
        bindings = {
            int(binding.id): binding
            for binding in session.query(ExecutionBinding)
            .filter(ExecutionBinding.id.in_({int(leg.execution_binding_id) for leg in legs}))
            .all()
        } if legs else {}
        by_pos_id = {str(leg.pos_id): leg for leg in legs if leg.pos_id}
        for row in rows:
            leg = by_pos_id.get(str(row.get("pos_id") or ""))
            if leg is None:
                continue
            binding = bindings.get(int(leg.execution_binding_id))
            attribution = _persisted_position_attribution(
                leg=leg,
                binding=binding,
                group_label_by_chat_id=group_label_by_chat_id,
                allow_terminal_leg=True,
            )
            if attribution is not None:
                row["persisted_attribution"] = attribution


def _order_item_attribution(
    item: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    group_label_by_chat_id: dict[int, str],
) -> dict[str, Any]:
    persisted = item.get("persisted_attribution")
    if isinstance(persisted, dict):
        return persisted
    default_order_role = _infer_exchange_order_role(item)
    if _is_tpsl_exchange_order(item):
        return {
            "state": "conflict",
            "label": "保护归属未验证",
            "chat_id": None,
            "group_name": "未归属",
            "strategy_id": None,
            "strategy_summary": "TPSL 未命中保护 ledger · 自动管理已冻结",
            "source_excerpt": "",
            "score": 0,
            "reasons": ["缺少 position_protection_ledger 强证据"],
            "order_role": default_order_role,
            "automatic_management_frozen": True,
        }
    return _exchange_item_attribution(
        item,
        candidates=candidates,
        group_label_by_chat_id=group_label_by_chat_id,
        default_order_role=default_order_role,
    )


def _persisted_position_attribution(
    *,
    leg: ExecutionOrderLeg | None,
    binding: ExecutionBinding | None,
    group_label_by_chat_id: dict[int, str],
    allow_terminal_leg: bool = False,
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
    equivalent_evidence = (
        evidence.get("evidence_type") == "equivalent_permutation_assignment"
    )
    equivalent_authoritative = (
        not equivalent_evidence or has_authoritative_persisted_position(leg)
    )
    verified = (
        state == "verified"
        and binding is not None
        and (allow_terminal_leg or str(leg.status or "").lower() not in terminal_leg_states)
        and equivalent_authoritative
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
        if evidence.get("evidence_type") == "equivalent_permutation_assignment":
            provenance_label = "等价腿确定性归属"
            reasons = ["已审核等价腿组件，按稳定排序确定腿/仓位映射"]
        else:
            provenance_label = None
            reasons = ["持久化 entry-leg 证据"]
    else:
        label = "归属待确认"
        rendered_state = "conflict"
        strategy_summary = "归属冲突 · 自动管理已冻结"
        provenance_label = None
        reasons = (
            ["等价腿归属证据不完整或已过期"]
            if equivalent_evidence
            else [state]
        )
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
        "provenance_label": provenance_label,
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
    persisted = item.get("persisted_attribution")
    if isinstance(persisted, dict):
        return persisted
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


def _safe_deepcoin_list(
    client,
    method_name: str,
    *,
    inst_id: str | None = None,
    error_state: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
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
            if error_state is not None:
                error_state["error"] = "unavailable"
            return []
    except Exception:
        logger.exception("Deepcoin %s load failed", method_name)
        if error_state is not None:
            error_state["error"] = "unavailable"
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


def _exchange_position_history_row(
    position: dict[str, Any], *, contract_spec_provider: DeepcoinContractSpecProvider | None
) -> dict[str, Any]:
    inst_id = _exchange_inst_id(position)
    symbol = _symbol_from_deepcoin_inst_id(inst_id)
    spec = contract_spec_provider.get_contract_spec(inst_id) if contract_spec_provider else None

    def quantity(field: str) -> str | None:
        contracts = _float_or_none(position.get(field))
        if contracts is None or contracts <= 0:
            return None
        if spec is not None:
            return f"{contracts * spec.contract_value:g} {symbol}"
        return f"{contracts:g} contracts {inst_id}"

    def exchange_time(field: str):
        value = _float_or_none(position.get(field))
        if value is None or value <= 0:
            return None
        return datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(DEFAULT_LOCAL_TIMEZONE)

    return {
        "history_sort_id": f"deepcoin-position:{position.get('posId')}",
        "history_sort_key": (str(position.get("posId") or ""), 0),
        "pos_id": _first_position_string(position, "posId"),
        "inst_id": inst_id,
        "symbol": symbol,
        "side": _normalize_deepcoin_position_side(position.get("posSide")),
        "source": "deepcoin_position_history",
        "history_metric_source": "deepcoin_position_history",
        "lifecycle_status": "exited",
        "exit_reason": "closed",
        "entry_price_actual": _float_or_none(position.get("avgPx")),
        "exit_price_actual": _float_or_none(position.get("closeAvgPx")),
        "realized_pnl": _float_or_none(position.get("pnl")),
        "position_size_text": quantity("pos"),
        "closed_size_text": quantity("closePos"),
        "entered_at": exchange_time("cTime"),
        "exited_at": exchange_time("uTime"),
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
        ledger_rows = (
            session.query(PositionProtectionLedger, ExecutionBinding, ExecutionOrderLeg)
            .join(
                ExecutionBinding,
                ExecutionBinding.id == PositionProtectionLedger.execution_binding_id,
            )
            .join(
                ExecutionOrderLeg,
                ExecutionOrderLeg.id == PositionProtectionLedger.execution_order_leg_id,
            )
            .filter(PositionProtectionLedger.venue == "deepcoin")
            .filter(PositionProtectionLedger.status == "verified")
            .filter(PositionProtectionLedger.order_id.in_(wanted_ids))
            .all()
        )
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .order_by(ExecutionBinding.updated_at.desc(), ExecutionBinding.id.desc())
            .all()
        )
        leg_rows = (
            session.query(ExecutionOrderLeg, ExecutionBinding)
            .join(
                ExecutionBinding,
                ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id,
            )
            .filter(ExecutionOrderLeg.venue == "deepcoin")
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.attribution_status == "verified")
            .filter(ExecutionOrderLeg.pos_id.in_(wanted_ids))
            .all()
        )
    ledger_by_order_id: dict[str, tuple[PositionProtectionLedger, ExecutionBinding, ExecutionOrderLeg]] = {}
    for ledger, binding, leg in ledger_rows:
        order_id = str(ledger.order_id or "")
        if order_id and order_id not in ledger_by_order_id:
            ledger_by_order_id[order_id] = (ledger, binding, leg)
    bindings_by_order_id: dict[str, ExecutionBinding] = {}
    for binding in bindings:
        binding_ids = [
            *_split_exchange_binding_ids(binding.order_id),
            *_split_exchange_binding_ids(binding.client_order_id),
        ]
        for binding_id in binding_ids:
            if binding_id in wanted_ids and binding_id not in bindings_by_order_id:
                bindings_by_order_id[binding_id] = binding
    legs_by_pos_id: dict[str, tuple[ExecutionOrderLeg, ExecutionBinding] | None] = {}
    for leg, binding in leg_rows:
        pos_id = str(leg.pos_id or "")
        if not pos_id:
            continue
        attribution = _persisted_position_attribution(
            leg=leg,
            binding=binding,
            group_label_by_chat_id=group_label_by_chat_id,
        )
        if not attribution or attribution.get("state") != "bound":
            continue
        if pos_id in legs_by_pos_id:
            legs_by_pos_id[pos_id] = None
        else:
            legs_by_pos_id[pos_id] = (leg, binding)
    for row in rows:
        ledger_match = ledger_by_order_id.get(str(row.get("order_id") or ""))
        if ledger_match is not None:
            ledger, binding, leg = ledger_match
            row["chat_id"] = binding.chat_id
            row["message_id"] = binding.message_id
            row["group_label"] = group_label_by_chat_id.get(binding.chat_id, str(binding.chat_id))
            row["symbol"] = binding.symbol or row.get("symbol")
            row["side"] = binding.side or row.get("side")
            row["execution_status"] = binding.status
            row["persisted_attribution"] = _protection_ledger_attribution(
                ledger=ledger,
                binding=binding,
                leg=leg,
                group_label_by_chat_id=group_label_by_chat_id,
                default_order_role=_infer_exchange_order_role(row),
            )
            continue
        binding = bindings_by_order_id.get(str(row.get("order_id") or ""))
        if binding is None:
            binding = bindings_by_order_id.get(str(row.get("client_order_id") or ""))
        if binding is None:
            leg_match = None
            if not _is_tpsl_exchange_order(row):
                leg_match = legs_by_pos_id.get(str(row.get("order_id") or ""))
                if leg_match is None:
                    leg_match = legs_by_pos_id.get(str(row.get("client_order_id") or ""))
            if leg_match is None:
                continue
            leg, binding = leg_match
            row["chat_id"] = binding.chat_id
            row["message_id"] = binding.message_id
            row["group_label"] = group_label_by_chat_id.get(binding.chat_id, str(binding.chat_id))
            row["symbol"] = binding.symbol or row.get("symbol")
            row["side"] = binding.side or row.get("side")
            row["execution_status"] = binding.status
            row["persisted_attribution"] = _persisted_position_attribution(
                leg=leg,
                binding=binding,
                group_label_by_chat_id=group_label_by_chat_id,
            )
            continue
        row["chat_id"] = binding.chat_id
        row["message_id"] = binding.message_id
        row["group_label"] = group_label_by_chat_id.get(binding.chat_id, str(binding.chat_id))
        row["symbol"] = binding.symbol or row.get("symbol")
        row["side"] = binding.side or row.get("side")
        row["execution_status"] = binding.status


def _protection_ledger_attribution(
    *,
    ledger: PositionProtectionLedger,
    binding: ExecutionBinding,
    leg: ExecutionOrderLeg,
    group_label_by_chat_id: dict[int, str],
    default_order_role: str | None,
) -> dict[str, Any]:
    chat_id = binding.chat_id
    group_name = group_label_by_chat_id.get(chat_id, str(chat_id))
    purpose_label = {
        "take_profit": "止盈保护",
        "stop_loss": "止损保护",
        "combined": "止盈止损保护",
    }.get(str(ledger.purpose or ""), "TPSL 保护")
    last_verified_at = ledger.last_verified_at or ledger.last_seen_at
    if last_verified_at is not None:
        if last_verified_at.tzinfo is None:
            last_verified_at = last_verified_at.replace(tzinfo=UTC)
        last_verified_display = last_verified_at.astimezone(
            DEFAULT_LOCAL_TIMEZONE
        ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        last_verified_display = None
    return {
        "state": "bound",
        "label": "已验证保护",
        "chat_id": chat_id,
        "group_name": group_name,
        "strategy_id": binding.strategy_instance_id,
        "strategy_summary": " · ".join(
            part
            for part in (
                binding.strategy_instance_id,
                f"{binding.symbol} {binding.side}",
                purpose_label,
            )
            if part
        ),
        "source_excerpt": "",
        "score": 100,
        "reasons": ["保护 ledger 已验证"],
        "order_role": default_order_role or purpose_label,
        "evidence_type": ledger.evidence_source,
        "pos_id": ledger.pos_id or leg.pos_id,
        "last_verified_at": last_verified_display,
        "ownership_state": "verified",
        "automatic_management_frozen": False,
    }


def _is_tpsl_exchange_order(item: dict[str, Any]) -> bool:
    return str(item.get("order_type") or "").upper() == "TPSL"


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


def _exchange_protection_display_rows(
    *,
    position: dict[str, Any],
    pending_orders: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return every exchange TPSL side relevant to a live-position card.

    This is intentionally a display-only view.  It must not be used to infer
    strategy ownership or relax the conservative protection-mutation checks.
    """

    position_id = _first_position_string(position, "posId", "pos_id", "id")
    instrument_id = str(position.get("instId") or "").upper()
    side = _normalize_deepcoin_position_side(
        position.get("posSide") or position.get("side")
    )
    display_rows: list[dict[str, str]] = []
    for order in pending_orders:
        if str(order.get("triggerOrderType") or "").upper() != "TPSL":
            continue
        order_position_id = _first_position_string(
            order,
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        )
        if order_position_id:
            if order_position_id != position_id:
                continue
            ownership_state = "已验证归属"
        elif str(order.get("instId") or "").upper() != instrument_id:
            continue
        else:
            explicit_position_side = _position_text_value(order.get("posSide"))
            if (
                explicit_position_side is not None
                and _normalize_deepcoin_position_side(explicit_position_side) != side
            ):
                continue
            # A TPSL's plain `side` is its closing-order direction, not reliable
            # position-side evidence. Without `posSide`, show it as a candidate
            # instead of either dropping it or claiming a verified association.
            ownership_state = "无法归属"

        for kind, keys in (
            ("take_profit", ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")),
            ("stop_loss", ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")),
        ):
            trigger_price = next(
                (
                    order.get(key)
                    for key in keys
                    if _is_nonzero_price(order.get(key))
                ),
                None,
            )
            trigger_price_text = _position_text_value(trigger_price)
            if trigger_price_text is None:
                continue
            display_rows.append(
                {
                    "kind": kind,
                    "trigger_price_text": trigger_price_text,
                    "size_text": _position_text_value(
                        order.get("sz") or order.get("size")
                    )
                    or "0",
                    "order_id": _first_position_string(
                        order,
                        "ordId",
                        "orderId",
                        "order_id",
                        "algoId",
                        "triggerOrderId",
                        "id",
                    )
                    or "-",
                    "ownership_state": ownership_state,
                }
            )
    state_sort = {"已验证归属": 0, "无法归属": 1}
    kind_sort = {"take_profit": 0, "stop_loss": 1}
    return sorted(
        display_rows,
        key=lambda row: (
            state_sort[row["ownership_state"]],
            kind_sort[row["kind"]],
            _float_or_none(row["trigger_price_text"]) or float("inf"),
            row["order_id"],
        ),
    )


def _register_exact_order_position_id(
    exact_order_position_ids: dict[str, str],
    conflicting_order_ids: set[str],
    order_id: str,
    pos_id: str,
) -> None:
    """Record one exact order owner, failing closed on conflicting local evidence."""

    if order_id in conflicting_order_ids:
        return
    existing_pos_id = exact_order_position_ids.get(order_id)
    if existing_pos_id is not None and existing_pos_id != pos_id:
        exact_order_position_ids.pop(order_id, None)
        conflicting_order_ids.add(order_id)
        return
    exact_order_position_ids[order_id] = pos_id


def _split_exchange_protection_display_rows(
    *,
    positions: list[dict[str, Any]],
    pending_orders: list[dict[str, Any]],
    exact_order_position_ids: dict[str, str] | None = None,
    account_ownership=None,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """Separate exact position TPSL rows from exchange rows without an owner."""
    from telegram_kol_research.position_tpsl_display import build_position_tpsl_display

    display = build_position_tpsl_display(
        positions=positions,
        pending_orders=pending_orders,
        exact_order_position_ids=exact_order_position_ids or {},
        account_ownership=account_ownership,
        contract_spec_provider=contract_spec_provider,
    )
    return (
        {
            pos_id: [row.as_dict() for row in rows]
            for pos_id, rows in display.by_pos_id.items()
        },
        [row.as_dict() for row in display.unattributed],
    )


def _summarize_verified_exchange_protection_rows(
    rows: list[dict[str, str]], *, side: str | None
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Project exact-position verified TPSL rows into the compact summary."""

    prices_by_kind = {"stop_loss": [], "take_profit": []}
    for row in rows:
        if str(row.get("ownership_state") or "") != "已验证归属":
            continue
        kind = str(row.get("kind") or "")
        price = _position_text_value(row.get("trigger_price_text"))
        if kind not in prices_by_kind or price is None:
            continue
        try:
            float(price)
        except ValueError:
            continue
        if price not in prices_by_kind[kind]:
            prices_by_kind[kind].append(price)

    is_long = str(side or "").lower() == "long"
    ordered_stops = sorted(prices_by_kind["stop_loss"], key=float, reverse=is_long)
    ordered_take_profits = sorted(
        prices_by_kind["take_profit"], key=float, reverse=not is_long
    )
    return (
        ordered_stops[0] if ordered_stops else None,
        ordered_stops[1] if len(ordered_stops) > 1 else None,
        tuple(ordered_take_profits),
    )


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
    entry_thresholds_for_symbol: Callable[[str], SymbolEntryThresholds],
    capability_snapshot: Any | None = None,
    now: datetime | None = None,
    exchange_symbols_verified: bool = True,
    contract_spec_provider: Any | None = None,
    execution_mode: str | None = None,
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
            "entry_thresholds": entry_thresholds_for_symbol(symbol).to_dict(),
            **_trading_symbol_capability_fields(
                symbol,
                selected=symbol in selected,
                snapshot=capability_snapshot,
                now=now,
                exchange_supported=exchange_symbols_verified,
                exchange_listing_available=exchange_symbols_verified,
                contract_spec_provider=contract_spec_provider,
                execution_mode=execution_mode,
            ),
        }
    for symbol in selected:
        if symbol not in rows_by_symbol:
            rows_by_symbol[symbol] = {
                "symbol": symbol,
                "instrument_id": _to_deepcoin_swap_instrument(symbol),
                "selected": True,
                "max_loss_usdt": symbol_max_loss_usdt.get(symbol),
                "entry_thresholds": entry_thresholds_for_symbol(symbol).to_dict(),
                **_trading_symbol_capability_fields(
                    symbol,
                    selected=True,
                    snapshot=capability_snapshot,
                    now=now,
                    exchange_supported=False,
                    exchange_listing_available=exchange_symbols_verified,
                    contract_spec_provider=contract_spec_provider,
                    execution_mode=execution_mode,
                ),
            }
    return sorted(rows_by_symbol.values(), key=lambda item: item["symbol"])


def _trading_symbol_capability_fields(
    symbol: str,
    *,
    selected: bool,
    snapshot: Any | None,
    now: datetime | None,
    exchange_supported: bool,
    exchange_listing_available: bool,
    contract_spec_provider: Any | None,
    execution_mode: str | None,
) -> dict[str, Any]:
    fetched_at = getattr(snapshot, "fetched_at", None)
    expires_at = getattr(snapshot, "expires_at", None)
    capabilities = getattr(snapshot, "capabilities_by_instrument_id", None)
    capability = (
        capabilities.get(_to_deepcoin_swap_instrument(symbol))
        if capabilities is not None
        else None
    )
    venue_supported = capability is not None or (
        snapshot is None and exchange_supported
    )
    venue_state = getattr(capability, "state", None)
    reason_code = "contract_spec_sync_unavailable"
    spec_status = "sync_unavailable"
    tradable = False

    if snapshot is None and exchange_listing_available and not exchange_supported:
        reason_code = "venue_instrument_unsupported"
        spec_status = "missing"
    elif snapshot is not None and not _valid_capability_time(fetched_at, expires_at, now):
        reason_code = "contract_spec_invalid"
        spec_status = "invalid"
    elif snapshot is not None and _utc_datetime(now) >= _utc_datetime(expires_at):
        reason_code = "contract_spec_stale"
        spec_status = "stale"
    elif snapshot is not None and capability is None:
        reason_code = "venue_instrument_unsupported"
        spec_status = "missing"
    elif capability is not None and venue_state != "live":
        reason_code = "venue_instrument_not_live"
        spec_status = "fresh"
    elif capability is not None:
        spec_status = "fresh"
        if selected:
            reason_code = "tradable"
            tradable = True
        else:
            reason_code = "global_not_allowed"

    dynamic_fields = {
        "venue_supported": venue_supported,
        "venue_state": venue_state,
        "spec_status": spec_status,
        "tradable": tradable,
        "reason_code": reason_code,
        "fetched_at": _format_capability_datetime(fetched_at),
        "expires_at": _format_capability_datetime(expires_at),
    }
    return {
        **dynamic_fields,
        **_trading_symbol_execution_fields(
            symbol,
            selected=selected,
            provider=contract_spec_provider,
            execution_mode=execution_mode,
            dynamic_fields=dynamic_fields,
        ),
    }


def _trading_symbol_execution_fields(
    symbol: str,
    *,
    selected: bool,
    provider: Any | None,
    execution_mode: str | None,
    dynamic_fields: dict[str, Any],
) -> dict[str, Any]:
    mode = execution_mode
    if mode not in {"static", "shadow", "live"}:
        mode = (
            "live"
            if callable(getattr(provider, "lookup_contract_spec", None))
            else "static"
            if provider is not None
            else "unavailable"
        )
    if mode == "live" or mode == "unavailable":
        return {
            "execution_mode": mode,
            "execution_tradable": bool(dynamic_fields["tradable"]),
            "execution_reason_code": str(dynamic_fields["reason_code"]),
        }

    effective_provider = getattr(provider, "static_provider", provider)
    instrument_id = _to_deepcoin_swap_instrument(symbol)
    try:
        contract_spec = effective_provider.get_contract_spec(instrument_id)
    except Exception:
        contract_spec = None
    spec_matches = (
        contract_spec is not None
        and str(getattr(contract_spec, "instrument_id", "")).upper() == instrument_id
    )
    if not spec_matches:
        execution_reason = "contract_spec_missing"
    elif not selected:
        execution_reason = "global_not_allowed"
    else:
        execution_reason = "tradable"
    return {
        "execution_mode": mode,
        "execution_tradable": execution_reason == "tradable",
        "execution_reason_code": execution_reason,
    }


def _valid_capability_time(
    fetched_at: Any,
    expires_at: Any,
    now: datetime | None,
) -> bool:
    values = (fetched_at, expires_at, now)
    if not all(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
        for value in values
    ):
        return False
    return (
        _utc_datetime(fetched_at) < _utc_datetime(expires_at)
        and _utc_datetime(fetched_at) <= _utc_datetime(now)
    )


def _utc_datetime(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capability time must be timezone-aware")
    return value.astimezone(UTC)


def _format_capability_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.utcoffset() is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bounded_contract_spec_status(status: Any) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    bounded = dict(status)
    error = bounded.get("last_error")
    if error is not None:
        bounded["last_error"] = str(error)[:240]
    return bounded


def _refreshable_contract_spec_provider(provider: Any) -> Any | None:
    """Find the authoritative refresh target without changing rollout authority."""

    candidate = provider
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        if (
            callable(getattr(candidate, "refresh", None))
            and isinstance(getattr(candidate, "ttl", None), timedelta)
        ):
            return candidate
        candidate = getattr(candidate, "authoritative_provider", None)
    return None


def _contract_spec_exchange_symbols(provider: Any) -> list[dict[str, str]] | None:
    snapshot = getattr(provider, "snapshot", None)
    capabilities = getattr(snapshot, "capabilities_by_instrument_id", None)
    if capabilities is None:
        return None
    return [
        {
            "symbol": instrument_id.removesuffix("-USDT-SWAP"),
            "instrument_id": instrument_id,
        }
        for instrument_id in sorted(capabilities)
        if instrument_id.endswith("-USDT-SWAP")
    ]


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
        deepcoin_client = (
            None
            if disabled_management_message_needs_no_client(
                app.state.session_factory,
                raw_message_id=raw_message_id,
            )
            else app.state.deepcoin_client_factory()
        )
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
    with app.state.session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        chat_id = raw_message.chat_id if raw_message is not None else None
    settings = load_trading_settings(app.state.session_factory)
    context_enabled = (
        chat_id is not None
        and settings.context_resolution_enabled_for_chat(int(chat_id))
    )
    return process_authoritative_message(
        app.state.session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=ai_config,
        media_root=app.state.media_root,
        auto_trade_executor=app.state.auto_trade_executor,
        context_resolver=resolve_contextual_strategy if context_enabled else None,
        exchange_state_provider=(
            lambda message_id, candidate_thread_ids=None: build_redacted_exchange_state(
                app.state.session_factory,
                message_id,
                candidate_thread_ids=candidate_thread_ids,
            )
        ) if context_enabled else None,
    )


def _extract_reply_evidence(app: FastAPI, *, raw_message_id: int):
    """Persist MiMo evidence for a recovered quote without executing it."""

    with app.state.session_factory() as session:
        raw = session.get(RawMessage, int(raw_message_id))
        if raw is None:
            raise LookupError("reply raw message not found")
        if not load_trading_settings(
            app.state.session_factory
        ).context_resolution_enabled_for_chat(int(raw.chat_id)):
            return None
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        fingerprint = build_message_input_fingerprint(
            raw,
            media_assets,
            media_root=app.state.media_root,
        )
        current_evidence = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
                MessageEvidenceVersion.input_fingerprint == fingerprint,
                MessageEvidenceVersion.extraction_status == "completed",
            )
            .first()
        )
        if current_evidence is not None:
            session.expunge(current_evidence)
            return current_evidence
    return assess_message_authoritatively(
        app.state.session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=load_ai_recognition_config(
            app.state.ai_recognition_config_path
        ),
        media_root=app.state.media_root,
        context_resolver=None,
    )


def _schedule_context_resolution_for_app(app: FastAPI, **event: Any) -> int:
    settings = load_trading_settings(app.state.session_factory)
    chat_id = event.get("chat_id")
    if chat_id is None and event.get("raw_message_id") is not None:
        with app.state.session_factory() as session:
            raw = session.get(RawMessage, int(event["raw_message_id"]))
            chat_id = raw.chat_id if raw is not None else None
    if chat_id is None or not settings.context_resolution_enabled_for_chat(int(chat_id)):
        return 0
    return schedule_context_reanalysis(app.state.session_factory, **event)


def _run_context_resolution_worker_for_app(app: FastAPI) -> dict[str, Any]:
    settings = load_trading_settings(app.state.session_factory)
    if not settings.context_resolution_enabled or not settings.live_management_execution_enabled:
        if settings.entry_revision_v2_mode != "disabled":
            revision_result = run_entry_revision_worker_once(
                app.state.session_factory,
                deepcoin_client=app.state.deepcoin_client_factory(),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                management_executor=app.state.entry_revision_risk_reduction_executor,
            )
            return {
                "status": "completed",
                "context_resolution": {"status": "disabled"},
                "entry_revision": revision_result,
            }
        return {"status": "disabled"}

    def reanalyze(raw_message_id: int, _fingerprint: str) -> dict[str, Any]:
        ai_config = load_ai_recognition_config(app.state.ai_recognition_config_path)
        result = process_authoritative_message(
            app.state.session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=ai_config,
            media_root=app.state.media_root,
            auto_trade_executor=app.state.auto_trade_executor,
            context_resolver=resolve_contextual_strategy,
            exchange_state_provider=lambda message_id, candidate_thread_ids=None: build_redacted_exchange_state(
                app.state.session_factory,
                message_id,
                candidate_thread_ids=candidate_thread_ids,
            ),
            reuse_current_evidence=True,
        )
        if result.assessment.agreement_status == "authoritative_failed":
            raise RuntimeError(
                result.assessment.mimo.error_message
                or "context reanalysis failed"
            )
        return {
            "status": str(result.automation.get("status") or "completed"),
        }

    def is_eligible(raw_message_id: int) -> bool:
        with app.state.session_factory() as session:
            raw = session.get(RawMessage, int(raw_message_id))
            chat_id = raw.chat_id if raw is not None else None
        current_settings = load_trading_settings(app.state.session_factory)
        return (
            chat_id is not None
            and current_settings.context_resolution_enabled_for_chat(int(chat_id))
        )

    def notify_final_failure(payload: dict[str, Any]) -> None:
        config = app.state.system_operator_bot_config
        if not isinstance(config, SystemOperatorBotConfig):
            logger.error(
                "context resolution retries exhausted raw_message_id=%s",
                payload.get("raw_message_id"),
            )
            return
        asyncio.run(
            send_system_operator_bot_message(
                config=config,
                text=(
                    "上下文二次判断重试已耗尽；未自动执行。\n"
                    f"raw_message_id={payload.get('raw_message_id')}"
                ),
            )
        )

    context_result = run_context_resolution_once(
        app.state.session_factory,
        context_fingerprint_factory=lambda raw_message_id: (
            build_context_state_fingerprint(
                app.state.session_factory,
                raw_message_id,
            )
        ),
        reanalyze=reanalyze,
        notify_final_failure=notify_final_failure,
        is_eligible=is_eligible,
    )
    if settings.entry_revision_v2_mode == "disabled":
        return context_result
    revision_result = run_entry_revision_worker_once(
        app.state.session_factory,
        deepcoin_client=app.state.deepcoin_client_factory(),
        contract_spec_provider=app.state.deepcoin_contract_spec_provider,
        management_executor=app.state.entry_revision_risk_reduction_executor,
    )
    return {
        "status": "completed",
        "context_resolution": context_result,
        "entry_revision": revision_result,
    }


async def _run_message_operation_supervisor_loop(app: FastAPI) -> None:
    """Run the deterministic supervisor outside every message critical path."""

    cursor = app.state.message_operation_supervisor_config.after_raw_message_id
    while True:
        observed_at = app.state.now_provider()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        else:
            observed_at = observed_at.astimezone(UTC)
        try:
            result = await asyncio.to_thread(
                app.state.message_operation_supervisor_runner,
                app.state.session_factory,
                after_raw_message_id=cursor,
                capture_after_raw_message_id=(
                    app.state.message_operation_supervisor_config.after_raw_message_id
                ),
                limit=(
                    app.state.message_operation_supervisor_config.batch_limit
                ),
                observed_at=observed_at,
                runtime_incident_config=app.state.runtime_incident_config,
            )
            if not isinstance(result, dict):
                raise RuntimeError("message operation supervisor result invalid")
            required_counts = {
                key: result.get(key)
                for key in (
                    "errors",
                    "outcome_errors",
                    "model_calls",
                    "outcome_model_calls",
                    "capture_errors",
                )
            }
            if any(
                type(value) is not int or value < 0
                for value in required_counts.values()
            ):
                raise RuntimeError("message operation supervisor result invalid")
            errors = (
                required_counts["errors"]
                + required_counts["outcome_errors"]
                + required_counts["capture_errors"]
            )
            model_calls = (
                required_counts["model_calls"]
                + required_counts["outcome_model_calls"]
            )
            if errors or model_calls:
                raise RuntimeError("message operation supervisor cycle failed")
            next_cursor = result.get("last_scanned_raw_message_id")
            if type(next_cursor) is not int or next_cursor < cursor:
                raise RuntimeError("message operation supervisor cursor invalid")
            cursor = next_cursor
            app.state.message_operation_supervisor_cursor = cursor
            app.state.message_operation_supervisor_last_success_at = observed_at
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("message operation supervisor cycle failed")
        await asyncio.sleep(
            app.state.message_operation_supervisor_interval_seconds
        )


def _message_operation_supervisor_watermark_is_valid(app: FastAPI) -> bool:
    config = app.state.message_operation_supervisor_config
    if not config.enabled:
        return True
    if (
        not config.shadow_only
        or type(config.after_raw_message_id) is not int
        or not 0 <= config.after_raw_message_id < 2**63 - 1
    ):
        return False
    try:
        with app.state.session_factory() as session:
            latest_raw_id = session.execute(
                select(func.max(RawMessage.id))
            ).scalar_one()
        return config.after_raw_message_id <= int(latest_raw_id or 0)
    except Exception:
        logger.exception("message operation supervisor watermark validation failed")
        return False


def _trading_settings_response(session_factory) -> dict[str, Any]:
    payload = load_trading_settings(session_factory).to_dict()
    payload["mimo_contract_circuit"] = asdict(
        load_mimo_contract_circuit(session_factory)
    )
    return payload


def _validate_mimo_contract_activation(
    session_factory,
    *,
    payload: dict[str, Any],
) -> None:
    current = load_trading_settings(session_factory)
    candidate = trading_settings_from_payload({**current.to_dict(), **payload})
    if (
        candidate.mimo_contract_mode == "v1"
        and candidate.mimo_v2_activation_after_raw_message_id
        != current.mimo_v2_activation_after_raw_message_id
    ):
        raise ValueError("mimo v1 rollback must preserve the activation watermark")
    if candidate.mimo_contract_mode != "v2_live_adapter":
        return
    is_activation = current.mimo_contract_mode != "v2_live_adapter"
    watermark_changed = (
        candidate.mimo_v2_activation_after_raw_message_id
        != current.mimo_v2_activation_after_raw_message_id
    )
    if not is_activation and not watermark_changed:
        return
    with session_factory() as session:
        latest_raw_id = session.execute(select(func.max(RawMessage.id))).scalar_one()
    safe_minimum = int(latest_raw_id or 0)
    watermark = candidate.mimo_v2_activation_after_raw_message_id
    if watermark <= 0 or watermark < safe_minimum:
        raise ValueError(
            "mimo v2 requires an explicit future-message watermark at or above "
            f"the current maximum raw message ID ({safe_minimum})"
        )


def _require_expected_mimo_contract_state(
    current,
    *,
    payload: dict[str, Any],
) -> None:
    if (
        "mimo_contract_expected_mode" not in payload
        or "mimo_contract_expected_watermark" not in payload
    ):
        raise ValueError("mimo contract changes require the expected current state")
    expected_mode = str(payload["mimo_contract_expected_mode"])
    try:
        expected_watermark = int(payload["mimo_contract_expected_watermark"])
    except (TypeError, ValueError) as exc:
        raise ValueError("mimo contract expected watermark must be an integer") from exc
    if (
        expected_mode != current.mimo_contract_mode
        or expected_watermark
        != current.mimo_v2_activation_after_raw_message_id
    ):
        raise HTTPException(
            status_code=409,
            detail="mimo contract settings changed; reload before saving",
        )


def create_web_app(
    database_path: str | Path,
    media_root: str | Path | None = None,
    live_target_titles: set[str] | None = None,
    live_listener_runner=None,
    source_deletion_recorder=None,
    telegram_client: Any | None = None,
    live_listener_status_reason: str | None = None,
    group_labels_by_title: dict[str, str] | None = None,
    now_provider=None,
    reconcile_runner=None,
    reconcile_interval_seconds: int = 300,
    reconcile_startup_delay_seconds: int | None = None,
    authoritative_gap_recovery_runner=None,
    authoritative_gap_recovery_interval_seconds: float = 20.0,
    group_config: GroupConfig | None = None,
    group_config_path: str | Path | None = None,
    recovery_runner=None,
    recovery_market_data_factory=None,
    deepcoin_contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    contract_spec_refresh_timeout_seconds: float = 20.0,
    deepcoin_client_factory=None,
    deepcoin_reconcile_runner=None,
    deepcoin_reconcile_interval_seconds: int = 30,
    deepcoin_reconcile_startup_delay_seconds: int = 5,
    ai_recognition_config_path: str | Path | None = None,
    semantic_review_runner=None,
    semantic_review_restart_delay_seconds: float = 1.0,
    strategy_management_worker_runner=None,
    entry_revision_risk_reduction_executor=None,
    strategy_management_worker_interval_seconds: float = 5.0,
    strategy_management_worker_startup_delay_seconds: float = 5.0,
    strategy_management_worker_max_batches: int = 10,
    break_even_convergence_worker_runner=None,
    break_even_convergence_worker_interval_seconds: float = 2.0,
    break_even_convergence_worker_startup_delay_seconds: float = 5.0,
    source_message_deletion_worker_runner=None,
    source_message_deletion_worker_interval_seconds: float = 5.0,
    source_message_deletion_worker_max_jobs: int = 10,
    message_processing_worker_runner=None,
    message_processing_worker_interval_seconds: float = 0.5,
    runtime_agent_production_audit_runner=None,
    runtime_agent_telegram_evidence_runner=None,
    runtime_incident_config: RuntimeIncidentConfig | None = None,
    runtime_incident_config_loader=None,
    message_operation_supervisor_config: (
        MessageOperationSupervisorConfig | None
    ) = None,
    message_operation_supervisor_runner=None,
    message_operation_supervisor_interval_seconds: float = 30.0,
    live_position_snapshot_path: str | Path | None = None,
    position_snapshot_now_provider=None,
    position_snapshot_refresh_seconds: float = 5.0,
    position_snapshot_stale_seconds: float = 30.0,
) -> FastAPI:
    """Create the minimal FastAPI app used by the web command."""

    resolved_database_path = Path(database_path)
    log_directory = resolved_database_path.parent / "logs"
    configure_application_logging(log_directory)
    resolved_media_root = Path(media_root) if media_root is not None else resolved_database_path.parent / "media"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            app.state.web_event_loop = asyncio.get_running_loop()
            if app.state.loop_lag_monitor_task is None:
                app.state.loop_lag_monitor_task = asyncio.create_task(
                    app.state.loop_lag_monitor.run()
                )
                app.state.loop_lag_monitor_task.add_done_callback(
                    _log_background_task_result("loop_lag_monitor_task")
                )
            if (
                app.state.contract_spec_refresh_orchestrator is not None
                and app.state.contract_spec_refresh_task is None
            ):
                app.state.contract_spec_refresh_task = asyncio.create_task(
                    app.state.contract_spec_refresh_orchestrator.run()
                )
                app.state.contract_spec_refresh_task.add_done_callback(
                    _log_background_task_result("contract_spec_refresh_task")
                )
            if (
                app.state.message_operation_supervisor_config.enabled
                and app.state.message_operation_supervisor_config.shadow_only
                and app.state.message_operation_supervisor_policy_status == "valid"
                and app.state.message_operation_supervisor_watermark_valid
                and app.state.message_operation_supervisor_task is None
            ):
                app.state.message_operation_supervisor_task = asyncio.create_task(
                    _run_message_operation_supervisor_loop(app)
                )
                app.state.message_operation_supervisor_task.add_done_callback(
                    _log_background_task_result(
                        "message_operation_supervisor_task"
                    )
                )
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
                context_resolution_scheduler=app.state.context_resolution_scheduler,
                context_resolution_worker=app.state.context_resolution_worker,
            )
            app.state.lifecycle_monitor_task = asyncio.create_task(
                app.state.lifecycle_monitor.run_loop()
            )
            if app.state.authoritative_gap_recovery_loop_task is None:
                app.state.authoritative_gap_recovery_loop_task = asyncio.create_task(
                    app.state.authoritative_gap_recovery_runner(
                        session_factory=app.state.session_factory,
                        authoritative_processor=app.state.authoritative_processor,
                        chat_titles_by_id_provider=(
                            lambda: _group_label_by_chat_id(app.state.group_config)
                        ),
                        interval_seconds=(
                            app.state.authoritative_gap_recovery_interval_seconds
                        ),
                        operation_lock=app.state.message_lock_provider,
                        system_operator_bot_config=(
                            app.state.system_operator_bot_config
                        ),
                        notification_bot_config=app.state.notification_bot_config,
                        loop_lag_snapshot_provider=(
                            app.state.loop_lag_monitor.snapshot
                        ),
                    )
                )
                app.state.authoritative_gap_recovery_loop_task.add_done_callback(
                    _log_background_task_result(
                        "authoritative_gap_recovery_loop_task"
                    )
                )
            app.state.deepcoin_reconcile_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.deepcoin_reconcile_runner,
                    startup_delay_seconds=app.state.deepcoin_reconcile_startup_delay_seconds,
                    session_factory=app.state.session_factory,
                    deepcoin_client_factory=app.state.deepcoin_client_factory,
                    interval_seconds=app.state.deepcoin_reconcile_interval_seconds,
                    now_provider=app.state.now_provider,
                    system_operator_bot_config=app.state.notification_bot_config,
                    terminal_entry_cleanup_bot_config=(
                        app.state.system_operator_bot_config
                    ),
                    contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                )
            )
            app.state.strategy_management_worker_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.strategy_management_worker_runner,
                    startup_delay_seconds=app.state.strategy_management_worker_startup_delay_seconds,
                    session_factory=app.state.session_factory,
                    deepcoin_client_factory=app.state.deepcoin_client_factory,
                    interval_seconds=app.state.strategy_management_worker_interval_seconds,
                    max_batches=app.state.strategy_management_worker_max_batches,
                    now_provider=app.state.now_provider,
                    contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                )
            )
            app.state.strategy_management_worker_task.add_done_callback(
                _log_background_task_result("strategy_management_worker_task")
            )
            app.state.break_even_convergence_worker_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.break_even_convergence_worker_runner,
                    startup_delay_seconds=(
                        app.state.break_even_convergence_worker_startup_delay_seconds
                    ),
                    session_factory=app.state.session_factory,
                    deepcoin_client_factory=app.state.deepcoin_client_factory,
                    interval_seconds=(
                        app.state.break_even_convergence_worker_interval_seconds
                    ),
                    now_provider=app.state.now_provider,
                )
            )
            app.state.break_even_convergence_worker_task.add_done_callback(
                _log_background_task_result(
                    "break_even_convergence_worker_task"
                )
            )
            app.state.source_message_deletion_worker_task = asyncio.create_task(
                app.state.source_message_deletion_worker_runner(
                    session_factory=app.state.session_factory,
                    deepcoin_client_factory=app.state.deepcoin_client_factory,
                    contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                    interval_seconds=(
                        app.state.source_message_deletion_worker_interval_seconds
                    ),
                    max_jobs=app.state.source_message_deletion_worker_max_jobs,
                    now_provider=app.state.now_provider,
                )
            )
            app.state.source_message_deletion_worker_task.add_done_callback(
                _log_background_task_result(
                    "source_message_deletion_worker_task"
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
            if isinstance(app.state.notification_bot_config, SystemOperatorBotConfig):
                app.state.strategy_management_notification_task = asyncio.create_task(
                    run_strategy_management_notification_loop(
                        session_factory=app.state.session_factory,
                        config=app.state.notification_bot_config,
                        group_labels=_group_label_by_chat_id(app.state.group_config),
                    )
                )
                app.state.strategy_management_notification_task.add_done_callback(
                    _log_background_task_result("strategy_management_notification_task")
                )
            if isinstance(
                app.state.system_operator_bot_config,
                SystemOperatorBotConfig,
            ):
                if (
                    app.state.runtime_incident_config
                    .telegram_notifications_enabled
                    or app.state.runtime_incident_config
                    .message_operation_stage1_enabled
                ):
                    app.state.runtime_incident_notification_task = (
                        asyncio.create_task(
                            run_runtime_incident_notification_loop(
                                session_factory=app.state.session_factory,
                                config=app.state.system_operator_bot_config,
                                runtime_config=app.state.runtime_incident_config,
                            )
                        )
                    )
                    app.state.runtime_incident_notification_task.add_done_callback(
                        _log_background_task_result(
                            "runtime_incident_notification_task"
                        )
                    )
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
            await ensure_message_processing_worker_mode()
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
                    reply_evidence_processor=app.state.reply_evidence_processor,
                    context_resolution_scheduler=app.state.context_resolution_scheduler,
                    context_resolution_worker=app.state.context_resolution_worker,
                    system_operator_bot_config=app.state.system_operator_bot_config,
                    notification_bot_config=app.state.notification_bot_config,
                    operation_lock=app.state.message_lock_provider,
                    source_deletion_recorder=app.state.source_deletion_recorder,
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
                        operation_lock=app.state.message_lock_provider,
                        strategy_alert_config=app.state.strategy_alert_config,
                        strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                        authoritative_processor=app.state.authoritative_processor,
                        system_operator_bot_config=app.state.system_operator_bot_config,
                        notification_bot_config=app.state.notification_bot_config,
                        startup_delay_seconds=app.state.reconcile_startup_delay_seconds,
                        loop_lag_snapshot_provider=app.state.loop_lag_monitor.snapshot,
                    )
                )
            if (
                app.state.live_position_snapshot_store.read() is None
                and app.state.position_snapshot_startup_task is None
            ):
                app.state.position_snapshot_startup_task = asyncio.create_task(
                    asyncio.to_thread(refresh_live_position_snapshot)
                )
                app.state.position_snapshot_startup_task.add_done_callback(
                    _log_background_task_result(
                        "position_snapshot_startup_task"
                    )
                )
            yield
        finally:
            loop_lag_monitor_task = app.state.loop_lag_monitor_task
            if loop_lag_monitor_task is not None:
                loop_lag_monitor_task.cancel()
                try:
                    await loop_lag_monitor_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.loop_lag_monitor_task = None
            contract_spec_refresh_task = app.state.contract_spec_refresh_task
            if contract_spec_refresh_task is not None:
                contract_spec_refresh_task.cancel()
                try:
                    await contract_spec_refresh_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.contract_spec_refresh_task = None
                app.state.contract_spec_refresh_orchestrator.close()
            message_operation_supervisor_task = (
                app.state.message_operation_supervisor_task
            )
            if message_operation_supervisor_task is not None:
                message_operation_supervisor_task.cancel()
                try:
                    await message_operation_supervisor_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.message_operation_supervisor_task = None
            position_snapshot_startup_task = (
                app.state.position_snapshot_startup_task
            )
            if position_snapshot_startup_task is not None:
                try:
                    await position_snapshot_startup_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.position_snapshot_startup_task = None
            position_snapshot_refresh_task = (
                app.state.position_snapshot_refresh_task
            )
            if position_snapshot_refresh_task is not None:
                try:
                    await asyncio.wrap_future(position_snapshot_refresh_task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.position_snapshot_refresh_task = None
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
            gap_recovery_loop_task = getattr(
                app.state, "authoritative_gap_recovery_loop_task", None
            )
            if gap_recovery_loop_task is not None:
                gap_recovery_loop_task.cancel()
                try:
                    await gap_recovery_loop_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.authoritative_gap_recovery_loop_task = None
            # ── live listener shutdown ──
            message_processing_worker_task = (
                app.state.message_processing_worker_task
            )
            if message_processing_worker_task is not None:
                message_processing_worker_task.cancel()
                try:
                    await message_processing_worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.message_processing_worker_task = None
            management_worker_task = app.state.strategy_management_worker_task
            if management_worker_task is not None:
                management_worker_task.cancel()
                try:
                    await management_worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.strategy_management_worker_task = None
            break_even_worker_task = (
                app.state.break_even_convergence_worker_task
            )
            if break_even_worker_task is not None:
                break_even_worker_task.cancel()
                try:
                    await break_even_worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.break_even_convergence_worker_task = None
            # Both worker loops submit their blocking ticks here, so the
            # executor is released only after both tasks are cancelled. The
            # shutdown never waits: a tick already in flight finishes on its
            # own thread, exactly as it did when it ran on the event loop.
            shutdown_management_worker_executor(wait=False)
            source_deletion_worker_task = (
                app.state.source_message_deletion_worker_task
            )
            if source_deletion_worker_task is not None:
                source_deletion_worker_task.cancel()
                try:
                    await source_deletion_worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.source_message_deletion_worker_task = None
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
            await _stop_live_listener_task(app)
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
            management_notification_task = app.state.strategy_management_notification_task
            if management_notification_task is not None:
                management_notification_task.cancel()
                try:
                    await management_notification_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.strategy_management_notification_task = None
            runtime_incident_notification_task = (
                app.state.runtime_incident_notification_task
            )
            if runtime_incident_notification_task is not None:
                runtime_incident_notification_task.cancel()
                try:
                    await runtime_incident_notification_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                app.state.runtime_incident_notification_task = None
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
    app.state.runtime_agent_production_audit_runner = (
        runtime_agent_production_audit_runner
        or (
            lambda: run_bounded_production_audit_command(
                resolved_database_path
            )
        )
    )
    app.state.runtime_agent_production_audit_lock = threading.Lock()
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
    loaded_notification_bot_config = load_notification_bot_config()
    app.state.notification_bot_config = (
        loaded_notification_bot_config
        if system_operator_bot_enabled(loaded_notification_bot_config)
        else None
    )

    async def default_runtime_agent_telegram_evidence_runner(
        channel: str,
    ):
        config = (
            app.state.system_operator_bot_config
            if channel == "system_operator"
            else app.state.notification_bot_config
        )
        if not isinstance(config, SystemOperatorBotConfig):
            raise RuntimeError("Telegram evidence configuration unavailable")
        return await probe_system_operator_bot_evidence(config=config)

    app.state.runtime_agent_telegram_evidence_runner = (
        runtime_agent_telegram_evidence_runner
        or default_runtime_agent_telegram_evidence_runner
    )
    app.state.runtime_agent_telegram_evidence_lock = threading.Lock()
    if runtime_incident_config is not None:
        app.state.runtime_incident_config_loader = lambda: runtime_incident_config
    elif runtime_incident_config_loader is not None:
        app.state.runtime_incident_config_loader = runtime_incident_config_loader
    else:
        app.state.runtime_incident_config_loader = load_runtime_incident_config
    try:
        app.state.runtime_incident_config = (
            app.state.runtime_incident_config_loader()
        )
    except Exception:
        app.state.runtime_incident_config = RuntimeIncidentConfig()
    app.state.message_operation_supervisor_config = (
        message_operation_supervisor_config
        if message_operation_supervisor_config is not None
        else load_message_operation_supervisor_config()
    )
    app.state.message_operation_supervisor_policy_status = (
        message_operation_supervisor_policy_status(
            app.state.message_operation_supervisor_config,
            app.state.runtime_incident_config,
        )
    )
    app.state.message_operation_supervisor_runner = (
        message_operation_supervisor_runner
        or run_message_operation_supervisor_cycle
    )
    app.state.message_operation_supervisor_interval_seconds = max(
        1.0,
        min(float(message_operation_supervisor_interval_seconds), 3600.0),
    )
    app.state.message_operation_supervisor_task = None
    app.state.message_operation_supervisor_last_success_at = None
    app.state.message_operation_supervisor_cursor = (
        app.state.message_operation_supervisor_config.after_raw_message_id
    )
    app.state.message_operation_supervisor_watermark_valid = (
        _message_operation_supervisor_watermark_is_valid(app)
    )
    app.state.monitor_incident_capture_lock = threading.Lock()
    app.state.chat_requester = request_grounded_chat_answer
    app.state.prompt_test_runner = run_prompt_draft_test
    app.state.live_target_titles = live_target_titles or set()
    app.state.live_listener_runner = live_listener_runner or run_live_listener
    app.state.source_deletion_recorder = source_deletion_recorder
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
    app.state.history_position_browse_snapshots = HistoryPositionBrowseSnapshotStore(
        now_provider=app.state.now_provider
    )
    app.state.reconcile_runner = reconcile_runner or run_periodic_reconcile
    app.state.recovery_runner = recovery_runner or run_recovery_dry_run
    app.state.recovery_market_data_factory = (
        recovery_market_data_factory or GateMarketDataProvider
    )
    app.state.deepcoin_contract_spec_provider = deepcoin_contract_spec_provider
    refreshable_contract_spec_provider = _refreshable_contract_spec_provider(
        deepcoin_contract_spec_provider
    )
    app.state.contract_spec_refresh_orchestrator = (
        DeepcoinContractSpecRefreshOrchestrator(
            refreshable_contract_spec_provider,
            refresh_timeout_seconds=contract_spec_refresh_timeout_seconds,
            now_provider=app.state.now_provider,
        )
        if refreshable_contract_spec_provider is not None
        else None
    )
    app.state.contract_spec_refresh_task = None
    app.state.deepcoin_client_factory = (
        deepcoin_client_factory or build_deepcoin_client_from_env
    )
    app.state.live_position_snapshot_store = LivePositionSnapshotStore(
        Path(live_position_snapshot_path)
        if live_position_snapshot_path is not None
        else resolved_database_path.parent
        / "web_cache"
        / "deepcoin_live_positions.json"
    )
    app.state.position_snapshot_now_provider = (
        position_snapshot_now_provider or app.state.now_provider
    )
    app.state.position_snapshot_refresh_seconds = max(
        0.0, float(position_snapshot_refresh_seconds)
    )
    app.state.position_snapshot_stale_seconds = max(
        app.state.position_snapshot_refresh_seconds,
        float(position_snapshot_stale_seconds),
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
    app.state.strategy_management_worker_runner = (
        strategy_management_worker_runner or run_strategy_management_worker_loop
    )
    app.state.entry_revision_risk_reduction_executor = (
        entry_revision_risk_reduction_executor
    )
    app.state.strategy_management_worker_interval_seconds = max(
        0.01, float(strategy_management_worker_interval_seconds)
    )
    app.state.strategy_management_worker_startup_delay_seconds = max(
        0.0, float(strategy_management_worker_startup_delay_seconds)
    )
    app.state.strategy_management_worker_max_batches = max(
        1, int(strategy_management_worker_max_batches)
    )
    app.state.strategy_management_worker_task = None
    app.state.message_processing_worker_runner = (
        message_processing_worker_runner or run_message_processing_worker_loop
    )
    app.state.message_processing_worker_interval_seconds = max(
        0.01, float(message_processing_worker_interval_seconds)
    )
    app.state.message_processing_worker_task = None
    app.state.break_even_convergence_worker_runner = (
        break_even_convergence_worker_runner
        or run_break_even_convergence_worker_loop
    )
    app.state.break_even_convergence_worker_interval_seconds = max(
        0.01, float(break_even_convergence_worker_interval_seconds)
    )
    app.state.break_even_convergence_worker_startup_delay_seconds = max(
        0.0, float(break_even_convergence_worker_startup_delay_seconds)
    )
    app.state.break_even_convergence_worker_task = None
    app.state.source_message_deletion_worker_runner = (
        source_message_deletion_worker_runner
        or run_source_message_deletion_worker_loop
    )
    app.state.source_message_deletion_worker_interval_seconds = max(
        0.1, float(source_message_deletion_worker_interval_seconds)
    )
    app.state.source_message_deletion_worker_max_jobs = max(
        1, int(source_message_deletion_worker_max_jobs)
    )
    app.state.source_message_deletion_worker_task = None
    app.state.authoritative_processor = lambda raw_message_id: _run_authoritative_processor(
        app,
        raw_message_id=raw_message_id,
    )
    app.state.reply_evidence_processor = lambda raw_message_id: _extract_reply_evidence(
        app,
        raw_message_id=raw_message_id,
    )
    app.state.context_resolution_scheduler = lambda **event: (
        _schedule_context_resolution_for_app(app, **event)
    )
    app.state.context_resolution_worker = lambda: (
        _run_context_resolution_worker_for_app(app)
    )
    app.state.reconcile_interval_seconds = reconcile_interval_seconds
    app.state.reconcile_startup_delay_seconds = (
        15
        if reconcile_startup_delay_seconds is None
        else reconcile_startup_delay_seconds
    )
    app.state.reconcile_task = None
    app.state.authoritative_gap_recovery_runner = (
        authoritative_gap_recovery_runner or run_authoritative_gap_recovery_loop
    )
    app.state.authoritative_gap_recovery_interval_seconds = (
        authoritative_gap_recovery_interval_seconds
    )
    app.state.authoritative_gap_recovery_loop_task = None
    app.state.deepcoin_reconcile_task = None
    app.state.telegram_bot_command_task = None
    app.state.system_operator_bot_command_task = None
    app.state.strategy_management_notification_task = None
    app.state.runtime_incident_notification_task = None
    app.state.position_snapshot_startup_task = None
    app.state.position_snapshot_refresh_task = None
    app.state.loop_lag_monitor = LoopLagMonitor(now_provider=app.state.now_provider)
    app.state.loop_lag_monitor_task = None
    app.state.web_event_loop = None
    app.state.telegram_auth_loader = load_telegram_auth_config
    app.state.telegram_client_factory = create_telegram_client
    app.state.reconcile_once_runner = run_reconcile_once
    app.state.telegram_session_lock_factory = acquire_telegram_session_lock
    app.state.telegram_operation_lock = asyncio.Lock()
    app.state.message_lock_registry = KeyedAsyncLockRegistry()
    app.state.message_lock_provider = MessageLockProvider(
        session_factory=app.state.session_factory,
        global_lock=app.state.telegram_operation_lock,
        registry=app.state.message_lock_registry,
    )
    app.state.asset_version = _static_asset_version()

    @app.middleware("http")
    async def cache_versioned_workbench_assets(request: Request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["X-Workbench-Asset-Version"] = str(
                app.state.asset_version
            )
        if (
            request.url.path in {"/static/app.js", "/static/app.css"}
            and request.query_params.get("v") == str(app.state.asset_version)
        ):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response

    async def ensure_message_processing_worker_mode() -> None:
        mode = load_trading_settings(
            app.state.session_factory
        ).message_pipeline_mode
        task = app.state.message_processing_worker_task
        if task is not None and task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("message processing worker task failed")
            app.state.message_processing_worker_task = None
            task = None
        if mode != "queue" or task is not None:
            return

        async def notify_terminal_failure(claim, reason) -> None:
            config = app.state.system_operator_bot_config
            if not system_operator_bot_enabled(config):
                return
            await send_system_operator_bot_message(
                config=config,
                text=(
                    "⚠️ 消息处理队列任务终止失败\n"
                    f"raw_message_id: {claim.raw_message_id}\n"
                    f"chat_id: {claim.chat_id}\n"
                    f"reason: {reason}"
                ),
            )

        app.state.message_processing_worker_task = asyncio.create_task(
            app.state.message_processing_worker_runner(
                session_factory=app.state.session_factory,
                interval_seconds=(
                    app.state.message_processing_worker_interval_seconds
                ),
                process_kwargs={
                    "recognition_enabled": True,
                    "strategy_alert_config": app.state.strategy_alert_config,
                    "strategy_alert_enabled_for_title": (
                        app.state.strategy_alert_enabled_for_title
                    ),
                    "strategy_alert_processor": process_strategy_alert_for_record,
                    "authoritative_processor": app.state.authoritative_processor,
                    "context_resolution_scheduler": (
                        app.state.context_resolution_scheduler
                    ),
                    "context_resolution_worker": app.state.context_resolution_worker,
                    "system_operator_bot_config": (
                        app.state.system_operator_bot_config
                    ),
                    "notification_bot_config": app.state.notification_bot_config,
                    "system_operator_conflict_sender": (
                        send_ai_recognition_conflict_review
                    ),
                },
                chat_title_provider=lambda chat_id: {
                    int(group.chat_id): group.chat_title
                    for group in app.state.group_config.groups
                    if group.chat_id is not None
                }.get(int(chat_id)),
                loop_lag_snapshot_provider=app.state.loop_lag_monitor.snapshot,
                terminal_failure_notifier=notify_terminal_failure,
            )
        )
        app.state.message_processing_worker_task.add_done_callback(
            _log_background_task_result("message_processing_worker_task")
        )
        def clear_completed_message_processing_worker(done_task) -> None:
            if app.state.message_processing_worker_task is done_task:
                app.state.message_processing_worker_task = None

        app.state.message_processing_worker_task.add_done_callback(
            clear_completed_message_processing_worker
        )

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
                reply_evidence_processor=app.state.reply_evidence_processor,
                context_resolution_scheduler=app.state.context_resolution_scheduler,
                context_resolution_worker=app.state.context_resolution_worker,
                system_operator_bot_config=app.state.system_operator_bot_config,
                notification_bot_config=app.state.notification_bot_config,
                operation_lock=app.state.message_lock_provider,
                source_deletion_recorder=app.state.source_deletion_recorder,
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
                    operation_lock=app.state.message_lock_provider,
                    strategy_alert_config=app.state.strategy_alert_config,
                    strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                    authoritative_processor=app.state.authoritative_processor,
                    system_operator_bot_config=app.state.system_operator_bot_config,
                    notification_bot_config=app.state.notification_bot_config,
                    startup_delay_seconds=0,
                    loop_lag_snapshot_provider=app.state.loop_lag_monitor.snapshot,
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

    @app.get("/api/runtime/loop-health")
    async def api_runtime_loop_health():
        """Report event loop lag. No database, no exchange, no locks.

        Declared ``async`` on purpose: the snapshot is pure in-memory work, so
        answering it must not depend on a threadpool that may itself be
        saturated when the loop is degraded.
        """

        monitor = app.state.loop_lag_monitor
        return {
            **monitor.snapshot(),
            "now": app.state.now_provider().isoformat(),
            "uptime_seconds": monitor.uptime_seconds(),
        }

    @app.get("/api/runtime/message-pipeline-parity")
    def api_runtime_message_pipeline_parity(
        after_raw_message_id: int = 0,
        limit: int = 1000,
        stuck_after_seconds: int = 300,
    ):
        """Project bounded active-pipeline parity without consuming jobs."""

        if after_raw_message_id < 0:
            raise HTTPException(
                status_code=422,
                detail="after_raw_message_id must be nonnegative",
            )
        if not 1 <= limit <= 5000:
            raise HTTPException(status_code=422, detail="limit must be 1..5000")
        if not 1 <= stuck_after_seconds <= 86400:
            raise HTTPException(
                status_code=422,
                detail="stuck_after_seconds must be 1..86400",
            )

        now = app.state.now_provider()
        pipeline_mode = load_trading_settings(
            app.state.session_factory
        ).message_pipeline_mode
        observed_shadow = pipeline_mode != "queue"
        with app.state.session_factory() as session:
            recent_raw_rows = (
                session.query(RawMessage.id)
                .filter(RawMessage.id > after_raw_message_id)
                .order_by(RawMessage.id.desc())
                .limit(limit + 1)
                .all()
            )
            truncated = len(recent_raw_rows) > limit
            raw_ids = sorted(
                int(row.id) for row in recent_raw_rows[:limit]
            )
            window_start = min(raw_ids) if raw_ids else None
            window_end = max(raw_ids) if raw_ids else None
            if window_start is None or window_end is None:
                job_rows = []
            else:
                bounded_job_rows = (
                    session.query(MessageProcessingJob)
                    .filter(
                        MessageProcessingJob.raw_message_id >= window_start,
                        MessageProcessingJob.raw_message_id <= window_end,
                        MessageProcessingJob.shadow.is_(observed_shadow),
                    )
                    .order_by(MessageProcessingJob.raw_message_id.desc())
                    .limit(limit + 1)
                    .all()
                )
                truncated = truncated or len(bounded_job_rows) > limit
                job_rows = bounded_job_rows[:limit]

        raw_id_set = set(raw_ids)
        job_raw_id_set = {int(job.raw_message_id) for job in job_rows}
        status_breakdown = {
            status: sum(1 for job in job_rows if job.status == status)
            for status in ("pending", "claimed", "succeeded", "failed", "expired")
        }
        pending_ages = []
        for job in job_rows:
            if job.status != "pending":
                continue
            enqueued_at = job.enqueued_at
            if enqueued_at.tzinfo is None:
                enqueued_at = enqueued_at.replace(tzinfo=UTC)
            pending_ages.append(max(0.0, (now - enqueued_at).total_seconds()))

        return {
            "after_raw_message_id": after_raw_message_id,
            "limit": limit,
            "truncated": truncated,
            "window_start_raw_message_id": window_start,
            "window_end_raw_message_id": window_end,
            "raw_messages": len(raw_ids),
            "pipeline_mode": pipeline_mode,
            "observed_job_kind": "shadow" if observed_shadow else "queue",
            "jobs": len(job_rows),
            "shadow_jobs": sum(1 for job in job_rows if job.shadow),
            "queue_jobs": sum(1 for job in job_rows if not job.shadow),
            "missing_job_count": len(raw_id_set - job_raw_id_set),
            "orphan_job_count": len(job_raw_id_set - raw_id_set),
            "stuck_pending_count": sum(
                1 for age in pending_ages if age >= stuck_after_seconds
            ),
            "status_breakdown": status_breakdown,
            "oldest_pending_age_seconds": (
                max(pending_ages) if pending_ages else None
            ),
            "stuck_after_seconds": stuck_after_seconds,
            "now": now.isoformat(),
        }

    @app.get("/api/runtime-agent/read-only-exchange-snapshot")
    def api_runtime_agent_read_only_exchange_snapshot(request: Request):
        client_host = request.client.host if request.client is not None else ""
        if (
            client_host not in {"127.0.0.1", "::1"}
            or "x-forwarded-for" in request.headers
        ):
            raise HTTPException(status_code=404, detail="not found")
        client = None
        try:
            client = app.state.deepcoin_client_factory()
            return build_read_only_exchange_snapshot(client)
        except Exception:
            logger.warning(
                "Runtime Agent read-only exchange snapshot is unavailable"
            )
            return incomplete_read_only_exchange_snapshot()
        finally:
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    logger.warning(
                        "Runtime Agent read-only exchange client cleanup failed"
                    )

    @app.post("/api/runtime-agent/read-only-production-audit")
    def api_runtime_agent_read_only_production_audit(request: Request):
        client_host = request.client.host if request.client is not None else ""
        if (
            client_host not in {"127.0.0.1", "::1"}
            or "x-forwarded-for" in request.headers
        ):
            raise HTTPException(status_code=404, detail="not found")
        acquired = app.state.runtime_agent_production_audit_lock.acquire(
            blocking=False
        )
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="production audit busy",
            )
        try:
            try:
                return project_bounded_production_audit(
                    app.state.runtime_agent_production_audit_runner()
                )
            except Exception:
                logger.warning(
                    "Runtime Agent read-only production audit is unavailable"
                )
                raise HTTPException(
                    status_code=503,
                    detail="production audit unavailable",
                )
        finally:
            app.state.runtime_agent_production_audit_lock.release()

    @app.post("/api/runtime-agent/read-only-telegram-evidence")
    async def api_runtime_agent_read_only_telegram_evidence(
        request: Request,
    ):
        client_host = (
            request.client.host if request.client is not None else ""
        )
        if (
            client_host not in {"127.0.0.1", "::1"}
            or "x-forwarded-for" in request.headers
        ):
            raise HTTPException(status_code=404, detail="not found")
        try:
            payload = await request.json()
        except (TypeError, ValueError):
            payload = None
        channel = (
            payload.get("channel")
            if isinstance(payload, dict)
            else None
        )
        if channel not in {"system_operator", "notification"}:
            raise HTTPException(
                status_code=422,
                detail="invalid Telegram evidence channel",
            )
        acquired = app.state.runtime_agent_telegram_evidence_lock.acquire(
            blocking=False
        )
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="Telegram evidence busy",
            )
        try:
            try:
                result = await app.state.runtime_agent_telegram_evidence_runner(
                    channel
                )
                return project_bounded_telegram_evidence(result)
            except Exception:
                logger.warning(
                    "Runtime Agent Telegram evidence is unavailable"
                )
                raise HTTPException(
                    status_code=503,
                    detail="Telegram evidence unavailable",
                )
        finally:
            app.state.runtime_agent_telegram_evidence_lock.release()

    def require_monitor_capture_auth(request: Request) -> RuntimeIncidentConfig:
        client_host = request.client.host if request.client is not None else ""
        try:
            config = app.state.runtime_incident_config_loader()
        except Exception:
            config = RuntimeIncidentConfig()
        configured_token = config.monitor_capture_token
        supplied_token = request.headers.get("x-monitor-capture-token", "")
        if (
            client_host not in {"127.0.0.1", "::1"}
            or "x-forwarded-for" in request.headers
            or not configured_token
            or not hmac.compare_digest(configured_token, supplied_token)
        ):
            raise HTTPException(status_code=404, detail="not found")
        return config

    @app.get("/api/runtime-incidents/monitor-capture-health")
    def api_runtime_incidents_monitor_capture_health(request: Request):
        require_monitor_capture_auth(request)
        return {"available": True, "schema_version": 1}

    @app.get("/api/runtime-incidents/message-operation-coverage")
    def api_runtime_incidents_message_operation_coverage(request: Request):
        require_monitor_capture_auth(request)
        config = app.state.message_operation_supervisor_config
        policy_status = app.state.message_operation_supervisor_policy_status
        enabled = bool(
            config.enabled
            and config.shadow_only
            and policy_status == "valid"
        )
        snapshot = build_message_operation_coverage_snapshot(
            app.state.session_factory,
            after_raw_message_id=config.after_raw_message_id,
            supervisor_last_success_at=(
                app.state.message_operation_supervisor_last_success_at
            ),
            observed_at=app.state.now_provider(),
            limit=1_000,
            coverage_enabled=enabled,
        )
        snapshot["supervisor_policy_status"] = policy_status
        return snapshot

    @app.post("/api/runtime-incidents/monitor-capture")
    async def api_runtime_incidents_monitor_capture(request: Request):
        capture_config = require_monitor_capture_auth(request)
        raw_content_length = request.headers.get("content-length")
        try:
            content_length = (
                int(raw_content_length)
                if raw_content_length is not None
                else None
            )
        except ValueError:
            content_length = -1
        if content_length is not None and not 0 <= content_length <= 4096:
            raise HTTPException(status_code=422, detail="invalid capture payload")
        chunks: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > 4096:
                raise HTTPException(
                    status_code=422,
                    detail="invalid capture payload",
                )
            chunks.append(chunk)
        body = b"".join(chunks)

        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=strict_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError("invalid constant")
                ),
            )
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "checked_at",
                "reason_codes",
                "adapter_failures",
                "notification_error",
            }:
                raise ValueError("invalid fields")
            if payload["schema_version"] != 1:
                raise ValueError("invalid version")
            checked_at = datetime.fromisoformat(payload["checked_at"])
            if checked_at.tzinfo is None:
                raise ValueError("timestamp must be aware")
            reason_codes = payload["reason_codes"]
            adapter_failures = payload["adapter_failures"]
            notification_error = payload["notification_error"]
            if (
                not isinstance(reason_codes, list)
                or len(reason_codes) > 2
                or any(
                    reason not in {"adapter_failure", "audit_incomplete"}
                    for reason in reason_codes
                )
                or len(set(reason_codes)) != len(reason_codes)
                or not isinstance(adapter_failures, list)
                or len(adapter_failures) > len(MONITOR_ADAPTER_NAMES)
                or any(
                    failure not in MONITOR_ADAPTER_NAMES
                    for failure in adapter_failures
                )
                or len(set(adapter_failures)) != len(adapter_failures)
                or notification_error
                not in {
                    None,
                    "notification_config_missing",
                    "notification_delivery_failed",
                }
            ):
                raise ValueError("invalid values")
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=422,
                detail="invalid capture payload",
            )

        acquired = app.state.monitor_incident_capture_lock.acquire(blocking=False)
        if not acquired:
            raise HTTPException(status_code=409, detail="capture busy")
        try:
            def persist_capture_projection() -> int:
                config = capture_config
                captured = len(
                    capture_monitor_state(
                        app.state.session_factory,
                        config=config,
                        checked_at=checked_at,
                        reason_codes=tuple(reason_codes),
                        adapter_failures=tuple(adapter_failures),
                    )
                )
                if notification_error is not None:
                    source_identity = hashlib.sha256(
                        json.dumps(
                            {
                                "reason_codes": reason_codes,
                                "adapter_failures": adapter_failures,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    captured += int(
                        capture_notification_failure(
                            app.state.session_factory,
                            config=config,
                            source_kind="production_safety_monitor_notification",
                            source_record_id=source_identity,
                            error_type=notification_error,
                            occurred_at=checked_at,
                        )
                        is not None
                    )
                captured += capture_uncaptured_runtime_incident_sources(
                    app.state.session_factory,
                    config_loader=lambda: config,
                )
                return captured

            captured = await _run_monitor_capture_writer(
                persist_capture_projection
            )
            return {"accepted": True, "captured": captured}
        finally:
            app.state.monitor_incident_capture_lock.release()

    @app.get("/api/management-batches")
    def api_management_batches(chat_id: int, limit: int = 50):
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="invalid management batch limit")
        return {
            "read_only": True,
            "batches": _management_batch_api_rows(
                app.state.session_factory,
                chat_id=chat_id,
                limit=limit,
                group_labels=_group_label_by_chat_id(app.state.group_config),
            ),
        }

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
        exited_positions = list_verified_deepcoin_history_positions(
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
            contract_spec_provider=app.state.deepcoin_contract_spec_provider,
        )
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
            "lazy_exchange_tabs": False,
        }

    def refresh_live_position_snapshot():
        store = app.state.live_position_snapshot_store
        if not store.begin_refresh():
            return store.read()
        try:
            exchange_snapshot = _load_exchange_live_snapshot(
                app.state.session_factory,
                deepcoin_client_factory=app.state.deepcoin_client_factory,
                group_label_by_chat_id=_group_label_by_chat_id(
                    app.state.group_config
                ),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
            if exchange_snapshot.get("error"):
                store.finish_failure(str(exchange_snapshot["error"]))
                return store.read()
            source = exchange_snapshot.get("_live_source")
            if not isinstance(source, dict):
                store.finish_failure("live source unavailable")
                return store.read()
            return store.finish_success(
                {
                    **_empty_exchange_snapshot(),
                    "_live_source": source,
                },
                captured_at=app.state.position_snapshot_now_provider(),
            )
        except Exception as exc:
            logger.exception("Deepcoin live position snapshot refresh failed")
            store.finish_failure(str(exc))
            return store.read()

    def position_snapshot_metadata(snapshot) -> dict[str, Any]:
        if snapshot is None:
            return {
                "version": "",
                "state": "error",
                "captured_at": None,
                "age_seconds": None,
                "last_error": "unavailable",
            }
        now = app.state.position_snapshot_now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        captured_at = snapshot.captured_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        else:
            captured_at = captured_at.astimezone(UTC)
        age_seconds = max(0.0, (now - captured_at).total_seconds())
        if snapshot.refreshing:
            state = "refreshing"
        elif snapshot.last_error:
            state = "error"
        elif age_seconds > app.state.position_snapshot_stale_seconds:
            state = "stale"
        else:
            state = "current"
        return {
            "version": snapshot.version,
            "state": state,
            "captured_at": captured_at,
            "age_seconds": age_seconds,
            "last_error": snapshot.last_error,
        }

    def schedule_live_position_snapshot_refresh() -> bool:
        startup_task = app.state.position_snapshot_startup_task
        if startup_task is not None and not startup_task.done():
            return True
        refresh_task = app.state.position_snapshot_refresh_task
        if refresh_task is not None and not refresh_task.done():
            return True
        event_loop = app.state.web_event_loop
        if event_loop is None:
            refresh_live_position_snapshot()
            return True
        refresh_task = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(refresh_live_position_snapshot),
            event_loop,
        )
        refresh_task.add_done_callback(
            _log_background_task_result("position_snapshot_refresh_task")
        )
        app.state.position_snapshot_refresh_task = refresh_task
        return True

    def build_initial_positions_panel_context(
        *,
        schedule_refresh: bool = False,
        allow_sync_refresh: bool = True,
    ) -> dict[str, Any]:
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
            symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(
                app.state.group_config
            ),
        )
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        group_label_by_chat_id = _group_label_by_chat_id(app.state.group_config)
        store = app.state.live_position_snapshot_store
        position_snapshot = store.read()
        refresh_scheduled = False
        if position_snapshot is None:
            if schedule_refresh:
                refresh_scheduled = schedule_live_position_snapshot_refresh()
            elif allow_sync_refresh:
                position_snapshot = refresh_live_position_snapshot()
        else:
            metadata = position_snapshot_metadata(position_snapshot)
            if (
                metadata["age_seconds"] is not None
                and metadata["age_seconds"]
                >= app.state.position_snapshot_refresh_seconds
            ):
                if schedule_refresh:
                    refresh_scheduled = schedule_live_position_snapshot_refresh()
                elif allow_sync_refresh:
                    position_snapshot = refresh_live_position_snapshot()
        exchange_snapshot = (
            _materialize_cached_live_position_snapshot(
                app.state.session_factory,
                payload=position_snapshot.payload,
                group_label_by_chat_id=group_label_by_chat_id,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
            if position_snapshot is not None
            else {
                **_empty_exchange_snapshot(),
                "error": "unavailable",
            }
        )
        exchange_snapshot = _annotate_exchange_snapshot_attribution(
            exchange_snapshot,
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=[],
            group_label_by_chat_id=group_label_by_chat_id,
        )
        return {
            "exchange_snapshot": exchange_snapshot,
            "holding_positions": holding_positions,
            "pending_entry_signals": pending_entry_signals,
            "exited_positions": [],
            "lazy_exchange_tabs": True,
            "position_snapshot": {
                **position_snapshot_metadata(position_snapshot),
                **({"state": "refreshing"} if refresh_scheduled else {}),
            },
        }

    def build_exchange_position_tab_context(
        tab_name: str,
        *,
        browse_token: str | None = None,
        cursor: str | None = None,
        filter_key: tuple[str | None, str | None] = (None, None),
    ) -> dict[str, Any]:
        pending_entry_signals = list_pending_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
            symbol_whitelist_by_chat_id=_symbol_whitelist_by_chat_id(
                app.state.group_config
            ),
        )
        holding_positions = list_holding_strategies(
            app.state.session_factory,
            chat_id=None,
            limit=200,
        )
        exited_positions = (
            list_verified_deepcoin_history_positions(
                app.state.session_factory,
                chat_id=None,
                limit=50,
            )
            if tab_name in {"order-history", "position-history"}
            else []
        )
        group_label_by_chat_id = _group_label_by_chat_id(app.state.group_config)
        history_pagination = None
        if tab_name == "position-history" and browse_token:
            try:
                page = app.state.history_position_browse_snapshots.page(
                    token=browse_token,
                    cursor=cursor,
                    page_size=20,
                    filter_key=filter_key,
                )
            except ValueError:
                return {
                    "tab_name": tab_name,
                    "exchange_snapshot": {**_empty_exchange_snapshot(), "error": "unavailable"},
                    "exchange_tab_captured_at": None,
                }
            exchange_snapshot = _empty_exchange_snapshot()
            exchange_snapshot["position_history"] = list(page.rows)
            history_pagination = {
                "token": browse_token,
                "next_cursor": page.next_cursor,
                "has_more": page.has_more,
                "page_item_count": len(page.rows),
                "total_count": page.total_count,
            }
        else:
            exchange_snapshot = _load_exchange_tab_snapshot(
                app.state.session_factory,
                tab_name=tab_name,
                deepcoin_client_factory=app.state.deepcoin_client_factory,
                group_label_by_chat_id=group_label_by_chat_id,
                pending_entry_signals=pending_entry_signals,
                trading_settings=load_trading_settings(app.state.session_factory),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                order_limit=100 if tab_name == "position-history" else 20,
                known_history_symbols=[
                    str(row.get("symbol") or "")
                    for row in exited_positions
                    if row.get("symbol")
                ],
            )
            if tab_name == "position-history" and not exchange_snapshot.get("error"):
                rows = tuple(
                    row for row in exchange_snapshot["position_history"]
                    if (
                        filter_key[0] is None
                        or (row.get("exited_at") and row["exited_at"].date().isoformat() >= filter_key[0])
                    )
                    and (
                        filter_key[1] is None
                        or (row.get("exited_at") and row["exited_at"].date().isoformat() <= filter_key[1])
                    )
                )
                token = app.state.history_position_browse_snapshots.create(
                    rows=rows,
                    filter_key=filter_key,
                )
                page = app.state.history_position_browse_snapshots.page(
                    token=token,
                    cursor=None,
                    page_size=20,
                    filter_key=filter_key,
                )
                exchange_snapshot["position_history"] = list(page.rows)
                history_pagination = {
                    "token": token,
                    "next_cursor": page.next_cursor,
                    "has_more": page.has_more,
                    "page_item_count": len(page.rows),
                    "total_count": page.total_count,
                }
        exchange_snapshot = _annotate_exchange_snapshot_attribution(
            exchange_snapshot,
            holding_positions=holding_positions,
            pending_entry_signals=pending_entry_signals,
            exited_positions=exited_positions,
            group_label_by_chat_id=group_label_by_chat_id,
        )
        captured_at = app.state.now_provider()
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        else:
            captured_at = captured_at.astimezone(UTC)
        return {
            "tab_name": tab_name,
            "exchange_snapshot": exchange_snapshot,
            "exchange_tab_captured_at": (
                None if exchange_snapshot.get("error") else captured_at
            ),
            "history_pagination": history_pagination,
        }

    def build_strategy_record_payload(
        *,
        filter_name: str,
        chat_id: int | None,
        limit: int,
        page: int = 1,
    ) -> dict[str, Any]:
        group_labels = _group_label_by_chat_id(app.state.group_config)
        # One request owns one read-only, already-annotated exchange snapshot.
        # Enrichment below is pure and never calls Deepcoin from a record loop.
        positions_context = build_positions_panel_context()
        exchange_snapshot = positions_context["exchange_snapshot"]
        exchange_pos_ids = {
            str(row.get("pos_id") or row.get("posId"))
            for row in exchange_snapshot.get("positions", [])
            if row.get("pos_id") or row.get("posId")
        }

        # Recent history is intentionally bounded for mobile response size. Local
        # SQL attention is loaded independently so newer normal records cannot
        # crowd it out. Current exchange pos_ids are loaded directly so an older
        # live binding is not mislabeled as an orphan due to the recent bound.
        scan_limit = max(200, page * limit)
        recent_limit = scan_limit
        local_attention_limit = scan_limit
        lifecycle_page_filter = (
            filter_name
            if filter_name in {"executing", "pending_entry", "finished"}
            else "all"
        )
        recent_records = load_strategy_record_summaries(
            app.state.session_factory,
            group_labels_by_chat_id=group_labels,
            filter_name=lifecycle_page_filter,
            chat_id=chat_id,
            limit=recent_limit,
            now=app.state.now_provider(),
        )
        local_attention_records = load_strategy_record_summaries(
            app.state.session_factory,
            group_labels_by_chat_id=group_labels,
            filter_name="needs_attention",
            chat_id=chat_id,
            limit=local_attention_limit,
            now=app.state.now_provider(),
        )
        current_live_binding_records = load_strategy_record_summaries(
            app.state.session_factory,
            group_labels_by_chat_id=group_labels,
            filter_name="all",
            chat_id=chat_id,
            live_binding_only=True,
            limit=None,
            now=app.state.now_provider(),
        )
        orphan_live_binding_records = load_live_bindings_without_lifecycle(
            app.state.session_factory,
            group_labels_by_chat_id=group_labels,
            chat_id=chat_id,
        )
        current_position_records = (
            load_strategy_record_summaries(
                app.state.session_factory,
                group_labels_by_chat_id=group_labels,
                filter_name="all",
                chat_id=chat_id,
                pos_ids=exchange_pos_ids,
                limit=None,
                now=app.state.now_provider(),
            )
            if exchange_pos_ids
            else []
        )

        candidates_by_lifecycle_id: dict[int, dict[str, object]] = {}
        for record in [
            *local_attention_records,
            *current_live_binding_records,
            *current_position_records,
            *recent_records,
        ]:
            lifecycle_id = record.get("lifecycle_id")
            if lifecycle_id is not None:
                candidates_by_lifecycle_id[int(lifecycle_id)] = record
        enriched = enrich_strategy_records_with_exchange(
            [
                *candidates_by_lifecycle_id.values(),
                *orphan_live_binding_records,
            ],
            exchange_snapshot=exchange_snapshot,
        )
        if chat_id is not None:
            enriched = [row for row in enriched if row.get("chat_id") == chat_id]
        unfiltered_enriched = enriched
        enriched = [
            row
            for row in unfiltered_enriched
            if _strategy_record_matches_filter(row, filter_name=filter_name)
        ]
        enriched.sort(key=_strategy_record_api_sort_key)

        summary_counts = count_strategy_records(
            app.state.session_factory,
            chat_id=chat_id,
        )
        exchange_applicable_count = summary_counts.pop("_exchange_applicable")
        attention_exchange_applicable_count = summary_counts.pop(
            "_attention_exchange_applicable"
        )
        lifecycle_sources = {
            int(row["lifecycle_id"]): row
            for row in [
                *local_attention_records,
                *current_live_binding_records,
                *current_position_records,
                *recent_records,
            ]
            if row.get("lifecycle_id") is not None
        }
        enriched_by_lifecycle_id = {
            int(row["lifecycle_id"]): row
            for row in unfiltered_enriched
            if row.get("lifecycle_id") is not None
        }
        if exchange_snapshot.get("error"):
            summary_counts["needs_attention"] += (
                exchange_applicable_count - attention_exchange_applicable_count
            )
        else:
            summary_counts["needs_attention"] += sum(
                1
                for lifecycle_id, row in enriched_by_lifecycle_id.items()
                if row.get("attention") is not None
                and lifecycle_sources.get(lifecycle_id, {}).get("attention") is None
            )
        for name in STRATEGY_RECORD_FILTERS - {"all", "needs_attention"}:
            summary_counts[name] += sum(
                1
                for lifecycle_id, row in enriched_by_lifecycle_id.items()
                if _strategy_record_matches_filter(row, filter_name=name)
                and not _strategy_record_matches_filter(
                    lifecycle_sources.get(lifecycle_id, {}),
                    filter_name=name,
                )
            )
        synthetic_records = [
            row for row in unfiltered_enriched if row.get("lifecycle_id") is None
        ]
        for name in STRATEGY_RECORD_FILTERS:
            summary_counts[name] += sum(
                1
                for row in synthetic_records
                if _strategy_record_matches_filter(row, filter_name=name)
            )

        start = (page - 1) * limit
        page_records = enriched[start : start + limit]
        total = summary_counts[filter_name]

        return {
            "read_only": True,
            "filter": filter_name,
            "chat_id": chat_id,
            "exchange_state": (
                "unknown" if exchange_snapshot.get("error") else "current"
            ),
            "exchange_error": bool(exchange_snapshot.get("error")),
            "exchange_message": (
                "Deepcoin 仓位快照暂不可用"
                if exchange_snapshot.get("error")
                else "Deepcoin 仓位快照已读取"
            ),
            "exchange_snapshot": exchange_snapshot,
            "records": page_records,
            "summary_counts": summary_counts,
            "page": page,
            "has_more": start + len(page_records) < total,
            "next_page": page + 1 if start + len(page_records) < total else None,
            "scan_scope": {
                "recent_limit": recent_limit,
                "local_attention_limit": local_attention_limit,
                "current_live_binding_scope": "all",
                "orphan_live_binding_scope": "all",
                "orphan_live_binding_count": len(orphan_live_binding_records),
                "current_exchange_pos_id_count": len(exchange_pos_ids),
            },
        }

    @app.get("/api/strategy-records")
    def api_strategy_records(
        filter_name: str = "needs_attention",
        chat_id: int | None = None,
        limit: int = 100,
        page: int = 1,
    ):
        if filter_name not in STRATEGY_RECORD_FILTERS:
            raise HTTPException(status_code=422, detail="invalid strategy record filter")
        if not 1 <= limit <= 200 or page < 1:
            raise HTTPException(status_code=422, detail="invalid strategy record limit")
        payload = build_strategy_record_payload(
            filter_name=filter_name,
            chat_id=chat_id,
            limit=limit,
            page=page,
        )
        payload.pop("exchange_snapshot", None)
        return payload

    @app.get("/strategy-records")
    def strategy_record_list_partial(
        request: Request,
        filter: str = "needs_attention",
        chat_id: str | None = None,
        limit: int = 50,
        page: int = 1,
    ):
        if (
            filter not in STRATEGY_RECORD_FILTERS
            or not 1 <= limit <= 100
            or page < 1
        ):
            raise HTTPException(
                status_code=422,
                detail="invalid strategy record query",
            )
        try:
            normalized_chat_id = int(chat_id) if chat_id not in {None, ""} else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid strategy record query",
            ) from exc
        payload = build_strategy_record_payload(
            filter_name=filter,
            chat_id=normalized_chat_id,
            limit=limit,
            page=page,
        )
        payload.pop("exchange_snapshot", None)
        monitor_status = build_monitor_status()
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        database_state = (
            "stale"
            if freshness["stale_hours"] is not None and freshness["stale_hours"] > 1
            else "current"
        )
        group_options = [
            {
                "chat_id": int(group.chat_id),
                "label": group.custom_group_label or group.chat_title,
            }
            for group in app.state.group_config.groups
            if group.chat_id is not None
        ]
        list_context_query: dict[str, object] = {"filter": filter}
        if normalized_chat_id is not None:
            list_context_query["chat_id"] = normalized_chat_id
        list_context_query["limit"] = limit
        list_context_query["page"] = page
        return templates.TemplateResponse(
            request,
            "_strategy_record_list.html",
            {
                **payload,
                "asset_version": app.state.asset_version,
                "applied_filter": filter,
                "applied_chat_id": normalized_chat_id,
                "applied_limit": limit,
                "applied_page": page,
                "detail_query": urlencode(list_context_query),
                "group_options": group_options,
                "last_success_at": freshness["latest_message_at"],
                "service_states": {
                    "telegram": monitor_status["state"],
                    "database": database_state,
                    "deepcoin": payload["exchange_state"],
                },
            },
        )

    @app.get("/strategy-records/{lifecycle_id}")
    def strategy_record_detail(
        request: Request,
        lifecycle_id: int,
        filter: str = "needs_attention",
        chat_id: str | None = None,
        limit: int = 50,
        page: int = 1,
    ):
        if (
            filter not in STRATEGY_RECORD_FILTERS
            or not 1 <= limit <= 100
            or page < 1
        ):
            raise HTTPException(
                status_code=422,
                detail="invalid strategy record return context",
            )
        try:
            normalized_chat_id = int(chat_id) if chat_id not in {None, ""} else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid strategy record return context",
            ) from exc
        return_query: dict[str, object] = {"filter": filter}
        if normalized_chat_id is not None:
            return_query["chat_id"] = normalized_chat_id
        return_query["limit"] = limit
        return_query["page"] = page
        return_href = f"/strategy-records?{urlencode(return_query)}"
        group_labels = _group_label_by_chat_id(app.state.group_config)
        detail = load_strategy_record_detail(
            app.state.session_factory,
            lifecycle_id=lifecycle_id,
            group_labels_by_chat_id=group_labels,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="strategy record not found")

        binding = detail["execution"].get("binding")
        binding = binding if isinstance(binding, dict) else None
        binding_venue = str(
            binding.get("venue") if binding is not None else "deepcoin"
        ).strip().lower()
        pos_id = binding.get("pos_id") if binding is not None else None
        exact_position_ids = detail["execution"].get("position_ids")
        exact_position_ids = (
            [str(value) for value in exact_position_ids]
            if isinstance(exact_position_ids, list)
            else []
        )
        position_ids_authoritative = (
            detail["execution"].get("position_ids_authoritative") is True
        )
        if not exact_position_ids and not position_ids_authoritative and pos_id:
            exact_position_ids = [str(pos_id)]
        if binding is not None and binding_venue != "deepcoin":
            exchange_evidence = {
                "state": "not_applicable",
                "message": f"非 Deepcoin 绑定（{binding_venue or '未知交易所'}），未读取或匹配 Deepcoin 仓位",
                "position": None,
                "positions": [],
                "reasons": [],
            }
        else:
            # The detail request captures exactly one annotated snapshot and
            # never fetches from inside a per-evidence loop.
            positions_context = build_positions_panel_context()
            exchange_snapshot = positions_context["exchange_snapshot"]
            if exchange_snapshot.get("error"):
                exchange_evidence = {
                    "state": "unknown",
                    "message": "Deepcoin 仓位快照暂不可用",
                    "position": None,
                    "positions": [],
                    "reasons": [],
                }
            else:
                snapshot_positions = exchange_snapshot.get("positions", [])
                snapshot_positions = (
                    snapshot_positions if isinstance(snapshot_positions, list) else []
                )
                matched_positions: list[dict[str, object]] = []
                match_errors: list[str] = []
                ownership_results: list[tuple[str, str, list[str]]] = []
                for expected_pos_id in exact_position_ids:
                    matches = [
                        row
                        for row in snapshot_positions
                        if isinstance(row, dict)
                        and str(row.get("pos_id") or row.get("posId") or "")
                        == expected_pos_id
                    ]
                    if len(matches) != 1:
                        match_errors.append(
                            f"pos_id {expected_pos_id} "
                            + ("匹配到多条仓位" if len(matches) > 1 else "未找到仓位")
                        )
                        continue
                    position = matches[0]
                    matched_positions.append(position)
                    if binding is not None:
                        ownership_results.append(
                            _strategy_detail_position_ownership(
                                detail=detail,
                                binding=binding,
                                position=position,
                                expected_pos_id=expected_pos_id,
                            )
                        )

                if match_errors or not exact_position_ids:
                    exchange_evidence = {
                        "state": "conflict" if match_errors else "not_found",
                        "message": (
                            "；".join(match_errors)
                            if match_errors
                            else "该策略未绑定实时仓位"
                        ),
                        "position": None,
                        "positions": matched_positions,
                        "reasons": [],
                    }
                else:
                    state_rank = {"confirmed": 0, "unconfirmed": 1, "conflict": 2}
                    state = max(
                        (result[0] for result in ownership_results),
                        key=lambda value: state_rank.get(value, 2),
                        default="unconfirmed",
                    )
                    reasons = [
                        reason
                        for _row_state, _message, row_reasons in ownership_results
                        for reason in row_reasons
                    ]
                    if state == "confirmed":
                        message = (
                            "Deepcoin 实时仓位及策略归属已确认"
                            if len(matched_positions) == 1
                            else f"{len(matched_positions)} 个 Deepcoin 实时仓位及策略归属已确认"
                        )
                    else:
                        message = next(
                            (
                                row_message
                                for row_state, row_message, _row_reasons in ownership_results
                                if row_state == state
                            ),
                            "Deepcoin 仓位归属尚未确认",
                        )
                    exchange_evidence = {
                        "state": state,
                        "message": message,
                        "position": (
                            matched_positions[0]
                            if len(matched_positions) == 1
                            else None
                        ),
                        "positions": matched_positions,
                        "reasons": reasons,
                    }
        exchange_evidence["management_drift"] = None
        evidence_positions = exchange_evidence.get("positions")
        if (
            exchange_evidence.get("state") == "confirmed"
            and isinstance(evidence_positions, list)
        ):
            drift_reason = management_execution_drift_reason(
                {
                    "expected_stop_loss": detail["overview"].get("stop_loss"),
                    "expected_take_profit": detail["overview"].get("take_profit"),
                    "expected_management_action": detail["overview"].get(
                        "management_action"
                    ),
                    "management_signal_message_id": detail["overview"].get(
                        "management_signal_message_id"
                    ),
                    "management_confirmations": detail["execution"].get(
                        "management_confirmations"
                    ),
                },
                [row for row in evidence_positions if isinstance(row, dict)],
            )
            if drift_reason is not None:
                exchange_evidence["state"] = "attention"
                exchange_evidence["message"] = "仓位管理与 Deepcoin 实际保护不一致"
                exchange_evidence["reasons"] = [
                    *exchange_evidence.get("reasons", []),
                    drift_reason,
                ]
                exchange_evidence["management_drift"] = {
                    "code": "management_execution_drift",
                    "expected_stop_loss": detail["overview"].get("stop_loss"),
                    "actual_stop_loss": [
                        row.get("stop_loss_text")
                        for row in evidence_positions
                        if isinstance(row, dict)
                    ],
                    "expected_take_profit": detail["overview"].get("take_profit"),
                    "actual_take_profit": [
                        row.get("take_profit_text")
                        for row in evidence_positions
                        if isinstance(row, dict)
                    ],
                    "reason": drift_reason,
                    "confirmation_state": "unexplained",
                }
        detail["execution"]["exchange_evidence"] = exchange_evidence
        return templates.TemplateResponse(
            request,
            "strategy_record_detail.html",
            {
                "asset_version": app.state.asset_version,
                "record": detail,
                "exchange": exchange_evidence,
                "return_href": return_href,
            },
        )

    @app.get("/home-dashboard")
    def home_dashboard_partial(request: Request):
        return templates.TemplateResponse(
            request,
            "_home_dashboard.html",
            build_home_dashboard_context(),
        )

    @app.get("/positions-panel")
    def positions_panel_partial(
        request: Request,
        initial: str | None = None,
    ):
        return templates.TemplateResponse(
            request,
            "_exchange_positions_panel.html",
            (
                build_initial_positions_panel_context(schedule_refresh=True)
                if initial == "positions"
                else build_positions_panel_context()
            ),
        )

    @app.get("/positions-panel/tabs/{tab_name}")
    def positions_panel_tab_partial(
        request: Request,
        tab_name: str,
        browse_token: str | None = None,
        cursor: str | None = None,
        closed_after: str | None = None,
        closed_before: str | None = None,
    ):
        if tab_name not in {"open-orders", "order-history", "position-history"}:
            raise HTTPException(status_code=404, detail="unknown exchange position tab")
        try:
            filter_key = (
                datetime.fromisoformat(closed_after).date().isoformat() if closed_after else None,
                datetime.fromisoformat(closed_before).date().isoformat() if closed_before else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid history date filter") from exc
        if filter_key[0] and filter_key[1] and filter_key[0] > filter_key[1]:
            raise HTTPException(status_code=422, detail="history date range is reversed")
        return templates.TemplateResponse(
            request,
            "_exchange_position_tab.html",
            build_exchange_position_tab_context(
                tab_name,
                browse_token=browse_token,
                cursor=cursor,
                filter_key=filter_key,
            ),
        )

    @app.get("/")
    def index(request: Request, view: str | None = None):
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        monitor_status = build_monitor_status()
        live_listener_enabled = monitor_status["state"] == "monitoring"
        context = {
            "live_listener_enabled": live_listener_enabled,
            "monitor_status": monitor_status,
            "live_listener_status_reason": app.state.live_listener_status_reason,
            "session_lock_owner_pid": _extract_session_lock_owner_pid(
                app.state.live_listener_status_reason
            ),
            "database_latest_message_at": freshness["latest_message_at"],
            "database_stale_hours": freshness["stale_hours"],
            "asset_version": app.state.asset_version,
            "render_deferred_more": False,
            "render_initial_positions": view == "positions",
        }
        if view == "positions":
            context.update(
                build_initial_positions_panel_context(allow_sync_refresh=False)
            )
        return templates.TemplateResponse(
            request,
            "index.html",
            context,
        )

    @app.get("/more-panel")
    def more_panel_partial(request: Request):
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        monitor_status = build_monitor_status()
        ai_recognition_config = load_ai_recognition_config(
            app.state.ai_recognition_config_path
        )
        return templates.TemplateResponse(
            request,
            "more_panel.html",
            {
                "live_listener_enabled": monitor_status["state"] == "monitoring",
                "monitor_status": monitor_status,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "session_lock_owner_pid": _extract_session_lock_owner_pid(
                    app.state.live_listener_status_reason
                ),
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "asset_version": app.state.asset_version,
                "render_deferred_more": True,
                "ai_recognition_config": ai_recognition_config,
                "ai_prompt_views": build_ai_prompt_views(ai_recognition_config),
                "recognition_profiles": list_recognition_profiles(),
                "trading_settings": load_trading_settings(app.state.session_factory),
                "mimo_contract_circuit": load_mimo_contract_circuit(
                    app.state.session_factory
                ),
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
        messages, has_more = load_group_message_page(
            app.state.session_factory,
            chat_id=chat_id,
            page_size=MESSAGE_PAGE_SIZE,
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
                "has_more": has_more,
                "message_page_size": MESSAGE_PAGE_SIZE,
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
                "context_resolution_enabled": load_trading_settings(
                    app.state.session_factory
                ).context_resolution_enabled_for_chat(chat_id),
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
        messages, has_more = load_group_message_page(
            app.state.session_factory,
            chat_id=chat_id,
            page_size=MESSAGE_PAGE_SIZE,
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
                "has_more": has_more,
                "message_page_size": MESSAGE_PAGE_SIZE,
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
                    contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                )
                if hasattr(client, "list_open_orders")
                else None
            )
            result = sync_manual_closed_deepcoin_positions(
                app.state.session_factory,
                client=client,
                synced_at=app.state.now_provider(),
            )
            if isinstance(app.state.notification_bot_config, SystemOperatorBotConfig):
                await deliver_pending_position_attribution_incidents(
                    app.state.session_factory,
                    config=app.state.notification_bot_config,
                    delivered_at=app.state.now_provider(),
                )
                await deliver_pending_position_protection_incidents(
                    app.state.session_factory,
                    config=app.state.notification_bot_config,
                    delivered_at=app.state.now_provider(),
                )
            if isinstance(
                app.state.system_operator_bot_config,
                SystemOperatorBotConfig,
            ):
                await deliver_terminal_entry_cleanup_notifications(
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
            if isinstance(
                app.state.system_operator_bot_config,
                SystemOperatorBotConfig,
            ):
                await deliver_terminal_entry_cleanup_notifications(
                    app.state.session_factory,
                    config=app.state.system_operator_bot_config,
                    delivered_at=app.state.now_provider(),
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
        return _trading_settings_response(app.state.session_factory)

    @app.get("/api/trading-settings/symbols")
    async def list_trading_setting_symbols():
        refresh_status = None
        orchestrator = app.state.contract_spec_refresh_orchestrator
        if orchestrator is not None:
            refresh_status = await orchestrator.refresh_once()
        settings = load_trading_settings(app.state.session_factory)
        saved_symbols = [str(symbol).upper() for symbol in settings.allowed_symbols]
        exchange_symbols = (
            _contract_spec_exchange_symbols(
                _refreshable_contract_spec_provider(
                    app.state.deepcoin_contract_spec_provider
                )
            )
            if orchestrator is not None
            else None
        )
        capability_provider = _refreshable_contract_spec_provider(
            app.state.deepcoin_contract_spec_provider
        )
        capability_snapshot = getattr(capability_provider, "snapshot", None)
        exchange_symbols_verified = exchange_symbols is not None
        if exchange_symbols is None:
            try:
                deepcoin_client = app.state.deepcoin_client_factory()
                if not hasattr(deepcoin_client, "list_swap_symbols"):
                    raise DeepcoinClientError("Deepcoin client cannot list symbols")
                exchange_symbols = deepcoin_client.list_swap_symbols()
                exchange_symbols_verified = True
            except Exception:
                exchange_symbols_verified = False
                exchange_symbols = [
                    {
                        "symbol": symbol,
                        "instrument_id": _to_deepcoin_swap_instrument(symbol),
                    }
                    for symbol in saved_symbols
                ]
        response = {
            "symbols": _build_trading_symbol_rows(
                exchange_symbols,
                selected_symbols=saved_symbols,
                symbol_max_loss_usdt=settings.symbol_max_loss_usdt,
                entry_thresholds_for_symbol=settings.entry_thresholds_for_symbol,
                capability_snapshot=capability_snapshot,
                now=app.state.now_provider(),
                exchange_symbols_verified=exchange_symbols_verified,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                execution_mode=(
                    settings.deepcoin_contract_specs_mode
                    if hasattr(
                        app.state.deepcoin_contract_spec_provider,
                        "authoritative_provider",
                    )
                    else None
                ),
            )
        }
        if refresh_status is not None:
            response["contract_specs"] = _bounded_contract_spec_status(refresh_status)
        return response

    @app.post("/api/trading-settings")
    async def update_trading_settings(payload: dict[str, Any]):
        refresh_status = None
        orchestrator = app.state.contract_spec_refresh_orchestrator
        if orchestrator is not None:
            # Network refresh completes before save_trading_settings opens its
            # short database transaction. Venue support never rewrites or
            # rejects the global allowlist.
            refresh_status = await orchestrator.refresh_once()
        try:
            current = load_trading_settings(app.state.session_factory)
            candidate = trading_settings_from_payload(
                {**current.to_dict(), **payload}
            )
            mimo_contract_change = (
                candidate.mimo_contract_mode != current.mimo_contract_mode
                or candidate.mimo_v2_activation_after_raw_message_id
                != current.mimo_v2_activation_after_raw_message_id
            )
            if mimo_contract_change:
                async with app.state.message_lock_provider.lock_all():
                    locked_current = load_trading_settings(
                        app.state.session_factory
                    )
                    _require_expected_mimo_contract_state(
                        locked_current,
                        payload=payload,
                    )
                    _validate_mimo_contract_activation(
                        app.state.session_factory,
                        payload=payload,
                    )
                    response = save_trading_settings(
                        app.state.session_factory,
                        payload,
                        updated_at=app.state.now_provider(),
                    ).to_dict()
            else:
                payload_without_unchanged_mimo = dict(payload)
                payload_without_unchanged_mimo.pop("mimo_contract_mode", None)
                payload_without_unchanged_mimo.pop(
                    "mimo_v2_activation_after_raw_message_id",
                    None,
                )
                payload_without_unchanged_mimo.pop(
                    "mimo_contract_expected_mode",
                    None,
                )
                payload_without_unchanged_mimo.pop(
                    "mimo_contract_expected_watermark",
                    None,
                )
                response = save_trading_settings(
                    app.state.session_factory,
                    payload_without_unchanged_mimo,
                    updated_at=app.state.now_provider(),
                ).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await ensure_message_processing_worker_mode()
        if refresh_status is not None:
            response["contract_specs"] = refresh_status
        response["mimo_contract_circuit"] = asdict(
            load_mimo_contract_circuit(app.state.session_factory)
        )
        return response

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
                notification_scheduled = _handle_authoritative_failure_notification(
                    session_factory=app.state.session_factory,
                    raw_message_id=raw_message_id,
                    sender=send_ai_recognition_conflict_review,
                    config=app.state.system_operator_bot_config,
                    payload=conflict_payload,
                )
            else:
                notification_scheduled = False
            await _deliver_authoritative_instruction_summary(
                processing_result=processing_result,
                session_factory=app.state.session_factory,
                raw_message_id=raw_message_id,
                chat_title=raw_message.sender_name,
                notification_bot_config=app.state.notification_bot_config,
                claimed_at=app.state.now_provider(),
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
                notification_scheduled
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
        messages, has_more = load_group_message_page(
            app.state.session_factory,
            chat_id=chat_id,
            page_size=MESSAGE_PAGE_SIZE,
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
                "has_more": has_more,
                "message_page_size": MESSAGE_PAGE_SIZE,
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
            async with app.state.message_lock_provider.lock_all():
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
                            "notification_bot_config": app.state.notification_bot_config,
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
            result = process_next_trade_signal_live(
                app.state.session_factory,
                deepcoin_client_factory=app.state.deepcoin_client_factory,
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


def _build_deepcoin_reconcile_client(deepcoin_client_factory, *, now_provider=None):
    """Build the reconcile client and stamp the run, as one blocking unit.

    Constructing the client reaches the exchange, so it belongs on the worker
    thread with the reconcile it feeds. The timestamp is taken here so it keeps
    its original position: after the client exists, before any reconcile work.
    """

    client = deepcoin_client_factory()
    synced_at = now_provider() if now_provider is not None else datetime.now(UTC)
    return client, synced_at


async def run_deepcoin_execution_reconcile_loop(
    *,
    session_factory,
    deepcoin_client_factory,
    interval_seconds: int = 30,
    now_provider=None,
    system_operator_bot_config: SystemOperatorBotConfig | None = None,
    terminal_entry_cleanup_bot_config: SystemOperatorBotConfig | None = None,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> None:
    while True:
        try:
            client, synced_at = await run_on_management_worker(
                _build_deepcoin_reconcile_client,
                deepcoin_client_factory,
                now_provider=now_provider,
            )
            if hasattr(client, "list_open_orders"):
                await run_on_management_worker(
                    reconcile_deepcoin_execution_bindings,
                    session_factory,
                    client=client,
                    recovered_at=synced_at,
                    contract_spec_provider=contract_spec_provider,
                )
                if system_operator_bot_enabled(system_operator_bot_config):
                    await deliver_pending_position_attribution_incidents(
                        session_factory,
                        config=system_operator_bot_config,
                        delivered_at=synced_at,
                    )
                    await deliver_pending_position_protection_incidents(
                        session_factory, config=system_operator_bot_config,
                        delivered_at=synced_at,
                    )
            await run_on_management_worker(
                sync_manual_closed_deepcoin_positions,
                session_factory,
                client=client,
                synced_at=synced_at,
            )
            if system_operator_bot_enabled(terminal_entry_cleanup_bot_config):
                await deliver_terminal_entry_cleanup_notifications(
                    session_factory,
                    config=terminal_entry_cleanup_bot_config,
                    delivered_at=synced_at,
                )
        except asyncio.CancelledError:
            raise
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
