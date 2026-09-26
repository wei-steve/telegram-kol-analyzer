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
:data:`ALLOWED_QUERY_SHAPES` names every shape allowed, and a test asserts
every statement this module runs matches one of them. Rules D6a and D6c add
two bounded sweeps that seek on a named index instead of a watermark, and a
second test reads the query planner's own answer for each of them -- a
statement that *looks* bounded over an unindexed column is exactly the scan
this module exists to avoid, and the shape regexes cannot tell the difference.

**A failed read is "unknown", never "healthy".** A locked, missing or
unexpected database ends the round as ``read_failed`` -- it never produces the
conclusion "nothing is wrong".

**The position predicate is the noise filter** (spec 4.1, from the phase 0
production study): most failed management instructions in production target
strategies that never had a position at all -- 35 of 220 in thirty days failed
with ``target_strategy_binding_visibility_retry_expired`` and *none* of them
had an execution binding. Those are not missed operations, and alerting on
them is how an on-call channel becomes noise nobody reads.

**Rules D6a/D6b/D6c** (``docs/plans/2026-09-26-oncall-d6-silent-stall-rules-design.md``)
answer three questions the first five rules could not: is a lane still sealed
by a source-deletion exit, did the system void a real instruction by itself,
and is an alarm still ringing that nobody has been told about. All three come
from one production case where three layers stayed silent for eleven days --
``docs/2026-09-26-silent-stall-case-note.md``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from telegram_kol_research.oncall_state import (
    LANE_STALL_ACTIVE,
    LANE_STALL_CHURNING,
    LANE_STALL_UNCLAIMABLE,
    RECOGNITION_CASE_PREFIX,
    SEALED_LANE_CASE_PREFIX,
    UNHEARD_INCIDENT_CASE_PREFIX,
    VOIDED_MESSAGE_CASE_PREFIX,
    OncallStateStore,
)


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

#: Rule D3 (design 4.1). The automation reasons that mean a real message was
#: *lost* rather than deliberately passed over. Four of them are named by
#: ``recognition_failure_attribution`` -- this module may not import it (the
#: architecture boundary allows no application import but ``oncall_state``), so
#: the strings are repeated here and a test pins them to that module's values.
#:
#: ``management_recognition_unresolved`` is the fifth the design names ("上下文
#: unresolved / exhausted"). In this codebase that string is currently a
#: *runtime incident type*, not an automation reason, and the context failure
#: that does reach a decision row arrives as ``agreement_status =
#: 'authoritative_failed'`` (``authoritative_recognition`` rewrites the result
#: to 识别失败 with "context resolution failed"). It is listed anyway so that a
#: decision row that ever does carry it is not missed.
LOSSY_RECOGNITION_REASONS = frozenset(
    {
        "target_not_verifiable",
        "mimo_authoritative_failed",
        "authoritative_gap_recovery_expired",
        "lifecycle_apply_failed",
        "management_recognition_unresolved",
    }
)

#: Rule D6b (design 2026-09-26, section 2). The terminal automation reason a
#: deferral gets when it outlives ``deferred_resume_timeout_minutes``: the
#: system decided by itself that a real KOL instruction will never run.
#:
#: **It is deliberately not in** :data:`LOSSY_RECOGNITION_REASONS`. D3 applies
#: a second filter after the lossy test -- the group must be holding a position
#: (:func:`read_chat_open_bindings`) -- and what D6b exists to catch is an
#: *entry* signal being eaten, where by definition there is no position yet.
#: Four of 陈哥's expired messages on 2026-09-15..25 were entries, and every one
#: of them would have been dropped by that filter. So D6b shares the intake but
#: not the verdict.
DEFERRED_EXPIRED_REASON = "deferred_expired"
#: The non-terminal twin: the message is *still waiting* behind a deletion
#: exit and may yet be resumed and executed normally. Never a case -- but it
#: must not be retired from the watch list either, or the row would be
#: forgotten before it can become :data:`DEFERRED_EXPIRED_REASON`.
DEFERRED_HOLD_REASON = "waiting_source_deletion_exit"

#: Rule D6a. The one source-deletion exit state that hangs about indefinitely
#: while still sealing a lane: the deletion worker's active states do not
#: include it, so nothing ever claims such a row again. The system's own
#: timeout sweep (``source_deletion_exit_timeout.STUCK_STATE``, the same word)
#: is the only automation left that can release it.
SEALED_LANE_STUCK_STATE = "recovery_required"
#: Rule D6a, extended 2026-09-26. The deletion worker's own active states,
#: copied from ``source_message_deletion_worker._ACTIVE_STATES`` (the same four
#: are spelled out a second time in ``historical_state_repair``). A row in one
#: of these is claimable and should be through the whole exit in seconds -- but
#: ``source_execution_barrier`` shuts the lane on ``state != 'succeeded'``, so
#: while one of them stands still the lane is exactly as shut as a stuck one,
#: and **nothing sweeps these**: the timeout sweep only looks at
#: :data:`SEALED_LANE_STUCK_STATE`.
SEALED_LANE_ACTIVE_STATES = (
    "pending",
    "cancelling_entries",
    "closing_positions",
    "reconciling",
)
#: Every state that shuts a lane, which is every state except two.
#: ``succeeded`` is the one the barrier lets through, and ``unbound`` has a NULL
#: ``raw_message_id``, so the barrier's first inner join never reaches the row
#: (:attr:`SealedLane.seals_a_lane` drops it a second time anyway).
#:
#: ``waiting`` is deliberately absent, and is not a state at all:
#: ``source_message_deletion_worker`` uses that word as a key in its ``counts``
#: dictionary for a round that ended without a verdict, and writes
#: ``reconciling`` to the row itself. Re-checked before this list was widened.
SEALED_LANE_SEALING_STATES = SEALED_LANE_ACTIVE_STATES + (SEALED_LANE_STUCK_STATE,)
#: The only state ``source_execution_barrier`` treats as "lane open again".
SEALED_LANE_RELEASED_STATE = "succeeded"

#: Rule D6c. ``runtime_incidents.status`` while nobody has picked the incident
#: up, and the severities worth waking somebody for.
INCIDENT_PENDING_STATUS = "pending"
INCIDENT_LOUD_SEVERITIES = frozenset({"high", "critical"})

#: D6a's horizon, and the only one: all three classes of sealed lane use it, and
#: both of the rule's age tests (``updated_at`` for "nothing has happened" and
#: ``created_at`` for "it has never finished") are measured against this one
#: number rather than two. For
#: :data:`SEALED_LANE_STUCK_STATE` the system's own sweep releases the exit
#: after ``source_deletion_exit_timeout_minutes`` (120 in production), so the
#: watch must sit *later* than that self-healing window or it would page for
#: something about to be fixed. For :data:`SEALED_LANE_ACTIVE_STATES` there is
#: no sweep to wait for and the work is seconds long, so six hours is far past
#: generous -- which is the point: one bar, no second number to keep in step,
#: and it is the same bar as ``case_stale_after``.
SEALED_LANE_STUCK_AFTER = timedelta(hours=6)
#: D6c's "still happening" window. An incident whose ``last_occurred_at`` has
#: not moved inside this window has stopped, and a stopped alarm needs nobody.
#: This is the criterion on purpose, rather than a large ``repeat_count``:
#: that number only says the failure happened a lot in the past.
INCIDENT_STILL_OCCURRING_WITHIN = timedelta(hours=1)
#: D6c's "nobody has heard" window. ``runtime_incidents`` coalesces by
#: fingerprint and notifies once, so ``notified_at`` can sit weeks behind a
#: failure that is still happening every five seconds.
INCIDENT_NOTIFICATION_SILENCE = timedelta(days=3)

