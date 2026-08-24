"""Exact copy-rehearsal plan for historical management batch 150."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


EXPECTED_ACTION_COUNT = 8
TARGET_BATCH_ID = 150
REPAIR_REASON = "historical_position_fully_closed"
TERMINAL_LEG_REASON = "historical_exchange_position_closed"

STRATEGY_INSTANCE_ID = "deepcoin:-1003048800035:4384:BTC:short"
TARGET_POS_ID = "1001124956792734"
SIBLING_POS_ID = "1001124961572300"
PARENT_TRIGGER_ORDER_ID = "1001124956792983"
TARGET_STOP_ORDER_ID = "1001124956792870"
SIBLING_STOP_ORDER_ID = "1001124961572299"
INSTRUMENT_ID = "BTC-USDT-SWAP"
SIDE = "short"
FULL_SIZE = "11"

_BATCH_EXPECTED = {
    "id": 150,
    "status": "recovery_required",
    "reason_code": "take_profit_cancel_retry_exhausted",
    "idempotency_fingerprint": (
        "723b6ce5b01d2efef22dd35a11ace2f91cb35a12a9fd1cb4ec00e2953c01ac36"
    ),
    "management_contract_fingerprint": (
        "ad8515aec6aac95b51b5edd0de6fe8aab9f6d89fa54ba23aeedb07af71a06006"
    ),
    "target_fingerprint": (
        "353682c2d65ce005f9b214ac4cd4433d0b0da141d49fe3456866af861d8c2e73"
    ),
    "recognition_generation": "eb1b4c395455496fbdedd6c267d66443",
    "contract_version": 2,
    "target_lifecycle_id": 952,
    "execution_binding_id": 320,
    "raw_message_id": 12780,
    "recognition_decision_id": 12779,
    "strategy_instance_id": STRATEGY_INSTANCE_ID,
    "reconciled_at": None,
    "completed_at": None,
}

_COMPONENT_EXPECTED = {
    22: {
        "management_batch_id": 150,
        "strategy_management_leg_id": 133,
        "component_kind": "consume_take_profit_stage",
        "sequence": 0,
        "status": "operator_required",
        "reason_code": "take_profit_cancel_retry_exhausted",
        "attempt_count": 3,
    },
    23: {
        "management_batch_id": 150,
        "strategy_management_leg_id": 133,
        "component_kind": "converge_partial_close",
        "sequence": 1,
        "status": "pending",
        "reason_code": None,
        "attempt_count": 0,
        "completed_at": None,
    },
    24: {
        "management_batch_id": 150,
        "strategy_management_leg_id": 133,
        "component_kind": "replace_remaining_protection",
        "sequence": 2,
        "status": "pending",
        "reason_code": None,
        "attempt_count": 0,
        "completed_at": None,
    },
}

_EXECUTION_LEG_EXPECTED = {
    553: {
        "execution_binding_id": 320,
        "strategy_instance_id": STRATEGY_INSTANCE_ID,
        "leg_index": 1,
        "purpose": "entry",
        "order_kind": "market",
        "order_id": TARGET_POS_ID,
        "client_order_id": "TKDBK4384E1",
        "pos_id": TARGET_POS_ID,
        "attribution_status": "verified",
        "terminal_reason": None,
        "status": "filled",
    },
    554: {
        "execution_binding_id": 320,
        "strategy_instance_id": STRATEGY_INSTANCE_ID,
        "leg_index": 2,
        "purpose": "entry",
        "order_kind": "trigger_limit",
        "order_id": PARENT_TRIGGER_ORDER_ID,
        "client_order_id": "TKDBK4384E2",
        "pos_id": SIBLING_POS_ID,
        "attribution_status": "verified",
        "terminal_reason": None,
        "status": "active",
    },
}

_COUNT_TABLES = (
    "strategy_management_components",
    "strategy_management_legs",
    "strategy_management_batches",
    "execution_order_legs",
    "execution_bindings",
    "strategy_lifecycles",
    "position_mutation_intents",
    "bound_position_close_reservations",
    "position_protection_ledger",
    "position_take_profit_orders",
    "execution_events",
    "raw_messages",
    "recognition_decisions",
)

_CAS_POLICY = {
    "execution_order_legs:553": {
        "ignored_before_fields": ("updated_at",),
    }
}


class Batch150TerminalizationRefused(RuntimeError):
    """Raised before commit whenever exact L3 evidence or CAS state differs."""


@dataclass(frozen=True, slots=True)
class Batch150TerminalizationAction:
    table: str
    pk: int
    before: Mapping[str, Any]
    after: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Batch150TerminalizationPlan:
    schema_version: int
    mode: str
    database_path: str
    target_batch_id: int
    code_sha: str
    repair_ts_utc: str
    quick_check: str
    table_counts: Mapping[str, int]
    cas_policy: Mapping[str, Mapping[str, tuple[str, ...]]]
    database_evidence: Mapping[str, Any]
    exchange_evidence: Mapping[str, Any]
    database_fingerprint: str
    exchange_fingerprint: str
    action_fingerprint: str
    rollback_fingerprint: str
    plan_fingerprint: str
    confirmation_token: str
    exchange_write_count: int
    actions: tuple[Batch150TerminalizationAction, ...]

    @property
    def action_count(self) -> int:
        return len(self.actions)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action_count"] = self.action_count
        return payload


@dataclass(frozen=True, slots=True)
class Batch150TerminalizationMutationResult:
    mode: str
    status: str
    changed_row_count: int
    quick_check: str
    table_counts_before: Mapping[str, int]
    table_counts_after: Mapping[str, int]


def build_batch150_terminalization_plan(
    database_path: str | Path,
    *,
    exchange_evidence: Mapping[str, Any],
    repair_ts: datetime,
    code_sha: str,
) -> Batch150TerminalizationPlan:
    """Build the exact eight-action plan without opening the database writable."""

    resolved = Path(database_path).expanduser().resolve()
    if not resolved.is_file():
        raise Batch150TerminalizationRefused("database_missing")
    if repair_ts.tzinfo is None:
        raise Batch150TerminalizationRefused("repair_timestamp_not_utc")
    repair_ts = repair_ts.astimezone(UTC)
    if not _valid_sha(code_sha):
        raise Batch150TerminalizationRefused("code_sha_invalid")
    normalized_exchange = _validate_exchange_evidence(exchange_evidence)
    repair_db_value = _database_timestamp(repair_ts)

    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise Batch150TerminalizationRefused("query_only_unavailable")
        connection.execute("BEGIN")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise Batch150TerminalizationRefused("quick_check_failed")
        recovery_ids = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM strategy_management_batches "
                "WHERE status='recovery_required' ORDER BY id"
            )
        )
        if recovery_ids != (TARGET_BATCH_ID,):
            raise Batch150TerminalizationRefused("target_set_changed")
        unsafe = connection.execute(
            "SELECT id FROM strategy_management_batches WHERE status IN "
            "('executing','reserved','submitted','submit_unknown','reconciling') "
            "ORDER BY id"
        ).fetchall()
        if unsafe:
            raise Batch150TerminalizationRefused("management_window_not_quiet")
        table_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _COUNT_TABLES
        }
        actions, database_evidence = _build_actions(
            connection, repair_db_value=repair_db_value
        )
        if len(actions) != EXPECTED_ACTION_COUNT:
            raise Batch150TerminalizationRefused("action_count_changed")
    finally:
        connection.rollback()
        connection.close()

    database_fingerprint = _sha(database_evidence)
    exchange_fingerprint = _sha(normalized_exchange)
    action_fingerprint = _sha([_action_payload(value) for value in actions])
    rollback_fingerprint = _sha(
        {
            "actions": [
                _reverse_action_payload(value) for value in reversed(actions)
            ],
            "cas_policy": _CAS_POLICY,
        }
    )
    repair_ts_utc = repair_ts.isoformat(timespec="seconds")
    plan_material = {
        "schema_version": 2,
        "mode": "batch150_historical_terminalization",
        "database_path": str(resolved),
        "target_batch_id": TARGET_BATCH_ID,
        "code_sha": code_sha,
        "repair_ts_utc": repair_ts_utc,
        "quick_check": quick_check,
        "table_counts": table_counts,
        "cas_policy": _CAS_POLICY,
        "database_fingerprint": database_fingerprint,
        "exchange_fingerprint": exchange_fingerprint,
        "action_fingerprint": action_fingerprint,
        "rollback_fingerprint": rollback_fingerprint,
        "exchange_write_count": 0,
    }
    plan_fingerprint = _sha(plan_material)
    confirmation_token = _sha(
        {
            "action": "apply_batch150_historical_terminalization",
            "plan_fingerprint": plan_fingerprint,
            "repair_ts_utc": repair_ts_utc,
        }
    )
    plan = Batch150TerminalizationPlan(
        schema_version=2,
        mode="batch150_historical_terminalization",
        database_path=str(resolved),
        target_batch_id=TARGET_BATCH_ID,
        code_sha=code_sha,
        repair_ts_utc=repair_ts_utc,
        quick_check=quick_check,
        table_counts=table_counts,
        cas_policy=_CAS_POLICY,
        database_evidence=database_evidence,
        exchange_evidence=normalized_exchange,
        database_fingerprint=database_fingerprint,
        exchange_fingerprint=exchange_fingerprint,
        action_fingerprint=action_fingerprint,
        rollback_fingerprint=rollback_fingerprint,
        plan_fingerprint=plan_fingerprint,
        confirmation_token=confirmation_token,
        exchange_write_count=0,
        actions=tuple(actions),
    )
    _validate_plan_integrity(plan)
    return plan


def write_batch150_terminalization_plan(
    path: str | Path, plan: Batch150TerminalizationPlan
) -> None:
    _validate_plan_integrity(plan)
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical_json(plan.to_dict()))
            stream.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(resolved, 0o600)


def load_batch150_terminalization_plan(
    path: str | Path,
) -> Batch150TerminalizationPlan:
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.pop("action_count", None) != EXPECTED_ACTION_COUNT
    ):
        raise Batch150TerminalizationRefused("plan_json_invalid")
    raw_actions = payload.get("actions")
    raw_cas_policy = payload.get("cas_policy")
    if not isinstance(raw_actions, list) or not isinstance(raw_cas_policy, dict):
        raise Batch150TerminalizationRefused("plan_json_invalid")
    try:
        payload["cas_policy"] = {
            key: {
                "ignored_before_fields": tuple(value["ignored_before_fields"]),
            }
            for key, value in raw_cas_policy.items()
        }
        payload["actions"] = tuple(
            Batch150TerminalizationAction(
                table=item["table"],
                pk=int(item["pk"]),
                before=item["before"],
                after=item["after"],
            )
            for item in raw_actions
        )
        plan = Batch150TerminalizationPlan(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise Batch150TerminalizationRefused("plan_json_invalid") from exc
    _validate_plan_integrity(plan)
    return plan


def render_batch150_rollback_sql(plan: Batch150TerminalizationPlan) -> str:
    _validate_plan_integrity(plan)
    lines = [
        "BEGIN IMMEDIATE;",
        f"-- plan_fingerprint: {plan.plan_fingerprint}",
        f"-- rollback_fingerprint: {plan.rollback_fingerprint}",
        f"-- exact_action_count: {plan.action_count}",
        "-- ignored_before_field: execution_order_legs:553 updated_at",
        "CREATE TEMP TABLE _batch150_repair_cas_guard "
        "(value INTEGER CHECK(value=1));",
    ]
    for action in reversed(plan.actions):
        before = action.after
        after = action.before
        changed_columns = [
            column
            for column in before
            if column != "id" and before[column] != after[column]
        ]
        assignments = ", ".join(
            f"{column}={_sql_literal(after[column])}" for column in changed_columns
        )
        ignored_fields = _ignored_before_fields(plan, action)
        predicates = " AND ".join(
            f"{column} IS {_sql_literal(value)}"
            for column, value in before.items()
            if column not in ignored_fields
        )
        lines.append(f"UPDATE {action.table} SET {assignments} WHERE {predicates};")
        lines.append("INSERT INTO _batch150_repair_cas_guard VALUES(changes());")
        lines.append("DELETE FROM _batch150_repair_cas_guard;")
    lines.append("DROP TABLE _batch150_repair_cas_guard;")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exact batch 150 historical terminalization rehearsal tool"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="build read-only artifacts")
    plan_parser.add_argument("--database-path", required=True)
    plan_parser.add_argument("--exchange-evidence", required=True)
    plan_parser.add_argument("--repair-ts-utc", required=True)
    plan_parser.add_argument("--code-sha", required=True)
    plan_parser.add_argument("--plan-path", required=True)
    plan_parser.add_argument("--rollback-sql-path", required=True)

    apply_parser = subparsers.add_parser("apply", help="apply exact CAS actions")
    _add_common_mutation_arguments(apply_parser)
    apply_parser.add_argument("--expected-plan-fingerprint", required=True)
    apply_parser.add_argument("--expected-repair-ts-utc", required=True)

    rollback_parser = subparsers.add_parser(
        "rollback", help="rollback exact CAS actions"
    )
    _add_common_mutation_arguments(rollback_parser)
    rollback_parser.add_argument("--expected-rollback-fingerprint", required=True)

    arguments = parser.parse_args(argv)
    if arguments.command == "plan":
        evidence = json.loads(
            Path(arguments.exchange_evidence).expanduser().read_text(encoding="utf-8")
        )
        plan = build_batch150_terminalization_plan(
            arguments.database_path,
            exchange_evidence=evidence,
            repair_ts=datetime.fromisoformat(arguments.repair_ts_utc),
            code_sha=arguments.code_sha,
        )
        write_batch150_terminalization_plan(arguments.plan_path, plan)
        _write_private_text(
            arguments.rollback_sql_path, render_batch150_rollback_sql(plan)
        )
        _print_summary(
            {
                "mode": plan.mode,
                "status": "planned",
                "action_count": plan.action_count,
                "cas_policy": plan.cas_policy,
                "plan_fingerprint": plan.plan_fingerprint,
                "action_fingerprint": plan.action_fingerprint,
                "rollback_fingerprint": plan.rollback_fingerprint,
                "database_fingerprint": plan.database_fingerprint,
                "exchange_fingerprint": plan.exchange_fingerprint,
                "exchange_write_count": plan.exchange_write_count,
            }
        )
        return 0

    plan = load_batch150_terminalization_plan(arguments.plan_path)
    if arguments.command == "apply":
        result = apply_batch150_terminalization_plan(
            arguments.database_path,
            plan=plan,
            expected_plan_fingerprint=arguments.expected_plan_fingerprint,
            expected_action_count=arguments.expected_action_count,
            expected_repair_ts_utc=arguments.expected_repair_ts_utc,
            confirmation_token=arguments.confirmation_token,
        )
    else:
        result = rollback_batch150_terminalization_plan(
            arguments.database_path,
            plan=plan,
            expected_rollback_fingerprint=arguments.expected_rollback_fingerprint,
            expected_action_count=arguments.expected_action_count,
            confirmation_token=arguments.confirmation_token,
        )
    _print_summary(asdict(result))
    return 0


def _add_common_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--plan-path", required=True)
    parser.add_argument("--expected-action-count", required=True, type=int)
    parser.add_argument("--confirmation-token", required=True)


def _print_summary(value: Mapping[str, Any]) -> None:
    print(_canonical_json(value))


def _write_private_text(path: str | Path, value: str) -> None:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(resolved, 0o600)


def apply_batch150_terminalization_plan(
    database_path: str | Path,
    *,
    plan: Batch150TerminalizationPlan,
    expected_plan_fingerprint: str,
    expected_action_count: int,
    expected_repair_ts_utc: str,
    confirmation_token: str,
) -> Batch150TerminalizationMutationResult:
    """Apply the exact plan using full-row compare-and-swap predicates."""

    _validate_plan_integrity(plan)
    if expected_plan_fingerprint != plan.plan_fingerprint:
        raise Batch150TerminalizationRefused("plan_fingerprint_mismatch")
    if expected_action_count != plan.action_count:
        raise Batch150TerminalizationRefused("action_count_mismatch")
    if expected_repair_ts_utc != plan.repair_ts_utc:
        raise Batch150TerminalizationRefused("repair_timestamp_mismatch")
    if confirmation_token != plan.confirmation_token:
        raise Batch150TerminalizationRefused("confirmation_token_mismatch")
    return _mutate_batch150_terminalization_plan(database_path, plan=plan, reverse=False)


def rollback_batch150_terminalization_plan(
    database_path: str | Path,
    *,
    plan: Batch150TerminalizationPlan,
    expected_rollback_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
) -> Batch150TerminalizationMutationResult:
    """Rollback the exact plan, refusing runtime-canonicalized or mixed rows."""

    _validate_plan_integrity(plan)
    if expected_rollback_fingerprint != plan.rollback_fingerprint:
        raise Batch150TerminalizationRefused("rollback_fingerprint_mismatch")
    if expected_action_count != plan.action_count:
        raise Batch150TerminalizationRefused("action_count_mismatch")
    if confirmation_token != plan.confirmation_token:
        raise Batch150TerminalizationRefused("confirmation_token_mismatch")
    return _mutate_batch150_terminalization_plan(database_path, plan=plan, reverse=True)


def _mutate_batch150_terminalization_plan(
    database_path: str | Path,
    *,
    plan: Batch150TerminalizationPlan,
    reverse: bool,
) -> Batch150TerminalizationMutationResult:
    resolved = Path(database_path).expanduser().resolve()
    if str(resolved) != plan.database_path:
        raise Batch150TerminalizationRefused("database_path_mismatch")
    if not resolved.is_file():
        raise Batch150TerminalizationRefused("database_missing")

    connection = sqlite3.connect(resolved)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise Batch150TerminalizationRefused("quick_check_failed")
        counts_before = _table_counts(connection)
        if counts_before != dict(plan.table_counts):
            raise Batch150TerminalizationRefused("table_counts_changed")

        actions = tuple(reversed(plan.actions)) if reverse else plan.actions
        start_rows = tuple(
            _read_action_row(connection, action, reverse=reverse)
            for action in actions
        )
        expected_before = tuple(
            dict(action.after if reverse else action.before) for action in actions
        )
        expected_after = tuple(
            dict(action.before if reverse else action.after) for action in actions
        )
        if _rows_equal_under_policy(plan, actions, start_rows, expected_after):
            _require_quiet_target_set(connection, reverse=not reverse)
            connection.rollback()
            return Batch150TerminalizationMutationResult(
                mode="rollback" if reverse else "apply",
                status="already_rolled_back" if reverse else "already_applied",
                changed_row_count=0,
                quick_check=quick_check,
                table_counts_before=counts_before,
                table_counts_after=counts_before,
            )
        if not _rows_equal_under_policy(plan, actions, start_rows, expected_before):
            raise Batch150TerminalizationRefused("database_state_mixed")
        _require_quiet_target_set(connection, reverse=reverse)

        for action in actions:
            _cas_update_action(connection, plan, action, reverse=reverse)

        final_rows = tuple(
            _read_action_row(connection, action, reverse=reverse)
            for action in actions
        )
        if final_rows != expected_after:
            raise Batch150TerminalizationRefused("postcondition_failed")
        counts_after = _table_counts(connection)
        if counts_after != counts_before:
            raise Batch150TerminalizationRefused("table_counts_changed")
        _require_quiet_target_set(connection, reverse=not reverse)
        post_quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if post_quick_check != "ok":
            raise Batch150TerminalizationRefused("quick_check_failed")
        connection.commit()
        return Batch150TerminalizationMutationResult(
            mode="rollback" if reverse else "apply",
            status="rolled_back" if reverse else "applied",
            changed_row_count=len(actions),
            quick_check=post_quick_check,
            table_counts_before=counts_before,
            table_counts_after=counts_after,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _COUNT_TABLES
    }


def _require_quiet_target_set(
    connection: sqlite3.Connection, *, reverse: bool
) -> None:
    recovery_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM strategy_management_batches "
            "WHERE status='recovery_required' ORDER BY id"
        )
    )
    expected = () if reverse else (TARGET_BATCH_ID,)
    if recovery_ids != expected:
        raise Batch150TerminalizationRefused("target_set_changed")
    unsafe = connection.execute(
        "SELECT id FROM strategy_management_batches WHERE status IN "
        "('executing','reserved','submitted','submit_unknown','reconciling') "
        "ORDER BY id"
    ).fetchall()
    if unsafe:
        raise Batch150TerminalizationRefused("management_window_not_quiet")


def _read_action_row(
    connection: sqlite3.Connection,
    action: Batch150TerminalizationAction,
    *,
    reverse: bool,
) -> dict[str, Any]:
    columns = tuple((action.after if reverse else action.before).keys())
    cursor = connection.execute(
        f"SELECT {','.join(columns)} FROM {action.table} WHERE id=?", (action.pk,)
    )
    row = cursor.fetchone()
    if row is None or cursor.fetchone() is not None:
        raise Batch150TerminalizationRefused("database_state_mixed")
    return dict(row)


def _cas_update_action(
    connection: sqlite3.Connection,
    plan: Batch150TerminalizationPlan,
    action: Batch150TerminalizationAction,
    *,
    reverse: bool,
) -> None:
    before = dict(action.after if reverse else action.before)
    after = dict(action.before if reverse else action.after)
    changed_columns = tuple(
        column
        for column in before
        if column != "id" and before[column] != after[column]
    )
    assignments = ",".join(f"{column}=?" for column in changed_columns)
    ignored_fields = _ignored_before_fields(plan, action)
    predicate_columns = tuple(
        column for column in before if column not in ignored_fields
    )
    predicates = " AND ".join(f"{column} IS ?" for column in predicate_columns)
    parameters = tuple(after[column] for column in changed_columns) + tuple(
        before[column] for column in predicate_columns
    )
    cursor = connection.execute(
        f"UPDATE {action.table} SET {assignments} WHERE {predicates}", parameters
    )
    if cursor.rowcount != 1:
        raise Batch150TerminalizationRefused("cas_rowcount_changed")


def _ignored_before_fields(
    plan: Batch150TerminalizationPlan,
    action: Batch150TerminalizationAction,
) -> frozenset[str]:
    action_policy = plan.cas_policy.get(f"{action.table}:{action.pk}", {})
    return frozenset(action_policy.get("ignored_before_fields", ()))


def _rows_equal_under_policy(
    plan: Batch150TerminalizationPlan,
    actions: tuple[Batch150TerminalizationAction, ...],
    actual_rows: tuple[Mapping[str, Any], ...],
    expected_rows: tuple[Mapping[str, Any], ...],
) -> bool:
    return all(
        {
            key: value
            for key, value in actual.items()
            if key not in _ignored_before_fields(plan, action)
        }
        == {
            key: value
            for key, value in expected.items()
            if key not in _ignored_before_fields(plan, action)
        }
        for action, actual, expected in zip(
            actions, actual_rows, expected_rows, strict=True
        )
    )


def _build_actions(connection: sqlite3.Connection, *, repair_db_value: str):
    batch = _one(
        connection,
        "SELECT * FROM strategy_management_batches WHERE id=150",
        "batch_missing",
    )
    _require_values(batch, _BATCH_EXPECTED, "batch_changed")

    management_legs = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM strategy_management_legs "
            "WHERE management_batch_id=150 ORDER BY id"
        )
    ]
    if [row["id"] for row in management_legs] != [133]:
        raise Batch150TerminalizationRefused("management_leg_set_changed")
    management_leg = management_legs[0]
    _require_values(
        management_leg,
        {
            "management_batch_id": 150,
            "execution_order_leg_id": 553,
            "pos_id": TARGET_POS_ID,
            "leg_index": 0,
            "status": "planned",
            "preflight_size": "6",
            "planned_close_size": "3",
        },
        "management_leg_changed",
    )

    components = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM strategy_management_components "
            "WHERE management_batch_id=150 ORDER BY id"
        )
    ]
    if [row["id"] for row in components] != [22, 23, 24]:
        raise Batch150TerminalizationRefused("component_set_changed")
    for row in components:
        _require_values(row, _COMPONENT_EXPECTED[row["id"]], "component_changed")

    binding = _one(
        connection,
        "SELECT * FROM execution_bindings WHERE id=320",
        "binding_missing",
    )
    _require_values(
        binding,
        {
            "strategy_instance_id": STRATEGY_INSTANCE_ID,
            "symbol": "BTC",
            "side": SIDE,
            "status": "active",
            "pos_id": TARGET_POS_ID,
            "last_exchange_status": "position_attribution_evidence_unavailable",
        },
        "binding_changed",
    )

    execution_legs = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM execution_order_legs "
            "WHERE execution_binding_id=320 ORDER BY id"
        )
    ]
    if [row["id"] for row in execution_legs] != [553, 554]:
        raise Batch150TerminalizationRefused("binding_leg_set_changed")
    for row in execution_legs:
        _require_values(
            row, _EXECUTION_LEG_EXPECTED[row["id"]], "execution_leg_changed"
        )

    lifecycle = _one(
        connection,
        "SELECT * FROM strategy_lifecycles WHERE id=952",
        "lifecycle_missing",
    )
    _require_values(
        lifecycle,
        {
            "execution_binding_id": 320,
            "lifecycle_status": "entered",
            "exit_reason": None,
            "exited_at": None,
        },
        "lifecycle_changed",
    )

    intents = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM position_mutation_intents "
            "WHERE execution_binding_id=320 ORDER BY id"
        )
    ]
    if not intents or any(row.get("status") != "confirmed" for row in intents):
        raise Batch150TerminalizationRefused("mutation_intent_unconfirmed")
    if any(
        row.get("execution_order_leg_id") != 553
        or row.get("pos_id") != TARGET_POS_ID
        for row in intents
    ):
        raise Batch150TerminalizationRefused("mutation_intent_identity_changed")

    reservations = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM bound_position_close_reservations "
            "WHERE execution_binding_id=320 ORDER BY id"
        )
    ]
    if reservations:
        raise Batch150TerminalizationRefused("close_reservation_present")

    raw_message = _one(
        connection,
        "SELECT id,message_id FROM raw_messages WHERE id=12780",
        "source_message_missing",
    )
    recognition = _one(
        connection,
        "SELECT id FROM recognition_decisions WHERE id=12779",
        "recognition_decision_missing",
    )

    actions: list[Batch150TerminalizationAction] = []
    for component_id in (23, 24):
        row = next(value for value in components if value["id"] == component_id)
        after = dict(row)
        after.update(
            status="safely_skipped",
            reason_code=REPAIR_REASON,
            last_progress_at=repair_db_value,
            completed_at=repair_db_value,
            updated_at=repair_db_value,
        )
        actions.append(
            Batch150TerminalizationAction(
                "strategy_management_components", component_id, row, after
            )
        )

    after_leg = dict(management_leg)
    after_leg.update(status="failed", updated_at=repair_db_value)
    actions.append(
        Batch150TerminalizationAction(
            "strategy_management_legs", 133, management_leg, after_leg
        )
    )

    after_batch = dict(batch)
    after_batch.update(
        status="resolved",
        reason_code=REPAIR_REASON,
        reconciled_at=repair_db_value,
        completed_at=repair_db_value,
        updated_at=repair_db_value,
    )
    actions.append(
        Batch150TerminalizationAction(
            "strategy_management_batches", 150, batch, after_batch
        )
    )

    for row in execution_legs:
        after = dict(row)
        after.update(
            status="closed",
            terminal_reason=TERMINAL_LEG_REASON,
            last_verified_at=repair_db_value,
            updated_at=repair_db_value,
        )
        actions.append(
            Batch150TerminalizationAction(
                "execution_order_legs", int(row["id"]), row, after
            )
        )

    after_binding = dict(binding)
    after_binding.update(
        status="closed",
        pos_id=None,
        last_exchange_status="historical_cleanup_terminal",
        recovered_at=repair_db_value,
        updated_at=repair_db_value,
    )
    actions.append(
        Batch150TerminalizationAction(
            "execution_bindings", 320, binding, after_binding
        )
    )

    after_lifecycle = dict(lifecycle)
    after_lifecycle.update(
        lifecycle_status="exited",
        exit_reason="exchange_closed",
        exited_at=repair_db_value,
        updated_at=repair_db_value,
    )
    actions.append(
        Batch150TerminalizationAction(
            "strategy_lifecycles", 952, lifecycle, after_lifecycle
        )
    )

    evidence = {
        "batch": batch,
        "management_leg": management_leg,
        "components": components,
        "binding": binding,
        "execution_legs": execution_legs,
        "lifecycle": lifecycle,
        "mutation_intents": intents,
        "close_reservations": reservations,
        "raw_message": raw_message,
        "recognition_decision": recognition,
    }
    return actions, evidence


def _validate_exchange_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.loads(_canonical_json(evidence))
    if payload.get("snapshot_complete") is not True or payload.get("snapshot_errors"):
        raise Batch150TerminalizationRefused("exchange_snapshot_incomplete")
    if payload.get("exchange_write_count") != 0:
        raise Batch150TerminalizationRefused("exchange_write_count_nonzero")
    for key in ("positions", "open_orders", "pending_trigger_orders"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise Batch150TerminalizationRefused("exchange_snapshot_incomplete")
        if rows:
            raise Batch150TerminalizationRefused("target_position_not_terminal")

    target = _unique_history(
        payload.get("target_position_history"),
        pos_id=TARGET_POS_ID,
        reason_prefix="target",
    )
    sibling = _unique_history(
        payload.get("sibling_position_history"),
        pos_id=SIBLING_POS_ID,
        reason_prefix="sibling",
    )
    _validate_owned_stop(
        payload.get("target_stop"),
        expected_order_id=TARGET_STOP_ORDER_ID,
        history=target,
        reason="target_stop_close_unproven",
    )
    _validate_owned_stop(
        payload.get("sibling_stop"),
        expected_order_id=SIBLING_STOP_ORDER_ID,
        history=sibling,
        reason="sibling_stop_close_unproven",
    )
    chain = payload.get("parent_child_chain")
    expected_chain = {
        "parent_trigger_order_id": PARENT_TRIGGER_ORDER_ID,
        "parent_instrument_id": INSTRUMENT_ID,
        "parent_side": SIDE,
        "parent_size": FULL_SIZE,
        "unique_child_regular_order_id": SIBLING_POS_ID,
        "child_pos_id": SIBLING_POS_ID,
        "child_instrument_id": INSTRUMENT_ID,
        "child_side": SIDE,
        "child_state": "filled",
        "child_size": FULL_SIZE,
        "child_fill_size": FULL_SIZE,
    }
    if not isinstance(chain, dict) or any(
        str(chain.get(key) or "") != value for key, value in expected_chain.items()
    ):
        raise Batch150TerminalizationRefused("parent_child_chain_unproven")
    try:
        parent_seconds = int(str(chain.get("parent_trigger_time") or "0"))
        child_millis = int(str(chain.get("child_created_at") or "0"))
    except ValueError as exc:
        raise Batch150TerminalizationRefused("parent_child_chain_unproven") from exc
    if parent_seconds <= 0 or child_millis // 1000 != parent_seconds:
        raise Batch150TerminalizationRefused("parent_child_chain_unproven")
    return payload


def _unique_history(value, *, pos_id: str, reason_prefix: str) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1:
        raise Batch150TerminalizationRefused(f"{reason_prefix}_history_not_unique")
    row = value[0]
    if not isinstance(row, dict):
        raise Batch150TerminalizationRefused(f"{reason_prefix}_history_not_unique")
    if (
        row.get("instId") != INSTRUMENT_ID
        or row.get("posId") != pos_id
        or row.get("posSide") != SIDE
        or not _same_positive_number(row.get("pos"), FULL_SIZE)
        or not _same_positive_number(row.get("closePos"), FULL_SIZE)
        or not _same_positive_number(row.get("pos"), row.get("closePos"))
    ):
        raise Batch150TerminalizationRefused(
            f"{reason_prefix}_full_close_unproven"
        )
    return row


def _validate_owned_stop(value, *, expected_order_id: str, history, reason: str):
    if not isinstance(value, dict):
        raise Batch150TerminalizationRefused(reason)
    if (
        value.get("ordId") != expected_order_id
        or value.get("instId") != INSTRUMENT_ID
        or value.get("posSide") != SIDE
        or str(value.get("uTime") or "") != str(history.get("uTime") or "")
    ):
        raise Batch150TerminalizationRefused(reason)
    try:
        trigger_seconds = int(str(value.get("triggerTime") or "0"))
        history_millis = int(str(history.get("uTime") or "0"))
    except ValueError as exc:
        raise Batch150TerminalizationRefused(reason) from exc
    if trigger_seconds <= 0 or history_millis // 1000 != trigger_seconds:
        raise Batch150TerminalizationRefused(reason)


def _validate_plan_integrity(plan: Batch150TerminalizationPlan) -> None:
    expected_actions = (
        ("strategy_management_components", 23),
        ("strategy_management_components", 24),
        ("strategy_management_legs", 133),
        ("strategy_management_batches", 150),
        ("execution_order_legs", 553),
        ("execution_order_legs", 554),
        ("execution_bindings", 320),
        ("strategy_lifecycles", 952),
    )
    if (
        plan.schema_version != 2
        or plan.mode != "batch150_historical_terminalization"
        or plan.target_batch_id != TARGET_BATCH_ID
        or plan.action_count != EXPECTED_ACTION_COUNT
        or plan.exchange_write_count != 0
        or plan.quick_check != "ok"
        or not _valid_sha(plan.code_sha)
        or set(plan.table_counts) != set(_COUNT_TABLES)
        or _canonical_json(plan.cas_policy) != _canonical_json(_CAS_POLICY)
        or tuple((action.table, action.pk) for action in plan.actions)
        != expected_actions
        or any(
            set(action.before) != set(action.after)
            or action.before.get("id") != action.pk
            or action.after.get("id") != action.pk
            for action in plan.actions
        )
        or str(Path(plan.database_path).expanduser().resolve()) != plan.database_path
    ):
        raise Batch150TerminalizationRefused("plan_integrity_invalid")
    if _sha(plan.database_evidence) != plan.database_fingerprint:
        raise Batch150TerminalizationRefused("database_fingerprint_invalid")
    if _sha(plan.exchange_evidence) != plan.exchange_fingerprint:
        raise Batch150TerminalizationRefused("exchange_fingerprint_invalid")
    if _sha([_action_payload(value) for value in plan.actions]) != plan.action_fingerprint:
        raise Batch150TerminalizationRefused("action_fingerprint_invalid")
    if (
        _sha(
            {
                "actions": [
                    _reverse_action_payload(value)
                    for value in reversed(plan.actions)
                ],
                "cas_policy": plan.cas_policy,
            }
        )
        != plan.rollback_fingerprint
    ):
        raise Batch150TerminalizationRefused("rollback_fingerprint_invalid")
    material = {
        "schema_version": plan.schema_version,
        "mode": plan.mode,
        "database_path": plan.database_path,
        "target_batch_id": plan.target_batch_id,
        "code_sha": plan.code_sha,
        "repair_ts_utc": plan.repair_ts_utc,
        "quick_check": plan.quick_check,
        "table_counts": dict(plan.table_counts),
        "cas_policy": plan.cas_policy,
        "database_fingerprint": plan.database_fingerprint,
        "exchange_fingerprint": plan.exchange_fingerprint,
        "action_fingerprint": plan.action_fingerprint,
        "rollback_fingerprint": plan.rollback_fingerprint,
        "exchange_write_count": plan.exchange_write_count,
    }
    if _sha(material) != plan.plan_fingerprint:
        raise Batch150TerminalizationRefused("plan_fingerprint_invalid")
    expected_token = _sha(
        {
            "action": "apply_batch150_historical_terminalization",
            "plan_fingerprint": plan.plan_fingerprint,
            "repair_ts_utc": plan.repair_ts_utc,
        }
    )
    if plan.confirmation_token != expected_token:
        raise Batch150TerminalizationRefused("confirmation_token_invalid")


def _one(connection: sqlite3.Connection, query: str, reason: str) -> dict[str, Any]:
    cursor = connection.execute(query)
    row = cursor.fetchone()
    if row is None or cursor.fetchone() is not None:
        raise Batch150TerminalizationRefused(reason)
    return dict(row)


def _require_values(actual, expected, reason):
    if any(actual.get(key) != value for key, value in expected.items()):
        raise Batch150TerminalizationRefused(reason)


def _same_positive_number(left: Any, right: Any) -> bool:
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return a.is_finite() and b.is_finite() and a > 0 and a == b


def _database_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(
        sep=" ", timespec="microseconds"
    )


def _action_payload(action: Batch150TerminalizationAction) -> dict[str, Any]:
    return {
        "table": action.table,
        "pk": action.pk,
        "before": dict(action.before),
        "after": dict(action.after),
    }


def _reverse_action_payload(action: Batch150TerminalizationAction) -> dict[str, Any]:
    return {
        "table": action.table,
        "pk": action.pk,
        "before": dict(action.after),
        "after": dict(action.before),
    }


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_sha(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
