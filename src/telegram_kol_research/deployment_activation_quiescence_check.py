from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import sqlite3
import sys

from telegram_kol_research.deployment_active_write_check import (
    ActiveWriteCheckError,
    count_active_exchange_writes_in_connection,
)


_AUTHORITY_KEY = "entry_revision_exchange_authority"


class ActivationQuiescenceError(ValueError):
    pass


def inspect_activation_quiescence(database_path: str | Path) -> int:
    connection: sqlite3.Connection | None = None
    failed = False
    cleanup_failed = False
    count = 0
    try:
        database = Path(database_path).resolve()
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone() != (1,):
            raise ActivationQuiescenceError("activation_quiescence_unknown")
        connection.execute("BEGIN")
        count = count_active_exchange_writes_in_connection(connection)
        rows = connection.execute(
            "SELECT value_json FROM trading_settings WHERE key = ?",
            (_AUTHORITY_KEY,),
        ).fetchall()
        if len(rows) != 1:
            raise ActivationQuiescenceError("activation_quiescence_unknown")
        try:
            document = json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ActivationQuiescenceError("activation_quiescence_unknown") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"released_at", "schema_version", "state"}
            or document.get("schema_version") != 1
            or document.get("state") != "idle"
            or not isinstance(document.get("released_at"), str)
            or not document["released_at"].strip()
        ):
            raise ActivationQuiescenceError("activation_quiescence_unknown")
        try:
            released_at = datetime.fromisoformat(
                document["released_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ActivationQuiescenceError("activation_quiescence_unknown") from exc
        if released_at.tzinfo is None:
            raise ActivationQuiescenceError("activation_quiescence_unknown")
    except (sqlite3.Error, OSError, ActiveWriteCheckError, ActivationQuiescenceError):
        failed = True
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                cleanup_failed = True
            try:
                connection.close()
            except Exception:
                cleanup_failed = True
    if failed or cleanup_failed:
        raise ActivationQuiescenceError("activation_quiescence_unknown") from None
    return count


def main() -> int:
    if len(sys.argv) != 2:
        print("ERROR activation_quiescence_unknown", file=sys.stderr)
        return 4
    try:
        count = inspect_activation_quiescence(sys.argv[1])
    except ActivationQuiescenceError:
        print("ERROR activation_quiescence_unknown", file=sys.stderr)
        return 4
    print(f"active_write_count={count} global_authority_state=idle")
    return 0 if count == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
