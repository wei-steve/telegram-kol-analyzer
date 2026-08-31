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
    444, 536, 547, 558, 607, 611, 623, 698, 707, 713, 724, 736, 763,
    767, 772, 777, 804, 807, 985, 1012, 1023, 1026, 1034, 1035, 1036,
)
PENDING_LIFECYCLE_IDS = (423, 426, 447, 452, 460, 469, 508, 509, 510, 839)
POSITION_BINDING_IDS = (
    2, 3, 5, 6, 10, 15, 17, 18, 22, 24, 26, 27, 39, 114, 120,
)
ORDER_BINDING_IDS = (
    4, 16, 19, 21, 25, 28, 31, 34, 36, 41, 43, 50, 54, 70, 80, 86,
    94, 98, 101, 102, 105, 108, 116, 118, 119, 121, 128, 145, 146, 147,
    289,
)
TARGET_BINDING_IDS = tuple(sorted(POSITION_BINDING_IDS + ORDER_BINDING_IDS))
BINDING_TO_LIFECYCLE = {
    6: 297, 15: 313, 16: 314, 17: 315, 18: 317, 19: 318, 21: 320,
    22: 323, 24: 326, 25: 327, 26: 329, 27: 331, 28: 332, 31: 335,
    34: 338, 36: 342, 39: 345, 41: 348, 43: 351, 50: 363, 54: 368,
    70: 387, 80: 398, 86: 405, 94: 416, 98: 423, 101: 426, 102: 427,
    105: 432, 108: 436, 114: 444, 116: 447, 118: 449, 119: 452,
    120: 457, 121: 460, 128: 469, 145: 508, 146: 509, 147: 510,
    289: 839,
}
POSITION_LEG_IDS = (2, 3, 6, 7, 12, 17, 20, 21, 28, 32, 36, 37, 59, 222, 232)
ORDER_LEG_IDS = (
    4, 5, 18, 19, 22, 23, 26, 27, 29, 33, 34, 35, 38, 39, 40, 44,
    45, 50, 51, 54, 55, 61, 62, 64, 65, 77, 78, 85, 86, 87, 88, 89,
    90, 142, 143, 161, 162, 184, 185, 192, 193, 198, 199, 200, 201,
    206, 207, 212, 213, 223, 225, 226, 228, 229, 230, 231, 233, 234,
    235, 248, 249, 279, 280, 281, 506,
)
TERMINAL_GUARD_LEG_IDS = (171, 172, 507)
TARGET_LEG_IDS = tuple(sorted(POSITION_LEG_IDS + ORDER_LEG_IDS))
GUARD_LEG_IDS = tuple(sorted(TARGET_LEG_IDS + TERMINAL_GUARD_LEG_IDS))
INTENT_IDS = (128, 129)
PROTECTION_LEG_IDS = (545, 546, 547, 548, 549, 550, 551, 552)
CONVERGENCE_IDS = (149, 150)
EXPECTED_CHANGED_ROWS = 173
PENDING_TRIGGER_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)

