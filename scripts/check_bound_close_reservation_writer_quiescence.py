#!/usr/bin/env python3
"""Read-only, aggregate writer-quiescence check for reservation recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import sys
from typing import Sequence

from telegram_kol_research.bound_close_reservation_recovery import (
    _REQUIRED_SOURCE_COLUMNS,
)
from telegram_kol_research.deployment_preflight import (
    _KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS,
    _WORK_SPECS,
)
from telegram_kol_research.trigger_protection_intents import (
    ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES,
)


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_CANONICAL_SQLITE_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}\Z"
)
_EXPLICIT_UTC_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
_ACTIVE_WINDOW = timedelta(minutes=10)
_MAX_INSPECTED_ROWS_PER_TABLE = 10_000
_OUTPUT_FIELDS = frozenset(
    {
        "block_regardless_of_age_writer_count",
        "blocking_writer_count",
        "checked_table_count",
        "fresh_active_or_unknown_writer_count",
        "historical_active_or_unknown_residue_count",
        "missing_table_count",
        "schema_version",
        "status",
        "target_reservation_count",
        "unrecognized_or_null_state_count",
    }
)
_TARGET_TABLE = "bound_position_close_reservations"
_DEEPCOIN_TABLE = "deepcoin_execution_operations"
_TARGET_STATES = frozenset(
    {
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    }
)
_TRIGGER_TAKE_PROFIT_KNOWN_STATES = frozenset(
    {
        "waiting_position",
        "waiting_backup_stop",
        "ready",
        "reserved",
        "submitted",
        "submit_unknown",
        "completed",
        "conflicted",
        "blocked",
    }
)

# Closed/non-writer states for every table in deployment_preflight._WORK_SPECS.
# The active side is authoritative in _WORK_SPECS.  The safe side was audited
# against each owning module's exported state constant where one exists and
# against all repository persistence assignments/model defaults otherwise.
# In particular, trigger_protection_intents.py exports
# ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES, while
# trigger_take_profit_convergence.py and its executor/binding/TP-order paths
# contain the complete persisted convergence transitions. Anything else,
# including NULL and a future state, is refused. A new durable state therefore
# requires an explicit review here before a stopped-service recovery window can
# treat it as quiescent.
#
# Audited persistence sources, in table order below (model defaults/migrations
# were checked as well):
# - deepcoin_execution_operations.py and deepcoin_execution_actions.py;
# - execution_bindings.py, recovery_live_submit.py, and exchange reconciliation;
# - message_instruction_items.py/auto_trade_execution.py and trade_signals.py;
# - instruction_execution_contracts.py and instruction execution adapters;
# - strategy_revision_planner.py and the strategy revision execution paths;
# - strategy_management_batches.py plus planner/executor/reconciliation modules;
# - position_mutation_intents.py/gateway.py and bound close recovery/apply paths;
# - trigger_backup_stop.py/executor.py and position_take_profit_orders.py;
# - position_protection_legs.py and protection repair/reconciliation modules;
# - trigger_protection_intents.py and trigger_protection_rescue_worker.py;
# - trigger_take_profit_convergence.py/executor.py, execution_bindings.py, and
#   position_take_profit_orders.py;
# - break_even_convergence planner/executor/worker modules; and
# - source_message_deletion.py/worker.py.
_SAFE_NONWRITER_STATES = {
    "deepcoin_execution_operations": frozenset(
        {
            "pre_submit_deferred",
            "completed",
            "submission_failed_no_exposure",
        }
    ),
    "execution_order_legs": frozenset(
        {
            "planned",
            "reserved",
            "submitted",
            "pending",
            "open",
            "active",
            "filled",
            "partially_filled",
            "partial",
            "confirmed",
            "succeeded",
            "failed",
            "rejected",
            "cancelled",
            "canceled",
            "manually_cancelled",
            "exchange_cancelled",
            "manually_closed",
            "closed",
            "expired",
            "invalidated",
            "blocked",
        }
    ),
    "message_instruction_items": frozenset(
        {"submitted", "succeeded", "failed"}
    ),
    "trade_signals": frozenset(
        {
            "submitted",
            "recovery_required",
            "confirmed",
            "failed",
            "rejected",
            "blocked",
            "skipped",
            "expired",
            "cancelled",
        }
    ),
    "instruction_execution_contracts": frozenset(
        {"verified", "failed", "expired", "completed"}
    ),
    "strategy_revision_batches": frozenset(
        {"succeeded", "failed", "blocked"}
    ),
    "strategy_management_batches": frozenset(
        {
            "blocked",
            "failed",
            "succeeded",
            "resolved",
            "shadow_planned",
            "completed",
        }
    ),
    "strategy_management_legs": frozenset(
        {
            "confirmed",
            "definitely_rejected",
            "failed",
            "blocked",
            "succeeded",
            "resolved",
            "restored",
            "safely_skipped",
        }
    ),
    "strategy_management_components": frozenset(
        {"blocked", "confirmed", "operator_required", "safely_skipped"}
    ),
    "position_mutation_intents": frozenset(
        {"not_sent", "confirmed", "rejected", "blocked"}
    ),
    _TARGET_TABLE: frozenset({"confirmed"}),
    "position_backup_stop_orders": frozenset(
        {
            "not_sent",
            "active",
            "verified",
            "cancelled",
            "superseded",
            "unverified_exchange",
            "failed",
            "rejected",
            "expired",
            "missing",
        }
    ),
    "position_take_profit_orders": frozenset(
        {
            "active",
            "cancelled",
            "filled",
            "expired",
            "conflicted",
            "completed",
        }
    ),
    "position_protection_legs": frozenset(
        {"verified", "filled", "failed", "blocked", "missing"}
    ),
    "trigger_protection_intents": frozenset(
        {"adopted", "failed", "resolved"}
    ),
    "trigger_protection_stop_rescues": frozenset(
        {"confirmed", "succeeded", "failed", "blocked"}
    ),
    "trigger_take_profit_convergences": frozenset(
        {
            "waiting_position",
            "waiting_backup_stop",
            "completed",
            "conflicted",
            "blocked",
        }
    ),
    "strategy_break_even_convergences": frozenset(
        {
            "blocked",
            "shadow_deciding",
            "shadow_planned",
            "completed",
            "failed_terminal",
            "succeeded",
        }
    ),
    "strategy_break_even_convergence_legs": frozenset(
        {
            "planned",
            "shadow_planned",
            "stop_confirmed",
            "verified",
            "confirmed",
            "succeeded",
            "failed_terminal",
            "blocked",
        }
    ),
    "source_message_deletion_exits": frozenset(
        {"succeeded", "failed", "blocked", "unbound"}
    ),
}


class _WriterQuiescenceError(ValueError):
    pass


def _identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
    return f'"{value}"'


def _unsafe_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    state_column: str,
    time_column: str | None,
    allowed_states: frozenset[str],
) -> list[tuple[object, ...]]:
    if not allowed_states:
        raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
    placeholders = ",".join("?" for _ in allowed_states)
    selected = _identifier(state_column)
    if time_column is not None:
        selected += f", {_identifier(time_column)}"
    rows = connection.execute(
        f"SELECT {selected} FROM {_identifier(table)} "
        f"WHERE {_identifier(state_column)} IS NULL "
        f"OR {_identifier(state_column)} NOT IN ({placeholders}) "
        f"LIMIT {_MAX_INSPECTED_ROWS_PER_TABLE + 1}",
        tuple(sorted(allowed_states)),
    ).fetchall()
    if len(rows) > _MAX_INSPECTED_ROWS_PER_TABLE:
        raise _WriterQuiescenceError(
            "writer_quiescence_inspection_limit_exceeded"
        )
    return rows


def _parse_writer_timestamp(value: object) -> datetime:
    if type(value) is not str or not value:
        raise _WriterQuiescenceError("writer_quiescence_timestamp_invalid")
    is_canonical_naive = _CANONICAL_SQLITE_DATETIME.fullmatch(value) is not None
    is_explicit_utc = _EXPLICIT_UTC_DATETIME.fullmatch(value) is not None
    if not is_canonical_naive and not is_explicit_utc:
        raise _WriterQuiescenceError("writer_quiescence_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _WriterQuiescenceError(
            "writer_quiescence_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        if not is_canonical_naive:
            raise _WriterQuiescenceError(
                "writer_quiescence_timestamp_invalid"
            )
        return parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() != timedelta(0):
        raise _WriterQuiescenceError("writer_quiescence_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) is not datetime or value.tzinfo is None:
        raise _WriterQuiescenceError("writer_quiescence_clock_invalid")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise _WriterQuiescenceError("writer_quiescence_clock_invalid") from exc
    if offset != timedelta(0):
        raise _WriterQuiescenceError("writer_quiescence_clock_invalid")
    return value.astimezone(timezone.utc)


def _build_result(
    *,
    checked_table_count: int,
    missing_table_count: int,
    target_reservation_count: int,
    fresh_active_or_unknown_writer_count: int,
    historical_active_or_unknown_residue_count: int,
    unrecognized_or_null_state_count: int,
    block_regardless_of_age_writer_count: int,
) -> dict[str, object]:
    counts = (
        checked_table_count,
        missing_table_count,
        target_reservation_count,
        fresh_active_or_unknown_writer_count,
        historical_active_or_unknown_residue_count,
        unrecognized_or_null_state_count,
        block_regardless_of_age_writer_count,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        raise _WriterQuiescenceError("writer_quiescence_result_invalid")
    if checked_table_count + missing_table_count != len(_WORK_SPECS):
        raise _WriterQuiescenceError("writer_quiescence_result_invalid")
    blocking = (
        fresh_active_or_unknown_writer_count
        + unrecognized_or_null_state_count
        + block_regardless_of_age_writer_count
    )
    status = (
        "ready"
        if 0 < target_reservation_count <= 64 and blocking == 0
        else "refused"
    )
    result: dict[str, object] = {
        "block_regardless_of_age_writer_count": (
            block_regardless_of_age_writer_count
        ),
        "blocking_writer_count": blocking,
        "checked_table_count": checked_table_count,
        "fresh_active_or_unknown_writer_count": (
            fresh_active_or_unknown_writer_count
        ),
        "historical_active_or_unknown_residue_count": (
            historical_active_or_unknown_residue_count
        ),
        "missing_table_count": missing_table_count,
        "schema_version": 1,
        "status": status,
        "target_reservation_count": target_reservation_count,
        "unrecognized_or_null_state_count": (
            unrecognized_or_null_state_count
        ),
    }
    if set(result) != _OUTPUT_FIELDS:
        raise _WriterQuiescenceError("writer_quiescence_result_invalid")
    return result


def inspect_writer_quiescence(
    database_path: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return only aggregate counts from one coherent read-only snapshot."""

    checked_at = _normalize_now(now)
    cutoff = checked_at - _ACTIVE_WINDOW
    path = Path(database_path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise _WriterQuiescenceError("writer_quiescence_database_invalid")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _WriterQuiescenceError("writer_quiescence_database_invalid") from exc

    spec_tables = tuple(spec.table for spec in _WORK_SPECS)
    if (
        len(set(spec_tables)) != len(spec_tables)
        or set(spec_tables) != set(_SAFE_NONWRITER_STATES)
        or _TARGET_TABLE not in spec_tables
    ):
        raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
    specs_by_table = {spec.table: spec for spec in _WORK_SPECS}
    protection_spec = specs_by_table["trigger_protection_intents"]
    if (
        (
            frozenset(protection_spec.active_states)
            | _SAFE_NONWRITER_STATES["trigger_protection_intents"]
        )
        != ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES
    ):
        raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
    convergence_spec = specs_by_table["trigger_take_profit_convergences"]
    if (
        (
            frozenset(convergence_spec.active_states)
            | _SAFE_NONWRITER_STATES["trigger_take_profit_convergences"]
        )
        != _TRIGGER_TAKE_PROFIT_KNOWN_STATES
    ):
        raise _WriterQuiescenceError("writer_quiescence_contract_invalid")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5,
        )
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise _WriterQuiescenceError("writer_quiescence_query_only_failed")
        connection.execute("BEGIN")
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required_source_tables = frozenset(_REQUIRED_SOURCE_COLUMNS)
        if not required_source_tables.issubset(available):
            raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
        missing_work_tables = frozenset(spec_tables) - available
        if missing_work_tables not in _KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS:
            raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
        checked = 0
        target_count = 0
        fresh_count = 0
        historical_count = 0
        unrecognized_count = 0
        block_regardless_count = 0
        for spec in _WORK_SPECS:
            if spec.table not in available:
                continue
            checked += 1
            columns = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_identifier(spec.table)})"
                ).fetchall()
            }
            if (
                spec.state_column not in columns
                or spec.time_column not in columns
            ):
                raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
            safe_states = _SAFE_NONWRITER_STATES[spec.table]
            active_states = frozenset(spec.active_states)
            known_work_states = active_states | frozenset(spec.unknown_states)
            if spec.table == _TARGET_TABLE:
                if (
                    active_states != _TARGET_STATES
                    or safe_states != frozenset({"confirmed"})
                ):
                    raise _WriterQuiescenceError(
                        "writer_quiescence_contract_invalid"
                    )
                rows = _unsafe_rows(
                    connection,
                    table=spec.table,
                    state_column=spec.state_column,
                    time_column=None,
                    allowed_states=safe_states,
                )
                for (state,) in rows:
                    if type(state) is str and state in _TARGET_STATES:
                        target_count += 1
                    else:
                        unrecognized_count += 1
                continue
            if known_work_states & safe_states:
                raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
            if spec.table == _DEEPCOIN_TABLE:
                block_regardless_count += len(
                    _unsafe_rows(
                        connection,
                        table=spec.table,
                        state_column=spec.state_column,
                        time_column=None,
                        allowed_states=safe_states,
                    )
                )
                continue
            rows = _unsafe_rows(
                connection,
                table=spec.table,
                state_column=spec.state_column,
                time_column=spec.time_column,
                allowed_states=safe_states,
            )
            for state, raw_timestamp in rows:
                if (
                    state is None
                    or type(state) is not str
                    or state not in known_work_states
                ):
                    unrecognized_count += 1
                    continue
                timestamp = _parse_writer_timestamp(raw_timestamp)
                if timestamp >= cutoff:
                    fresh_count += 1
                else:
                    historical_count += 1
        connection.rollback()
    except _WriterQuiescenceError:
        raise
    except (OverflowError, sqlite3.Error, TypeError, ValueError) as exc:
        raise _WriterQuiescenceError("writer_quiescence_read_failed") from exc
    finally:
        if connection is not None:
            connection.close()

    return _build_result(
        checked_table_count=checked,
        missing_table_count=len(_WORK_SPECS) - checked,
        target_reservation_count=target_count,
        fresh_active_or_unknown_writer_count=fresh_count,
        historical_active_or_unknown_residue_count=historical_count,
        unrecognized_or_null_state_count=unrecognized_count,
        block_regardless_of_age_writer_count=block_regardless_count,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 1:
            raise _WriterQuiescenceError("writer_quiescence_arguments_invalid")
        result = inspect_writer_quiescence(arguments[0])
    except (OSError, TypeError, ValueError, OverflowError, sqlite3.Error) as exc:
        reason = (
            str(exc)
            if isinstance(exc, _WriterQuiescenceError)
            else "writer_quiescence_failed"
        )
        result = {"reason_code": reason, "schema_version": 1, "status": "error"}
        code = 1
    else:
        code = 0 if result["status"] == "ready" else 2
    sys.stdout.write(
        json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
