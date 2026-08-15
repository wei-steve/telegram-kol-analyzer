"""Typed, bounded facts for the dormant production-monitor sentinel.

Local database facts are deliberately read through one SQLite URI-mode
read-only connection and one explicit query-only transaction.  The module
does not import the legacy composite/UI cache readers and owns no write path.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from telegram_kol_research.config import (
    MESSAGE_OPERATION_SUPERVISOR_POLICY_STATUSES,
)
from telegram_kol_research.production_monitor_policy import (
    IMMEDIATE,
    REASON_POLICIES,
    CandidateObservation,
)
from telegram_kol_research.production_monitor_snapshot import (
    SnapshotGeneration,
    SnapshotManifest,
)
from telegram_kol_research.production_monitor_snapshot import (
    ProductionMonitorSnapshotStore,
)
from telegram_kol_research.production_monitor_state import (
    MONITOR_STATE_MAX_CANDIDATES,
)
from telegram_kol_research.runtime_incident_snapshot import (
    INSTRUCTION_EXECUTION_CONTRADICTION_CODES,
)


FACT_STATUS_COMPLETE = "COMPLETE"
FACT_STATUS_UNKNOWN = "UNKNOWN"
FACT_STATUSES = frozenset({FACT_STATUS_COMPLETE, FACT_STATUS_UNKNOWN})

LOCAL_FACT_UNKNOWN_CODES = frozenset(
    {
        "database_unavailable",
        "database_schema_missing",
        "database_schema_invalid",
        "database_row_limit_exceeded",
        "database_timestamp_invalid",
        "database_status_invalid",
    }
)

MONITOR_READINESS_FIELDS = frozenset(
    {
        "service_generation",
        "deepcoin_reconcile_first_success_at",
        "deepcoin_reconcile_last_success_at",
        "management_worker_last_success_at",
        "message_supervisor_last_success_at",
        "message_supervisor_policy_status",
    }
)

DEFAULT_LOCAL_FACT_ROW_LIMIT = 1_000
DEFAULT_READINESS_MAX_AGE = timedelta(minutes=5)
DEFAULT_TERMINAL_FACT_RETENTION = timedelta(hours=1)
DEFAULT_SNAPSHOT_MAX_AGE = timedelta(minutes=5)
MAX_SNAPSHOT_CAPTURE_DURATION = timedelta(seconds=45)
DEFAULT_JOURNAL_CAPTURE_MAX_DURATION = timedelta(minutes=1)
MAX_LOCAL_CANDIDATES = 64

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "service_generation",
        "capture_started_at",
        "capture_completed_at",
        "complete",
        "markers",
        "resolved_markers",
    }
)
_CLOSED_MODES = frozenset({"disabled", "shadow", "live"})
_DATABASE_OWNED_REASON_CODES = frozenset(
    {
        "audit_abnormal",
        "event_unknown_status",
        "event_recovery_status",
        "stalled_composite_component",
        "stale_entry_preamble_unresolved",
    }
)
_COVERAGE_COUNT_FIELDS = frozenset(
    {
        "executable_messages_total", "contracts_created_total",
        "contracts_verified_total", "contracts_violated_total",
        "executable_without_contract_total", "violations_without_stage1_total",
        "stage1_pending", "stage1_delivered", "stage1_failed", "agent_pending",
        "agent_diagnosed", "agent_failed", "agent_timed_out",
        "incidents_without_terminal_stage2_total", "handoffs_persisted_total",
        "stage2_pending", "stage2_delivered", "stage2_failed",
        "oldest_nonterminal_age_seconds",
        "instruction_execution_contradictions_total",
    }
)
_COVERAGE_FIELDS = _COVERAGE_COUNT_FIELDS | {
    "schema_version", "coverage_enabled", "scan_truncated",
    "supervisor_policy_status", "supervisor_last_success_at",
    "instruction_execution_scan_truncated", "instruction_execution_facts",
}
_REQUIRED_COLUMNS = {
    "strategy_management_batches": frozenset(
        {
            "id",
            "status",
            "reason_code",
            "last_progress_at",
            "execution_deadline_at",
            "updated_at",
        }
    ),
    "strategy_management_components": frozenset(
        {
            "id",
            "management_batch_id",
            "status",
            "last_progress_at",
            "execution_deadline_at",
            "updated_at",
            "strategy_management_leg_id",
            "component_kind",
            "desired_json",
        }
    ),
    "strategy_management_legs": frozenset(
        {"id", "management_batch_id", "pos_id"}
    ),
    "instruction_execution_contracts": frozenset(
        {"id", "state", "last_progress_at", "deadline_at", "updated_at"}
    ),
    "deepcoin_execution_operations": frozenset(
        {"id", "state", "updated_at", "deadline_at"}
    ),
    "entry_preambles": frozenset({"id", "status", "created_at", "updated_at"}),
}

_MANAGEMENT_BATCH_STATUSES = frozenset(
    {
        "ready",
        "protection_ready",
        "blocked",
        "executing",
        "reserved",
        "submitted",
        "submit_unknown",
        "reconciling",
        "recovery_required",
        "partial_failed",
        "failed",
        "succeeded",
        "resolved",
    }
)
_ACTIVE_MANAGEMENT_BATCH_STATUSES = _MANAGEMENT_BATCH_STATUSES - {
    "blocked",
    "partial_failed",
    "failed",
    "succeeded",
    "resolved",
}
_COMPONENT_STATUSES = frozenset(
    {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "definitely_rejected",
        "blocked",
        "recovery_required",
        "confirmed",
        "operator_required",
        "safely_skipped",
    }
)
_ACTIVE_COMPONENT_STATUSES = frozenset(
    {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "recovery_required",
    }
)
_INSTRUCTION_CONTRACT_STATES = frozenset(
    {"pending", "deferred", "submitting", "submit_unknown", "verified", "failed", "expired"}
)
_DEEPCOIN_OPERATION_STATES = frozenset(
    {
        "planned",
        "entry_prepared",
        "entry_submitting",
        "entry_pending_readback",
        "entry_unknown",
        "entry_rejected",
        "entry_confirmed",
        "protection_prepared",
        "protection_pending_readback",
        "protection_unknown",
        "protected",
        "next_leg_preflight",
        "pre_submit_deferred",
        "completed",
        "recovery_required",
        "submission_failed_no_exposure",
    }
)
_ENTRY_PREAMBLE_STATUSES = frozenset(
    {"pending", "consumed", "expired", "invalidated"}
)


@dataclass(frozen=True, slots=True)
class LocalMonitorFactResult:
    status: str
    unknown_code: str | None
    candidates: tuple[CandidateObservation, ...]


@dataclass(frozen=True, slots=True)
class MonitorReadinessEvidence:
    service_generation: str
    deepcoin_reconcile_first_success_at: datetime | None
    deepcoin_reconcile_last_success_at: datetime | None
    management_worker_last_success_at: datetime | None
    message_supervisor_last_success_at: datetime | None
    message_supervisor_policy_status: str


class _TypedUnknown(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def read_local_monitor_facts(
    database_path: str | Path,
    *,
    snapshot_generations: Sequence[SnapshotGeneration],
    now: datetime,
    row_limit: int = DEFAULT_LOCAL_FACT_ROW_LIMIT,
    connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    between_queries_hook: Callable[[], None] | None = None,
) -> LocalMonitorFactResult:
    """Read every local fact from one coherent SQLite snapshot.

    ``between_queries_hook`` exists for WAL concurrency verification.  It is
    invoked after the batch query has established the read snapshot and before
    dependent component rows are read.
    """

    checked_at = _aware_utc(now, field="now")
    if (
        type(row_limit) is not int
        or not 1 <= row_limit <= DEFAULT_LOCAL_FACT_ROW_LIMIT
        or not isinstance(snapshot_generations, Sequence)
        or isinstance(snapshot_generations, (str, bytes, bytearray))
        or len(snapshot_generations) > 3
    ):
        raise ValueError("local monitor fact bounds are invalid")
    try:
        latest_snapshot = _latest_complete_snapshot(snapshot_generations)
        retained_after_raw = (
            checked_at - DEFAULT_TERMINAL_FACT_RETENTION
        ).isoformat()
        uri = f"file:{Path(database_path)}?mode=ro"
        with connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {
                str(row[0])
                for row in _bounded_query(
                    connection,
                    "SELECT name FROM sqlite_master WHERE type='table'",
                    (),
                    limit=256,
                )
            }
            if not set(_REQUIRED_COLUMNS).issubset(tables):
                raise _TypedUnknown("database_schema_missing")
            for table, expected_columns in _REQUIRED_COLUMNS.items():
                columns = {
                    str(row[0])
                    for row in _bounded_query(
                        connection,
                        "SELECT name FROM pragma_table_info(?)",
                        (table,),
                        limit=128,
                    )
                }
                if not expected_columns.issubset(columns):
                    raise _TypedUnknown("database_schema_invalid")

            batch_rows = _bounded_query(
                connection,
                "SELECT id, status, reason_code, last_progress_at, "
                "execution_deadline_at, updated_at FROM strategy_management_batches "
                "WHERE status NOT IN ('blocked','partial_failed','failed','succeeded','resolved') "
                "OR datetime(updated_at) >= datetime(?) OR datetime(updated_at) IS NULL "
                "ORDER BY id",
                (retained_after_raw,),
                limit=row_limit,
            )
            if between_queries_hook is not None:
                between_queries_hook()
            component_rows = _bounded_query(
                connection,
                "SELECT c.id, c.management_batch_id, c.status, c.last_progress_at, "
                "c.execution_deadline_at, c.updated_at, c.strategy_management_leg_id, "
                "c.component_kind, c.desired_json, b.status, l.pos_id "
                "FROM strategy_management_components AS c "
                "LEFT JOIN strategy_management_batches AS b ON b.id=c.management_batch_id "
                "LEFT JOIN strategy_management_legs AS l "
                "ON l.id=c.strategy_management_leg_id "
                "AND l.management_batch_id=c.management_batch_id "
                "WHERE c.status NOT IN ('definitely_rejected','blocked','confirmed',"
                "'operator_required','safely_skipped') "
                "OR datetime(c.updated_at) >= datetime(?) "
                "OR datetime(c.updated_at) IS NULL ORDER BY c.id",
                (retained_after_raw,),
                limit=row_limit,
            )
            contract_rows = _bounded_query(
                connection,
                "SELECT id, state, last_progress_at, deadline_at, updated_at "
                "FROM instruction_execution_contracts "
                "WHERE state NOT IN ('verified','failed','expired') "
                "OR datetime(updated_at) >= datetime(?) OR datetime(updated_at) IS NULL "
                "ORDER BY id",
                (retained_after_raw,),
                limit=row_limit,
            )
            operation_rows = _bounded_query(
                connection,
                "SELECT id, state, updated_at, deadline_at "
                "FROM deepcoin_execution_operations "
                "WHERE state NOT IN ('entry_rejected','protected','completed',"
                "'submission_failed_no_exposure') "
                "OR datetime(updated_at) >= datetime(?) OR datetime(updated_at) IS NULL "
                "ORDER BY id",
                (retained_after_raw,),
                limit=row_limit,
            )
            preamble_rows = _bounded_query(
                connection,
                "SELECT id, status, created_at, updated_at FROM entry_preambles "
                "WHERE status = 'pending' OR datetime(updated_at) >= datetime(?) "
                "OR datetime(updated_at) IS NULL "
                "ORDER BY id",
                (retained_after_raw,),
                limit=row_limit,
            )
    except _TypedUnknown as exc:
        return _unknown_local(exc.code)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _unknown_local("database_unavailable")

    readiness_generation: str | None = None
    try:
        candidates = _build_local_candidates(
            batch_rows=batch_rows,
            component_rows=component_rows,
            contract_rows=contract_rows,
            operation_rows=operation_rows,
            preamble_rows=preamble_rows,
            latest_snapshot=latest_snapshot,
            observed_at=checked_at,
        )
    except _TypedUnknown as exc:
        return _unknown_local(exc.code)
    if len(candidates) > MAX_LOCAL_CANDIDATES:
        return _unknown_local("database_row_limit_exceeded")
    return LocalMonitorFactResult(
        status=FACT_STATUS_COMPLETE,
        unknown_code=None,
        candidates=candidates,
    )


def parse_monitor_readiness_projection(
    payload: Mapping[str, Any],
) -> MonitorReadinessEvidence:
    """Strictly parse the secret-free loopback readiness projection."""

    if not isinstance(payload, Mapping) or set(payload) != MONITOR_READINESS_FIELDS:
        raise ValueError("readiness projection fields are invalid")
    generation = payload["service_generation"]
    policy_status = payload["message_supervisor_policy_status"]
    if not isinstance(generation, str) or _SHA256.fullmatch(generation) is None:
        raise ValueError("service generation is invalid")
    if (
        not isinstance(policy_status, str)
        or policy_status not in MESSAGE_OPERATION_SUPERVISOR_POLICY_STATUSES
    ):
        raise ValueError("message supervisor policy status is invalid")
    return MonitorReadinessEvidence(
        service_generation=generation,
        deepcoin_reconcile_first_success_at=_parse_optional_timestamp(
            payload["deepcoin_reconcile_first_success_at"]
        ),
        deepcoin_reconcile_last_success_at=_parse_optional_timestamp(
            payload["deepcoin_reconcile_last_success_at"]
        ),
        management_worker_last_success_at=_parse_optional_timestamp(
            payload["management_worker_last_success_at"]
        ),
        message_supervisor_last_success_at=_parse_optional_timestamp(
            payload["message_supervisor_last_success_at"]
        ),
        message_supervisor_policy_status=policy_status,
    )


def build_readiness_candidates(
    evidence: MonitorReadinessEvidence,
    *,
    now: datetime,
    heartbeat_max_age: timedelta = DEFAULT_READINESS_MAX_AGE,
) -> tuple[CandidateObservation, ...]:
    """Project readiness without treating elapsed startup time as success."""

    if not isinstance(evidence, MonitorReadinessEvidence):
        raise TypeError("readiness evidence is invalid")
    checked_at = _aware_utc(now, field="now")
    if not isinstance(heartbeat_max_age, timedelta) or heartbeat_max_age <= timedelta(0):
        raise ValueError("heartbeat maximum age is invalid")
    timestamps = (
        evidence.deepcoin_reconcile_first_success_at,
        evidence.deepcoin_reconcile_last_success_at,
        evidence.management_worker_last_success_at,
        evidence.message_supervisor_last_success_at,
    )
    active_reason: str | None = None
    first, last, management, supervisor = timestamps
    if any(
        value is not None
        and _aware_utc(value, field="heartbeat") > checked_at
        for value in timestamps
    ):
        active_reason = "readiness_unavailable"
    elif first is None or last is None or management is None or supervisor is None:
        active_reason = "service_starting"
    elif _aware_utc(first, field="heartbeat") > _aware_utc(last, field="heartbeat"):
        active_reason = "readiness_unavailable"
    elif evidence.message_supervisor_policy_status != "valid":
        active_reason = "message_operation_supervisor_policy_invalid"
    elif checked_at - _aware_utc(supervisor, field="heartbeat") > heartbeat_max_age:
        active_reason = "message_operation_supervisor_stale"
    elif any(
        checked_at - _aware_utc(value, field="heartbeat") > heartbeat_max_age
        for value in (last, management)
    ):
        active_reason = "readiness_unavailable"
    reason_codes = (
        "service_starting",
        "readiness_unavailable",
        "message_operation_supervisor_policy_invalid",
        "message_operation_supervisor_stale",
    )
    return tuple(
        _readiness_candidate(
            evidence,
            reason_code,
            checked_at,
            anomaly_present=reason_code == active_reason,
        )
        for reason_code in reason_codes
    )


def collect_production_monitor_observation(
    *,
    database_path: str | Path,
    snapshot_path: str | Path,
    checkout_path: str | Path,
    settings_url: str,
    coverage_path: str | Path,
    journal_path: str | Path,
    expected_head: str,
    expected_auto_trade_enabled: bool,
    expected_max_concurrent_positions: int,
    expected_management_execution_mode: str,
    expected_entry_preamble_mode: str,
    readiness_url: str,
    incident_loopback_url: str,
    monitor_capture_token: str | None,
    now: datetime | None = None,
    http_get: Callable[..., Any] = httpx.get,
) -> Any:
    """Collect the Task-6 adapters without submitting or notifying.

    The local import keeps the pure fact types independent from sentinel state
    persistence while making this object directly callable by the runner.
    """

    from telegram_kol_research.production_monitor_sentinel import (
        SentinelObservation,
    )

    checked_at = _aware_utc(now or datetime.now(UTC), field="now")
    for endpoint in (settings_url, readiness_url, incident_loopback_url):
        _require_loopback_url(endpoint)
    if not isinstance(expected_head, str) or _GIT_SHA.fullmatch(expected_head) is None:
        raise ValueError("expected head is invalid")
    if monitor_capture_token is not None and (
        not isinstance(monitor_capture_token, str)
        or not 32 <= len(monitor_capture_token) <= 128
    ):
        raise ValueError("monitor capture token is invalid")

    _validate_settings_expectations(
        expected_auto_trade_enabled=expected_auto_trade_enabled,
        expected_max_concurrent_positions=expected_max_concurrent_positions,
        expected_management_execution_mode=expected_management_execution_mode,
        expected_entry_preamble_mode=expected_entry_preamble_mode,
    )
    failures: set[str] = set()
    candidates: list[CandidateObservation] = []

    generations: tuple[SnapshotGeneration, ...] = ()
    try:
        manifest = ProductionMonitorSnapshotStore(snapshot_path).load()
        generations = select_usable_snapshot_generations(manifest, now=checked_at)
        if not generations:
            failures.add("exchange_snapshot")
    except Exception:
        failures.add("exchange_snapshot")

    local = read_local_monitor_facts(
        database_path,
        snapshot_generations=generations,
        now=checked_at,
    )
    if local.status == FACT_STATUS_COMPLETE:
        candidates.extend(local.candidates)
    else:
        failures.update({"events", "audit", "composite", "entry_preamble"})

    try:
        readiness_response = http_get(
            readiness_url,
            headers={
                "x-monitor-capture-token": monitor_capture_token or "",
                "cache-control": "no-cache",
            },
            timeout=5.0,
        )
        readiness_payload = _bounded_http_json(readiness_response, limit=4096)
        readiness = parse_monitor_readiness_projection(readiness_payload)
        readiness_generation = readiness.service_generation
        candidates.extend(build_readiness_candidates(readiness, now=checked_at))
    except Exception:
        failures.update({"service", "readiness"})

    try:
        settings_response = http_get(settings_url, timeout=5.0)
        settings_payload = _bounded_http_json(settings_response, limit=65_536)
        candidates.extend(
            build_settings_candidates(
                settings_payload,
                observed_at=checked_at,
                expected_auto_trade_enabled=expected_auto_trade_enabled,
                expected_max_concurrent_positions=expected_max_concurrent_positions,
                expected_management_execution_mode=expected_management_execution_mode,
                expected_entry_preamble_mode=expected_entry_preamble_mode,
            )
        )
    except Exception:
        failures.add("settings")

    try:
        head = subprocess.run(
            ["git", "-C", str(Path(checkout_path)), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        ).stdout.strip()
        if _GIT_SHA.fullmatch(head) is None:
            raise ValueError("checkout head is invalid")
        candidates.append(
            _local_candidate(
                reason_code="reviewed_sha_drift",
                identity="reviewed_checkout",
                observed_at=checked_at,
                anomaly_present=head != expected_head,
                last_progress_at=None,
                execution_deadline_at=None,
                durable_terminal_fact=head == expected_head,
            )
        )
    except Exception:
        failures.add("head")

    try:
        coverage_payload = _bounded_json_file(Path(coverage_path), limit=65_536)
        candidates.extend(build_coverage_candidates(coverage_payload, now=checked_at))
    except Exception:
        failures.add("coverage")

    try:
        candidates.extend(
            read_journal_candidates(
                journal_path,
                observed_at=checked_at,
                expected_service_generation=readiness_generation,
            )
        )
    except Exception:
        failures.add("journal")

    if len(candidates) > MONITOR_STATE_MAX_CANDIDATES:
        return SentinelObservation(
            checked_at=checked_at,
            candidates=(),
            adapter_failures=tuple(sorted(failures | {"events"})),
        )

    return SentinelObservation(
        checked_at=checked_at,
        candidates=tuple(candidates),
        adapter_failures=tuple(sorted(failures)),
    )


def select_usable_snapshot_generations(
    manifest: SnapshotManifest,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_SNAPSHOT_MAX_AGE,
) -> tuple[SnapshotGeneration, ...]:
    """Reject stale success or any newer failed/incomplete attempt."""

    checked_at = _aware_utc(now, field="now")
    if not isinstance(manifest, SnapshotManifest):
        raise TypeError("snapshot manifest is invalid")
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise ValueError("snapshot maximum age is invalid")
    latest = manifest.latest_attempt
    success = manifest.last_success
    if latest is None or success is None or latest.outcome != "SUCCESS":
        return ()
    if latest.generation != success.generation or latest != success:
        return ()
    completed = _aware_utc(success.request_completed_at, field="snapshot completion")
    started = _aware_utc(success.request_started_at, field="snapshot start")
    if (
        started >= completed
        or completed - started > MAX_SNAPSHOT_CAPTURE_DURATION
        or completed > checked_at
        or checked_at - completed > max_age
    ):
        return ()
    return _latest_snapshot_tail(manifest.generations, latest_generation=success.generation)


def build_settings_candidates(
    payload: object,
    *,
    observed_at: datetime,
    expected_auto_trade_enabled: bool,
    expected_max_concurrent_positions: int,
    expected_management_execution_mode: str,
    expected_entry_preamble_mode: str,
) -> tuple[CandidateObservation, ...]:
    _validate_settings_expectations(
        expected_auto_trade_enabled=expected_auto_trade_enabled,
        expected_max_concurrent_positions=expected_max_concurrent_positions,
        expected_management_execution_mode=expected_management_execution_mode,
        expected_entry_preamble_mode=expected_entry_preamble_mode,
    )
    if not isinstance(payload, Mapping):
        raise ValueError("settings projection is invalid")
    actual = {
        "auto_trade_enabled": payload.get("auto_trade_enabled"),
        "max_concurrent_positions": payload.get("max_concurrent_positions"),
        "management_execution_mode": payload.get("management_execution_mode"),
        "entry_preamble_mode": payload.get("entry_preamble_mode"),
    }
    if (
        type(actual["auto_trade_enabled"]) is not bool
        or type(actual["max_concurrent_positions"]) is not int
        or not 1 <= actual["max_concurrent_positions"] <= 1_000
        or actual["management_execution_mode"] not in _CLOSED_MODES
        or actual["entry_preamble_mode"] not in _CLOSED_MODES
    ):
        raise ValueError("settings projection is invalid")
    expectations = {
        "auto_trade_enabled": expected_auto_trade_enabled,
        "max_concurrent_positions": expected_max_concurrent_positions,
        "management_execution_mode": expected_management_execution_mode,
        "entry_preamble_mode": expected_entry_preamble_mode,
    }
    reasons = {
        "auto_trade_enabled": "auto_trade_enabled_drift",
        "max_concurrent_positions": "max_concurrent_positions_drift",
        "management_execution_mode": "management_execution_mode_drift",
        "entry_preamble_mode": "entry_preamble_mode_drift",
    }
    return tuple(
        _local_candidate(
            reason_code=reasons[field],
            identity=f"settings:{field}",
            observed_at=observed_at,
            anomaly_present=actual[field] != expected,
            last_progress_at=None,
            execution_deadline_at=None,
            durable_terminal_fact=actual[field] == expected,
        )
        for field, expected in expectations.items()
    )


def build_coverage_candidates(
    payload: object,
    *,
    now: datetime,
) -> tuple[CandidateObservation, ...]:
    checked_at = _aware_utc(now, field="now")
    if not isinstance(payload, Mapping) or set(payload) != _COVERAGE_FIELDS:
        raise ValueError("coverage projection fields are invalid")
    if type(payload.get("schema_version")) is not int or payload.get(
        "schema_version"
    ) != 1:
        raise ValueError("coverage schema is invalid")
    enabled = payload.get("coverage_enabled")
    scan_truncated = payload.get("scan_truncated")
    execution_truncated = payload.get("instruction_execution_scan_truncated")
    policy = payload.get("supervisor_policy_status")
    facts = payload.get("instruction_execution_facts")
    if (
        type(enabled) is not bool
        or type(scan_truncated) is not bool
        or type(execution_truncated) is not bool
        or policy not in MESSAGE_OPERATION_SUPERVISOR_POLICY_STATUSES
        or not isinstance(facts, list)
        or len(facts) > 20
    ):
        raise ValueError("coverage projection is invalid")
    counts: dict[str, int] = {}
    for field in _COVERAGE_COUNT_FIELDS:
        value = payload.get(field)
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError("coverage count is invalid")
        counts[field] = value
    if counts["instruction_execution_contradictions_total"] != len(facts):
        raise ValueError("coverage contradiction count is invalid")
    fact_fields = {
        "reason_code",
        "contract_id",
        "message_instruction_item_id",
        "raw_message_id",
        "future_contract",
        "exact_historical",
    }
    for fact in facts:
        if not isinstance(fact, Mapping) or set(fact) != fact_fields:
            raise ValueError("coverage contradiction fact is invalid")
        identifiers = (
            fact.get("contract_id"),
            fact.get("message_instruction_item_id"),
            fact.get("raw_message_id"),
        )
        if (
            fact.get("reason_code") not in INSTRUCTION_EXECUTION_CONTRADICTION_CODES
            or any(
                type(identifier) is not int or identifier <= 0
                for identifier in identifiers
            )
            or type(fact.get("future_contract")) is not bool
            or type(fact.get("exact_historical")) is not bool
            or not (fact["future_contract"] or fact["exact_historical"])
        ):
            raise ValueError("coverage contradiction fact is invalid")
    if (
        counts["contracts_created_total"]
        + counts["executable_without_contract_total"]
        != counts["executable_messages_total"]
        or counts["contracts_verified_total"]
        + counts["contracts_violated_total"]
        > counts["contracts_created_total"]
        or counts["stage1_pending"]
        + counts["stage1_delivered"]
        + counts["stage1_failed"]
        + counts["violations_without_stage1_total"]
        != counts["contracts_violated_total"]
        or counts["stage2_pending"]
        + counts["stage2_delivered"]
        + counts["stage2_failed"]
        > counts["handoffs_persisted_total"]
    ):
        raise ValueError("coverage count invariants are invalid")
    heartbeat = _parse_optional_timestamp(payload.get("supervisor_last_success_at"))
    incomplete = (
        not enabled
        or policy != "valid"
        or scan_truncated
        or execution_truncated
        or heartbeat is None
        or heartbeat > checked_at + timedelta(minutes=1)
        or checked_at - heartbeat > DEFAULT_READINESS_MAX_AGE
    )
    flags = {
        "message_operation_coverage_incomplete": incomplete,
        "message_operation_supervisor_policy_invalid": policy != "valid",
        "executable_message_missing_contract": bool(counts["executable_without_contract_total"]),
        "contract_violation_missing_stage1": bool(counts["violations_without_stage1_total"]),
        "message_operation_incident_missing_terminal": bool(counts["incidents_without_terminal_stage2_total"]),
        "instruction_execution_contradiction": bool(facts),
    }
    return tuple(
        _local_candidate(
            reason_code=reason,
            identity=f"coverage:{reason}",
            observed_at=checked_at,
            anomaly_present=present,
            last_progress_at=None,
            execution_deadline_at=None,
            durable_terminal_fact=not present,
        )
        for reason, present in flags.items()
    )


def read_journal_candidates(
    journal_path: str | Path,
    *,
    observed_at: datetime,
    expected_service_generation: str | None,
    byte_limit: int = 65_536,
    max_age: timedelta = DEFAULT_READINESS_MAX_AGE,
    max_capture_duration: timedelta = DEFAULT_JOURNAL_CAPTURE_MAX_DURATION,
) -> tuple[CandidateObservation, ...]:
    checked_at = _aware_utc(observed_at, field="observed_at")
    if (
        not isinstance(expected_service_generation, str)
        or _SHA256.fullmatch(expected_service_generation) is None
        or not isinstance(max_age, timedelta)
        or max_age <= timedelta(0)
        or not isinstance(max_capture_duration, timedelta)
        or max_capture_duration <= timedelta(0)
    ):
        raise ValueError("journal evidence authority is invalid")
    path = Path(journal_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > byte_limit:
        raise ValueError("journal evidence is invalid")
    raw = path.read_bytes()
    if len(raw) > byte_limit:
        raise ValueError("journal evidence is invalid")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, Mapping) or set(payload) != _JOURNAL_FIELDS:
        raise ValueError("journal evidence is invalid")
    markers = payload.get("markers")
    resolved_markers = payload.get("resolved_markers")
    generation = payload.get("service_generation")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("complete") is not True
        or generation != expected_service_generation
        or not isinstance(markers, list)
        or not isinstance(resolved_markers, list)
        or len(markers) > 100
        or len(resolved_markers) > 100
        or len(set(markers)) != len(markers)
        or len(set(resolved_markers)) != len(resolved_markers)
        or not set(markers).isdisjoint(resolved_markers)
        or any(
            not isinstance(reason, str)
            or reason not in REASON_POLICIES
            or reason in _DATABASE_OWNED_REASON_CODES
            or REASON_POLICIES[reason].classification != IMMEDIATE
            for reason in (*markers, *resolved_markers)
        )
    ):
        raise ValueError("journal evidence is invalid")
    started = _parse_required_timestamp(
        payload.get("capture_started_at"), field="journal capture start"
    )
    completed = _parse_required_timestamp(
        payload.get("capture_completed_at"), field="journal capture completion"
    )
    if (
        started > completed
        or completed - started > max_capture_duration
        or completed > checked_at
        or checked_at - completed > max_age
    ):
        raise ValueError("journal evidence window is invalid")
    return tuple(
        _local_candidate(
            reason_code=reason,
            identity=f"journal:{reason}",
            observed_at=checked_at,
            anomaly_present=reason in markers,
            last_progress_at=None,
            execution_deadline_at=None,
            durable_terminal_fact=reason in resolved_markers,
        )
        for reason in sorted((*markers, *resolved_markers))
    )


def _bounded_query(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
    *,
    limit: int,
) -> list[tuple[Any, ...]]:
    rows = connection.execute(f"{sql} LIMIT ?", (*parameters, limit + 1)).fetchall()
    if len(rows) > limit:
        raise _TypedUnknown("database_row_limit_exceeded")
    return rows


def _validate_settings_expectations(
    *,
    expected_auto_trade_enabled: object,
    expected_max_concurrent_positions: object,
    expected_management_execution_mode: object,
    expected_entry_preamble_mode: object,
) -> None:
    if (
        type(expected_auto_trade_enabled) is not bool
        or type(expected_max_concurrent_positions) is not int
        or not 1 <= expected_max_concurrent_positions <= 1_000
        or expected_management_execution_mode not in _CLOSED_MODES
        or expected_entry_preamble_mode not in _CLOSED_MODES
    ):
        raise ValueError("settings expectations are invalid")


def _latest_snapshot_tail(
    generations: Sequence[SnapshotGeneration],
    *,
    latest_generation: int,
) -> tuple[SnapshotGeneration, ...]:
    complete = _latest_complete_snapshot(generations)
    if complete is None or complete.generation != latest_generation:
        return ()
    return tuple(generations[-2:])


def _require_loopback_url(value: object) -> None:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("monitor endpoint is invalid")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("monitor endpoint must be loopback HTTP")


def _bounded_http_json(response: object, *, limit: int) -> object:
    status_code = getattr(response, "status_code", None)
    content = getattr(response, "content", None)
    if type(status_code) is not int or status_code != 200:
        raise ValueError("monitor endpoint unavailable")
    if not isinstance(content, bytes) or len(content) > limit:
        raise ValueError("monitor endpoint response invalid")
    return json.loads(content.decode("utf-8"))


def _bounded_json_file(path: Path, *, limit: int) -> object:
    if path.is_symlink():
        raise ValueError("monitor evidence path is unsafe")
    metadata = path.stat()
    if not path.is_file() or metadata.st_size > limit:
        raise ValueError("monitor evidence file is invalid")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("monitor evidence file is invalid")
    return json.loads(raw.decode("utf-8"))


def _latest_complete_snapshot(
    generations: Sequence[SnapshotGeneration],
) -> SnapshotGeneration | None:
    previous_generation: int | None = None
    latest: SnapshotGeneration | None = None
    for generation in generations:
        if not isinstance(generation, SnapshotGeneration):
            raise ValueError("snapshot generation is invalid")
        if generation.outcome != "SUCCESS":
            raise ValueError("snapshot generation is incomplete")
        if previous_generation is not None and generation.generation <= previous_generation:
            raise ValueError("snapshot generations are out of order")
        previous_generation = generation.generation
        latest = generation
    return latest


def _build_local_candidates(
    *,
    batch_rows: Sequence[tuple[Any, ...]],
    component_rows: Sequence[tuple[Any, ...]],
    contract_rows: Sequence[tuple[Any, ...]],
    operation_rows: Sequence[tuple[Any, ...]],
    preamble_rows: Sequence[tuple[Any, ...]],
    latest_snapshot: SnapshotGeneration | None,
    observed_at: datetime,
) -> tuple[CandidateObservation, ...]:
    candidates: list[CandidateObservation] = []
    for raw_id, raw_status, _reason, raw_progress, raw_deadline, raw_updated in batch_rows:
        row_id = _positive_int(raw_id)
        status = _closed_status(raw_status, _MANAGEMENT_BATCH_STATUSES)
        progress = _parse_database_timestamp(raw_progress)
        deadline = _parse_database_timestamp(raw_deadline)
        _parse_database_timestamp(raw_updated)
        terminal_batch = status in {
            "blocked", "partial_failed", "failed", "succeeded", "resolved"
        }
        candidates.append(
            _local_candidate(
                reason_code="audit_abnormal",
                identity=f"management_batch:{row_id}",
                observed_at=observed_at,
                anomaly_present=status in {"submit_unknown", "recovery_required"},
                last_progress_at=progress,
                execution_deadline_at=deadline,
                durable_terminal_fact=terminal_batch,
            )
        )
        if status not in {"submit_unknown", "recovery_required"}:
            # A post-deadline snapshot proves only when the exchange was read.
            # These batch rows carry no exact exchange-object identity, so an
            # active local status must remain UNKNOWN until reconciliation
            # publishes a durable terminal row; status alone is not a mismatch.
            candidates.append(
                _local_candidate(
                    reason_code="stalled_composite_component",
                    identity=f"management_batch_settling:{row_id}",
                    observed_at=observed_at,
                    anomaly_present=status in _ACTIVE_MANAGEMENT_BATCH_STATUSES,
                    evidence_complete=(
                        terminal_batch
                    ),
                    snapshot=latest_snapshot,
                    last_progress_at=progress,
                    execution_deadline_at=deadline,
                    durable_terminal_fact=terminal_batch,
                )
            )

    for (
        raw_id,
        raw_batch_id,
        raw_status,
        raw_progress,
        raw_deadline,
        raw_updated,
        raw_leg_id,
        raw_kind,
        raw_desired,
        raw_batch_status,
        raw_pos_id,
    ) in component_rows:
        row_id = _positive_int(raw_id)
        batch_id = _positive_int(raw_batch_id)
        status = _closed_status(raw_status, _COMPONENT_STATUSES)
        progress = _parse_database_timestamp(raw_progress)
        deadline = _parse_database_timestamp(raw_deadline)
        _parse_database_timestamp(raw_updated)
        batch_status = _closed_status(raw_batch_status, _MANAGEMENT_BATCH_STATUSES)
        if raw_leg_id is not None:
            _positive_int(raw_leg_id)
        if not isinstance(raw_kind, str) or not 1 <= len(raw_kind) <= 64:
            raise _TypedUnknown("database_schema_invalid")
        anomaly_present = (
            status in _ACTIVE_COMPONENT_STATUSES
            and batch_status in _ACTIVE_MANAGEMENT_BATCH_STATUSES
        )
        terminal_component = status in {
            "confirmed",
            "operator_required",
            "safely_skipped",
            "definitely_rejected",
            "blocked",
        }
        exact_mismatch = (
            _converge_partial_close_mismatch(
                desired_json=raw_desired,
                pos_id=raw_pos_id,
                snapshot=latest_snapshot,
            )
            if anomaly_present and raw_kind == "converge_partial_close"
            else None
        )
        candidates.append(
            _local_candidate(
                reason_code="stalled_composite_component",
                identity=f"management_component:{row_id}",
                observed_at=observed_at,
                anomaly_present=(
                    exact_mismatch if exact_mismatch is not None else anomaly_present
                ),
                # Only the exact identity/Decimal comparison is complete.
                # Unsupported component semantics remain UNKNOWN while an
                # asynchronous reconciliation callback may still be pending.
                evidence_complete=(
                    terminal_component
                    or (anomaly_present and exact_mismatch is not None)
                ),
                snapshot=latest_snapshot,
                last_progress_at=progress,
                execution_deadline_at=deadline,
                durable_terminal_fact=terminal_component,
            )
        )

    for raw_id, raw_state, raw_progress, raw_deadline, raw_updated in contract_rows:
        row_id = _positive_int(raw_id)
        state = _closed_status(raw_state, _INSTRUCTION_CONTRACT_STATES)
        progress = _parse_database_timestamp(raw_progress)
        deadline = _parse_database_timestamp(raw_deadline)
        _parse_database_timestamp(raw_updated)
        terminal_contract = state in {"verified", "failed", "expired"}
        candidates.append(
            _local_candidate(
                reason_code="event_unknown_status",
                identity=f"instruction_contract:{row_id}",
                observed_at=observed_at,
                anomaly_present=state == "submit_unknown",
                last_progress_at=progress,
                execution_deadline_at=deadline,
                durable_terminal_fact=terminal_contract,
            )
        )

    for raw_id, raw_state, raw_progress, raw_deadline in operation_rows:
        row_id = _positive_int(raw_id)
        state = _closed_status(raw_state, _DEEPCOIN_OPERATION_STATES)
        progress = _parse_database_timestamp(raw_progress)
        deadline = _parse_database_timestamp(raw_deadline)
        terminal_operation = state in {
            "entry_rejected", "protected", "completed", "submission_failed_no_exposure"
        }
        for reason_code, anomaly_present in (
            ("event_unknown_status", state in {"entry_unknown", "protection_unknown"}),
            ("event_recovery_status", state == "recovery_required"),
        ):
            candidates.append(
                _local_candidate(
                    reason_code=reason_code,
                    identity=f"deepcoin_operation:{row_id}",
                    observed_at=observed_at,
                    anomaly_present=anomaly_present,
                    last_progress_at=progress,
                    execution_deadline_at=deadline,
                    durable_terminal_fact=terminal_operation,
                )
            )
    for raw_id, raw_status, raw_created, raw_updated in preamble_rows:
        row_id = _positive_int(raw_id)
        status = _closed_status(raw_status, _ENTRY_PREAMBLE_STATUSES)
        _parse_database_timestamp(raw_created)
        progress = _parse_database_timestamp(raw_updated)
        terminal_preamble = status != "pending"
        candidates.append(
            _local_candidate(
                reason_code="stale_entry_preamble_unresolved",
                identity=f"entry_preamble:{row_id}",
                observed_at=observed_at,
                anomaly_present=status == "pending",
                evidence_complete=terminal_preamble,
                last_progress_at=progress,
                execution_deadline_at=None,
                durable_terminal_fact=terminal_preamble,
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.fingerprint))


def _local_candidate(
    *,
    reason_code: str,
    identity: str,
    observed_at: datetime,
    anomaly_present: bool = True,
    evidence_complete: bool = True,
    snapshot: SnapshotGeneration | None = None,
    last_progress_at: datetime | None,
    execution_deadline_at: datetime | None,
    durable_terminal_fact: bool = False,
) -> CandidateObservation:
    return CandidateObservation(
        reason_code=reason_code,
        fingerprint=hashlib.sha256(
            f"production-monitor-fact-v1:{reason_code}:{identity}".encode("utf-8")
        ).hexdigest(),
        observed_at=observed_at,
        anomaly_present=anomaly_present,
        evidence_complete=evidence_complete,
        snapshot_generation=None if snapshot is None else snapshot.generation,
        snapshot_started_at=None if snapshot is None else snapshot.request_started_at,
        snapshot_completed_at=None if snapshot is None else snapshot.request_completed_at,
        last_progress_at=last_progress_at,
        execution_deadline_at=execution_deadline_at,
        durable_terminal_fact=durable_terminal_fact,
    )


def _converge_partial_close_mismatch(
    *,
    desired_json: object,
    pos_id: object,
    snapshot: SnapshotGeneration | None,
) -> bool | None:
    """Compare only the exact position-size contract; never infer identities."""

    if (
        snapshot is None
        or not isinstance(desired_json, str)
        or not 1 <= len(desired_json) <= 4096
        or not isinstance(pos_id, str)
        or not 1 <= len(pos_id) <= 255
    ):
        return None
    try:
        desired = json.loads(desired_json)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(desired, Mapping):
        return None
    desired_pos_id = desired.get("pos_id")
    if (
        not isinstance(desired_pos_id, str)
        or not desired_pos_id
        or desired_pos_id != pos_id
    ):
        return None
    target = _exact_nonnegative_decimal(desired.get("target_remaining_size"))
    if target is None:
        return None
    position_collections = [
        collection
        for collection in snapshot.collections
        if collection.name == "positions"
    ]
    if len(position_collections) != 1:
        return None
    collection = position_collections[0]
    if (
        not collection.available
        or not collection.schema_valid
        or not collection.complete
        or collection.row_count != len(collection.rows)
    ):
        return None
    matches = [
        row
        for row in collection.rows
        if isinstance(row, Mapping) and row.get("posId") == pos_id
    ]
    if len(matches) > 1:
        return None
    if not matches:
        actual = Decimal(0)
    else:
        actual = _exact_nonnegative_decimal(matches[0].get("pos"))
        if actual is None:
            return None
    return actual != target


def _exact_nonnegative_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _readiness_candidate(
    evidence: MonitorReadinessEvidence,
    reason_code: str,
    observed_at: datetime,
    *,
    anomaly_present: bool,
) -> CandidateObservation:
    return _local_candidate(
        reason_code=reason_code,
        identity="readiness:process",
        observed_at=observed_at,
        anomaly_present=anomaly_present,
        last_progress_at=None,
        execution_deadline_at=None,
    )


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("readiness timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("readiness timestamp is invalid") from exc
    return _aware_utc(parsed, field="readiness timestamp")


def _parse_required_timestamp(value: object, *, field: str) -> datetime:
    parsed = _parse_optional_timestamp(value)
    if parsed is None:
        raise ValueError(f"{field} is invalid")
    return parsed


def _parse_database_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise _TypedUnknown("database_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _TypedUnknown("database_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    elif parsed.utcoffset() is None:
        raise _TypedUnknown("database_timestamp_invalid")
    return parsed.astimezone(UTC)


def _closed_status(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _TypedUnknown("database_status_invalid")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _TypedUnknown("database_schema_invalid")
    return value


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _unknown_local(code: str) -> LocalMonitorFactResult:
    if code not in LOCAL_FACT_UNKNOWN_CODES:
        code = "database_unavailable"
    return LocalMonitorFactResult(
        status=FACT_STATUS_UNKNOWN,
        unknown_code=code,
        candidates=(),
    )
