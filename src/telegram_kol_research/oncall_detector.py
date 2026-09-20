"""Deterministic, read-only detection of "the message asked, nothing happened".

Phase 1 of the Codex on-call remediation program
(``docs/plans/2026-09-19-codex-oncall-phase1-spec.md``, sections 3 and 4).
No AI, no exchange call, no write to the production database.

**Why the reads are shaped the way they are.** On 2026-09-15 an analysis
session ran full-table scans against the production SQLite file and froze the
worker's event loop eight times for 15-30 seconds. So this module opens the
production database read-only (``mode=ro`` plus ``PRAGMA query_only=ON``), one
short connection per round, and never scans: each table is read forward by
primary-key watermark, and rows that need re-checking later are remembered by
primary key in the watcher's own state database and re-read as point queries.
:data:`ALLOWED_QUERY_SHAPES` names the only three shapes allowed, and a test
asserts every statement this module runs matches one of them.

**A failed read is "unknown", never "healthy".** A locked, missing or
unexpected database ends the round as ``read_failed`` -- it never produces the
conclusion "nothing is wrong".

**The position predicate is the noise filter** (spec 4.1, from the phase 0
production study): most failed management instructions in production target
strategies that never had a position at all -- 35 of 220 in thirty days failed
with ``target_strategy_binding_visibility_retry_expired`` and *none* of them
had an execution binding. Those are not missed operations, and alerting on
them is how an on-call channel becomes noise nobody reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from telegram_kol_research.oncall_state import OncallStateStore


logger = logging.getLogger(__name__)


#: Reduce-risk management actions. ``replace_entry`` / ``cancel_pending_entry``
#: belong to the entry side and are out of phase 1's scope.
RISK_REDUCING_ACTIONS = frozenset(
    {
        "full_exit",
        "partial_take_profit",
        "partial_then_break_even",
        "move_stop_to_break_even",
        "adjust_stop_loss",
    }
)

#: Refusals that are the user's own configuration rather than a fault.
BENIGN_SKIP_REASONS = frozenset(
    {
        "kol_or_group_auto_trade_disabled",
        "symbol_not_allowed",
        "group_not_configured_for_auto_trade",
        "confidence_below_minimum",
    }
)

MANAGEMENT_BATCH_FAULT_STATUSES = frozenset(
    {"blocked", "partial_failed", "recovery_required", "submit_unknown"}
)
#: A plan-only block is the management switch being off, not a failure.
BATCH_BENIGN_BLOCK_REASON = "management_disabled_plan_only"

ITEM_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "unknown"})
ITEM_IN_FLIGHT_STATUSES = frozenset({"pending", "executing", "submitted"})
AWAITING_CONFIRMATION = "awaiting_user_confirmation"

#: What reconcile writes on a binding whose position it saw this round. Same
#: evidence string ``management_target_verification`` trusts.
LIVE_OWNERSHIP_EVIDENCE = "position_ownership_verified"

POSITION_VERIFIED_OPEN = "verified_open"
POSITION_OPEN_SNAPSHOT_STALE = "open_snapshot_stale"
POSITION_ABSENT = "absent"
POSITION_ATTRIBUTION_UNKNOWN = "attribution_unknown"

WATCH_INSTRUCTION_ITEM = "instruction_item"
WATCH_MANAGEMENT_BATCH = "management_batch"
WATCH_PROCESSING_JOB = "processing_job"

COUNTER_SKIPPED_NO_POSITION = "counter:skipped_no_position"
COUNTER_SKIPPED_ATTRIBUTION_UNKNOWN = "counter:skipped_position_attribution_unknown"
COUNTER_READ_FAILED_ROUNDS = "counter:read_failed_rounds"
META_CONSECUTIVE_READ_FAILURES = "consecutive_read_failures"
META_CONSECUTIVE_WORKER_HEALTH_FAILURES = "consecutive_worker_health_failures"

HEALTH_CASE_DB_READ = "health:D5a_database_unreadable"
HEALTH_CASE_WORKER_LOOP = "health:D5b_worker_loop_health"
HEALTH_CASE_STALLED_JOBS = "health:D4_message_processing_stalled"

#: The only three query shapes this module is allowed to send to production.
ALLOWED_QUERY_SHAPES = (
    "watermark: WHERE id > ? ORDER BY id LIMIT n",
    "point: WHERE id = ? / WHERE id IN (?, ...)",
    "bounded indexed lookup: WHERE <indexed column> = ? ... LIMIT n",
)


class ProductionReadError(RuntimeError):
    """The production database could not be read this round.

    Raised for a lock, a missing file or an unexpected schema alike: the
    caller must record "unknown", never "healthy".
    """


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Thresholds from spec 4.2. Times are wall-clock ages of a stuck row."""

    awaiting_confirmation_after: timedelta = timedelta(minutes=10)
    in_flight_after: timedelta = timedelta(minutes=5)
    batch_fault_after: timedelta = timedelta(minutes=2)
    processing_job_stalled_after: timedelta = timedelta(minutes=3)
    case_stale_after: timedelta = timedelta(hours=6)
    #: How recently reconcile must have rewritten a binding for "open" to be a
    #: verified fact rather than an assumption. Matches
    #: ``management_target_verification.DEFAULT_SNAPSHOT_MAX_AGE``.
    position_snapshot_max_age: timedelta = timedelta(minutes=5)
    read_failure_alert_rounds: int = 5
    worker_health_failure_rounds: int = 3
    intake_limit: int = 200
    watch_limit: int = 500


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    read_failed: bool = False
    read_error_type: str | None = None
    new_case_ids: tuple[int, ...] = ()
    resolved_case_ids: tuple[int, ...] = ()
    stale_case_ids: tuple[int, ...] = ()
    skipped_no_position: int = 0
    watched_objects: int = 0


