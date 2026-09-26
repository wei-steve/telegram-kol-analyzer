"""Daily retention for the two tables that only ever grow.

``docs/plans/2026-09-26-server-disk-usage-analysis.md`` sections 3.3 and 5.3.
Run as ``python -m telegram_kol_research.db_retention --database-path
data/research.db [--apply]``. Without ``--apply`` it only counts. It prints one
JSON line summarising what it found and did.

Two jobs, nothing else:

* ``pending_tpsl_snapshot_observations`` -- delete rows whose ``observed_at``
  is older than ``--tpsl-retain-days`` (7), except the newest row of every
  ``(venue, instrument_id)``. "Newest" is exactly what the only reader
  (``strategy_records.py``, the pending snapshot lookup) takes:
  ``ORDER BY observed_at DESC, id DESC LIMIT 1``. So whatever that reader
  returns before the job, it returns after it.
* ``context_resolution_attempts`` -- for terminal rows older than
  ``--context-request-retain-days`` (30), replace ``request_summary_json`` with
  a tagged ``retention_stub`` marker
  (:func:`telegram_kol_research.context_request_storage.build_retention_stub`).
  Every other column is left as it is. Already-tagged values are skipped, so a
  second run changes nothing.

How it stays out of the live runtime's way:

* Batches of at most :data:`MAX_BATCH_SIZE` rows, each its own short
  ``BEGIN IMMEDIATE`` transaction, with a pause between batches.
* A total run-time cap (10 minutes by default). When it is reached the run
  stops between batches and the next run carries on.
* ``busy_timeout`` 30 s. A ``database is locked`` / busy error abandons the
  current batch (rolled back), is recorded in the summary, and ends the run
  cleanly -- there is no retry loop. The next scheduled run tries again.
* It never runs ``VACUUM`` and never changes a pragma that persists. Freed
  pages are reused by SQLite; shrinking the file is a separate decision.
* It writes only these two tables. To judge the age of an attempt it also
  reads ``raw_messages`` (see :func:`_context_candidate_ids`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from telegram_kol_research.context_request_storage import build_retention_stub
from telegram_kol_research.context_resolution_worker import UNRESOLVED_DECISIONS


logger = logging.getLogger(__name__)

TPSL_TABLE = "pending_tpsl_snapshot_observations"
CONTEXT_TABLE = "context_resolution_attempts"

DEFAULT_TPSL_RETAIN_DAYS = 7
DEFAULT_CONTEXT_REQUEST_RETAIN_DAYS = 30
#: Hard upper bound on rows touched by one transaction, whatever is asked for.
MAX_BATCH_SIZE = 5000
DEFAULT_TPSL_BATCH_SIZE = 5000
#: A context row carries ~64 KB of request (up to ~130 KB), and replacing it
#: means reading all of it inside the write transaction. 200 rows is ~13 MB,
#: which keeps one write lock in the tens of milliseconds.
DEFAULT_CONTEXT_BATCH_SIZE = 200
DEFAULT_BATCH_SLEEP_SECONDS = 0.2
DEFAULT_MAX_RUNTIME_SECONDS = 600.0
BUSY_TIMEOUT_MS = 30_000

#: Statuses after which the context worker never picks a row up again. Read
#: from ``context_resolution_worker.py`` and ``context_resolution.py``:
#:
#: * ``_claimable`` only claims ``pending_reanalysis`` / ``retry_pending``, and
#:   ``running`` once its claim is stale -- all three are excluded here.
#: * ``schedule_context_reanalysis`` only promotes ``completed`` (and
#:   ``pending_reanalysis``) rows, and only when the decision is ``unresolved``
#:   / ``hold`` *and* a declared reanalysis trigger fires. ``completed`` is
#:   therefore listed, but :func:`_context_row_is_settled` drops every
#:   completed row that could still be promoted.
#: * ``exhausted``, ``superseded``, ``blocked_disabled``,
#:   ``blocked_execution_terminal`` and ``reanalysis_capped`` are written only
#:   as final states; ``failed`` is a legacy final state still present in old
#:   rows. None of them is read back by any claim or schedule query.
#:
#: This is an allowlist: a status added later (``pending`` is already named by
#: the Web card) is left alone until somebody adds it here on purpose.
CONTEXT_SETTLED_STATUSES = frozenset(
    {
        "completed",
        "exhausted",
        "superseded",
        "failed",
        "blocked_disabled",
        "blocked_execution_terminal",
        "reanalysis_capped",
    }
)

#: ``DateTime`` columns are written by SQLAlchemy, which stores
#: ``YYYY-MM-DD HH:MM:SS.ffffff`` with the UTC wall time (``models.utc_now``).
#: Comparing against a cutoff in the same text form keeps the comparison on
#: the ``(venue, instrument_id, observed_at)`` index instead of wrapping the
#: column in a date function. A value in another shape (``T`` separator) sorts
#: *after* the same instant in this shape, so it can only be kept longer,
#: never deleted early.
_SQLITE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class _DatabaseLocked(Exception):
    """A batch hit ``database is locked``; the run ends here."""

    def __init__(self, report: "TaskReport") -> None:
        super().__init__(report.details.get("error"))
        self.report = report


@dataclass
class TaskReport:
    name: str
    candidates: int = 0
    processed: int = 0
    batches: int = 0
    elapsed_seconds: float = 0.0
    stop_reason: str = "not_started"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "processed": self.processed,
            "batches": self.batches,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stop_reason": self.stop_reason,
            **self.details,
        }


@dataclass
class _Budget:
    deadline: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    batch_sleep_seconds: float

    def exhausted(self) -> bool:
        return self.clock() >= self.deadline

    def pause(self) -> None:
        if self.batch_sleep_seconds > 0:
            self.sleep(self.batch_sleep_seconds)


def _sqlite_datetime(value: datetime) -> str:
    utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return utc.replace(tzinfo=None).strftime(_SQLITE_DATETIME_FORMAT)


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def connect(database_path: str | Path, *, apply: bool) -> sqlite3.Connection:
    """Open an existing database; never create one. Dry-run opens read-only."""

    path = Path(database_path).resolve(strict=True)
    mode = "rw" if apply else "ro"
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode={mode}",
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def _begin_immediate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# pending_tpsl_snapshot_observations
# --------------------------------------------------------------------------


def _tpsl_groups(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every distinct ``(venue, instrument_id)``, by skipping along the index.

    One indexed probe per group instead of a scan of ~2 million index entries.
    """

    groups: list[tuple[str, str]] = []
    row = connection.execute(
        f"SELECT venue, instrument_id FROM {TPSL_TABLE} "
        "ORDER BY venue, instrument_id LIMIT 1"
    ).fetchone()
    while row is not None:
        groups.append((row[0], row[1]))
        row = connection.execute(
            f"SELECT venue, instrument_id FROM {TPSL_TABLE} "
            "WHERE (venue, instrument_id) > (?, ?) "
            "ORDER BY venue, instrument_id LIMIT 1",
            (row[0], row[1]),
        ).fetchone()
    return groups