#: The agreement status that means no authoritative decision was produced.
RECOGNITION_FAILED_STATUS = "authoritative_failed"

#: ``agreement_status`` while the pipeline has not finished with the message.
RECOGNITION_PENDING_STATUS = "pending"

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
WATCH_RECOGNITION_DECISION = "recognition_decision"
#: Stop ladder phase 1. Take-profit ledger rows are watched only so that
#: "the orders say stage N, the shadow says stage N-1" can be **counted**.
WATCH_PROTECTION_LEDGER = "protection_ledger"

COUNTER_SKIPPED_NO_POSITION = "counter:skipped_no_position"
COUNTER_SKIPPED_ATTRIBUTION_UNKNOWN = "counter:skipped_position_attribution_unknown"
COUNTER_READ_FAILED_ROUNDS = "counter:read_failed_rounds"
#: The one ladder observable, and it is a counter on purpose. The account
#: owner ruled out alerting on it: a take profit whose level the shadow has
#: not recorded is a gap in an observation, not a position at risk -- the
#: position keeps the stop it already has either way.
COUNTER_STOP_LADDER_LEVEL_UNRECORDED = "counter:stop_ladder_level_unrecorded"
META_CONSECUTIVE_READ_FAILURES = "consecutive_read_failures"
META_CONSECUTIVE_WORKER_HEALTH_FAILURES = "consecutive_worker_health_failures"

# D6a/D6b/D6c reason codes. The watcher's own vocabulary, except
# :data:`DEFERRED_EXPIRED_REASON`, which is the pipeline's own spelling and is
# reused verbatim so the alert and the database row say the same word. D6a has
# two of them, because one shut lane has two possible causes and they need two
# different people.

#: The lane is shut and the worker will never claim the exit again
#: (:data:`SEALED_LANE_STUCK_STATE`). Unchanged since 2026-09-26 so that cases
#: already open in production keep the code they were filed under.
REASON_SEALED_LANE = "source_deletion_exit_sealed_lane"
#: The lane is shut and the exit is still one the worker *can* claim
#: (:data:`SEALED_LANE_ACTIVE_STATES`). Same rule, same severity, same case-key
#: namespace -- a different cause, and a different person can fix it, so it
#: gets its own code rather than its own rule number.
REASON_STALLED_LANE = "source_deletion_exit_stalled_lane"
#: The lane is shut, the exit is active, and it is *moving* -- claimed again and
#: again, every time landing back in the same active state -- and yet it was
#: created more than :data:`SEALED_LANE_STUCK_AFTER` ago and has still not
#: finished. This is the third cause, added 2026-09-26. It needs its own code
#: because the other two both describe neglect and this one is its opposite:
#: somebody (the worker) is working on it constantly and getting nowhere, and
#: ``attempt_count`` is the evidence that says so.
REASON_CHURNING_LANE = "source_deletion_exit_churning_lane"
REASON_INCIDENT_NEVER_NOTIFIED = "runtime_incident_never_notified"
REASON_INCIDENT_NOTIFICATION_STALE = "runtime_incident_notification_stale"

HEALTH_CASE_DB_READ = "health:D5a_database_unreadable"
HEALTH_CASE_WORKER_LOOP = "health:D5b_worker_loop_health"
HEALTH_CASE_STALLED_JOBS = "health:D4_message_processing_stalled"

#: The only query shapes this module is allowed to send to production. The
#: stop-ladder read is registered here as its own line rather than folded into
#: the generic bounded lookup: ``execution_events`` is a large table and the
#: index this relies on (``ix_execution_events_pos``) is the whole reason the
#: read is affordable, so naming it is what keeps a later change honest.
ALLOWED_QUERY_SHAPES = (
    "watermark: WHERE id > ? ORDER BY id LIMIT n",
    "point: WHERE id = ? / WHERE id IN (?, ...)",
    "bounded indexed lookup: WHERE <indexed column> = ? ... LIMIT n",
    "stop ladder: SELECT id, action, after_json FROM execution_events "
    "WHERE pos_id = ? ORDER BY id DESC LIMIT 20",
    # D3 asks whether a message already produced management work, and D1d asks
    # whether its batch succeeded. Both are index-seeking on raw_message_id,
    # which carries its own index on both tables.
    "message scope: WHERE raw_message_id = ? ORDER BY id [DESC] LIMIT n",
    # D6a's sweep. ``ix_source_message_deletion_exits_state`` is (state,
    # updated_at), and SQLite turns the ``IN`` list into one index seek per
    # listed state -- ``EXPLAIN QUERY PLAN`` reports the same
    # ``SEARCH ... USING INDEX ... (state=? AND updated_at<?)`` as the
    # single-state spelling did, which is why the five states are one statement
    # with one shared ``LIMIT`` rather than five statements with five. (It is
    # ``USING COVERING INDEX`` only if the projection is ``id`` alone; the real
    # projection reads ``last_reason`` and the rest, so the plan fetches the
    # row. Measured both ways 2026-09-26 -- the seek is identical, and adding
    # ``created_at``/``attempt_count`` to the projection did not change it.) The
    # tempting spelling -- ``state NOT IN ('succeeded', 'unbound')`` -- cannot
    # use that index at all and scans the table, which is the thing this module
    # exists not to do.
    "sealed lane: SELECT ... FROM source_message_deletion_exits "
    "WHERE state IN (?, ...) AND updated_at <= ? ORDER BY id LIMIT n "
    "(never state NOT IN (...), which cannot use that index and scans)",
    # D6a's "how many messages has this lane already eaten". ``automation_reason``
    # carries no index, so the count is driven from the chat side instead:
    # ix_raw_messages_chat_id is covering for (chat_id, rowid), and the decisions
    # are then read by their own unique raw_message_id index.
    "chat scope: SELECT id FROM raw_messages WHERE chat_id = ? AND id > ? "
    "ORDER BY id LIMIT n",
    "message key: WHERE raw_message_id = ? / WHERE raw_message_id IN (?, ...)",
    # D6c's sweep. ``ix_runtime_incidents_claimable`` is (status,
    # claim_expires_at, last_occurred_at); the equality on status is the seek
    # and ``last_occurred_at`` is filtered from the same index entry. Severity
    # and ``notified_at`` are decided in Python because no index covers them.
    "unheard incident: SELECT ... FROM runtime_incidents "
    "WHERE status = ? AND last_occurred_at >= ? ORDER BY id LIMIT n",
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
    #: Spec 4.2 said 3 minutes. Three production days showed ten stalls of one
    #: or two messages, each over inside 1-14 minutes -- one message in AI
    #: context resolution, not a stuck queue. Ten minutes keeps the real
    #: 2026-09-16 shape (queue dead for an hour) and drops that noise.
    processing_job_stalled_after: timedelta = timedelta(minutes=10)
    #: Rule D3. The main pipeline retries a failed authoritative recognition
    #: by itself after 60 seconds
    #: (``telegram_live_listener.AUTHORITATIVE_FAILURE_RETRY_DELAY_SECONDS``),
    #: so a case opened the instant a failure lands would page for something
    #: that heals on its own a minute later. The design names no threshold;
    #: five minutes is D1d's, and it leaves room for several retries.
    recognition_failure_after: timedelta = timedelta(minutes=5)
    #: Rules D6a and D6c. The module-level constants are the policy; these
    #: fields exist so a test can inject a clock and a threshold together
    #: instead of sleeping.
    sealed_lane_stuck_after: timedelta = SEALED_LANE_STUCK_AFTER
    incident_still_occurring_within: timedelta = INCIDENT_STILL_OCCURRING_WITHIN
    incident_notification_silence: timedelta = INCIDENT_NOTIFICATION_SILENCE
    case_stale_after: timedelta = timedelta(hours=6)
    #: How recently reconcile must have rewritten a binding for "open" to be a
    #: verified fact rather than an assumption. Matches
    #: ``management_target_verification.DEFAULT_SNAPSHOT_MAX_AGE``.
    position_snapshot_max_age: timedelta = timedelta(minutes=5)
    #: How long order-level fill evidence may sit ahead of the ladder shadow
    #: before it is counted. Counted, never alerted (spec section 1 item 6).
    stop_ladder_unrecorded_after: timedelta = timedelta(minutes=5)
    read_failure_alert_rounds: int = 5
    worker_health_failure_rounds: int = 3
    intake_limit: int = 200
    watch_limit: int = 500
    #: D6a. How many lane-sealing exits one round looks at, across all of
    #: :data:`SEALED_LANE_SEALING_STATES` together. Every one of them costs two
    #: point queries to name its lane, so this is the round's real cost
    #: ceiling; it stays at a hundred for the five states because the sweep is
    #: ordered by id ascending, which returns the oldest rows -- the only ones
    #: that can be past the six-hour bar -- first.
    stuck_lane_limit: int = 100
    #: D6a. How many of a chat's messages after the seal are examined when
    #: counting what the lane has already voided. The count is reported as
    #: "at least", together with this bound.
    voided_scan_limit: int = 200
    #: D6c. How many still-occurring pending incidents one round looks at.
    incident_limit: int = 200


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


def as_production_text(moment: datetime) -> str:
    """One moment, spelled the way production's own DATETIME columns are.

    Rules D6a and D6c compare a timestamp *inside* SQL, which nothing in this
    module did before: it is what makes their sweeps index-seeking instead of
    table scans. SQLAlchemy's SQLite dialect writes naive UTC with six digits
    of microseconds, and the comparison is lexicographic, so the cutoff has to
    be produced in exactly that spelling -- ``datetime.isoformat`` drops the
    microseconds when they are zero, which would sort *before* an otherwise
    equal stored value.
    """

    return moment.astimezone(UTC).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )


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
#: Deliberately narrow: the model's prompt and raw reply live in
#: ``authoritative_payload_json`` and this module never selects that column.
_DECISION_COLUMNS = (
    "id, raw_message_id, agreement_status, automation_status, "
    "automation_reason, created_at, updated_at"
)

