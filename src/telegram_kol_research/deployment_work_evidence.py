"""Small, deterministic deployment evidence policy."""

from __future__ import annotations

from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Callable, Literal


MAX_EVIDENCE_COUNT = 1_000_000_000
MAX_EVIDENCE_ROWS = 100_000
EVIDENCE_REGISTRY_VERSION = 1


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
    required_columns: tuple[str, ...]
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
        if not set(adapter.required_columns) <= present_columns:
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
        "SELECT venue, pos_id, order_id, status "
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
    for venue, pos_id, _order_id, status in rows:
        if isinstance(status, str) and status in live_statuses:
            if isinstance(venue, str) and venue and isinstance(pos_id, str) and pos_id:
                key = (venue, pos_id)
                live_keys[key] = live_keys.get(key, 0) + 1
    duplicate_keys = {key for key, count in live_keys.items() if count > 1}

    totals = {field.name: 0 for field in fields(EvidenceTally)}
    totals["invalid_evidence"] += len(duplicate_keys)
    for venue, pos_id, _order_id, status in rows:
        if (
            isinstance(venue, str)
            and isinstance(pos_id, str)
            and (venue, pos_id) in duplicate_keys
        ):
            continue
        if (
            not isinstance(venue, str)
            or not venue
            or not isinstance(pos_id, str)
            or not pos_id
            or not isinstance(status, str)
            or not status
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


WORK_EVIDENCE_ADAPTERS = (
    EvidenceAdapter(
        name="position_backup_stop_orders",
        required_tables=("position_backup_stop_orders",),
        required_columns=("id", "venue", "pos_id", "order_id", "status"),
        collect=_collect_backup_stop_orders,
    ),
    EvidenceAdapter(
        name="source_message_deletion_exits",
        required_tables=("source_message_deletion_exits",),
        required_columns=(
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
        collect=_collect_source_deletion_exits,
    ),
)