def _tpsl_keeper_id(connection: sqlite3.Connection, venue: str, instrument_id: str) -> int | None:
    # Same order as the reader in strategy_records.py.
    row = connection.execute(
        f"SELECT id FROM {TPSL_TABLE} WHERE venue = ? AND instrument_id = ? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (venue, instrument_id),
    ).fetchone()
    return None if row is None else int(row[0])


def _tpsl_count(
    connection: sqlite3.Connection,
    venue: str,
    instrument_id: str,
    cutoff: str,
    keeper_id: int | None,
) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {TPSL_TABLE} WHERE venue = ? AND instrument_id = ? "
            "AND observed_at < ? AND id != ?",
            (venue, instrument_id, cutoff, -1 if keeper_id is None else keeper_id),
        ).fetchone()[0]
    )


def prune_tpsl_observations(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    retain_days: int,
    batch_size: int,
    apply: bool,
    budget: _Budget,
    monotonic: Callable[[], float],
) -> TaskReport:
    report = TaskReport(name=TPSL_TABLE)
    started = monotonic()
    cutoff = _sqlite_datetime(now - timedelta(days=int(retain_days)))
    report.details = {"cutoff": cutoff, "retain_days": int(retain_days), "groups": 0}
    try:
        groups = _tpsl_groups(connection)
        report.details["groups"] = len(groups)
        pending: list[tuple[str, str]] = []
        for venue, instrument_id in groups:
            keeper = _tpsl_keeper_id(connection, venue, instrument_id)
            count = _tpsl_count(connection, venue, instrument_id, cutoff, keeper)
            report.candidates += count
            if count:
                pending.append((venue, instrument_id))
        if not apply:
            report.stop_reason = "dry_run"
            return report
        report.stop_reason = "completed"
        for venue, instrument_id in pending:
            while True:
                if budget.exhausted():
                    report.stop_reason = "time_limit"
                    return report
                try:
                    _begin_immediate(connection)
                    # Re-read inside the write lock: a newer row may have
                    # arrived, and the keeper is whatever is newest *now*.
                    keeper = _tpsl_keeper_id(connection, venue, instrument_id)
                    cursor = connection.execute(
                        f"DELETE FROM {TPSL_TABLE} WHERE id IN ("
                        f"SELECT id FROM {TPSL_TABLE} WHERE venue = ? AND instrument_id = ? "
                        "AND observed_at < ? AND id != ? "
                        "ORDER BY observed_at, id LIMIT ?)",
                        (
                            venue,
                            instrument_id,
                            cutoff,
                            -1 if keeper is None else keeper,
                            int(batch_size),
                        ),
                    )
                    deleted = int(cursor.rowcount or 0)
                    connection.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    _rollback_quietly(connection)
                    if _is_lock_error(exc):
                        report.stop_reason = "database_locked"
                        report.details["error"] = str(exc)
                        raise _DatabaseLocked(report) from exc
                    raise
                report.batches += 1
                report.processed += deleted
                budget.pause()
                if deleted < int(batch_size):
                    break
        return report
    finally:
        report.elapsed_seconds = monotonic() - started