_LEDGER_COLUMNS = "id, venue, pos_id, purpose, status, evidence_json, updated_at"
_LADDER_EVENT_ACTION_PREFIX = "stop_ladder"
#: ``created_at`` and ``attempt_count`` joined this list on 2026-09-26 for D6a's
#: third cause: "still moving, still not finished" can only be asked of
#: ``created_at``, and ``attempt_count`` is the evidence that distinguishes a row
#: nobody claims from one that is claimed constantly. Only the projection grew --
#: the statement's WHERE / ORDER BY / LIMIT are untouched, and
#: ``EXPLAIN QUERY PLAN`` reports the same
#: ``SEARCH ... USING INDEX ix_source_message_deletion_exits_state
#: (state=? AND updated_at<?)`` as before, measured both ways.
_EXIT_COLUMNS = (
    "id, raw_message_id, execution_binding_id, state, last_reason, updated_at, "
    "created_at, attempt_count"
)
#: Deliberately narrow: ``redacted_summary`` is the only free text, and the
#: incident ledger already guarantees it carries no credential material.
#: ``diagnosis_json`` and ``evidence_refs_json`` are never selected.
_INCIDENT_COLUMNS = (
    "id, source_kind, source_record_id, incident_type, severity, status, "
    "repeat_count, first_occurred_at, last_occurred_at, notified_at, "
    "redacted_summary"
)

