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
    progress_columns: tuple[str, ...] = ()
    origin_columns: tuple[str, ...] = ("created_at", "planned_at", "reserved_at")


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
    )


WORK_EVIDENCE_ADAPTERS = (
    _adapter(
        "execution_order_legs",
        "execution_order_legs",
        "status",
        in_flight=("submitting", "cancel_submitting"),
        unknown=("submit_unknown", "unknown_exchange_outcome", "unknown"),
        terminal=(
            "pending",
            "submitted",
            "open",
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
    ),
    _adapter(
        "trade_signals",
        "trade_signals",
        "status",
        restart=("pending",),
        in_flight=("processing",),
        unknown=("unknown_exchange_outcome", "partial_submission_failed"),
        terminal=("submitted", "succeeded", "failed", "skipped", "completed"),
    ),
    _adapter(
        "execution_contracts",
        "instruction_execution_contracts",
        "state",
        restart=("pending", "deferred"),
        in_flight=("submitting",),
        unknown=("submit_unknown",),
        terminal=("verified", "failed", "expired"),
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
    ),
    _adapter(
        "management_components",
        "strategy_management_components",
        "status",
        restart=("pending", "definitely_rejected"),
        in_flight=("preflighting", "submitting"),
        unknown=("awaiting_exchange", "recovery_required"),
        terminal=("confirmed", "operator_required", "safely_skipped"),
    ),
    _adapter(
        "position_mutations",
        "position_mutation_intents",
        "status",
        restart=("submitted",),
        in_flight=("reserved", "submitting"),
        unknown=("submit_unknown", "recovery_required"),
        terminal=("confirmed", "rejected", "blocked"),
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
    ),
    _adapter(
        "backup_stop_orders",
        "position_backup_stop_orders",
        "status",
        in_flight=("submitting",),
        unknown=("pending_readback", "unknown_exchange_outcome"),
        terminal=("active", "verified", "filled", "cancelled", "expired", "failed"),
    ),
    _adapter(
        "take_profit_orders",
        "position_take_profit_orders",
        "status",
        unknown=("cancel_requested",),
        terminal=("active", "cancelled", "expired", "filled"),
    ),
    _adapter(
        "protection_legs",
        "position_protection_legs",
        "status",
        restart=("planned", "waiting_fill"),
        in_flight=("submitting",),
        unknown=("protection_recovery_pending",),
        terminal=("verified", "filled", "cancelled", "failed"),
    ),
    _adapter(
        "protection_intents",
        "trigger_protection_intents",
        "recovery_state",
        restart=("pending",),
        in_flight=("submitting",),
        unknown=("retrying",),
        terminal=("adopted", "resolved", "failed", "blocked"),
    ),
    _adapter(
        "protection_rescues",
        "trigger_protection_stop_rescues",
        "status",
        restart=("ready", "submitted"),
        in_flight=("reserved",),
        unknown=("submit_unknown", "recovery_required"),
        terminal=("verified", "failed", "blocked"),
    ),
    _adapter(
        "trigger_take_profit_convergences",
        "trigger_take_profit_convergences",
        "status",
        restart=("ready", "submitted"),
        in_flight=("reserved",),
        unknown=("submit_unknown",),
        terminal=("completed", "conflicted", "blocked", "failed"),
    ),
    _adapter(
        "break_even_convergences",
        "strategy_break_even_convergences",
        "status",
        restart=("planned",),
        in_flight=(
            "claimed",
            "preflight_verified",
            "deciding_by_market",
            "executing_market_decisions",
        ),
        unknown=("recovery_required",),
        terminal=("completed", "succeeded", "blocked", "failed"),
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
    ),
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
        if adapter.state_column not in columns:
            raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
        evidence_columns = tuple(
            column
            for column in (*adapter.progress_columns, *adapter.origin_columns)
            if column in columns
        )
        states = sorted(
            adapter.restart_safe_states
            | adapter.in_flight_states
            | adapter.unknown_states
            | adapter.terminal_states
        )
        if not states:
            continue
        selected = [adapter.state_column, *evidence_columns]
        rows = connection.execute(
            f"SELECT {', '.join(_identifier(item) for item in selected)} "
            f"FROM {_identifier(adapter.table)}",
        ).fetchall()
        aggregated: dict[tuple[str, str, tuple[bool, ...]], int] = {}
        for row in rows:
            state = str(row[0])
            if state in adapter.unknown_states:
                classification = "unknown_outcome"
            elif state in adapter.in_flight_states:
                classification = "in_flight_write"
            elif state in adapter.restart_safe_states:
                authoritative = _first_timestamp(row[1:])
                classification = (
                    "historical_residue"
                    if authoritative is not None and authoritative < cutoff
                    else "restart_safe_wait"
                )
            elif state in adapter.terminal_states:
                classification = "terminal"
            else:
                classification = "malformed"
            counts[classification][adapter.output_name] = (
                counts[classification].get(adapter.output_name, 0) + 1
            )
            evidence_presence = tuple(value not in (None, "") for value in row[1:])
            key = (state, classification, evidence_presence)
            aggregated[key] = aggregated.get(key, 0) + 1
        for (state, classification, evidence_presence), count in sorted(
            aggregated.items()
        ):
            fingerprint_rows.append(
                {
                    "source": adapter.output_name,
                    "state": state,
                    "classification": classification,
                    "evidence_presence": evidence_presence,
                    "count": count,
                }
            )
    bounded = {
        classification: dict(sorted(sources.items()))
        for classification, sources in counts.items()
        if sources
    }
    body = json.dumps(fingerprint_rows, sort_keys=True, separators=(",", ":"))
    return DeploymentWorkSummary(
        counts=bounded,
        fingerprint=sha256(body.encode("utf-8")).hexdigest(),
    )


def _first_timestamp(values: Collection[object]) -> datetime | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    return None


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
            if not isinstance(source, str) or not source:
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_BOUNDED_COUNT
            ):
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