# --------------------------------------------------------------------------
# context_resolution_attempts
# --------------------------------------------------------------------------


def _context_candidate_ids(connection: sqlite3.Connection, cutoff: str) -> list[int]:
    """Ids with a settled status whose *message* is older than the cutoff.

    Why the message time and not the row's own ``created_at``: the production
    snapshot has ``created_at`` NULL on nearly every row (6,713 of 6,714), so
    that column cannot carry an age. Why not the row's ``updated_at`` alone:
    ``status``, ``created_at`` and ``updated_at`` sit *after* the ~64 KB
    ``request_summary_json`` in the record, so reading any of them for every row
    walks every row's overflow chain -- a ~0.5 GB read each night, the kind of
    scan that has stalled the worker before. Both queries here are answered
    from covering indexes (``status`` / ``raw_message_id``) plus the
    ``raw_messages`` primary key, and touch no attempt payload at all.

    The row's own time is still checked, inside the write transaction, by
    :func:`_context_row_is_settled`; this is only the cheap first cut. A
    message cannot be younger than an attempt about it, so the cut never
    excludes a row that is actually old.
    """

    placeholders = ",".join("?" for _ in CONTEXT_SETTLED_STATUSES)
    settled = {
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM {CONTEXT_TABLE} WHERE status IN ({placeholders})",
            tuple(sorted(CONTEXT_SETTLED_STATUSES)),
        )
    }
    old_message = {
        int(row[0])
        for row in connection.execute(
            f"SELECT cra.id FROM {CONTEXT_TABLE} AS cra "
            "JOIN raw_messages AS rm ON rm.id = cra.raw_message_id "
            "WHERE COALESCE(rm.posted_at, rm.created_at) < ?",
            (cutoff,),
        )
    }
    return sorted(settled & old_message)