_TERMINAL_BINDING_STATES = (
    "closed", "cancelled", "canceled", "completed", "failed",
    "resolved", "superseded", "expired", "rejected",
)
_TERMINAL_ENTRY_LEG_STATES = (
    "cancelled", "manually_cancelled", "exchange_cancelled",
    "manually_closed", "closed", "expired", "invalidated",
)
_UNRESOLVED_ORDER_LEG_STATES = (
    "unknown", "pending", "submitted", "open",
    "partially_filled", "partial_filled", "partial",
)
_TERMINAL_INTENT_STATES = (
    "resolved", "completed", "cancelled", "canceled", "expired",
    "rejected", "failed",
)
_TERMINAL_DEPENDENT_STATES = (
    "closed", "cancelled", "canceled", "completed", "failed", "resolved",
    "superseded", "expired", "rejected", "filled",
)
_DRIFT_FIELDS = frozenset({"updated_at", "recovered_at"})
GUARD_LIFECYCLE_IDS = tuple(
    sorted(set(ENTERED_LIFECYCLE_IDS) | set(BINDING_TO_LIFECYCLE.values()))
)
_TARGET_TABLES = {
    "strategy_lifecycles": GUARD_LIFECYCLE_IDS,
    "execution_bindings": TARGET_BINDING_IDS,
    "execution_order_legs": GUARD_LEG_IDS,
    "trigger_protection_intents": INTENT_IDS,
    "position_protection_legs": PROTECTION_LEG_IDS,
    "trigger_take_profit_convergences": CONVERGENCE_IDS,
}
_EXPECTED_TARGET_COUNTS = {
    "entered_lifecycles": 25,
    "pending_lifecycles": 10,
    "execution_bindings": 46,
    "execution_order_legs": 80,
    "trigger_protection_intents": 2,
    "position_protection_legs": 8,
    "trigger_take_profit_convergences": 2,
}


class AlignmentRefused(RuntimeError):
    """Raised before commit when an exact safety assertion fails."""


@dataclass(frozen=True, slots=True)
class AlignmentInspection:
    fingerprint: str
    action_count: int
    exchange_fingerprint: str
    target_counts: Mapping[str, int]
    guard_counts: Mapping[str, int]


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
        target_counts=dict(_EXPECTED_TARGET_COUNTS),
        guard_counts={name: len(rows) for name, rows in state.items()},
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
            connection, state=state, code_sha=code_sha, repair_value=repair_value
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
            connection, state=state, code_sha=code_sha, repair_value=repair_value
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
    _derive_and_validate_targets(connection)
    state = {
        table: _read_rows(connection, table, ids)
        for table, ids in _TARGET_TABLES.items()
    }
    return {
        table: [_without_runtime_timestamps(row) for row in rows]
        for table, rows in state.items()
    }


