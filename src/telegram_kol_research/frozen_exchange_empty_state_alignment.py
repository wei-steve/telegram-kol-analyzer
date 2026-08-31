"""One exact L3 convergence from frozen local claims to an empty exchange."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


ENTERED_LIFECYCLE_IDS = (
    444, 547, 558, 623, 707, 713, 724, 736, 763, 767,
    772, 777, 804, 807, 985, 1012, 1023, 1026, 1034, 1035,
)
BINDING_TO_LIFECYCLE = {
    98: 423, 101: 426, 114: 444, 116: 447, 119: 452,
    121: 460, 128: 469, 145: 508, 146: 509, 147: 510, 289: 839,
}
PENDING_BINDING_IDS = (98, 101, 116, 119, 121, 128, 145, 146, 147, 289)
PENDING_LEG_IDS = (
    192, 193, 198, 199, 225, 226, 230, 231, 234,
    235, 248, 249, 279, 280, 281, 506, 507,
)
HISTORICAL_LEG_IDS = (222, 223)
INTENT_IDS = (128, 129)
PROTECTION_LEG_IDS = (545, 546, 547, 548, 549, 550, 551, 552)
CONVERGENCE_IDS = (149, 150)
EXPECTED_CHANGED_ROWS = 72

_TERMINAL_BINDING_STATES = (
    "closed", "cancelled", "canceled", "completed", "failed",
    "resolved", "superseded", "expired", "rejected",
)
_DRIFT_FIELDS = frozenset({"updated_at", "recovered_at"})
_TARGET_TABLES = {
    "strategy_lifecycles": tuple(
        sorted(set(ENTERED_LIFECYCLE_IDS) | set(BINDING_TO_LIFECYCLE.values()))
    ),
    "execution_bindings": tuple(sorted(BINDING_TO_LIFECYCLE)),
    "execution_order_legs": tuple(sorted(PENDING_LEG_IDS + HISTORICAL_LEG_IDS)),
    "trigger_protection_intents": INTENT_IDS,
    "position_protection_legs": PROTECTION_LEG_IDS,
    "trigger_take_profit_convergences": CONVERGENCE_IDS,
}


class AlignmentRefused(RuntimeError):
    """Raised before commit when an exact safety assertion fails."""


@dataclass(frozen=True, slots=True)
class AlignmentInspection:
    fingerprint: str
    action_count: int
    exchange_fingerprint: str
    target_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    status: str
    changed_rows_by_table: Mapping[str, int]
    execution_audit_rows: int
    attribution_audit_rows: int


def inspect_alignment(
    database_path: str | Path,
    *,
    exchange_evidence: Mapping[str, Any],
    observed_at: datetime,
    code_sha: str,
) -> AlignmentInspection:
    """Read and fingerprint the exact cohort; never opens the DB writable."""

    resolved = Path(database_path).expanduser().resolve()
    if not resolved.is_file():
        raise AlignmentRefused("database_missing")
    exchange = _validate_exchange(exchange_evidence, now=observed_at)
    _validate_sha(code_sha)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise AlignmentRefused("query_only_unavailable")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AlignmentRefused("quick_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AlignmentRefused("foreign_key_check_failed")
        state = _read_and_validate_state(connection)
    finally:
        connection.close()
    return AlignmentInspection(
        fingerprint=_fingerprint({"code_sha": code_sha, "state": state}),
        action_count=EXPECTED_CHANGED_ROWS,
        exchange_fingerprint=_fingerprint(exchange),
        target_counts={name: len(rows) for name, rows in state.items()},
    )


def apply_alignment(
    database_path: str | Path,
    *,
    exchange_evidence: Mapping[str, Any],
    expected_fingerprint: str,
    repair_ts: datetime,
    code_sha: str,
    applied_at: datetime | None = None,
    fail_after_step: int | None = None,
) -> AlignmentResult:
    """Validate and commit the fixed cohort in one BEGIN IMMEDIATE transaction."""

    resolved = Path(database_path).expanduser().resolve()
    _validate_sha(code_sha)
    now = applied_at or datetime.now(UTC)
    _validate_exchange(exchange_evidence, now=now)
    if repair_ts.tzinfo is None:
        raise AlignmentRefused("repair_timestamp_not_utc")
    repair_value = _database_timestamp(repair_ts)
    connection = sqlite3.connect(resolved)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    changed: dict[str, int] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_and_validate_state(connection)
        actual = _fingerprint({"code_sha": code_sha, "state": state})
        if actual != expected_fingerprint:
            raise AlignmentRefused("database_state_changed")
        _require_audits_absent(
            connection, code_sha=code_sha, repair_value=repair_value
        )
        steps = (
            ("strategy_lifecycles", _update_entered_lifecycles),
            ("strategy_lifecycles", _update_pending_lifecycles),
            ("execution_bindings", _update_bindings),
            ("execution_order_legs", _update_order_legs),
            ("trigger_protection_intents", _update_intents),
            ("position_protection_legs", _update_protection_legs),
            ("trigger_take_profit_convergences", _update_convergences),
        )
        for index, (table, operation) in enumerate(steps, 1):
            count = operation(connection, repair_value)
            changed[table] = changed.get(table, 0) + count
            if fail_after_step == index:
                raise RuntimeError("injected apply failure")
        execution_count = _insert_execution_audits(
            connection, state=state, code_sha=code_sha, repair_value=repair_value
        )
        attribution_count = _insert_attribution_audits(
            connection, code_sha=code_sha, repair_value=repair_value
        )
        if sum(changed.values()) != EXPECTED_CHANGED_ROWS:
            raise AlignmentRefused("changed_row_count_mismatch")
        _validate_postconditions(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AlignmentRefused("foreign_key_check_failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return AlignmentResult(
        status="applied",
        changed_rows_by_table=dict(sorted(changed.items())),
        execution_audit_rows=execution_count,
        attribution_audit_rows=attribution_count,
    )


def _read_and_validate_state(
    connection: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    state = {
        table: _read_rows(connection, table, ids)
        for table, ids in _TARGET_TABLES.items()
    }
    entered = tuple(
        row[0]
        for row in connection.execute(
            "SELECT id FROM strategy_lifecycles "
            "WHERE lifecycle_status='entered' ORDER BY id"
        )
    )
    if entered != ENTERED_LIFECYCLE_IDS:
        raise AlignmentRefused("entered_lifecycle_set_changed")
    terminal_slots = ",".join("?" for _ in _TERMINAL_BINDING_STATES)
    active_bindings = tuple(
        row[0]
        for row in connection.execute(
            "SELECT id FROM execution_bindings WHERE venue='deepcoin' "
            f"AND lower(status) NOT IN ({terminal_slots}) ORDER BY id",
            _TERMINAL_BINDING_STATES,
        )
    )
    if active_bindings != tuple(sorted(BINDING_TO_LIFECYCLE)):
        raise AlignmentRefused("active_binding_set_changed")
    binding_slots = ",".join("?" for _ in BINDING_TO_LIFECYCLE)
    for table, expected, reason in (
        (
            "execution_order_legs",
            tuple(sorted(PENDING_LEG_IDS + HISTORICAL_LEG_IDS)),
            "entry_leg_set_changed",
        ),
        ("trigger_protection_intents", INTENT_IDS, "trigger_intent_set_changed"),
        ("position_protection_legs", PROTECTION_LEG_IDS, "protection_leg_set_changed"),
        (
            "trigger_take_profit_convergences",
            CONVERGENCE_IDS,
            "convergence_set_changed",
        ),
    ):
        ids = tuple(
            row[0]
            for row in connection.execute(
                f"SELECT id FROM {table} WHERE execution_binding_id IN "
                f"({binding_slots}) ORDER BY id",
                tuple(BINDING_TO_LIFECYCLE),
            )
        )
        if ids != expected:
            raise AlignmentRefused(reason)

    lifecycles = {row["id"]: row for row in state["strategy_lifecycles"]}
    bindings = {row["id"]: row for row in state["execution_bindings"]}
    legs = {row["id"]: row for row in state["execution_order_legs"]}
    for lifecycle_id in ENTERED_LIFECYCLE_IDS:
        row = lifecycles[lifecycle_id]
        expected_binding = 114 if lifecycle_id == 444 else None
        if (
            row["lifecycle_status"] != "entered"
            or row["execution_binding_id"] != expected_binding
        ):
            raise AlignmentRefused("entered_lifecycle_changed")
    expected_binding_status = {
        98: "stale",
        101: "stale",
        114: "unknown",
        116: "stale",
    }
    for binding_id, lifecycle_id in BINDING_TO_LIFECYCLE.items():
        lifecycle = lifecycles[lifecycle_id]
        binding = bindings[binding_id]
        if lifecycle["execution_binding_id"] != binding_id:
            raise AlignmentRefused("binding_lifecycle_identity_changed")
        if binding_id != 114 and lifecycle["lifecycle_status"] != "pending_entry":
            raise AlignmentRefused("pending_lifecycle_changed")
        if (
            binding["venue"] != "deepcoin"
            or binding["status"] != expected_binding_status.get(binding_id, "open")
            or binding["pos_id"] is not None
        ):
            raise AlignmentRefused("binding_state_changed")
    expected_leg_binding = {
        192: 98, 193: 98, 198: 101, 199: 101, 222: 114, 223: 114,
        225: 116, 226: 116, 230: 119, 231: 119, 234: 121, 235: 121,
        248: 128, 249: 128, 279: 145, 280: 146, 281: 147, 506: 289, 507: 289,
    }
    for leg_id, binding_id in expected_leg_binding.items():
        row = legs[leg_id]
        if (
            row["execution_binding_id"] != binding_id
            or row["venue"] != "deepcoin"
            or row["purpose"] != "entry"
            or row["order_kind"] != "trigger_limit"
        ):
            raise AlignmentRefused("entry_leg_identity_changed")
    if not (
        legs[222]["status"] == "active"
        and legs[222]["attribution_status"] == "attribution_conflict"
        and legs[222]["pos_id"] == "1001124072502100"
        and legs[223]["status"] == "unknown"
        and legs[223]["pos_id"] is None
    ):
        raise AlignmentRefused("historical_leg_state_changed")
    for leg_id in PENDING_LEG_IDS:
        if (
            legs[leg_id]["status"] not in {"unknown", "pending", "cancelled"}
            or legs[leg_id]["pos_id"] is not None
        ):
            raise AlignmentRefused("pending_leg_state_changed")
    for row in state["trigger_protection_intents"]:
        if row["execution_binding_id"] != 289 or row["recovery_state"] != "pending":
            raise AlignmentRefused("trigger_intent_set_changed")
    for row in state["position_protection_legs"]:
        if row["execution_binding_id"] != 289 or row["status"] != "planned":
            raise AlignmentRefused("protection_leg_set_changed")
    for row in state["trigger_take_profit_convergences"]:
        if (
            row["execution_binding_id"] != 289
            or row["status"] != "waiting_backup_stop"
        ):
            raise AlignmentRefused("convergence_set_changed")
    return {
        table: [_without_runtime_timestamps(row) for row in rows]
        for table, rows in state.items()
    }


def _update_entered_lifecycles(connection, repair_value: str) -> int:
    return _update_ids(
        connection,
        "strategy_lifecycles",
        ENTERED_LIFECYCLE_IDS,
        "lifecycle_status='exited',exit_reason='exchange_closed',"
        "exited_at=?,updated_at=?",
        (repair_value, repair_value),
        "lifecycle_status='entered'",
    )


def _update_pending_lifecycles(connection, repair_value: str) -> int:
    return _update_ids(
        connection,
        "strategy_lifecycles",
        tuple(BINDING_TO_LIFECYCLE[row] for row in PENDING_BINDING_IDS),
        "lifecycle_status='expired',exit_reason='expired',exited_at=?,"
        "management_action='operator_cancelled_pending_entries',"
        "management_note='All unfilled entry orders were cancelled at Deepcoin.',"
        "expiry_review_next_at=NULL,updated_at=?",
        (repair_value, repair_value),
        "lifecycle_status='pending_entry'",
    )


def _update_bindings(connection, repair_value: str) -> int:
    historical = _update_ids(
        connection,
        "execution_bindings",
        (114,),
        "status='closed',pos_id=NULL,last_exchange_status='historical_cleanup_terminal',"
        "recovered_at=?,updated_at=?",
        (repair_value, repair_value),
        "status='unknown' AND pos_id IS NULL",
    )
    pending = _update_ids(
        connection,
        "execution_bindings",
        PENDING_BINDING_IDS,
        "status='cancelled',pos_id=NULL,"
        "last_exchange_status='operator_cancelled_pending_entries',updated_at=?",
        (repair_value,),
        "pos_id IS NULL",
    )
    return historical + pending


def _update_order_legs(connection, repair_value: str) -> int:
    historical = _update_ids(
        connection,
        "execution_order_legs",
        HISTORICAL_LEG_IDS,
        "status='closed',terminal_reason='historical_exchange_position_closed',"
        "last_verified_at=?,updated_at=?",
        (repair_value, repair_value),
        "purpose='entry'",
    )
    pending = _update_ids(
        connection,
        "execution_order_legs",
        PENDING_LEG_IDS,
        "status='cancelled',terminal_reason='operator_cancelled_unfilled_entry_leg',"
        "last_verified_at=?,updated_at=?",
        (repair_value, repair_value),
        "purpose='entry' AND pos_id IS NULL",
    )
    return historical + pending


def _update_intents(connection, repair_value: str) -> int:
    return _update_ids(
        connection,
        "trigger_protection_intents",
        INTENT_IDS,
        "recovery_state='resolved',recovery_disposition='terminal',"
        "last_reason_code='parent_trigger_cancelled_before_entry',"
        "next_attempt_at=NULL,updated_at=?",
        (repair_value,),
        "recovery_state='pending'",
    )


def _update_protection_legs(connection, repair_value: str) -> int:
    return _update_ids(
        connection,
        "position_protection_legs",
        PROTECTION_LEG_IDS,
        "status='cancelled',updated_at=?",
        (repair_value,),
        "status='planned'",
    )


def _update_convergences(connection, repair_value: str) -> int:
    return _update_ids(
        connection,
        "trigger_take_profit_convergences",
        CONVERGENCE_IDS,
        "status='completed',reason_code='parent_trigger_cancelled_before_entry',"
        "completed_at=?,updated_at=?",
        (repair_value, repair_value),
        "status='waiting_backup_stop'",
    )


def _insert_execution_audits(
    connection, *, state, code_sha: str, repair_value: str
) -> int:
    bindings = {row["id"]: row for row in state["execution_bindings"]}
    lifecycles = {row["id"]: row for row in state["strategy_lifecycles"]}
    legs = {row["id"]: row for row in state["execution_order_legs"]}
    rows = []
    for leg_id in PENDING_LEG_IDS:
        leg = legs[leg_id]
        binding_id = leg["execution_binding_id"]
        binding = bindings[binding_id]
        lifecycle = lifecycles[BINDING_TO_LIFECYCLE[binding_id]]
        rows.append(
            (
                binding_id,
                binding.get("strategy_instance_id"),
                "deepcoin",
                "reconcile_manual_pending_entry_cancel",
                "confirmed",
                lifecycle.get("chat_id"),
                lifecycle.get("message_id"),
                binding.get("symbol"),
                binding.get("side"),
                leg.get("order_id"),
                "operator_confirmed_all_entry_orders_cancelled",
                _canonical({"pending": False, "terminalized": True}),
                None,
                repair_value,
                "not_needed",
                _audit_hash(code_sha, repair_value, "pending", leg_id),
                0,
            )
        )
    rows.append(
        (
            None,
            None,
            "deepcoin",
            "historical_state_convergence_repair",
            "completed",
            None,
            None,
            None,
            None,
            None,
            "supervised_historical_state_repair",
            None,
            _canonical(
                {
                    "code_sha": code_sha,
                    "entered_lifecycle_ids": ENTERED_LIFECYCLE_IDS,
                    "binding_ids": tuple(sorted(BINDING_TO_LIFECYCLE)),
                    "reason": "complete_exchange_snapshot_empty",
                }
            ),
            repair_value,
            "not_needed",
            _audit_hash(code_sha, repair_value, "summary", 0),
            0,
        )
    )
    connection.executemany(
        "INSERT INTO execution_events "
        "(execution_binding_id,strategy_instance_id,venue,action,status,chat_id,"
        "message_id,symbol,side,order_id,reason,after_json,response_json,created_at,"
        "notification_status,notification_fingerprint,notification_attempts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _insert_attribution_audits(
    connection, *, code_sha: str, repair_value: str
) -> int:
    specs = (
        (222, "1001124072502100", "active", "closed", "terminalize_historical_entry_leg"),
        (223, None, "unknown", "closed", "terminalize_historical_entry_leg"),
        (None, None, "unknown", "closed", "close_historical_binding"),
        (None, None, "entered", "exited", "exit_historical_lifecycle"),
    )
    rows = []
    for index, (leg_id, pos_id, prior, new, action) in enumerate(specs, 1):
        rows.append(
            (
                114,
                leg_id,
                "deepcoin",
                pos_id,
                "historical_cleanup",
                prior,
                new,
                _audit_hash(code_sha, repair_value, "historical", index),
                _canonical(
                    {
                        "action": action,
                        "code_sha": code_sha,
                        "lifecycle_id": 444,
                        "old_pos_id": pos_id,
                        "new_pos_id": pos_id,
                        "terminal_evidence": {
                            "source": "complete_exchange_snapshot_empty",
                            "reason": "exchange_closed",
                        },
                    }
                ),
                repair_value,
            )
        )
    connection.executemany(
        "INSERT INTO position_attribution_audits "
        "(execution_binding_id,execution_order_leg_id,venue,pos_id,event_type,"
        "prior_state,new_state,fingerprint,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _require_audits_absent(
    connection, *, code_sha: str, repair_value: str
) -> None:
    execution_hashes = [
        _audit_hash(code_sha, repair_value, "pending", leg_id)
        for leg_id in PENDING_LEG_IDS
    ] + [_audit_hash(code_sha, repair_value, "summary", 0)]
    attribution_hashes = [
        _audit_hash(code_sha, repair_value, "historical", index)
        for index in range(1, 5)
    ]
    for table, column, hashes in (
        ("execution_events", "notification_fingerprint", execution_hashes),
        ("position_attribution_audits", "fingerprint", attribution_hashes),
    ):
        slots = ",".join("?" for _ in hashes)
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE {column} IN ({slots}) LIMIT 1", hashes
        ).fetchone() is not None:
            raise AlignmentRefused("audit_already_exists")


def _validate_postconditions(connection) -> None:
    if connection.execute(
        "SELECT 1 FROM strategy_lifecycles "
        "WHERE lifecycle_status='entered' LIMIT 1"
    ).fetchone() is not None:
        raise AlignmentRefused("entered_lifecycle_remains")
    slots = ",".join("?" for _ in _TERMINAL_BINDING_STATES)
    if connection.execute(
        "SELECT 1 FROM execution_bindings WHERE venue='deepcoin' "
        f"AND lower(status) NOT IN ({slots}) LIMIT 1",
        _TERMINAL_BINDING_STATES,
    ).fetchone() is not None:
        raise AlignmentRefused("active_binding_remains")
    binding_ids = tuple(sorted(BINDING_TO_LIFECYCLE))
    binding_slots = ",".join("?" for _ in binding_ids)
    if connection.execute(
        "SELECT 1 FROM execution_order_legs WHERE execution_binding_id IN "
        f"({binding_slots}) AND lower(status) NOT IN "
        "('closed','cancelled','canceled','expired','rejected','filled') LIMIT 1",
        binding_ids,
    ).fetchone() is not None:
        raise AlignmentRefused("active_order_leg_remains")


def _read_rows(connection, table: str, ids) -> list[dict[str, Any]]:
    slots = ",".join("?" for _ in ids)
    rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE id IN ({slots}) ORDER BY id", tuple(ids)
        )
    ]
    if tuple(row["id"] for row in rows) != tuple(sorted(ids)):
        raise AlignmentRefused(f"{table}_target_set_changed")
    return rows


def _update_ids(connection, table, ids, assignments, values, predicate) -> int:
    slots = ",".join("?" for _ in ids)
    cursor = connection.execute(
        f"UPDATE {table} SET {assignments} "
        f"WHERE id IN ({slots}) AND {predicate}",
        tuple(values) + tuple(ids),
    )
    if cursor.rowcount != len(ids):
        raise AlignmentRefused(f"{table}_compare_and_set_failed")
    return cursor.rowcount


def _validate_exchange(value, *, now: datetime) -> dict[str, Any]:
    if now.tzinfo is None:
        raise AlignmentRefused("snapshot_timestamp_invalid")
    payload = json.loads(_canonical(value))
    if (
        payload.get("snapshot_complete") is not True
        or payload.get("snapshot_errors") != {}
        or payload.get("exchange_write_count") != 0
    ):
        raise AlignmentRefused("exchange_snapshot_incomplete")
    for key in ("positions", "open_orders", "pending_trigger_orders"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise AlignmentRefused("exchange_snapshot_incomplete")
        if rows:
            raise AlignmentRefused("exchange_account_not_flat")
    captured = _parse_utc(payload.get("captured_at"))
    current = now.astimezone(UTC)
    if captured > current + timedelta(seconds=30):
        raise AlignmentRefused("snapshot_timestamp_invalid")
    if current - captured > timedelta(minutes=2):
        raise AlignmentRefused("exchange_snapshot_not_fresh")
    return payload


def _without_runtime_timestamps(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _DRIFT_FIELDS}


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise AlignmentRefused("code_sha_invalid")


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlignmentRefused("snapshot_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise AlignmentRefused("snapshot_timestamp_invalid")
    return parsed.astimezone(UTC)


def _database_timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="microseconds")
    )


def _audit_hash(code_sha: str, repair_value: str, kind: str, identity: int) -> str:
    value = f"{code_sha}:{repair_value}:{kind}:{identity}"
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exact frozen empty-exchange alignment")
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--exchange-evidence", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(
            Path(args.exchange_evidence).read_text(encoding="utf-8")
        )
        observed_at = _parse_utc(args.observed_at)
        inspection = inspect_alignment(
            args.database_path,
            exchange_evidence=evidence,
            observed_at=observed_at,
            code_sha=args.code_sha,
        )
        if not args.apply:
            print(json.dumps(asdict(inspection), sort_keys=True))
            return 0
        if not args.expected_fingerprint:
            raise AlignmentRefused("expected_fingerprint_required")
        result = apply_alignment(
            args.database_path,
            exchange_evidence=evidence,
            expected_fingerprint=args.expected_fingerprint,
            repair_ts=observed_at,
            code_sha=args.code_sha,
        )
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    except (AlignmentRefused, OSError, json.JSONDecodeError) as exc:
        print(f"Refusing: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