_WATERMARK_TABLES = (
    ("message_instruction_items", WATCH_INSTRUCTION_ITEM),
    ("strategy_management_batches", WATCH_MANAGEMENT_BATCH),
    ("message_processing_jobs", WATCH_PROCESSING_JOB),
    ("position_protection_ledger", WATCH_PROTECTION_LEDGER),
    ("recognition_decisions", WATCH_RECOGNITION_DECISION),
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
            _count_stop_ladder_levels_unrecorded(reader, store, now, settings)
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

    # Every decision row is watched, not only the ones that already look
    # lossy: ``automation_reason`` is written by a *later* update than the one
    # that inserts the row, so filtering at intake would drop exactly the rows
    # D3 exists for. The re-check retires a row the moment it settles clean.
    last_decision = store.get_watermark("recognition_decisions") or 0
    rows = reader.read_forward(
        "recognition_decisions", _DECISION_COLUMNS, last_decision, config.intake_limit
    )
    for row in rows:
        store.add_watch_item(
            kind=WATCH_RECOGNITION_DECISION,
            object_id=int(row["id"]),
            chat_id=None,
            now=now,
        )
    if rows:
        store.set_watermark("recognition_decisions", int(rows[-1]["id"]))

    last_ledger = store.get_watermark("position_protection_ledger") or 0
    rows = reader.read_forward(
        "position_protection_ledger", _LEDGER_COLUMNS, last_ledger, config.intake_limit
    )
    for row in rows:
        if str(row["purpose"] or "").lower() in _TAKE_PROFIT_PURPOSES:
            store.add_watch_item(
                kind=WATCH_PROTECTION_LEDGER,
                object_id=int(row["id"]),
                chat_id=None,
                now=now,
            )
    if rows:
        store.set_watermark("position_protection_ledger", int(rows[-1]["id"]))


#: ``position_protection_ledger.purpose`` values that are take profits.
_TAKE_PROFIT_PURPOSES = frozenset({"take_profit", "tp", "profit"})


def _count_stop_ladder_levels_unrecorded(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> int:
    """Count take-profit fills the ladder shadow has not caught up with.

    **This never opens a case and never alerts**, by the account owner's
    explicit instruction, and it is not a safety mechanism: whichever way the
    comparison comes out, the position keeps the stop it already has.  What it
    is for is the one thing an observation window otherwise cannot see -- the
    shadow going quiet while real fills happen -- and a counter is enough for
    that.

    It also never fails the round.  A ladder read that goes wrong is logged
    and dropped, because turning a counter's read into the round's verdict
    would let a diagnostic put the watcher into ``read_failed`` and raise
    D5a after five rounds.
    """

    try:
        watch_items = store.open_watch_items(
            WATCH_PROTECTION_LEDGER, config.watch_limit
        )
        if not watch_items:
            return 0
        expired = _expired_watch_ids(watch_items, now, config)
        store.retire_watch_items(WATCH_PROTECTION_LEDGER, expired)
        ids = [item.object_id for item in watch_items if item.object_id not in expired]
        if not ids:
            return 0
        rows = reader.read_by_ids("position_protection_ledger", _LEDGER_COLUMNS, ids)
        store.touch_watch_items(WATCH_PROTECTION_LEDGER, ids, now)
        found = {int(row["id"]) for row in rows}
        store.retire_watch_items(
            WATCH_PROTECTION_LEDGER, [value for value in ids if value not in found]
        )
        counted = 0
        retire: list[int] = []
        for row in rows:
            verdict = _stop_ladder_row_verdict(reader, row, now=now, config=config)
            if verdict is None:
                continue  # keep watching: the evidence may still arrive
            retire.append(int(row["id"]))
            counted += int(verdict)
        store.retire_watch_items(WATCH_PROTECTION_LEDGER, retire)
        if counted:
            store.bump_counter(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, counted)
        return counted
    except Exception:  # pragma: no cover - a counter never decides a round
        logger.warning("oncall stop-ladder count failed", exc_info=True)
        return 0


def _stop_ladder_row_verdict(
    reader: ProductionReader,
    row: sqlite3.Row,
    *,
    now: datetime,
    config: DetectorConfig,
) -> bool | None:
    """``True`` counted, ``False`` settled, ``None`` still worth watching."""

    if str(row["purpose"] or "").lower() not in _TAKE_PROFIT_PURPOSES:
        return False
    evidence = _json_object(row["evidence_json"]).get("take_profit_fill")
    if not isinstance(evidence, Mapping):
        return None
    try:
        level = int(evidence.get("level"))
    except (TypeError, ValueError):
        # Written by ``protection_health``, which knows the order filled but
        # not where it sits in the ladder. Nothing to compare, nothing to say.
        return False
    pos_id = str(row["pos_id"] or "").strip()
    if not pos_id or level <= 0:
        return False
    age = _age(now, as_utc(row["updated_at"]))
    if age is None or age < config.stop_ladder_unrecorded_after:
        return None
    return level > _recorded_stop_ladder_level(reader, pos_id=pos_id)


def _recorded_stop_ladder_level(reader: ProductionReader, *, pos_id: str) -> int:
    rows = reader.query(
        "SELECT id, action, after_json FROM execution_events "
        "WHERE pos_id = ? ORDER BY id DESC LIMIT 20",
        (pos_id,),
    )
    levels = [0]
    for row in rows:
        if not str(row["action"] or "").startswith(_LADDER_EVENT_ACTION_PREFIX):
            continue
        try:
            levels.append(int(_json_object(row["after_json"]).get("filled_level")))
        except (TypeError, ValueError):
            continue
    return max(levels)


# --------------------------------------------------------------------------
# Rule D6a: a lane nobody can use, and nobody can see
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SealedLane:
    """One lane-sealing deletion exit, and the lane it seals.

    ``symbol``/``side`` are ``None`` when the lane cannot be named. That is not
    a detail: ``source_execution_barrier`` blocks a new message only through a
    join of exit -> ``raw_messages`` -> ``signal_candidates`` with both symbol
    and side present, so an exit that cannot be named here seals nothing and
    :attr:`seals_a_lane` is false.
    """

    exit_id: int
    raw_message_id: int | None
    execution_binding_id: int | None
    state: str
    last_reason: str
    updated_at: datetime | None
    created_at: datetime | None = None
    attempt_count: int = 0
    chat_id: int | None = None
    symbol: str | None = None
    side: str | None = None

    @property
    def seals_a_lane(self) -> bool:
        return (
            self.raw_message_id is not None
            and self.chat_id is not None
            and bool(self.symbol)
            and bool(self.side)
        )

    @property
    def is_active(self) -> bool:
        return self.state in SEALED_LANE_ACTIVE_STATES

    def idle_for(self, now: datetime) -> timedelta | None:
        """How long since anything at all touched this row."""

        return _age(now, self.updated_at)

    def unfinished_for(self, now: datetime) -> timedelta | None:
        """How long since the exit was created without reaching a verdict.

        This is the age the barrier cares about: the lane has been shut since
        the row appeared, whatever has happened to the row since.
        """

        return _age(now, self.created_at)

    def stall_class(self, now: datetime, *, stuck_after: timedelta) -> str:
        """Which of the three stories this lane is.

        The barrier does not care -- anything but ``succeeded`` shuts the lane
        -- but the reader does, because a different person acts on each:

        * ``recovery_required`` has no owner at all; nothing will claim it
          again (:data:`LANE_STALL_UNCLAIMABLE`).
        * an active state that has not been touched for the whole window is
          held by the worker and standing still (:data:`LANE_STALL_ACTIVE`).
        * an active state that *has* been touched inside the window, on a row
          older than the window, is being worked on constantly and finishing
          never (:data:`LANE_STALL_CHURNING`).

        The third is decided last, so a row that both was created long ago and
        then went quiet reads as the plainer story -- standing still -- rather
        than as churn it is no longer doing.
        """

        if not self.is_active:
            return LANE_STALL_UNCLAIMABLE
        idle = self.idle_for(now)
        if idle is not None and idle >= stuck_after:
            return LANE_STALL_ACTIVE
        return LANE_STALL_CHURNING

    def reason_code(self, now: datetime, *, stuck_after: timedelta) -> str:
        stall_class = self.stall_class(now, stuck_after=stuck_after)
        if stall_class == LANE_STALL_ACTIVE:
            return REASON_STALLED_LANE
        if stall_class == LANE_STALL_CHURNING:
            return REASON_CHURNING_LANE
        return REASON_SEALED_LANE


def _read_sealed_lanes(
    reader: ProductionReader, now: datetime, config: DetectorConfig
) -> tuple[SealedLane, ...]:
    """Every exit currently in a state that shuts a lane, lane named.

    The state list is :data:`SEALED_LANE_SEALING_STATES`, not
    :data:`SEALED_LANE_STUCK_STATE` alone, because the barrier's filter is
    ``state != 'succeeded'``: an exit stalled in ``pending`` seals the lane just
    as completely as one parked in ``recovery_required``, and no sweep will ever
    release it. The states are listed positively so the read is an index seek
    per state; the negative spelling cannot use the index at all.

    The design asserted that the 91 ``unbound`` exits cannot seal a lane
    because their ``raw_message_id`` is NULL. Re-checked against the barrier
    rather than taken on trust: ``source_execution_barrier``'s overlapping-exit
    query inner-joins ``RawMessage.id == SourceMessageDeletionExit.raw_message_id``,
    so a NULL there removes the row from the join entirely -- and the state
    filter here removes them anyway. The same join also requires the deleted
    message to own a candidate with *both* symbol and side, which is why
    :attr:`SealedLane.seals_a_lane` demands the same and not less.

    The ``updated_at`` bound is ``now`` rather than the six-hour bar on purpose:
    D6b reads the same result to *name* the exit that ate a message, and that
    exit may have been sealing the lane for ten minutes. The age test belongs
    to :func:`_sealed_lane_observations`.
    """

    placeholders = ", ".join("?" for _ in SEALED_LANE_SEALING_STATES)
    rows = reader.query(
        "SELECT " + _EXIT_COLUMNS + " FROM source_message_deletion_exits "
        f"WHERE state IN ({placeholders}) AND updated_at <= ? ORDER BY id LIMIT ?",
        (
            *SEALED_LANE_SEALING_STATES,
            as_production_text(now),
            int(config.stuck_lane_limit),
        ),
    )
    lanes: list[SealedLane] = []
    for row in rows:
        raw_message_id = (
            int(row["raw_message_id"]) if row["raw_message_id"] is not None else None
        )
        chat_id: int | None = None
        symbol: str | None = None
        side: str | None = None
        if raw_message_id is not None:
            raw_message = reader.read_one(
                "raw_messages", _RAW_MESSAGE_COLUMNS, raw_message_id
            )
            if raw_message is not None:
                chat_id = int(raw_message["chat_id"])
                symbol, side = _latest_candidate_symbol_side(reader, raw_message_id)
        lanes.append(
            SealedLane(
                exit_id=int(row["id"]),
                raw_message_id=raw_message_id,
                execution_binding_id=(
                    int(row["execution_binding_id"])
                    if row["execution_binding_id"] is not None
                    else None
                ),
                state=str(row["state"] or ""),
                last_reason=str(row["last_reason"] or ""),
                updated_at=as_utc(row["updated_at"]),
                created_at=as_utc(row["created_at"]),
                attempt_count=int(row["attempt_count"] or 0),
                chat_id=chat_id,
                symbol=symbol,
                side=side,
            )
        )
    return tuple(lanes)


def _latest_candidate_symbol_side(
    reader: ProductionReader, raw_message_id: int
) -> tuple[str | None, str | None]:
    """Name the lane the way the barrier names it, and no other way.

    ``deferred_instruction_recovery._latest_candidate_symbol_side`` is the one
    function the barrier's own resume path uses: the highest-id candidate of
    the message that carries both a symbol and a side, upper-cased symbol and
    lower-cased side. That module cannot be imported here (architecture
    boundary), so the rule is reproduced -- reading the last 20 candidates and
    picking in Python keeps the statement inside the declared message-scope
    shape instead of inventing a new one for two ``IS NOT NULL`` predicates.
    """

    rows = reader.query(
        "SELECT id, symbol, side FROM signal_candidates "
        "WHERE raw_message_id = ? ORDER BY id DESC LIMIT 20",
        (int(raw_message_id),),
    )
    for row in rows:
        symbol = str(row["symbol"] or "").strip().upper()
        side = str(row["side"] or "").strip().lower()
        if symbol and side:
            return symbol, side
    return None, None


def _count_voided_messages(
    reader: ProductionReader,
    *,
    chat_id: int,
    after_raw_message_id: int,
    config: DetectorConfig,
) -> tuple[int, int]:
    """How many of this group's later messages the system has already voided.

    Returns ``(voided, examined)``. This is the line that turns "exit 310 is
    stuck" into "you are four entry strategies down", so it is worth two
    bounded reads. ``examined`` is reported with it, because the answer is
    always "at least this many, out of the next ``examined`` messages".
    """

    rows = reader.query(
        "SELECT id FROM raw_messages WHERE chat_id = ? AND id > ? "
        "ORDER BY id LIMIT ?",
        (int(chat_id), int(after_raw_message_id), int(config.voided_scan_limit)),
    )
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return 0, 0
    voided = 0
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        placeholders = ",".join("?" for _ in chunk)
        decisions = reader.query(
            "SELECT raw_message_id, automation_reason FROM recognition_decisions "
            f"WHERE raw_message_id IN ({placeholders})",
            chunk,
        )
        voided += sum(
            1
            for decision in decisions
            if str(decision["automation_reason"] or "").strip()
            == DEFERRED_EXPIRED_REASON
        )
    return voided, len(ids)


def _sealed_lane_observations(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    lanes: Sequence[SealedLane],
) -> list[_Observation]:
    """Rule D6a. A lane shut longer than the system's own healing window.

    Three causes, one rule: :data:`REASON_STALLED_LANE` when the worker still
    holds the row and is not finishing, :data:`REASON_CHURNING_LANE` when it is
    being claimed over and over and still not finishing, and
    :data:`REASON_SEALED_LANE` when nothing will claim it again. One case key per
    exit whichever it is, so a lane that slides from one cause into another
    keeps its case and updates its story instead of opening a second alert
    about the same shut lane.

    Two independent age tests, one bar:

    * ``updated_at`` older than the bar -- "nothing has happened to it". This is
      the original test and it finds the first and third causes.
    * ``created_at`` older than the bar, on an active exit -- "it has never
      finished", *even when* ``updated_at`` is seconds old. Without this a row
      claimed every five seconds is invisible: every claim writes
      ``updated_at = now``, so its idle age never grows. Production says the bar
      does not misfire: of 160 exits that succeeded in 2026-09, 153 were done
      inside a minute, the one-to-six-hour bucket was **empty**, and all four
      beyond six hours were the pathological ones.

    The ``created_at`` test is deliberately limited to the active states.
    ``recovery_required`` cannot show this shape: the only writer that touches
    such a row is ``source_deletion_exit_timeout._release``, and it writes
    ``succeeded`` in the same statement, so there is no path that refreshes a
    stuck row's ``updated_at`` while leaving it stuck.
    """

    observations: list[_Observation] = []
    seen: set[int] = set()
    for lane in lanes:
        if not lane.seals_a_lane:
            # Nothing is held, so there is nothing to tell anybody about.
            continue
        age = lane.idle_for(now)
        unfinished = lane.unfinished_for(now)
        idle_too_long = age is not None and age >= config.sealed_lane_stuck_after
        unfinished_too_long = (
            lane.is_active
            and unfinished is not None
            and unfinished >= config.sealed_lane_stuck_after
        )
        if not (idle_too_long or unfinished_too_long):
            continue
        seen.add(lane.exit_id)
        stall_class = lane.stall_class(
            now, stuck_after=config.sealed_lane_stuck_after
        )
        reason_code = lane.reason_code(
            now, stuck_after=config.sealed_lane_stuck_after
        )
        assert lane.chat_id is not None and lane.raw_message_id is not None
        voided, examined = _count_voided_messages(
            reader,
            chat_id=lane.chat_id,
            after_raw_message_id=lane.raw_message_id,
            config=config,
        )
        observations.append(
            _Observation(
                case_key=_sealed_lane_case_key(lane.exit_id),
                rule="D6a",
                severity="high",
                raw_message_id=lane.raw_message_id,
                chat_id=lane.chat_id,
                reason_code=reason_code,
                evidence={
                    "kind": "sealed_lane",
                    "reason_code": reason_code,
                    "stall_class": stall_class,
                    "exit_id": lane.exit_id,
                    "exit_state": lane.state,
                    "exit_last_reason": lane.last_reason,
                    "execution_binding_id": lane.execution_binding_id,
                    "symbol": lane.symbol or "",
                    "side": lane.side or "",
                    "sealed_since": (
                        lane.updated_at.isoformat()
                        if lane.updated_at is not None
                        else None
                    ),
                    "minutes_sealed": _minutes(age),
                    # The two below are what the churning story is told from:
                    # how long the exit has existed without a verdict, and how
                    # many times it has been claimed while getting nowhere.
                    "exit_created_at": (
                        lane.created_at.isoformat()
                        if lane.created_at is not None
                        else None
                    ),
                    "minutes_unfinished": _minutes(unfinished),
                    "attempt_count": lane.attempt_count,
                    "voided_messages": voided,
                    "voided_scan_examined": examined,
                },
            )
        )
    observations.extend(
        _sealed_lane_clears(reader, store, exclude=seen)
    )
    return observations


def _sealed_lane_clears(
    reader: ProductionReader, store: OncallStateStore, *, exclude: set[int]
) -> list[_Observation]:
    """A D6a case ends when the exit reaches ``succeeded`` -- and only then.

    The barrier reopens the lane on that state alone, whether the system healed
    itself or a person did it by hand. Any other state leaves the lane shut and
    the case open: a fresh ``updated_at`` on the same row, a ``pending`` that
    became ``reconciling``, an active state that gave up into
    ``recovery_required``. The six-hour staleness sweep is what ends such a case
    otherwise.
    """

    clears: list[_Observation] = []
    for case in store.open_cases():
        if not case.case_key.startswith(SEALED_LANE_CASE_PREFIX):
            continue
        exit_id = _case_key_object_id(case.case_key, SEALED_LANE_CASE_PREFIX)
        if exit_id is None or exit_id in exclude:
            continue
        row = reader.read_one(
            "source_message_deletion_exits", _EXIT_COLUMNS, exit_id
        )
        if row is not None and str(row["state"] or "") != SEALED_LANE_RELEASED_STATE:
            continue
        clears.append(
            _Observation(
                case_key=case.case_key,
                rule=None,
                cleared=True,
                chat_id=case.chat_id,
                raw_message_id=case.raw_message_id,
            )
        )
    return clears


# --------------------------------------------------------------------------
# Rule D6c: the alarm is still ringing and nobody has been told since
# --------------------------------------------------------------------------


def _unheard_incident_observations(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> list[_Observation]:
    """Rule D6c. Four conditions, and the third is "still happening".

    ``repeat_count`` is not one of them. Exits 310/311 reached 356933 repeats
    while notified once, on 2026-09-15 -- but a count that large is equally
    consistent with a failure that stopped a week ago, and nobody needs waking
    for that. ``last_occurred_at`` inside the window is the fact that makes it
    urgent.
    """

    rows = reader.query(
        "SELECT " + _INCIDENT_COLUMNS + " FROM runtime_incidents "
        "WHERE status = ? AND last_occurred_at >= ? ORDER BY id LIMIT ?",
        (
            INCIDENT_PENDING_STATUS,
            as_production_text(now - config.incident_still_occurring_within),
            int(config.incident_limit),
        ),
    )
    observations: list[_Observation] = []
    seen: set[int] = set()
    for row in rows:
        reason_code = _unheard_incident_reason(row, now=now, config=config)
        if reason_code is None:
            continue
        incident_id = int(row["id"])
        seen.add(incident_id)
        notified_at = as_utc(row["notified_at"])
        observations.append(
            _Observation(
                case_key=_unheard_incident_case_key(incident_id),
                rule="D6c",
                severity="high",
                reason_code=reason_code,
                evidence={
                    "kind": "unheard_incident",
                    "reason_code": reason_code,
                    "incident_id": incident_id,
                    "incident_type": str(row["incident_type"] or ""),
                    "incident_severity": str(row["severity"] or ""),
                    "source_kind": str(row["source_kind"] or ""),
                    "source_record_id": str(row["source_record_id"] or "")[:64],
                    "repeat_count": (
                        int(row["repeat_count"])
                        if row["repeat_count"] is not None
                        else None
                    ),
                    "first_occurred_at": _isoformat(as_utc(row["first_occurred_at"])),
                    "last_occurred_at": _isoformat(as_utc(row["last_occurred_at"])),
                    "notified_at": _isoformat(notified_at),
                    "minutes_since_last_occurrence": _minutes(
                        _age(now, as_utc(row["last_occurred_at"]))
                    ),
                    "minutes_since_notified": _minutes(_age(now, notified_at)),
                    "summary": str(row["redacted_summary"] or "")[:400],
                },
            )
        )
    observations.extend(_unheard_incident_clears(reader, store, now, config, seen))
    return observations


def _unheard_incident_reason(
    row: Mapping[str, Any] | sqlite3.Row,
    *,
    now: datetime,
    config: DetectorConfig,
) -> str | None:
    """The code D6c opens under, or ``None`` when this incident is fine."""

    if str(row["status"] or "").strip().lower() != INCIDENT_PENDING_STATUS:
        return None
    if str(row["severity"] or "").strip().lower() not in INCIDENT_LOUD_SEVERITIES:
        return None
    last_occurred = as_utc(row["last_occurred_at"])
    age = _age(now, last_occurred)
    if age is None or age > config.incident_still_occurring_within:
        return None
    notified_at = as_utc(row["notified_at"])
    if notified_at is None:
        return REASON_INCIDENT_NEVER_NOTIFIED
    silence = _age(now, notified_at)
    if silence is not None and silence >= config.incident_notification_silence:
        return REASON_INCIDENT_NOTIFICATION_STALE
    return None


def _unheard_incident_clears(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    exclude: set[int],
) -> list[_Observation]:
    """Stopped happening, or somebody was told: either way the case is over."""

    clears: list[_Observation] = []
    for case in store.open_cases():
        if not case.case_key.startswith(UNHEARD_INCIDENT_CASE_PREFIX):
            continue
        incident_id = _case_key_object_id(case.case_key, UNHEARD_INCIDENT_CASE_PREFIX)
        if incident_id is None or incident_id in exclude:
            continue
        row = reader.read_one("runtime_incidents", _INCIDENT_COLUMNS, incident_id)
        if (
            row is not None
            and _unheard_incident_reason(row, now=now, config=config) is not None
        ):
            # The sweep's limit hid it this round; the condition still holds.
            continue
        clears.append(
            _Observation(case_key=case.case_key, rule=None, cleared=True)
        )
    return clears


def _recheck(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
) -> list[_Observation]:
    observations: list[_Observation] = []
    # The sealed lanes are read once and used twice: D6a opens a case for the
    # ones that have been stuck long enough, and D6b names the exit that ate a
    # message. Reading them once is both cheaper and self-consistent -- the two
    # alerts then agree about which exit is to blame.
    lanes = _read_sealed_lanes(reader, now, config)
    observations.extend(_recheck_instruction_items(reader, store, now, config))
    observations.extend(_recheck_management_batches(reader, store, now, config))
    observations.extend(
        _recheck_recognition_decisions(reader, store, now, config, lanes=lanes)
    )
    observations.extend(_sealed_lane_observations(reader, store, now, config, lanes))
    observations.extend(_unheard_incident_observations(reader, store, now, config))
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
    # Phase 3 (remediation requests, spec 5.2/A6b): whether the *outcome* was
    # specifically ``shadow_planned`` -- distinct from ``reason_code``, which
    # prefers a human-readable ``result.reason`` over the raw status and so
    # cannot be trusted to say "shadow_planned" even when that is what
    # happened. Carried in evidence so the request-eligibility check does not
    # need to re-derive it from production.
    result_status_for_evidence: str | None = None

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
            result_status_for_evidence = result_status
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
        if _message_has_a_successful_batch(reader, row["raw_message_id"], candidate):
            # Production, 2026-09-21, raw 18089: the batch had succeeded and the
            # stop was already at the new price, but the instruction item was
            # never moved on from ``submitted``. The exchange did what the
            # message asked; a stale item status is not a missed instruction.
            return _clear_observation(row, candidate)
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
        result_status=result_status_for_evidence,
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


def _message_has_a_successful_batch(
    reader: ProductionReader,
    raw_message_id: Any,
    candidate: sqlite3.Row | None,
) -> bool:
    """Whether a management batch for this message and action already succeeded."""

    if raw_message_id is None:
        return False
    action = str(candidate["management_action"] or "").strip() if candidate else ""
    rows = reader.query(
        "SELECT id, intent, status FROM strategy_management_batches "
        "WHERE raw_message_id = ? ORDER BY id DESC LIMIT 10",
        (int(raw_message_id),),
    )
    return any(
        str(row["status"] or "") in {"succeeded", "resolved"}
        and (not action or str(row["intent"] or "") == action)
        for row in rows
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


def _recheck_recognition_decisions(
    reader: ProductionReader,
    store: OncallStateStore,
    now: datetime,
    config: DetectorConfig,
    *,
    lanes: Sequence[SealedLane] = (),
) -> list[_Observation]:
    watch_items = store.open_watch_items(WATCH_RECOGNITION_DECISION, config.watch_limit)
    if not watch_items:
        return []
    expired = _expired_watch_ids(watch_items, now, config)
    ids = [item.object_id for item in watch_items if item.object_id not in expired]
    store.retire_watch_items(WATCH_RECOGNITION_DECISION, expired)
    if not ids:
        return []
    rows = reader.read_by_ids("recognition_decisions", _DECISION_COLUMNS, ids)
    store.touch_watch_items(WATCH_RECOGNITION_DECISION, ids, now)
    found = {int(row["id"]) for row in rows}
    store.retire_watch_items(
        WATCH_RECOGNITION_DECISION, [value for value in ids if value not in found]
    )
    observations: list[_Observation] = []
    retire: list[int] = []
    for row in rows:
        observation = _evaluate_recognition_decision(
            reader, row, now=now, config=config, lanes=lanes
        )
        if observation is None:
            retire.append(int(row["id"]))
            continue
        if observation.retire:
            retire.append(int(row["id"]))
        observations.append(observation)
    store.retire_watch_items(WATCH_RECOGNITION_DECISION, retire)
    return observations


def lossy_recognition_reason(
    agreement_status: Any, automation_reason: Any
) -> str | None:
    """The reason code D3 opens under, or ``None`` when nothing was lost.

    A decision is lossy when the authority produced no usable answer at all
    (``agreement_status = 'authoritative_failed'``, which is also where a
    failed context resolution lands) or when the automation reason is one of
    the skips the design names. Every other skip -- the user's own switches,
    "the message asked for nothing", "no target was named" -- is a decision,
    not a loss, and alerting on those is what buried the real cases before.
    """

    code = str(automation_reason or "").strip()
    if code in LOSSY_RECOGNITION_REASONS:
        return code
    if str(agreement_status or "").strip().lower() == RECOGNITION_FAILED_STATUS:
        return code or RECOGNITION_FAILED_STATUS
    return None


def _message_has_management_work(reader: ProductionReader, raw_message_id: Any) -> bool:
    """Whether D1 or D2 already owns this message.

    A recognition that produced a management instruction item or a management
    batch is one the rest of the chain can see and rule on. D3 exists for the
    messages that produced *nothing*, so it stands down here rather than
    paging a second time for the same failure.
    """

    if raw_message_id is None:
        return False
    rows = reader.query(
        "SELECT id, instruction_kind FROM message_instruction_items "
        "WHERE raw_message_id = ? ORDER BY id LIMIT 10",
        (int(raw_message_id),),
    )
    if any(str(row["instruction_kind"] or "") == "management" for row in rows):
        return True
    rows = reader.query(
        "SELECT id, status FROM strategy_management_batches "
        "WHERE raw_message_id = ? ORDER BY id LIMIT 10",
        (int(raw_message_id),),
    )
    return bool(rows)


def _evaluate_recognition_decision(
    reader: ProductionReader,
    row: sqlite3.Row,
    *,
    now: datetime,
    config: DetectorConfig,
    lanes: Sequence[SealedLane] = (),
) -> _Observation | None:
    """Rules D3 and D6b, in that order, over one recognition decision row."""

    raw_message_id = row["raw_message_id"]
    if raw_message_id is None:
        return None
    automation_reason = str(row["automation_reason"] or "").strip()
    if automation_reason == DEFERRED_EXPIRED_REASON:
        # Rule D6b. Its own branch and its own case key: no lossy-reason test
        # and, above all, no "the group must hold a position" filter.
        return _voided_message_observation(reader, row, now=now, lanes=lanes)
    if automation_reason == DEFERRED_HOLD_REASON:
        # Still held behind a deletion exit, and it may yet be resumed and
        # executed normally. Not a case -- and deliberately not a *clear*
        # either, because a clear retires the watch item and the row would then
        # never be re-read when it turns into ``deferred_expired`` half an hour
        # later, which is the only state D6b is allowed to report.
        return _Observation(case_key=None, rule=None)
    case_key = _recognition_case_key(raw_message_id)
    reason_code = lossy_recognition_reason(
        row["agreement_status"], row["automation_reason"]
    )
    if reason_code is None:
        if (
            str(row["agreement_status"] or "").strip().lower()
            == RECOGNITION_PENDING_STATUS
            and not str(row["automation_status"] or "").strip()
        ):
            # The chain has not finished with this message yet. Neither a case
            # nor a clear: keep watching until it settles or the watch expires.
            return _Observation(case_key=None, rule=None)
        # Recognised, or re-recognised after a failure. Either way the case is
        # over, which is how "a later decision succeeds" resolves it.
        return _Observation(
            case_key=case_key,
            rule=None,
            cleared=True,
            raw_message_id=int(raw_message_id),
            retire=True,
        )

    if _message_has_management_work(reader, raw_message_id):
        return _Observation(
            case_key=case_key,
            rule=None,
            cleared=True,
            raw_message_id=int(raw_message_id),
            retire=True,
        )

    age = _age(now, as_utc(row["updated_at"]) or as_utc(row["created_at"]))
    if age is None or age < config.recognition_failure_after:
        return _Observation(case_key=None, rule=None)

    raw_message = reader.read_one("raw_messages", _RAW_MESSAGE_COLUMNS, raw_message_id)
    chat_id = int(raw_message["chat_id"]) if raw_message is not None else None
    open_rows = (
        read_chat_open_bindings(
            reader,
            chat_id=chat_id,
            now=now,
            snapshot_max_age=config.position_snapshot_max_age,
        )
        if chat_id is not None
        else []
    )
    if not open_rows:
        # No position, no case -- the same noise filter rule D1 applies.
        return _Observation(
            case_key=None,
            rule=None,
            reason_code="__no_position__",
            retire=True,
        )
    summaries = tuple(
        dict.fromkeys(f"{row_['symbol']} {row_['side']}" for row_, _state in open_rows)
    )[:8]
    return _Observation(
        case_key=case_key,
        rule="D3",
        severity="high",
        raw_message_id=int(raw_message_id),
        chat_id=chat_id,
        reason_code=reason_code,
        evidence=_build_recognition_evidence(
            raw_message=raw_message,
            reason_code=reason_code,
            agreement_status=str(row["agreement_status"] or ""),
            automation_status=str(row["automation_status"] or ""),
            automation_reason=str(row["automation_reason"] or ""),
            position_state=open_rows[0][1],
            group_open_positions=summaries,
            now=now,
        ),
        retire=False,
    )


def _voided_message_observation(
    reader: ProductionReader,
    row: sqlite3.Row,
    *,
    now: datetime,
    lanes: Sequence[SealedLane],
) -> _Observation:
    """Rule D6b. The system decided this real instruction will never run.

    No age threshold: ``deferred_expired`` is already terminal, and the row only
    reached it by outliving ``deferred_resume_timeout_minutes`` (30 in
    production). No position prerequisite either -- see
    :data:`DEFERRED_EXPIRED_REASON` for why that filter would have swallowed
    every entry this rule exists to report.
    """

    raw_message_id = int(row["raw_message_id"])
    raw_message = reader.read_one("raw_messages", _RAW_MESSAGE_COLUMNS, raw_message_id)
    chat_id = int(raw_message["chat_id"]) if raw_message is not None else None
    symbol, side = _latest_candidate_symbol_side(reader, raw_message_id)
    blocking = _blocking_sealed_lane(
        lanes, chat_id=chat_id, symbol=symbol, side=side, raw_message_id=raw_message_id
    )
    evidence = _build_recognition_evidence(
        raw_message=raw_message,
        reason_code=DEFERRED_EXPIRED_REASON,
        agreement_status=str(row["agreement_status"] or ""),
        automation_status=str(row["automation_status"] or ""),
        automation_reason=str(row["automation_reason"] or ""),
        position_state=POSITION_ABSENT,
        group_open_positions=(),
        now=now,
    )
    evidence["kind"] = "voided_message"
    evidence["symbol"] = symbol or ""
    evidence["side"] = side or ""
    # Naming the exit is what lets a reader line this alert up with the D6a
    # case for the same lane instead of treating them as two unrelated faults.
    evidence["blocking_exit_id"] = blocking.exit_id if blocking is not None else None
    evidence["blocking_exit_state"] = blocking.state if blocking is not None else None
    evidence["blocking_exit_last_reason"] = (
        blocking.last_reason if blocking is not None else None
    )
    return _Observation(
        case_key=_voided_message_case_key(raw_message_id),
        rule="D6b",
        severity="high",
        raw_message_id=raw_message_id,
        chat_id=chat_id,
        reason_code=DEFERRED_EXPIRED_REASON,
        evidence=evidence,
        # Terminal, so nothing will ever clear it: the six-hour staleness
        # sweep is what closes the case file.
        retire=True,
    )


def _blocking_sealed_lane(
    lanes: Sequence[SealedLane],
    *,
    chat_id: int | None,
    symbol: str | None,
    side: str | None,
    raw_message_id: int,
) -> SealedLane | None:
    """The sealed lane this message was held behind, if it can be named.

    Same test the barrier uses -- same chat, same symbol, same side, a
    different message -- over the exits this round already read. Since that read
    covers every lane-sealing state and not ``recovery_required`` alone, the
    exit named here is now the one the barrier would actually have blocked on,
    including an exit still in an active state. This only ever fills in
    ``blocking_exit_*`` in the D6b alert: whether a D6b case opens is decided by
    ``automation_reason`` and nothing else.
    """

    if chat_id is None or not symbol or not side:
        return None
    for lane in lanes:
        if not lane.seals_a_lane or lane.raw_message_id == raw_message_id:
            continue
        if lane.chat_id == chat_id and lane.symbol == symbol and lane.side == side:
            return lane
    return None


def _build_recognition_evidence(
    *,
    raw_message: sqlite3.Row | None,
    reason_code: str,
    agreement_status: str,
    automation_status: str,
    automation_reason: str,
    position_state: str,
    group_open_positions: Sequence[str],
    now: datetime,
) -> dict[str, Any]:
    """What the D3 alert needs. The excerpt is untrusted text, bounded here."""

    posted_at = as_utc(raw_message["posted_at"]) if raw_message is not None else None
    return {
        "kind": "recognition",
        "reason_code": reason_code,
        "agreement_status": agreement_status,
        "automation_status": automation_status,
        "automation_reason": automation_reason,
        "message_id": int(raw_message["message_id"]) if raw_message is not None else None,
        "posted_at": posted_at.isoformat() if posted_at is not None else None,
        "message_text": (
            str(raw_message["text"] or "")[:400] if raw_message is not None else ""
        ),
        "minutes_since_message": _minutes(_age(now, posted_at)),
        "position_state": position_state,
        "group_open_positions": list(group_open_positions),
    }


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


def _recognition_case_key(raw_message_id: Any) -> str:
    return f"{RECOGNITION_CASE_PREFIX}{int(raw_message_id)}"


def _sealed_lane_case_key(exit_id: Any) -> str:
    return f"{SEALED_LANE_CASE_PREFIX}{int(exit_id)}"


def _voided_message_case_key(raw_message_id: Any) -> str:
    return f"{VOIDED_MESSAGE_CASE_PREFIX}{int(raw_message_id)}"


def _unheard_incident_case_key(incident_id: Any) -> str:
    return f"{UNHEARD_INCIDENT_CASE_PREFIX}{int(incident_id)}"


def _case_key_object_id(case_key: str, prefix: str) -> int | None:
    """The production row id a D6 case key names, or ``None`` if unreadable."""

    try:
        return int(str(case_key).removeprefix(prefix))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _isoformat(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


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
    result_status: str | None = None,
) -> dict[str, Any]:
    """Everything the alert text needs, captured once, bounded to 8 KB.

    The message excerpt is untrusted external text. It is stored verbatim but
    bounded, and the formatter is the only place that decides how to show it.

    ``result_status`` (D1b only) is the instruction item's raw
    ``result_json["status"]`` -- ``"skipped"`` or ``"shadow_planned"`` --
    kept separate from ``reason_code`` because a remediation-request decision
    needs to tell those two apart and ``reason_code`` prefers the human
    ``result.reason`` over the status word whenever one is present.
    """

    posted_at = as_utc(raw_message["posted_at"]) if raw_message is not None else None
    return {
        "action": action,
        "reason_code": reason_code,
        "item_status": item_status,
        "result_status": result_status,
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