def _derive_and_validate_targets(connection: sqlite3.Connection) -> None:
    entered = _query_ids(
        connection,
        "SELECT id FROM strategy_lifecycles "
        "WHERE lifecycle_status='entered' ORDER BY id",
    )
    if entered != ENTERED_LIFECYCLE_IDS:
        raise AlignmentRefused("entered_lifecycle_target_set_changed")

    position_bindings, order_bindings = _query_binding_claims(connection)
    if position_bindings != POSITION_BINDING_IDS:
        raise AlignmentRefused("position_binding_target_set_changed")
    if order_bindings != ORDER_BINDING_IDS:
        raise AlignmentRefused("order_binding_target_set_changed")

    binding_slots = _slots(TARGET_BINDING_IDS)
    relationship_rows = tuple(
        (int(row[0]), int(row[1]))
        for row in connection.execute(
            "SELECT execution_binding_id,id FROM strategy_lifecycles "
            f"WHERE execution_binding_id IN ({binding_slots}) "
            "ORDER BY execution_binding_id,id",
            TARGET_BINDING_IDS,
        )
    )
    if relationship_rows != tuple(sorted(BINDING_TO_LIFECYCLE.items())):
        raise AlignmentRefused("binding_lifecycle_relationship_set_changed")

    pending_lifecycles = _query_ids(
        connection,
        "SELECT id FROM strategy_lifecycles "
        f"WHERE execution_binding_id IN ({binding_slots}) "
        "AND lifecycle_status='pending_entry' ORDER BY id",
        TARGET_BINDING_IDS,
    )
    if pending_lifecycles != PENDING_LIFECYCLE_IDS:
        raise AlignmentRefused("pending_lifecycle_target_set_changed")

    terminal_leg_slots = _slots(_TERMINAL_ENTRY_LEG_STATES)
    guard_legs = _query_ids(
        connection,
        "SELECT id FROM execution_order_legs "
        f"WHERE execution_binding_id IN ({binding_slots}) "
        "AND purpose='entry' ORDER BY id",
        TARGET_BINDING_IDS,
    )
    if guard_legs != GUARD_LEG_IDS:
        raise AlignmentRefused("entry_leg_guard_set_changed")
    target_legs = _query_ids(
        connection,
        "SELECT id FROM execution_order_legs "
        f"WHERE execution_binding_id IN ({binding_slots}) "
        "AND purpose='entry' "
        f"AND lower(status) NOT IN ({terminal_leg_slots}) ORDER BY id",
        TARGET_BINDING_IDS + _TERMINAL_ENTRY_LEG_STATES,
    )
    if target_legs != TARGET_LEG_IDS:
        raise AlignmentRefused("entry_leg_target_set_changed")
    position_legs = _query_ids(
        connection,
        "SELECT id FROM execution_order_legs "
        f"WHERE id IN ({_slots(TARGET_LEG_IDS)}) "
        "AND nullif(trim(pos_id),'') IS NOT NULL ORDER BY id",
        TARGET_LEG_IDS,
    )
    if position_legs != POSITION_LEG_IDS:
        raise AlignmentRefused("position_leg_target_set_changed")
    if tuple(row for row in target_legs if row not in set(position_legs)) != ORDER_LEG_IDS:
        raise AlignmentRefused("order_leg_target_set_changed")

    dependent_specs = (
        (
            "trigger_protection_intents",
            "recovery_state",
            _TERMINAL_INTENT_STATES,
            INTENT_IDS,
            "trigger_intent_target_set_changed",
        ),
        (
            "position_protection_legs",
            "status",
            _TERMINAL_DEPENDENT_STATES,
            PROTECTION_LEG_IDS,
            "protection_leg_target_set_changed",
        ),
        (
            "trigger_take_profit_convergences",
            "status",
            _TERMINAL_DEPENDENT_STATES,
            CONVERGENCE_IDS,
            "convergence_target_set_changed",
        ),
    )
    for table, status_column, terminal_states, expected, reason in dependent_specs:
        ids = _query_ids(
            connection,
            f"SELECT id FROM {table} "
            f"WHERE execution_binding_id IN ({binding_slots}) "
            f"AND lower({status_column}) NOT IN ({_slots(terminal_states)}) "
            "ORDER BY id",
            TARGET_BINDING_IDS + terminal_states,
        )
        if ids != expected:
            raise AlignmentRefused(reason)


