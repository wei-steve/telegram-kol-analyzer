"""Pure, secret-free production safety snapshot evaluation."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from telegram_kol_research.config import (
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_management_state,
    capture_monitor_state,
    capture_notification_failure,
    capture_protection_state,
    capture_runtime_incident_best_effort,
)
from telegram_kol_research.protection_health import (
    current_protection_incident_health_status,
)
from telegram_kol_research.system_operator_bot import (
    load_system_operator_bot_config,
    send_system_operator_bot_message,
    system_operator_bot_enabled,
)


MAX_ALERT_LENGTH = 1200
MAX_SAFE_COUNT = 1_000_000_000
logger = logging.getLogger(__name__)

_SAFE_EVENT_VALUE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_GIT_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_TIMESTAMP = re.compile(r"[0-9T:+.-]{1,40}\Z")
_SHA256_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ADAPTER_NAMES = frozenset(
    {"service", "head", "settings", "journal", "events", "audit", "composite"}
)
_MONITOR_CAPTURE_REASON_CODES = frozenset(
    {"adapter_failure", "audit_incomplete"}
)
_MONITOR_CAPTURE_NOTIFICATION_ERRORS = frozenset(
    {"notification_config_missing", "notification_delivery_failed"}
)
_MANAGEMENT_MODES = frozenset({"disabled", "shadow", "live"})
_SERVICE_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "maintenance",
        "reloading",
        "unknown",
    }
)
_AUDIT_ALERT_STATES = (
    "blocked",
    "partial_failed",
    "recovery_required",
    "submit_unknown",
)
_TRANSIENT_AUDIT_SNAPSHOT_REASONS = frozenset(
    {
        "source_snapshots_differ",
        "source_component_changed_during_read",
        "source_component_set_changed",
    }
)
_NORMAL_EVENT_STATUSES = frozenset(
    {"submitted", "succeeded", "confirmed", "cancelled", "skipped", "resolved", "restored"}
)
_RECOVERY_EVENT_STATUSES = frozenset({"recovery_required"})
_FIXED_REASON_CODES = frozenset(
    {
        "adapter_failure",
        "audit_abnormal",
        "audit_incomplete",
        "auto_trade_enabled_drift",
        "entry_preamble_mode_drift",
        "duplicate_manual_close",
        "event_recovery_status",
        "event_unknown_status",
        "journal_errors",
        "malformed_snapshot",
        "management_execution_mode_drift",
        "max_concurrent_positions_drift",
        "service_inactive",
        "state_invalid",
        "completed_batch_missing_component_evidence",
        "duplicate_composite_close_submission",
        "live_position_retained_tp_oversized",
        "composite_position_without_verified_stop",
        "stalled_composite_component",
    }
)
_LOW_REPEAT_REASON_CODES = frozenset({"audit_abnormal"})
_LEGACY_STATE_FIELDS = frozenset(
    {
        "last_window_at",
        "last_full_audit_date",
        "anomaly_fingerprint",
        "last_notification_at",
    }
)
_STATE_FIELDS = _LEGACY_STATE_FIELDS | {"active_reason_codes"}


def build_monitor_incident_capture_projection(
    *,
    checked_at: datetime,
    reason_codes: Sequence[str],
    adapter_failures: Sequence[str],
    notification_status: str,
    monitor_error: str | None,
) -> dict[str, Any]:
    """Build the only projection accepted by the trusted incident writer."""

    timestamp = _require_aware_datetime(checked_at).isoformat()
    reasons = sorted(
        set(reason_codes).intersection(_MONITOR_CAPTURE_REASON_CODES)
    )
    failures = sorted(set(adapter_failures).intersection(_ADAPTER_NAMES))
    notification_error = (
        monitor_error
        if notification_status in {"config_missing", "delivery_failed"}
        and monitor_error in _MONITOR_CAPTURE_NOTIFICATION_ERRORS
        else None
    )
    return {
        "schema_version": 1,
        "checked_at": timestamp,
        "reason_codes": reasons,
        "adapter_failures": failures,
        "notification_error": notification_error,
    }


def send_monitor_incident_capture(
    url: str,
    *,
    token: str,
    projection: Mapping[str, Any],
) -> int:
    """Submit one bounded projection over the fixed loopback channel."""

    parsed = urlsplit(str(url))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/runtime-incidents/monitor-capture"
    ):
        raise ValueError("monitor incident capture URL must be exact loopback HTTP")
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise ValueError("monitor incident capture token is invalid")
    # The production web process has pre-existing synchronous maintenance
    # windows that can delay loopback request dispatch for tens of seconds.
    # This wait affects only the isolated monitor oneshot; it never blocks the
    # listener or trading process. Keep it bounded above SQLite's 30s busy
    # timeout so a healthy writer still has a chance to answer.
    with httpx.Client(timeout=45.0, trust_env=False) as client:
        response = client.post(
            url,
            headers={"x-monitor-capture-token": token},
            json=dict(projection),
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, Mapping) or payload.get("accepted") is not True:
        raise ValueError("monitor incident capture response is invalid")
    captured = payload.get("captured")
    if not isinstance(captured, int) or isinstance(captured, bool) or captured < 0:
        raise ValueError("monitor incident capture count is invalid")
    return min(captured, 102)
_FINGERPRINT_DETAIL_KEYS_BY_REASON = {
    "service_inactive": ("service_state",),
    "auto_trade_enabled_drift": (
        "auto_trade_enabled",
        "expected_auto_trade_enabled",
    ),
    "management_execution_mode_drift": (
        "management_execution_mode",
        "expected_management_execution_mode",
    ),
    "entry_preamble_mode_drift": (
        "entry_preamble_mode",
        "expected_entry_preamble_mode",
    ),
    "max_concurrent_positions_drift": (
        "max_concurrent_positions",
        "expected_max_concurrent_positions",
    ),
    "journal_errors": ("journal_error_count",),
    "event_unknown_status": ("unknown_event_count",),
    "event_recovery_status": ("recovery_event_count",),
    "duplicate_manual_close": ("duplicate_manual_close_count",),
    "audit_abnormal": (
        "audit_abnormal_count",
        "audit_state_counts",
        "actionable_batch_refs",
        "actionable_batches_total",
        "actionable_batches_truncated",
    ),
    "audit_incomplete": ("audit_complete",),
    "adapter_failure": ("adapter_failures",),
    "malformed_snapshot": (),
    "state_invalid": (),
    "completed_batch_missing_component_evidence": ("composite_invariant_codes",),
    "duplicate_composite_close_submission": ("composite_invariant_codes",),
    "live_position_retained_tp_oversized": ("composite_invariant_codes",),
    "composite_position_without_verified_stop": ("composite_invariant_codes",),
    "stalled_composite_component": ("composite_invariant_codes",),
    "stale_entry_preamble_unresolved": ("entry_preamble_invariant_codes",),
    "entry_preamble_ambiguous": ("entry_preamble_invariant_codes",),
    "live_entry_preamble_binding_evidence_missing": (
        "entry_preamble_invariant_codes",
    ),
}
_NOTIFICATION_SUPPRESSION = timedelta(hours=6)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_LOOKBACK = timedelta(minutes=35)
_MAX_COMMAND_OUTPUT_BYTES = 1_048_576
_MAX_HTTP_OUTPUT_BYTES = 65_536
_MAX_ABNORMAL_EVENTS = 200
_MAX_JOURNAL_ERRORS = 10_000
_MAX_ACTIONABLE_BATCH_REFS = 10
_ACTIONABLE_BATCH_REF = re.compile(r"^batch:([1-9][0-9]{0,18})$")
MONITOR_TEST_NOTIFICATION_TEXT = (
    "【监控测试】服务器安全监控通知链路验证\n"
    "本消息仅验证系统运维通知，不包含交易指令。"
)


def _load_monitor_bot_config():
    """Load only the service environment, never checkout configuration files."""

    return load_system_operator_bot_config(env_file_paths=[])


@dataclass(frozen=True, slots=True)
class MonitorExpectations:
    head: str
    auto_trade_enabled: bool
    management_execution_mode: str
    max_concurrent_positions: int
    entry_preamble_mode: str


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    service_state: str
    head: str
    settings: Mapping[str, Any]
    journal_error_count: int
    abnormal_events: Sequence[Mapping[str, Any]]
    audit: Mapping[str, Any] | None
    composite_invariant_codes: Sequence[str] = ()
    entry_preamble_invariant_codes: Sequence[str] = ()
    adapter_failures: Sequence[str] = ()
    state_invalid: bool = False


@dataclass(frozen=True, slots=True)
class MonitorResult:
    healthy: bool
    reason_codes: tuple[str, ...]
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MonitorAlertPresentation:
    severity: str
    title: str
    problems: tuple[str, ...]
    impact: str
    operator_action: str
    technical_codes: tuple[str, ...]
    actionable_batch_ids: tuple[int, ...] = ()
    additional_problem_count: int = 0
    additional_batch_count: int = 0


@dataclass(frozen=True, slots=True)
class MonitorState:
    last_window_at: str | None = None
    last_full_audit_date: str | None = None
    anomaly_fingerprint: str | None = None
    last_notification_at: str | None = None
    active_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MonitorNotificationDecision:
    should_notify: bool
    next_state: MonitorState
    kind: str


@dataclass(frozen=True, slots=True)
class MonitorRunOutcome:
    result: MonitorResult
    notification_status: str
    audit_ran: bool
    monitor_error: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.result.healthy and self.monitor_error is None else 1


@dataclass(frozen=True, slots=True)
class ProductionSafetyAdapters:
    """Bounded observation-only adapters used by the server monitor."""

    database_path: Path
    live_position_snapshot_path: Path | None = None
    checkout_path: Path = Path(".")
    settings_url: str = "http://127.0.0.1:8000/api/trading-settings"
    service_name: str = "telegram-kol.service"
    audit_command: tuple[str, ...] = (
        sys.executable,
        "-m",
        "telegram_kol_research.cli",
    )

    def read_service_state(self) -> str:
        # A successful response from the service-owned loopback endpoint proves
        # the app is serving without granting this monitor system-bus access.
        read_loopback_settings(self.settings_url)
        return "active"

    def read_git_head(self) -> str:
        safe_checkout = self.checkout_path.resolve()
        completed = _run_bounded_command(
            (
                "git",
                "-c",
                f"safe.directory={safe_checkout}",
                "rev-parse",
                "HEAD",
            ),
            timeout_seconds=5,
            cwd=self.checkout_path,
        )
        if completed.returncode != 0:
            raise RuntimeError("git_head_unavailable")
        return completed.output.strip()

    def read_settings(self) -> Mapping[str, Any]:
        return read_loopback_settings(self.settings_url)

    def count_journal_errors(self, *, since: datetime) -> int:
        completed = _run_bounded_command(
            (
                "journalctl",
                "--unit",
                self.service_name,
                "--priority",
                "err",
                "--since",
                since.isoformat(),
                "--no-pager",
                "--output",
                "cat",
            ),
            timeout_seconds=10,
            max_output_bytes=262_144,
        )
        if completed.returncode != 0:
            raise RuntimeError("journal_unavailable")
        return min(
            _MAX_JOURNAL_ERRORS,
            sum(1 for line in completed.output.splitlines() if line.strip()),
        )

    def read_abnormal_events(
        self, *, since: datetime, limit: int
    ) -> tuple[Mapping[str, Any], ...]:
        return read_abnormal_execution_events(
            self.database_path,
            since=since,
            limit=limit,
        )

    def read_composite_invariants(self, *, now: datetime) -> tuple[str, ...]:
        snapshot_path = self.live_position_snapshot_path or (
            self.database_path.parent
            / "web_cache"
            / "deepcoin_live_positions.json"
        )
        return read_composite_management_invariants(
            self.database_path,
            now=now,
            live_position_snapshot_path=snapshot_path,
        )

    def read_entry_preamble_invariants(self, *, now: datetime) -> tuple[str, ...]:
        return read_entry_preamble_invariants(self.database_path, now=now)

    def run_management_audit(self) -> Mapping[str, Any]:
        completed = _run_bounded_command(
            (
                *self.audit_command,
                "audit-management-batches",
                "--database-path",
                str(self.database_path),
                "--limit",
                "20",
                "--output-format",
                "json",
            ),
            timeout_seconds=180,
        )
        try:
            payload = json.loads(
                completed.output,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("audit_output_invalid") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("audit_output_invalid")
        if completed.returncode != 0:
            snapshot_reason = payload.get("snapshot_reason")
            if snapshot_reason in _TRANSIENT_AUDIT_SNAPSHOT_REASONS:
                raise _SourceSnapshotsDiffer(f"audit_{snapshot_reason}")
            raise RuntimeError("audit_command_failed")
        return payload


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    output: str


class _SourceSnapshotsDiffer(RuntimeError):
    """Signal one retry without exposing a nonzero child result as audit data."""


@dataclass(frozen=True, slots=True)
class _LoadedMonitorState:
    state: MonitorState
    invalid_existing_file: bool


def load_monitor_state(path: str | Path) -> MonitorState:
    """Load the exact monitor-state schema, rebuilding malformed state safely."""

    return _load_monitor_state(path).state


def _load_monitor_state(path: str | Path) -> _LoadedMonitorState:
    """Distinguish a normal first run from an unsafe existing state file."""

    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        return _LoadedMonitorState(
            state=_monitor_state_from_payload(payload),
            invalid_existing_file=False,
        )
    except FileNotFoundError:
        return _LoadedMonitorState(
            state=MonitorState(),
            invalid_existing_file=False,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _LoadedMonitorState(
            state=MonitorState(),
            invalid_existing_file=True,
        )


def save_monitor_state(path: str | Path, state: MonitorState) -> None:
    """Atomically persist only the allowlisted monitor state with mode 0600."""

    destination = Path(path)
    payload = _monitor_state_payload(state)
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(rendered)
            temporary_file.flush()
            os.fchmod(temporary_file.fileno(), 0o600)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def should_run_daily_audit(
    *,
    now: datetime,
    last_successful_date: str | None,
    force: bool = False,
) -> bool:
    """Schedule at most one successful audit day after 09:00 Shanghai time."""

    localized = _require_aware_datetime(now).astimezone(_SHANGHAI)
    if force:
        return True
    if localized.hour < 9:
        return False
    return last_successful_date != localized.date().isoformat()


def run_daily_management_audit(run_once) -> Mapping[str, Any]:
    """Retry only one transient source-snapshot mismatch, exactly once."""

    try:
        first = run_once()
    except _SourceSnapshotsDiffer:
        return run_once()
    if not isinstance(first, Mapping):
        raise TypeError("audit result must be a mapping")
    if first.get("snapshot_reason") not in _TRANSIENT_AUDIT_SNAPSHOT_REASONS:
        return first
    second = run_once()
    if not isinstance(second, Mapping):
        raise TypeError("audit result must be a mapping")
    return second


def read_abnormal_execution_events(
    database_path: str | Path,
    *,
    since: datetime,
    limit: int = 100,
    connect=sqlite3.connect,
) -> tuple[Mapping[str, Any], ...]:
    """Read bounded identity/status fields from SQLite without write access."""

    if type(limit) is not int or not 1 <= limit <= _MAX_ABNORMAL_EVENTS:
        raise ValueError("invalid abnormal event limit")
    since_utc = _require_aware_datetime(since).astimezone(UTC).replace(tzinfo=None)
    uri = f"file:{database_path}?mode=ro"
    query = """
        SELECT action, status, pos_id
        FROM execution_events
        WHERE created_at >= ?
          AND (
            status NOT IN ('submitted', 'succeeded', 'confirmed', 'cancelled',
                           'skipped', 'resolved', 'restored')
            OR action = 'close_bound_position_market'
          )
        ORDER BY created_at DESC, action, status, pos_id
        LIMIT ?
    """
    with connect(uri, uri=True) as connection:
        rows = connection.execute(
            query,
            (since_utc.isoformat(sep=" "), limit + 1),
        ).fetchall()
    if len(rows) > limit:
        raise RuntimeError("events_incomplete")
    return tuple(
        {"action": row[0], "status": row[1], "pos_id": row[2]}
        for row in rows
    )


def read_entry_preamble_invariants(
    database_path: str | Path,
    *,
    now: datetime,
    stale_after: timedelta = timedelta(hours=6),
    connect=sqlite3.connect,
) -> tuple[str, ...]:
    """Read bounded preamble/strategy evidence invariants without write access."""

    checked_at = _require_aware_datetime(now).astimezone(UTC).replace(tzinfo=None)
    stale_before = checked_at - stale_after
    required_tables = {
        "entry_preambles",
        "entry_strategy_assemblies",
        "execution_bindings",
        "raw_messages",
        "signal_candidates",
    }
    reasons: set[str] = set()
    uri = f"file:{database_path}?mode=ro"
    with connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        available = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not required_tables.issubset(available):
            return ()
        eligibility = """
            FROM entry_preambles AS p
            JOIN raw_messages AS source ON source.id = p.raw_message_id
            WHERE p.status = 'pending'
              AND NOT EXISTS (
                SELECT 1
                FROM signal_candidates AS c
                JOIN raw_messages AS boundary ON boundary.id = c.raw_message_id
                WHERE boundary.chat_id = p.chat_id
                  AND (
                    c.event_type IN ('entry_signal', 'strategy_revision', 'close_signal')
                    OR c.management_action IN ('cancel_entry', 'cancel')
                  )
                  AND (
                    boundary.posted_at > source.posted_at
                    OR (boundary.posted_at = source.posted_at AND boundary.message_id > source.message_id)
                    OR (boundary.posted_at = source.posted_at AND boundary.message_id = source.message_id AND boundary.id > source.id)
                  )
              )
        """
        stale = connection.execute(
            "SELECT 1 " + eligibility + " AND p.created_at < ? LIMIT 1",
            (stale_before.isoformat(sep=" "),),
        ).fetchone()
        if stale:
            reasons.add("stale_entry_preamble_unresolved")
        ambiguous = connection.execute(
            "SELECT 1 "
            + eligibility
            + " GROUP BY p.chat_id, p.symbol, p.side HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if ambiguous:
            reasons.add("entry_preamble_ambiguous")
        evidence_rows = connection.execute(
            """
            SELECT a.fingerprint, b.payload_json
            FROM entry_strategy_assemblies AS a
            LEFT JOIN execution_bindings AS b
              ON b.strategy_instance_id = a.strategy_instance_id
            ORDER BY a.id DESC LIMIT 200
            """
        ).fetchall()
        for fingerprint, payload_json in evidence_rows:
            try:
                payload = json.loads(payload_json or "{}")
                draft = payload.get("draft") if isinstance(payload, dict) else None
                evidence = (
                    draft.get("entry_preamble_assembly")
                    if isinstance(draft, dict)
                    else None
                )
                if evidence is None and isinstance(payload, dict):
                    evidence = payload.get("entry_preamble_assembly")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = None
            if not isinstance(evidence, dict) or str(
                evidence.get("assembly_fingerprint") or ""
            ) != str(fingerprint):
                reasons.add("live_entry_preamble_binding_evidence_missing")
                break
    return tuple(sorted(reasons))


def read_composite_management_invariants(
    database_path: str | Path,
    *,
    now: datetime,
    stale_after: timedelta = timedelta(minutes=15),
    live_position_snapshot_path: str | Path | None = None,
    connect=sqlite3.connect,
) -> tuple[str, ...]:
    """Read only bounded composite-v2 safety invariants from persisted evidence."""

    checked_at = _require_aware_datetime(now).astimezone(UTC).replace(tzinfo=None)
    stale_before = checked_at - stale_after
    required_tables = {
        "strategy_management_batches",
        "strategy_management_legs",
        "strategy_management_components",
        "position_mutation_intents",
        "position_protection_ledger",
    }
    uri = f"file:{database_path}?mode=ro"
    reasons: set[str] = set()
    with connect(uri, uri=True) as connection:
        available = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        # A pre-v2 checkout is version context, not a production safety failure.
        if not required_tables.issubset(available):
            return ()
        batches = connection.execute(
            """
            SELECT id, status, management_contract_json
            FROM strategy_management_batches
            WHERE management_contract_json IS NOT NULL
            ORDER BY id DESC
            """
        ).fetchall()
        batch_ids = [int(row[0]) for row in batches]
        if not batch_ids:
            return ()
        live_position_sizes = (
            _read_fresh_live_position_sizes(
                live_position_snapshot_path, now=now
            )
            if live_position_snapshot_path is not None
            else {}
        )
        placeholders = ",".join("?" for _ in batch_ids)
        legs = connection.execute(
            f"""
            SELECT id, management_batch_id, execution_order_leg_id, pos_id
            FROM strategy_management_legs
            WHERE management_batch_id IN ({placeholders})
            """,
            batch_ids,
        ).fetchall()
        components = connection.execute(
            f"""
            SELECT id, management_batch_id, strategy_management_leg_id,
                   component_kind, status, desired_json, evidence_json,
                   last_progress_at, updated_at
            FROM strategy_management_components
            WHERE management_batch_id IN ({placeholders})
            ORDER BY id
            """,
            batch_ids,
        ).fetchall()

        components_by_batch: dict[int, list[tuple]] = {}
        for row in components:
            components_by_batch.setdefault(int(row[1]), []).append(row)
        leg_scopes_by_batch: dict[int, set[str]] = {}
        for row in legs:
            leg_scopes_by_batch.setdefault(int(row[1]), set()).add(str(row[0]))
        for batch_id, status, contract_json in batches:
            try:
                contract = json.loads(contract_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                contract = {}
            rows = components_by_batch.get(int(batch_id), [])
            if str(status) == "succeeded":
                by_scope_kind: dict[tuple[str, str], list[tuple]] = {}
                for row in rows:
                    by_scope_kind.setdefault(
                        (str(row[2]), str(row[3])), []
                    ).append(row)
                required = {
                    str(item.get("component_kind") if isinstance(item, dict) else item)
                    for item in (contract.get("required_components") or [])
                }
                scopes = leg_scopes_by_batch.get(int(batch_id), set())
                if required and not scopes:
                    reasons.add("completed_batch_missing_component_evidence")
                for scope in scopes:
                    for kind in required:
                        matches = by_scope_kind.get((scope, kind), [])
                        row = matches[0] if len(matches) == 1 else None
                        try:
                            evidence = json.loads(row[6] or "[]") if row else []
                        except (TypeError, ValueError, json.JSONDecodeError):
                            evidence = []
                        if (
                            row is None
                            or str(row[4]) != "confirmed"
                            or not evidence
                        ):
                            reasons.add(
                                "completed_batch_missing_component_evidence"
                            )
                            break

        active_component_states = {
            "pending", "preflighting", "submitting", "awaiting_exchange",
            "recovery_required",
        }
        batch_status_by_id = {int(row[0]): str(row[1]) for row in batches}
        for row in components:
            if (
                str(row[4]) not in active_component_states
                or batch_status_by_id.get(int(row[1]))
                in {"succeeded", "blocked", "resolved"}
            ):
                continue
            rendered = row[7] or row[8]
            try:
                progressed_at = datetime.fromisoformat(str(rendered))
            except (TypeError, ValueError):
                reasons.add("stalled_composite_component")
                continue
            if progressed_at.tzinfo is not None:
                progressed_at = progressed_at.astimezone(UTC).replace(tzinfo=None)
            if progressed_at <= stale_before:
                reasons.add("stalled_composite_component")

        duplicate = connection.execute(
            """
            SELECT substr(idempotency_key, 1, instr(idempotency_key, ':close:') - 1),
                   COUNT(*)
            FROM position_mutation_intents
            WHERE operation = 'close_position'
              AND instr(idempotency_key, ':close:') > 1
              AND status IN ('submitting', 'submitted', 'recovery_required')
            GROUP BY 1 HAVING COUNT(*) > 1 LIMIT 1
            """
        ).fetchone()
        if duplicate is not None:
            reasons.add("duplicate_composite_close_submission")

        leg_by_id = {int(row[0]): row for row in legs}
        for component in components:
            is_confirmed = str(component[4]) == "confirmed"
            succeeded_batch = batch_status_by_id.get(int(component[1])) == "succeeded"
            if (not is_confirmed and not succeeded_batch) or component[2] is None:
                continue
            leg = leg_by_id.get(int(component[2]))
            if leg is None:
                continue
            ledger = connection.execute(
                """
                SELECT purpose, size_text, status
                FROM position_protection_ledger
                WHERE execution_order_leg_id = ? AND pos_id = ?
                """,
                (int(leg[2]), str(leg[3])),
            ).fetchall()
            if str(component[3]) == "converge_partial_close" and is_confirmed:
                try:
                    desired = json.loads(component[5] or "{}")
                    remaining = Decimal(str(desired["target_remaining_size"]))
                    retained = sum(
                        Decimal(str(row[1]))
                        for row in ledger
                        if row[0] == "take_profit"
                        and row[2] == "verified"
                        and row[1] not in (None, "")
                    )
                except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
                    continue
                live_size = (
                    live_position_sizes.get(str(leg[3]), Decimal("0"))
                    if live_position_snapshot_path is not None
                    else remaining
                )
                if retained > live_size:
                    reasons.add("live_position_retained_tp_oversized")
            elif str(component[3]) == "replace_remaining_protection":
                verified_stops = {
                    str(row[0]) for row in ledger
                    if row[0] in {"stop_loss", "backup_stop"}
                    and row[2] == "verified"
                }
                if verified_stops != {"stop_loss", "backup_stop"}:
                    reasons.add("composite_position_without_verified_stop")
    return tuple(sorted(reasons))


def _read_fresh_live_position_sizes(
    path: str | Path, *, now: datetime, max_age: timedelta = timedelta(minutes=5)
) -> dict[str, Decimal]:
    source = Path(path)
    if source.stat().st_size > _MAX_HTTP_OUTPUT_BYTES:
        raise RuntimeError("live_position_snapshot_invalid")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        captured_at = datetime.fromisoformat(str(payload["captured_at"]))
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        checked_at = _require_aware_datetime(now).astimezone(UTC)
        if captured_at.astimezone(UTC) < checked_at - max_age:
            raise RuntimeError("live_position_snapshot_stale")
        live_source = payload["payload"]["_live_source"]
        positions = live_source["positions"]
        if not isinstance(positions, list):
            raise TypeError("positions_not_list")
        result: dict[str, Decimal] = {}
        for row in positions:
            pos_id = str(row.get("posId") or "")
            if not pos_id or pos_id in result:
                raise ValueError("position_identity_invalid")
            size = Decimal(str(row.get("pos")))
            if size < 0:
                raise ValueError("position_size_invalid")
            result[pos_id] = size
        return result
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise RuntimeError("live_position_snapshot_invalid") from exc


def read_loopback_settings(
    url: str,
    *,
    timeout_seconds: float = 30.0,
) -> Mapping[str, Any]:
    """Read the local settings endpoint while refusing any non-loopback URL."""

    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("settings URL must use loopback HTTP")
    body = bytearray()
    with httpx.stream(
        "GET",
        url,
        timeout=timeout_seconds,
        trust_env=False,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_HTTP_OUTPUT_BYTES:
                raise ValueError("settings response too large")
    payload = json.loads(
        body,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, Mapping):
        raise ValueError("settings response must be an object")
    return {
        "auto_trade_enabled": payload.get("auto_trade_enabled"),
        "management_execution_mode": payload.get("management_execution_mode"),
        "max_concurrent_positions": payload.get("max_concurrent_positions"),
        "entry_preamble_mode": payload.get("entry_preamble_mode"),
    }


def run_production_safety_monitor(
    *,
    expectations: MonitorExpectations,
    state_path: str | Path,
    adapters,
    now: datetime,
    notify: bool,
    force_full_audit: bool = False,
    abnormal_event_limit: int = 100,
    lookback: timedelta = _DEFAULT_LOOKBACK,
    load_bot_config=_load_monitor_bot_config,
    send_bot_message=send_system_operator_bot_message,
    runtime_incident_session_factory=None,
    runtime_incident_config: RuntimeIncidentConfig | None = None,
    runtime_incident_capture_url: str | None = None,
    runtime_incident_capture_token: str | None = None,
    send_runtime_incident_capture=send_monitor_incident_capture,
) -> MonitorRunOutcome:
    """Collect, evaluate, deduplicate, optionally notify, and persist state."""

    checked_at = _require_aware_datetime(now)
    loaded_state = _load_monitor_state(state_path)
    state = loaded_state.state
    state_integrity_alert_pending = (
        loaded_state.invalid_existing_file
        or (
            state.anomaly_fingerprint is not None
            and state.last_notification_at is None
        )
    )
    if state.last_window_at is None:
        since = checked_at - lookback
    else:
        since = datetime.fromisoformat(state.last_window_at)

    failures: list[str] = []
    service_state = _read_adapter(adapters.read_service_state, "service", failures, "unknown")
    head = _read_adapter(adapters.read_git_head, "head", failures, "0" * 40)
    settings = _read_adapter(adapters.read_settings, "settings", failures, {})
    journal_error_count = _read_adapter(
        lambda: adapters.count_journal_errors(since=since),
        "journal",
        failures,
        0,
    )
    abnormal_events = _read_adapter(
        lambda: adapters.read_abnormal_events(
            since=since,
            limit=abnormal_event_limit,
        ),
        "events",
        failures,
        (),
    )
    composite_reader = getattr(adapters, "read_composite_invariants", None)
    composite_invariant_codes = (
        _read_adapter(
            lambda: composite_reader(now=checked_at),
            "composite",
            failures,
            (),
        )
        if callable(composite_reader)
        else ()
    )
    entry_preamble_reader = getattr(
        adapters, "read_entry_preamble_invariants", None
    )
    entry_preamble_invariant_codes = (
        _read_adapter(
            lambda: entry_preamble_reader(now=checked_at),
            "entry_preamble",
            failures,
            (),
        )
        if callable(entry_preamble_reader)
        else ()
    )
    window_sources_complete = not {"journal", "events"}.intersection(failures)

    audit = None
    audit_ran = should_run_daily_audit(
        now=checked_at,
        last_successful_date=state.last_full_audit_date,
        force=force_full_audit,
    )
    if audit_ran:
        audit = _read_adapter(
            lambda: run_daily_management_audit(adapters.run_management_audit),
            "audit",
            failures,
            None,
        )

    result = evaluate_monitor_snapshot(
        MonitorSnapshot(
            service_state=service_state,
            head=head,
            settings=settings,
            journal_error_count=journal_error_count,
            abnormal_events=abnormal_events,
            audit=audit,
            composite_invariant_codes=composite_invariant_codes,
            entry_preamble_invariant_codes=entry_preamble_invariant_codes,
            adapter_failures=tuple(failures),
            state_invalid=state_integrity_alert_pending,
        ),
        expectations,
    )

    successful_audit_date = state.last_full_audit_date
    if audit is not None and _audit_result_is_healthy(audit):
        successful_audit_date = checked_at.astimezone(_SHANGHAI).date().isoformat()
    base_state = MonitorState(
        last_window_at=(checked_at if window_sources_complete else since).isoformat(),
        last_full_audit_date=successful_audit_date,
        anomaly_fingerprint=state.anomaly_fingerprint,
        last_notification_at=state.last_notification_at,
        active_reason_codes=state.active_reason_codes,
    )
    audit_rechecked_healthy = (
        audit_ran
        and audit is not None
        and _audit_result_is_healthy(audit)
    )
    decision = decide_monitor_notification(
        result,
        base_state,
        now=checked_at,
        audit_rechecked_healthy=audit_rechecked_healthy,
    )
    if state_integrity_alert_pending and not result.healthy:
        # A fingerprint without a delivery timestamp is the four-field schema's
        # durable marker for a repaired state-integrity alert awaiting delivery.
        # Keep operational progress while notify=False, config failure, or send
        # failure prevents acknowledgement by the operator.
        next_state = MonitorState(
            last_window_at=base_state.last_window_at,
            last_full_audit_date=base_state.last_full_audit_date,
            anomaly_fingerprint=fingerprint_monitor_result(result),
            last_notification_at=None,
            active_reason_codes=base_state.active_reason_codes,
        )
    else:
        next_state = decision.next_state if not decision.should_notify else base_state
    notification_status = (
        "not_needed"
        if result.healthy and not decision.should_notify
        else "disabled"
    )
    monitor_error = None

    if decision.should_notify and notify:
        try:
            config = load_bot_config()
        except Exception:
            config = None
        if not system_operator_bot_enabled(config):
            notification_status = "config_missing"
            monitor_error = "notification_config_missing"
        else:
            try:
                _run_maybe_awaitable(
                    send_bot_message(
                        config=config,
                        text=(
                            format_monitor_recovery(checked_at=checked_at)
                            if decision.kind == "recovery"
                            else format_monitor_alert(result, checked_at=checked_at)
                        ),
                    )
                )
            except Exception:
                notification_status = "delivery_failed"
                monitor_error = "notification_delivery_failed"
            else:
                notification_status = "sent"
                next_state = decision.next_state
                continuing_reasons = tuple(
                    reason
                    for reason in result.reason_codes
                    if reason != "state_invalid"
                )
                if state_integrity_alert_pending and continuing_reasons:
                    # The delivered message included state_invalid, but that
                    # synthetic one-shot reason is acknowledged now. Persist
                    # only the continuing real anomaly so it keeps its normal
                    # six-hour dedupe identity on the repaired next run.
                    continuing_result = MonitorResult(
                        healthy=False,
                        reason_codes=continuing_reasons,
                        details=result.details,
                    )
                    next_state = MonitorState(
                        last_window_at=decision.next_state.last_window_at,
                        last_full_audit_date=decision.next_state.last_full_audit_date,
                        anomaly_fingerprint=fingerprint_monitor_result(
                            continuing_result
                        ),
                        last_notification_at=decision.next_state.last_notification_at,
                        active_reason_codes=tuple(sorted(continuing_reasons)),
                    )
                elif state_integrity_alert_pending:
                    next_state = MonitorState(
                        last_window_at=decision.next_state.last_window_at,
                        last_full_audit_date=decision.next_state.last_full_audit_date,
                        anomaly_fingerprint=decision.next_state.anomaly_fingerprint,
                        last_notification_at=decision.next_state.last_notification_at,
                        active_reason_codes=(),
                    )
    elif not result.healthy and not decision.should_notify:
        notification_status = "suppressed"
        next_state = decision.next_state

    try:
        save_monitor_state(state_path, next_state)
    except (OSError, TypeError, ValueError):
        monitor_error = "state_write_failed"

    if runtime_incident_session_factory is not None:
        incident_config_loader = (
            (lambda: runtime_incident_config)
            if runtime_incident_config is not None
            else load_runtime_incident_config
        )
        capture_runtime_incident_best_effort(
            capture_monitor_state,
            runtime_incident_session_factory,
            config_loader=incident_config_loader,
            checked_at=checked_at,
            reason_codes=result.reason_codes,
            adapter_failures=tuple(failures),
        )
        if notification_status in {"config_missing", "delivery_failed"}:
            capture_runtime_incident_best_effort(
                capture_notification_failure,
                runtime_incident_session_factory,
                config_loader=incident_config_loader,
                source_kind="production_safety_monitor_notification",
                source_record_id=fingerprint_monitor_result(result),
                error_type=monitor_error,
                occurred_at=checked_at,
            )
        capture_uncaptured_runtime_incident_sources(
            runtime_incident_session_factory,
            config_loader=incident_config_loader,
        )
    if runtime_incident_capture_url and runtime_incident_capture_token:
        try:
            send_runtime_incident_capture(
                runtime_incident_capture_url,
                token=runtime_incident_capture_token,
                projection=build_monitor_incident_capture_projection(
                    checked_at=checked_at,
                    reason_codes=result.reason_codes,
                    adapter_failures=tuple(failures),
                    notification_status=notification_status,
                    monitor_error=monitor_error,
                ),
            )
        except Exception:
            logger.warning("Monitor incident capture writer is unavailable")

    return MonitorRunOutcome(
        result=result,
        notification_status=notification_status,
        audit_ran=audit_ran,
        monitor_error=monitor_error,
    )


def capture_uncaptured_runtime_incident_sources(
    session_factory,
    *,
    config_loader=load_runtime_incident_config,
    limit: int = 100,
) -> int:
    """Best-effort scan durable sources outside listener and trading paths."""

    from telegram_kol_research.models import (
        PositionBackupStopOrder,
        PositionProtectionIncident,
        RuntimeIncident,
        StrategyManagementBatch,
        StrategyManagementLeg,
        TriggerProtectionIntent,
        TriggerTakeProfitConvergence,
    )
    from sqlalchemy import String, and_, cast, exists, func, literal, or_

    try:
        config = config_loader()
        capture_management = any(
            config.captures(incident_type)
            for incident_type in (
                "management_submit_unknown",
                "management_partial_failed",
                "management_recovery_required",
            )
        )
        capture_protection = config.captures("severe_protection_incident")
        if not capture_management and not capture_protection:
            return 0
        bounded_limit = max(1, min(int(limit), 100))
        captured = 0
        if capture_management:
            incident_types = {
                "submit_unknown": "management_submit_unknown",
                "partial_failed": "management_partial_failed",
                "recovery_required": "management_recovery_required",
            }
            enabled_incident_types = tuple(
                (status, incident_type)
                for status, incident_type in incident_types.items()
                if config.captures(incident_type)
            )
            projected_batches = []
            with session_factory() as session:
                for status, incident_type in enabled_incident_types:
                    remaining = bounded_limit - len(projected_batches)
                    if remaining <= 0:
                        break
                    status_on_leg = exists().where(
                        and_(
                            StrategyManagementLeg.management_batch_id
                            == StrategyManagementBatch.id,
                            StrategyManagementLeg.status == status,
                        )
                    )
                    already_captured = exists().where(
                        and_(
                            RuntimeIncident.source_kind
                            == "strategy_management_batch",
                            RuntimeIncident.source_record_id
                            == cast(StrategyManagementBatch.id, String),
                            RuntimeIncident.incident_type == incident_type,
                        )
                    )
                    batches = (
                        session.query(StrategyManagementBatch)
                        .filter(
                            or_(
                                StrategyManagementBatch.status == status,
                                status_on_leg,
                            ),
                            ~already_captured,
                        )
                        .order_by(StrategyManagementBatch.id.asc())
                        .limit(remaining)
                        .all()
                    )
                    projected_batches.extend(
                        (
                            int(batch.id),
                            status,
                            str(batch.reason_code or "") or None,
                            batch.updated_at,
                        )
                        for batch in batches
                    )
            for batch_id, status, reason_code, updated_at in projected_batches:
                occurred_at = updated_at
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=UTC)
                row = capture_management_state(
                    session_factory,
                    config=config,
                    batch_id=batch_id,
                    status=status,
                    reason_code=reason_code,
                    occurred_at=occurred_at,
                )
                captured += int(row is not None)

        if capture_protection:
            with session_factory() as session:
                sources = (
                    session.query(PositionProtectionIncident)
                    .outerjoin(
                        RuntimeIncident,
                        and_(
                            RuntimeIncident.source_kind
                            == "position_protection_incident",
                            RuntimeIncident.source_record_id
                            == cast(PositionProtectionIncident.id, String),
                        ),
                    )
                    .filter(RuntimeIncident.id.is_(None))
                    .order_by(PositionProtectionIncident.id.asc())
                    .limit(bounded_limit)
                    .all()
                )
                projected_sources = [
                    (
                        int(source.id),
                        str(source.incident_type),
                        source.created_at,
                        current_protection_incident_health_status(
                            session, incident=source
                        ),
                        bool(
                            session.query(PositionBackupStopOrder.id)
                            .filter(
                                PositionBackupStopOrder.execution_order_leg_id
                                == source.execution_order_leg_id,
                                PositionBackupStopOrder.pos_id == source.pos_id,
                                PositionBackupStopOrder.status == "active",
                                PositionBackupStopOrder.order_id.is_not(None),
                            )
                            .first()
                        ),
                    )
                    for source in sources
                ]
            for (
                source_id,
                incident_type,
                created_at,
                current_health_status,
                exact_backup_verified,
            ) in projected_sources:
                classification = classify_protection_incident(
                    incident_type,
                    exact_backup_verified=exact_backup_verified,
                )
                if classification == "healthy":
                    continue
                occurred_at = created_at
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=UTC)
                row = capture_protection_state(
                    session_factory,
                    config=config,
                    source_record_id=str(source_id),
                    severity="high" if classification == "critical" else "medium",
                    reason_code=incident_type,
                    occurred_at=occurred_at,
                    current_health_status=current_health_status,
                )
                captured += int(row is not None)
            now = datetime.now(UTC)
            with session_factory() as session:
                convergence_source_id = (
                    literal("tp-convergence-")
                    + cast(TriggerTakeProfitConvergence.id, String)
                    + literal("-")
                    + TriggerTakeProfitConvergence.status
                    + literal("-")
                    + func.coalesce(
                        TriggerTakeProfitConvergence.reason_code, "none"
                    )
                )
                convergence_already_captured = exists().where(and_(
                    RuntimeIncident.source_kind
                    == "position_protection_incident",
                    RuntimeIncident.source_record_id == convergence_source_id,
                    RuntimeIncident.incident_type
                    == "severe_protection_incident",
                ))
                intent_source_id = (
                    literal("trigger-intent-")
                    + cast(TriggerProtectionIntent.id, String)
                    + literal("-")
                    + TriggerProtectionIntent.recovery_state
                    + literal("-")
                    + func.coalesce(
                        TriggerProtectionIntent.recovery_disposition, "none"
                    )
                    + literal("-")
                    + cast(TriggerProtectionIntent.retry_attempts, String)
                    + literal("-")
                    + func.coalesce(
                        func.strftime(
                            "%Y%m%d%H%M%S",
                            TriggerProtectionIntent.next_attempt_at,
                        ),
                        "none",
                    )
                    + literal("-")
                    + func.coalesce(
                        TriggerProtectionIntent.last_reason_code, "none"
                    )
                )
                intent_already_captured = exists().where(and_(
                    RuntimeIncident.source_kind
                    == "position_protection_incident",
                    RuntimeIncident.source_record_id == intent_source_id,
                    RuntimeIncident.incident_type
                    == "severe_protection_incident",
                ))
                exact_backup_exists = exists().where(and_(
                    PositionBackupStopOrder.execution_order_leg_id
                    == TriggerProtectionIntent.execution_order_leg_id,
                    PositionBackupStopOrder.status == "active",
                    PositionBackupStopOrder.order_id.is_not(None),
                ))
                convergence_rows = (
                    session.query(TriggerTakeProfitConvergence)
                    .filter(or_(
                        TriggerTakeProfitConvergence.status == "submit_unknown",
                        TriggerTakeProfitConvergence.status == "conflicted",
                        TriggerTakeProfitConvergence.reason_code.like("%immutable%"),
                        TriggerTakeProfitConvergence.reason_code.like("%unowned%"),
                    ))
                    .filter(~convergence_already_captured)
                    .order_by(TriggerTakeProfitConvergence.id.asc())
                    .limit(bounded_limit)
                    .all()
                )
                intent_rows = (
                    session.query(TriggerProtectionIntent)
                    .filter(or_(
                        TriggerProtectionIntent.recovery_disposition.in_((
                            "manual_review", "terminal",
                        )),
                        TriggerProtectionIntent.recovery_state.in_((
                            "submit_unknown", "recovery_required",
                        )),
                        and_(
                            TriggerProtectionIntent.recovery_state.in_((
                                "pending", "retrying", "failed",
                            )),
                            TriggerProtectionIntent.next_attempt_at.is_not(None),
                            TriggerProtectionIntent.next_attempt_at <= now,
                            ~exact_backup_exists,
                        ),
                    ))
                    .filter(~intent_already_captured)
                    .order_by(TriggerProtectionIntent.id.asc())
                    .limit(bounded_limit)
                    .all()
                )
                projected_transitions = [
                    (
                        f"tp-convergence-{int(row.id)}-{row.status}-{row.reason_code or 'none'}",
                        str(row.reason_code or f"convergence_{row.status}"),
                        row.updated_at,
                        "critical",
                    )
                    for row in convergence_rows
                ] + [
                    (
                        _trigger_intent_transition_source_id(row),
                        str(
                            row.last_reason_code
                            or row.recovery_disposition
                            or f"trigger_protection_{row.recovery_state}"
                        ),
                        row.updated_at,
                        "high",
                    )
                    for row in intent_rows
                ]
            projected_transitions = projected_transitions[:bounded_limit]
            for source_record_id, reason_code, updated_at, severity in projected_transitions:
                occurred_at = updated_at
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=UTC)
                row = capture_protection_state(
                    session_factory,
                    config=config,
                    source_record_id=source_record_id,
                    severity=severity,
                    reason_code=reason_code,
                    occurred_at=occurred_at,
                )
                captured += int(row is not None)
        return captured
    except Exception as exc:
        logger.warning(
            "Runtime incident durable source scan failed open: error=%s",
            type(exc).__name__,
        )
        return 0


def classify_protection_incident(
    incident_type: str,
    *,
    exact_backup_verified: bool,
) -> str:
    """Map bounded protection state to operator severity."""

    normalized = str(incident_type or "").strip().lower()
    if normalized in {
        "backup_stop_shadow_ready",
        "stop_rescue_shadow_ready",
        "take_profit_convergence_ready",
        "take_profit_convergence_completed",
        "trigger_protection_assignment_shadow_plan",
    }:
        return "healthy"
    if normalized in {
        "backup_exchange_outcome_unknown",
        "convergence_submit_unknown",
        "immutable_ownership_conflict",
        "position_owner_unverified",
    }:
        return "critical"
    if exact_backup_verified and normalized in {
        "protection_assignment_not_mutual_unique",
        "trigger_protection_assignment_not_mutual_unique",
        "backup_stop_blocked",
        "native_stop_assignment_pending",
        "stop_trigger_failed",
    }:
        return "warning"
    return "critical"


def _trigger_intent_transition_source_id(intent) -> str:
    next_attempt = getattr(intent, "next_attempt_at", None)
    next_attempt_marker = (
        next_attempt.strftime("%Y%m%d%H%M%S")
        if isinstance(next_attempt, datetime)
        else "none"
    )
    return (
        f"trigger-intent-{int(intent.id)}-{intent.recovery_state}-"
        f"{intent.recovery_disposition or 'none'}-{int(intent.retry_attempts)}-"
        f"{next_attempt_marker}-{intent.last_reason_code or 'none'}"
    )


def send_monitor_test_notification(
    *,
    load_bot_config=_load_monitor_bot_config,
    send_bot_message=send_system_operator_bot_message,
) -> str:
    """Send only the fixed, clearly labelled monitor delivery test."""

    config = load_bot_config()
    if not system_operator_bot_enabled(config):
        raise RuntimeError("notification_config_missing")
    _run_maybe_awaitable(
        send_bot_message(config=config, text=MONITOR_TEST_NOTIFICATION_TEXT)
    )
    return "sent"


def _run_bounded_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
    cwd: str | Path | None = None,
) -> _CommandResult:
    if (
        not isinstance(argv, (tuple, list))
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError("command argv must contain fixed nonempty strings")
    if timeout_seconds <= 0 or max_output_bytes < 0:
        raise ValueError("command bounds must be nonnegative")
    process = subprocess.Popen(
        tuple(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("command_output_invalid")
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_and_wait(process)
                raise subprocess.TimeoutExpired(tuple(argv), timeout_seconds)
            events = selector.select(remaining)
            if not events:
                _kill_and_wait(process)
                raise subprocess.TimeoutExpired(tuple(argv), timeout_seconds)
            for key, _ in events:
                chunk = os.read(
                    key.fd,
                    min(65_536, max_output_bytes - len(output) + 1),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    _kill_and_wait(process)
                    raise RuntimeError("command_output_too_large")
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_and_wait(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
    return _CommandResult(
        returncode=returncode,
        output=bytes(output).decode("utf-8", errors="strict"),
    )


def _kill_and_wait(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _read_adapter(reader, name: str, failures: list[str], fallback):
    try:
        return reader()
    except Exception:
        failures.append(name)
        return fallback


def _audit_result_is_healthy(audit: Mapping[str, Any]) -> bool:
    reasons: set[str] = set()
    details: dict[str, Any] = {}
    _evaluate_audit(audit, reasons, details)
    return not reasons


def _run_maybe_awaitable(value: object) -> object:
    if inspect.isawaitable(value):
        import asyncio

        return asyncio.run(value)
    return value


def _require_aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def fingerprint_monitor_result(result: MonitorResult) -> str:
    """Return a canonical SHA-256 for one bounded, secret-free result summary."""

    unknown_reason = any(
        code not in _FIXED_REASON_CODES for code in result.reason_codes
    )
    reason_codes = (
        ["unknown_monitor_problem"]
        if unknown_reason
        else sorted(set(result.reason_codes))
    )
    if not reason_codes and not result.healthy:
        reason_codes = ["unknown_monitor_problem"]
    detail_keys = {
        key
        for reason in reason_codes
        for key in _FINGERPRINT_DETAIL_KEYS_BY_REASON.get(reason, ())
    }
    details: dict[str, object] = {}
    for key in sorted(detail_keys):
        if key not in result.details:
            continue
        rendered = _safe_fingerprint_detail(key, result.details[key])
        if rendered is not None:
            details[key] = rendered
    presentation = build_monitor_alert_presentation(result)
    canonical = json.dumps(
        {
            "details": details,
            "healthy": result.healthy is True,
            "reason_codes": reason_codes,
            "severity": presentation.severity,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _safe_fingerprint_detail(key: str, value: object) -> object | None:
    if key == "composite_invariant_codes":
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            return None
        codes = tuple(sorted(set(value)))
        return list(codes) if set(codes) <= _COMPOSITE_INVARIANT_CODES else None
    if key == "audit_state_counts":
        if not isinstance(value, Mapping):
            return None
        rendered: dict[str, int] = {}
        for state in _AUDIT_ALERT_STATES:
            count = _safe_count(value.get(state))
            if count is None:
                return None
            rendered[state] = count
        return rendered
    if key == "actionable_batch_refs":
        return _safe_fingerprint_actionable_refs(value)
    if key == "actionable_batches_truncated":
        return value if type(value) is bool else None
    rendered = _safe_detail_value(key, value)
    return rendered


def _safe_fingerprint_actionable_refs(value: object) -> list[dict[str, object]] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > _MAX_ACTIONABLE_BATCH_REFS
    ):
        return None
    rendered: list[dict[str, object]] = []
    seen_refs: set[str] = set()
    state_order = {state: index for index, state in enumerate(_AUDIT_ALERT_STATES)}
    for item in value:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            return None
        batch_ref, states = item
        if (
            not isinstance(batch_ref, str)
            or _ACTIONABLE_BATCH_REF.fullmatch(batch_ref) is None
            or batch_ref in seen_refs
            or not isinstance(states, Sequence)
            or isinstance(states, (str, bytes, bytearray))
            or not states
        ):
            return None
        normalized_states = tuple(str(state) for state in states)
        if (
            any(state not in state_order for state in normalized_states)
            or len(set(normalized_states)) != len(normalized_states)
            or tuple(sorted(normalized_states, key=state_order.__getitem__))
            != normalized_states
        ):
            return None
        seen_refs.add(batch_ref)
        rendered.append({"batch_ref": batch_ref, "states": list(normalized_states)})
    return rendered


def decide_monitor_notification(
    result: MonitorResult,
    state: MonitorState,
    *,
    now: datetime,
    audit_rechecked_healthy: bool = False,
) -> MonitorNotificationDecision:
    """Apply fingerprint change and reason-aware repeat-notification policy."""

    _monitor_state_payload(state)
    now_rendered = _canonical_aware_datetime(now)
    if result.healthy:
        active_reasons = set(state.active_reason_codes)
        if not active_reasons:
            return MonitorNotificationDecision(
                should_notify=False,
                next_state=MonitorState(
                    last_window_at=state.last_window_at,
                    last_full_audit_date=state.last_full_audit_date,
                    anomaly_fingerprint=None,
                    last_notification_at=state.last_notification_at,
                ),
                kind="none",
            )
        unresolved_audit_reasons = active_reasons.intersection(
            {"audit_abnormal", "audit_incomplete"}
        )
        if unresolved_audit_reasons and not audit_rechecked_healthy:
            return MonitorNotificationDecision(
                should_notify=False,
                next_state=MonitorState(
                    last_window_at=state.last_window_at,
                    last_full_audit_date=state.last_full_audit_date,
                    anomaly_fingerprint=state.anomaly_fingerprint,
                    last_notification_at=state.last_notification_at,
                    active_reason_codes=tuple(sorted(unresolved_audit_reasons)),
                ),
                kind="none",
            )
        return MonitorNotificationDecision(
            should_notify=True,
            next_state=MonitorState(
                last_window_at=state.last_window_at,
                last_full_audit_date=state.last_full_audit_date,
                anomaly_fingerprint=None,
                last_notification_at=now_rendered,
            ),
            kind="recovery",
        )

    fingerprint = fingerprint_monitor_result(result)
    should_notify = fingerprint != state.anomaly_fingerprint
    if not should_notify:
        if state.last_notification_at is None:
            should_notify = True
        elif _uses_low_repeat_policy(result):
            should_notify = False
        else:
            last_notification = datetime.fromisoformat(state.last_notification_at)
            should_notify = now - last_notification >= _NOTIFICATION_SUPPRESSION
    if not should_notify:
        return MonitorNotificationDecision(
            should_notify=False,
            next_state=state,
            kind="none",
        )
    active_reason_codes = {
        reason
        for reason in result.reason_codes
        if reason in _FIXED_REASON_CODES
    }
    if not audit_rechecked_healthy:
        active_reason_codes.update(
            reason
            for reason in state.active_reason_codes
            if reason in {"audit_abnormal", "audit_incomplete"}
        )
    return MonitorNotificationDecision(
        should_notify=True,
        next_state=MonitorState(
            last_window_at=state.last_window_at,
            last_full_audit_date=state.last_full_audit_date,
            anomaly_fingerprint=fingerprint,
            last_notification_at=now_rendered,
            active_reason_codes=tuple(sorted(active_reason_codes)),
        ),
        kind="anomaly",
    )


def _uses_low_repeat_policy(result: MonitorResult) -> bool:
    reason_codes = set(result.reason_codes)
    return bool(reason_codes) and reason_codes <= _LOW_REPEAT_REASON_CODES


def evaluate_monitor_snapshot(
    snapshot: MonitorSnapshot,
    expectations: MonitorExpectations,
) -> MonitorResult:
    """Evaluate one already-collected snapshot without I/O or raw error retention."""

    reasons: set[str] = set()
    details: dict[str, Any] = {}

    service_state = _safe_service_state(snapshot.service_state)
    if service_state is None:
        reasons.add("malformed_snapshot")
        service_state = "invalid"
    if service_state != "active":
        reasons.add("service_inactive")
        details["service_state"] = service_state

    observed_head = _safe_git_head(snapshot.head)
    expected_head = _safe_git_head(expectations.head)
    if observed_head is None or expected_head is None:
        reasons.add("malformed_snapshot")
    elif observed_head != expected_head:
        details["head"] = observed_head or "invalid"
        details["expected_head"] = expected_head or "invalid"

    _evaluate_settings(snapshot.settings, expectations, reasons, details)

    journal_error_count = _safe_count(snapshot.journal_error_count)
    if journal_error_count is None:
        reasons.add("malformed_snapshot")
    elif journal_error_count:
        reasons.add("journal_errors")
        details["journal_error_count"] = journal_error_count

    adapter_failures = _safe_adapter_failures(snapshot.adapter_failures)
    if adapter_failures is None:
        reasons.add("malformed_snapshot")
    elif adapter_failures:
        reasons.add("adapter_failure")
        details["adapter_failures"] = adapter_failures

    if type(snapshot.state_invalid) is not bool:
        reasons.add("malformed_snapshot")
    elif snapshot.state_invalid:
        reasons.add("state_invalid")

    _evaluate_events(snapshot.abnormal_events, reasons, details)
    _evaluate_composite_invariants(
        snapshot.composite_invariant_codes, reasons, details
    )
    _evaluate_entry_preamble_invariants(
        snapshot.entry_preamble_invariant_codes, reasons, details
    )
    if snapshot.audit is not None:
        _evaluate_audit(snapshot.audit, reasons, details)

    reason_codes = tuple(sorted(reasons))
    return MonitorResult(
        healthy=not reason_codes,
        reason_codes=reason_codes,
        details=details,
    )


_COMPOSITE_INVARIANT_CODES = frozenset(
    {
        "completed_batch_missing_component_evidence",
        "duplicate_composite_close_submission",
        "live_position_retained_tp_oversized",
        "composite_position_without_verified_stop",
        "stalled_composite_component",
    }
)


def _evaluate_composite_invariants(
    values: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        reasons.add("malformed_snapshot")
        return
    observed: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value not in _COMPOSITE_INVARIANT_CODES:
            reasons.add("malformed_snapshot")
            continue
        observed.add(value)
    reasons.update(observed)
    if observed:
        details["composite_invariant_codes"] = tuple(sorted(observed))


_ENTRY_PREAMBLE_INVARIANT_CODES = frozenset(
    {
        "stale_entry_preamble_unresolved",
        "entry_preamble_ambiguous",
        "live_entry_preamble_binding_evidence_missing",
    }
)


def _evaluate_entry_preamble_invariants(
    values: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        reasons.add("malformed_snapshot")
        return
    observed: set[str] = set()
    for value in values:
        if value not in _ENTRY_PREAMBLE_INVARIANT_CODES:
            reasons.add("malformed_snapshot")
            continue
        observed.add(value)
    reasons.update(observed)
    if observed:
        details["entry_preamble_invariant_codes"] = tuple(sorted(observed))


def _evaluate_settings(
    settings: object,
    expectations: MonitorExpectations,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(settings, Mapping):
        reasons.add("malformed_snapshot")
        return

    auto_trade_enabled = settings.get("auto_trade_enabled")
    expected_auto_trade_enabled = expectations.auto_trade_enabled
    if (
        type(auto_trade_enabled) is not bool
        or type(expected_auto_trade_enabled) is not bool
    ):
        reasons.add("malformed_snapshot")
    elif auto_trade_enabled != expected_auto_trade_enabled:
        reasons.add("auto_trade_enabled_drift")
        details["auto_trade_enabled"] = auto_trade_enabled
        details["expected_auto_trade_enabled"] = expected_auto_trade_enabled

    management_mode = _safe_management_mode(settings.get("management_execution_mode"))
    expected_mode = _safe_management_mode(expectations.management_execution_mode)
    if management_mode is None or expected_mode is None:
        reasons.add("malformed_snapshot")
    elif management_mode != expected_mode:
        reasons.add("management_execution_mode_drift")
        details["management_execution_mode"] = management_mode
        details["expected_management_execution_mode"] = expected_mode

    position_limit = _safe_count(settings.get("max_concurrent_positions"))
    expected_limit = _safe_count(expectations.max_concurrent_positions)
    if position_limit is None or expected_limit is None:
        reasons.add("malformed_snapshot")
    elif position_limit != expected_limit:
        reasons.add("max_concurrent_positions_drift")
        details["max_concurrent_positions"] = position_limit
        details["expected_max_concurrent_positions"] = expected_limit

    entry_preamble_mode = _safe_entry_preamble_mode(
        settings.get("entry_preamble_mode")
    )
    expected_entry_preamble_mode = _safe_entry_preamble_mode(
        expectations.entry_preamble_mode
    )
    if entry_preamble_mode is None or expected_entry_preamble_mode is None:
        reasons.add("malformed_snapshot")
    elif entry_preamble_mode != expected_entry_preamble_mode:
        reasons.add("entry_preamble_mode_drift")
        details["entry_preamble_mode"] = entry_preamble_mode
        details["expected_entry_preamble_mode"] = expected_entry_preamble_mode


def _evaluate_events(
    events: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
        reasons.add("malformed_snapshot")
        return

    manual_close_positions: list[str] = []
    unknown_count = 0
    recovery_count = 0
    for event in events:
        if not isinstance(event, Mapping):
            reasons.add("malformed_snapshot")
            continue
        action = _safe_event_value(event.get("action"))
        status = _safe_event_value(event.get("status"))
        if action is None or status is None:
            reasons.add("malformed_snapshot")
            continue
        if status in _RECOVERY_EVENT_STATUSES:
            recovery_count += 1
        elif status not in _NORMAL_EVENT_STATUSES and not (
            action == "close_bound_position_reservation" and status == "reserved"
        ):
            unknown_count += 1

        if action == "close_bound_position_market":
            pos_id = _safe_event_value(event.get("pos_id"))
            if pos_id is None:
                reasons.add("malformed_snapshot")
            else:
                manual_close_positions.append(pos_id)

    if unknown_count:
        reasons.add("event_unknown_status")
        details["unknown_event_count"] = unknown_count
    if recovery_count:
        reasons.add("event_recovery_status")
        details["recovery_event_count"] = recovery_count

    duplicate_count = sum(
        count - 1 for count in Counter(manual_close_positions).values() if count > 1
    )
    if duplicate_count:
        reasons.add("duplicate_manual_close")
        details["duplicate_manual_close_count"] = duplicate_count


def _audit_safety_evidence_is_complete(audit: Mapping[str, Any]) -> bool:
    if (
        audit.get("output_complete") is True
        and audit.get("batches_truncated") is False
    ):
        return True
    if (
        audit.get("output_complete") is not False
        or audit.get("batches_truncated") is not True
    ):
        return False

    limit = _safe_count(audit.get("limit"))
    returned = _safe_count(audit.get("batches_returned"))
    counts = audit.get("counts")
    batches = audit.get("batches")
    if (
        limit is None
        or limit < 1
        or returned != limit
        or not isinstance(counts, Mapping)
        or not isinstance(batches, Sequence)
        or isinstance(batches, (str, bytes, bytearray))
        or len(batches) != returned
        or audit.get("all_history_legs_complete") is not True
    ):
        return False
    total = _safe_count(counts.get("batches_total"))
    if total is None or total <= returned:
        return False
    return all(
        isinstance(batch, Mapping) and batch.get("legs_truncated") is False
        for batch in batches
    )


def _evaluate_audit(
    audit: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(audit, Mapping):
        reasons.update({"audit_incomplete", "malformed_snapshot"})
        return

    incomplete = False
    abnormal = False

    if audit.get("snapshot_status") != "stable":
        incomplete = True
    if audit.get("snapshot_validation") != "ok":
        incomplete = True
    if not _audit_safety_evidence_is_complete(audit):
        incomplete = True
    if audit.get("schema_status") != "available":
        abnormal = True

    legacy = audit.get("legacy_pending_management")
    if not isinstance(legacy, Mapping):
        incomplete = True
        reasons.add("malformed_snapshot")
    elif (
        legacy.get("complete") is not True
        or legacy.get("truncated") is not False
        or legacy.get("scan_truncated") is not False
    ):
        incomplete = True

    counts = audit.get("counts")
    abnormal_count = 0
    audit_state_counts: dict[str, int] = {}
    if not isinstance(counts, Mapping):
        incomplete = True
        reasons.add("malformed_snapshot")
    else:
        for state in _AUDIT_ALERT_STATES:
            count = _safe_count(counts.get(state))
            if count is None:
                incomplete = True
                reasons.add("malformed_snapshot")
            else:
                abnormal_count += count
                audit_state_counts[state] = count
    if abnormal_count:
        abnormal = True
        details["audit_abnormal_count"] = abnormal_count

    actionable_batch_refs = _safe_actionable_batch_refs(
        audit.get("actionable_batches"),
        audit_state_counts=audit_state_counts,
    )
    if actionable_batch_refs is None:
        incomplete = True
        reasons.add("malformed_snapshot")
    else:
        total, truncated, batch_refs = actionable_batch_refs
        details["audit_state_counts"] = dict(audit_state_counts)
        details["actionable_batch_refs"] = batch_refs
        details["actionable_batches_total"] = total
        details["actionable_batches_truncated"] = truncated

    for field in ("malformed_row_count", "malformed_field_count"):
        count = _safe_count(audit.get(field))
        if count is None:
            incomplete = True
            reasons.add("malformed_snapshot")
        elif count:
            abnormal = True

    if incomplete:
        reasons.add("audit_incomplete")
        details["audit_complete"] = False
    if abnormal:
        reasons.add("audit_abnormal")
        details["audit_abnormal"] = True


def _safe_actionable_batch_refs(
    value: object,
    *,
    audit_state_counts: Mapping[str, int],
) -> tuple[int, bool, tuple[tuple[str, tuple[str, ...]], ...]] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "total",
        "returned",
        "truncated",
        "items",
    }:
        return None
    total = _safe_count(value.get("total"))
    returned = _safe_count(value.get("returned"))
    truncated = value.get("truncated")
    items = value.get("items")
    if (
        total is None
        or returned is None
        or type(truncated) is not bool
        or not isinstance(items, Sequence)
        or isinstance(items, (str, bytes, bytearray))
        or returned != min(total, _MAX_ACTIONABLE_BATCH_REFS)
        or truncated is not (total > _MAX_ACTIONABLE_BATCH_REFS)
        or len(items) != returned
    ):
        return None
    abnormal_count = sum(audit_state_counts.values())
    if total > abnormal_count or (abnormal_count > 0) is not (total > 0):
        return None

    rendered: list[tuple[str, tuple[str, ...]]] = []
    seen_refs: set[str] = set()
    state_order = {state: index for index, state in enumerate(_AUDIT_ALERT_STATES)}
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"batch_ref", "states"}:
            return None
        batch_ref = item.get("batch_ref")
        states = item.get("states")
        if (
            not isinstance(batch_ref, str)
            or _ACTIONABLE_BATCH_REF.fullmatch(batch_ref) is None
            or batch_ref in seen_refs
            or not isinstance(states, Sequence)
            or isinstance(states, (str, bytes, bytearray))
            or not states
            or any(state not in state_order for state in states)
        ):
            return None
        normalized_states = tuple(str(state) for state in states)
        if (
            len(set(normalized_states)) != len(normalized_states)
            or tuple(sorted(normalized_states, key=state_order.__getitem__))
            != normalized_states
        ):
            return None
        seen_refs.add(batch_ref)
        rendered.append((batch_ref, normalized_states))
    return total, truncated, tuple(rendered)


def format_monitor_alert(
    result: MonitorResult,
    *,
    checked_at: datetime | str,
) -> str:
    """Build one bounded operator-facing alert from fixed Chinese templates."""

    presentation = build_monitor_alert_presentation(result)
    if len(presentation.problems) == 1:
        problems = presentation.problems[0]
    else:
        problems = "\n".join(
            f"{index}. {problem}"
            for index, problem in enumerate(presentation.problems, start=1)
        )
    if presentation.additional_problem_count:
        problems += f"\n另外还有{presentation.additional_problem_count}个问题。"
    lines = [
        f"【{presentation.title}】",
        "",
        "发生了什么：",
        problems,
        "",
        "当前影响：",
        presentation.impact,
        "",
        "你需要做什么：",
        presentation.operator_action,
        "",
        "通知来源：",
        "系统定时安全检查，不是 AI Agent。",
        "",
        "排查信息：",
        f"检查时间：{_safe_checked_at_shanghai(checked_at)}",
        f"技术代码：{','.join(presentation.technical_codes)}",
    ]
    text = "\n".join(lines)
    if len(text) <= MAX_ALERT_LENGTH:
        return text
    fallback = (
        "【🔴立即处理：生产安全检查发现无法解释的问题】\n\n"
        "当前影响：\n生产安全状态暂时无法确认。\n\n"
        "你需要做什么：\n不要重复下单或平仓，请联系开发者检查生产安全监控。\n\n"
        "通知来源：\n系统定时安全检查，不是 AI Agent。"
    )
    return fallback[:MAX_ALERT_LENGTH]


def format_monitor_recovery(*, checked_at: datetime | str) -> str:
    """Render one fixed recovery notice after every active cause was rechecked."""

    return "\n".join(
        (
            "【🔵状态提醒：生产安全监控已恢复正常】",
            "",
            "发生了什么：",
            "系统已重新完成相关安全检查，先前记录的问题当前不再出现。",
            "",
            "当前影响：",
            "生产安全监控当前未发现仍在持续的同类问题。",
            "",
            "你需要做什么：",
            "无需处理，继续正常观察即可。",
            "",
            "通知来源：",
            "系统定时安全检查，不是 AI Agent。",
            "",
            "排查信息：",
            f"检查时间：{_safe_checked_at_shanghai(checked_at)}",
            "技术代码：monitor_recovered",
        )
    )


_ALERT_RULES: Mapping[str, tuple[str, str, str]] = {
    "service_inactive": (
        "critical",
        "自动交易服务未正常运行",
        "自动交易服务没有正常运行，新的 Telegram 消息可能无法处理。",
    ),
    "auto_trade_enabled_drift": (
        "critical",
        "自动交易开关与批准设置不同",
        "自动交易开关与批准设置不同。",
    ),
    "management_execution_mode_drift": (
        "critical",
        "仓位管理模式与批准设置不同",
        "仓位管理模式与批准设置不同。",
    ),
    "entry_preamble_mode_drift": (
        "critical",
        "前置仓位提示模式与批准设置不同",
        "前置仓位提示模式与批准设置不同。",
    ),
    "max_concurrent_positions_drift": (
        "critical",
        "持仓数量限制与批准设置不同",
        "持仓数量限制与批准设置不同。",
    ),
    "event_unknown_status": (
        "critical",
        "交易所结果无法确认",
        "交易请求已经发出，但交易所结果无法确认。",
    ),
    "event_recovery_status": (
        "critical",
        "仓位管理操作需要恢复",
        "仓位管理操作没有正常结束，需要恢复处理。",
    ),
    "duplicate_manual_close": (
        "critical",
        "发现重复平仓迹象",
        "同一仓位可能被重复发起平仓。",
    ),
    "adapter_failure": (
        "critical",
        "生产安全检查未完整运行",
        "安全监控无法读取关键生产信息。",
    ),
    "audit_incomplete": (
        "critical",
        "仓位管理记录检查未完成",
        "仓位管理记录检查没有完整完成。",
    ),
    "malformed_snapshot": (
        "critical",
        "安全检查数据无法识别",
        "安全检查收到无法识别的数据。",
    ),
    "audit_abnormal": (
        "review",
        "历史交易管理记录需要核查",
        "历史仓位管理任务缺少足够证据，无法确认当时是否完整结束。",
    ),
    "journal_errors": (
        "review",
        "交易服务近期出现程序错误",
        "交易服务近期记录了程序错误。",
    ),
    "state_invalid": (
        "review",
        "监控通知记录发生异常",
        "监控自己的通知记录发生异常或被重建。",
    ),
    "completed_batch_missing_component_evidence": (
        "critical", "复合仓位管理完成证据不足",
        "批次已标记完成，但存在组件未确认或缺少交易所证据。",
    ),
    "duplicate_composite_close_submission": (
        "critical", "复合指令可能重复平仓", "同一组件存在多个已发出的平仓意图。",
    ),
    "live_position_retained_tp_oversized": (
        "critical", "保留止盈数量超过剩余仓位", "剩余止盈单总量大于已核验的剩余仓位。",
    ),
    "composite_position_without_verified_stop": (
        "critical", "复合管理后缺少已核验止损", "剩余仓位没有同时核验主止损和备用止损。",
    ),
    "stalled_composite_component": (
        "critical", "复合仓位管理组件停滞", "有组件超过时限未取得持久化进展。",
    ),
}
_ALERT_REASON_PRIORITY = (
    "event_unknown_status",
    "duplicate_composite_close_submission",
    "composite_position_without_verified_stop",
    "live_position_retained_tp_oversized",
    "completed_batch_missing_component_evidence",
    "stalled_composite_component",
    "duplicate_manual_close",
    "service_inactive",
    "auto_trade_enabled_drift",
    "management_execution_mode_drift",
    "entry_preamble_mode_drift",
    "max_concurrent_positions_drift",
    "event_recovery_status",
    "adapter_failure",
    "audit_incomplete",
    "malformed_snapshot",
    "audit_abnormal",
    "journal_errors",
    "state_invalid",
)


def build_monitor_alert_presentation(result: MonitorResult) -> MonitorAlertPresentation:
    """Translate validated monitor facts into one deterministic operator message."""

    known_reasons = tuple(
        reason for reason in _ALERT_REASON_PRIORITY if reason in result.reason_codes
    )
    unknown_reason_present = any(
        reason not in _ALERT_RULES for reason in result.reason_codes
    )
    if not known_reasons or unknown_reason_present:
        return MonitorAlertPresentation(
            severity="critical",
            title="🔴立即处理：生产安全检查发现无法解释的问题",
            problems=("安全检查发现异常，但通知程序无法生成完整说明。",),
            impact="生产安全状态暂时无法确认。",
            operator_action="不要重复下单或平仓，请联系开发者检查生产安全监控。",
            technical_codes=("unknown_monitor_problem",),
        )

    audit_counts = result.details.get("audit_state_counts")
    audit_submit_unknown = (
        isinstance(audit_counts, Mapping)
        and _safe_count(audit_counts.get("submit_unknown")) not in {None, 0}
    )
    severity = "critical" if any(
        _ALERT_RULES[reason][0] == "critical" for reason in known_reasons
    ) or audit_submit_unknown else "review"
    primary_reason = known_reasons[0]
    title_text = _ALERT_RULES[primary_reason][1]
    total = _safe_count(result.details.get("actionable_batches_total"))
    if primary_reason == "audit_abnormal" and audit_submit_unknown:
        title_text = "存在结果无法确认的历史交易任务"
    elif primary_reason == "audit_abnormal" and total is not None and total > 0:
        title_text = f"{total}条历史交易管理记录无法确认"

    problems = tuple(_ALERT_RULES[reason][2] for reason in known_reasons[:3])
    batch_ids = _safe_actionable_batch_ids(result.details.get("actionable_batch_refs"))
    additional_batch_count = max(0, (total or 0) - len(batch_ids))

    if severity == "critical":
        if {"event_unknown_status", "event_recovery_status", "duplicate_manual_close"}.intersection(
            known_reasons
        ) or audit_submit_unknown:
            impact = "对应订单或仓位的最终状态尚未确认，重复操作可能扩大风险。"
            operator_action = "不要重复下单或平仓，请先由开发者核对交易所订单和当前仓位。"
        elif {"adapter_failure", "audit_incomplete", "malformed_snapshot"}.intersection(
            known_reasons
        ):
            impact = "安全检查未能完整完成，当前生产安全状态无法确认。"
            operator_action = "不要重复下单或平仓，请联系开发者恢复生产安全检查。"
        elif "service_inactive" in known_reasons:
            impact = "新的 Telegram 消息可能无法处理，自动交易流程可能已经中断。"
            operator_action = "不要反复发送交易指令，请联系开发者恢复自动交易服务。"
        else:
            impact = "生产风险设置与批准值不同，可能影响自动交易或仓位管理。"
            operator_action = "暂停新的手动操作，请联系开发者核对并恢复批准设置。"
    elif known_reasons == ("audit_abnormal",):
        impact = (
            "没有检测到交易服务停止或设置变化。仅凭现有历史资料，"
            "无法进一步确认这些记录是否影响过对应仓位。"
        )
        operator_action = "不需要立即操作，也不要手动重复平仓。安排开发者核查"
        if batch_ids:
            operator_action += "管理批次 " + "、".join(str(value) for value in batch_ids)
            if additional_batch_count:
                operator_action += (
                    f"（共{total}个，仅展示前{len(batch_ids)}个）"
                )
        else:
            operator_action += "对应管理记录"
        operator_action += "；状态不变时不会重复提醒。"
    else:
        impact = "交易服务仍可能运行，但这些问题的实际影响尚未确认。"
        operator_action = "不需要手动重复交易，请安排开发者检查相关日志和监控状态。"

    return MonitorAlertPresentation(
        severity=severity,
        title=("🔴立即处理：" if severity == "critical" else "🟡稍后核查：")
        + title_text,
        problems=problems,
        impact=impact,
        operator_action=operator_action,
        technical_codes=tuple(sorted(known_reasons)),
        actionable_batch_ids=batch_ids,
        additional_problem_count=max(0, len(known_reasons) - len(problems)),
        additional_batch_count=additional_batch_count,
    )


def _safe_actionable_batch_ids(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > _MAX_ACTIONABLE_BATCH_REFS
    ):
        return ()
    batch_ids: list[int] = []
    for item in value:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            return ()
        batch_ref = item[0]
        if not isinstance(batch_ref, str):
            return ()
        matched = _ACTIONABLE_BATCH_REF.fullmatch(batch_ref)
        if matched is None:
            return ()
        batch_id = int(matched.group(1))
        if batch_id in batch_ids:
            return ()
        batch_ids.append(batch_id)
    return tuple(batch_ids)


def _safe_checked_at_shanghai(value: datetime | str) -> str:
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and _SAFE_TIMESTAMP.fullmatch(value):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        return "时间无法确认"
    return parsed.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M（北京时间）")


def _safe_checked_at(value: datetime | str) -> str:
    rendered = value.isoformat() if isinstance(value, datetime) else value
    if isinstance(rendered, str) and _SAFE_TIMESTAMP.fullmatch(rendered):
        return rendered
    return "unknown"


def _safe_detail_value(key: str, value: object) -> str | None:
    if key == "adapter_failures":
        failures = _safe_adapter_failures(value)
        return ",".join(failures) if failures else None
    if key == "service_state":
        return _safe_service_state(value) or "invalid"
    if key in {"head", "expected_head"}:
        head = _safe_git_head(value)
        return head[:12] if head is not None else "invalid"
    if key in {"management_execution_mode", "expected_management_execution_mode"}:
        return _safe_management_mode(value) or "invalid"
    if key in {"entry_preamble_mode", "expected_entry_preamble_mode"}:
        return _safe_entry_preamble_mode(value) or "invalid"
    if type(value) is bool:
        return "true" if value else "false"
    if _safe_count(value) is not None:
        return str(value)
    return "invalid"


def _safe_event_value(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_EVENT_VALUE.fullmatch(value):
        return None
    return value


def _safe_git_head(value: object) -> str | None:
    if not isinstance(value, str) or not _GIT_HEAD.fullmatch(value):
        return None
    return value


def _safe_management_mode(value: object) -> str | None:
    if not isinstance(value, str) or value not in _MANAGEMENT_MODES:
        return None
    return value


def _safe_entry_preamble_mode(value: object) -> str | None:
    if value not in {"disabled", "shadow", "live"}:
        return None
    return value


def _safe_service_state(value: object) -> str | None:
    if not isinstance(value, str) or value not in _SERVICE_STATES:
        return None
    return value


def _safe_count(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_COUNT:
        return None
    return value


def _safe_adapter_failures(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    failures: set[str] = set()
    for item in value:
        failures.add(item if isinstance(item, str) and item in _ADAPTER_NAMES else "unknown")
    return tuple(sorted(failures))


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _monitor_state_from_payload(payload: object) -> MonitorState:
    if not isinstance(payload, dict) or set(payload) not in {
        _LEGACY_STATE_FIELDS,
        _STATE_FIELDS,
    }:
        raise ValueError("invalid monitor state schema")
    state = MonitorState(
        last_window_at=payload["last_window_at"],
        last_full_audit_date=payload["last_full_audit_date"],
        anomaly_fingerprint=payload["anomaly_fingerprint"],
        last_notification_at=payload["last_notification_at"],
        active_reason_codes=tuple(payload.get("active_reason_codes", ())),
    )
    _monitor_state_payload(state)
    return state


def _monitor_state_payload(state: MonitorState) -> dict[str, object]:
    if not isinstance(state, MonitorState):
        raise TypeError("state must be MonitorState")
    if state.last_window_at is not None:
        _validate_canonical_aware_datetime(state.last_window_at)
    if state.last_full_audit_date is not None:
        try:
            parsed_date = date.fromisoformat(state.last_full_audit_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid full-audit date") from exc
        if parsed_date.isoformat() != state.last_full_audit_date:
            raise ValueError("non-canonical full-audit date")
    if state.anomaly_fingerprint is not None and (
        not isinstance(state.anomaly_fingerprint, str)
        or not _SHA256_FINGERPRINT.fullmatch(state.anomaly_fingerprint)
    ):
        raise ValueError("invalid anomaly fingerprint")
    if state.last_notification_at is not None:
        _validate_canonical_aware_datetime(state.last_notification_at)
    if (
        not isinstance(state.active_reason_codes, tuple)
        or len(state.active_reason_codes) > len(_FIXED_REASON_CODES)
        or any(
            not isinstance(reason, str) or reason not in _FIXED_REASON_CODES
            for reason in state.active_reason_codes
        )
        or len(set(state.active_reason_codes)) != len(state.active_reason_codes)
        or tuple(sorted(state.active_reason_codes)) != state.active_reason_codes
    ):
        raise ValueError("invalid active monitor reasons")
    return {
        "last_window_at": state.last_window_at,
        "last_full_audit_date": state.last_full_audit_date,
        "anomaly_fingerprint": state.anomaly_fingerprint,
        "last_notification_at": state.last_notification_at,
        "active_reason_codes": list(state.active_reason_codes),
    }


def _canonical_aware_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.isoformat()


def _validate_canonical_aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError("timestamp must be canonical and timezone-aware")
    return parsed
