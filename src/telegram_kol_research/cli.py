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
import tempfile
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from enum import Enum
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.execution_bindings import (
    repair_execution_order_legs_from_binding_payloads,
)
from telegram_kol_research.entry_protection_ledger_repair import (
    apply_entry_protection_ledger_repair_plan,
    build_entry_protection_ledger_repair_plan,
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
from telegram_kol_research.position_attribution_repair import (
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)
from telegram_kol_research.position_management_remediation import (
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
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
    SignalCandidate,
)
from telegram_kol_research.models import SyncCheckpoint
from telegram_kol_research.recognition_decisions import update_recognition_execution_outcome
from telegram_kol_research.reporting import load_leaderboard_rows, write_report
from telegram_kol_research.config import load_runtime_incident_config
from telegram_kol_research.runtime_agent_tools import (
    RuntimeAgentToolRegistry,
    build_local_exchange_comparison,
    build_prior_attempts_summary,
    build_protection_summary,
    build_worker_history_summary,
)
from telegram_kol_research.runtime_agent_exchange_snapshot import (
    RuntimeAgentExchangeSnapshotRefresh,
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
from telegram_kol_research.web_app import create_web_app

app = typer.Typer(help="Telegram KOL win-rate research CLI.")


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
    except OSError as exc:
        raise ManagementAuditSnapshotError(
            "source_copy_failed", status="snapshot_unavailable"
        ) from exc
    if set(first) != set(second) or any(first[name] != second[name] for name in first):
        raise ManagementAuditSnapshotError("source_snapshots_differ")
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
                **{state: 0 for state in _MANAGEMENT_ALERT_STATES},
            },
            "batches_returned": 0,
            "batches_truncated": False,
            "all_history_legs_complete": False,
            "malformed_row_count": 0,
            "malformed_field_count": 0,
            "output_complete": False,
            "batches": [],
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
            for state in _MANAGEMENT_ALERT_STATES:
                exclusion = (
                    " AND NOT (" + informational_predicate + ")"
                    if state == "blocked" and informational_predicate is not None
                    else ""
                )
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


def _audit_management_batches_read_only(database_path: Path, *, limit: int) -> dict:
    try:
        with tempfile.TemporaryDirectory(prefix="management-audit-") as temporary:
            snapshot_path, snapshot_info = _build_stable_private_snapshots(
                database_path, Path(temporary)
            )
            return _audit_management_snapshot(
                snapshot_path, limit=limit, snapshot_info=snapshot_info
            )
    except ManagementAuditSnapshotError:
        raise
    except OSError as exc:
        raise ManagementAuditSnapshotError(
            "private_snapshot_unavailable", status="snapshot_unavailable"
        ) from exc


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
    expected_head: str = typer.Option(..., "--expected-head"),
    expected_auto_trade_enabled: bool | None = typer.Option(
        None,
        "--expected-auto-trade-enabled/--no-expected-auto-trade-enabled",
    ),
    expected_management_mode: str = typer.Option(
        ...,
        "--expected-management-mode",
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
    state_path: Path = typer.Option(
        Path("/var/lib/telegram-kol-monitor/state.json"),
        "--state-path",
    ),
    settings_url: str = typer.Option(
        "http://127.0.0.1:8000/api/trading-settings",
        "--settings-url",
    ),
    lookback_minutes: int = typer.Option(35, "--lookback-minutes", min=1, max=120),
    notify: bool = typer.Option(False, "--notify"),
    force_full_audit: bool = typer.Option(False, "--force-full-audit"),
    test_notification: bool = typer.Option(False, "--test-notification"),
) -> None:
    """Run bounded read-only server safety checks and optional alerts."""

    if expected_auto_trade_enabled is None:
        raise typer.BadParameter(
            "choose --expected-auto-trade-enabled or --no-expected-auto-trade-enabled"
        )
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

    outcome = run_production_safety_monitor(
        expectations=MonitorExpectations(
            head=expected_head,
            auto_trade_enabled=expected_auto_trade_enabled,
            management_execution_mode=expected_management_mode,
            max_concurrent_positions=expected_max_concurrent_positions,
        ),
        state_path=state_path,
        adapters=ProductionSafetyAdapters(
            database_path=database_path,
            checkout_path=checkout_path,
            settings_url=settings_url,
        ),
        now=datetime.now(UTC),
        notify=notify,
        force_full_audit=force_full_audit,
        lookback=timedelta(minutes=lookback_minutes),
        runtime_incident_session_factory=create_existing_session_factory(
            database_path
        ),
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
            try:
                summary = json.loads(row.redacted_summary)
            except (TypeError, ValueError, json.JSONDecodeError):
                summary = {}
            return {
                "data": {
                    "incident_id": row.id,
                    "incident_type": row.incident_type,
                    "severity": row.severity,
                    "status": row.status,
                    "generation": row.generation,
                    "repeat_count": row.repeat_count,
                    "redacted_summary": summary,
                },
                "evidence_refs": [f"incident:{row.id}"],
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
        data = {
            "incident_id": incident.id,
            "available": False,
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

    return RuntimeAgentToolRegistry(
        providers={
            "get_incident_summary": incident_summary,
            "get_lifecycle_state": lifecycle_state,
            "get_worker_state": worker_state,
            "get_service_audit_state": service_audit_state,
            "get_journal_summary": journal_summary,
            "get_exchange_snapshot": exchange_snapshot,
            "compare_local_exchange": compare_local_exchange,
            "get_prior_attempts": prior_attempts,
            "get_protection_summary": protection_summary,
        },
        max_output_bytes=max_output_bytes,
    )


def _build_runtime_agent_action_handlers(
    tools: RuntimeAgentToolRegistry,
    *,
    exchange_snapshot_refresh: (
        RuntimeAgentExchangeSnapshotRefresh | None
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
    return handlers


def _read_runtime_agent_exchange_snapshot() -> dict[str, Any]:
    with httpx.Client(timeout=5.0) as client:
        response = client.get(
            "http://127.0.0.1:8000/api/runtime-agent/"
            "read-only-exchange-snapshot"
        )
        response.raise_for_status()
        return response.json()


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
    tools = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=runtime_config.agent_max_tool_output_bytes,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
    )
    action_handlers = _build_runtime_agent_action_handlers(
        tools,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
    )
    worker_config = RuntimeAgentWorkerConfig(
        enabled=runtime_config.agent_enabled,
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
    )
    if runtime_config.agent_enabled:
        llm_config = load_runtime_agent_llm_config()

        def model_turn(**kwargs):
            return request_structured_chat_turn(
                config=llm_config,
                messages=kwargs["messages"],
                tool_schemas=kwargs["tool_schemas"],
                timeout_seconds=kwargs["timeout_seconds"],
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
    tools = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=runtime_config.agent_max_tool_output_bytes,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
    )
    action_handlers = _build_runtime_agent_action_handlers(
        tools,
        exchange_snapshot_refresh=exchange_snapshot_refresh,
    )
    worker_config = RuntimeAgentWorkerConfig(
        enabled=True,
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
) -> None:
    """Read a bounded, redacted management batch and legacy queue summary."""

    normalized_format = output_format.strip().lower()
    if normalized_format not in {"json", "text"}:
        raise typer.BadParameter("output-format must be one of: text, json")
    try:
        audit = _audit_management_batches_read_only(database_path, limit=limit)
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
                **{state: 0 for state in _MANAGEMENT_ALERT_STATES},
            },
            "batches_returned": 0,
            "batches_truncated": False,
            "all_history_legs_complete": False,
            "malformed_row_count": 0,
            "malformed_field_count": 0,
            "output_complete": False,
            "batches": [],
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
    if plan.has_actions and (
        not action_id
        or not pos_id
        or not expected_fingerprint
        or not confirmation_token
    ):
        typer.echo(
            "Refusing apply: --action-id, --pos-id, "
            "--expected-fingerprint, and --confirmation-token are required.",
            err=True,
        )
        raise typer.Exit(code=2)
    result = apply_entry_protection_ledger_repair_plan(
        session_factory,
        plan,
        action_id=action_id or "",
        pos_id=pos_id or "",
        expected_fingerprint=expected_fingerprint or "",
        confirmation_token=confirmation_token or "",
    )
    typer.echo(f"Applied {result.applied} entry protection ledger repair(s).")


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
    database_path: Path = Path("data/research.db"),
    config_path: Path = Path("config/groups.yaml"),
    deepcoin_contract_specs_path: Path = Path("config/deepcoin_contract_specs.yaml"),
) -> None:
    """Run the local web workbench."""

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
    deepcoin_contract_spec_provider = load_deepcoin_contract_specs(
        deepcoin_contract_specs_path,
        required=False,
    )

    telegram_client = None
    live_listener_status_reason = None
    telegram_session_lock = None
    telegram_session_lock_entered = False
    try:
        auth_config = load_telegram_auth_config()
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

    app_instance = create_web_app(
        database_path=database_path,
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
            system_operator_bot_config = load_notification_bot_config()

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
