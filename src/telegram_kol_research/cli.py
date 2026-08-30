"""CLI entrypoints for the Telegram KOL research app."""

import asyncio
import ctypes
import hashlib
import json
import os
import platform
import re
import sqlite3
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from enum import Enum
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import typer
from sqlalchemy import create_engine, tuple_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.backfill import build_backfill_windows
from telegram_kol_research.ai_recognition_config import load_ai_recognition_config
from telegram_kol_research.authoritative_recognition import process_authoritative_message
from telegram_kol_research.context_resolution import resolve_contextual_strategy
from telegram_kol_research.context_resolution_worker import (
    build_redacted_exchange_state,
    build_context_state_fingerprint,
    run_context_resolution_once,
)
from telegram_kol_research.binance_market_data import BinanceMarketDataProvider
from telegram_kol_research.dataset_export import export_dataset_jsonl
from telegram_kol_research.db import (
    create_existing_session_factory,
    create_session_factory,
)
from telegram_kol_research.config import (
    load_message_operation_supervisor_config,
    load_runtime_scanner_config,
    message_operation_supervisor_policy_status,
)
from telegram_kol_research.message_operation_supervisor import (
    run_message_operation_supervisor_cycle,
)
from telegram_kol_research.runtime_incident_scanner import build_scanner_facts, run_scanner_cycle
from telegram_kol_research.deepcoin_contract_specs import (
    RefreshableDeepcoinContractSpecProvider,
    RolloutDeepcoinContractSpecProvider,
    load_deepcoin_contract_specs,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    load_deepcoin_contract_spec_snapshot,
)
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_order_builder import (
    deepcoin_order_draft_fingerprint,
)
from telegram_kol_research.entry_draft_revisions import (
    EntryDraftRevisionError,
    revise_entry_draft,
)
from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot_read_only,
    repair_execution_order_legs_from_binding_payloads,
)
from telegram_kol_research.management_history_recovery import (
    ManagementHistoryRecoveryConflict,
    apply_management_history_recovery,
    plan_management_history_recovery,
)
from telegram_kol_research.entry_protection_ledger_repair import (
    apply_entry_protection_ledger_repair_plan,
    build_entry_protection_ledger_repair_plan,
)
from telegram_kol_research.take_profit_protection_leg_repair import (
    apply_take_profit_protection_leg_repair_plan,
    build_take_profit_protection_leg_repair_plan,
)
from telegram_kol_research.entry_assembly_fingerprint_repair import (
    apply_entry_assembly_fingerprint_repair_plan,
    build_entry_assembly_fingerprint_repair_plan,
)
from telegram_kol_research.evidence_backfill import (
    plan_mimo_evidence_backfill,
    run_mimo_evidence_backfill,
)
from telegram_kol_research.current_protection_backfill import (
    SupervisedProtectionMapping,
    apply_current_protection_backfill_plan,
    build_current_protection_backfill_plan,
)
from telegram_kol_research.backup_stop_repair import (
    BackupStopRepairPlan,
    apply_backup_stop_repair_plan,
    build_backup_stop_repair_plan,
)
from telegram_kol_research.legacy_conditional_cancel import (
    REVIEWED_LEGACY_CONDITIONAL_TARGETS,
    apply_reviewed_legacy_conditional_cancel_plan,
    build_reviewed_legacy_conditional_cancel_plan,
)
from telegram_kol_research.manual_pending_entry_reconciliation import (
    apply_manual_pending_entry_reconciliation,
    build_manual_pending_entry_reconciliation_plan,
)
from telegram_kol_research.scoped_release_activation import (
    ActivationError,
    SystemRuntimeAdapter,
    exclusive_runtime_control_lock,
    require_stopped_legacy_runtime_boundary,
)
from telegram_kol_research.reviewed_pending_entry_targets import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)
from telegram_kol_research.position_attribution_repair import (
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)
from telegram_kol_research.historical_state_repair import (
    HistoricalStateRepairRefused,
    apply_historical_state_repair_plan,
    build_historical_state_repair_plan,
    load_historical_state_repair_snapshot_read_only,
)
from telegram_kol_research.position_management_remediation import (
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
)
from telegram_kol_research.position_management_liveness_recovery import (
    apply_position_management_liveness_recovery,
    build_position_management_liveness_recovery_plan,
)
from telegram_kol_research.protection_incident_convergence import (
    PROTECTION_INCIDENT_CLASSIFICATIONS,
    audit_protection_incident_convergence,
)
from telegram_kol_research.production_safety_monitor import (
    MonitorExpectations,
    ProductionSafetyAdapters,
    run_production_safety_monitor,
    send_monitor_test_notification,
)
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.kol_audit_market_data import (
    BinanceAuditMarketData,
    load_cached_candles,
)
from telegram_kol_research.kol_pnl_audit import (
    load_audit_messages,
    load_reviewed_decisions,
    reconstruct_audit_strategies,
    replay_audit_strategy,
)
from telegram_kol_research.kol_pnl_reporting import (
    AuditReportMetadata,
    compare_lifecycle_snapshot,
    summarize_audit_results,
    write_audit_artifacts,
)
from telegram_kol_research.llm_adjudication import (
    export_llm_adjudication_pack,
    export_llm_submission_sample,
)
from telegram_kol_research.llm_import import import_llm_adjudication_results
from telegram_kol_research.llm_chat import (
    load_runtime_agent_llm_config,
    request_structured_chat_turn,
)
from telegram_kol_research.media_retention import cleanup_media_files
from telegram_kol_research.media_dedupe import dedupe_media_assets
from telegram_kol_research.mimo_v2_replay import (
    load_replay_message_ids,
    run_mimo_v2_replay,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    PositionProtectionLedger,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TradeIdea,
    RawMessage,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
    SignalCandidate,
)
from telegram_kol_research.models import SyncCheckpoint
from telegram_kol_research.recognition_decisions import update_recognition_execution_outcome
from telegram_kol_research.semantic_review_control import (
    SemanticReviewControlError,
    SemanticReviewDisablePlan,
    SemanticReviewRollbackPlan,
    SemanticReviewRollbackTarget,
    SemanticReviewTarget,
    apply_semantic_review_disable_plan,
    apply_semantic_review_rollback_plan,
    build_semantic_review_disable_plan,
    build_semantic_review_rollback_plan,
)
from telegram_kol_research.reporting import load_leaderboard_rows, write_report
from telegram_kol_research.config import load_runtime_incident_config
from telegram_kol_research.runtime_agent_tools import (
    RuntimeAgentToolRegistry,
    build_broker_tool_provider,
    build_local_exchange_comparison,
    build_prior_attempts_summary,
    build_protection_summary,
    build_worker_history_summary,
)
from telegram_kol_research.runtime_agent_investigation_broker import (
    BROAD_READ_ONLY_EVIDENCE_KINDS,
    InvestigationBroker,
    build_sqlalchemy_audit_recorder,
)
from telegram_kol_research.runtime_agent_exchange_snapshot import (
    RuntimeAgentExchangeSnapshotRefresh,
)
from telegram_kol_research.runtime_agent_production_audit import (
    RuntimeAgentProductionAuditRefresh,
)
from telegram_kol_research.runtime_agent_telegram_evidence import (
    RuntimeAgentTelegramEvidenceRefresh,
)
from telegram_kol_research.runtime_agent_evaluation import (
    evaluate_runtime_agent_case,
    load_runtime_agent_corpus,
    summarize_runtime_agent_evaluations,
)
from telegram_kol_research.runtime_agent_worker import (
    RuntimeAgentWorkerConfig,
    run_runtime_agent_loop,
    run_runtime_agent_once,
)
from telegram_kol_research.runtime_incident_handoff import (
    RuntimeIncidentHandoffError,
    load_runtime_incident_handoff,
    load_latest_runtime_incident_handoff_artifact,
)
from telegram_kol_research.runtime_incidents import get_runtime_incident
from telegram_kol_research.worker_command_reconciliation import (
    reconcile_worker_command_by_id,
)
from telegram_kol_research.raw_ingest import (
    NormalizedMessageRecord,
    normalize_message_payload,
    persist_normalized_messages,
    repair_history_checkpoints,
)
from telegram_kol_research.review_queue import (
    apply_review_decision,
    apply_review_decision_to_db,
    list_pending_candidates,
    list_pending_candidates_from_db,
    load_candidates,
    write_candidates,
)
from telegram_kol_research.recovery_runner import (
    RecoveryDryRunProviderMissingError,
    run_recovery_dry_run,
)
from telegram_kol_research.recognition_experiments import run_mimo_direct_experiment
from telegram_kol_research.strategy_alerts import (
    load_strategy_alert_config,
    strategy_alerts_enabled,
)
from telegram_kol_research.system_operator_bot import (
    load_notification_bot_config,
    load_system_operator_bot_config,
    send_ai_recognition_conflict_review,
    system_operator_bot_enabled,
)
from telegram_kol_research.telegram_client import (
    create_telegram_client,
    discover_dialogs,
    ensure_telegram_login,
    fetch_dialog_messages,
    filter_target_dialogs,
    load_telegram_auth_config,
    maybe_await,
)
from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
    describe_session_lock_owner,
    reap_stopped_session_lock_owner,
    release_session_lock_owner,
)
from telegram_kol_research.time_utils import normalize_to_utc_naive
from telegram_kol_research.telegram_live_listener import (
    _build_authoritative_notification_payload,
    _filter_callable_kwargs,
    run_live_listener,
)
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.tpsl_ownership_audit import (
    build_tpsl_ownership_audit,
    load_readonly_protection_ledger,
)
from telegram_kol_research.tpsl_ledger_backfill import (
    apply_tpsl_ledger_backfill_plan,
    build_tpsl_ledger_backfill_plan,
)
from telegram_kol_research.web_app import (
    DEFAULT_INGEST_REFRESH_URL,
    create_web_app,
    resolve_runtime_role,
    runtime_role_owns_telegram_session,
)

app = typer.Typer(help="Telegram KOL win-rate research CLI.")
deepcoin_contract_specs_app = typer.Typer(
    help="Inspect or explicitly refresh Deepcoin contract specifications."
)
app.add_typer(deepcoin_contract_specs_app, name="deepcoin-contract-specs")

_DEEPCOIN_CONTRACT_SPEC_CACHE_PATH = Path(
    "data/deepcoin_contract_specs_cache.json"
)
_DEEPCOIN_CONTRACT_SPEC_CLI_TEXT_LIMIT = 512


def _bounded_contract_spec_cli_text(value: object) -> str:
    rendered = str(value)
    if len(rendered) <= _DEEPCOIN_CONTRACT_SPEC_CLI_TEXT_LIMIT:
        return rendered
    return rendered[: _DEEPCOIN_CONTRACT_SPEC_CLI_TEXT_LIMIT - 3] + "..."


def _format_contract_spec_cli_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _echo_contract_spec_summary(payload: dict[str, object]) -> None:
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _fresh_contract_spec_summary(
    *, cache_path: Path, snapshot: object
) -> dict[str, object]:
    capabilities = snapshot.capabilities_by_instrument_id
    return {
        "cache_path": _bounded_contract_spec_cli_text(cache_path),
        "digest_sha256": snapshot.source_digest_sha256,
        "expires_at": _format_contract_spec_cli_datetime(snapshot.expires_at),
        "fetched_at": _format_contract_spec_cli_datetime(snapshot.fetched_at),
        "instrument_count": len(capabilities),
        "live_instrument_count": sum(
            capability.state == "live" for capability in capabilities.values()
        ),
        "state": "fresh",
    }


@deepcoin_contract_specs_app.command("status")
def deepcoin_contract_specs_status(
    cache_path: Path = typer.Option(
        _DEEPCOIN_CONTRACT_SPEC_CACHE_PATH,
        "--cache-path",
        help="Validated dynamic contract-spec cache to inspect read-only.",
    ),
) -> None:
    """Report validated cache health without network access or file creation."""

    now = datetime.now(UTC)
    try:
        snapshot = load_deepcoin_contract_spec_snapshot(cache_path, now=now)
    except FileNotFoundError:
        summary = {
            "cache_path": _bounded_contract_spec_cli_text(cache_path),
            "state": "missing",
        }
    except ValueError as exc:
        state = (
            "stale"
            if str(exc) == "Deepcoin contract spec cache is stale"
            else "invalid"
        )
        summary = {
            "cache_path": _bounded_contract_spec_cli_text(cache_path),
            "state": state,
        }
    except OSError:
        summary = {
            "cache_path": _bounded_contract_spec_cli_text(cache_path),
            "state": "unreadable",
        }
    else:
        summary = _fresh_contract_spec_summary(
            cache_path=cache_path,
            snapshot=snapshot,
        )
    _echo_contract_spec_summary(summary)


@deepcoin_contract_specs_app.command("refresh")
def deepcoin_contract_specs_refresh(
    cache_path: Path = typer.Option(
        _DEEPCOIN_CONTRACT_SPEC_CACHE_PATH,
        "--cache-path",
        help="Validated dynamic contract-spec cache to publish atomically.",
    ),
    ttl_hours: float = typer.Option(
        24.0,
        "--ttl-hours",
        min=0.000001,
        help="Positive cache lifetime in hours.",
    ),
) -> None:
    """Fetch, validate, and atomically publish one explicit snapshot."""

    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=cache_path,
        instrument_loader=lambda: (
            build_deepcoin_client_from_env().list_swap_instruments()
        ),
        ttl=timedelta(hours=ttl_hours),
    )
    previous_snapshot = provider.snapshot
    refreshed = provider.refresh()
    snapshot = provider.snapshot
    if not refreshed or snapshot is None:
        cache_preserved = bool(
            previous_snapshot is not None
            and snapshot is not None
            and previous_snapshot.source_digest_sha256
            == snapshot.source_digest_sha256
        )
        _echo_contract_spec_summary(
            {
                "cache_preserved": cache_preserved,
                "refresh_succeeded": False,
                "state": "refresh_failed",
            }
        )
        raise typer.Exit(code=1)

    summary = _fresh_contract_spec_summary(
        cache_path=cache_path,
        snapshot=snapshot,
    )
    summary["refresh_succeeded"] = True
    _echo_contract_spec_summary(summary)


class SyncMode(str, Enum):
    discover = "discover"
    backfill = "backfill"
    parse = "parse"
    full = "full"


class ExperimentInputKind(str, Enum):
    all = "all"
    text = "text"
    image = "image"


_MANAGEMENT_SIGNAL_ACTIONS = frozenset(
    {
        "adjust_position_tpsl",
        "adjust_stop_loss",
        "adjust_take_profit",
        "close_position",
        "exit_position",
        "partial_close_and_move_stop_to_entry",
        "temporary_close",
        "temporary_exit",
    }
)
_MANAGEMENT_ALERT_STATES = (
    "blocked",
    "submit_unknown",
    "partial_failed",
    "recovery_required",
)
_TRANSIENT_MANAGEMENT_SNAPSHOT_REASONS = frozenset(
    {
        "source_snapshots_differ",
        "source_component_changed_during_read",
        "source_component_set_changed",
    }
)
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_MANAGEMENT_PAYLOAD_CHARS = 65_536
_MAX_MANAGEMENT_PAYLOAD_BYTES = 262_144
_MAX_JSON_NESTING_DEPTH = 64
_MAX_CANONICAL_ID_DIGITS = 20
_MAX_CANONICAL_ID = 9_223_372_036_854_775_807
_MAX_DECIMAL_INPUT_CHARS = 128
_MAX_DECIMAL_DIGITS = 40
_MAX_DECIMAL_FIXED_CHARS = 128
_WEB_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10


def _build_web_server(app_instance, *, host: str, port: int):
    """Close long-lived SSE streams before Uvicorn waits for requests to drain."""

    import uvicorn

    class WebServer(uvicorn.Server):
        async def shutdown(self, sockets=None) -> None:
            app_instance.state.live_update_broker.close()
            await super().shutdown(sockets=sockets)

    config = uvicorn.Config(
        app_instance,
        host=host,
        port=port,
        timeout_graceful_shutdown=_WEB_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )
    return WebServer(config=config)


class ManagementAuditSnapshotError(RuntimeError):
    """Source files could not produce two identical coherent private snapshots."""

    def __init__(self, reason: str, *, status: str = "snapshot_unstable"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _component_stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_atime_ns,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _write_all(descriptor: int, chunk: bytes) -> None:
    offset = 0
    while offset < len(chunk):
        written = os.write(descriptor, chunk[offset:])
        if written <= 0:
            raise ManagementAuditSnapshotError(
                "private_snapshot_write_failed", status="snapshot_unavailable"
            )
        offset += written


def _stream_linux_noatime_component(
    source: Path, destination: Path, *, noatime_flag: int
) -> dict:
    """Stream without atime writes via O_NOATIME or a verified read-only mount."""

    before = source.stat(follow_symlinks=False)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags | noatime_flag)
    except OSError as exc:
        try:
            mount_flags = os.statvfs(source).f_flag
            read_only_flag = os.ST_RDONLY
        except (AttributeError, OSError):
            mount_flags = 0
            read_only_flag = 1
        if not mount_flags & read_only_flag:
            raise ManagementAuditSnapshotError(
                "noatime_open_failed", status="snapshot_unavailable"
            ) from exc
        try:
            source_fd = os.open(source, flags)
        except OSError as fallback_exc:
            raise ManagementAuditSnapshotError(
                "readonly_mount_open_failed", status="snapshot_unavailable"
            ) from fallback_exc
    destination_fd = None
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_fd, chunk)
        try:
            os.fsync(destination_fd)
        except OSError as exc:
            raise ManagementAuditSnapshotError(
                "private_snapshot_unavailable", status="snapshot_unavailable"
            ) from exc
        descriptor_stat = os.fstat(source_fd)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)
    after = source.stat(follow_symlinks=False)
    before_signature = _component_stat_signature(before)
    after_signature = _component_stat_signature(after)
    descriptor_signature = _component_stat_signature(descriptor_stat)
    if before_signature != after_signature or descriptor_signature != after_signature:
        raise ManagementAuditSnapshotError("source_component_changed_during_read")
    return {"stat": after_signature, "size": size, "sha256": digest.hexdigest()}