def _json_value(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _context_row_is_settled(row: sqlite3.Row, cutoff: str) -> str | None:
    """``None`` if the row may be stubbed, otherwise the reason it may not."""

    status = str(row["status"] or "")
    if status not in CONTEXT_SETTLED_STATUSES:
        return "skipped_not_settled"
    if status == "completed":
        # Mirrors schedule_context_reanalysis: an unresolved/hold decision with
        # any declared trigger can still be promoted to pending_reanalysis.
        # Any declared trigger at all, not only the ones mapped today -- the
        # conservative side.
        decision = _json_value(row["decision_json"], {})
        triggers = _json_value(row["reanalysis_triggers_json"], [])
        unresolved = (
            isinstance(decision, dict)
            and str(decision.get("decision") or "") in UNRESOLVED_DECISIONS
        )
        if unresolved and isinstance(triggers, list) and triggers:
            return "skipped_reanalysis_eligible"
    # The row's own last activity. ``updated_at`` is set by every writer and is
    # never earlier than the creation time; ``created_at`` is the fallback. If
    # both are NULL the message age from _context_candidate_ids stands alone.
    row_time = row["updated_at"] if row["updated_at"] is not None else row["created_at"]
    if row_time is not None and str(row_time) >= cutoff:
        return "skipped_recent_activity"
    return None


def _context_batch(
    connection: sqlite3.Connection,
    ids: list[int],
    *,
    cutoff: str,
    stubbed_at: str,
    apply: bool,
    counters: dict[str, int],
) -> int:
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT id, status, decision_json, reanalysis_triggers_json, updated_at, "
        f"created_at, request_summary_json FROM {CONTEXT_TABLE} "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        tuple(ids),
    ).fetchall()
    changed = 0
    for row in rows:
        reason = _context_row_is_settled(row, cutoff)
        if reason is not None:
            counters[reason] = counters.get(reason, 0) + 1
            continue
        stored = row["request_summary_json"]
        stub = build_retention_stub(str(stored), stubbed_at=stubbed_at) if stored is not None else None
        if stub is None:
            # Already a marker (reference-only, archived, an earlier stub) or
            # not parseable -- either way, not ours to replace.
            counters["skipped_not_legacy_full"] = counters.get("skipped_not_legacy_full", 0) + 1
            continue
        original_bytes = len(str(stored).encode("utf-8"))
        if len(stub.encode("utf-8")) >= original_bytes:
            counters["skipped_stub_not_smaller"] = counters.get("skipped_stub_not_smaller", 0) + 1
            continue
        counters["eligible"] = counters.get("eligible", 0) + 1
        counters["original_bytes"] = counters.get("original_bytes", 0) + original_bytes
        if apply:
            connection.execute(
                f"UPDATE {CONTEXT_TABLE} SET request_summary_json = ? WHERE id = ?",
                (stub, int(row["id"])),
            )
            changed += 1
    return changed


