"""The on-call watcher's own SQLite state: cases, watch items, alerts, meta.

Phase 1 of the Codex on-call remediation program
(``docs/plans/2026-09-19-codex-oncall-phase1-spec.md``).

This database is **not** the production database. The watcher never writes to
production -- the 2026-09-16 lock incident is the reason -- so every piece of
state it needs to survive a restart lives here instead: how far each table has
been read (``meta`` watermarks), which production rows still need re-checking
(``watch_items``), which problems are open (``cases``), and which alerts have
been composed and whether they were delivered (``alerts``).

Restart safety is a property of this file. Watermarks are only ever advanced,
cases are keyed by a deterministic ``case_key``, and every alert carries a
``dedupe_key`` under a unique index, so a watcher restarted against the same
state database re-opens no case and re-sends no alert.

Nothing secret is stored here: the bot token is held in memory by the sending
process and never reaches a row (see ``oncall_alerts``).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


CASE_STATUSES = frozenset({"open", "resolved", "stale"})

#: Case-key namespaces. The key decides which story an alert tells and which
#: evidence a case file carries, so the prefixes live here -- one vocabulary
#: shared by the detector that writes them and the alerts that read them.
HEALTH_CASE_PREFIX = "health:"
MANAGEMENT_CASE_PREFIX = "mgmt:"
#: Rule D3: one case per message, because a recognition that produced nothing
#: has no action to key on.
RECOGNITION_CASE_PREFIX = "recog:"
#: Rule D6a: one case per sealed source-deletion exit. The exit id is what a
#: person acts on, and it is stable for the whole life of the seal.
SEALED_LANE_CASE_PREFIX = "lane:"
#: Rule D6b: one case per message the system voided. Deliberately *not* the
#: D3 prefix -- the two rules can both have something to say about one
#: message, and a resolved D3 case must not swallow a later loss.
VOIDED_MESSAGE_CASE_PREFIX = "voided:"
#: Rule D6c: one case per runtime incident that is still happening and that
#: nobody has been told about lately. Keyed by incident id, not fingerprint:
#: the id is what the operator bot's own reports show.
UNHEARD_INCIDENT_CASE_PREFIX = "unheard:"

#: Rule D6a's two ways for one lane to stay shut. They are the same problem --
#: nothing of this group, symbol and side can get in -- but not the same cause,
#: and the cause decides who can do something about it, so the alert has to say
#: which one it is. The words live here because the detector writes them into
#: the case evidence and the alert reads them back, and the two modules may not
#: import each other.
#:
#: ``LANE_STALL_ACTIVE``: the exit is in one of the deletion worker's active
#: states. It is claimable, it should be through in seconds, and nothing
#: sweeps it -- standing still means the claim or one of the steps is going
#: round in circles.
#:
#: ``LANE_STALL_UNCLAIMABLE``: the exit is in ``recovery_required``, which the
#: worker never claims again. Only the system's own timeout sweep or a person
#: can move it.
LANE_STALL_ACTIVE = "active"
LANE_STALL_UNCLAIMABLE = "unclaimable"

ALERT_STATUSES = frozenset({"pending", "sent", "dry_run", "failed"})
WATCH_STATUSES = frozenset({"open", "retired"})

DIAGNOSIS_QUEUED = "queued"
DIAGNOSIS_DONE = "done"
DIAGNOSIS_FAILED = "failed"
DIAGNOSIS_SKIPPED = "skipped"
DIAGNOSIS_STATUSES = frozenset(
    {DIAGNOSIS_QUEUED, DIAGNOSIS_DONE, DIAGNOSIS_FAILED, DIAGNOSIS_SKIPPED}
)

MESSAGE_NONE = "none"
MESSAGE_QUEUED = "queued"
MESSAGE_SUPPRESSED = "suppressed"

#: Evidence is bounded so one pathological message cannot grow the state file.
MAX_EVIDENCE_BYTES = 8192

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_key TEXT NOT NULL UNIQUE,
        rule TEXT NOT NULL,
        severity TEXT NOT NULL,
        raw_message_id INTEGER,
        chat_id INTEGER,
        item_ids_json TEXT NOT NULL DEFAULT '[]',
        batch_ids_json TEXT NOT NULL DEFAULT '[]',
        reason_code TEXT,
        target_uncertain INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        alerted_at TEXT,
        resolved_at TEXT,
        evidence_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cases_status ON cases (status, id)",
    "CREATE INDEX IF NOT EXISTS ix_cases_chat_alerted ON cases (chat_id, alerted_at)",
    """
    CREATE TABLE IF NOT EXISTS watch_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        object_id INTEGER NOT NULL,
        chat_id INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        first_seen_at TEXT NOT NULL,
        last_checked_at TEXT,
        UNIQUE (kind, object_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_watch_items_open ON watch_items (status, kind, object_id)",
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER,
        kind TEXT NOT NULL,
        dedupe_key TEXT UNIQUE,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        delivery_error TEXT,
        created_at TEXT NOT NULL,
        delivered_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_alerts_pending ON alerts (status, id)",
    # Phase 2. One row per case that Codex was, or was deliberately not,
    # asked about. ``verdict_json`` holds the *validated* answer only, so a
    # rejected answer is remembered as a failure and never as a diagnosis.
    """
    CREATE TABLE IF NOT EXISTS diagnoses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        request_fingerprint TEXT,
        prompt_version TEXT,
        failure_class TEXT,
        skip_reason TEXT,
        verdict_json TEXT,
        message_state TEXT NOT NULL DEFAULT 'none',
        requested_at TEXT,
        completed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_diagnoses_status ON diagnoses (status, id)",
)


def isoformat(moment: datetime) -> str:
    """One spelling for every timestamp written to this database."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_isoformat(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def bounded_evidence_json(evidence: dict[str, Any]) -> str:
    """Serialise evidence, dropping the largest fields until it fits."""

    payload = dict(evidence)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    while len(encoded.encode("utf-8")) > MAX_EVIDENCE_BYTES and payload:
        widest = max(
            payload,
            key=lambda key: len(
                json.dumps(payload[key], ensure_ascii=False, sort_keys=True)
            ),
        )
        payload[widest] = "<truncated>"
        candidate = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if candidate == encoded:
            payload.pop(widest)
            candidate = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        encoded = candidate
    return encoded


@dataclass(frozen=True, slots=True)
class CaseRecord:
    id: int
    case_key: str
    rule: str
    severity: str
    raw_message_id: int | None
    chat_id: int | None
    item_ids: tuple[int, ...]
    batch_ids: tuple[int, ...]
    reason_code: str | None
    target_uncertain: bool
    status: str
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    alerted_at: datetime | None
    resolved_at: datetime | None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: int
    case_id: int | None
    kind: str
    dedupe_key: str | None
    body: str
    status: str
    attempts: int
    delivery_error: str | None


@dataclass(frozen=True, slots=True)
class DiagnosisRecord:
    """What the watcher asked Codex about this case, and what came back."""

    case_id: int
    status: str
    attempts: int
    request_fingerprint: str | None
    prompt_version: str | None
    failure_class: str | None
    skip_reason: str | None
    verdict: dict[str, Any] | None
    message_state: str
    requested_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class WatchItem:
    kind: str
    object_id: int
    chat_id: int | None
    first_seen_at: datetime | None


def _case_from_row(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        id=int(row["id"]),
        case_key=str(row["case_key"]),
        rule=str(row["rule"]),
        severity=str(row["severity"]),
        raw_message_id=(
            int(row["raw_message_id"]) if row["raw_message_id"] is not None else None
        ),
        chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
        item_ids=tuple(_json_int_list(row["item_ids_json"])),
        batch_ids=tuple(_json_int_list(row["batch_ids_json"])),
        reason_code=row["reason_code"],
        target_uncertain=bool(row["target_uncertain"]),
        status=str(row["status"]),
        first_seen_at=parse_isoformat(row["first_seen_at"]),
        last_seen_at=parse_isoformat(row["last_seen_at"]),
        alerted_at=parse_isoformat(row["alerted_at"]),
        resolved_at=parse_isoformat(row["resolved_at"]),
        evidence=_json_dict(row["evidence_json"]),
    )


def _json_int_list(value: Any) -> list[int]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[int] = []
    for item in parsed:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OncallStateStore:
    """Every durable decision the watcher makes, in one small SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection:
            for statement in SCHEMA_STATEMENTS:
                self.connection.execute(statement)

    def close(self) -> None:
        with closing(self.connection):
            pass

    def __enter__(self) -> "OncallStateStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_int_meta(self, key: str, default: int = 0) -> int:
        raw = self.get_meta(key)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return default

    def bump_counter(self, key: str, amount: int = 1) -> int:
        current = self.get_int_meta(key, 0) + int(amount)
        self.set_meta(key, str(current))
        return current

    def get_watermark(self, table: str) -> int | None:
        raw = self.get_meta(f"watermark:{table}")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def set_watermark(self, table: str, last_id: int) -> None:
        """Watermarks only move forward; a stale write must never replay rows."""

        current = self.get_watermark(table)
        if current is not None and int(last_id) <= current:
            return
        self.set_meta(f"watermark:{table}", str(int(last_id)))

    # --------------------------------------------------------- watch items

    def add_watch_item(
        self,
        *,
        kind: str,
        object_id: int,
        chat_id: int | None,
        now: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO watch_items "
                "(kind, object_id, chat_id, status, first_seen_at) "
                "VALUES (?, ?, ?, 'open', ?)",
                (str(kind), int(object_id), chat_id, isoformat(now)),
            )

    def open_watch_items(self, kind: str, limit: int = 500) -> tuple[WatchItem, ...]:
        rows = self.connection.execute(
            "SELECT kind, object_id, chat_id, first_seen_at FROM watch_items "
            "WHERE status = 'open' AND kind = ? ORDER BY object_id LIMIT ?",
            (str(kind), int(limit)),
        ).fetchall()
        return tuple(
            WatchItem(
                kind=str(row["kind"]),
                object_id=int(row["object_id"]),
                chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
                first_seen_at=parse_isoformat(row["first_seen_at"]),
            )
            for row in rows
        )

    def touch_watch_items(self, kind: str, object_ids: Iterable[int], now: datetime) -> None:
        ids = [int(value) for value in object_ids]
        if not ids:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE watch_items SET last_checked_at = ? "
                "WHERE kind = ? AND object_id = ?",
                [(isoformat(now), str(kind), object_id) for object_id in ids],
            )

    def retire_watch_items(self, kind: str, object_ids: Iterable[int]) -> None:
        ids = [int(value) for value in object_ids]
        if not ids:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE watch_items SET status = 'retired' "
                "WHERE kind = ? AND object_id = ?",
                [(str(kind), object_id) for object_id in ids],
            )

    # --------------------------------------------------------------- cases

    def get_case_by_key(self, case_key: str) -> CaseRecord | None:
        row = self.connection.execute(
            "SELECT * FROM cases WHERE case_key = ?", (str(case_key),)
        ).fetchone()
        return _case_from_row(row) if row is not None else None

    def get_case(self, case_id: int) -> CaseRecord | None:
        row = self.connection.execute(
            "SELECT * FROM cases WHERE id = ?", (int(case_id),)
        ).fetchone()
        return _case_from_row(row) if row is not None else None

    def open_cases(self) -> tuple[CaseRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM cases WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return tuple(_case_from_row(row) for row in rows)

    def upsert_case(
        self,
        *,
        case_key: str,
        rule: str,
        severity: str,
        now: datetime,
        raw_message_id: int | None = None,
        chat_id: int | None = None,
        item_ids: Sequence[int] = (),
        batch_ids: Sequence[int] = (),
        reason_code: str | None = None,
        target_uncertain: bool = False,
        evidence: dict[str, Any] | None = None,
        reopen: bool = False,
    ) -> tuple[CaseRecord, bool]:
        """Create the case, or refresh the one already open under this key.

        Returns ``(case, created)``. ``created`` is what decides whether an
        opening alert is composed, so a case re-seen every round for six hours
        still produces exactly one.

        ``reopen`` is for health rules, whose key is the rule itself: a stall
        that comes back is a new episode and must be able to alert again. The
        rule's own cooldown, not the case row, is what stops it flapping. An
        instruction case is never re-opened -- its key names one message and
        one action, and that story ends once.
        """

        existing = self.get_case_by_key(case_key)
        if existing is not None and existing.status != "open":
            if not reopen:
                with self.connection:
                    self.connection.execute(
                        "UPDATE cases SET last_seen_at = ? WHERE id = ?",
                        (isoformat(now), existing.id),
                    )
                return existing, False
            with self.connection:
                self.connection.execute(
                    "UPDATE cases SET status = 'open', alerted_at = NULL, "
                    "resolved_at = NULL, first_seen_at = ?, last_seen_at = ?, "
                    "reason_code = COALESCE(?, reason_code), evidence_json = ? "
                    "WHERE id = ?",
                    (
                        isoformat(now),
                        isoformat(now),
                        reason_code,
                        bounded_evidence_json(evidence or {}),
                        existing.id,
                    ),
                )
            reopened = self.get_case(existing.id)
            assert reopened is not None
            return reopened, True
        merged_items = sorted({*(existing.item_ids if existing else ()), *item_ids})
        merged_batches = sorted({*(existing.batch_ids if existing else ()), *batch_ids})
        if existing is None:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO cases (case_key, rule, severity, raw_message_id, "
                    "chat_id, item_ids_json, batch_ids_json, reason_code, "
                    "target_uncertain, status, first_seen_at, last_seen_at, "
                    "evidence_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                    (
                        str(case_key),
                        str(rule),
                        str(severity),
                        raw_message_id,
                        chat_id,
                        json.dumps(merged_items),
                        json.dumps(merged_batches),
                        reason_code,
                        1 if target_uncertain else 0,
                        isoformat(now),
                        isoformat(now),
                        bounded_evidence_json(evidence or {}),
                    ),
                )
            created = self.get_case(int(cursor.lastrowid))
            assert created is not None
            return created, True

        combined_rules = _combine_rules(existing.rule, rule)
        combined_evidence = dict(existing.evidence)
        combined_evidence.update(evidence or {})
        combined_evidence["rules"] = list(combined_rules.split("+"))
        with self.connection:
            self.connection.execute(
                "UPDATE cases SET rule = ?, severity = ?, item_ids_json = ?, "
                "batch_ids_json = ?, reason_code = COALESCE(?, reason_code), "
                "target_uncertain = ?, last_seen_at = ?, evidence_json = ? "
                "WHERE id = ?",
                (
                    combined_rules,
                    _higher_severity(existing.severity, severity),
                    json.dumps(merged_items),
                    json.dumps(merged_batches),
                    reason_code,
                    1 if (existing.target_uncertain or target_uncertain) else 0,
                    isoformat(now),
                    bounded_evidence_json(combined_evidence),
                    existing.id,
                ),
            )
        refreshed = self.get_case(existing.id)
        assert refreshed is not None
        return refreshed, False

    def mark_case_alerted(self, case_id: int, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE cases SET alerted_at = ? WHERE id = ? AND alerted_at IS NULL",
                (isoformat(now), int(case_id)),
            )

    def resolve_case(self, case_id: int, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE cases SET status = 'resolved', resolved_at = ?, "
                "last_seen_at = ? WHERE id = ? AND status = 'open'",
                (isoformat(now), isoformat(now), int(case_id)),
            )

    def mark_case_stale(self, case_id: int, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE cases SET status = 'stale', last_seen_at = ? "
                "WHERE id = ? AND status = 'open'",
                (isoformat(now), int(case_id)),
            )

    def count_cases_alerted_since(self, *, chat_id: int | None, since: datetime) -> int:
        if chat_id is None:
            return 0
        row = self.connection.execute(
            "SELECT COUNT(*) AS total FROM cases "
            "WHERE chat_id = ? AND alerted_at IS NOT NULL AND alerted_at >= ?",
            (int(chat_id), isoformat(since)),
        ).fetchone()
        return int(row["total"]) if row is not None else 0

    def count_cases_since(self, *, since: datetime, status: str | None = None) -> int:
        if status is None:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM cases WHERE first_seen_at >= ?",
                (isoformat(since),),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM cases "
                "WHERE status = ? AND resolved_at IS NOT NULL AND resolved_at >= ?",
                (str(status), isoformat(since)),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    # -------------------------------------------------------------- alerts

    def enqueue_alert(
        self,
        *,
        kind: str,
        body: str,
        now: datetime,
        case_id: int | None = None,
        dedupe_key: str | None = None,
        status: str = "pending",
    ) -> int | None:
        """Queue one alert; return its id, or ``None`` when already queued.

        The unique ``dedupe_key`` is what makes "one alert per case" survive a
        restart: re-deriving the same key inserts nothing.
        """

        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO alerts "
                "(case_id, kind, dedupe_key, body, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    str(kind),
                    dedupe_key,
                    str(body),
                    str(status),
                    isoformat(now),
                ),
            )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def pending_alerts(self, limit: int = 50, max_attempts: int = 5) -> tuple[AlertRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM alerts WHERE status = 'pending' AND attempts < ? "
            "ORDER BY id LIMIT ?",
            (int(max_attempts), int(limit)),
        ).fetchall()
        return tuple(
            AlertRecord(
                id=int(row["id"]),
                case_id=int(row["case_id"]) if row["case_id"] is not None else None,
                kind=str(row["kind"]),
                dedupe_key=row["dedupe_key"],
                body=str(row["body"]),
                status=str(row["status"]),
                attempts=int(row["attempts"]),
                delivery_error=row["delivery_error"],
            )
            for row in rows
        )

    def mark_alert_sent(self, alert_id: int, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE alerts SET status = 'sent', delivered_at = ?, "
                "attempts = attempts + 1, delivery_error = NULL WHERE id = ?",
                (isoformat(now), int(alert_id)),
            )

    def update_pending_alert_body(self, dedupe_key: str, body: str) -> None:
        """Refresh a queued-but-unsent alert whose facts are still moving.

        The daily-cap notice is the only user: it carries a running count of
        what it is suppressing, and that count keeps rising until the moment
        the message actually goes out.
        """

        with self.connection:
            self.connection.execute(
                "UPDATE alerts SET body = ? WHERE dedupe_key = ? AND status = 'pending'",
                (str(body), str(dedupe_key)),
            )

    def mark_alert_dry_run(self, alert_id: int, now: datetime) -> None:
        """Composed but deliberately not delivered. Never recorded as sent."""

        with self.connection:
            self.connection.execute(
                "UPDATE alerts SET status = 'dry_run', delivered_at = ? WHERE id = ?",
                (isoformat(now), int(alert_id)),
            )

    def mark_alert_failed(
        self, alert_id: int, *, error_type: str, max_attempts: int = 5
    ) -> None:
        """Record only the exception's *type*; a message could carry the URL."""

        with self.connection:
            self.connection.execute(
                "UPDATE alerts SET attempts = attempts + 1, delivery_error = ?, "
                "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END "
                "WHERE id = ?",
                (str(error_type)[:64], int(max_attempts), int(alert_id)),
            )

    # ----------------------------------------------------------- diagnoses

    def get_diagnosis(self, case_id: int) -> DiagnosisRecord | None:
        row = self.connection.execute(
            "SELECT * FROM diagnoses WHERE case_id = ?", (int(case_id),)
        ).fetchone()
        return _diagnosis_from_row(row) if row is not None else None

    def queued_diagnoses(self) -> tuple[DiagnosisRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM diagnoses WHERE status = ? ORDER BY id",
            (DIAGNOSIS_QUEUED,),
        ).fetchall()
        return tuple(_diagnosis_from_row(row) for row in rows)

    def record_diagnosis_request(
        self,
        *,
        case_id: int,
        attempt: int,
        fingerprint: str,
        prompt_version: str,
        now: datetime,
    ) -> None:
        """One row per case; a second attempt updates it rather than adding."""

        with self.connection:
            self.connection.execute(
                "INSERT INTO diagnoses (case_id, status, attempts, "
                "request_fingerprint, prompt_version, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET status = excluded.status, "
                "attempts = excluded.attempts, "
                "request_fingerprint = excluded.request_fingerprint, "
                "prompt_version = excluded.prompt_version, "
                "requested_at = excluded.requested_at, "
                "failure_class = NULL, skip_reason = NULL",
                (
                    int(case_id),
                    DIAGNOSIS_QUEUED,
                    int(attempt),
                    str(fingerprint),
                    str(prompt_version),
                    isoformat(now),
                ),
            )

    def record_diagnosis_result(
        self,
        *,
        case_id: int,
        status: str,
        now: datetime,
        verdict: dict[str, Any] | None = None,
        failure_class: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO diagnoses (case_id, status, attempts, verdict_json, "
                "failure_class, skip_reason, completed_at) "
                "VALUES (?, ?, 0, ?, ?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET status = excluded.status, "
                "verdict_json = COALESCE(excluded.verdict_json, diagnoses.verdict_json), "
                "failure_class = excluded.failure_class, "
                "skip_reason = excluded.skip_reason, "
                "completed_at = excluded.completed_at",
                (
                    int(case_id),
                    str(status),
                    (
                        json.dumps(verdict, ensure_ascii=False, sort_keys=True)
                        if verdict is not None
                        else None
                    ),
                    failure_class,
                    skip_reason,
                    isoformat(now),
                ),
            )

    def set_diagnosis_message_state(self, case_id: int, state: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE diagnoses SET message_state = ? WHERE case_id = ?",
                (str(state), int(case_id)),
            )

    def count_alerts_since(self, *, since: datetime, kinds: Sequence[str] | None = None) -> int:
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            row = self.connection.execute(
                f"SELECT COUNT(*) AS total FROM alerts WHERE created_at >= ? "
                f"AND kind IN ({placeholders})",
                (isoformat(since), *[str(kind) for kind in kinds]),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM alerts WHERE created_at >= ?",
                (isoformat(since),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0


def _diagnosis_from_row(row: sqlite3.Row) -> DiagnosisRecord:
    return DiagnosisRecord(
        case_id=int(row["case_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        request_fingerprint=row["request_fingerprint"],
        prompt_version=row["prompt_version"],
        failure_class=row["failure_class"],
        skip_reason=row["skip_reason"],
        verdict=_json_dict(row["verdict_json"]) or None,
        message_state=str(row["message_state"]),
        requested_at=parse_isoformat(row["requested_at"]),
        completed_at=parse_isoformat(row["completed_at"]),
    )


def _combine_rules(existing: str, incoming: str) -> str:
    """The union of both sides, each split on ``+`` first.

    ``incoming`` is itself a joined string whenever one round merges several
    observations. Compared whole against the parts of ``existing`` it never
    matched, so it was appended again every round: production, 2026-09-21,
    case 4 grew to several hundred ``D1a+`` in a day.
    """

    parts = {
        part
        for side in (existing, incoming)
        for part in str(side or "").split("+")
        if part
    }
    return "+".join(sorted(parts))


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _higher_severity(left: str, right: str) -> str:
    return (
        left
        if _SEVERITY_ORDER.get(left, 0) >= _SEVERITY_ORDER.get(right, 0)
        else right
    )
