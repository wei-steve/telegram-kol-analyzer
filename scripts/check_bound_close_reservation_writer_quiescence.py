#!/usr/bin/env python3
"""Read-only, aggregate writer-quiescence check for reservation recovery."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Sequence

from telegram_kol_research.deployment_preflight import _WORK_SPECS


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_TARGET_TABLE = "bound_position_close_reservations"
_TARGET_STATES = frozenset(
    {
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    }
)

# Closed non-writer states for every table in deployment_preflight._WORK_SPECS.
# Anything else, including NULL and a future state, is refused. A new durable
# state therefore requires an explicit review here before a stopped-service
# recovery window can treat it as quiescent.
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
        {"adopted", "failed", "blocked"}
    ),
    "trigger_protection_stop_rescues": frozenset(
        {"confirmed", "succeeded", "failed", "blocked"}
    ),
    "trigger_take_profit_convergences": frozenset(
        {"waiting_position", "completed", "conflicted", "blocked"}
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
        {"succeeded", "failed", "blocked"}
    ),
}


class _WriterQuiescenceError(ValueError):
    pass


def _identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
    return f'"{value}"'


def _count_where(
    connection: sqlite3.Connection,
    *,
    table: str,
    state_column: str,
    allowed_states: frozenset[str],
) -> int:
    if not allowed_states:
        raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
    placeholders = ",".join("?" for _ in allowed_states)
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_identifier(table)} "
        f"WHERE {_identifier(state_column)} IS NULL "
        f"OR {_identifier(state_column)} NOT IN ({placeholders})",
        tuple(sorted(allowed_states)),
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise _WriterQuiescenceError("writer_quiescence_read_invalid")
    return int(row[0])


def inspect_writer_quiescence(database_path: str | Path) -> dict[str, object]:
    """Return only aggregate counts from one coherent read-only snapshot."""

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
        checked = 0
        target_count = 0
        other_count = 0
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
            if spec.state_column not in columns:
                raise _WriterQuiescenceError("writer_quiescence_schema_invalid")
            safe_states = _SAFE_NONWRITER_STATES[spec.table]
            active_states = frozenset(spec.active_states)
            if spec.table == _TARGET_TABLE:
                if active_states != _TARGET_STATES:
                    raise _WriterQuiescenceError(
                        "writer_quiescence_contract_invalid"
                    )
                target_placeholders = ",".join("?" for _ in _TARGET_STATES)
                target_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {_identifier(spec.table)} "
                        f"WHERE {_identifier(spec.state_column)} "
                        f"IN ({target_placeholders})",
                        tuple(sorted(_TARGET_STATES)),
                    ).fetchone()[0]
                )
                other_count += _count_where(
                    connection,
                    table=spec.table,
                    state_column=spec.state_column,
                    allowed_states=_TARGET_STATES | safe_states,
                )
                continue
            if active_states & safe_states:
                raise _WriterQuiescenceError("writer_quiescence_contract_invalid")
            other_count += _count_where(
                connection,
                table=spec.table,
                state_column=spec.state_column,
                allowed_states=safe_states,
            )
        connection.rollback()
    except _WriterQuiescenceError:
        raise
    except (OverflowError, sqlite3.Error, TypeError, ValueError) as exc:
        raise _WriterQuiescenceError("writer_quiescence_read_failed") from exc
    finally:
        if connection is not None:
            connection.close()

    status = (
        "ready"
        if 0 < target_count <= 64 and other_count == 0
        else "refused"
    )
    return {
        "checked_table_count": checked,
        "missing_table_count": len(_WORK_SPECS) - checked,
        "other_active_or_unknown_writer_count": other_count,
        "schema_version": 1,
        "status": status,
        "target_reservation_count": target_count,
    }


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