def _query_binding_claims(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    binding_terminal_slots = _slots(_TERMINAL_BINDING_STATES)
    leg_terminal_slots = _slots(_TERMINAL_ENTRY_LEG_STATES)
    unresolved_order_slots = _slots(_UNRESOLVED_ORDER_LEG_STATES)
    intent_terminal_slots = _slots(_TERMINAL_INTENT_STATES)
    dependent_terminal_slots = _slots(_TERMINAL_DEPENDENT_STATES)
    rows = connection.execute(
        "WITH nonterminal AS ("
        " SELECT b.* FROM execution_bindings b WHERE b.venue='deepcoin' "
        f" AND lower(b.status) NOT IN ({binding_terminal_slots})"
        ") SELECT b.id,"
        " CASE WHEN nullif(trim(b.pos_id),'') IS NOT NULL"
        "   OR EXISTS (SELECT 1 FROM strategy_lifecycles s"
        "              WHERE s.execution_binding_id=b.id"
        "              AND s.lifecycle_status='entered')"
        "   OR EXISTS (SELECT 1 FROM execution_order_legs l"
        "              WHERE l.execution_binding_id=b.id"
        "              AND l.purpose='entry'"
        "              AND nullif(trim(l.pos_id),'') IS NOT NULL"
        f"              AND lower(l.status) NOT IN ({leg_terminal_slots}))"
        " THEN 1 ELSE 0 END AS claims_position,"
        " CASE WHEN lower(b.status) IN ('open','active')"
        "   OR EXISTS (SELECT 1 FROM execution_order_legs l"
        "              WHERE l.execution_binding_id=b.id"
        "              AND l.purpose='entry'"
        "              AND nullif(trim(l.order_id),'') IS NOT NULL"
        f"              AND lower(l.status) IN ({unresolved_order_slots}))"
        "   OR EXISTS (SELECT 1 FROM trigger_protection_intents i"
        "              WHERE i.execution_binding_id=b.id"
        "              AND (nullif(trim(i.parent_trigger_order_id),'') IS NOT NULL"
        "                   OR nullif(trim(i.adopted_order_id),'') IS NOT NULL)"
        f"              AND lower(i.recovery_state) NOT IN ({intent_terminal_slots}))"
        "   OR EXISTS (SELECT 1 FROM position_protection_legs p"
        "              WHERE p.execution_binding_id=b.id"
        "              AND nullif(trim(p.exchange_order_id),'') IS NOT NULL"
        f"              AND lower(p.status) NOT IN ({dependent_terminal_slots}))"
        " THEN 1 ELSE 0 END AS claims_order"
        " FROM nonterminal b ORDER BY b.id",
        (
            _TERMINAL_BINDING_STATES
            + _TERMINAL_ENTRY_LEG_STATES
            + _UNRESOLVED_ORDER_LEG_STATES
            + _TERMINAL_INTENT_STATES
            + _TERMINAL_DEPENDENT_STATES
        ),
    )
    position: list[int] = []
    order: list[int] = []
    for binding_id, claims_position, claims_order in rows:
        if claims_position:
            position.append(int(binding_id))
        elif claims_order:
            order.append(int(binding_id))
    return tuple(position), tuple(order)


def _query_ids(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> tuple[int, ...]:
    return tuple(int(row[0]) for row in connection.execute(sql, parameters))


def _slots(values: tuple[Any, ...]) -> str:
    return ",".join("?" for _ in values)


def _sql_literals(values: tuple[str, ...]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)


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
        PENDING_LIFECYCLE_IDS,
        "lifecycle_status='expired',exit_reason='expired',exited_at=?,"
        "management_action='operator_cancelled_pending_entries',"
        "management_note='All unfilled entry orders were cancelled at Deepcoin.',"
        "expiry_review_next_at=NULL,updated_at=?",
        (repair_value, repair_value),
        "lifecycle_status='pending_entry'",
    )


def _update_bindings(connection, repair_value: str) -> int:
    positions = _update_ids(
        connection,
        "execution_bindings",
        POSITION_BINDING_IDS,
        "status='closed',pos_id=NULL,last_exchange_status='historical_cleanup_terminal',"
        "recovered_at=?,updated_at=?",
        (repair_value, repair_value),
        f"lower(status) NOT IN ({_sql_literals(_TERMINAL_BINDING_STATES)})",
    )
    orders = _update_ids(
        connection,
        "execution_bindings",
        ORDER_BINDING_IDS,
        "status='cancelled',pos_id=NULL,"
        "last_exchange_status='operator_cancelled_pending_entries',updated_at=?",
        (repair_value,),
        f"lower(status) NOT IN ({_sql_literals(_TERMINAL_BINDING_STATES)})",
    )
    return positions + orders


def _update_order_legs(connection, repair_value: str) -> int:
    positions = _update_ids(
        connection,
        "execution_order_legs",
        POSITION_LEG_IDS,
        "status='closed',terminal_reason='historical_exchange_position_closed',"
        "last_verified_at=?,updated_at=?",
        (repair_value, repair_value),
        "purpose='entry' AND nullif(trim(pos_id),'') IS NOT NULL "
        f"AND lower(status) NOT IN ({_sql_literals(_TERMINAL_ENTRY_LEG_STATES)})",
    )
    orders = _update_ids(
        connection,
        "execution_order_legs",
        ORDER_LEG_IDS,
        "status='cancelled',terminal_reason='operator_cancelled_unfilled_entry_leg',"
        "last_verified_at=?,updated_at=?",
        (repair_value, repair_value),
        "purpose='entry' AND nullif(trim(pos_id),'') IS NULL "
        f"AND lower(status) IN ({_sql_literals(_UNRESOLVED_ORDER_LEG_STATES)})",
    )
    return positions + orders


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
    legs = {row["id"]: row for row in state["execution_order_legs"]}
    rows = []
    audited_binding_ids: set[int] = set()
    for leg_id in ORDER_LEG_IDS:
        leg = legs[leg_id]
        binding_id = leg["execution_binding_id"]
        audited_binding_ids.add(int(binding_id))
        binding = bindings[binding_id]
        rows.append(
            (
                binding_id,
                binding.get("strategy_instance_id"),
                "deepcoin",
                "reconcile_manual_pending_entry_cancel",
                "confirmed",
                binding.get("chat_id"),
                binding.get("message_id"),
                binding.get("symbol"),
                binding.get("side"),
                leg.get("order_id"),
                "operator_confirmed_all_entry_orders_cancelled",
                _canonical({"pending": False, "terminalized": True}),
                None,
                repair_value,
                "not_needed",
                _audit_hash(code_sha, repair_value, "order_leg", leg_id),
                0,
            )
        )
    for binding_id in ORDER_BINDING_IDS:
        if binding_id in audited_binding_ids:
            continue
        binding = bindings[binding_id]
        rows.append(
            (
                binding_id,
                binding.get("strategy_instance_id"),
                "deepcoin",
                "reconcile_manual_pending_entry_cancel",
                "confirmed",
                binding.get("chat_id"),
                binding.get("message_id"),
                binding.get("symbol"),
                binding.get("side"),
                binding.get("order_id"),
                "operator_confirmed_all_entry_orders_cancelled",
                _canonical({"pending": False, "terminalized": True}),
                None,
                repair_value,
                "not_needed",
                _audit_hash(code_sha, repair_value, "order_binding", binding_id),
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
                    "binding_ids": TARGET_BINDING_IDS,
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
    connection, *, state, code_sha: str, repair_value: str
) -> int:
    bindings = {row["id"]: row for row in state["execution_bindings"]}
    legs = {row["id"]: row for row in state["execution_order_legs"]}
    lifecycles = {row["id"]: row for row in state["strategy_lifecycles"]}
    rows = []
    for leg_id in POSITION_LEG_IDS:
        leg = legs[leg_id]
        binding_id = int(leg["execution_binding_id"])
        rows.append(
            (
                binding_id,
                leg_id,
                "deepcoin",
                leg.get("pos_id"),
                "historical_cleanup",
                leg.get("status"),
                "closed",
                _audit_hash(code_sha, repair_value, "position_leg", leg_id),
                _canonical(
                    {
                        "action": "terminalize_historical_entry_leg",
                        "code_sha": code_sha,
                        "lifecycle_id": BINDING_TO_LIFECYCLE.get(binding_id),
                        "old_pos_id": leg.get("pos_id"),
                        "new_pos_id": leg.get("pos_id"),
                        "terminal_evidence": {
                            "source": "complete_exchange_snapshot_empty",
                            "reason": "exchange_closed",
                        },
                    }
                ),
                repair_value,
            )
        )
    for binding_id in POSITION_BINDING_IDS:
        binding = bindings[binding_id]
        rows.append(
            (
                binding_id,
                None,
                "deepcoin",
                binding.get("pos_id"),
                "historical_cleanup",
                binding.get("status"),
                "closed",
                _audit_hash(code_sha, repair_value, "position_binding", binding_id),
                _canonical(
                    {
                        "action": "close_historical_binding",
                        "code_sha": code_sha,
                        "lifecycle_id": BINDING_TO_LIFECYCLE.get(binding_id),
                        "old_pos_id": binding.get("pos_id"),
                        "new_pos_id": None,
                        "terminal_evidence": {
                            "source": "complete_exchange_snapshot_empty",
                            "reason": "exchange_closed",
                        },
                    }
                ),
                repair_value,
            )
        )
    for lifecycle_id in ENTERED_LIFECYCLE_IDS:
        lifecycle = lifecycles[lifecycle_id]
        rows.append(
            (
                lifecycle.get("execution_binding_id"),
                None,
                "deepcoin",
                None,
                "historical_cleanup",
                lifecycle.get("lifecycle_status"),
                "exited",
                _audit_hash(
                    code_sha, repair_value, "entered_lifecycle", lifecycle_id
                ),
                _canonical(
                    {
                        "action": "exit_historical_lifecycle",
                        "code_sha": code_sha,
                        "lifecycle_id": lifecycle_id,
                        "old_pos_id": None,
                        "new_pos_id": None,
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
    connection, *, state, code_sha: str, repair_value: str
) -> None:
    execution_hashes = [
        _audit_hash(code_sha, repair_value, "order_leg", leg_id)
        for leg_id in ORDER_LEG_IDS
    ] + [_audit_hash(code_sha, repair_value, "summary", 0)]
    order_leg_binding_ids = {
        int(row["execution_binding_id"])
        for row in state["execution_order_legs"]
        if int(row["id"]) in set(ORDER_LEG_IDS)
    }
    execution_hashes.extend(
        _audit_hash(code_sha, repair_value, "order_binding", binding_id)
        for binding_id in ORDER_BINDING_IDS
        if binding_id not in order_leg_binding_ids
    )
    attribution_hashes = [
        *(
            _audit_hash(code_sha, repair_value, "position_leg", leg_id)
            for leg_id in POSITION_LEG_IDS
        ),
        *(
            _audit_hash(code_sha, repair_value, "position_binding", binding_id)
            for binding_id in POSITION_BINDING_IDS
        ),
        *(
            _audit_hash(code_sha, repair_value, "entered_lifecycle", lifecycle_id)
            for lifecycle_id in ENTERED_LIFECYCLE_IDS
        ),
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
    position_claims, order_claims = _query_binding_claims(connection)
    if position_claims:
        raise AlignmentRefused("position_binding_claim_remains")
    if order_claims:
        raise AlignmentRefused("order_binding_claim_remains")
    binding_slots = _slots(TARGET_BINDING_IDS)
    if connection.execute(
        "SELECT 1 FROM execution_order_legs WHERE execution_binding_id IN "
        f"({binding_slots}) AND purpose='entry' AND lower(status) NOT IN "
        f"({_slots(_TERMINAL_ENTRY_LEG_STATES)}) LIMIT 1",
        TARGET_BINDING_IDS + _TERMINAL_ENTRY_LEG_STATES,
    ).fetchone() is not None:
        raise AlignmentRefused("active_order_leg_remains")
    if connection.execute(
        "SELECT 1 FROM strategy_lifecycles "
        f"WHERE execution_binding_id IN ({binding_slots}) "
        "AND lifecycle_status='pending_entry' LIMIT 1",
        TARGET_BINDING_IDS,
    ).fetchone() is not None:
        raise AlignmentRefused("pending_lifecycle_remains")


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
    trigger_results = payload.get("pending_trigger_orders_by_instrument")
    if not isinstance(trigger_results, Mapping) or set(trigger_results) != set(
        PENDING_TRIGGER_INSTRUMENTS
    ):
        raise AlignmentRefused("exchange_snapshot_incomplete")
    for instrument in PENDING_TRIGGER_INSTRUMENTS:
        result = trigger_results[instrument]
        if (
            not isinstance(result, Mapping)
            or result.get("complete") is not True
            or "error" not in result
            or result.get("error") is not None
            or not isinstance(result.get("orders"), list)
        ):
            raise AlignmentRefused("exchange_snapshot_incomplete")
        if result["orders"]:
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
