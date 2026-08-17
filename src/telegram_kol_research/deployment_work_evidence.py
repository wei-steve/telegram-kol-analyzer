"""Small, deterministic deployment evidence policy."""

from __future__ import annotations

from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Callable, Literal


MAX_EVIDENCE_COUNT = 1_000_000_000
MAX_EVIDENCE_ROWS = 100_000
EVIDENCE_REGISTRY_VERSION = 2
_TERMINAL_EXECUTION_LEG_STATUSES = frozenset(
    {
        "closed",
        "cancelled",
        "canceled",
        "failed",
        "expired",
        "invalidated",
        "rejected",
        "error",
        "manually_cancelled",
        "manually_closed",
        "exchange_cancelled",
        "filled",
        "completed",
        "done",
        "succeeded",
        "success",
    }
)


@dataclass(frozen=True, slots=True)
class DeploymentEvidenceCounts:
    active_write: int = 0
    unknown_outcome: int = 0
    queued_work: int = 0
    inactive: int = 0
    invalid_evidence: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > MAX_EVIDENCE_COUNT
            ):
                raise ValueError("evidence_count_invalid")


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    decision: Literal["PASS", "WARN", "BLOCK"]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceTally:
    active_write: int = 0
    unknown_outcome: int = 0
    queued_work: int = 0
    inactive: int = 0
    invalid_evidence: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceAdapter:
    name: str
    required_tables: tuple[str, ...]
    required_columns: tuple[tuple[str, tuple[str, ...]], ...]
    collect: Callable[[sqlite3.Connection], EvidenceTally]


@dataclass(frozen=True, slots=True)
class DeploymentEvidenceSnapshot:
    counts: DeploymentEvidenceCounts
    evidence_fingerprint: str
    registered_adapter_count: int


def decide_deployment(
    *,
    counts: DeploymentEvidenceCounts,
    writer_changed: bool,
) -> DeploymentDecision:
    """Evaluate the fixed deployment safety matrix without operator overrides."""

    if not isinstance(counts, DeploymentEvidenceCounts):
        raise ValueError("evidence_counts_invalid")
    if not isinstance(writer_changed, bool):
        raise ValueError("writer_changed_invalid")

    blocking_reasons: list[str] = []
    if counts.invalid_evidence:
        blocking_reasons.append("invalid_registered_evidence")
    if counts.active_write:
        blocking_reasons.append("active_exchange_write")
    if counts.unknown_outcome:
        blocking_reasons.append("unknown_exchange_outcome")
    if writer_changed and counts.queued_work:
        blocking_reasons.append("writer_changed_with_queued_work")
    if blocking_reasons:
        return DeploymentDecision("BLOCK", tuple(blocking_reasons))
    if counts.queued_work:
        return DeploymentDecision("WARN", ("queued_work_with_unchanged_writer",))
    return DeploymentDecision("PASS", ())


def collect_deployment_evidence(
    database_path: str | Path,
) -> DeploymentEvidenceSnapshot:
    """Collect bounded aggregate evidence from an immutable SQLite snapshot."""

    database = Path(database_path).resolve()
    if not database.is_file():
        raise ValueError("evidence_database_invalid")
    uri = f"{database.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValueError("evidence_database_invalid") from exc
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only != (1,):
            raise ValueError("evidence_database_not_read_only")
        tallies = tuple(_collect_adapter(connection, adapter) for adapter in WORK_EVIDENCE_ADAPTERS)
    except sqlite3.Error as exc:
        raise ValueError("evidence_database_invalid") from exc
    finally:
        connection.close()

    totals = {
        field.name: sum(getattr(tally, field.name) for tally in tallies)
        for field in fields(DeploymentEvidenceCounts)
    }
    counts = DeploymentEvidenceCounts(**totals)
    digest = sha256()
    digest.update(f"evidence-registry-v{EVIDENCE_REGISTRY_VERSION}\0".encode("ascii"))
    for adapter, tally in zip(WORK_EVIDENCE_ADAPTERS, tallies, strict=True):
        digest.update(adapter.name.encode("ascii"))
        digest.update(b"\0")
        for field in fields(EvidenceTally):
            digest.update(f"{field.name}={getattr(tally, field.name)}\0".encode("ascii"))
    return DeploymentEvidenceSnapshot(
        counts=counts,
        evidence_fingerprint=digest.hexdigest(),
        registered_adapter_count=len(WORK_EVIDENCE_ADAPTERS),
    )