def _hash_private_component(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _fsync_private_component(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ManagementAuditSnapshotError(
            "private_snapshot_unavailable", status="snapshot_unavailable"
        ) from exc


def _clone_darwin_component(source: Path, destination: Path) -> dict:
    """Atomically COW-clone APFS source data without reading/updating source atime."""

    before = source.stat(follow_symlinks=False)
    try:
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    except AttributeError as exc:
        raise ManagementAuditSnapshotError(
            "darwin_clone_unavailable", status="snapshot_unavailable"
        ) from exc
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    clone_nofollow = 0x0001
    result = clonefile(
        os.fsencode(source), os.fsencode(destination), clone_nofollow
    )
    if result != 0:
        raise ManagementAuditSnapshotError(
            "darwin_clone_failed", status="snapshot_unavailable"
        )
    after = source.stat(follow_symlinks=False)
    before_signature = _component_stat_signature(before)
    after_signature = _component_stat_signature(after)
    if before_signature != after_signature:
        destination.unlink(missing_ok=True)
        raise ManagementAuditSnapshotError("source_component_changed_during_clone")
    size, digest = _hash_private_component(destination)
    if size != after.st_size:
        destination.unlink(missing_ok=True)
        raise ManagementAuditSnapshotError("private_clone_size_mismatch")
    _fsync_private_component(destination)
    return {"stat": after_signature, "size": size, "sha256": digest}


def _stream_snapshot_component(source: Path, destination: Path) -> dict:
    if source.is_symlink() or not source.is_file():
        raise ManagementAuditSnapshotError("source_component_not_regular")
    system = platform.system()
    if system == "Darwin":
        return _clone_darwin_component(source, destination)
    if system == "Linux":
        noatime_flag = getattr(os, "O_NOATIME", None)
        if noatime_flag is None:
            raise ManagementAuditSnapshotError(
                "noatime_capability_unavailable", status="snapshot_unavailable"
            )
        return _stream_linux_noatime_component(
            source, destination, noatime_flag=noatime_flag
        )
    raise ManagementAuditSnapshotError(
        "safe_source_copy_unsupported", status="snapshot_unavailable"
    )


def _source_component_paths(database_path: Path) -> dict[str, Path]:
    candidates = {
        "main": database_path,
        "wal": database_path.with_name(database_path.name + "-wal"),
        "shm": database_path.with_name(database_path.name + "-shm"),
        "journal": database_path.with_name(database_path.name + "-journal"),
    }
    if candidates["journal"].exists():
        raise ManagementAuditSnapshotError("rollback_journal_present")
    if not candidates["main"].exists():
        raise ManagementAuditSnapshotError("source_database_missing")
    return {
        name: path
        for name, path in candidates.items()
        if name != "journal" and path.exists()
    }


def _capture_source_components(database_path: Path, snapshot_root: Path) -> dict[str, dict]:
    snapshot_root.mkdir(mode=0o700)
    paths_before = _source_component_paths(database_path)
    destinations = {
        "main": snapshot_root / "audit.db",
        "wal": snapshot_root / "audit.db-wal",
        "shm": snapshot_root / "source-shm.evidence",
    }
    captured = {
        name: _stream_snapshot_component(path, destinations[name])
        for name, path in paths_before.items()
    }
    paths_after = _source_component_paths(database_path)
    if tuple(sorted(paths_before)) != tuple(sorted(paths_after)):
        raise ManagementAuditSnapshotError("source_component_set_changed")
    return captured


def _validate_private_snapshot(snapshot_path: Path) -> None:
    try:
        with sqlite3.connect(snapshot_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute("PRAGMA quick_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise ManagementAuditSnapshotError("snapshot_quick_check_failed")
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index') LIMIT 1"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ManagementAuditSnapshotError("snapshot_validation_failed") from exc


def _build_stable_private_snapshots(
    database_path: Path, temporary_root: Path
) -> tuple[Path, dict]:
    try:
        first_root = temporary_root / "snapshot-1"
        second_root = temporary_root / "snapshot-2"
        first = _capture_source_components(database_path, first_root)
        second = _capture_source_components(database_path, second_root)
    except ManagementAuditSnapshotError as exc:
        if exc.reason in _TRANSIENT_MANAGEMENT_SNAPSHOT_REASONS:
            return _build_sqlite_online_snapshot(database_path, temporary_root)
        raise
    except OSError as exc:
        raise ManagementAuditSnapshotError(
            "source_copy_failed", status="snapshot_unavailable"
        ) from exc
    if set(first) != set(second) or any(first[name] != second[name] for name in first):
        return _build_sqlite_online_snapshot(database_path, temporary_root)
    first_path = first_root / "audit.db"
    second_path = second_root / "audit.db"
    _validate_private_snapshot(first_path)
    _validate_private_snapshot(second_path)
    return second_path, {
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "snapshot_copies_verified": 2,
        "snapshot_components": sorted(first),
    }


def _build_sqlite_online_snapshot(
    database_path: Path,
    temporary_root: Path,
) -> tuple[Path, dict]:
    """Use SQLite's online backup API when live WAL churn defeats file copies."""

    backup_root = temporary_root / "sqlite-online-backup"
    backup_root.mkdir(mode=0o700)
    snapshot_path = backup_root / "audit.db"
    source_uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
            source.execute("PRAGMA query_only = ON")
            with sqlite3.connect(snapshot_path) as destination:
                source.backup(destination, pages=1024, sleep=0.01)
                destination.commit()
        _fsync_private_component(snapshot_path)
        _validate_private_snapshot(snapshot_path)
    except (OSError, sqlite3.Error) as exc:
        raise ManagementAuditSnapshotError(
            "sqlite_online_backup_failed", status="snapshot_unavailable"
        ) from exc
    return snapshot_path, {
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "snapshot_copies_verified": 1,
        "snapshot_components": ["sqlite_online_backup"],
    }


def _redacted_ref(kind: str, value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if not normalized or len(normalized) > 256:
        return None
    digest = hashlib.sha256(
        kind.encode("ascii") + b":" + normalized.encode("utf-8")
    ).hexdigest()[:10]
    return f"{kind}:{digest}"


def _bounded_text(value: object, *, limit: int = 64) -> str | None:
    if value is None:
        return None
    normalized = str(value).replace("\r", " ").replace("\n", " ")
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _identity_ref(
    kind: str, value: object, malformed_fields: list[str], field: str
) -> str | None:
    reference = _redacted_ref(kind, value)
    if reference is None:
        malformed_fields.append(field)
        return None
    return reference


def _safe_token_value(
    value: object, malformed_fields: list[str], field: str
) -> str:
    if value is None:
        malformed_fields.append(field)
        return "invalid"
    normalized = str(value)
    if not _SAFE_TOKEN.fullmatch(normalized):
        malformed_fields.append(field)
        return "invalid"
    return normalized


def _safe_decimal_value(
    value: object, malformed_fields: list[str], field: str
) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value)
    if len(raw) > _MAX_DECIMAL_INPUT_CHARS:
        malformed_fields.append(field)
        return None
    try:
        normalized = Decimal(raw)
    except (InvalidOperation, ValueError):
        malformed_fields.append(field)
        return None
    if not normalized.is_finite() or normalized < 0:
        malformed_fields.append(field)
        return None
    decimal_tuple = normalized.as_tuple()
    digit_count = len(decimal_tuple.digits)
    exponent = decimal_tuple.exponent
    if (
        not isinstance(exponent, int)
        or digit_count > _MAX_DECIMAL_DIGITS
        or abs(exponent) > _MAX_DECIMAL_FIXED_CHARS
    ):
        malformed_fields.append(field)
        return None
    estimated_fixed_chars = (
        digit_count + exponent
        if exponent >= 0
        else max(digit_count, -exponent) + 2
    ) + int(decimal_tuple.sign)
    if estimated_fixed_chars > _MAX_DECIMAL_FIXED_CHARS:
        malformed_fields.append(field)
        return None
    try:
        rendered = format(normalized, "f")
    except (ValueError, OverflowError, MemoryError):
        malformed_fields.append(field)
        return None
    if len(rendered) > _MAX_DECIMAL_FIXED_CHARS:
        malformed_fields.append(field)
        return None
    return rendered


def _safe_leg_index(
    value: object, malformed_fields: list[str], field: str = "leg_index"
) -> int | None:
    if isinstance(value, bool):
        malformed_fields.append(field)
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        malformed_fields.append(field)
        return None
    if normalized < 0 or str(value).strip() not in {str(normalized), f"+{normalized}"}:
        malformed_fields.append(field)
        return None
    return normalized


def _safe_timestamp(
    value: object, malformed_fields: list[str], field: str
) -> str | None:
    if value is None or value == "":
        malformed_fields.append(field)
        return None
    normalized = str(value)
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        malformed_fields.append(field)
        return None
    return _bounded_text(normalized, limit=40)


def _json_structure_within_depth(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string and not escaped


def _bounded_json_value(value: object) -> tuple[object | None, bool]:
    if not isinstance(value, str) or len(value) > _MAX_MANAGEMENT_PAYLOAD_CHARS:
        return None, True
    try:
        if len(value.encode("utf-8")) > _MAX_MANAGEMENT_PAYLOAD_BYTES:
            return None, True
        if not _json_structure_within_depth(value):
            return None, True
        return json.loads(value or "{}"), False
    except (
        json.JSONDecodeError,
        TypeError,
        RecursionError,
        ValueError,
        OverflowError,
        MemoryError,
    ):
        return None, True


def _malformed_json_fields(row: sqlite3.Row, fields: tuple[str, ...]) -> list[str]:
    malformed: list[str] = []
    keys = set(row.keys())
    for field in fields:
        if field not in keys or row[field] is None or row[field] == "":
            continue
        _, field_malformed = _bounded_json_value(row[field])
        if field_malformed:
            malformed.append(field)
    return malformed


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _has_columns(
    connection: sqlite3.Connection, table: str, required: set[str]
) -> bool:
    return required.issubset(_sqlite_columns(connection, table))


def _canonical_positive_id(payload: object, key: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= _MAX_CANONICAL_ID else None
    if (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_CANONICAL_ID_DIGITS
        and value.isascii()
        and value.isdigit()
        and not value.startswith("0")
    ):
        try:
            normalized = int(value)
        except (ValueError, OverflowError, MemoryError):
            return None
        return normalized if normalized <= _MAX_CANONICAL_ID else None
    return None


def _audit_pending_legacy_management_read_only(
    connection: sqlite3.Connection, *, limit: int
) -> dict:
    required = {
        "id",
        "source_type",
        "venue",
        "chat_id",
        "message_id",
        "action",
        "status",
        "payload_json",
        "created_at",
    }
    if not _has_columns(connection, "trade_signals", required):
        return {
            "status": "schema_unavailable",
            "candidate_pending_count": None,
            "scanned_count": 0,
            "total": None,
            "returned": 0,
            "truncated": False,
            "complete": False,
            "scan_truncated": False,
            "malformed_payload_count": 0,
            "malformed_row_count": 0,
            "malformed_field_count": 0,
            "by_action": {},
            "items": [],
        }
    placeholders = ",".join("?" for _ in _MANAGEMENT_SIGNAL_ACTIONS)
    parameters = tuple(sorted(_MANAGEMENT_SIGNAL_ACTIONS))
    candidate_pending_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM trade_signals "
            "WHERE venue = 'deepcoin' AND status = 'pending' "
            f"AND action IN ({placeholders})",
            parameters,
        ).fetchone()[0]
    )
    cursor = connection.execute(
        "SELECT id, source_type, chat_id, message_id, action, status, payload_json "
        "FROM trade_signals WHERE venue = 'deepcoin' AND status = 'pending' "
        f"AND action IN ({placeholders}) ORDER BY created_at ASC, id ASC",
        parameters,
    )
    legacy_count = 0
    scanned_count = 0
    malformed_payload_count = 0
    malformed_row_count = 0
    malformed_field_count = 0
    selected: list[dict] = []
    by_action: dict[str, int] = {}
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        for row in rows:
            scanned_count += 1
            raw_payload = row["payload_json"]
            payload, malformed = _bounded_json_value(raw_payload)
            if malformed:
                payload = {}
            canonical_batch_id = _canonical_positive_id(
                payload, "management_batch_id"
            )
            if not isinstance(payload, dict) or (
                "management_batch_id" in payload and canonical_batch_id is None
            ):
                malformed = True
            if canonical_batch_id is not None:
                continue
            legacy_count += 1
            malformed_payload_count += int(malformed)
            action_fields: list[str] = []
            action = _safe_token_value(row["action"], action_fields, "action")
            by_action[action] = by_action.get(action, 0) + 1
            item_fields = list(action_fields)
            item = {
                "signal_ref": _identity_ref(
                    "signal", row["id"], item_fields, "signal_id"
                ),
                "action": action,
                "status": _safe_token_value(row["status"], item_fields, "status"),
                "source_type": _safe_token_value(
                    row["source_type"], item_fields, "source_type"
                ),
                "chat_ref": _identity_ref(
                    "chat", row["chat_id"], item_fields, "chat_id"
                ),
                "message_ref": _identity_ref(
                    "message", row["message_id"], item_fields, "message_id"
                ),
                "payload_status": "malformed" if malformed else "missing_batch_id",
            }
            if malformed:
                item_fields.append("payload_json")
            item["malformed_fields"] = sorted(set(item_fields))
            if item_fields:
                malformed_row_count += 1
                malformed_field_count += len(set(item_fields))
            if len(selected) < limit:
                selected.append(item)
    return {
        "status": "available",
        "candidate_pending_count": candidate_pending_count,
        "scanned_count": scanned_count,
        "total": legacy_count,
        "returned": len(selected),
        "truncated": len(selected) < legacy_count,
        "complete": scanned_count == candidate_pending_count,
        "scan_truncated": False,
        "malformed_payload_count": malformed_payload_count,
        "malformed_row_count": malformed_row_count,
        "malformed_field_count": malformed_field_count,
        "by_action": dict(sorted(by_action.items())),
        "items": selected,
    }


def _management_batch_malformed_fields(batch: sqlite3.Row) -> set[str]:
    batch_fields = _malformed_json_fields(batch, ("target_snapshot_json",))
    source_fields: list[str] = []
    target_fields: list[str] = []
    _identity_ref("batch", batch["id"], batch_fields, "batch_id")
    _safe_token_value(batch["status"], batch_fields, "status")
    _safe_token_value(batch["intent"], batch_fields, "intent")
    _safe_token_value(
        batch["effective_action"], batch_fields, "effective_action"
    )
    _safe_token_value(batch["execution_mode"], batch_fields, "execution_mode")
    _safe_timestamp(batch["planned_at"], batch_fields, "planned_at")
    _identity_ref(
        "raw_message",
        batch["raw_message_id"],
        source_fields,
        "raw_message_id",
    )
    _identity_ref("chat", batch["source_chat_id"], source_fields, "source_chat_id")
    _identity_ref(
        "message",
        batch["source_message_id"],
        source_fields,
        "source_message_id",
    )
    _identity_ref(
        "lifecycle",
        batch["target_lifecycle_id"],
        target_fields,
        "target_lifecycle_id",
    )
    _identity_ref(
        "strategy",
        batch["strategy_instance_id"],
        target_fields,
        "strategy_instance_id",
    )
    _identity_ref(
        "binding",
        batch["execution_binding_id"],
        target_fields,
        "execution_binding_id",
    )
    return set(batch_fields + source_fields + target_fields)


def _management_leg_malformed_fields(leg: sqlite3.Row) -> set[str]:
    fields = _malformed_json_fields(leg, ("last_error",))
    _identity_ref("leg", leg["id"], fields, "leg_id")
    _safe_leg_index(leg["leg_index"], fields)
    _safe_token_value(leg["status"], fields, "status")
    _identity_ref("pos", leg["pos_id"], fields, "pos_id")
    _safe_decimal_value(leg["preflight_size"], fields, "preflight_size")
    _safe_decimal_value(
        leg["planned_close_size"], fields, "planned_close_size"
    )
    return set(fields)


def _audit_all_management_evidence(
    connection: sqlite3.Connection,
    *,
    source_select: str,
    source_join: str,
) -> tuple[int, int, bool]:
    malformed_row_count = 0
    malformed_field_count = 0
    batches = connection.execute(
        "SELECT b.id, b.raw_message_id, b.target_lifecycle_id, "
        "b.strategy_instance_id, b.execution_binding_id, b.intent, "
        "b.effective_action, b.execution_mode, b.status, "
        "b.target_snapshot_json, b.planned_at "
        + source_select
        + "FROM strategy_management_batches b "
        + source_join
    )
    for batch in batches:
        fields = _management_batch_malformed_fields(batch)
        if fields:
            malformed_row_count += 1
            malformed_field_count += len(fields)

    legs = connection.execute(
        "SELECT id, pos_id, leg_index, status, preflight_size, "
        "planned_close_size, last_error FROM strategy_management_legs"
    )
    for leg in legs:
        fields = _management_leg_malformed_fields(leg)
        if fields:
            malformed_row_count += 1
            malformed_field_count += len(fields)

    oversized_leg_group_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT management_batch_id FROM strategy_management_legs "
            "GROUP BY management_batch_id HAVING COUNT(*) > 100)"
        ).fetchone()[0]
    )
    return (
        malformed_row_count,
        malformed_field_count,
        oversized_leg_group_count == 0,
    )


def _audit_management_snapshot(
    snapshot_path: Path, *, limit: int, snapshot_info: dict
) -> dict:
    with sqlite3.connect(snapshot_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        batch_required = {
            "id",
            "raw_message_id",
            "target_lifecycle_id",
            "strategy_instance_id",
            "execution_binding_id",
            "intent",
            "effective_action",
            "execution_mode",
            "status",
            "target_snapshot_json",
            "planned_at",
            "completed_at",
        }
        leg_required = {
            "id",
            "management_batch_id",
            "pos_id",
            "leg_index",
            "status",
            "preflight_size",
            "planned_close_size",
            "last_error",
        }
        schema_available = _has_columns(
            connection, "strategy_management_batches", batch_required
        ) and _has_columns(connection, "strategy_management_legs", leg_required)
        result = {
            **snapshot_info,
            "schema_status": (
                "available" if schema_available else "management_schema_missing"
            ),
            "limit": limit,
            "counts": {
                "batches_total": 0,
                "informational_noop": 0,
                "terminal_blocked": 0,
                **{state: 0 for state in _MANAGEMENT_ALERT_STATES},
            },
            "batches_returned": 0,
            "batches_truncated": False,
            "all_history_legs_complete": False,
            "malformed_row_count": 0,
            "malformed_field_count": 0,
            "output_complete": False,
            "batches": [],
            "actionable_batches": {
                "total": 0,
                "returned": 0,
                "truncated": False,
                "items": [],
            },
        }
        if schema_available:
            result["counts"]["batches_total"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM strategy_management_batches"
                ).fetchone()[0]
            )
            informational_predicate = None
            if _has_columns(
                connection, "strategy_management_batches", {"reason_code"}
            ):
                informational_predicate = (
                    "b.intent = 'hold_update' "
                    "AND b.status = 'blocked' "
                    "AND COALESCE(b.reason_code, '') = "
                    "'management_intent_not_supported' "
                    "AND NOT EXISTS (SELECT 1 FROM strategy_management_legs noop_leg "
                    "WHERE noop_leg.management_batch_id = b.id)"
                )
                result["counts"]["informational_noop"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM strategy_management_batches b WHERE "
                        + informational_predicate
                    ).fetchone()[0]
                )
            actionable_leg_states = (
                "'reserved', 'submitted', 'submit_unknown', 'partial', "
                "'inconsistent', 'partial_failed', 'recovery_required'"
            )
            terminal_blocked_predicate = (
                "b.status = 'blocked' "
                "AND b.completed_at IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM strategy_management_legs terminal_leg "
                "WHERE terminal_leg.management_batch_id = b.id "
                f"AND terminal_leg.status IN ({actionable_leg_states})"
                ")"
            )
            result["counts"]["terminal_blocked"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM strategy_management_batches b WHERE "
                    + terminal_blocked_predicate
                ).fetchone()[0]
            )
            actionable_state_predicates: list[str] = []
            for state in _MANAGEMENT_ALERT_STATES:
                exclusions = []
                if state == "blocked":
                    exclusions.append(terminal_blocked_predicate)
                    if informational_predicate is not None:
                        exclusions.append(informational_predicate)
                exclusion = "".join(
                    " AND NOT (" + predicate + ")" for predicate in exclusions
                )
                state_predicate = (
                    "(b.status = '"
                    + state
                    + "' OR EXISTS (SELECT 1 FROM strategy_management_legs alert_leg "
                    "WHERE alert_leg.management_batch_id = b.id "
                    "AND alert_leg.status = '"
                    + state
                    + "'))"
                    + exclusion
                )
                actionable_state_predicates.append("(" + state_predicate + ")")
                result["counts"][state] = int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT b.id) "
                        "FROM strategy_management_batches b "
                        "LEFT JOIN strategy_management_legs l "
                        "ON l.management_batch_id = b.id "
                        "WHERE (b.status = ? OR l.status = ?)" + exclusion,
                        (state, state),
                    ).fetchone()[0]
                )
            actionable_predicate = " OR ".join(actionable_state_predicates)
            actionable_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM strategy_management_batches b WHERE "
                    + actionable_predicate
                ).fetchone()[0]
            )
            actionable_rows = connection.execute(
                "SELECT b.id, b.status FROM strategy_management_batches b WHERE "
                + actionable_predicate
                + " ORDER BY b.id ASC LIMIT 11"
            ).fetchall()
            actionable_items = []
            for actionable_row in actionable_rows[:10]:
                batch_id = _canonical_positive_id(
                    {"batch_id": actionable_row["id"]},
                    "batch_id",
                )
                states = []
                for state, state_predicate in zip(
                    _MANAGEMENT_ALERT_STATES,
                    actionable_state_predicates,
                    strict=True,
                ):
                    if connection.execute(
                        "SELECT 1 FROM strategy_management_batches b "
                        "WHERE b.id = ? AND " + state_predicate,
                        (actionable_row["id"],),
                    ).fetchone() is not None:
                        states.append(state)
                if batch_id is None or not states:
                    result["malformed_row_count"] += 1
                    result["malformed_field_count"] += 1
                    continue
                actionable_items.append(
                    {
                        "batch_ref": f"batch:{batch_id}",
                        "states": states,
                    }
                )
            result["actionable_batches"] = {
                "total": actionable_total,
                "returned": len(actionable_items),
                "truncated": actionable_total > len(actionable_items),
                "items": actionable_items,
            }
            has_raw_source = _has_columns(
                connection, "raw_messages", {"id", "chat_id", "message_id"}
            )
            source_select = (
                ", r.chat_id AS source_chat_id, r.message_id AS source_message_id "
                if has_raw_source
                else ", NULL AS source_chat_id, NULL AS source_message_id "
            )
            source_join = (
                "LEFT JOIN raw_messages r ON r.id = b.raw_message_id "
                if has_raw_source
                else ""
            )
            (
                result["malformed_row_count"],
                result["malformed_field_count"],
                result["all_history_legs_complete"],
            ) = _audit_all_management_evidence(
                connection,
                source_select=source_select,
                source_join=source_join,
            )
            batches = connection.execute(
                "SELECT b.id, b.raw_message_id, b.target_lifecycle_id, "
                "b.strategy_instance_id, b.execution_binding_id, b.intent, "
                "b.effective_action, b.execution_mode, b.status, "
                "b.target_snapshot_json, b.planned_at "
                + source_select
                + "FROM strategy_management_batches b "
                + source_join
                + "ORDER BY b.planned_at DESC, b.id DESC LIMIT ?",
                (limit + 1,),
            ).fetchall()
            result["batches_truncated"] = len(batches) > limit
            for batch in batches[:limit]:
                batch_fields = _malformed_json_fields(
                    batch, ("target_snapshot_json",)
                )
                legs = connection.execute(
                    "SELECT id, pos_id, leg_index, status, preflight_size, "
                    "planned_close_size, last_error FROM strategy_management_legs "
                    "WHERE management_batch_id = ? ORDER BY leg_index ASC, id ASC "
                    "LIMIT 101",
                    (batch["id"],),
                ).fetchall()
                leg_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM strategy_management_legs "
                        "WHERE management_batch_id = ?",
                        (batch["id"],),
                    ).fetchone()[0]
                )
                legs_truncated = len(legs) > 100
                legs = legs[:100]
                source_fields: list[str] = []
                target_fields: list[str] = []
                batch_ref = _identity_ref(
                    "batch", batch["id"], batch_fields, "batch_id"
                )
                batch_status = _safe_token_value(
                    batch["status"], batch_fields, "status"
                )
                intent = _safe_token_value(batch["intent"], batch_fields, "intent")
                effective_action = _safe_token_value(
                    batch["effective_action"], batch_fields, "effective_action"
                )
                execution_mode = _safe_token_value(
                    batch["execution_mode"], batch_fields, "execution_mode"
                )
                planned_at = _safe_timestamp(
                    batch["planned_at"], batch_fields, "planned_at"
                )
                rendered_legs: list[dict] = []
                for leg in legs:
                    leg_fields = _malformed_json_fields(leg, ("last_error",))
                    rendered_legs.append(
                        {
                            "leg_ref": _identity_ref(
                                "leg", leg["id"], leg_fields, "leg_id"
                            ),
                            "leg_index": _safe_leg_index(
                                leg["leg_index"], leg_fields
                            ),
                            "status": _safe_token_value(
                                leg["status"], leg_fields, "status"
                            ),
                            "pos_ref": _identity_ref(
                                "pos", leg["pos_id"], leg_fields, "pos_id"
                            ),
                            "preflight_size": _safe_decimal_value(
                                leg["preflight_size"],
                                leg_fields,
                                "preflight_size",
                            ),
                            "planned_close_size": _safe_decimal_value(
                                leg["planned_close_size"],
                                leg_fields,
                                "planned_close_size",
                            ),
                            "malformed_fields": sorted(set(leg_fields)),
                            "malformed_json_fields": sorted(
                                set(
                                    field
                                    for field in leg_fields
                                    if field == "last_error"
                                )
                            ),
                        }
                    )
                source = {
                    "raw_message_ref": _identity_ref(
                        "raw_message",
                        batch["raw_message_id"],
                        source_fields,
                        "raw_message_id",
                    ),
                    "chat_ref": _identity_ref(
                        "chat",
                        batch["source_chat_id"],
                        source_fields,
                        "source_chat_id",
                    ),
                    "message_ref": _identity_ref(
                        "message",
                        batch["source_message_id"],
                        source_fields,
                        "source_message_id",
                    ),
                    "malformed_fields": sorted(set(source_fields)),
                }
                target = {
                    "lifecycle_ref": _identity_ref(
                        "lifecycle",
                        batch["target_lifecycle_id"],
                        target_fields,
                        "target_lifecycle_id",
                    ),
                    "strategy_ref": _identity_ref(
                        "strategy",
                        batch["strategy_instance_id"],
                        target_fields,
                        "strategy_instance_id",
                    ),
                    "binding_ref": _identity_ref(
                        "binding",
                        batch["execution_binding_id"],
                        target_fields,
                        "execution_binding_id",
                    ),
                    "malformed_fields": sorted(set(target_fields)),
                }
                all_batch_fields = sorted(
                    set(batch_fields + source_fields + target_fields)
                )
                result["batches"].append(
                    {
                        "batch_ref": batch_ref,
                        "status": batch_status,
                        "intent": intent,
                        "effective_action": effective_action,
                        "execution_mode": execution_mode,
                        "planned_at": planned_at,
                        "source": source,
                        "target": target,
                        "malformed_fields": all_batch_fields,
                        "malformed_json_fields": _malformed_json_fields(
                            batch, ("target_snapshot_json",)
                        ),
                        "leg_count": leg_count,
                        "legs_returned": len(legs),
                        "legs_truncated": legs_truncated,
                        "legs": rendered_legs,
                    }
                )
            result["batches_returned"] = len(result["batches"])
        legacy = _audit_pending_legacy_management_read_only(connection, limit=limit)
        result["legacy_pending_management"] = legacy
        result["malformed_row_count"] += legacy["malformed_row_count"]
        result["malformed_field_count"] += legacy["malformed_field_count"]
        result["output_complete"] = (
            not result["batches_truncated"]
            and result["all_history_legs_complete"]
            and all(not batch["legs_truncated"] for batch in result["batches"])
            and legacy["complete"]
            and not legacy["truncated"]
        )
        return result