def stub_context_requests(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    retain_days: int,
    batch_size: int,
    apply: bool,
    budget: _Budget,
    monotonic: Callable[[], float],
) -> TaskReport:
    report = TaskReport(name=CONTEXT_TABLE)
    started = monotonic()
    cutoff = _sqlite_datetime(now - timedelta(days=int(retain_days)))
    stubbed_at = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    counters: dict[str, int] = {"eligible": 0, "original_bytes": 0}
    report.details = {"cutoff": cutoff, "retain_days": int(retain_days)}
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        candidate_ids = _context_candidate_ids(connection, cutoff)
        report.candidates = len(candidate_ids)
        report.stop_reason = "completed" if apply else "dry_run"
        for offset in range(0, len(candidate_ids), int(batch_size)):
            if budget.exhausted():
                report.stop_reason = "time_limit"
                break
            ids = candidate_ids[offset : offset + int(batch_size)]
            try:
                if apply:
                    _begin_immediate(connection)
                changed = _context_batch(
                    connection,
                    ids,
                    cutoff=cutoff,
                    stubbed_at=stubbed_at,
                    apply=apply,
                    counters=counters,
                )
                if apply:
                    connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                _rollback_quietly(connection)
                if _is_lock_error(exc):
                    report.stop_reason = "database_locked"
                    report.details["error"] = str(exc)
                    raise _DatabaseLocked(report) from exc
                raise
            report.batches += 1
            report.processed += changed
            budget.pause()
        return report
    finally:
        connection.row_factory = previous_factory
        report.details.update(counters)
        report.elapsed_seconds = monotonic() - started


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run_retention(
    database_path: str | Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
    tpsl_retain_days: int = DEFAULT_TPSL_RETAIN_DAYS,
    context_request_retain_days: int = DEFAULT_CONTEXT_REQUEST_RETAIN_DAYS,
    tpsl_batch_size: int = DEFAULT_TPSL_BATCH_SIZE,
    context_batch_size: int = DEFAULT_CONTEXT_BATCH_SIZE,
    batch_sleep_seconds: float = DEFAULT_BATCH_SLEEP_SECONDS,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run both jobs in order under one time budget; return the summary."""

    for name, value in (
        ("tpsl_batch_size", tpsl_batch_size),
        ("context_batch_size", context_batch_size),
    ):
        if not 1 <= int(value) <= MAX_BATCH_SIZE:
            raise ValueError(f"{name} must be between 1 and {MAX_BATCH_SIZE}")
    for name, value in (
        ("tpsl_retain_days", tpsl_retain_days),
        ("context_request_retain_days", context_request_retain_days),
    ):
        if int(value) < 1:
            raise ValueError(f"{name} must be at least 1")
    if float(batch_sleep_seconds) < 0 or float(max_runtime_seconds) <= 0:
        raise ValueError("batch sleep must be >= 0 and max runtime > 0")

    moment = now or datetime.now(UTC)
    started = monotonic()
    budget = _Budget(
        deadline=started + float(max_runtime_seconds),
        clock=monotonic,
        sleep=sleep,
        batch_sleep_seconds=float(batch_sleep_seconds),
    )
    reports = {
        TPSL_TABLE: TaskReport(name=TPSL_TABLE),
        CONTEXT_TABLE: TaskReport(name=CONTEXT_TABLE),
    }
    stop_reason = "completed" if apply else "dry_run"
    connection = connect(database_path, apply=apply)
    try:
        for task, retain, size in (
            (prune_tpsl_observations, tpsl_retain_days, tpsl_batch_size),
            (stub_context_requests, context_request_retain_days, context_batch_size),
        ):
            try:
                report = task(
                    connection,
                    now=moment,
                    retain_days=int(retain),
                    batch_size=int(size),
                    apply=apply,
                    budget=budget,
                    monotonic=monotonic,
                )
            except _DatabaseLocked as locked:
                reports[locked.report.name] = locked.report
                stop_reason = "database_locked"
                logger.warning(
                    "db retention stopped: database locked during %s batch; "
                    "batch rolled back, next run continues",
                    locked.report.name,
                )
                break
            except sqlite3.OperationalError as exc:
                # A read outside any batch (the candidate scan) can report
                # busy too; same clean stop, nothing was written by it.
                if not _is_lock_error(exc):
                    raise
                name = TPSL_TABLE if task is prune_tpsl_observations else CONTEXT_TABLE
                reports[name] = TaskReport(
                    name=name,
                    stop_reason="database_locked",
                    details={"error": str(exc)},
                )
                stop_reason = "database_locked"
                logger.warning(
                    "db retention stopped: database locked while reading %s", name
                )
                break
            reports[report.name] = report
            if report.stop_reason == "time_limit":
                stop_reason = "time_limit"
                break
    finally:
        connection.close()
    return {
        "mode": "apply" if apply else "dry_run",
        "database_path": str(database_path),
        "now": moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "stop_reason": stop_reason,
        "elapsed_seconds": round(monotonic() - started, 3),
        "tasks": {name: report.to_dict() for name, report in reports.items()},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m telegram_kol_research.db_retention",
        description=(
            "Prune old pending-TPSL observations and replace old context "
            "request payloads with retention stubs. Dry-run unless --apply."
        ),
    )
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tpsl-retain-days", type=int, default=DEFAULT_TPSL_RETAIN_DAYS)
    parser.add_argument(
        "--context-request-retain-days",
        type=int,
        default=DEFAULT_CONTEXT_REQUEST_RETAIN_DAYS,
    )
    parser.add_argument("--tpsl-batch-size", type=int, default=DEFAULT_TPSL_BATCH_SIZE)
    parser.add_argument(
        "--context-batch-size", type=int, default=DEFAULT_CONTEXT_BATCH_SIZE
    )
    parser.add_argument(
        "--batch-sleep-seconds", type=float, default=DEFAULT_BATCH_SLEEP_SECONDS
    )
    parser.add_argument(
        "--max-runtime-seconds", type=float, default=DEFAULT_MAX_RUNTIME_SECONDS
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parser().parse_args(list(argv) if argv is not None else None)
    summary = run_retention(
        args.database_path,
        apply=bool(args.apply),
        tpsl_retain_days=args.tpsl_retain_days,
        context_request_retain_days=args.context_request_retain_days,
        tpsl_batch_size=args.tpsl_batch_size,
        context_batch_size=args.context_batch_size,
        batch_sleep_seconds=args.batch_sleep_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    # A lock or the time cap is an expected, resumable stop, not a failure:
    # exit 0 so the timer unit does not read as broken; the JSON line and the
    # WARNING say what happened. Anything unexpected raises and exits non-zero.
    return 0


if __name__ == "__main__":
    sys.exit(main())