def _collect_adapter(
    connection: sqlite3.Connection,
    adapter: EvidenceAdapter,
) -> EvidenceTally:
    required_columns = dict(adapter.required_columns)
    for table in adapter.required_tables:
        table_row = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if table_row is None:
            return EvidenceTally(invalid_evidence=1)
        present_columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not set(required_columns.get(table, ())) <= present_columns:
            return EvidenceTally(invalid_evidence=1)
    return adapter.collect(connection)


def _bounded_rows(
    connection: sqlite3.Connection,
    sql: str,
) -> tuple[list[sqlite3.Row | tuple[object, ...]], bool]:
    rows = connection.execute(sql).fetchmany(MAX_EVIDENCE_ROWS + 1)
    if len(rows) > MAX_EVIDENCE_ROWS:
        return [], False
    return rows, True


def _collect_backup_stop_orders(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT venue, pos_id, order_id, client_order_id, status "
        "FROM position_backup_stop_orders ORDER BY id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)

    live_statuses = {
        "submitting",
        "pending_readback",
        "active",
        "unknown_exchange_outcome",
    }
    live_keys: dict[tuple[str, str], int] = {}
    order_keys: dict[tuple[str, str], int] = {}
    for venue, pos_id, order_id, _client_order_id, status in rows:
        if isinstance(status, str) and status in live_statuses:
            if isinstance(venue, str) and venue and isinstance(pos_id, str) and pos_id:
                key = (venue, pos_id)
                live_keys[key] = live_keys.get(key, 0) + 1
        if (
            isinstance(venue, str)
            and venue
            and isinstance(order_id, str)
            and order_id
        ):
            key = (venue, order_id)
            order_keys[key] = order_keys.get(key, 0) + 1
    duplicate_keys = {key for key, count in live_keys.items() if count > 1}
    duplicate_orders = {key for key, count in order_keys.items() if count > 1}

    totals = {field.name: 0 for field in fields(EvidenceTally)}
    totals["invalid_evidence"] += len(duplicate_keys) + len(duplicate_orders)
    for venue, pos_id, order_id, client_order_id, status in rows:
        duplicate_position = (
            isinstance(venue, str)
            and isinstance(pos_id, str)
            and (venue, pos_id) in duplicate_keys
        )
        duplicate_order = (
            isinstance(venue, str)
            and isinstance(order_id, str)
            and (venue, order_id) in duplicate_orders
        )
        if duplicate_position or duplicate_order:
            continue
        if (
            not isinstance(venue, str)
            or not venue
            or not isinstance(pos_id, str)
            or not pos_id
            or not isinstance(client_order_id, str)
            or not client_order_id
            or not isinstance(status, str)
            or not status
        ):
            totals["invalid_evidence"] += 1
        elif status in {"active", "missing", "cancelled"} and (
            not isinstance(order_id, str) or not order_id
        ):
            totals["invalid_evidence"] += 1
        elif status == "submitting":
            totals["active_write"] += 1
        elif status in {"pending_readback", "unknown_exchange_outcome"}:
            totals["unknown_outcome"] += 1
        elif status in {"active", "missing", "cancelled", "unverified_exchange", "failed"}:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_source_deletion_exits(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT source_event_id, raw_message_id, target_lifecycle_id, "
        "execution_binding_id, strategy_instance_id, target_fingerprint, state, "
        "claim_token, claimed_at FROM source_message_deletion_exits ORDER BY id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)

    event_counts: dict[int, int] = {}
    for source_event_id, *_rest in rows:
        if isinstance(source_event_id, int) and not isinstance(source_event_id, bool):
            event_counts[source_event_id] = event_counts.get(source_event_id, 0) + 1
    duplicate_events = {event_id for event_id, count in event_counts.items() if count > 1}

    totals = {field.name: 0 for field in fields(EvidenceTally)}
    totals["invalid_evidence"] += len(duplicate_events)
    for row in rows:
        (
            source_event_id,
            raw_message_id,
            target_lifecycle_id,
            execution_binding_id,
            strategy_instance_id,
            target_fingerprint,
            state,
            claim_token,
            claimed_at,
        ) = row
        if source_event_id in duplicate_events:
            continue
        if (
            not isinstance(source_event_id, int)
            or isinstance(source_event_id, bool)
            or source_event_id < 1
            or not isinstance(state, str)
            or not state
        ):
            totals["invalid_evidence"] += 1
            continue
        target_values = (
            raw_message_id,
            target_lifecycle_id,
            execution_binding_id,
            strategy_instance_id,
            target_fingerprint,
        )
        claim_values = (claim_token, claimed_at)
        if state == "unbound":
            if all(value is None for value in target_values + claim_values):
                totals["inactive"] += 1
            else:
                totals["invalid_evidence"] += 1
        elif state in {"succeeded", "ignored", "failed", "cancelled"}:
            totals["inactive"] += 1
        elif state in {"pending", "waiting"}:
            if any(value is not None for value in target_values) and all(
                value is None for value in claim_values
            ):
                totals["queued_work"] += 1
            else:
                totals["invalid_evidence"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_execution_bindings(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT b.id, b.status, l.status "
        "FROM execution_bindings AS b "
        "LEFT JOIN execution_order_legs AS l ON l.execution_binding_id = b.id "
        "ORDER BY b.id, l.id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    grouped: dict[int, tuple[object, list[object]]] = {}
    for binding_id, binding_status, leg_status in rows:
        if not isinstance(binding_id, int) or isinstance(binding_id, bool):
            return EvidenceTally(invalid_evidence=1)
        current = grouped.setdefault(binding_id, (binding_status, []))
        if current[0] != binding_status:
            return EvidenceTally(invalid_evidence=1)
        if leg_status is not None:
            current[1].append(leg_status)

    orphan_legs = connection.execute(
        "SELECT COUNT(*) FROM execution_order_legs AS l "
        "LEFT JOIN execution_bindings AS b "
        "ON b.id = l.execution_binding_id WHERE b.id IS NULL"
    ).fetchone()
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    if orphan_legs is None or not isinstance(orphan_legs[0], int):
        return EvidenceTally(invalid_evidence=1)
    totals["invalid_evidence"] += min(orphan_legs[0], MAX_EVIDENCE_COUNT)
    known_binding = {
        "open",
        "active",
        "unknown",
        "closed",
        "cancelled",
        "completed",
        "failed",
        "resolved",
        "superseded",
        "stale",
        "rejected",
        "expired",
    }
    active_legs = {"submitting", "cancel_submitting"}
    unknown_legs = {"submit_unknown", "unknown_exchange_outcome"}
    known_legs = active_legs | unknown_legs | _TERMINAL_EXECUTION_LEG_STATUSES | {
        "unknown",
        "pending",
        "open",
        "submitted",
        "active",
        "partially_filled",
        "inconsistent",
        "confirmed",
        "partial",
        "live",
        "partiallyfilled",
        "partial_filled",
    }
    for binding_status, leg_statuses in grouped.values():
        if not isinstance(binding_status, str) or binding_status not in known_binding:
            totals["invalid_evidence"] += 1
        elif any(not isinstance(status, str) or status not in known_legs for status in leg_statuses):
            totals["invalid_evidence"] += 1
        elif any(status in active_legs for status in leg_statuses):
            totals["active_write"] += 1
        elif any(status in unknown_legs for status in leg_statuses):
            totals["unknown_outcome"] += 1
        else:
            totals["inactive"] += 1
    return EvidenceTally(**totals)


def _collect_instruction_contracts(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT i.id, i.status, c.id, c.state, c.terminal_kind, "
        "c.completion_scope FROM message_instruction_items AS i "
        "LEFT JOIN instruction_execution_contracts AS c "
        "ON c.message_instruction_item_id = i.id ORDER BY i.id, c.id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    grouped: dict[int, tuple[object, list[tuple[object, object, object, object]]]] = {}
    for item_id, item_status, contract_id, state, terminal_kind, scope in rows:
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            return EvidenceTally(invalid_evidence=1)
        current = grouped.setdefault(item_id, (item_status, []))
        if current[0] != item_status:
            return EvidenceTally(invalid_evidence=1)
        if contract_id is not None:
            current[1].append((contract_id, state, terminal_kind, scope))

    orphan_contracts = connection.execute(
        "SELECT COUNT(*) FROM instruction_execution_contracts AS c "
        "LEFT JOIN message_instruction_items AS i "
        "ON i.id = c.message_instruction_item_id WHERE i.id IS NULL"
    ).fetchone()
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    if orphan_contracts is None or not isinstance(orphan_contracts[0], int):
        return EvidenceTally(invalid_evidence=1)
    totals["invalid_evidence"] += min(orphan_contracts[0], MAX_EVIDENCE_COUNT)

    item_statuses = {
        "pending",
        "executing",
        "submitted",
        "succeeded",
        "failed",
        "unknown",
    }
    for item_status, contracts in grouped.values():
        if not isinstance(item_status, str) or item_status not in item_statuses:
            totals["invalid_evidence"] += 1
            continue
        if len(contracts) > 1:
            totals["invalid_evidence"] += 1
            continue
        if not contracts:
            if item_status in {"pending", "executing"}:
                totals["queued_work"] += 1
            else:
                totals["inactive"] += 1
            continue
        _contract_id, state, terminal_kind, scope = contracts[0]
        if not isinstance(state, str):
            totals["invalid_evidence"] += 1
        elif state == "submitting":
            totals["active_write"] += 1
        elif state == "submit_unknown":
            totals["unknown_outcome"] += 1
        elif state in {"pending", "deferred"}:
            totals["queued_work"] += 1
        elif state == "verified":
            if terminal_kind is None or scope not in {"full", "partial"}:
                totals["invalid_evidence"] += 1
            else:
                totals["inactive"] += 1
        elif state in {"failed", "expired"}:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_management_batches(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT b.id, b.status, b.reason_code, b.execution_mode, c.status "
        "FROM strategy_management_batches AS b "
        "LEFT JOIN strategy_management_components AS c "
        "ON c.management_batch_id = b.id ORDER BY b.id, c.id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    grouped: dict[int, tuple[object, object, object, list[object]]] = {}
    for batch_id, status, reason, mode, component_status in rows:
        if not isinstance(batch_id, int) or isinstance(batch_id, bool):
            return EvidenceTally(invalid_evidence=1)
        current = grouped.setdefault(batch_id, (status, reason, mode, []))
        if current[:3] != (status, reason, mode):
            return EvidenceTally(invalid_evidence=1)
        if component_status is not None:
            current[3].append(component_status)

    orphan_components = connection.execute(
        "SELECT COUNT(*) FROM strategy_management_components AS c "
        "LEFT JOIN strategy_management_batches AS b "
        "ON b.id = c.management_batch_id WHERE b.id IS NULL"
    ).fetchone()
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    if orphan_components is None or not isinstance(orphan_components[0], int):
        return EvidenceTally(invalid_evidence=1)
    totals["invalid_evidence"] += min(
        orphan_components[0], MAX_EVIDENCE_COUNT
    )
    active_children = {"submitting", "cancel_submitting"}
    unknown_children = {
        "submit_unknown",
        "unknown_exchange_outcome",
        "awaiting_exchange",
        "submitted",
    }
    queued_children = {
        "pending",
        "ready",
        "reserved",
        "preflighting",
        "recovery_required",
    }
    known_children = active_children | unknown_children | {
        *queued_children,
        "succeeded",
        "failed",
        "blocked",
        "resolved",
        "cancelled",
        "definitely_rejected",
        "operator_required",
        "confirmed",
        "safely_skipped",
    }
    temporary_visibility = {
        "protection_missing_cancellable_order_id",
        "target_protection_snapshot_incomplete",
    }
    for status, reason, mode, component_statuses in grouped.values():
        if (
            not isinstance(status, str)
            or not isinstance(mode, str)
            or any(
                not isinstance(child, str) or child not in known_children
                for child in component_statuses
            )
        ):
            totals["invalid_evidence"] += 1
        elif any(child in active_children for child in component_statuses):
            totals["active_write"] += 1
        elif any(child in unknown_children for child in component_statuses):
            totals["unknown_outcome"] += 1
        elif any(child in queued_children for child in component_statuses):
            totals["queued_work"] += 1
        elif status == "executing":
            totals["active_write"] += 1
        elif status in {"submitted", "submit_unknown"}:
            totals["unknown_outcome"] += 1
        elif status in {
            "ready",
            "protection_ready",
            "reserved",
            "reconciling",
            "partial_failed",
        }:
            totals["queued_work"] += 1
        elif status == "recovery_required":
            if reason == "deferred_entry_cancel_race_detected":
                totals["queued_work"] += 1
            else:
                totals["inactive"] += 1
        elif status == "blocked" and reason in temporary_visibility:
            totals["queued_work"] += 1
        elif status in {
            "succeeded",
            "blocked",
            "resolved",
            "failed",
        }:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_strategy_revisions(connection: sqlite3.Connection) -> EvidenceTally:
    batch_rows, batches_bounded = _bounded_rows(
        connection,
        "SELECT id, status, reason_code, advance_claim_token, advance_claimed_at "
        "FROM strategy_revision_batches ORDER BY id",
    )
    leg_rows, legs_bounded = _bounded_rows(
        connection,
        "SELECT revision_batch_id, status FROM strategy_revision_legs ORDER BY id",
    )
    replacement_rows, replacements_bounded = _bounded_rows(
        connection,
        "SELECT revision_batch_id, status FROM entry_revision_replacements ORDER BY id",
    )
    if not (batches_bounded and legs_bounded and replacements_bounded):
        return EvidenceTally(invalid_evidence=1)

    batches: dict[int, tuple[object, object, object, object]] = {}
    for batch_id, status, reason, claim_token, claimed_at in batch_rows:
        if (
            not isinstance(batch_id, int)
            or isinstance(batch_id, bool)
            or batch_id in batches
        ):
            return EvidenceTally(invalid_evidence=1)
        batches[batch_id] = (status, reason, claim_token, claimed_at)

    legs: dict[int, list[object]] = {batch_id: [] for batch_id in batches}
    replacements: dict[int, list[object]] = {batch_id: [] for batch_id in batches}
    orphan_count = 0
    for batch_id, status in leg_rows:
        if batch_id not in legs:
            orphan_count += 1
        else:
            legs[batch_id].append(status)
    for batch_id, status in replacement_rows:
        if batch_id not in replacements:
            orphan_count += 1
        else:
            replacements[batch_id].append(status)

    known_batch_statuses = {
        "planned",
        "shadow_planned",
        "cancelling_old_entries",
        "old_entries_terminal",
        "submitting_replacements",
        "rebuilding",
        "reconciling",
        "succeeded",
        "recovery_required",
        "failed",
        "blocked",
    }
    active_leg_statuses = {"cancel_submitting"}
    unknown_leg_statuses = {"submit_unknown"}
    known_leg_statuses = active_leg_statuses | unknown_leg_statuses | {
        "planned",
        "cancelled",
        "retained",
        "terminal",
    }
    active_replacement_statuses = {"submit_reserved"}
    unknown_replacement_statuses = {"submitted"}
    known_replacement_statuses = active_replacement_statuses | unknown_replacement_statuses | {
        "planned",
        "verified",
    }
    ambiguous_recovery_reasons = {
        "revision_advance_claim_stale",
        "revision_cancel_outcome_unknown",
        "revision_replacement_submit_unknown",
        "revision_replacement_reconciliation_required",
        "entry_revision_stale_claim_write_ambiguous",
        "entry_revision_cancel_outcome_unknown",
        "entry_revision_cancel_not_terminal",
        "entry_revision_cancel_restart_requires_reconciliation",
        "entry_revision_replacement_outcome_unknown",
        "entry_revision_replacement_identity_missing",
        "entry_revision_replacement_readback_missing",
        "entry_revision_replacement_economics_mismatch",
        "entry_revision_replacement_position_unprotected",
        "entry_revision_replacement_restart_requires_reconciliation",
    }
    queued_batch_statuses = {
        "planned",
        "cancelling_old_entries",
        "old_entries_terminal",
        "rebuilding",
    }
    inactive_batch_statuses = {"shadow_planned", "succeeded", "failed", "blocked"}

    totals = {field.name: 0 for field in fields(EvidenceTally)}
    totals["invalid_evidence"] += orphan_count
    for batch_id, (status, reason, claim_token, claimed_at) in batches.items():
        leg_statuses = legs[batch_id]
        replacement_statuses = replacements[batch_id]
        no_claim = claim_token is None and claimed_at is None
        live_claim = (
            isinstance(claim_token, str)
            and bool(claim_token)
            and isinstance(claimed_at, str)
            and bool(claimed_at)
        )
        if (
            not isinstance(status, str)
            or status not in known_batch_statuses
            or (reason is not None and not isinstance(reason, str))
            or not (no_claim or live_claim)
            or any(
                not isinstance(child, str) or child not in known_leg_statuses
                for child in leg_statuses
            )
            or any(
                not isinstance(child, str) or child not in known_replacement_statuses
                for child in replacement_statuses
            )
        ):
            totals["invalid_evidence"] += 1
        elif live_claim and (
            any(child in active_leg_statuses for child in leg_statuses)
            or any(child in active_replacement_statuses for child in replacement_statuses)
        ):
            totals["active_write"] += 1
        elif (
            any(child in unknown_leg_statuses for child in leg_statuses)
            or any(
                child in unknown_replacement_statuses
                for child in replacement_statuses
            )
            or any(child in active_leg_statuses for child in leg_statuses)
            or any(
                child in active_replacement_statuses
                for child in replacement_statuses
            )
            or (status == "recovery_required" and reason in ambiguous_recovery_reasons)
            or status == "reconciling"
        ):
            totals["unknown_outcome"] += 1
        elif status == "submitting_replacements":
            totals["active_write"] += 1
        elif status in queued_batch_statuses:
            totals["queued_work"] += 1
        elif status in inactive_batch_statuses or status == "recovery_required":
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_trigger_protection(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT i.recovery_state, b.status, l.status "
        "FROM trigger_protection_intents AS i "
        "LEFT JOIN execution_bindings AS b ON b.id = i.execution_binding_id "
        "LEFT JOIN execution_order_legs AS l ON l.id = i.execution_order_leg_id "
        "ORDER BY i.id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    terminal_bindings = {"closed", "cancelled", "completed", "failed", "resolved", "superseded"}
    terminal_legs = _TERMINAL_EXECUTION_LEG_STATUSES
    for recovery_state, binding_status, leg_status in rows:
        if not all(isinstance(value, str) and value for value in (recovery_state, binding_status, leg_status)):
            totals["invalid_evidence"] += 1
        elif binding_status in terminal_bindings and leg_status in terminal_legs:
            totals["inactive"] += 1
        elif recovery_state in {"submitting", "cancel_submitting"}:
            totals["active_write"] += 1
        elif recovery_state in {"submit_unknown", "unknown_exchange_outcome"}:
            totals["unknown_outcome"] += 1
        elif recovery_state in {"pending", "retrying", "failed"}:
            totals["queued_work"] += 1
        elif recovery_state in {"recovery_required", "succeeded", "resolved", "adopted"}:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_position_mutations(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT status FROM position_mutation_intents ORDER BY id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    for (status,) in rows:
        if status in {"submitting", "cancel_submitting"}:
            totals["active_write"] += 1
        elif status in {"submitted", "recovery_required", "submit_unknown", "unknown_exchange_outcome"}:
            totals["unknown_outcome"] += 1
        elif status == "reserved":
            totals["queued_work"] += 1
        elif status in {"confirmed", "blocked", "rejected", "failed", "cancelled"}:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


def _collect_trade_signals(connection: sqlite3.Connection) -> EvidenceTally:
    rows, bounded = _bounded_rows(
        connection,
        "SELECT id, chat_id, message_id, symbol, side, status "
        "FROM trade_signals ORDER BY id",
    )
    if not bounded:
        return EvidenceTally(invalid_evidence=1)
    totals = {field.name: 0 for field in fields(EvidenceTally)}
    verified_leg_states = _TERMINAL_EXECUTION_LEG_STATUSES | {
        "active",
        "partially_filled",
    }
    for _signal_id, chat_id, message_id, symbol, side, status in rows:
        if not isinstance(status, str):
            totals["invalid_evidence"] += 1
        elif status == "pending":
            totals["queued_work"] += 1
        elif status in {"processing", "submitting", "cancel_submitting"}:
            totals["active_write"] += 1
        elif status in {"unknown_exchange_outcome", "submit_unknown"}:
            totals["unknown_outcome"] += 1
        elif status == "partial_submission_failed":
            bindings = connection.execute(
                "SELECT id FROM execution_bindings WHERE chat_id = ? AND message_id = ? "
                "AND symbol = ? AND side = ? ORDER BY id LIMIT 2",
                (chat_id, message_id, symbol, side),
            ).fetchall()
            if len(bindings) != 1:
                totals["unknown_outcome"] += 1
                continue
            leg_rows = connection.execute(
                "SELECT status FROM execution_order_legs "
                "WHERE execution_binding_id = ? ORDER BY id LIMIT ?",
                (bindings[0][0], MAX_EVIDENCE_ROWS + 1),
            ).fetchall()
            if leg_rows and len(leg_rows) <= MAX_EVIDENCE_ROWS and all(
                isinstance(row[0], str) and row[0] in verified_leg_states
                for row in leg_rows
            ):
                totals["inactive"] += 1
            else:
                totals["unknown_outcome"] += 1
        elif status in {"submitted", "succeeded", "executed", "failed", "cancelled"}:
            totals["inactive"] += 1
        else:
            totals["invalid_evidence"] += 1
    return EvidenceTally(**totals)


WORK_EVIDENCE_ADAPTERS = (
    EvidenceAdapter(
        name="position_backup_stop_orders",
        required_tables=("position_backup_stop_orders",),
        required_columns=(
            (
                "position_backup_stop_orders",
                ("id", "venue", "pos_id", "order_id", "client_order_id", "status"),
            ),
        ),
        collect=_collect_backup_stop_orders,
    ),
    EvidenceAdapter(
        name="source_message_deletion_exits",
        required_tables=("source_message_deletion_exits",),
        required_columns=(
            (
                "source_message_deletion_exits",
                (
                    "id",
                    "source_event_id",
                    "raw_message_id",
                    "target_lifecycle_id",
                    "execution_binding_id",
                    "strategy_instance_id",
                    "target_fingerprint",
                    "state",
                    "claim_token",
                    "claimed_at",
                ),
            ),
        ),
        collect=_collect_source_deletion_exits,
    ),
    EvidenceAdapter(
        name="execution_bindings",
        required_tables=("execution_bindings", "execution_order_legs"),
        required_columns=(
            ("execution_bindings", ("id", "status")),
            ("execution_order_legs", ("id", "execution_binding_id", "status")),
        ),
        collect=_collect_execution_bindings,
    ),
    EvidenceAdapter(
        name="instruction_execution_contracts",
        required_tables=("message_instruction_items", "instruction_execution_contracts"),
        required_columns=(
            ("message_instruction_items", ("id", "status")),
            (
                "instruction_execution_contracts",
                (
                    "id",
                    "message_instruction_item_id",
                    "state",
                    "terminal_kind",
                    "completion_scope",
                ),
            ),
        ),
        collect=_collect_instruction_contracts,
    ),
    EvidenceAdapter(
        name="strategy_management_batches",
        required_tables=("strategy_management_batches", "strategy_management_components"),
        required_columns=(
            (
                "strategy_management_batches",
                ("id", "status", "reason_code", "execution_mode"),
            ),
            (
                "strategy_management_components",
                ("id", "management_batch_id", "status"),
            ),
        ),
        collect=_collect_management_batches,
    ),
    EvidenceAdapter(
        name="strategy_revision_batches",
        required_tables=(
            "strategy_revision_batches",
            "strategy_revision_legs",
            "entry_revision_replacements",
        ),
        required_columns=(
            (
                "strategy_revision_batches",
                (
                    "id",
                    "status",
                    "reason_code",
                    "advance_claim_token",
                    "advance_claimed_at",
                ),
            ),
            (
                "strategy_revision_legs",
                ("id", "revision_batch_id", "status"),
            ),
            (
                "entry_revision_replacements",
                ("id", "revision_batch_id", "status"),
            ),
        ),
        collect=_collect_strategy_revisions,
    ),
    EvidenceAdapter(
        name="trigger_protection_intents",
        required_tables=(
            "trigger_protection_intents",
            "execution_bindings",
            "execution_order_legs",
        ),
        required_columns=(
            (
                "trigger_protection_intents",
                (
                    "id",
                    "execution_binding_id",
                    "execution_order_leg_id",
                    "recovery_state",
                ),
            ),
            ("execution_bindings", ("id", "status")),
            ("execution_order_legs", ("id", "status")),
        ),
        collect=_collect_trigger_protection,
    ),
    EvidenceAdapter(
        name="position_mutation_intents",
        required_tables=("position_mutation_intents",),
        required_columns=(("position_mutation_intents", ("id", "status")),),
        collect=_collect_position_mutations,
    ),
    EvidenceAdapter(
        name="trade_signals",
        required_tables=("trade_signals", "execution_bindings", "execution_order_legs"),
        required_columns=(
            (
                "trade_signals",
                ("id", "chat_id", "message_id", "symbol", "side", "status"),
            ),
            (
                "execution_bindings",
                ("id", "chat_id", "message_id", "symbol", "side", "status"),
            ),
            ("execution_order_legs", ("id", "execution_binding_id", "status")),
        ),
        collect=_collect_trade_signals,
    ),
)