def _audit_management_batches_read_only(
    database_path: Path,
    *,
    limit: int,
    scratch_root: Path | None = None,
) -> dict:
    try:
        if scratch_root is not None:
            private_root = Path(scratch_root)
            if private_root.is_symlink() or not private_root.is_dir():
                raise ManagementAuditSnapshotError(
                    "private_snapshot_root_invalid",
                    status="snapshot_unavailable",
                )
            return _build_stable_private_snapshots_and_audit(
                database_path,
                private_root,
                limit=limit,
            )
        with tempfile.TemporaryDirectory(prefix="management-audit-") as temporary:
            return _build_stable_private_snapshots_and_audit(
                database_path,
                Path(temporary),
                limit=limit,
            )
    except ManagementAuditSnapshotError:
        raise
    except OSError as exc:
        raise ManagementAuditSnapshotError(
            "private_snapshot_unavailable", status="snapshot_unavailable"
        ) from exc


def _build_stable_private_snapshots_and_audit(
    database_path: Path,
    scratch_root: Path,
    *,
    limit: int,
) -> dict:
    snapshot_path, snapshot_info = _build_stable_private_snapshots(
        database_path,
        scratch_root,
    )
    return _audit_management_snapshot(
        snapshot_path,
        limit=limit,
        snapshot_info=snapshot_info,
    )


def _record_within_window(record: NormalizedMessageRecord, *, start_at, end_at) -> bool:
    posted_at = record.posted_at
    if posted_at is None:
        return True
    normalized_start = normalize_to_utc_naive(start_at)
    normalized_end = normalize_to_utc_naive(end_at)
    return normalized_start <= posted_at <= normalized_end


def _load_normalized_records_from_db(
    database_path: Path,
) -> list[NormalizedMessageRecord]:
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_messages = (
            session.query(RawMessage)
            .order_by(RawMessage.chat_id, RawMessage.message_id)
            .all()
        )

    records: list[NormalizedMessageRecord] = []
    for raw_message in raw_messages:
        payload = {}
        if raw_message.raw_payload:
            try:
                payload = json.loads(raw_message.raw_payload)
            except json.JSONDecodeError:
                payload = {}
        records.append(
            normalize_message_payload(
                {
                    "chat_id": raw_message.chat_id,
                    "message_id": raw_message.message_id,
                    "sender_id": raw_message.sender_id,
                    "sender_name": raw_message.sender_name,
                    "text": raw_message.text,
                    "reply_to_msg_id": raw_message.reply_to_message_id,
                    "posted_at": raw_message.posted_at,
                    "edit_date": raw_message.edit_date,
                    "media": payload.get("media"),
                },
                archived_target_group=raw_message.archived_target_group,
            )
        )
    return records


async def _process_raw_messages_with_mimo_authority(
    session_factory,
    *,
    raw_message_ids: list[int],
    ai_recognition_config_path: Path,
    media_root: Path,
    system_operator_bot_config=None,
) -> int:
    if not raw_message_ids:
        return 0
    config = load_ai_recognition_config(ai_recognition_config_path)
    with session_factory() as session:
        before_ids = {
            row[0]
            for row in session.query(SignalCandidate.id)
            .filter(SignalCandidate.raw_message_id.in_(raw_message_ids))
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .all()
        }
    notification_tasks: list[asyncio.Task[None]] = []
    try:
        for raw_message_id in raw_message_ids:
            processing_result = await asyncio.to_thread(
                process_authoritative_message,
                session_factory,
                raw_message_id=raw_message_id,
                ai_recognition_config=config,
                media_root=media_root,
                auto_trade_executor=None,
            )
            # Successful MiMo decisions remain pending for the Web service's
            # semantic-review worker. CLI parse has no live worker of its own.
            if (
                processing_result.assessment.agreement_status != "authoritative_failed"
                or not system_operator_bot_enabled(system_operator_bot_config)
            ):
                continue
            with session_factory() as session:
                raw_message = session.get(RawMessage, raw_message_id)
                if raw_message is None:
                    continue
                payload = _build_authoritative_notification_payload(
                    raw_message=raw_message,
                    chat_title=raw_message.sender_name,
                    processing_result=processing_result,
                )
            if payload is None:
                continue
            outcome_kwargs = {
                "raw_message_id": raw_message_id,
                "automation_status": str(
                    processing_result.automation.get("status") or "unknown"
                ),
                "automation_reason": processing_result.automation.get("reason"),
            }
            await asyncio.to_thread(
                update_recognition_execution_outcome,
                session_factory,
                **outcome_kwargs,
                notification_status="scheduled",
            )
            notification_tasks.append(
                asyncio.create_task(
                    _deliver_cli_authoritative_failure_notification(
                        session_factory=session_factory,
                        config=system_operator_bot_config,
                        payload=payload,
                        outcome_kwargs=outcome_kwargs,
                    )
                )
            )
    finally:
        if notification_tasks:
            await asyncio.gather(*notification_tasks, return_exceptions=True)
    with session_factory() as session:
        after_ids = {
            row[0]
            for row in session.query(SignalCandidate.id)
            .filter(SignalCandidate.raw_message_id.in_(raw_message_ids))
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .all()
        }
    return len(after_ids - before_ids)


async def _deliver_cli_authoritative_failure_notification(
    *,
    session_factory,
    config,
    payload: dict,
    outcome_kwargs: dict,
) -> None:
    try:
        await send_ai_recognition_conflict_review(config=config, payload=payload)
    except Exception as exc:
        await asyncio.to_thread(
            update_recognition_execution_outcome,
            session_factory,
            **outcome_kwargs,
            notification_status="failed",
            notification_error=str(exc),
        )
    else:
        await asyncio.to_thread(
            update_recognition_execution_outcome,
            session_factory,
            **outcome_kwargs,
            notification_status="sent",
        )


def _run_parse_mode(
    database_path: Path,
    *,
    ai_recognition_config_path: Path,
    media_root: Path,
) -> tuple[int, int]:
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message_ids = [
            row[0]
            for row in session.query(RawMessage.id)
            .order_by(RawMessage.chat_id, RawMessage.message_id, RawMessage.id)
            .all()
        ]
    inserted_candidates = asyncio.run(
        _process_raw_messages_with_mimo_authority(
            session_factory,
            raw_message_ids=raw_message_ids,
            ai_recognition_config_path=ai_recognition_config_path,
            media_root=media_root,
            system_operator_bot_config=load_notification_bot_config(),
        )
    )
    trade_stats = persist_trade_ideas_from_candidates(session_factory)
    return inserted_candidates, trade_stats["inserted_trade_ideas"]


def _load_history_checkpoints(session_factory) -> dict[int, dict[str, int | datetime | None]]:
    with session_factory() as session:
        checkpoints = (
            session.query(SyncCheckpoint)
            .filter(SyncCheckpoint.sync_kind == "history")
            .all()
        )
    return {
        checkpoint.chat_id: {
            "last_message_id": checkpoint.last_message_id,
            "last_message_at": checkpoint.last_message_at,
        }
        for checkpoint in checkpoints
    }


async def _run_telegram_sync(
    *,
    client,
    session_factory,
    target_titles: set[str],
    windows_by_title,
    message_limit: int,
    mode: SyncMode,
    ai_recognition_config_path: Path,
    media_root: Path,
) -> tuple[list[dict[str, str | int | bool | None]], int, int, int]:
    await ensure_telegram_login(
        client,
        prompt_phone=lambda: typer.prompt("Telegram phone number"),
        prompt_code=lambda: typer.prompt("Telegram login code"),
        prompt_password=lambda: typer.prompt("Telegram 2FA password", hide_input=True),
        echo=lambda message: typer.echo(message),
    )

    dialogs = await discover_dialogs(client)
    matched_dialogs = filter_target_dialogs(dialogs, target_titles)
    if mode == SyncMode.discover:
        return matched_dialogs, 0, 0, 0

    history_checkpoints = _load_history_checkpoints(session_factory)

    inserted_messages = 0
    inserted_candidates = 0
    inserted_trade_ideas = 0

    for dialog in matched_dialogs:
        fetch_kwargs = _filter_callable_kwargs(
            fetch_dialog_messages,
            {"limit": message_limit, "media_root": media_root},
        )
        payloads = await fetch_dialog_messages(client, dialog, **fetch_kwargs)
        dialog_id = dialog.get("id")
        checkpoint = None
        if dialog_id is not None:
            checkpoint = history_checkpoints.get(int(dialog_id))
        if checkpoint and checkpoint.get("last_message_id") is not None:
            payloads = [
                payload
                for payload in payloads
                if int(payload.get("message_id") or 0) > int(checkpoint["last_message_id"])
            ]
        normalized_records = [
            normalize_message_payload(payload, archived_target_group=True)
            for payload in payloads
        ]
        window = windows_by_title.get(dialog.get("title"))
        if window is not None:
            normalized_records = [
                record
                for record in normalized_records
                if _record_within_window(
                    record,
                    start_at=window.start_at,
                    end_at=window.end_at,
                )
            ]
        stats = persist_normalized_messages(
            session_factory, normalized_records, sync_kind="history"
        )
        inserted_messages += stats["inserted_messages"]
        if mode == SyncMode.backfill:
            continue
        inserted_keys = stats.get("inserted_message_keys") or []
        with session_factory() as session:
            raw_message_ids = [
                row[0]
                for row in session.query(RawMessage.id)
                .filter(
                    tuple_(RawMessage.chat_id, RawMessage.message_id).in_(inserted_keys)
                )
                .order_by(RawMessage.posted_at, RawMessage.message_id, RawMessage.id)
                .all()
            ] if inserted_keys else []
        inserted_candidates += await _process_raw_messages_with_mimo_authority(
            session_factory,
            raw_message_ids=raw_message_ids,
            ai_recognition_config_path=ai_recognition_config_path,
            media_root=media_root,
            system_operator_bot_config=load_notification_bot_config(),
        )
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]

    return matched_dialogs, inserted_messages, inserted_candidates, inserted_trade_ideas


@app.command("mimo-experiment")
def mimo_experiment(
    database_path: Path = Path("data/research.db"),
    ai_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum messages to consider."),
    kind: ExperimentInputKind = typer.Option(
        ExperimentInputKind.all,
        "--kind",
        help="Message input kind to test.",
    ),
    rerun: bool = typer.Option(
        False,
        "--rerun",
        help="Re-run messages that already have this experiment result.",
    ),
) -> None:
    """Run the MiMo direct multimodal recognition experiment as a side channel."""

    session_factory = create_session_factory(database_path)
    try:
        stats = run_mimo_direct_experiment(
            session_factory,
            ai_recognition_config_path=ai_config_path,
            media_root=media_root,
            limit=limit,
            input_kind=kind.value,
            rerun=rerun,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "MiMo experiment finished: "
        f"considered={stats.considered}, "
        f"succeeded={stats.succeeded}, "
        f"failed={stats.failed}, "
        f"skipped_no_input={stats.skipped_no_input}"
    )


@app.command()
def sync(
    config_path: Path = Path("config/groups.yaml"),
    database_path: Path = Path("data/research.db"),
    message_limit: int = 100,
    mode: SyncMode = SyncMode.full,
    ai_recognition_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
) -> None:
    """Sync Telegram messages."""

    group_config = load_group_config(config_path)
    target_titles = {group.chat_title for group in group_config.groups if group.enabled}
    effective_now = datetime.now(UTC)
    windows_by_title = {
        window.chat_title: window
        for window in build_backfill_windows(
            group_config,
            now=effective_now,
        )
    }

    if mode == SyncMode.parse:
        session_factory = create_session_factory(database_path)
        repair_history_checkpoints(session_factory)
        inserted_candidates, inserted_trade_ideas = _run_parse_mode(
            database_path,
            ai_recognition_config_path=ai_recognition_config_path,
            media_root=media_root,
        )
        typer.echo(f"Parse only mode: read raw messages from {database_path}")
        typer.echo(
            f"Persisted {inserted_candidates} signal candidate(s) to {database_path}"
        )
        typer.echo(f"Persisted {inserted_trade_ideas} trade idea(s) to {database_path}")
        return

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    try:
        with acquire_telegram_session_lock(auth_config.session_path):
            client = create_telegram_client(auth_config)
            session_factory = create_session_factory(database_path)
            repair_history_checkpoints(session_factory)

            matched_dialogs: list[dict[str, str | int | bool | None]] = []
            inserted_messages = 0
            inserted_candidates = 0
            inserted_trade_ideas = 0
            unmatched_titles: set[str] = set()

            try:
                (
                    matched_dialogs,
                    inserted_messages,
                    inserted_candidates,
                    inserted_trade_ideas,
                ) = asyncio.run(
                    _run_telegram_sync(
                        client=client,
                        session_factory=session_factory,
                        target_titles=target_titles,
                        windows_by_title=windows_by_title,
                        message_limit=message_limit,
                        mode=mode,
                        ai_recognition_config_path=ai_recognition_config_path,
                        media_root=media_root,
                    )
                )
                matched_titles = {str(dialog.get("title")) for dialog in matched_dialogs}
                unmatched_titles = target_titles - matched_titles
                if mode == SyncMode.discover:
                    typer.echo(f"Discovered {len(matched_dialogs)} archived target group(s)")
                    typer.echo("Discovery only mode: no messages were fetched or persisted.")
                    for dialog in matched_dialogs:
                        typer.echo(f"- {dialog.get('title')}")
                    if unmatched_titles:
                        typer.echo("Configured groups not currently matched:")
                        for title in sorted(unmatched_titles):
                            typer.echo(f"- {title}")
                    return
            except Exception as exc:
                typer.echo(f"Telegram sync error: {exc}", err=False)
                raise typer.Exit(code=1) from exc
            finally:
                disconnect = getattr(client, "disconnect", None)
                if callable(disconnect):
                    try:
                        asyncio.run(maybe_await(disconnect()))
                    except RuntimeError:
                        pass

            typer.echo(f"Discovered {len(matched_dialogs)} archived target group(s)")
            typer.echo(f"Persisted {inserted_messages} raw message(s) to {database_path}")
            if mode != SyncMode.backfill:
                typer.echo(
                    f"Persisted {inserted_candidates} signal candidate(s) to {database_path}"
                )
                typer.echo(f"Persisted {inserted_trade_ideas} trade idea(s) to {database_path}")
            for dialog in matched_dialogs:
                typer.echo(f"- {dialog.get('title')}")
            if unmatched_titles:
                typer.echo("Configured groups not currently matched:")
                for title in sorted(unmatched_titles):
                    typer.echo(f"- {title}")
    except TelegramSessionLockError as exc:
        typer.echo(str(exc), err=False)
        raise typer.Exit(code=1) from exc


@app.command()
def report(
    output_path: Path = Path("reports/leaderboard.json"),
    database_path: Path = Path("data/research.db"),
    mode: str = "strict",
) -> None:
    """Generate leaderboard reports."""

    session_factory = create_session_factory(database_path)
    rows = load_leaderboard_rows(session_factory, mode=mode)
    written_path = write_report(
        output_path,
        {
            "mode": mode,
            "database_path": str(database_path),
            "rows": rows,
        },
    )
    typer.echo(f"Report written to {written_path}")


