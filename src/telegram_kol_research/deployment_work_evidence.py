"""Bounded deployment work evidence and policy classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from typing import Collection, Mapping


WORK_CLASSIFICATIONS = (
    "in_flight_write",
    "unknown_outcome",
    "restart_safe_wait",
    "historical_residue",
    "terminal",
    "malformed",
)
DEPLOYMENT_CHANGE_CLASSES = frozenset(
    {"code", "schema_compatible", "execution_writer", "live_promotion"}
)
WRITER_SENSITIVE_CHANGE_CLASSES = frozenset(
    {"execution_writer", "live_promotion"}
)
_MAX_BOUNDED_COUNT = 1_000_000


class DeploymentWorkEvidenceError(ValueError):
    """Work evidence is incomplete, unbounded, or otherwise malformed."""


@dataclass(frozen=True, slots=True)
class DeploymentWorkDecision:
    blocking_reason_codes: tuple[str, ...]
    warning_reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkEvidenceAdapter:
    """Reviewed durable-state mapping for one restart-sensitive work table."""

    output_name: str
    table: str
    state_column: str
    restart_safe_states: frozenset[str]
    in_flight_states: frozenset[str]
    unknown_states: frozenset[str]
    terminal_states: frozenset[str]
    required_columns: frozenset[str] = frozenset({"created_at"})
    progress_columns: tuple[str, ...] = ()
    origin_columns: tuple[str, ...] = ("created_at", "planned_at", "reserved_at")
    restart_surface_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentWorkSummary:
    counts: Mapping[str, Mapping[str, int]]
    fingerprint: str


def _adapter(
    output_name: str,
    table: str,
    state_column: str,
    *,
    restart: Collection[str] = (),
    in_flight: Collection[str] = (),
    unknown: Collection[str] = (),
    terminal: Collection[str] = (),
    progress: tuple[str, ...] = (),
    restart_surface: tuple[str, ...] = (),
) -> WorkEvidenceAdapter:
    return WorkEvidenceAdapter(
        output_name=output_name,
        table=table,
        state_column=state_column,
        restart_safe_states=frozenset(restart),
        in_flight_states=frozenset(in_flight),
        unknown_states=frozenset(unknown),
        terminal_states=frozenset(terminal),
        progress_columns=progress,
        restart_surface_files=restart_surface,
    )


WORK_EVIDENCE_ADAPTERS = (
    _adapter(
        "execution_order_legs",
        "execution_order_legs",
        "status",
        restart=("pending", "submitted", "open"),
        in_flight=("submitting", "cancel_submitting"),
        unknown=("submit_unknown", "unknown_exchange_outcome", "unknown"),
        terminal=(
            "active",
            "filled",
            "partial_closed",
            "cancelled",
            "canceled",
            "closed",
            "exchange_cancelled",
            "expired",
            "invalidated",
            "manually_cancelled",
            "manually_closed",
            "retained",
            "verified",
            "failed",
            "rejected",
        ),
        restart_surface=(
            "src/telegram_kol_research/execution_bindings.py",
            "src/telegram_kol_research/instruction_execution_reconciliation.py",
        ),
    ),
    _adapter(
        "instruction_items",
        "message_instruction_items",
        "status",
        restart=("pending",),
        in_flight=("executing",),
        unknown=("unknown",),
        terminal=("submitted", "succeeded", "failed"),
        progress=("last_progress_at",),
        restart_surface=(
            "src/telegram_kol_research/message_instruction_items.py",
            "src/telegram_kol_research/instruction_execution_reconciliation.py",
        ),
    ),
    _adapter(
        "trade_signals",
        "trade_signals",
        "status",
        restart=("pending",),
        in_flight=("processing",),
        unknown=("unknown_exchange_outcome", "partial_submission_failed"),
        terminal=("submitted", "succeeded", "failed", "skipped", "completed"),
        restart_surface=("src/telegram_kol_research/auto_trade_execution.py",),
    ),
    _adapter(
        "execution_contracts",
        "instruction_execution_contracts",
        "state",
        restart=("pending", "deferred"),
        in_flight=("submitting",),
        unknown=("submit_unknown",),
        terminal=("verified", "failed", "expired"),
        restart_surface=(
            "src/telegram_kol_research/instruction_execution_contracts.py",
            "src/telegram_kol_research/instruction_execution_reconciliation.py",
        ),
    ),
    _adapter(
        "strategy_revisions",
        "strategy_revision_batches",
        "status",
        restart=("planned", "old_entries_terminal", "reconciling"),
        in_flight=(
            "cancelling_old_entries",
            "submitting_replacements",
            "rebuilding",
        ),
        unknown=("recovery_required",),
        terminal=("succeeded", "failed", "blocked"),
        restart_surface=(
            "src/telegram_kol_research/entry_revision_executor.py",
            "src/telegram_kol_research/strategy_revision_planner.py",
        ),
    ),
    _adapter(
        "management_batches",
        "strategy_management_batches",
        "status",
        restart=(
            "ready",
            "pending",
            "submitted",
            "reconciling",
            "protection_ready",
        ),
        in_flight=("reserved", "executing"),
        unknown=("submit_unknown", "partial_failed", "recovery_required"),
        terminal=("succeeded", "blocked", "resolved"),
        progress=("last_progress_at",),
        restart_surface=(
            "src/telegram_kol_research/strategy_management_batches.py",
            "src/telegram_kol_research/strategy_management_reconciliation.py",
            "src/telegram_kol_research/strategy_management_worker.py",
        ),
    ),
    _adapter(
        "management_legs",
        "strategy_management_legs",
        "status",
        restart=("planned", "submitted"),
        in_flight=("reserved",),
        unknown=("submit_unknown", "recovery_required"),
        terminal=(
            "confirmed",
            "partial",
            "succeeded",
            "restored",
            "failed",
            "inconsistent",
        ),
        restart_surface=(
            "src/telegram_kol_research/strategy_management_reconciliation.py",
            "src/telegram_kol_research/strategy_management_worker.py",
        ),
    ),
    _adapter(
        "management_components",
        "strategy_management_components",
        "status",
        restart=("pending", "definitely_rejected"),
        in_flight=("preflighting", "submitting"),
        unknown=("awaiting_exchange", "recovery_required"),
        terminal=("confirmed", "operator_required", "safely_skipped"),
        restart_surface=(
            "src/telegram_kol_research/strategy_management_composite_reconciliation.py",
            "src/telegram_kol_research/strategy_management_composite_executor.py",
        ),
    ),
    _adapter(
        "position_mutations",
        "position_mutation_intents",
        "status",
        restart=("submitted",),
        in_flight=("reserved", "submitting"),
        unknown=("submit_unknown", "recovery_required"),
        terminal=("confirmed", "rejected", "blocked"),
        restart_surface=(
            "src/telegram_kol_research/position_mutation_intents.py",
        ),
    ),
    _adapter(
        "position_closes",
        "bound_position_close_reservations",
        "status",
        restart=("submitted",),
        in_flight=("reserved",),
        unknown=(
            "submit_unknown",
            "unknown_exchange_outcome",
            "recovery_required",
        ),
        terminal=("confirmed", "failed", "rejected", "blocked", "closed"),
        restart_surface=(
            "src/telegram_kol_research/deepcoin_execution_actions.py",
        ),
    ),
    _adapter(
        "backup_stop_orders",
        "position_backup_stop_orders",
        "status",
        in_flight=("submitting",),
        unknown=("pending_readback", "unknown_exchange_outcome"),
        terminal=("active", "verified", "filled", "cancelled", "expired", "failed"),
        restart_surface=(
            "src/telegram_kol_research/trigger_backup_stop_executor.py",
        ),
    ),
    _adapter(
        "take_profit_orders",
        "position_take_profit_orders",
        "status",
        unknown=("cancel_requested",),
        terminal=("active", "cancelled", "expired", "filled"),
        restart_surface=(
            "src/telegram_kol_research/position_take_profit_orders.py",
        ),
    ),
    _adapter(
        "protection_legs",
        "position_protection_legs",
        "status",
        restart=("planned", "waiting_fill"),
        in_flight=("submitting",),
        unknown=("protection_recovery_pending",),
        terminal=("verified", "filled", "cancelled", "failed"),
        restart_surface=(
            "src/telegram_kol_research/position_protection_legs.py",
        ),
    ),
    _adapter(
        "protection_intents",
        "trigger_protection_intents",
        "recovery_state",
        restart=("pending",),
        in_flight=("submitting",),
        unknown=("retrying",),
        terminal=("adopted", "resolved", "failed", "blocked"),
        restart_surface=(
            "src/telegram_kol_research/trigger_protection_intents.py",
        ),
    ),
    _adapter(
        "protection_rescues",
        "trigger_protection_stop_rescues",
        "status",
        restart=("ready", "submitted"),
        in_flight=("reserved",),
        unknown=("submit_unknown", "recovery_required"),
        terminal=("verified", "failed", "blocked"),
        restart_surface=(
            "src/telegram_kol_research/strategy_management_executor.py",
        ),
    ),
    _adapter(
        "trigger_take_profit_convergences",
        "trigger_take_profit_convergences",
        "status",
        restart=("ready", "submitted", "waiting_backup_stop"),
        in_flight=("reserved",),
        unknown=("submit_unknown",),
        terminal=("completed", "conflicted", "blocked", "failed"),
        restart_surface=(
            "src/telegram_kol_research/position_take_profit_orders.py",
        ),
    ),
    _adapter(
        "break_even_convergences",
        "strategy_break_even_convergences",
        "status",
        restart=("planned", "shadow_deciding"),
        in_flight=(
            "claimed",
            "preflight_verified",
            "deciding_by_market",
            "executing_market_decisions",
        ),
        unknown=("recovery_required",),
        terminal=(
            "completed",
            "succeeded",
            "blocked",
            "failed",
            "failed_terminal",
            "shadow_planned",
        ),
        restart_surface=(
            "src/telegram_kol_research/break_even_convergence_worker.py",
        ),
    ),
    _adapter(
        "break_even_convergence_legs",
        "strategy_break_even_convergence_legs",
        "status",
        in_flight=("decision_reserved",),
        unknown=("submit_unknown", "recovery_required"),
        terminal=(
            "planned",
            "submitted",
            "completed",
            "succeeded",
            "blocked",
            "failed",
            "failed_terminal",
            "verified",
            "confirmed",
            "cancelled",
            "shadow_planned",
        ),
        restart_surface=(
            "src/telegram_kol_research/break_even_convergence_worker.py",
        ),
    ),
    _adapter(
        "source_deletions",
        "source_message_deletion_exits",
        "state",
        restart=("pending", "reconciling"),
        in_flight=("cancelling_entries", "closing_positions"),
        unknown=("recovery_required",),
        terminal=("succeeded", "blocked", "failed"),
        restart_surface=(
            "src/telegram_kol_research/source_message_deletion_worker.py",
        ),
    ),
)

WORK_EVIDENCE_SOURCES = frozenset(
    adapter.output_name for adapter in WORK_EVIDENCE_ADAPTERS
)
_NON_WORK_STATE_TABLES = frozenset(
    {
        "ai_prompt_invocations",
        "ai_prompt_test_runs",
        "ai_prompt_versions",
        "context_resolution_attempts",
        "entry_assembly_attempts",
        "entry_preambles",
        "entry_revision_replacements",
        "entry_strategy_fragments",
        "execution_bindings",
        "execution_events",
        "message_operation_contracts",
        "message_operation_items",
        "message_operation_stage1_notifications",
        "message_recognitions",
        "position_protection_ledger",
        "position_protection_revisions",
        "recognition_experiments",
        "recovery_order_confirmations",
        "runtime_agent_model_usage",
        "runtime_agent_recovery_attempts",
        "runtime_incident_handoff_artifacts",
        "runtime_incident_observations",
        "runtime_incidents",
        "strategy_alerts",
        "strategy_management_notifications",
        "strategy_message_links",
        "strategy_revision_legs",
        "strategy_threads",
        "trade_ideas",
    }
)


def collect_work_evidence(
    connection: sqlite3.Connection,
    *,
    available_tables: Collection[str],
    now: datetime,
    historical_after: timedelta = timedelta(hours=1),
) -> DeploymentWorkSummary:
    """Collect bounded, privacy-safe evidence without consulting ``updated_at``."""

    checked_at = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    cutoff = checked_at - historical_after
    counts: dict[str, dict[str, int]] = {
        classification: {} for classification in WORK_CLASSIFICATIONS
    }
    fingerprint_rows: list[dict[str, object]] = []
    for adapter in WORK_EVIDENCE_ADAPTERS:
        if adapter.table not in available_tables:
            continue
        columns = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_identifier(adapter.table)})"
            ).fetchall()
        }
        required = {adapter.state_column, *adapter.required_columns}
        if not required.issubset(columns):
            raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
        evidence_columns = tuple(
            dict.fromkeys(
                column
                for column in (
                    *adapter.progress_columns,
                    *adapter.origin_columns,
                )
                if column in columns
            )
        )
        states = sorted(
            adapter.restart_safe_states
            | adapter.in_flight_states
            | adapter.unknown_states
            | adapter.terminal_states
        )
        rows = _aggregate_adapter_rows(
            connection,
            adapter=adapter,
            evidence_columns=evidence_columns,
            recognized_states=states,
            cutoff=cutoff,
        )
        for row in rows:
            state = "" if row[0] is None else str(row[0])
            malformed = bool(row[1])
            historical = bool(row[2])
            evidence_presence = tuple(bool(value) for value in row[3:-1])
            count = int(row[-1])
            if count < 0 or count > _MAX_BOUNDED_COUNT:
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
            if malformed:
                classification = "malformed"
            elif state in adapter.unknown_states:
                classification = "unknown_outcome"
            elif state in adapter.in_flight_states:
                classification = "in_flight_write"
            elif state in adapter.restart_safe_states:
                classification = (
                    "historical_residue" if historical else "restart_safe_wait"
                )
            elif state in adapter.terminal_states:
                classification = "terminal"
            else:
                classification = "malformed"
            counts[classification][adapter.output_name] = (
                counts[classification].get(adapter.output_name, 0) + count
            )
            fingerprint_rows.append(
                {
                    "source": adapter.output_name,
                    "state": state,
                    "classification": classification,
                    "evidence_presence": evidence_presence,
                    "count": count,
                }
            )

    unregistered = _unregistered_state_table_count(
        connection,
        available_tables=available_tables,
    )
    if unregistered:
        counts["malformed"]["unregistered_work_tables"] = unregistered
        fingerprint_rows.append(
            {
                "source": "unregistered_work_tables",
                "state": "unregistered",
                "classification": "malformed",
                "evidence_presence": (),
                "count": unregistered,
            }
        )
    bounded = {
        classification: dict(sorted(sources.items()))
        for classification, sources in counts.items()
        if sources
    }
    _validate_counts(bounded)
    body = json.dumps(fingerprint_rows, sort_keys=True, separators=(",", ":"))
    return DeploymentWorkSummary(
        counts=bounded,
        fingerprint=sha256(body.encode("utf-8")).hexdigest(),
    )


def _aggregate_adapter_rows(
    connection: sqlite3.Connection,
    *,
    adapter: WorkEvidenceAdapter,
    evidence_columns: tuple[str, ...],
    recognized_states: Collection[str],
    cutoff: datetime,
):
    state = _identifier(adapter.state_column)
    placeholders = ",".join("?" for _ in recognized_states)
    normalized_state = (
        f"CASE WHEN {state} IN ({placeholders}) "
        f"THEN {state} ELSE '__unrecognized__' END"
    )
    malformed_parts = [
        f"{state} IS NULL",
        f"{state} NOT IN ({placeholders})",
    ]
    for column in adapter.required_columns:
        identifier = _identifier(column)
        malformed_parts.extend(
            (f"{identifier} IS NULL", f"julianday({identifier}) IS NULL")
        )
    for column in evidence_columns:
        if column in adapter.required_columns:
            continue
        identifier = _identifier(column)
        malformed_parts.append(
            f"({identifier} IS NOT NULL AND julianday({identifier}) IS NULL)"
        )
    if "created_at" in evidence_columns:
        created = _identifier("created_at")
        for column in adapter.progress_columns:
            if column not in evidence_columns:
                continue
            progress = _identifier(column)
            malformed_parts.append(
                f"({progress} IS NOT NULL AND "
                f"julianday({progress}) < julianday({created}))"
            )
    authoritative_columns = [
        column
        for column in (*adapter.progress_columns, *adapter.origin_columns)
        if column in evidence_columns
    ]
    authoritative = ", ".join(_identifier(column) for column in authoritative_columns)
    if len(authoritative_columns) > 1:
        authoritative = f"COALESCE({authoritative})"
    presence_expressions = [
        f"CASE WHEN {_identifier(column)} IS NULL THEN 0 ELSE 1 END"
        for column in evidence_columns
    ]
    inner_fields = [
        f"{normalized_state} AS state_value",
        f"CASE WHEN {' OR '.join(malformed_parts)} THEN 1 ELSE 0 END "
        "AS malformed_value",
        f"CASE WHEN julianday({authoritative}) < julianday(?) "
        "THEN 1 ELSE 0 END AS historical_value",
        *(
            f"{expression} AS evidence_{index}"
            for index, expression in enumerate(presence_expressions)
        ),
    ]
    group_fields = [
        "state_value",
        "malformed_value",
        "historical_value",
        *(f"evidence_{index}" for index in range(len(presence_expressions))),
    ]
    sql = (
        f"SELECT {', '.join(group_fields)}, COUNT(*) "
        f"FROM (SELECT {', '.join(inner_fields)} "
        f"FROM {_identifier(adapter.table)}) "
        f"GROUP BY {', '.join(group_fields)}"
    )
    parameters = [
        *recognized_states,
        *recognized_states,
        cutoff.replace(tzinfo=None).isoformat(" "),
    ]
    return connection.execute(sql, parameters)


def _unregistered_state_table_count(
    connection: sqlite3.Connection,
    *,
    available_tables: Collection[str],
) -> int:
    registered = {adapter.table for adapter in WORK_EVIDENCE_ADAPTERS}
    allowed = registered | set(_NON_WORK_STATE_TABLES)
    unregistered = 0
    for table in sorted(set(available_tables) - allowed):
        columns = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_identifier(table)})"
            ).fetchall()
        }
        if columns.intersection({"state", "status"}):
            unregistered += 1
    if unregistered > _MAX_BOUNDED_COUNT:
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    return unregistered


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    return f'"{value}"'


def classify_deployment_work(
    *,
    counts: Mapping[str, Mapping[str, int]],
    change_class: str,
) -> DeploymentWorkDecision:
    """Map bounded durable work facts to deterministic deployment reasons."""

    normalized_class = str(change_class).strip().lower()
    if normalized_class not in DEPLOYMENT_CHANGE_CLASSES:
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    _validate_counts(counts)

    blocking: set[str] = set()
    warnings: set[str] = set()
    if _has_work(counts, "in_flight_write"):
        blocking.add("deployment_in_flight_write")
    if _has_work(counts, "unknown_outcome"):
        blocking.add("deployment_unknown_outcome")
    if _has_work(counts, "malformed"):
        blocking.add("deployment_evidence_malformed")
    for classification, reason in (
        ("restart_safe_wait", "deployment_restart_safe_wait"),
        ("historical_residue", "deployment_historical_residue"),
    ):
        if not _has_work(counts, classification):
            continue
        target = (
            blocking
            if normalized_class in WRITER_SENSITIVE_CHANGE_CLASSES
            else warnings
        )
        target.add(reason)
    return DeploymentWorkDecision(
        blocking_reason_codes=tuple(sorted(blocking)),
        warning_reason_codes=tuple(sorted(warnings)),
    )


def _has_work(counts: Mapping[str, Mapping[str, int]], classification: str) -> bool:
    return any(value > 0 for value in counts.get(classification, {}).values())


def _validate_counts(counts: Mapping[str, Mapping[str, int]]) -> None:
    if not isinstance(counts, Mapping):
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    allowed = set(WORK_CLASSIFICATIONS)
    for classification, sources in counts.items():
        if classification not in allowed or not isinstance(sources, Mapping):
            raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
        for source, value in sources.items():
            allowed_sources = WORK_EVIDENCE_SOURCES | (
                {"unregistered_work_tables"}
                if classification == "malformed"
                else set()
            )
            if source not in allowed_sources:
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_BOUNDED_COUNT
            ):
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