@dataclass(slots=True)
class _Observation:
    """One watched production row's verdict for this round."""

    case_key: str | None
    rule: str | None
    severity: str = "high"
    raw_message_id: int | None = None
    chat_id: int | None = None
    item_ids: tuple[int, ...] = ()
    batch_ids: tuple[int, ...] = ()
    reason_code: str | None = None
    target_uncertain: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    cleared: bool = False
    retire: bool = False


class ProductionReader:
    """A short-lived, structurally read-only view of the production database."""

    def __init__(self, database_path: str | Path, *, timeout: float = 5.0):
        self.database_path = Path(database_path)
        self.uri = f"file:{self.database_path}?mode=ro"
        self.statements: list[str] = []
        try:
            self.connection = sqlite3.connect(self.uri, uri=True, timeout=timeout)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
        except sqlite3.Error as exc:
            raise ProductionReadError(type(exc).__name__) from exc

    def __enter__(self) -> "ProductionReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:  # pragma: no cover - close never matters
            pass

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        self.statements.append(sql)
        try:
            return list(self.connection.execute(sql, tuple(parameters)).fetchall())
        except sqlite3.Error as exc:
            raise ProductionReadError(type(exc).__name__) from exc

    def max_id(self, table: str) -> int:
        """Only used once per table, at first start, to place the watermark."""

        sql = f"SELECT MAX(id) AS max_id FROM {table}"
        self.statements.append(sql)
        try:
            row = self.connection.execute(sql).fetchone()
        except sqlite3.Error as exc:
            raise ProductionReadError(type(exc).__name__) from exc
        return int(row["max_id"]) if row is not None and row["max_id"] is not None else 0

    def read_forward(self, table: str, columns: str, last_id: int, limit: int) -> list[sqlite3.Row]:
        return self.query(
            f"SELECT {columns} FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
            (int(last_id), int(limit)),
        )

    def read_by_ids(self, table: str, columns: str, ids: Sequence[int]) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        ordered = [int(value) for value in ids]
        for start in range(0, len(ordered), 100):
            chunk = ordered[start : start + 100]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self.query(
                    f"SELECT {columns} FROM {table} WHERE id IN ({placeholders})",
                    chunk,
                )
            )
        return rows

    def read_one(self, table: str, columns: str, row_id: int | None) -> sqlite3.Row | None:
        if row_id is None:
            return None
        rows = self.query(
            f"SELECT {columns} FROM {table} WHERE id = ?", (int(row_id),)
        )
        return rows[0] if rows else None


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def as_utc(value: Any) -> datetime | None:
    """Production stores naive UTC; read it back as the UTC it was written as."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _age(now: datetime, moment: datetime | None) -> timedelta | None:
    if moment is None:
        return None
    return now - moment


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# The position predicate (spec 4.1)
# --------------------------------------------------------------------------


def classify_binding(
    row: Mapping[str, Any] | sqlite3.Row | None,
    *,
    now: datetime,
    snapshot_max_age: timedelta,
) -> str:
    """Does this execution binding stand for a position that is open now?

    Aligned with ``auto_trade_execution._load_active_execution_bindings``
    (venue, ``status IN ('open', 'active')``) and tightened with what
    ``management_target_verification`` treats as proof: reconcile rewrites
    every binding each round, so a binding it last confirmed within
    ``snapshot_max_age`` carrying ``position_ownership_verified`` is a
    position the exchange showed open. A binding that still says open but that
    reconcile has not touched recently is *not* evidence of absence -- that is
    exactly the state a frozen worker leaves behind -- so it is reported as
    ``open_snapshot_stale`` and still opens a case.
    """

    if row is None:
        return POSITION_ABSENT
    if str(row["venue"] or "") != "deepcoin":
        return POSITION_ABSENT
    status = str(row["status"] or "").lower()
    pos_id = str(row["pos_id"] or "").strip()
    if not pos_id:
        return POSITION_ABSENT
    if status not in {"open", "active"}:
        # ``unknown`` means reconcile found conflicting attribution evidence.
        # Phase 1 follows the spec and does not open a case for it, but it is
        # counted separately so the daily summary can show whether the silence
        # ever covers something real.
        return POSITION_ATTRIBUTION_UNKNOWN if status == "unknown" else POSITION_ABSENT
    recovered_at = as_utc(row["recovered_at"])
    fresh = recovered_at is not None and (now - recovered_at) <= snapshot_max_age
    if fresh and str(row["last_exchange_status"] or "") == LIVE_OWNERSHIP_EVIDENCE:
        return POSITION_VERIFIED_OPEN
    return POSITION_OPEN_SNAPSHOT_STALE


def binding_indicates_position(state: str) -> bool:
    return state in {POSITION_VERIFIED_OPEN, POSITION_OPEN_SNAPSHOT_STALE}


_BINDING_COLUMNS = (
    "id, strategy_instance_id, chat_id, symbol, side, venue, pos_id, "
    "status, last_exchange_status, recovered_at"
)


@dataclass(frozen=True, slots=True)
class PositionVerdict:
    state: str
    rule: str | None
    binding_id: int | None = None
    symbol: str | None = None
    side: str | None = None
    target_uncertain: bool = False
    group_open_positions: tuple[str, ...] = ()


def resolve_position_state(
    reader: ProductionReader,
    *,
    now: datetime,
    config: DetectorConfig,
    lifecycle_id: int | None,
    strategy_instance_id: str | None,
    chat_id: int | None,
    symbol: str | None,
    side: str | None,
) -> PositionVerdict:
    """Spec 4.1's three tests, in order; the first that holds wins."""

    # 1. The candidate's lifecycle carries an execution binding.
    if lifecycle_id is not None:
        lifecycle = reader.read_one(
            "strategy_lifecycles", "id, execution_binding_id", lifecycle_id
        )
        binding_id = (
            int(lifecycle["execution_binding_id"])
            if lifecycle is not None and lifecycle["execution_binding_id"] is not None
            else None
        )
        if binding_id is not None:
            row = reader.read_one("execution_bindings", _BINDING_COLUMNS, binding_id)
            state = classify_binding(
                row, now=now, snapshot_max_age=config.position_snapshot_max_age
            )
            if binding_indicates_position(state):
                return PositionVerdict(
                    state=state,
                    rule="lifecycle_binding",
                    binding_id=binding_id,
                    symbol=str(row["symbol"]) if row is not None else None,
                    side=str(row["side"]) if row is not None else None,
                )
            if state == POSITION_ATTRIBUTION_UNKNOWN:
                return PositionVerdict(state=state, rule="lifecycle_binding")

    # 2. The instruction item's own strategy instance.
    instance_id = str(strategy_instance_id or "").strip()
    if instance_id:
        rows = reader.query(
            "SELECT " + _BINDING_COLUMNS + " FROM execution_bindings "
            "WHERE strategy_instance_id = ? ORDER BY id DESC LIMIT 20",
            (instance_id,),
        )
        for row in rows:
            state = classify_binding(
                row, now=now, snapshot_max_age=config.position_snapshot_max_age
            )
            if binding_indicates_position(state):
                return PositionVerdict(
                    state=state,
                    rule="strategy_instance_binding",
                    binding_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    side=str(row["side"]),
                )

    # 3. Target undetermined: any matching open position in the same chat.
    if chat_id is not None:
        open_rows = read_chat_open_bindings(
            reader,
            chat_id=chat_id,
            now=now,
            snapshot_max_age=config.position_snapshot_max_age,
        )
        summaries: list[str] = []
        matched: PositionVerdict | None = None
        for row, state in open_rows:
            summaries.append(f"{row['symbol']} {row['side']}")
            if matched is not None:
                continue
            if symbol and str(row["symbol"] or "").upper() != str(symbol).upper():
                continue
            if side and str(row["side"] or "").lower() != str(side).lower():
                continue
            matched = PositionVerdict(
                state=state,
                rule="chat_open_binding",
                binding_id=int(row["id"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                target_uncertain=True,
            )
        if matched is not None:
            return PositionVerdict(
                state=matched.state,
                rule=matched.rule,
                binding_id=matched.binding_id,
                symbol=matched.symbol,
                side=matched.side,
                target_uncertain=True,
                group_open_positions=tuple(dict.fromkeys(summaries))[:8],
            )
        return PositionVerdict(
            state=POSITION_ABSENT,
            rule=None,
            group_open_positions=tuple(dict.fromkeys(summaries))[:8],
        )
    return PositionVerdict(state=POSITION_ABSENT, rule=None)


def read_chat_open_bindings(
    reader: ProductionReader,
    *,
    chat_id: int,
    now: datetime,
    snapshot_max_age: timedelta,
) -> list[tuple[sqlite3.Row, str]]:
    """Bounded, index-seeking read of one chat's currently open bindings."""

    rows = reader.query(
        "SELECT " + _BINDING_COLUMNS + " FROM execution_bindings "
        "WHERE chat_id = ? AND venue = 'deepcoin' AND status IN ('open', 'active') "
        "ORDER BY id DESC LIMIT 50",
        (int(chat_id),),
    )
    result: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        state = classify_binding(row, now=now, snapshot_max_age=snapshot_max_age)
        if binding_indicates_position(state):
            result.append((row, state))
    return result


# --------------------------------------------------------------------------
# Group names
# --------------------------------------------------------------------------


def read_group_name(reader: ProductionReader, *, chat_id: int | None) -> str:
    """The name a person would recognise, or the chat id when there is none.

    ``strategy_alerts`` is the only production table that stores a chat's own
    title; ``sources`` holds the KOL's display name for the same chat, which
    is the next best thing a reader would recognise.
    """

    if chat_id is None:
        return "未知群"
    rows = reader.query(
        "SELECT chat_title FROM strategy_alerts WHERE chat_id = ? "
        "ORDER BY message_id DESC LIMIT 1",
        (int(chat_id),),
    )
    if rows and str(rows[0]["chat_title"] or "").strip():
        return str(rows[0]["chat_title"]).strip()
    rows = reader.query(
        "SELECT custom_label, display_name FROM sources WHERE chat_id = ? "
        "ORDER BY id LIMIT 1",
        (int(chat_id),),
    )
    if rows:
        label = str(rows[0]["custom_label"] or "").strip()
        if label:
            return label
        display = str(rows[0]["display_name"] or "").strip()
        if display:
            return display
    return str(chat_id)


# --------------------------------------------------------------------------
# The round
# --------------------------------------------------------------------------

_ITEM_COLUMNS = (
    "id, raw_message_id, signal_candidate_id, instruction_kind, "
    "strategy_instance_id, status, result_json, error_json, retired_at, "
    "created_at, updated_at"
)
_BATCH_COLUMNS = (
    "id, raw_message_id, target_lifecycle_id, strategy_instance_id, "
    "execution_binding_id, intent, effective_action, status, reason_code, "
    "planned_at, updated_at"
)
_JOB_COLUMNS = "id, raw_message_id, chat_id, status, enqueued_at"
_CANDIDATE_COLUMNS = (
    "id, raw_message_id, symbol, side, target_lifecycle_id, management_action, "
    "management_fraction, stop_loss_text, take_profit_text, entry_text"
)
_RAW_MESSAGE_COLUMNS = "id, chat_id, message_id, sender_name, posted_at, text"

_WATERMARK_TABLES = (
    ("message_instruction_items", WATCH_INSTRUCTION_ITEM),
    ("strategy_management_batches", WATCH_MANAGEMENT_BATCH),
    ("message_processing_jobs", WATCH_PROCESSING_JOB),
)


def run_detection_round(
    *,
    reader_factory: Callable[[], ProductionReader],
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig | None = None,
    worker_health_probe: Callable[[], bool] | None = None,
) -> RoundOutcome:
    """One detection pass. Never raises for a production read failure."""

    settings = config or DetectorConfig()
    new_cases: list[int] = []
    resolved: list[int] = []
    skipped_no_position = 0
    watched = 0

    try:
        reader = reader_factory()
    except ProductionReadError as exc:
        return _record_read_failure(store, now, settings, str(exc))

    try:
        with reader:
            _initialise_watermarks(reader, store)
            _intake(reader, store, now, settings)
            observations = _recheck(reader, store, now, settings)
            watched = len(observations)
            group_names: dict[int, str] = {}
            hits_by_key: dict[str, list[_Observation]] = {}
            clears_by_key: dict[str, list[_Observation]] = {}
            for observation in observations:
                if observation.case_key is None:
                    continue
                bucket = clears_by_key if observation.cleared else hits_by_key
                bucket.setdefault(observation.case_key, []).append(observation)
            skipped_no_position = sum(
                1 for observation in observations if observation.reason_code == "__no_position__"
            )
            attribution_unknown = sum(
                1
                for observation in observations
                if observation.reason_code == "__attribution_unknown__"
            )
            if attribution_unknown:
                store.bump_counter(
                    COUNTER_SKIPPED_ATTRIBUTION_UNKNOWN, attribution_unknown
                )
            for case_key, hits in hits_by_key.items():
                merged = _merge_observations(hits)
                if merged.chat_id is not None and merged.chat_id not in group_names:
                    group_names[merged.chat_id] = read_group_name(
                        reader, chat_id=merged.chat_id
                    )
                evidence = dict(merged.evidence)
                if merged.chat_id is not None:
                    evidence.setdefault("group_name", group_names[merged.chat_id])
                case, created = store.upsert_case(
                    case_key=case_key,
                    rule=merged.rule or "D1",
                    severity=merged.severity,
                    now=now,
                    raw_message_id=merged.raw_message_id,
                    chat_id=merged.chat_id,
                    item_ids=merged.item_ids,
                    batch_ids=merged.batch_ids,
                    reason_code=merged.reason_code,
                    target_uncertain=merged.target_uncertain,
                    evidence=evidence,
                )
                if created:
                    new_cases.append(case.id)
            for case_key in clears_by_key:
                if case_key in hits_by_key:
                    continue
                case = store.get_case_by_key(case_key)
                if case is not None and case.status == "open":
                    store.resolve_case(case.id, now)
                    resolved.append(case.id)
            resolved.extend(_evaluate_stalled_jobs(reader, store, now, settings, new_cases))
            store.set_meta(META_CONSECUTIVE_READ_FAILURES, "0")
            _resolve_health_case(store, HEALTH_CASE_DB_READ, now, resolved)
    except ProductionReadError as exc:
        return _record_read_failure(store, now, settings, str(exc))
    except sqlite3.Error as exc:  # pragma: no cover - the reader wraps these
        return _record_read_failure(store, now, settings, type(exc).__name__)

    if worker_health_probe is not None:
        _evaluate_worker_health(
            store, now, settings, worker_health_probe, new_cases, resolved
        )

    stale_ids = _mark_stale_cases(store, now, settings)
    if skipped_no_position:
        store.bump_counter(COUNTER_SKIPPED_NO_POSITION, skipped_no_position)
    return RoundOutcome(
        read_failed=False,
        new_case_ids=tuple(new_cases),
        resolved_case_ids=tuple(dict.fromkeys(resolved)),
        stale_case_ids=tuple(stale_ids),
        skipped_no_position=skipped_no_position,
        watched_objects=watched,
    )


def _record_read_failure(
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    error_type: str,
) -> RoundOutcome:
    """A read that failed is unknown. It is never "nothing is wrong"."""

    store.bump_counter(COUNTER_READ_FAILED_ROUNDS)
    failures = store.get_int_meta(META_CONSECUTIVE_READ_FAILURES, 0) + 1
    store.set_meta(META_CONSECUTIVE_READ_FAILURES, str(failures))
    logger.warning(
        "oncall production read failed rounds=%s error_type=%s", failures, error_type
    )
    new_cases: list[int] = []
    if failures >= config.read_failure_alert_rounds:
        case, created = store.upsert_case(
            case_key=HEALTH_CASE_DB_READ,
            rule="D5a",
            severity="high",
            now=now,
            evidence={
                "consecutive_failures": failures,
                "error_type": error_type,
            },
            reopen=True,
        )
        if created:
            new_cases.append(case.id)
    return RoundOutcome(
        read_failed=True,
        read_error_type=error_type,
        new_case_ids=tuple(new_cases),
    )


def _initialise_watermarks(reader: ProductionReader, store: OncallStateStore) -> None:
    """First start never replays history: each watermark starts at max(id)."""

    for table, _kind in _WATERMARK_TABLES:
        if store.get_watermark(table) is None:
            store.set_meta(f"watermark:{table}", str(reader.max_id(table)))


def _intake(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> None:
    """Read each table forward by watermark and remember what to re-check."""

    last_item = store.get_watermark("message_instruction_items") or 0
    rows = reader.read_forward(
        "message_instruction_items", _ITEM_COLUMNS, last_item, config.intake_limit
    )
    for row in rows:
        if str(row["instruction_kind"] or "") == "management":
            store.add_watch_item(
                kind=WATCH_INSTRUCTION_ITEM,
                object_id=int(row["id"]),
                chat_id=None,
                now=now,
            )
    if rows:
        store.set_watermark("message_instruction_items", int(rows[-1]["id"]))

    last_batch = store.get_watermark("strategy_management_batches") or 0
    rows = reader.read_forward(
        "strategy_management_batches", _BATCH_COLUMNS, last_batch, config.intake_limit
    )
    for row in rows:
        store.add_watch_item(
            kind=WATCH_MANAGEMENT_BATCH,
            object_id=int(row["id"]),
            chat_id=None,
            now=now,
        )
    if rows:
        store.set_watermark("strategy_management_batches", int(rows[-1]["id"]))

    last_job = store.get_watermark("message_processing_jobs") or 0
    rows = reader.read_forward(
        "message_processing_jobs", _JOB_COLUMNS, last_job, config.intake_limit
    )
    for row in rows:
        store.add_watch_item(
            kind=WATCH_PROCESSING_JOB,
            object_id=int(row["id"]),
            chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
            now=now,
        )
    if rows:
        store.set_watermark("message_processing_jobs", int(rows[-1]["id"]))


def _recheck(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> list[_Observation]:
    observations: list[_Observation] = []
    observations.extend(_recheck_instruction_items(reader, store, now, config))
    observations.extend(_recheck_management_batches(reader, store, now, config))
    return observations


def _expired_watch_ids(
    watch_items: Iterable[Any], now: datetime, config: DetectorConfig
) -> set[int]:
    expired: set[int] = set()
    for item in watch_items:
        if item.first_seen_at is not None and (now - item.first_seen_at) > config.case_stale_after:
            expired.add(item.object_id)
    return expired


def _recheck_instruction_items(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> list[_Observation]:
    watch_items = store.open_watch_items(WATCH_INSTRUCTION_ITEM, config.watch_limit)
    if not watch_items:
        return []
    expired = _expired_watch_ids(watch_items, now, config)
    ids = [item.object_id for item in watch_items if item.object_id not in expired]
    store.retire_watch_items(WATCH_INSTRUCTION_ITEM, expired)
    if not ids:
        return []
    rows = reader.read_by_ids("message_instruction_items", _ITEM_COLUMNS, ids)
    store.touch_watch_items(WATCH_INSTRUCTION_ITEM, ids, now)
    found = {int(row["id"]) for row in rows}
    store.retire_watch_items(
        WATCH_INSTRUCTION_ITEM, [value for value in ids if value not in found]
    )
    observations: list[_Observation] = []
    retire: list[int] = []
    for row in rows:
        observation = _evaluate_instruction_item(reader, row, now=now, config=config)
        if observation is None:
            retire.append(int(row["id"]))
            continue
        if observation.retire:
            retire.append(int(row["id"]))
        observations.append(observation)
    store.retire_watch_items(WATCH_INSTRUCTION_ITEM, retire)
    return observations


def _evaluate_instruction_item(
    reader: ProductionReader,
    row: sqlite3.Row,
    *,
    now: datetime,
    config: DetectorConfig,
) -> _Observation | None:
    """Rules D1a-D1d for one management instruction item."""

    if row["retired_at"] is not None:
        return None
    candidate = reader.read_one(
        "signal_candidates", _CANDIDATE_COLUMNS, row["signal_candidate_id"]
    )
    action = str(candidate["management_action"] or "").strip() if candidate else ""
    if action not in RISK_REDUCING_ACTIONS:
        return None

    status = str(row["status"] or "").lower()
    result = _json_object(row["result_json"])
    error = _json_object(row["error_json"])
    updated_at = as_utc(row["updated_at"]) or as_utc(row["created_at"])

    rule: str | None = None
    severity = "high"
    reason_code: str | None = None
    terminal_clear = False

    if status in {"failed", "unknown"}:
        rule = "D1a"
        reason_code = str(error.get("reason") or result.get("reason") or status)
    elif status == "succeeded":
        result_status = str(result.get("status") or "").lower()
        if result_status in {"skipped", "shadow_planned"}:
            reason = str(result.get("reason") or result_status)
            if reason in BENIGN_SKIP_REASONS:
                return _clear_observation(row, candidate)
            rule = "D1b"
            reason_code = reason
        else:
            terminal_clear = True
    elif status == AWAITING_CONFIRMATION:
        age = _age(now, updated_at)
        if age is None or age < config.awaiting_confirmation_after:
            return _Observation(case_key=None, rule=None)
        rule = "D1c"
        reason_code = str(
            result.get("confirmation_reason_code") or "confirmation_timeout"
        )
    elif status in ITEM_IN_FLIGHT_STATUSES:
        age = _age(now, updated_at)
        if age is None or age < config.in_flight_after:
            return _Observation(case_key=None, rule=None)
        rule = "D1d"
        severity = "medium"
        reason_code = f"instruction_stuck_{status}"
    else:
        return _Observation(case_key=None, rule=None)

    if terminal_clear:
        return _clear_observation(row, candidate)

    raw_message = reader.read_one(
        "raw_messages", _RAW_MESSAGE_COLUMNS, row["raw_message_id"]
    )
    chat_id = int(raw_message["chat_id"]) if raw_message is not None else None
    lifecycle_id = (
        int(candidate["target_lifecycle_id"])
        if candidate is not None and candidate["target_lifecycle_id"] is not None
        else None
    )
    verdict = resolve_position_state(
        reader,
        now=now,
        config=config,
        lifecycle_id=lifecycle_id,
        strategy_instance_id=row["strategy_instance_id"],
        chat_id=chat_id,
        symbol=candidate["symbol"] if candidate is not None else None,
        side=candidate["side"] if candidate is not None else None,
    )
    if not binding_indicates_position(verdict.state):
        # The loudest noise source in production: an instruction whose target
        # never had a position. No case, no alert, just a counter.
        return _Observation(
            case_key=None,
            rule=None,
            reason_code=(
                "__attribution_unknown__"
                if verdict.state == POSITION_ATTRIBUTION_UNKNOWN
                else "__no_position__"
            ),
            retire=True,
        )

    evidence = _build_case_evidence(
        raw_message=raw_message,
        candidate=candidate,
        action=action,
        reason_code=reason_code,
        verdict=verdict,
        now=now,
        item_status=status,
    )
    return _Observation(
        case_key=_management_case_key(row["raw_message_id"], action),
        rule=rule,
        severity=severity,
        raw_message_id=int(row["raw_message_id"]),
        chat_id=chat_id,
        item_ids=(int(row["id"]),),
        reason_code=reason_code,
        target_uncertain=verdict.target_uncertain,
        evidence=evidence,
        # A case stays under observation even when the item is terminal: the
        # spec asks for a "recovered by itself" notice when a later run makes
        # it succeed. The six-hour watch expiry is what ends it.
        retire=False,
    )


def _clear_observation(
    row: sqlite3.Row,
    candidate: sqlite3.Row | None,
) -> _Observation:
    action = str(candidate["management_action"] or "").strip() if candidate else ""
    return _Observation(
        case_key=_management_case_key(row["raw_message_id"], action),
        rule=None,
        cleared=True,
        item_ids=(int(row["id"]),),
        raw_message_id=int(row["raw_message_id"]),
        retire=True,
    )


def _recheck_management_batches(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> list[_Observation]:
    watch_items = store.open_watch_items(WATCH_MANAGEMENT_BATCH, config.watch_limit)
    if not watch_items:
        return []
    expired = _expired_watch_ids(watch_items, now, config)
    ids = [item.object_id for item in watch_items if item.object_id not in expired]
    store.retire_watch_items(WATCH_MANAGEMENT_BATCH, expired)
    if not ids:
        return []
    rows = reader.read_by_ids("strategy_management_batches", _BATCH_COLUMNS, ids)
    store.touch_watch_items(WATCH_MANAGEMENT_BATCH, ids, now)
    found = {int(row["id"]) for row in rows}
    store.retire_watch_items(
        WATCH_MANAGEMENT_BATCH, [value for value in ids if value not in found]
    )
    observations: list[_Observation] = []
    retire: list[int] = []
    for row in rows:
        observation = _evaluate_management_batch(reader, row, now=now, config=config)
        if observation is None:
            retire.append(int(row["id"]))
            continue
        if observation.retire:
            retire.append(int(row["id"]))
        if observation.case_key is not None:
            observations.append(observation)
    store.retire_watch_items(WATCH_MANAGEMENT_BATCH, retire)
    return observations


def _evaluate_management_batch(
    reader: ProductionReader,
    row: sqlite3.Row,
    *,
    now: datetime,
    config: DetectorConfig,
) -> _Observation | None:
    """Rule D2. A batch exists only for a position, so 4.1 is not re-run."""

    status = str(row["status"] or "").lower()
    # ``intent`` is the instruction's own vocabulary (what the candidate's
    # ``management_action`` says, and therefore what rule D1 keys on);
    # ``effective_action`` is the planner's exchange verb (``partial_close`` for
    # a ``partial_then_break_even``). Keying on the verb split one message into
    # two cases in production on 2026-09-20 (raw 17813), so the intent wins.
    action = str(row["intent"] or row["effective_action"] or "").strip()
    case_key = _management_case_key(row["raw_message_id"], action)
    if status in {"succeeded", "resolved"}:
        return _Observation(
            case_key=case_key,
            rule=None,
            cleared=True,
            batch_ids=(int(row["id"]),),
            raw_message_id=int(row["raw_message_id"]),
            retire=True,
        )
    if status not in MANAGEMENT_BATCH_FAULT_STATUSES:
        return _Observation(case_key=None, rule=None)
    reason_code = str(row["reason_code"] or "")
    if status == "blocked" and reason_code == BATCH_BENIGN_BLOCK_REASON:
        return _Observation(case_key=None, rule=None, retire=True)
    stuck_since = as_utc(row["updated_at"]) or as_utc(row["planned_at"])
    age = _age(now, stuck_since)
    if age is None or age < config.batch_fault_after:
        return _Observation(case_key=None, rule=None)

    raw_message = reader.read_one(
        "raw_messages", _RAW_MESSAGE_COLUMNS, row["raw_message_id"]
    )
    candidate = None
    chat_id = int(raw_message["chat_id"]) if raw_message is not None else None
    binding = reader.read_one(
        "execution_bindings", _BINDING_COLUMNS, row["execution_binding_id"]
    )
    verdict = PositionVerdict(
        state=classify_binding(
            binding, now=now, snapshot_max_age=config.position_snapshot_max_age
        ),
        rule="batch_binding",
        binding_id=(
            int(row["execution_binding_id"])
            if row["execution_binding_id"] is not None
            else None
        ),
        symbol=str(binding["symbol"]) if binding is not None else None,
        side=str(binding["side"]) if binding is not None else None,
    )
    evidence = _build_case_evidence(
        raw_message=raw_message,
        candidate=candidate,
        action=action,
        reason_code=reason_code or f"batch_{status}",
        verdict=verdict,
        now=now,
        item_status=status,
    )
    evidence["batch_status"] = status
    return _Observation(
        case_key=case_key,
        rule="D2",
        severity="high",
        raw_message_id=int(row["raw_message_id"]),
        chat_id=chat_id,
        batch_ids=(int(row["id"]),),
        reason_code=reason_code or f"batch_{status}",
        evidence=evidence,
    )


def _evaluate_stalled_jobs(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    new_cases: list[int],
) -> list[int]:
    """Rule D4: message processing is not draining its own queue."""

    watch_items = store.open_watch_items(WATCH_PROCESSING_JOB, config.watch_limit)
    resolved: list[int] = []
    if not watch_items:
        _resolve_health_case(store, HEALTH_CASE_STALLED_JOBS, now, resolved)
        return resolved
    ids = [item.object_id for item in watch_items]
    rows = reader.read_by_ids("message_processing_jobs", _JOB_COLUMNS, ids)
    found = {int(row["id"]) for row in rows}
    store.retire_watch_items(
        WATCH_PROCESSING_JOB, [value for value in ids if value not in found]
    )
    stalled: list[sqlite3.Row] = []
    settled: list[int] = []
    for row in rows:
        status = str(row["status"] or "").lower()
        if status not in {"pending", "claimed"}:
            settled.append(int(row["id"]))
            continue
        age = _age(now, as_utc(row["enqueued_at"]))
        if age is not None and age >= config.processing_job_stalled_after:
            stalled.append(row)
    store.retire_watch_items(WATCH_PROCESSING_JOB, settled)
    if stalled:
        oldest = min(stalled, key=lambda row: str(row["enqueued_at"] or ""))
        case, created = store.upsert_case(
            case_key=HEALTH_CASE_STALLED_JOBS,
            rule="D4",
            severity="high",
            now=now,
            chat_id=int(oldest["chat_id"]) if oldest["chat_id"] is not None else None,
            evidence={
                "stalled_jobs": len(stalled),
                "oldest_job_id": int(oldest["id"]),
                "oldest_raw_message_id": int(oldest["raw_message_id"]),
                "oldest_enqueued_at": str(oldest["enqueued_at"]),
                "minutes": _minutes(_age(now, as_utc(oldest["enqueued_at"]))),
            },
            reopen=True,
        )
        if created:
            new_cases.append(case.id)
    else:
        _resolve_health_case(store, HEALTH_CASE_STALLED_JOBS, now, resolved)
    return resolved


def _evaluate_worker_health(
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    probe: Callable[[], bool],
    new_cases: list[int],
    resolved: list[int],
) -> None:
    """Rule D5b. A probe that raises counts as a failure, never as health."""

    try:
        healthy = bool(probe())
    except Exception:  # noqa: BLE001 - any probe failure is "unknown"
        logger.warning("oncall worker health probe failed", exc_info=True)
        healthy = False
    if healthy:
        store.set_meta(META_CONSECUTIVE_WORKER_HEALTH_FAILURES, "0")
        _resolve_health_case(store, HEALTH_CASE_WORKER_LOOP, now, resolved)
        return
    failures = store.get_int_meta(META_CONSECUTIVE_WORKER_HEALTH_FAILURES, 0) + 1
    store.set_meta(META_CONSECUTIVE_WORKER_HEALTH_FAILURES, str(failures))
    if failures < config.worker_health_failure_rounds:
        return
    case, created = store.upsert_case(
        case_key=HEALTH_CASE_WORKER_LOOP,
        rule="D5b",
        severity="high",
        now=now,
        evidence={"consecutive_failures": failures},
        reopen=True,
    )
    if created:
        new_cases.append(case.id)


def _resolve_health_case(
    store: OncallStateStore, case_key: str, now: datetime, resolved: list[int]
) -> None:
    case = store.get_case_by_key(case_key)
    if case is not None and case.status == "open":
        store.resolve_case(case.id, now)
        resolved.append(case.id)


def _mark_stale_cases(
    store: OncallStateStore, now: datetime, config: DetectorConfig
) -> list[int]:
    stale: list[int] = []
    for case in store.open_cases():
        first_seen = case.first_seen_at
        if first_seen is None or (now - first_seen) <= config.case_stale_after:
            continue
        store.mark_case_stale(case.id, now)
        stale.append(case.id)
    return stale


def _merge_observations(observations: Sequence[_Observation]) -> _Observation:
    merged = _Observation(
        case_key=observations[0].case_key,
        rule=observations[0].rule,
        severity=observations[0].severity,
        raw_message_id=observations[0].raw_message_id,
        chat_id=observations[0].chat_id,
        evidence=dict(observations[0].evidence),
        reason_code=observations[0].reason_code,
    )
    item_ids: list[int] = []
    batch_ids: list[int] = []
    rules: list[str] = []
    for observation in observations:
        item_ids.extend(observation.item_ids)
        batch_ids.extend(observation.batch_ids)
        if observation.rule and observation.rule not in rules:
            rules.append(observation.rule)
        merged.target_uncertain = merged.target_uncertain or observation.target_uncertain
        if observation.chat_id is not None:
            merged.chat_id = observation.chat_id
        if observation.raw_message_id is not None:
            merged.raw_message_id = observation.raw_message_id
        for key, value in observation.evidence.items():
            merged.evidence.setdefault(key, value)
        if merged.reason_code is None:
            merged.reason_code = observation.reason_code
        merged.severity = (
            "high"
            if "high" in {merged.severity, observation.severity}
            else observation.severity
        )
    merged.item_ids = tuple(sorted(set(item_ids)))
    merged.batch_ids = tuple(sorted(set(batch_ids)))
    merged.rule = "+".join(sorted(rules)) if rules else merged.rule
    merged.evidence["rules"] = sorted(rules)
    return merged


def _management_case_key(raw_message_id: Any, action: str) -> str:
    return f"mgmt:{int(raw_message_id)}:{action or 'unknown'}"


def _minutes(delta: timedelta | None) -> int | None:
    if delta is None:
        return None
    return max(0, int(delta.total_seconds() // 60))


def _build_case_evidence(
    *,
    raw_message: sqlite3.Row | None,
    candidate: sqlite3.Row | None,
    action: str,
    reason_code: str | None,
    verdict: PositionVerdict,
    now: datetime,
    item_status: str,
) -> dict[str, Any]:
    """Everything the alert text needs, captured once, bounded to 8 KB.

    The message excerpt is untrusted external text. It is stored verbatim but
    bounded, and the formatter is the only place that decides how to show it.
    """

    posted_at = as_utc(raw_message["posted_at"]) if raw_message is not None else None
    return {
        "action": action,
        "reason_code": reason_code,
        "item_status": item_status,
        "symbol": (
            str(candidate["symbol"] or "")
            if candidate is not None and candidate["symbol"]
            else (verdict.symbol or "")
        ),
        "side": (
            str(candidate["side"] or "")
            if candidate is not None and candidate["side"]
            else (verdict.side or "")
        ),
        "stop_loss_text": (
            str(candidate["stop_loss_text"] or "")[:64] if candidate is not None else ""
        ),
        "message_id": int(raw_message["message_id"]) if raw_message is not None else None,
        "posted_at": posted_at.isoformat() if posted_at is not None else None,
        "message_text": (
            str(raw_message["text"] or "")[:400] if raw_message is not None else ""
        ),
        "minutes_since_message": _minutes(_age(now, posted_at)),
        "position_state": verdict.state,
        "position_rule": verdict.rule,
        "execution_binding_id": verdict.binding_id,
        "target_uncertain": verdict.target_uncertain,
        "group_open_positions": list(verdict.group_open_positions),
    }