@app.command("audit-kol-pnl")
def audit_kol_pnl(
    messages_json: str = typer.Option(
        ...,
        "--messages-json",
        help="Raw-message JSON array path, or '-' to read from stdin.",
    ),
    decisions_json: Path = typer.Option(
        ...,
        "--decisions-json",
        help="Reviewed reconstruction decisions JSON.",
    ),
    lifecycle_json: Path | None = typer.Option(
        None,
        "--lifecycle-json",
        help="Optional read-only lifecycle snapshot JSON for comparison.",
    ),
    chat_id: int = typer.Option(..., "--chat-id"),
    symbols: list[str] = typer.Option(
        None,
        "--symbol",
        help="Audit symbol; repeat for BTC and ETH.",
    ),
    cutoff: str = typer.Option(..., "--cutoff", help="UTC ISO-8601 audit cutoff."),
    output_dir: Path = typer.Option(..., "--output-dir"),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Require an existing verified candle cache; make no market request.",
    ),
    reconstruction_only: bool = typer.Option(
        False,
        "--reconstruction-only",
        help="Write normalized reconstruction evidence without replaying candles.",
    ),
) -> None:
    """Build a read-only, evidence-backed BTC/ETH KOL strategy PnL audit."""

    bounded_output = _bounded_audit_output_directory(output_dir)
    normalized_symbols = tuple(dict.fromkeys(
        str(symbol or "").strip().upper() for symbol in (symbols or ["BTC", "ETH"])
    ))
    if not normalized_symbols or any(symbol not in {"BTC", "ETH"} for symbol in normalized_symbols):
        raise typer.BadParameter("--symbol must contain only BTC and/or ETH")
    audit_cutoff = _parse_audit_cutoff(cutoff)
    try:
        raw_message_payload = _load_json_input(messages_json)
        raw_decision_payload = json.loads(decisions_json.read_text(encoding="utf-8"))
        messages = tuple(
            message
            for message in load_audit_messages(raw_message_payload)
            if message.chat_id == chat_id and message.posted_at <= audit_cutoff
        )
        decisions = load_reviewed_decisions(raw_decision_payload)
        reconstruction = reconstruct_audit_strategies(messages, decisions)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"Audit input validation failed: {exc}")
        raise typer.Exit(code=2) from exc

    bounded_output.mkdir(parents=True, exist_ok=True)
    reconstruction_payload = {
        "chat_id": chat_id,
        "cutoff": _audit_timestamp_text(audit_cutoff),
        "symbols": list(normalized_symbols),
        "strategies": [
            strategy.to_dict()
            for strategy in reconstruction.strategies
            if strategy.symbol in normalized_symbols
        ],
        "excluded": [
            {"message_id": item.message_id, "reason": item.reason}
            for item in reconstruction.excluded
        ],
        "unresolved": [
            {"message_id": item.message_id, "reason": item.reason}
            for item in reconstruction.unresolved
        ],
    }
    _write_audit_json(
        bounded_output / "reconstruction.json", reconstruction_payload
    )
    if reconstruction.unresolved:
        typer.echo(
            f"Audit reconstruction has {len(reconstruction.unresolved)} unresolved item(s); "
            "no final PnL report was claimed."
        )
        raise typer.Exit(code=2)
    if reconstruction_only:
        typer.echo(f"Audit reconstruction written to {bounded_output / 'reconstruction.json'}")
        return

    scoped_strategies = tuple(
        strategy
        for strategy in reconstruction.strategies
        if strategy.symbol in normalized_symbols
    )
    if not scoped_strategies:
        typer.echo("Audit reconstruction contains no in-scope strategies.")
        raise typer.Exit(code=2)

    candle_rows: dict[str, tuple[Any, ...]] = {}
    candle_digests: list[str] = []
    start_at = min(strategy.published_at for strategy in scoped_strategies)
    try:
        for symbol in normalized_symbols:
            if not any(strategy.symbol == symbol for strategy in scoped_strategies):
                continue
            candles, digest = _capture_or_load_audit_candles(
                symbol=symbol,
                start_at=start_at,
                end_at=audit_cutoff,
                cache_path=bounded_output / "candles" / f"{symbol.lower()}-5m.json",
                offline=offline,
            )
            candle_rows[symbol] = candles
            candle_digests.append(f"{symbol}:{digest}")
    except Exception as exc:
        typer.echo(f"Audit candle evidence failed: {exc}")
        raise typer.Exit(code=2) from exc

    results = tuple(
        replay_audit_strategy(
            strategy,
            candle_rows[strategy.symbol],
            cutoff=audit_cutoff,
        )
        for strategy in scoped_strategies
    )
    lifecycle_rows: list[dict[str, Any]] = []
    if lifecycle_json is not None:
        try:
            loaded_lifecycles = json.loads(lifecycle_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            typer.echo(f"Lifecycle snapshot validation failed: {exc}")
            raise typer.Exit(code=2) from exc
        if not isinstance(loaded_lifecycles, list):
            typer.echo("Lifecycle snapshot must be a JSON array.")
            raise typer.Exit(code=2)
        lifecycle_rows = loaded_lifecycles

    source_digest = _canonical_json_digest(raw_message_payload)
    decision_digest = _canonical_json_digest(raw_decision_payload)
    combined_candle_digest = hashlib.sha256(
        "\n".join(sorted(candle_digests)).encode("utf-8")
    ).hexdigest()
    metadata = AuditReportMetadata(
        audit_cutoff=_audit_timestamp_text(audit_cutoff),
        source_sha256=source_digest,
        candle_sha256=combined_candle_digest,
        decision_sha256=decision_digest,
        code_revision=_audit_code_revision(),
        methodology_version="1",
    )
    summaries = summarize_audit_results(results)
    differences = compare_lifecycle_snapshot(scoped_strategies, lifecycle_rows)
    written = write_audit_artifacts(
        output_dir=bounded_output,
        results=results,
        summaries=summaries,
        differences=differences,
        metadata=metadata,
    )
    typer.echo(f"Audit report written to {written.markdown_path}")
    typer.echo(f"Machine-readable results written to {written.json_path}")


def _capture_or_load_audit_candles(
    *,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
    cache_path: Path,
    offline: bool,
) -> tuple[tuple[Any, ...], str]:
    if offline:
        candles, manifest = load_cached_candles(cache_path)
        if manifest.symbol != symbol or manifest.interval != "5m":
            raise ValueError("cached candle scope does not match the audit")
        if manifest.start_at > start_at or manifest.end_at < end_at:
            raise ValueError("cached candles do not cover the audit interval")
        return candles, manifest.sha256
    with BinanceAuditMarketData() as provider:
        candles, manifest = provider.capture_candles(
            symbol=symbol,
            interval="5m",
            start_at=start_at,
            end_at=end_at,
            cache_path=cache_path,
        )
    return candles, manifest.sha256


def _bounded_audit_output_directory(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = {
        Path(resolved.anchor),
        Path.home().resolve(),
        Path.cwd().resolve(),
        Path(__file__).resolve().parents[2],
    }
    if resolved in forbidden:
        raise typer.BadParameter("--output-dir must be a bounded output directory")
    return resolved


def _load_json_input(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_audit_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("--cutoff must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter("--cutoff must include a timezone")
    return parsed.astimezone(UTC)


def _audit_timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_digest(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _audit_code_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _write_audit_json(path: Path, payload: Any) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.replace(path)


@app.command("recovery-dry-run")
def recovery_dry_run(
    config_path: Path = Path("config/groups.yaml"),
    database_path: Path = Path("data/research.db"),
    lookback_hours: int = 48,
    market_provider: str = "none",
    persist: bool = False,
) -> None:
    """Evaluate restart-recovery candidates without placing orders."""

    group_config = load_group_config(config_path)
    session_factory = create_session_factory(database_path)
    market_data = None
    try:
        market_data = _build_recovery_market_provider(market_provider)
        result = run_recovery_dry_run(
            session_factory,
            group_config=group_config,
            now=datetime.now(UTC),
            lookback_hours=lookback_hours,
            market_data=market_data,
            persist=persist,
        )
    except RecoveryDryRunProviderMissingError as exc:
        typer.echo(f"Recovery dry-run unavailable: {exc}", err=False)
        raise typer.Exit(code=1) from exc
    finally:
        close_provider = getattr(market_data, "close", None)
        if callable(close_provider):
            close_provider()

    typer.echo(f"Recovery dry-run candidates: {result.total_candidates}")
    if not result.action_counts:
        typer.echo("No recovery actions.")
        return
    for action, count in sorted(result.action_counts.items()):
        typer.echo(f"{action}: {count}")


@app.command("entry-draft-revision")
def entry_draft_revision(
    draft_path: Path = typer.Option(..., "--draft-path"),
    market_price: str = typer.Option(..., "--market-price"),
    leg_indices: list[int] = typer.Option(..., "--leg-index"),
    operation: str = typer.Option("market_first_leg", "--operation"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    batch_id: int | None = typer.Option(None, "--batch-id"),
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    deepcoin_contract_specs_path: Path = typer.Option(
        Path("config/deepcoin_contract_specs.yaml"),
        "--deepcoin-contract-specs-path",
    ),
) -> None:
    """Dry-run a risk-preserving entry revision; apply only by fingerprint."""

    try:
        original = json.loads(draft_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter("--draft-path must contain valid JSON") from exc
    if not isinstance(original, dict):
        raise typer.BadParameter("--draft-path must contain a JSON object")
    supplied_original = original
    try:
        parsed_market_price = Decimal(market_price)
    except InvalidOperation as exc:
        raise typer.BadParameter("--market-price must be a decimal") from exc
    if batch_id is not None:
        from telegram_kol_research.recovery_live_submit import (
            RecoveryLiveSubmitError,
            load_entry_draft_revision_authority,
        )

        session_factory = create_existing_session_factory(database_path)
        try:
            original, parent_fingerprint = load_entry_draft_revision_authority(
                session_factory,
                batch_id=batch_id,
                supplied_draft=original,
            )
        except RecoveryLiveSubmitError as exc:
            typer.echo(f"Refusing revision: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    else:
        parent_fingerprint = deepcoin_order_draft_fingerprint(original)
    try:
        revised = revise_entry_draft(
            original,
            operation=operation,
            market_price=parsed_market_price,
            authorized_leg_indices=tuple(leg_indices),
        )
    except EntryDraftRevisionError as exc:
        typer.echo(f"Refusing revision: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    original_legs = original.get("order_legs") or []
    revised_legs = revised.get("order_legs") or []
    plan = {
        "mode": "apply" if apply else "dry_run",
        "parent_draft_fingerprint": parent_fingerprint,
        "revision_fingerprint": revised.get("draft_fingerprint"),
        "operation": operation,
        "authorized_leg_indices": list(leg_indices),
        "execution_deadline_at": original.get("execution_deadline_at")
        or original.get("deadline_at"),
        "risk_budget_usdt": original.get("risk_budget_usdt"),
        "blocking_reasons": [],
        "leg_mappings": [
            {
                "leg_index": index,
                "original_client_order_id": before.get("client_order_id"),
                "revised_client_order_id": after.get("client_order_id"),
                "original_order_type": before.get("order_type"),
                "revised_order_type": after.get("order_type"),
                "original_risk_budget_usdt": before.get("risk_budget_usdt"),
                "revised_risk_budget_usdt": after.get("risk_budget_usdt"),
            }
            for index, (before, after) in enumerate(
                zip(original_legs, revised_legs, strict=True), start=1
            )
        ],
    }
    typer.echo(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    if not apply:
        return
    if not expected_fingerprint or batch_id is None:
        typer.echo(
            "Refusing apply: --expected-fingerprint and --batch-id are required.",
            err=True,
        )
        raise typer.Exit(code=2)
    from telegram_kol_research.recovery_live_submit import (
        submit_entry_draft_revision_live,
    )

    session_factory = create_existing_session_factory(database_path)
    result = submit_entry_draft_revision_live(
        session_factory,
        batch_id=batch_id,
        original_draft=supplied_original,
        operation=operation,
        market_price=parsed_market_price,
        authorized_leg_indices=tuple(leg_indices),
        expected_parent_fingerprint=expected_fingerprint,
        deepcoin_client=build_deepcoin_client_from_env(),
        contract_spec_provider=load_deepcoin_contract_specs(
            deepcoin_contract_specs_path
        ),
        submitted_at=datetime.now(UTC),
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("repair-execution-order-legs")
def repair_execution_order_legs(
    database_path: Path = Path("data/research.db"),
) -> None:
    """Backfill per-order Deepcoin execution leg rows from legacy bindings."""

    session_factory = create_session_factory(database_path)
    repaired = repair_execution_order_legs_from_binding_payloads(session_factory)
    typer.echo(f"Repaired {repaired} execution order leg(s) in {database_path}")


@app.command("monitor-production-safety")
def monitor_production_safety(
    expected_release_commit: str | None = typer.Option(
        None, "--expected-release-commit"
    ),
    expected_release_manifest_sha256: str | None = typer.Option(
        None, "--expected-release-manifest-sha256"
    ),
    release_path: Path | None = typer.Option(None, "--release-path"),
    expected_auto_trade_enabled: bool | None = typer.Option(
        None,
        "--expected-auto-trade-enabled/--no-expected-auto-trade-enabled",
    ),
    expected_management_mode: str = typer.Option(
        ...,
        "--expected-management-mode",
    ),
    expected_entry_preamble_mode: str = typer.Option(
        ...,
        "--expected-entry-preamble-mode",
    ),
    expected_entry_message_assembly_v2_mode: str | None = typer.Option(
        None, "--expected-entry-message-assembly-v2-mode"
    ),
    expected_entry_revision_v2_mode: str | None = typer.Option(
        None, "--expected-entry-revision-v2-mode"
    ),
    expected_max_concurrent_positions: int = typer.Option(
        ...,
        "--expected-max-concurrent-positions",
        min=0,
    ),
    database_path: Path = typer.Option(
        Path("data/research.db"),
        "--database-path",
    ),
    checkout_path: Path = typer.Option(
        Path("."),
        "--checkout-path",
    ),
    state_path: str = typer.Option(
        "/var/lib/telegram-kol-monitor/state.json",
        "--state-path",
    ),
    settings_url: str = typer.Option(
        "http://127.0.0.1:8000/api/trading-settings",
        "--settings-url",
    ),
    web_loop_health_url: str = typer.Option(
        "http://127.0.0.1:8000/api/runtime/loop-health",
        "--web-loop-health-url",
    ),
    ingest_loop_health_url: str = typer.Option(
        "http://127.0.0.1:8001/api/runtime/loop-health",
        "--ingest-loop-health-url",
    ),
    worker_loop_health_url: str = typer.Option(
        "http://127.0.0.1:8002/api/runtime/loop-health",
        "--worker-loop-health-url",
    ),
    message_operation_coverage_url: str = typer.Option(
        "http://127.0.0.1:8000/api/runtime-incidents/message-operation-coverage",
        "--message-operation-coverage-url",
    ),
    live_position_sizes_url: str = typer.Option(
        "http://127.0.0.1:8000/api/runtime-incidents/live-position-sizes",
        "--live-position-sizes-url",
    ),
    contract_spec_health_url: str = typer.Option(
        "http://127.0.0.1:8002/api/runtime-incidents/contract-spec-health",
        "--contract-spec-health-url",
    ),
    lookback_minutes: int = typer.Option(35, "--lookback-minutes", min=1, max=120),
    notify: bool = typer.Option(False, "--notify"),
    force_full_audit: bool = typer.Option(False, "--force-full-audit"),
    test_notification: bool = typer.Option(False, "--test-notification"),
    runtime_incident_capture_url: str | None = typer.Option(
        None,
        "--runtime-incident-capture-url",
    ),
) -> None:
    """Run bounded read-only server safety checks and optional alerts."""

    if expected_auto_trade_enabled is None:
        raise typer.BadParameter(
            "choose --expected-auto-trade-enabled or --no-expected-auto-trade-enabled"
        )
    if (
        expected_release_commit is None
        or expected_release_manifest_sha256 is None
        or release_path is None
    ):
        raise typer.BadParameter(
            "immutable release monitoring requires commit, manifest hash, and path"
        )
    expected_head = expected_release_commit
    if test_notification:
        if not notify:
            raise typer.BadParameter("--test-notification requires --notify")
        try:
            notification_status = send_monitor_test_notification()
        except Exception:
            typer.echo(
                json.dumps(
                    {
                        "healthy": False,
                        "mode": "test_notification",
                        "notification_status": "failed",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            raise typer.Exit(code=1)
        typer.echo(
            json.dumps(
                {
                    "healthy": True,
                    "mode": "test_notification",
                    "notification_status": notification_status,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return

    runtime_config = load_runtime_incident_config(
        environment_only=True,
    )
    runtime_incident_session_factory = (
        None
        if runtime_incident_capture_url
        else create_existing_session_factory(database_path)
    )
    outcome = run_production_safety_monitor(
        expectations=MonitorExpectations(
            head=expected_head,
            auto_trade_enabled=expected_auto_trade_enabled,
            management_execution_mode=expected_management_mode,
            max_concurrent_positions=expected_max_concurrent_positions,
            entry_preamble_mode=expected_entry_preamble_mode,
            entry_message_assembly_v2_mode=expected_entry_message_assembly_v2_mode,
            entry_revision_v2_mode=expected_entry_revision_v2_mode,
            release_manifest_sha256=expected_release_manifest_sha256,
        ),
        state_path=Path(state_path),
        adapters=ProductionSafetyAdapters(
            database_path=database_path,
            checkout_path=checkout_path,
            release_path=release_path,
            release_commit=expected_release_commit,
            release_manifest_sha256=expected_release_manifest_sha256,
            settings_url=settings_url,
            web_loop_health_url=web_loop_health_url,
            ingest_loop_health_url=ingest_loop_health_url,
            worker_loop_health_url=worker_loop_health_url,
            message_operation_coverage_url=message_operation_coverage_url,
            live_position_sizes_url=live_position_sizes_url,
            contract_spec_health_url=contract_spec_health_url,
            monitor_capture_token=runtime_config.monitor_capture_token,
        ),
        now=datetime.now(UTC),
        notify=notify,
        force_full_audit=force_full_audit,
        lookback=timedelta(minutes=lookback_minutes),
        runtime_incident_session_factory=runtime_incident_session_factory,
        runtime_incident_capture_url=runtime_incident_capture_url,
        runtime_incident_capture_token=runtime_config.monitor_capture_token,
    )
    summary = {
        "audit_ran": outcome.audit_ran,
        "healthy": outcome.result.healthy,
        "monitor_error": outcome.monitor_error,
        "notification_status": outcome.notification_status,
        "reason_codes": list(outcome.result.reason_codes),
    }
    typer.echo(
        json.dumps(
            summary,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


def _build_runtime_agent_cli_tools(
    session_factory,
    *,
    max_output_bytes: int,
    monitor_state_path: Path = Path(
        "/var/lib/telegram-kol-monitor/state.json"
    ),
    journal_reader=None,
    exchange_snapshot_refresh: (
        RuntimeAgentExchangeSnapshotRefresh | None
    ) = None,
    production_audit_refresh: (
        RuntimeAgentProductionAuditRefresh | None
    ) = None,
    telegram_evidence_refresh: (
        RuntimeAgentTelegramEvidenceRefresh | None
    ) = None,
    deployed_code_version: str = "unknown",
) -> RuntimeAgentToolRegistry:
    def load_incident(session, incident_id: int):
        row = session.get(RuntimeIncident, incident_id)
        if row is None:
            raise ValueError("runtime incident does not exist")
        return row

    def load_management_batch(session, incident):
        if incident.source_kind != "strategy_management_batch":
            return None
        try:
            batch_id = int(incident.source_record_id)
        except (TypeError, ValueError):
            return None
        return session.get(StrategyManagementBatch, batch_id)

    def load_protection_incident(session, incident):
        if incident.source_kind != "position_protection_incident":
            return None
        try:
            protection_id = int(incident.source_record_id)
        except (TypeError, ValueError):
            return None
        return session.get(PositionProtectionIncident, protection_id)

    def isoformat(value):
        return value.isoformat() if value is not None else None

    def incident_summary(*, incident_id: int):
        with session_factory() as session:
            row = load_incident(session, incident_id)
            live_verification = (
                telegram_evidence_refresh.consume_verification(
                    incident_id=int(incident_id)
                )
                if telegram_evidence_refresh is not None
                else None
            )
            try:
                summary = json.loads(row.redacted_summary)
            except (TypeError, ValueError, json.JSONDecodeError):
                summary = {}
            data = {
                "incident_id": row.id,
                "incident_type": row.incident_type,
                "severity": row.severity,
                "status": row.status,
                "generation": row.generation,
                "repeat_count": row.repeat_count,
                "redacted_summary": summary,
            }
            if live_verification is not None:
                data.update(live_verification)
            return {
                "data": data,
                "evidence_refs": (
                    [
                        f"incident:{row.id}",
                        f"telegram-evidence:{row.id}",
                    ]
                    if live_verification is not None
                    else [f"incident:{row.id}"]
                ),
            }

    def message_evidence(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            raw_ids = [
                row[0]
                for row in session.query(
                    RuntimeIncidentAffectedMessage.raw_message_id
                )
                .filter(
                    RuntimeIncidentAffectedMessage.runtime_incident_id
                    == incident.id
                )
                .order_by(RuntimeIncidentAffectedMessage.id)
                .limit(32)
                .all()
            ]
            messages = (
                session.query(RawMessage)
                .filter(RawMessage.id.in_(raw_ids))
                .order_by(RawMessage.id)
                .all()
                if raw_ids
                else []
            )
            reply_keys = {
                (row.chat_id, row.reply_to_message_id)
                for row in messages
                if row.reply_to_message_id is not None
            }
            replies = {
                (row.chat_id, row.message_id): row
                for row in (
                    session.query(RawMessage)
                    .filter(
                        tuple_(RawMessage.chat_id, RawMessage.message_id).in_(
                            reply_keys
                        )
                    )
                    .limit(32)
                    .all()
                    if reply_keys
                    else []
                )
            }
            data = []
            references = [f"incident:{incident.id}"]
            for row in messages:
                reply = replies.get((row.chat_id, row.reply_to_message_id))
                data.append(
                    {
                        "raw_message_id": row.id,
                        "message_id": row.message_id,
                        "posted_at": isoformat(row.posted_at),
                        "source_status": row.source_status,
                        "text": str(row.text or "")[:512],
                        "reply_to_message_id": row.reply_to_message_id,
                        "reply_text": (
                            str(reply.text or "")[:512]
                            if reply is not None
                            else None
                        ),
                    }
                )
                references.append(f"raw_message:{row.id}")
            return {
                "data": {
                    "incident_id": incident.id,
                    "messages": data,
                },
                "evidence_refs": references[:32],
            }

    def deployed_code(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
        return {
            "data": {
                "incident_id": incident.id,
                "deployed_code_version": deployed_code_version,
                "version_verified": deployed_code_version != "unknown",
            },
            "evidence_refs": [f"deployment:{deployed_code_version}"],
        }
    def lifecycle_state(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            batch = load_management_batch(session, incident)
            protection = load_protection_incident(session, incident)
            lifecycle_id = batch.target_lifecycle_id if batch is not None else None
            binding_id = (
                batch.execution_binding_id
                if batch is not None
                else (
                    protection.execution_binding_id
                    if protection is not None
                    else None
                )
            )
            lifecycle = (
                session.get(StrategyLifecycle, lifecycle_id)
                if lifecycle_id is not None
                else (
                    session.query(StrategyLifecycle)
                    .filter(StrategyLifecycle.execution_binding_id == binding_id)
                    .order_by(StrategyLifecycle.id.desc())
                    .first()
                    if binding_id is not None
                    else None
                )
            )
            binding = (
                session.get(ExecutionBinding, binding_id)
                if binding_id is not None
                else None
            )
            order_legs = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
                .order_by(ExecutionOrderLeg.leg_index, ExecutionOrderLeg.id)
                .limit(20)
                .all()
                if binding_id is not None
                else []
            )
            return {
                "data": {
                    "incident_id": incident.id,
                    "applicable": bool(lifecycle or binding or order_legs),
                    "lifecycle": (
                        {
                            "id": lifecycle.id,
                            "status": lifecycle.lifecycle_status,
                            "symbol": lifecycle.symbol,
                            "side": lifecycle.side,
                            "exit_reason": lifecycle.exit_reason,
                            "updated_at": isoformat(lifecycle.updated_at),
                        }
                        if lifecycle is not None
                        else None
                    ),
                    "binding": (
                        {
                            "id": binding.id,
                            "status": binding.status,
                            "venue": binding.venue,
                            "symbol": binding.symbol,
                            "side": binding.side,
                            "last_exchange_status": binding.last_exchange_status,
                        }
                        if binding is not None
                        else None
                    ),
                    "order_legs": [
                        {
                            "id": leg.id,
                            "purpose": leg.purpose,
                            "leg_index": leg.leg_index,
                            "status": leg.status,
                            "attribution_status": leg.attribution_status,
                            "terminal_reason": leg.terminal_reason,
                        }
                        for leg in order_legs
                    ],
                },
                "evidence_refs": [f"incident:{incident.id}"],
            }

    def worker_state(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            data = {
                "incident_id": incident.id,
                "worker_kind": incident.source_kind,
                "applicable": False,
            }
            references = [f"incident:{incident.id}"]
            if incident.source_kind == "context_resolution_attempt":
                try:
                    attempt_id = int(incident.source_record_id)
                except (TypeError, ValueError):
                    attempt_id = 0
                attempt = session.get(ContextResolutionAttempt, attempt_id)
                if attempt is not None:
                    history_rows = (
                        session.query(ContextResolutionAttempt)
                        .filter(
                            ContextResolutionAttempt.raw_message_id
                            == attempt.raw_message_id
                        )
                        .order_by(
                            ContextResolutionAttempt.updated_at.desc(),
                            ContextResolutionAttempt.id.desc(),
                        )
                        .limit(10)
                        .all()
                    )
                    data.update(
                        {
                            "applicable": True,
                            "status": attempt.status,
                            "attempts": attempt.attempts,
                            "error_class": attempt.error_class,
                            "next_attempt_at": isoformat(attempt.next_attempt_at),
                            "claimed": bool(attempt.claim_token),
                            "updated_at": isoformat(attempt.updated_at),
                            "history": build_worker_history_summary(
                                [
                                    {
                                        "record_id": row.id,
                                        "status": row.status,
                                        "attempts": row.attempts,
                                        "error_class": row.error_class,
                                        "updated_at": isoformat(row.updated_at),
                                    }
                                    for row in history_rows
                                ]
                            ),
                        }
                    )
                    references.extend(
                        f"context-attempt:{row.id}" for row in history_rows
                    )
            else:
                batch = load_management_batch(session, incident)
                if batch is not None:
                    history_rows = (
                        session.query(StrategyManagementBatch)
                        .filter(
                            StrategyManagementBatch.strategy_instance_id
                            == batch.strategy_instance_id
                        )
                        .order_by(
                            StrategyManagementBatch.updated_at.desc(),
                            StrategyManagementBatch.id.desc(),
                        )
                        .limit(10)
                        .all()
                    )
                    data.update(
                        {
                            "applicable": True,
                            "status": batch.status,
                            "reason_code": batch.reason_code,
                            "execution_mode": batch.execution_mode,
                            "started_at": isoformat(batch.started_at),
                            "completed_at": isoformat(batch.completed_at),
                            "updated_at": isoformat(batch.updated_at),
                            "history": build_worker_history_summary(
                                [
                                    {
                                        "record_id": row.id,
                                        "status": row.status,
                                        "attempts": (
                                            row.visibility_retry_attempts
                                        ),
                                        "error_class": row.reason_code,
                                        "updated_at": isoformat(row.updated_at),
                                    }
                                    for row in history_rows
                                ]
                            ),
                        }
                    )
                    references.extend(
                        f"management-batch:{row.id}" for row in history_rows
                    )
            return {"data": data, "evidence_refs": references}

    def service_audit_state(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
        live_verification = (
            production_audit_refresh.consume_verification(
                incident_id=int(incident_id)
            )
            if production_audit_refresh is not None
            else None
        )
        if live_verification is not None:
            return {
                "data": {
                    "incident_id": incident.id,
                    **live_verification,
                },
                "evidence_refs": [
                    f"incident:{incident.id}",
                    f"audit-run:{incident.id}",
                ],
            }
        data = {
            "incident_id": incident.id,
            "available": False,
            "audit_run_completed": False,
            "complete": False,
            "monitor_error": None,
            "last_full_audit_date": None,
            "last_window_at": None,
            "last_notification_at": None,
            "anomaly_present": None,
        }
        try:
            raw = json.loads(monitor_state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            data.update(
                {
                    "available": True,
                    "last_full_audit_date": str(
                        raw.get("last_full_audit_date") or ""
                    )[:64]
                    or None,
                    "last_window_at": str(raw.get("last_window_at") or "")[:64]
                    or None,
                    "last_notification_at": str(
                        raw.get("last_notification_at") or ""
                    )[:64]
                    or None,
                    "anomaly_present": bool(raw.get("anomaly_fingerprint")),
                }
            )
        return {
            "data": data,
            "evidence_refs": ["audit-state:production-safety"],
        }

    def default_journal_reader():
        try:
            completed = subprocess.run(
                (
                    "journalctl",
                    "-u",
                    "telegram-kol.service",
                    "-n",
                    "80",
                    "--output=json",
                    "--no-pager",
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        entries = []
        for line in completed.stdout.splitlines()[:80]:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                {
                    "priority": str(payload.get("PRIORITY") or "")[:2],
                    "timestamp": str(
                        payload.get("__REALTIME_TIMESTAMP") or ""
                    )[:32],
                }
            )
        return tuple(entries)

    def journal_summary(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
        entries = tuple((journal_reader or default_journal_reader)())[:80]
        priority_counts: dict[str, int] = {}
        latest_timestamp = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            priority = str(entry.get("priority") or "unknown")[:16]
            priority_counts[priority] = priority_counts.get(priority, 0) + 1
            timestamp = str(entry.get("timestamp") or "")[:64]
            if timestamp and (
                latest_timestamp is None or timestamp > latest_timestamp
            ):
                latest_timestamp = timestamp
        return {
            "data": {
                "incident_id": incident.id,
                "available": bool(entries),
                "entry_count": len(entries),
                "priority_counts": priority_counts,
                "latest_timestamp": latest_timestamp,
            },
            "evidence_refs": ["journal:telegram-kol"],
        }

    def stored_exchange_state(session, incident):
        batch = load_management_batch(session, incident)
        if batch is None:
            return []
        legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
            .limit(20)
            .all()
        )
        rows = []
        for leg in legs:
            try:
                snapshot = json.loads(leg.last_exchange_snapshot_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            exchange_status = next(
                (
                    snapshot.get(key)
                    for key in (
                        "status",
                        "order_status",
                        "position_status",
                        "state",
                    )
                    if isinstance(snapshot.get(key), (str, int, float, bool))
                ),
                None,
            )
            exchange_size = next(
                (
                    snapshot.get(key)
                    for key in ("size", "position_size", "remaining_size")
                    if isinstance(snapshot.get(key), (str, int, float))
                ),
                None,
            )
            rows.append(
                {
                    "record_id": leg.id,
                    "leg_index": leg.leg_index,
                    "local_status": leg.status,
                    "snapshot_present": bool(snapshot),
                    "exchange_status": (
                        str(exchange_status)[:64]
                        if exchange_status is not None
                        else None
                    ),
                    "exchange_size": (
                        str(exchange_size)[:64]
                        if exchange_size is not None
                        else None
                    ),
                }
            )
        return rows

    def exchange_snapshot(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            batch = load_management_batch(session, incident)
            rows = stored_exchange_state(session, incident)
            return {
                "data": {
                    "incident_id": incident.id,
                    "snapshot_kind": "durable_last_observed",
                    "applicable": batch is not None,
                    "legs": rows,
                },
                "evidence_refs": [f"incident:{incident.id}"],
            }

    def compare_local_exchange(*, incident_id: int):
        live_comparison = (
            exchange_snapshot_refresh.consume_comparison(
                incident_id=int(incident_id)
            )
            if exchange_snapshot_refresh is not None
            else None
        )
        if live_comparison is not None:
            return {
                "data": {
                    "incident_id": int(incident_id),
                    **live_comparison,
                },
                "evidence_refs": [
                    f"incident:{int(incident_id)}",
                    f"exchange-snapshot:{int(incident_id)}",
                ],
            }
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            batch = load_management_batch(session, incident)
            rows = stored_exchange_state(session, incident)
            comparison = build_local_exchange_comparison(rows)
            return {
                "data": {
                    "incident_id": incident.id,
                    "comparison_kind": "local_vs_durable_last_observed",
                    "applicable": batch is not None,
                    **comparison,
                },
                "evidence_refs": [f"incident:{incident.id}"],
            }

    def prior_attempts(*, incident_id: int):
        with session_factory() as session:
            current = load_incident(session, incident_id)
            rows = (
                session.query(RuntimeIncident)
                .filter(
                    RuntimeIncident.fingerprint == current.fingerprint,
                    RuntimeIncident.id != current.id,
                )
                .order_by(
                    RuntimeIncident.generation.desc(),
                    RuntimeIncident.id.desc(),
                )
                .limit(10)
                .all()
            )
            summary = build_prior_attempts_summary(
                [
                    {
                        "incident_id": row.id,
                        "generation": row.generation,
                        "status": row.status,
                        "recovery_status": row.recovery_status,
                        "agent_attempt_count": row.agent_attempt_count,
                    }
                    for row in rows
                ]
            )
            return {
                "data": {
                    "incident_id": current.id,
                    **summary,
                },
                "evidence_refs": [
                    f"incident:{row.id}" for row in rows
                ] or [f"incident:{current.id}"],
            }

    def protection_summary(*, incident_id: int):
        with session_factory() as session:
            incident = load_incident(session, incident_id)
            protection = load_protection_incident(session, incident)
            evidence = {}
            if protection is not None:
                try:
                    loaded = json.loads(protection.evidence_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    loaded = {}
                if isinstance(loaded, dict):
                    evidence = loaded
            return {
                "data": {
                    "incident_id": incident.id,
                    "applicable": protection is not None,
                    "protection": (
                        build_protection_summary(evidence)
                        if protection is not None
                        else None
                    ),
                },
                "evidence_refs": (
                    [f"position-protection:{protection.id}"]
                    if protection is not None
                    else [f"incident:{incident.id}"]
                ),
            }

    providers = {
            "get_incident_summary": incident_summary,
            "get_lifecycle_state": lifecycle_state,
            "get_worker_state": worker_state,
            "get_service_audit_state": service_audit_state,
            "get_journal_summary": journal_summary,
            "get_exchange_snapshot": exchange_snapshot,
            "compare_local_exchange": compare_local_exchange,
            "get_prior_attempts": prior_attempts,
            "get_protection_summary": protection_summary,
        }
    broker_sources = {
        "message_evidence": message_evidence,
        "database_projection": lifecycle_state,
        "processing_timeline": worker_state,
        "journal_summary": journal_summary,
        "deployed_code": deployed_code,
        "configuration_state": service_audit_state,
        "exchange_snapshot": compare_local_exchange,
        "telegram_evidence": incident_summary,
        "prior_incidents": prior_attempts,
    }
    broker = InvestigationBroker(
        providers={
            kind: (
                lambda request, source=source: source(
                    incident_id=request.incident_id
                )
            )
            for kind, source in broker_sources.items()
        },
        incident_exists=lambda incident_id: (
            get_runtime_incident(session_factory, incident_id=incident_id)
            is not None
        ),
        audit_recorder=build_sqlalchemy_audit_recorder(session_factory),
        clock=lambda: datetime.now(UTC),
    )
    for evidence_kind in BROAD_READ_ONLY_EVIDENCE_KINDS:
        providers[f"investigate_{evidence_kind}"] = build_broker_tool_provider(
            broker, evidence_kind=evidence_kind
        )
    return RuntimeAgentToolRegistry(
        providers=providers,
        max_output_bytes=max_output_bytes,
    )


def _build_runtime_agent_action_handlers(
    tools: RuntimeAgentToolRegistry,
    *,
    exchange_snapshot_refresh: (
        RuntimeAgentExchangeSnapshotRefresh | None
    ) = None,
    production_audit_refresh: (
        RuntimeAgentProductionAuditRefresh | None
    ) = None,
    telegram_evidence_refresh: (
        RuntimeAgentTelegramEvidenceRefresh | None
    ) = None,
):
    """Wire only concrete, reviewed Phase 6 actions into production entrypoints."""

    def build_read_only_reconciliation_plan(
        *,
        incident_id: int,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> bool:
        del idempotency_key, expected_fingerprint
        comparison = tools.execute(
            "compare_local_exchange",
            {"incident_id": int(incident_id)},
            expected_incident_id=int(incident_id),
        )
        return (
            comparison.data.get("comparison_kind")
            == "local_vs_durable_last_observed"
            and comparison.data.get("applicable") is True
        )

    handlers = {
        "build_read_only_reconciliation_plan": (
            build_read_only_reconciliation_plan
        )
    }
    if exchange_snapshot_refresh is not None:

        def refresh_read_only_exchange_snapshot(
            *,
            incident_id: int,
            idempotency_key: str,
            expected_fingerprint: str,
        ) -> bool:
            return exchange_snapshot_refresh.refresh(
                incident_id=int(incident_id),
                idempotency_key=str(idempotency_key),
                expected_fingerprint=str(expected_fingerprint),
            )

        handlers["refresh_read_only_exchange_snapshot"] = (
            refresh_read_only_exchange_snapshot
        )
    if production_audit_refresh is not None:

        def rerun_production_audit(
            *,
            incident_id: int,
            idempotency_key: str,
            expected_fingerprint: str,
        ) -> bool:
            return production_audit_refresh.rerun(
                incident_id=int(incident_id),
                idempotency_key=str(idempotency_key),
                expected_fingerprint=str(expected_fingerprint),
            )

        handlers["rerun_production_audit"] = rerun_production_audit
    if telegram_evidence_refresh is not None:

        def fetch_missing_telegram_evidence(
            *,
            incident_id: int,
            idempotency_key: str,
            expected_fingerprint: str,
        ) -> bool:
            return telegram_evidence_refresh.refresh(
                incident_id=int(incident_id),
                idempotency_key=str(idempotency_key),
                expected_fingerprint=str(expected_fingerprint),
            )

        fetch_missing_telegram_evidence.is_applicable = (  # type: ignore[attr-defined]
            telegram_evidence_refresh.is_applicable
        )
        handlers["fetch_missing_telegram_evidence"] = (
            fetch_missing_telegram_evidence
        )
    return handlers


def _read_runtime_agent_exchange_snapshot() -> dict[str, Any]:
    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            "http://127.0.0.1:8000/api/runtime-agent/"
            "read-only-exchange-snapshot"
        )
        response.raise_for_status()
        return response.json()


def _read_runtime_agent_production_audit() -> dict[str, Any]:
    with httpx.Client(timeout=25.0, trust_env=False) as client:
        response = client.post(
            "http://127.0.0.1:8000/api/runtime-agent/"
            "read-only-production-audit"
        )
        response.raise_for_status()
        return response.json()


def _build_runtime_agent_production_audit_refresh(
) -> RuntimeAgentProductionAuditRefresh:
    return RuntimeAgentProductionAuditRefresh(
        runner=_read_runtime_agent_production_audit
    )


def _read_runtime_agent_telegram_evidence(
    channel: str,
) -> dict[str, Any]:
    if channel not in {"system_operator", "notification"}:
        raise ValueError("invalid Telegram evidence channel")
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        response = client.post(
            "http://127.0.0.1:8000/api/runtime-agent/"
            "read-only-telegram-evidence",
            json={"channel": channel},
        )
        response.raise_for_status()
        return response.json()


def _build_runtime_agent_telegram_evidence_refresh(
    session_factory,
) -> RuntimeAgentTelegramEvidenceRefresh:
    return RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=_read_runtime_agent_telegram_evidence,
    )


@app.command("runtime-incident-agent-evaluate")
def runtime_incident_agent_evaluate(
    corpus_path: Path = typer.Option(
        Path("tests/fixtures/runtime_incidents"),
        "--corpus-path",
    ),
) -> None:
    """Run the deterministic Phase 4 offline evaluation corpus."""

    cases = load_runtime_agent_corpus(corpus_path)
    summary = summarize_runtime_agent_evaluations(
        [
            evaluate_runtime_agent_case(case, case.reviewed_output)
            for case in cases
        ]
    )
    typer.echo(
        json.dumps(
            summary,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if not summary["all_passed"]:
        raise typer.Exit(code=1)


@app.command("runtime-incident-agent-once")
def runtime_incident_agent_once(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
) -> None:
    """Diagnose at most one runtime incident through the dormant read-only agent."""

    runtime_config = load_runtime_incident_config(environment_only=True)
    session_factory = create_session_factory(database_path)
    exchange_snapshot_refresh = RuntimeAgentExchangeSnapshotRefresh(
        reader=_read_runtime_agent_exchange_snapshot
    )
    production_audit_refresh = (
        _build_runtime_agent_production_audit_refresh()
    )
    telegram_evidence_refresh = (
        _build_runtime_agent_telegram_evidence_refresh(session_factory)
    )
    tools = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=runtime_config.agent_max_tool_output_bytes,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
        production_audit_refresh=production_audit_refresh,
        telegram_evidence_refresh=telegram_evidence_refresh,
        deployed_code_version=runtime_config.agent_deployed_code_version,
    )
    action_handlers = _build_runtime_agent_action_handlers(
        tools,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
        production_audit_refresh=production_audit_refresh,
        telegram_evidence_refresh=telegram_evidence_refresh,
    )
    worker_config = RuntimeAgentWorkerConfig(
        enabled=runtime_config.agent_enabled,
        incident_types=runtime_config.agent_incident_types,
        message_operation_enabled=(
            runtime_config.message_operation_agent_enabled
        ),
        message_operation_after_contract_id=(
            runtime_config.message_operation_agent_after_contract_id
        ),
        deployed_code_version=runtime_config.agent_deployed_code_version,
        max_tool_steps=runtime_config.agent_max_tool_steps,
        max_wall_seconds=runtime_config.agent_max_wall_seconds,
        max_prompt_bytes=runtime_config.agent_max_prompt_bytes,
        max_model_output_bytes=runtime_config.agent_max_tool_output_bytes,
        claim_lease_seconds=runtime_config.agent_claim_lease_seconds,
        shadow_playbooks=runtime_config.agent_shadow_playbooks,
        actions_enabled=runtime_config.agent_actions_enabled,
        action_playbooks=runtime_config.agent_action_playbooks,
        action_circuit_threshold=(
            runtime_config.agent_action_circuit_threshold
        ),
        token_budget_enabled=runtime_config.agent_token_budget_enabled,
        per_incident_token_limit=runtime_config.agent_per_incident_token_limit,
        daily_token_limit=runtime_config.agent_daily_token_limit,
        max_completion_tokens=runtime_config.agent_max_completion_tokens,
    )
    if runtime_config.agent_enabled:
        llm_config = load_runtime_agent_llm_config()

        def model_turn(**kwargs):
            return request_structured_chat_turn(
                config=llm_config,
                messages=kwargs["messages"],
                tool_schemas=kwargs["tool_schemas"],
                timeout_seconds=kwargs["timeout_seconds"],
                max_completion_tokens=kwargs.get("max_completion_tokens"),
                usage_callback=kwargs.get("usage_callback"),
            )

    else:
        def model_turn(**kwargs):
            raise RuntimeError("disabled runtime agent cannot call the model")

    result = run_runtime_agent_once(
        session_factory,
        config=worker_config,
        tools=tools,
        action_handlers=action_handlers,
        model_turn=model_turn,
    )
    typer.echo(
        json.dumps(
            {
                "incident_id": result.incident_id,
                "status": result.status,
                "tool_steps": result.tool_steps,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@app.command("runtime-incident-agent-worker")
def runtime_incident_agent_worker(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    poll_seconds: float = typer.Option(
        5.0, "--poll-seconds", min=0.25, max=60.0
    ),
) -> None:
    """Run the independently supervised runtime incident sidecar loop."""

    runtime_config = load_runtime_incident_config(environment_only=True)
    if not runtime_config.agent_enabled:
        typer.echo(
            '{"incident_id":null,"status":"disabled","tool_steps":0}'
        )
        return
    session_factory = create_session_factory(database_path)
    exchange_snapshot_refresh = RuntimeAgentExchangeSnapshotRefresh(
        reader=_read_runtime_agent_exchange_snapshot
    )
    production_audit_refresh = (
        _build_runtime_agent_production_audit_refresh()
    )
    telegram_evidence_refresh = (
        _build_runtime_agent_telegram_evidence_refresh(session_factory)
    )
    tools = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=runtime_config.agent_max_tool_output_bytes,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
        production_audit_refresh=production_audit_refresh,
        telegram_evidence_refresh=telegram_evidence_refresh,
        deployed_code_version=runtime_config.agent_deployed_code_version,
    )
    action_handlers = _build_runtime_agent_action_handlers(
        tools,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
        production_audit_refresh=production_audit_refresh,
        telegram_evidence_refresh=telegram_evidence_refresh,
    )
    worker_config = RuntimeAgentWorkerConfig(
        enabled=True,
        incident_types=runtime_config.agent_incident_types,
        message_operation_enabled=(
            runtime_config.message_operation_agent_enabled
        ),
        message_operation_after_contract_id=(
            runtime_config.message_operation_agent_after_contract_id
        ),
        deployed_code_version=runtime_config.agent_deployed_code_version,
        max_tool_steps=runtime_config.agent_max_tool_steps,
        max_wall_seconds=runtime_config.agent_max_wall_seconds,
        max_prompt_bytes=runtime_config.agent_max_prompt_bytes,
        max_model_output_bytes=runtime_config.agent_max_tool_output_bytes,
        claim_lease_seconds=runtime_config.agent_claim_lease_seconds,
        shadow_playbooks=runtime_config.agent_shadow_playbooks,
        actions_enabled=runtime_config.agent_actions_enabled,
        action_playbooks=runtime_config.agent_action_playbooks,
        action_circuit_threshold=(
            runtime_config.agent_action_circuit_threshold
        ),
        token_budget_enabled=runtime_config.agent_token_budget_enabled,
        per_incident_token_limit=runtime_config.agent_per_incident_token_limit,
        daily_token_limit=runtime_config.agent_daily_token_limit,
        max_completion_tokens=runtime_config.agent_max_completion_tokens,
    )
    llm_config = load_runtime_agent_llm_config()

    def run_once():
        return run_runtime_agent_once(
            session_factory,
            config=worker_config,
            tools=tools,
            action_handlers=action_handlers,
            model_turn=lambda **kwargs: request_structured_chat_turn(
                config=llm_config,
                messages=kwargs["messages"],
                tool_schemas=kwargs["tool_schemas"],
                timeout_seconds=kwargs["timeout_seconds"],
                max_completion_tokens=kwargs.get("max_completion_tokens"),
                usage_callback=kwargs.get("usage_callback"),
            ),
        )

    def report(result):
        typer.echo(
            json.dumps(
                {
                    "incident_id": result.incident_id,
                    "status": result.status,
                    "tool_steps": result.tool_steps,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    try:
        run_runtime_agent_loop(
            run_once=run_once,
            on_result=report,
            poll_seconds=poll_seconds,
        )
    except KeyboardInterrupt:
        return


@app.command("runtime-incident-scanner")
def runtime_incident_scanner(
    database_path: Path = typer.Option(Path("data/research.db"), "--database-path"),
    once: bool = typer.Option(False, "--once"),
) -> None:
    """Run the independent shadow-only invariant scanner."""
    config = load_runtime_scanner_config(environ=dict(os.environ), env_file_paths=[])
    if not config.enabled:
        typer.echo('{"abnormal":0,"observations":0,"status":"disabled"}')
        return
    if not config.shadow_only:
        raise typer.BadParameter("scanner must remain shadow-only in Phase 8R.3")
    if not database_path.is_file():
        raise typer.BadParameter("scanner database must already exist")
    session_factory = create_existing_session_factory(database_path)

    def cycle() -> None:
        observed_at = datetime.now(UTC)
        result = run_scanner_cycle(
            session_factory=session_factory,
            config=config,
            facts_by_rule=build_scanner_facts(
                session_factory, rules=config.rules, observed_at=observed_at
            ),
            observed_at=observed_at,
        )
        typer.echo(json.dumps({"status": "shadow", **result}, sort_keys=True, separators=(",", ":")))

    if once:
        cycle()
        return
    try:
        while True:
            cycle()
            import time
            time.sleep(config.interval_seconds)
    except KeyboardInterrupt:
        return


@app.command("message-operation-supervisor")
def message_operation_supervisor(
    database_path: Path = typer.Option(Path("data/research.db"), "--database-path"),
    shadow: bool = typer.Option(False, "--shadow"),
    once: bool = typer.Option(False, "--once"),
) -> None:
    """Run one future-only Phase 8R.5 deterministic shadow projection cycle."""

    config = load_message_operation_supervisor_config(
        environ=dict(os.environ)
    )
    if not config.enabled:
        typer.echo('{"status":"disabled"}')
        return
    if not config.shadow_only or not shadow:
        raise typer.BadParameter(
            "message operation supervisor must remain explicitly shadow-only"
        )
    runtime_incident_config = load_runtime_incident_config(
        environ=dict(os.environ)
    )
    if (
        message_operation_supervisor_policy_status(
            config, runtime_incident_config
        )
        != "valid"
    ):
        raise typer.BadParameter(
            "message operation supervisor capture policy invalid"
        )
    if not once:
        raise typer.BadParameter(
            "Phase 8R.5 permits only an explicitly bounded --once cycle"
        )
    if not database_path.is_file():
        raise typer.BadParameter("supervisor database must already exist")
    session_factory = create_existing_session_factory(database_path)
    observed_at = datetime.now(UTC)
    result = run_message_operation_supervisor_cycle(
        session_factory,
        after_raw_message_id=config.after_raw_message_id,
        capture_after_raw_message_id=config.after_raw_message_id,
        limit=config.batch_limit,
        observed_at=observed_at,
        runtime_incident_config=runtime_incident_config,
    )
    typer.echo(
        json.dumps(
            {"status": "shadow", **result},
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@app.command("runtime-incident-handoff")
def runtime_incident_handoff(
    incident_id: int = typer.Argument(..., min=1),
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
) -> None:
    """Print the bounded Codex handoff rebuilt from durable ledger fields."""

    session_factory = create_session_factory(database_path)
    try:
        handoff = load_runtime_incident_handoff(
            session_factory,
            incident_id=incident_id,
        )
    except RuntimeIncidentHandoffError as exc:
        raise typer.BadParameter(str(exc)) from exc
    artifact = load_latest_runtime_incident_handoff_artifact(
        session_factory, incident_id=incident_id
    )
    if artifact is not None:
        handoff = {
            **handoff,
            "stable_handoff_id": artifact.id,
            "diagnosis_revision": artifact.diagnosis_revision,
            "outcome_kind": artifact.outcome_kind,
            "content_sha256": artifact.content_fingerprint,
            "delivery_status": artifact.status,
        }
    typer.echo(
        json.dumps(
            handoff,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@app.command("audit-management-batches")
def audit_management_batches(
    database_path: Path = Path("data/research.db"),
    limit: int = typer.Option(20, min=1, max=100),
    output_format: str = typer.Option("text", "--output-format"),
    scratch_root: Path | None = typer.Option(
        None,
        "--scratch-root",
        hidden=True,
    ),
) -> None:
    """Read a bounded, redacted management batch and legacy queue summary."""

    normalized_format = output_format.strip().lower()
    if normalized_format not in {"json", "text"}:
        raise typer.BadParameter("output-format must be one of: text, json")
    try:
        audit = _audit_management_batches_read_only(
            database_path,
            limit=limit,
            scratch_root=scratch_root,
        )
    except (
        ManagementAuditSnapshotError,
        sqlite3.Error,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        MemoryError,
    ) as exc:
        if isinstance(exc, ManagementAuditSnapshotError):
            snapshot_status = exc.status
            reason = exc.reason
        elif isinstance(exc, sqlite3.Error):
            snapshot_status = "snapshot_unavailable"
            reason = "snapshot_audit_failed"
        else:
            snapshot_status = "snapshot_unavailable"
            reason = "audit_data_validation_failed"
        audit = {
            "snapshot_status": snapshot_status,
            "snapshot_validation": "not_run",
            "snapshot_reason": reason,
            "schema_status": "not_checked",
            "limit": limit,
            "counts": {
                "batches_total": 0,
                "informational_noop": 0,
                "terminal_blocked": 0,
                **{state: 0 for state in _MANAGEMENT_ALERT_STATES},
            },
            "batches_returned": 0,
            "batches_truncated": False,
            "all_history_legs_complete": False,
            "malformed_row_count": 0,
            "malformed_field_count": 0,
            "output_complete": False,
            "batches": [],
            "actionable_batches": {
                "total": 0,
                "returned": 0,
                "truncated": False,
                "items": [],
            },
            "legacy_pending_management": {
                "status": "not_checked",
                "candidate_pending_count": None,
                "scanned_count": 0,
                "total": None,
                "returned": 0,
                "truncated": False,
                "complete": False,
                "scan_truncated": False,
                "malformed_payload_count": 0,
                "malformed_row_count": 0,
                "malformed_field_count": 0,
                "by_action": {},
                "items": [],
            },
        }
        typer.echo(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
            if normalized_format == "json"
            else (
                f"snapshot_status={snapshot_status} "
                f"snapshot_validation=not_run snapshot_reason={reason}\n"
                "audit_payload="
                + json.dumps(
                    audit,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        )
        raise typer.Exit(code=1) from exc
    if normalized_format == "json":
        typer.echo(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return
    counts = audit["counts"]
    typer.echo(
        f"snapshot_status={audit['snapshot_status']} "
        f"snapshot_validation={audit['snapshot_validation']} "
        f"snapshot_copies_verified={audit['snapshot_copies_verified']} "
        f"snapshot_components={','.join(audit['snapshot_components'])}"
    )
    typer.echo(f"Management schema: {audit['schema_status']}")
    typer.echo(
        "Batch counts: "
        + ", ".join(
            f"{key}={counts[key]}"
            for key in (
                "batches_total",
                "informational_noop",
                "terminal_blocked",
                *_MANAGEMENT_ALERT_STATES,
            )
        )
    )
    typer.echo(
        f"Recent batches: {audit['batches_returned']}"
        f" (truncated={str(audit['batches_truncated']).lower()})"
    )
    for batch in audit["batches"]:
        source = batch["source"]
        typer.echo(
            f"- batch={batch['batch_ref']} state={batch['status']} "
            f"action={batch['effective_action']} mode={batch['execution_mode']} "
            f"source={source['chat_ref']}/{source['message_ref']} "
            f"legs_returned={batch['legs_returned']} "
            f"legs_truncated={str(batch['legs_truncated']).lower()}"
        )
        for leg in batch["legs"]:
            typer.echo(
                f"  leg={leg['leg_ref']} pos={leg['pos_ref']} state={leg['status']} "
                f"size={leg['preflight_size']} close={leg['planned_close_size']}"
            )
    legacy = audit["legacy_pending_management"]
    typer.echo(
        f"Legacy pending management: status={legacy['status']} "
        f"total={legacy['total']} returned={legacy['returned']} "
        f"truncated={str(legacy['truncated']).lower()} "
        f"complete={str(legacy['complete']).lower()} "
        f"scan_truncated={str(legacy['scan_truncated']).lower()} "
        f"candidate_pending_count={legacy['candidate_pending_count']} "
        f"scanned_count={legacy['scanned_count']}"
    )
    typer.echo(
        "by_action="
        + json.dumps(legacy["by_action"], ensure_ascii=False, sort_keys=True)
    )
    for item in legacy["items"]:
        typer.echo(
            f"- signal={item['signal_ref']} action={item['action']} "
            f"source={item['chat_ref']}/{item['message_ref']} "
            f"payload_status={item['payload_status']}"
        )
    typer.echo(
        f"malformed_row_count={audit['malformed_row_count']} "
        f"malformed_field_count={audit['malformed_field_count']} "
        f"output_complete={str(audit['output_complete']).lower()}"
    )
    # The compact payload makes text mode information-equivalent to JSON mode
    # without reintroducing any raw identifier or unbounded database text.
    typer.echo(
        "audit_payload="
        + json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


@app.command("audit-protection-incidents")
def audit_protection_incidents(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    limit: int = typer.Option(100, "--limit", min=1, max=100),
    output_format: str = typer.Option("text", "--output-format"),
) -> None:
    """Classify historical protection incidents without writes or alerts."""

    normalized_format = str(output_format).strip().lower()
    if normalized_format not in {"json", "text"}:
        raise typer.BadParameter("output-format must be one of: text, json")
    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise typer.BadParameter(
            "database path must name an existing file; no file was created"
        )
    try:
        with tempfile.TemporaryDirectory(
            prefix="protection-incident-audit-"
        ) as temporary:
            snapshot_path, snapshot_info = _build_stable_private_snapshots(
                resolved_path, Path(temporary)
            )
            snapshot_session_factory = create_existing_session_factory(snapshot_path)
            try:
                exchange_snapshot = (
                    load_deepcoin_execution_reconciliation_snapshot_read_only(
                        snapshot_session_factory,
                        client=build_deepcoin_client_from_env(),
                    )
                )
            except Exception:
                exchange_snapshot = SimpleNamespace(
                    positions=[],
                    pending_trigger_orders=[],
                    errors={"exchange_snapshot": "unavailable"},
                )
            audit = audit_protection_incident_convergence(
                snapshot_session_factory,
                snapshot=exchange_snapshot,
                limit=limit,
                database_evidence_stable=(
                    snapshot_info.get("snapshot_status") == "stable"
                    and snapshot_info.get("snapshot_validation") == "ok"
                ),
            )
            audit = {**snapshot_info, **audit}
    except (ManagementAuditSnapshotError, OSError, sqlite3.Error):
        audit = {
            "schema_version": 1,
            "mode": "read_only",
            "snapshot_status": "snapshot_unavailable",
            "snapshot_validation": "not_run",
            "limit": limit,
            "counts": {
                name: 0 for name in PROTECTION_INCIDENT_CLASSIFICATIONS
            },
            "incident_total": 0,
            "incidents_returned": 0,
            "incidents_truncated": False,
            "exchange_snapshot_complete": False,
            "database_evidence_stable": False,
            "output_complete": False,
            "incidents": [],
        }
    if normalized_format == "json":
        typer.echo(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return
    typer.echo(
        " ".join(
            (
                f"snapshot_status={audit.get('snapshot_status', 'unknown')}",
                f"output_complete={str(audit['output_complete']).lower()}",
                f"incident_total={audit['incident_total']}",
                *(f"{name}={audit['counts'][name]}" for name in PROTECTION_INCIDENT_CLASSIFICATIONS),
            )
        )
    )
    for item in audit["incidents"]:
        typer.echo(
            f"- incident={item['incident_ref']} position={item['position_ref']} "
            f"classification={item['classification']} "
            f"type={item['incident_type_ref']}"
        )


@app.command("repair-position-attribution")
def repair_position_attribution(
    database_path: Path = Path("data/research.db"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
) -> None:
    """Plan or explicitly apply audited position-attribution repairs."""

    session_factory = create_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime.now(UTC),
    )
    typer.echo("APPLY" if apply else "DRY RUN")
    typer.echo(
        json.dumps(
            asdict(plan),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    if not apply:
        return
    if plan.has_actions and not expected_fingerprint:
        typer.echo(
            "Refusing apply: --expected-fingerprint is required for a nonempty plan.",
            err=True,
        )
        raise typer.Exit(code=2)
    if plan.unresolved_conflicts:
        typer.echo("Refusing apply: unresolved attribution conflicts remain.", err=True)
        raise typer.Exit(code=2)
    result = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=expected_fingerprint,
    )
    typer.echo(f"Applied {result.applied} repair action(s).")


@app.command("repair-historical-state-convergence")
def repair_historical_state_convergence(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    expected_action_count: int | None = typer.Option(
        None, "--expected-action-count"
    ),
    confirmation_token: str | None = typer.Option(
        None, "--confirmation-token"
    ),
) -> None:
    """Plan or explicitly apply local-only historical state convergence."""

    session_factory = create_existing_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    snapshot = load_historical_state_repair_snapshot_read_only(
        session_factory,
        client=client,
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=datetime.now(UTC),
    )
    payload = asdict(plan)
    payload["action_count"] = plan.action_count
    typer.echo("APPLY" if apply else "DRY RUN")
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    if not apply and plan.conflicts:
        typer.echo("Refusing dry run: unresolved repair conflicts remain.", err=True)
        raise typer.Exit(code=2)
    if not apply:
        return
    missing = []
    if not expected_fingerprint:
        missing.append("--expected-fingerprint")
    if expected_action_count is None:
        missing.append("--expected-action-count")
    if not confirmation_token:
        missing.append("--confirmation-token")
    if missing:
        typer.echo(
            "Refusing apply: required gate(s) missing: " + ", ".join(missing),
            err=True,
        )
        raise typer.Exit(code=2)
    if plan.conflicts:
        typer.echo("Refusing apply: unresolved repair conflicts remain.", err=True)
        raise typer.Exit(code=2)
    try:
        result = apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: (
                load_historical_state_repair_snapshot_read_only(
                    session_factory,
                    client=client,
                )
            ),
            expected_fingerprint=expected_fingerprint,
            expected_action_count=expected_action_count,
            confirmation_token=confirmation_token,
            applied_at=datetime.now(UTC),
        )
    except HistoricalStateRepairRefused as exc:
        typer.echo(f"Refusing apply: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"Applied {result.applied_actions} historical repair action(s). "
        f"Audit event {result.audit_event_id}."
    )


@app.command("recover-management-history")
def recover_management_history(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    batch_id: int = typer.Option(..., "--batch-id", min=1),
    apply: bool = typer.Option(False, "--apply"),
    evidence_fingerprint: str | None = typer.Option(
        None, "--evidence-fingerprint"
    ),
) -> None:
    """Dry-run or converge one exact paused management batch."""

    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise typer.BadParameter(
            "database path must name an existing file; no file was created"
        )
    session_factory = create_existing_session_factory(resolved_path)
    client = build_deepcoin_client_from_env()
    snapshot = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory,
        client=client,
    )
    _load_management_history_position_history(
        session_factory,
        batch_id=batch_id,
        client=client,
        snapshot=snapshot,
    )
    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=snapshot,
        planned_at=datetime.now(UTC),
    )
    if not apply:
        typer.echo(
            json.dumps(
                {"mode": "dry_run", "decision": asdict(decision)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if decision.status != "ready":
            raise typer.Exit(code=2)
        return
    if evidence_fingerprint is None:
        typer.echo(
            json.dumps(
                {
                    "mode": "apply",
                    "status": "refused",
                    "reason_code": "evidence_fingerprint_required",
                    "decision": asdict(decision),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise typer.Exit(code=2)
    try:
        result = apply_management_history_recovery(
            session_factory,
            decision=decision,
            expected_fingerprint=evidence_fingerprint,
            applied_at=datetime.now(UTC),
        )
    except ManagementHistoryRecoveryConflict:
        typer.echo(
            json.dumps(
                {
                    "mode": "apply",
                    "status": "refused",
                    "reason_code": "recovery_evidence_conflict",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise typer.Exit(code=2)
    typer.echo(
        json.dumps(
            {"mode": "apply", "result": asdict(result)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _load_management_history_position_history(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    client: Any,
    snapshot: Any,
) -> None:
    """Attach exact closed-position rows for old submitted management legs."""

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if batch is None:
            return
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.id)
            .all()
        )
        submitted_pos_ids = [
            str(leg.pos_id)
            for leg in legs
            if str(leg.status or "") != "planned"
        ]
        instrument_id = (
            f"{str(binding.symbol).upper()}-USDT-SWAP"
            if binding is not None
            else None
        )
    if not submitted_pos_ids:
        return
    method = getattr(client, "list_position_history", None)
    if method is None or instrument_id is None:
        snapshot.errors["position_history"] = "unavailable"
        return
    rows: list[dict[str, Any]] = []
    try:
        for pos_id in submitted_pos_ids:
            result = method(inst_id=instrument_id, pos_id=pos_id)
            if not isinstance(result, list) or not all(
                isinstance(row, dict) for row in result
            ):
                raise ValueError("invalid position history schema")
            rows.extend(result)
    except Exception:
        snapshot.errors["position_history"] = "unavailable"
        return
    snapshot.position_history = rows


@app.command("plan-current-protection-backfill")
def plan_current_protection_backfill(
    mapping_file: Path = typer.Option(..., "--mapping-file", exists=True, readable=True),
    database_path: Path = Path("data/research.db"),
    apply: bool = typer.Option(False, "--apply"),
    action_id: str | None = typer.Option(None, "--action-id"),
    pos_id: str | None = typer.Option(None, "--pos-id"),
    expected_fingerprint: str | None = typer.Option(None, "--expected-fingerprint"),
    confirmation_token: str | None = typer.Option(None, "--confirmation-token"),
) -> None:
    """Plan or apply explicitly supervised current TPSL mappings."""

    try:
        payload = json.loads(mapping_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("mapping file must contain JSON") from exc
    rows = payload.get("mappings") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise typer.BadParameter("mapping file must be a list or {'mappings': [...]}")
    mappings = [
        SupervisedProtectionMapping(
            order_id=str(row.get("order_id") or ""),
            pos_id=str(row.get("pos_id") or ""),
            evidence_hash=str(row.get("evidence_hash") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]
    client = build_deepcoin_client_from_env()
    positions = list(client.list_positions())
    instrument_ids = sorted(
        {
            str(row.get("instId") or row.get("instrumentId") or "").upper()
            for row in positions
            if isinstance(row, dict)
            and str(row.get("instId") or row.get("instrumentId") or "").strip()
        }
    )
    pending_orders: list[dict] = []
    for instrument_id in instrument_ids:
        pending_orders.extend(client.list_trigger_orders_pending(inst_id=instrument_id))
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        verified_order_ids = {
            str(row.order_id)
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.venue == "deepcoin")
            .filter(PositionProtectionLedger.status == "verified")
            .all()
        }
    plan = build_current_protection_backfill_plan(
        mappings=mappings,
        positions=positions,
        pending_orders=pending_orders,
        verified_order_ids=verified_order_ids,
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "database_path": str(database_path),
                "plan": asdict(plan),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not apply:
        return
    if plan.actions and (
        not action_id
        or not pos_id
        or not expected_fingerprint
        or not confirmation_token
    ):
        raise typer.BadParameter(
            "--action-id, --pos-id, --expected-fingerprint, and "
            "--confirmation-token are required for apply"
        )
    result = apply_current_protection_backfill_plan(
        session_factory,
        plan,
        action_id=action_id or "",
        pos_id=pos_id or "",
        expected_fingerprint=expected_fingerprint or "",
        confirmation_token=confirmation_token or "",
    )
    typer.echo(f"Applied {result.applied} supervised current protection ledger row(s).")


@app.command("repair-entry-protection-ledger")
def repair_entry_protection_ledger(
    database_path: Path = Path("data/research.db"),
    apply: bool = typer.Option(False, "--apply"),
    action_id: str | None = typer.Option(None, "--action-id"),
    confirmation_token: str | None = typer.Option(
        None, "--confirmation-token"
    ),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    binding_id: int | None = typer.Option(None, "--binding-id"),
    event_id: int | None = typer.Option(None, "--event-id"),
    pos_id: str | None = typer.Option(None, "--pos-id"),
    include_trigger_entries: bool = typer.Option(False, "--include-trigger-entries"),
) -> None:
    """Dry-run or repair historical entry-protection TPSL ledger rows."""

    if apply and (
        not include_trigger_entries
        or binding_id is None
        or not pos_id
        or not action_id
        or not expected_fingerprint
        or not confirmation_token
    ):
        typer.echo(
            "Refusing apply: --include-trigger-entries, --binding-id, "
            "--pos-id, --action-id, --expected-fingerprint, and "
            "--confirmation-token are required.",
            err=True,
        )
        raise typer.Exit(code=2)
    session_factory = create_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime.now(UTC),
        binding_id=binding_id,
        event_id=event_id,
        pos_id=pos_id,
        include_trigger_entries=include_trigger_entries,
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "database_path": str(database_path),
                "plan": asdict(plan),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    if not apply:
        return
    if len(plan.actions) != 1:
        typer.echo(
            "Refusing apply: the current bounded repair plan must contain "
            "exactly one action.",
            err=True,
        )
        raise typer.Exit(code=2)
    current_plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime.now(UTC),
        binding_id=binding_id,
        event_id=event_id,
        pos_id=pos_id,
        include_trigger_entries=include_trigger_entries,
    )
    result = apply_entry_protection_ledger_repair_plan(
        session_factory,
        current_plan,
        action_id=action_id or "",
        pos_id=pos_id or "",
        expected_fingerprint=expected_fingerprint or "",
        confirmation_token=confirmation_token or "",
    )
    typer.echo(f"Applied {result.applied} entry protection ledger repair(s).")


@app.command("repair-take-profit-protection-leg")
def repair_take_profit_protection_leg(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    apply: bool = typer.Option(False, "--apply"),
    action_id: str | None = typer.Option(None, "--action-id"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    confirmation_token: str | None = typer.Option(
        None, "--confirmation-token"
    ),
) -> None:
    """Converge one verified existing TP order onto its logical leg."""

    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        typer.echo(
            "Refusing repair: database does not exist; no file was created.",
            err=True,
        )
        raise typer.Exit(code=2)
    if apply and (
        not action_id or not expected_fingerprint or not confirmation_token
    ):
        typer.echo(
            "Refusing apply: --action-id, --expected-fingerprint, and "
            "--confirmation-token are required.",
            err=True,
        )
        raise typer.Exit(code=2)
    session_factory = create_existing_session_factory(resolved_path)
    client = build_deepcoin_client_from_env()
    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=datetime.now(UTC),
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "database_path": str(resolved_path),
                "plan": asdict(plan),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    if not apply:
        return
    if len(plan.actions) != 1:
        typer.echo(
            "Refusing apply: the current repair plan must contain exactly "
            "one action.",
            err=True,
        )
        raise typer.Exit(code=2)
    result = apply_take_profit_protection_leg_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        action_id=action_id or "",
        expected_fingerprint=expected_fingerprint or "",
        confirmation_token=confirmation_token or "",
        applied_at=datetime.now(UTC),
    )
    typer.echo(
        f"Applied {result.applied} take-profit protection-leg repair(s)."
    )


def _redacted_entry_assembly_fingerprint_plan(plan: Any) -> dict[str, Any]:
    action = plan.action
    return {
        "action": (
            {
                "assembly_id": action.assembly_id,
                "execution_binding_id": action.execution_binding_id,
                "trade_signal_id": action.trade_signal_id,
                "old_fingerprint": action.old_fingerprint,
                "final_fingerprint": action.final_fingerprint,
                "repair_fingerprint": action.repair_fingerprint,
            }
            if action is not None
            else None
        ),
        "conflicts": list(plan.conflicts),
        "fingerprint": plan.fingerprint,
    }


@app.command("repair-entry-assembly-fingerprint")
def repair_entry_assembly_fingerprint(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    assembly_id: int = typer.Option(..., "--assembly-id"),
    execution_binding_id: int = typer.Option(..., "--execution-binding-id"),
    apply: bool = typer.Option(False, "--apply"),
    expected_plan_fingerprint: str | None = typer.Option(
        None, "--expected-plan-fingerprint"
    ),
) -> None:
    """Plan or append one bounded entry-assembly fingerprint repair event."""

    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        typer.echo(
            "Refusing repair: database does not exist; no file was created.",
            err=True,
        )
        raise typer.Exit(code=2)
    session_factory = create_existing_session_factory(resolved_path)
    plan = build_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=assembly_id,
        execution_binding_id=execution_binding_id,
    )
    redacted_plan = _redacted_entry_assembly_fingerprint_plan(plan)
    typer.echo(json.dumps(
        {
            "mode": "apply_plan" if apply else "dry_run",
            "plan": redacted_plan,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    if plan.action is None or plan.conflicts:
        typer.echo(
            "Refusing repair: the current plan must contain exactly one "
            "conflict-free action.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not apply:
        return
    if not expected_plan_fingerprint:
        typer.echo(
            "Refusing apply: --expected-plan-fingerprint is required.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        event_id = apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=assembly_id,
            execution_binding_id=execution_binding_id,
            expected_plan_fingerprint=expected_plan_fingerprint,
            applied_at=datetime.now(UTC),
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"Refusing apply: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(
        {"mode": "apply", "event_id": event_id},
        ensure_ascii=False,
        sort_keys=True,
    ))


def _backup_stop_conflicts_for_target(
    plan: BackupStopRepairPlan, *, pos_id: str
) -> tuple[dict[str, str], ...]:
    """Return only conflicts that block the position selected for an apply."""

    return tuple(
        conflict
        for conflict in plan.conflicts
        if str(conflict.get("pos_id") or "").strip() == pos_id
    )


@app.command("repair-backup-stops")
def repair_backup_stops(
    database_path: Path = Path("data/research.db"),
    deepcoin_contract_specs_path: Path = Path("config/deepcoin_contract_specs.yaml"),
    pos_id: str | None = typer.Option(None, "--pos-id"),
    action_id: str | None = typer.Option(None, "--action-id"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(None, "--expected-fingerprint"),
    confirmation_token: str | None = typer.Option(None, "--confirmation-token"),
) -> None:
    """Dry-run or apply one fingerprinted exact-position backup stop repair."""

    session_factory = create_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    contract_spec_provider = load_deepcoin_contract_specs(deepcoin_contract_specs_path)
    plan = build_backup_stop_repair_plan(
        session_factory,
        deepcoin_client=client,
        contract_spec_provider=contract_spec_provider,
        now=datetime.now(UTC),
    )
    typer.echo(json.dumps({
        "mode": "apply" if apply else "dry_run",
        "database_path": str(database_path),
        "plan": asdict(plan),
    }, ensure_ascii=False, indent=2, default=str))
    if not apply:
        return
    clean_pos_id = str(pos_id or "").strip()
    if (
        not clean_pos_id
        or not action_id
        or not expected_fingerprint
        or not confirmation_token
    ):
        typer.echo(
            "Refusing apply: --apply requires --action-id, --pos-id, "
            "--expected-fingerprint, and --confirmation-token.",
            err=True,
        )
        raise typer.Exit(code=2)
    if _backup_stop_conflicts_for_target(plan, pos_id=clean_pos_id):
        typer.echo(
            "Refusing apply: target position has unresolved backup-stop conflicts.",
            err=True,
        )
        raise typer.Exit(code=2)
    result = apply_backup_stop_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        contract_spec_provider=contract_spec_provider,
        pos_id=clean_pos_id,
        action_id=action_id,
        expected_fingerprint=expected_fingerprint,
        confirmation_token=confirmation_token,
        now=datetime.now(UTC),
    )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("cancel-reviewed-legacy-conditionals")
def cancel_reviewed_legacy_conditionals(
    database_path: Path = Path("data/research.db"),
    pos_id: str | None = typer.Option(None, "--pos-id"),
    action_id: str | None = typer.Option(None, "--action-id"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    confirmation_token: str | None = typer.Option(
        None, "--confirmation-token"
    ),
) -> None:
    """Plan or cancel one explicitly reviewed legacy conditional order."""

    session_factory = create_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=REVIEWED_LEGACY_CONDITIONAL_TARGETS,
        now=datetime.now(UTC),
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "database_path": str(database_path),
                "plan": asdict(plan),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    if not apply:
        return
    clean_pos_id = str(pos_id or "").strip()
    if (
        not clean_pos_id
        or not action_id
        or not expected_fingerprint
        or not confirmation_token
    ):
        raise typer.BadParameter(
            "--apply requires --action-id, --pos-id, "
            "--expected-fingerprint, and --confirmation-token"
        )
    if plan.conflicts:
        raise typer.BadParameter(
            "reviewed target set has unresolved cancellation conflicts"
        )
    result = apply_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=REVIEWED_LEGACY_CONDITIONAL_TARGETS,
        pos_id=clean_pos_id,
        action_id=action_id,
        expected_fingerprint=expected_fingerprint,
        confirmation_token=confirmation_token,
        now=datetime.now(UTC),
    )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    if result.status != "cancelled":
        raise typer.Exit(code=2)


@app.command("finalize-cancelled-pending-entries")
def finalize_cancelled_pending_entries(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    backup_path: Path = typer.Option(..., "--backup-path"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
) -> None:
    """Finalize reviewed entries after the operator cancels all at Deepcoin."""

    session_factory = create_existing_session_factory(database_path)
    client = build_deepcoin_client_from_env()
    runtime = SystemRuntimeAdapter(expected_uid=0)

    def runtime_guard() -> None:
        require_stopped_legacy_runtime_boundary(runtime)

    try:
        with exclusive_runtime_control_lock(expected_uid=0):
            plan = build_manual_pending_entry_reconciliation_plan(
                session_factory,
                deepcoin_client=client,
                targets=REVIEWED_PENDING_ENTRY_TARGETS,
                runtime_guard=runtime_guard,
            )
            typer.echo(
                json.dumps(
                    {
                        "mode": "apply" if apply else "dry_run",
                        "status": plan.status,
                        "reason_code": plan.reason_code,
                        "target_count": len(plan.target_order_ids),
                        "evidence_sha256": plan.evidence_sha256,
                        "fingerprint": plan.fingerprint,
                    },
                    sort_keys=True,
                )
            )
            if plan.status == "completed":
                return
            if plan.status != "ready":
                raise typer.Exit(code=2)
            if not apply:
                return
            if not expected_fingerprint:
                raise typer.BadParameter("--apply requires --expected-fingerprint")
            result = apply_manual_pending_entry_reconciliation(
                session_factory,
                database_path=database_path,
                backup_path=backup_path,
                deepcoin_client=client,
                targets=REVIEWED_PENDING_ENTRY_TARGETS,
                expected_fingerprint=expected_fingerprint,
                runtime_guard=runtime_guard,
            )
            typer.echo(
                json.dumps(
                    {
                        "status": result.status,
                        "terminalized_count": result.terminalized_count,
                        "authority_seeded": result.authority_seeded,
                        "backup_path": str(result.backup_path),
                        "backup_sha256": result.backup_sha256,
                    },
                    sort_keys=True,
                )
            )
    except ActivationError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("repair-position-management")
def repair_position_management(
    database_path: Path = Path("data/research.db"),
    deepcoin_contract_specs_path: Path = Path("config/deepcoin_contract_specs.yaml"),
    apply: bool = typer.Option(False, "--apply"),
    action_id: str | None = typer.Option(None, "--action-id"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
) -> None:
    """Dry-run or apply one exact fingerprinted management remediation."""

    if apply and (not action_id or not expected_fingerprint):
        typer.echo(
            "Refusing apply: --action-id and --expected-fingerprint are required.",
            err=True,
        )
        raise typer.Exit(code=2)
    if apply:
        session_factory = create_session_factory(database_path)
    else:
        resolved_database_path = database_path.resolve()
        if not resolved_database_path.is_file():
            typer.echo(
                "Refusing dry-run: database does not exist; no file was created.",
                err=True,
            )
            raise typer.Exit(code=2)
        engine = create_engine(
            "sqlite+pysqlite://",
            creator=lambda: sqlite3.connect(
                f"file:{resolved_database_path}?mode=ro",
                uri=True,
            ),
        )
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = build_deepcoin_client_from_env()
    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime.now(UTC),
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "database_path": str(database_path),
                "plan": asdict(plan),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    if not apply:
        return
    contract_spec_provider = load_deepcoin_contract_specs(
        deepcoin_contract_specs_path
    )
    try:
        result = apply_position_management_remediation_action(
            session_factory,
            deepcoin_client=client,
            action_id=str(action_id),
            expected_fingerprint=str(expected_fingerprint),
            now=datetime.now(UTC),
            contract_spec_provider=contract_spec_provider,
        )
    except ValueError as exc:
        typer.echo(f"Refusing apply: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("recover-position-management-liveness")
def recover_position_management_liveness(
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    pos_id: str = typer.Option(..., "--pos-id"),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None, "--expected-fingerprint"
    ),
    deepcoin_contract_specs_path: Path = typer.Option(
        Path("config/deepcoin_contract_specs.yaml"),
        "--deepcoin-contract-specs-path",
    ),
) -> None:
    """Dry-run or apply one exact reviewed management-liveness recovery."""

    normalized_pos_id = str(pos_id or "").strip()
    if not normalized_pos_id:
        raise typer.BadParameter("--pos-id must name one exact position")
    if apply and not expected_fingerprint:
        typer.echo(
            "Refusing apply: --expected-fingerprint is required.", err=True
        )
        raise typer.Exit(code=2)
    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        typer.echo(
            "Refusing recovery: database does not exist; no file was created.",
            err=True,
        )
        raise typer.Exit(code=2)
    session_factory = create_existing_session_factory(resolved_path)
    client = build_deepcoin_client_from_env()
    contract_spec_provider = load_deepcoin_contract_specs(
        deepcoin_contract_specs_path
    )
    if apply:
        try:
            result = apply_position_management_liveness_recovery(
                session_factory,
                pos_id=normalized_pos_id,
                expected_fingerprint=str(expected_fingerprint),
                deepcoin_client=client,
                contract_spec_provider=contract_spec_provider,
                applied_at=datetime.now(UTC),
            )
        except ValueError as exc:
            typer.echo(f"Refusing apply: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(json.dumps(
            {"mode": "apply", "result": asdict(result)},
            ensure_ascii=False, sort_keys=True,
        ))
        return
    plan = build_position_management_liveness_recovery_plan(
        session_factory,
        pos_id=normalized_pos_id,
        deepcoin_client=client,
        contract_spec_provider=contract_spec_provider,
        planned_at=datetime.now(UTC),
    )
    typer.echo(json.dumps(
        {"mode": "dry_run", "plan": asdict(plan)},
        ensure_ascii=False, indent=2, sort_keys=True,
    ))


@app.command("archive-unbound-holdings")
def archive_unbound_holdings(
    lifecycle_ids: list[int] = typer.Option(
        ...,
        "--lifecycle-id",
        "-i",
        help="Strategy lifecycle ID to archive. Repeat for multiple IDs.",
    ),
    database_path: Path = Path("data/research.db"),
    apply: bool = typer.Option(False, "--apply"),
    expected_count: int | None = typer.Option(
        None,
        "--expected-count",
        min=1,
        help="Refuse --apply unless this equals the number of unique lifecycle IDs.",
    ),
    note: str = typer.Option(
        "operator_unbound_holding_cleanup",
        "--note",
        help="Short note stored on archived lifecycle rows.",
    ),
) -> None:
    """Archive entered lifecycle rows that have no Deepcoin execution ownership."""

    unique_ids = list(dict.fromkeys(int(item) for item in lifecycle_ids))
    if apply and expected_count is None:
        typer.echo("Refusing apply: --expected-count is required.", err=True)
        raise typer.Exit(code=2)
    if apply and expected_count != len(unique_ids):
        typer.echo(
            f"Refusing apply: expected-count={expected_count} but ids={len(unique_ids)}.",
            err=True,
        )
        raise typer.Exit(code=2)

    session_factory = create_session_factory(database_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict[str, object]] = []
    refused: list[dict[str, object]] = []

    with session_factory() as session:
        for lifecycle_id in unique_ids:
            lifecycle = session.get(StrategyLifecycle, lifecycle_id)
            row: dict[str, object] = {
                "lifecycle_id": lifecycle_id,
                "status": "would_archive",
                "reasons": [],
            }
            reasons: list[str] = []
            if lifecycle is None:
                reasons.append("lifecycle_not_found")
            else:
                row.update(
                    {
                        "chat_id": lifecycle.chat_id,
                        "message_id": lifecycle.message_id,
                        "symbol": lifecycle.symbol,
                        "side": lifecycle.side,
                        "lifecycle_status": lifecycle.lifecycle_status,
                        "execution_binding_id": lifecycle.execution_binding_id,
                    }
                )
                if lifecycle.lifecycle_status != "entered":
                    reasons.append("lifecycle_not_entered")
                if lifecycle.execution_binding_id is not None:
                    reasons.append("lifecycle_has_execution_binding")
                matching_bindings = (
                    session.query(ExecutionBinding)
                    .filter(ExecutionBinding.venue == "deepcoin")
                    .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
                    .filter(ExecutionBinding.message_id == lifecycle.message_id)
                    .filter(ExecutionBinding.symbol == lifecycle.symbol)
                    .filter(ExecutionBinding.side == lifecycle.side)
                    .count()
                )
                if matching_bindings:
                    reasons.append("matching_deepcoin_binding_exists")
                management_batches = (
                    session.query(StrategyManagementBatch)
                    .filter(StrategyManagementBatch.target_lifecycle_id == lifecycle.id)
                    .count()
                )
                if management_batches:
                    reasons.append("management_batch_exists")

            if reasons:
                row["status"] = "refused"
                row["reasons"] = reasons
                refused.append(row)
            rows.append(row)

        if apply and refused:
            typer.echo(
                json.dumps(
                    {
                        "mode": "apply",
                        "applied": 0,
                        "refused": refused,
                        "rows": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            typer.echo("Refusing apply: at least one lifecycle is unsafe to archive.", err=True)
            raise typer.Exit(code=2)

        applied = 0
        if apply:
            for row in rows:
                lifecycle = session.get(StrategyLifecycle, int(row["lifecycle_id"]))
                if lifecycle is None:
                    continue
                lifecycle.lifecycle_status = "invalidated"
                lifecycle.exit_reason = "context_invalidated"
                lifecycle.exited_at = now
                lifecycle.updated_at = now
                lifecycle.management_action = "operator_archived_unbound_holding"
                lifecycle.management_note = note[:512]
                if lifecycle.trade_idea_id is not None:
                    trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
                    if trade_idea is not None and trade_idea.status == "open":
                        trade_idea.status = "closed"
                        trade_idea.closed_at = now
                row["status"] = "archived"
                row["lifecycle_status"] = "invalidated"
                row["exit_reason"] = "context_invalidated"
                applied += 1
            session.commit()

    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "requested": len(unique_ids),
                "applied": applied,
                "refused": len(refused),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def _build_recovery_market_provider(market_provider: str):
    normalized = market_provider.strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized == "gate":
        return GateMarketDataProvider()
    if normalized == "binance":
        return BinanceMarketDataProvider()
    raise typer.BadParameter("market-provider must be one of: none, gate, binance")


def _parse_evidence_backfill_timestamp(
    value: str | None,
    *,
    option_name: str,
) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(
            f"{option_name} must be an ISO-8601 timestamp"
        ) from exc


@app.command("replay-mimo-v2")
def replay_mimo_v2(
    database: Path = typer.Option(..., "--database"),
    message_id_file: Path = typer.Option(..., "--message-id-file"),
    artifact_dir: Path = typer.Option(..., "--artifact-dir"),
    max_messages: int = typer.Option(200, "--max-messages", min=1, max=200),
    ai_config_path: Path = typer.Option(
        Path("config/ai_recognition.yaml"),
        "--ai-config-path",
    ),
    media_root: Path = typer.Option(Path("data/media"), "--media-root"),
) -> None:
    """Compare MiMo v1/v2 in an isolated database copy with no trade writes."""

    try:
        raw_message_ids = load_replay_message_ids(message_id_file)
        result = run_mimo_v2_replay(
            source_database=database,
            artifact_dir=artifact_dir,
            raw_message_ids=raw_message_ids,
            max_messages=max_messages,
            ai_recognition_config_path=ai_config_path,
            media_root=media_root,
        )
    except (LookupError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    if not result.passed:
        raise typer.Exit(code=2)


@app.command("backfill-mimo-evidence")
def backfill_mimo_evidence(
    database_path: Path = Path("data/research.db"),
    ai_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
    chat_ids: list[int] = typer.Option(
        [],
        "--chat-id",
        help="Telegram chat ID to backfill. Repeat for multiple chats.",
    ),
    use_configured_context_chats: bool = typer.Option(
        False,
        "--use-configured-context-chats",
        help="Also use the persisted contextual-resolution chat list.",
    ),
    start_at: str | None = typer.Option(None, "--start-at"),
    end_at: str | None = typer.Option(None, "--end-at"),
    limit: int = typer.Option(100, "--limit", min=1),
    scan_limit: int = typer.Option(1000, "--scan-limit", min=1),
    scan_cursor: str | None = typer.Option(None, "--scan-cursor"),
    delay_seconds: float = typer.Option(2.0, "--delay-seconds", min=0.0),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Backfill immutable MiMo evidence without applying strategy actions."""

    session_factory = create_session_factory(database_path)
    scoped_chat_ids = [int(chat_id) for chat_id in chat_ids]
    if use_configured_context_chats:
        settings = load_trading_settings(session_factory)
        scoped_chat_ids.extend(settings.context_resolution_live_chat_ids)
    scoped_chat_ids = sorted(set(scoped_chat_ids))
    if not scoped_chat_ids:
        raise typer.BadParameter(
            "at least one chat scope is required via --chat-id or "
            "--use-configured-context-chats"
        )
    parsed_start = _parse_evidence_backfill_timestamp(
        start_at,
        option_name="--start-at",
    )
    parsed_end = _parse_evidence_backfill_timestamp(
        end_at,
        option_name="--end-at",
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=scoped_chat_ids,
        media_root=media_root,
        start_at=parsed_start,
        end_at=parsed_end,
        limit=limit,
        retry_failed=retry_failed,
        scan_limit=scan_limit,
        scan_cursor=scan_cursor,
    )
    ai_config = load_ai_recognition_config(ai_config_path) if apply else None
    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=ai_config,
        media_root=media_root,
        apply=apply,
        delay_seconds=delay_seconds,
    )
    rows = (
        list(result.rows)
        if apply
        else [
            {
                "raw_message_id": item.raw_message_id,
                "chat_id": item.chat_id,
                "message_id": item.message_id,
                "status": item.status,
            }
            for item in plan.items
            if item.status == "process"
        ]
    )
    typer.echo(
        json.dumps(
            {
                **asdict(result),
                "chat_ids": list(plan.chat_ids),
                "start_at": plan.start_at,
                "end_at": plan.end_at,
                "limit": plan.limit,
                "scan_limit": plan.scan_limit,
                "scan_cursor": plan.scan_cursor,
                "next_scan_cursor": (
                    plan.scan_cursor
                    if apply and result.resume_required
                    else plan.next_scan_cursor
                ),
                "retry_failed": plan.retry_failed,
                "rows": rows,
            },
            ensure_ascii=False,
            default=str,
        )
    )


@app.command("resolve-context-once")
def resolve_context_once(
    database_path: Path = Path("data/research.db"),
    ai_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
) -> None:
    """Reanalyse at most one scheduled context item without exchange writes."""

    session_factory = create_session_factory(database_path)
    settings = load_trading_settings(session_factory)
    if not settings.context_resolution_enabled or not settings.live_management_execution_enabled:
        typer.echo(json.dumps({"status": "disabled"}, ensure_ascii=False))
        return
    ai_config = load_ai_recognition_config(ai_config_path)

    def reanalyze(raw_message_id: int, _fingerprint: str) -> dict[str, Any]:
        result = process_authoritative_message(
            session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=ai_config,
            media_root=media_root,
            auto_trade_executor=None,
            context_resolver=resolve_contextual_strategy,
            exchange_state_provider=lambda message_id, candidate_thread_ids=None: build_redacted_exchange_state(
                session_factory,
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
        return {"status": str(result.automation.get("status") or "completed")}

    def is_eligible(raw_message_id: int) -> bool:
        with session_factory() as session:
            raw = session.get(RawMessage, int(raw_message_id))
            return (
                raw is not None
                and settings.context_resolution_enabled_for_chat(int(raw.chat_id))
            )

    outcome = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda raw_message_id: (
            build_context_state_fingerprint(session_factory, raw_message_id)
        ),
        reanalyze=reanalyze,
        is_eligible=is_eligible,
    )
    typer.echo(json.dumps(outcome, ensure_ascii=False, default=str))


@app.command("export-dataset")
def export_dataset(
    output_path: Path = Path("exports/llm-dataset.jsonl"),
    database_path: Path = Path("data/research.db"),
    review_only: bool = False,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = False,
) -> None:
    """Export message-centered JSONL rows for model adjudication."""

    session_factory = create_session_factory(database_path)
    written_path = export_dataset_jsonl(
        session_factory,
        output_path,
        review_only=review_only,
        confidence_threshold=confidence_threshold,
        signal_like_only=signal_like_only,
    )
    typer.echo(f"Dataset written to {written_path}")


@app.command("export-llm-pack")
def export_llm_pack(
    output_dir: Path = Path("exports/llm-adjudication"),
    database_path: Path = Path("data/research.db"),
    review_only: bool = True,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = True,
) -> None:
    """Export dataset plus prompt/schema contract for model adjudication."""

    session_factory = create_session_factory(database_path)
    manifest = export_llm_adjudication_pack(
        session_factory,
        output_dir,
        review_only=review_only,
        confidence_threshold=confidence_threshold,
        signal_like_only=signal_like_only,
    )
    typer.echo(
        f"LLM pack written to {output_dir} ({manifest['record_count']} record(s))"
    )


@app.command("export-llm-submit-sample")
def export_llm_submit_sample(
    pack_dir: Path = Path("exports/llm-adjudication"),
    output_path: Path = Path("exports/llm-adjudication/submit-sample.md"),
    limit: int = 5,
) -> None:
    """Export a copy-ready small submission sample for model adjudication."""

    written_path = export_llm_submission_sample(pack_dir, output_path, limit=limit)
    typer.echo(f"LLM submission sample written to {written_path}")


@app.command("import-llm-results")
def import_llm_results(
    input_path: Path = typer.Option(...),
    database_path: Path = Path("data/research.db"),
    confirmation_threshold: float = 0.8,
    report_output_dir: Path = Path("reports"),
) -> None:
    """Import LLM adjudication JSON back into candidates and trade ideas."""

    session_factory = create_session_factory(database_path)
    stats = import_llm_adjudication_results(
        session_factory,
        input_path,
        confirmation_threshold=confirmation_threshold,
    )
    typer.echo(f"Processed {stats['processed_items']} LLM adjudication item(s)")
    typer.echo(f"Created {stats['created_candidates']} candidate(s)")
    typer.echo(f"Updated {stats['updated_candidates']} candidate(s)")
    typer.echo(f"Rejected {stats['rejected_candidates']} candidate(s)")
    typer.echo(f"Persisted {stats['inserted_trade_ideas']} trade idea(s)")
    typer.echo(f"Persisted {stats['inserted_trade_updates']} trade update(s)")

    strict_report_path = write_report(
        report_output_dir / "leaderboard-strict.json",
        {
            "mode": "strict",
            "database_path": str(database_path),
            "rows": load_leaderboard_rows(session_factory, mode="strict"),
        },
    )
    expanded_report_path = write_report(
        report_output_dir / "leaderboard-expanded.json",
        {
            "mode": "expanded",
            "database_path": str(database_path),
            "rows": load_leaderboard_rows(session_factory, mode="expanded"),
        },
    )
    typer.echo(f"Refreshed report {strict_report_path}")
    typer.echo(f"Refreshed report {expanded_report_path}")


@app.command()
def review(
    database_path: Path = Path("data/research.db"),
    candidate_file: Path | None = None,
    candidate_id: int | None = None,
    decision: str | None = None,
    note: str | None = None,
) -> None:
    """List pending candidates or apply a manual review decision."""

    if candidate_file is None:
        session_factory = create_session_factory(database_path)

        if candidate_id is None:
            pending = list_pending_candidates_from_db(session_factory)
            typer.echo(f"Pending candidates: {len(pending)}")
            for candidate in pending:
                typer.echo(str(candidate))
            return

        if decision is None:
            raise typer.BadParameter(
                "decision is required when candidate_id is provided"
            )

        try:
            updated = apply_review_decision_to_db(
                session_factory,
                candidate_id=candidate_id,
                decision=decision,
                note=note,
            )
        except LookupError as exc:
            raise typer.BadParameter(str(exc)) from exc

        typer.echo(f"Review decision written to database for candidate {updated['id']}")
        return

    candidates = load_candidates(candidate_file)

    if candidate_id is None:
        pending = list_pending_candidates(candidates)
        typer.echo(f"Pending candidates: {len(pending)}")
        for candidate in pending:
            typer.echo(str(candidate))
        return

    updated_candidates = []
    found = False
    for candidate in candidates:
        if candidate.get("id") == candidate_id:
            found = True
            if decision is None:
                raise typer.BadParameter(
                    "decision is required when candidate_id is provided"
                )
            updated_candidates.append(
                apply_review_decision(candidate, decision=decision, note=note)
            )
        else:
            updated_candidates.append(candidate)

    if not found:
        raise typer.BadParameter(f"candidate_id {candidate_id} not found")

    written_path = write_candidates(candidate_file, updated_candidates)
    typer.echo(f"Review decision written to {written_path}")


@app.command()
def web(
    host: str = "127.0.0.1",
    port: int = 8000,
    runtime_role: str = typer.Option(
        "all",
        "--runtime-role",
        envvar="TELEGRAM_KOL_RUNTIME_ROLE",
    ),
    ingest_refresh_url: str = typer.Option(
        DEFAULT_INGEST_REFRESH_URL,
        "--ingest-refresh-url",
        envvar="TELEGRAM_KOL_INGEST_REFRESH_URL",
    ),
    database_path: Path = Path("data/research.db"),
    config_path: Path = Path("config/groups.yaml"),
    deepcoin_contract_specs_path: Path = Path("config/deepcoin_contract_specs.yaml"),
    deepcoin_contract_specs_cache_path: Path = typer.Option(
        Path("data/deepcoin_contract_specs_cache.json"),
        "--deepcoin-contract-specs-cache-path",
    ),
    deepcoin_contract_specs_ttl_hours: float = typer.Option(
        24.0,
        "--deepcoin-contract-specs-ttl-hours",
        min=0.000001,
    ),
) -> None:
    """Run the local web workbench."""

    try:
        runtime_role = resolve_runtime_role(runtime_role)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--runtime-role") from exc

    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        typer.echo(
            "Web dependencies are not installed in the current environment. "
            "Install project dependencies first.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    group_config = load_group_config(config_path)
    live_target_titles = {
        group.chat_title
        for group in group_config.groups
        if group.enabled
    }
    group_labels_by_title = {
        group.chat_title: (group.custom_group_label or group.chat_title)
        for group in group_config.groups
        if group.enabled
    }
    static_contract_spec_provider = load_deepcoin_contract_specs(
        deepcoin_contract_specs_path,
        required=False,
    )
    settings_session_factory = create_session_factory(database_path)

    def build_runtime_deepcoin_client():
        if runtime_role == "all":
            return build_deepcoin_client_from_env()
        return build_deepcoin_client_from_env(env_file_paths=[])

    authoritative_contract_spec_provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=deepcoin_contract_specs_cache_path,
        instrument_loader=lambda: (
            build_runtime_deepcoin_client().list_swap_instruments()
        ),
        ttl=timedelta(hours=deepcoin_contract_specs_ttl_hours),
    )
    deepcoin_contract_spec_provider = RolloutDeepcoinContractSpecProvider(
        static_provider=static_contract_spec_provider,
        authoritative_provider=authoritative_contract_spec_provider,
        mode_loader=lambda: load_trading_settings(
            settings_session_factory
        ).deepcoin_contract_specs_mode,
    )

    telegram_client = None
    live_listener_status_reason = None
    telegram_session_lock = None
    telegram_session_lock_entered = False
    if runtime_role_owns_telegram_session(runtime_role):
        try:
            auth_config = (
                load_telegram_auth_config()
                if runtime_role == "all"
                else load_telegram_auth_config(env_file_paths=[])
            )
            reap_stopped_session_lock_owner(
                auth_config.session_path,
                current_command="telegram-kol-research web",
            )
            telegram_session_lock = acquire_telegram_session_lock(auth_config.session_path)
            telegram_session_lock.__enter__()
            telegram_session_lock_entered = True
            telegram_client = create_telegram_client(auth_config)
        except TelegramSessionLockError as exc:
            live_listener_status_reason = str(exc)
            typer.echo(
                f"Telegram live listener disabled: {exc}",
                err=False,
            )
        except (ValueError, RuntimeError) as exc:
            if telegram_session_lock_entered and telegram_session_lock is not None:
                telegram_session_lock.__exit__(None, None, None)
                telegram_session_lock_entered = False
            live_listener_status_reason = "缺少 Telegram API 凭据或 Telethon 运行依赖"
            typer.echo(
                f"Telegram live listener disabled: {exc}",
                err=False,
            )
    else:
        live_listener_status_reason = (
            f"Telegram live listener is owned by the ingest runtime role, not {runtime_role}"
        )

    app_instance = create_web_app(
        database_path=database_path,
        runtime_role=runtime_role,
        ingest_refresh_url=ingest_refresh_url,
        live_target_titles=live_target_titles,
        telegram_client=telegram_client,
        live_listener_status_reason=live_listener_status_reason,
        group_labels_by_title=group_labels_by_title,
        group_config=group_config,
        group_config_path=config_path,
        deepcoin_contract_spec_provider=deepcoin_contract_spec_provider,
    )
    try:
        server = _build_web_server(
            app_instance,
            host=host,
            port=port,
        )
        server.run()
    finally:
        if telegram_session_lock_entered and telegram_session_lock is not None:
            telegram_session_lock.__exit__(None, None, None)


@app.command("session-status")
def session_status() -> None:
    """Show which process currently owns the Telegram session lock."""

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    owner = describe_session_lock_owner(auth_config.session_path)
    if owner is None:
        typer.echo(f"Telegram session is free: {auth_config.session_path}")
        return
    typer.echo(f"Telegram session owner: {owner.format_for_humans()}")


@app.command("session-release")
def session_release(pid: int = typer.Option(..., "--pid")) -> None:
    """Release a Telegram session owner after explicitly confirming its PID."""

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    owner = release_session_lock_owner(
        auth_config.session_path,
        expected_pid=pid,
        current_command="telegram-kol-research session-release",
    )
    if owner is None:
        typer.echo(
            "Telegram session owner was not released. "
            "Check `session-status`, then pass the exact same PID.",
            err=False,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Released Telegram session owner: {owner.format_for_humans()}")


@app.command()
def alerts(
    database_path: Path = Path("data/research.db"),
    config_path: Path = Path("config/groups.yaml"),
    ai_recognition_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
) -> None:
    """Run realtime AI strategy alert forwarding without the web UI."""

    group_config = load_group_config(config_path)
    target_titles = {group.chat_title for group in group_config.groups if group.enabled}
    alert_config = load_strategy_alert_config()
    if not strategy_alerts_enabled(alert_config):
        typer.echo(
            "Strategy alerts are not configured. Set TELEGRAM_KOL_ALERT_BOT_TOKEN and TELEGRAM_KOL_ALERT_CHAT_ID.",
            err=False,
        )
        raise typer.Exit(code=1)

    try:
        auth_config = load_telegram_auth_config()
        reap_stopped_session_lock_owner(
            auth_config.session_path,
            current_command="telegram-kol-research alerts",
        )
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    try:
        with acquire_telegram_session_lock(auth_config.session_path):
            client = create_telegram_client(auth_config)
            session_factory = create_session_factory(database_path)
            broker = LiveUpdateBroker()
            system_operator_bot_config = load_system_operator_bot_config()
            notification_bot_config = load_notification_bot_config()

            def authoritative_processor(raw_message_id: int):
                return process_authoritative_message(
                    session_factory,
                    raw_message_id=raw_message_id,
                    ai_recognition_config=load_ai_recognition_config(
                        ai_recognition_config_path
                    ),
                    media_root=media_root,
                    auto_trade_executor=None,
                )

            try:
                asyncio.run(
                    run_live_listener(
                        client=client,
                        session_factory=session_factory,
                        broker=broker,
                        target_titles=target_titles,
                        media_root=media_root,
                        strategy_alert_config=alert_config,
                        strategy_alert_enabled_for_title=lambda title: any(
                            group.enabled
                            and group.ai_strategy_enabled
                            and group.chat_title == title
                            for group in group_config.groups
                        ),
                        ai_recognition_config_path=ai_recognition_config_path,
                        authoritative_processor=authoritative_processor,
                        system_operator_bot_config=system_operator_bot_config,
                        notification_bot_config=notification_bot_config,
                    )
                )
            finally:
                broker.close()
                disconnect = getattr(client, "disconnect", None)
                if callable(disconnect):
                    try:
                        asyncio.run(maybe_await(disconnect()))
                    except RuntimeError:
                        pass
    except TelegramSessionLockError as exc:
        typer.echo(str(exc), err=False)
        raise typer.Exit(code=1) from exc


@app.command("media-cleanup")
def media_cleanup(
    database_path: Path = Path("data/research.db"),
    media_root: Path = Path("data/media"),
    retain_days: int = 14,
    max_media_dir_gb: float | None = 5.0,
    min_free_disk_gb: float | None = 10.0,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Preview deletions by default; pass --apply to delete files.",
    ),
) -> None:
    """Clean old local media cache files without deleting message history."""

    session_factory = create_session_factory(database_path)
    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=retain_days,
        max_media_dir_gb=max_media_dir_gb,
        min_free_disk_gb=min_free_disk_gb,
        dry_run=dry_run,
    )
    mode_label = "dry-run" if dry_run else "applied"
    typer.echo(f"Media cleanup {mode_label}")
    typer.echo(f"Scanned assets: {result.scanned_assets}")
    typer.echo(f"Eligible assets: {result.eligible_assets}")
    typer.echo(f"Protected assets: {result.protected_assets}")
    typer.echo(f"Missing files: {result.missing_files}")
    typer.echo(f"Deleted files: {result.deleted_files}")
    typer.echo(f"Cleared local paths: {result.cleared_local_paths}")
    typer.echo(f"Freed bytes: {result.freed_bytes}")


@app.command("audit-tpsl-ownership")
def audit_tpsl_ownership(
    database_path: Path = typer.Option(
        Path("data/research.db"),
        "--database-path",
        help="Existing production database opened read-only.",
    ),
    output_json: bool = typer.Option(
        False,
        "--output-json",
        help="Emit the stable machine-readable report.",
    ),
) -> None:
    """Read live Deepcoin TPSL ownership coverage without exchange writes."""

    try:
        ledger_rows = load_readonly_protection_ledger(database_path)
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            "database path must name an existing file; no file was created"
        ) from exc
    client = build_deepcoin_client_from_env()
    positions = [
        row for row in client.list_positions()
        if isinstance(row, dict)
    ]
    pending_orders: list[dict[str, Any]] = []
    instrument_ids = sorted(
        {
            str(row.get("instId") or row.get("InstrumentID") or "").upper()
            for row in positions
            if str(row.get("instId") or row.get("InstrumentID") or "").strip()
        }
    )
    for instrument_id in instrument_ids:
        pending_orders.extend(
            row
            for row in client.list_trigger_orders_pending(inst_id=instrument_id)
            if isinstance(row, dict)
        )
    report = build_tpsl_ownership_audit(
        positions=positions,
        pending_orders=pending_orders,
        ledger_rows=ledger_rows,
    )
    payload = report.as_dict()
    if output_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for key, value in payload.items():
        typer.echo(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")


@app.command("backfill-canonical-tpsl-ledger")
def backfill_canonical_tpsl_ledger(
    database_path: Path = typer.Option(
        Path("data/research.db"),
        "--database-path",
    ),
    apply: bool = typer.Option(False, "--apply"),
    expected_fingerprint: str | None = typer.Option(
        None,
        "--expected-fingerprint",
    ),
    confirmation_token: str | None = typer.Option(
        None,
        "--confirmation-token",
    ),
) -> None:
    """Backfill exact current TPSL owners into the canonical ledger."""

    resolved_database_path = database_path.expanduser().resolve()
    if not resolved_database_path.is_file():
        raise typer.BadParameter(
            "database path must name an existing file; no file was created"
        )
    client = build_deepcoin_client_from_env()
    positions = [
        row for row in client.list_positions()
        if isinstance(row, dict)
    ]
    pending_orders: list[dict[str, Any]] = []
    for instrument_id in sorted(
        {
            str(row.get("instId") or row.get("InstrumentID") or "").upper()
            for row in positions
            if str(row.get("instId") or row.get("InstrumentID") or "").strip()
        }
    ):
        pending_orders.extend(
            row
            for row in client.list_trigger_orders_pending(inst_id=instrument_id)
            if isinstance(row, dict)
        )

    readonly_engine = create_engine(
        f"sqlite+pysqlite:///file:{resolved_database_path}?mode=ro&uri=true"
    )
    readonly_session_factory = sessionmaker(
        bind=readonly_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    plan = build_tpsl_ledger_backfill_plan(
        readonly_session_factory,
        positions=positions,
        pending_orders=pending_orders,
        snapshot_complete=True,
    )
    result = None
    if apply:
        if not expected_fingerprint or not confirmation_token:
            raise typer.BadParameter(
                "--expected-fingerprint and --confirmation-token are required for apply"
            )
        writable_session_factory = create_session_factory(resolved_database_path)

        def rebuild():
            return build_tpsl_ledger_backfill_plan(
                writable_session_factory,
                positions=positions,
                pending_orders=pending_orders,
                snapshot_complete=True,
            )

        result = apply_tpsl_ledger_backfill_plan(
            writable_session_factory,
            plan,
            expected_fingerprint=expected_fingerprint,
            confirmation_token=confirmation_token,
            fresh_plan_builder=rebuild,
        )
    typer.echo(
        json.dumps(
            {
                "mode": "apply" if apply else "dry_run",
                "fingerprint": plan.fingerprint,
                "actions": [asdict(row) for row in plan.actions],
                "refusals": [asdict(row) for row in plan.refusals],
                "applied": result.applied if result is not None else 0,
                "exchange_write_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("worker-command-reconcile")
def worker_command_reconcile(
    command_id: str = typer.Option(..., "--command-id"),
    database_path: Path = typer.Option(
        Path("data/research.db"), "--database-path"
    ),
    apply_confirmed: bool = typer.Option(False, "--apply-confirmed"),
) -> None:
    """Audit one uncertain worker command; apply only a confirmed outcome."""

    session_factory = create_session_factory(database_path)
    report = reconcile_worker_command_by_id(
        session_factory,
        command_id=command_id,
        deepcoin_client_factory=build_deepcoin_client_from_env,
        apply_confirmed=apply_confirmed,
    )
    typer.echo(
        json.dumps(
            {
                "mode": "apply_confirmed" if apply_confirmed else "dry_run",
                "command_id": report.command_id,
                "outcome": report.outcome,
                "reason": report.reason,
                "applied": report.applied,
                "exchange_write_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _semantic_review_disable_plan_from_dict(
    payload: dict[str, Any],
) -> SemanticReviewDisablePlan:
    return SemanticReviewDisablePlan(
        database_identity=str(payload["database_identity"]),
        cutoff=str(payload["cutoff"]),
        status_counts={
            str(key): int(value)
            for key, value in dict(payload["status_counts"]).items()
        },
        running_count=int(payload["running_count"]),
        targets=tuple(
            SemanticReviewTarget(**target)
            for target in payload["targets"]
        ),
        quick_check=str(payload["quick_check"]),
        provider_call_count=int(payload.get("provider_call_count", 0)),
        notification_count=int(payload.get("notification_count", 0)),
        exchange_write_count=int(payload.get("exchange_write_count", 0)),
        plan_sha=str(payload["plan_sha"]),
    )


def _write_semantic_review_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _semantic_review_rollback_plan_from_dict(
    payload: dict[str, Any],
) -> SemanticReviewRollbackPlan:
    return SemanticReviewRollbackPlan(
        database_identity=str(payload["database_identity"]),
        preimage_plan_sha=str(payload["preimage_plan_sha"]),
        targets=tuple(
            SemanticReviewRollbackTarget(
                raw_message_id=int(target["raw_message_id"]),
                preimage=SemanticReviewTarget(**target["preimage"]),
                current_row_fingerprint=str(target["current_row_fingerprint"]),
            )
            for target in payload["targets"]
        ),
        quick_check=str(payload["quick_check"]),
        provider_call_count=int(payload.get("provider_call_count", 0)),
        notification_count=int(payload.get("notification_count", 0)),
        exchange_write_count=int(payload.get("exchange_write_count", 0)),
        plan_sha=str(payload["plan_sha"]),
    )


@app.command("semantic-review-terminalize")
def semantic_review_terminalize(
    database_path: Path = typer.Option(..., "--database-path"),
    plan_output: Path = typer.Option(..., "--plan-output"),
    apply: bool = typer.Option(False, "--apply"),
    expected_plan_sha: str | None = typer.Option(None, "--expected-plan-sha"),
) -> None:
    """Plan by default, or apply an exact semantic-review terminalization plan."""

    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        typer.echo("Refusing terminalization: database does not exist.", err=True)
        raise typer.Exit(code=2)
    if apply and (
        expected_plan_sha is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha) is None
    ):
        typer.echo(
            "Refusing apply: --expected-plan-sha must be an exact 64-hex value.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        session_factory = create_existing_session_factory(resolved_path)
        if apply:
            stored = json.loads(plan_output.read_text(encoding="utf-8"))
            plan_payload = stored.get("plan", stored)
            plan = _semantic_review_disable_plan_from_dict(plan_payload)
            result = apply_semantic_review_disable_plan(
                session_factory,
                plan,
                expected_plan_sha=expected_plan_sha or "",
                applied_at=datetime.now(UTC),
            )
        else:
            plan = build_semantic_review_disable_plan(
                session_factory,
                cutoff=datetime.now(UTC),
            )
            result = None
        evidence = {
            "mode": "apply" if apply else "dry_run",
            "plan_sha": plan.plan_sha,
            "status_counts": plan.status_counts,
            "running_count": plan.running_count,
            "target_count": len(plan.targets),
            "changed_count": result.changed_count if result is not None else 0,
            "quick_check": plan.quick_check,
            "provider_call_count": 0,
            "notification_count": 0,
            "exchange_write_count": 0,
            "post_apply_sha": (
                result.post_apply_sha if result is not None else None
            ),
            "plan": plan.to_dict(),
        }
        _write_semantic_review_evidence(plan_output, evidence)
    except (KeyError, TypeError, ValueError, OSError, SemanticReviewControlError) as exc:
        typer.echo(f"Refusing terminalization: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


@app.command("semantic-review-terminalize-rollback")
def semantic_review_terminalize_rollback(
    database_path: Path = typer.Option(..., "--database-path"),
    preimage_plan: Path = typer.Option(..., "--preimage-plan"),
    plan_output: Path = typer.Option(..., "--plan-output"),
    apply: bool = typer.Option(False, "--apply"),
    expected_plan_sha: str | None = typer.Option(None, "--expected-plan-sha"),
) -> None:
    """Plan by default, or apply an exact targeted semantic-review rollback."""

    resolved_path = database_path.expanduser().resolve()
    if not resolved_path.is_file():
        typer.echo("Refusing rollback: database does not exist.", err=True)
        raise typer.Exit(code=2)
    if not preimage_plan.expanduser().resolve().is_file():
        typer.echo("Refusing rollback: preimage plan does not exist.", err=True)
        raise typer.Exit(code=2)
    if apply and (
        expected_plan_sha is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha) is None
    ):
        typer.echo(
            "Refusing rollback: --expected-plan-sha must be an exact 64-hex value.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        session_factory = create_existing_session_factory(resolved_path)
        if apply:
            stored = json.loads(plan_output.read_text(encoding="utf-8"))
            plan = _semantic_review_rollback_plan_from_dict(
                stored.get("plan", stored)
            )
            result = apply_semantic_review_rollback_plan(
                session_factory,
                plan,
                expected_plan_sha=expected_plan_sha or "",
            )
        else:
            stored_preimage = json.loads(
                preimage_plan.read_text(encoding="utf-8")
            )
            disable_plan = _semantic_review_disable_plan_from_dict(
                stored_preimage.get("plan", stored_preimage)
            )
            plan = build_semantic_review_rollback_plan(
                session_factory,
                preimage_plan=disable_plan,
            )
            result = None
        evidence = {
            "mode": "apply" if apply else "dry_run",
            "plan_sha": plan.plan_sha,
            "preimage_plan_sha": plan.preimage_plan_sha,
            "target_count": len(plan.targets),
            "changed_count": result.changed_count if result is not None else 0,
            "quick_check": plan.quick_check,
            "provider_call_count": 0,
            "notification_count": 0,
            "exchange_write_count": 0,
            "post_rollback_sha": (
                result.post_rollback_sha if result is not None else None
            ),
            "plan": plan.to_dict(),
        }
        _write_semantic_review_evidence(plan_output, evidence)
    except (KeyError, TypeError, ValueError, OSError, SemanticReviewControlError) as exc:
        typer.echo(f"Refusing rollback: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


@app.command("media-dedupe")
def media_dedupe(
    database_path: Path = Path("data/research.db"),
    media_root: Path = Path("data/media"),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Preview duplicate media row cleanup by default; pass --apply to delete rows.",
    ),
) -> None:
    """Remove duplicate media rows while keeping message history and best media metadata."""

    backup_path = None
    if not dry_run:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = database_path.with_name(f"{database_path.name}.bak-{timestamp}")
        shutil.copy2(database_path, backup_path)

    session_factory = create_session_factory(database_path)
    result = dedupe_media_assets(
        session_factory,
        media_root=media_root,
        dry_run=dry_run,
    )
    mode_label = "dry-run" if dry_run else "applied"
    typer.echo(f"Media dedupe {mode_label}")
    if backup_path is not None:
        typer.echo(f"Database backup: {backup_path}")
    typer.echo(f"Duplicate message groups: {result.duplicate_message_groups}")
    typer.echo(f"Scanned assets: {result.scanned_assets}")
    typer.echo(f"Kept assets: {result.kept_assets}")
    typer.echo(f"Deleted assets: {result.deleted_assets}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
