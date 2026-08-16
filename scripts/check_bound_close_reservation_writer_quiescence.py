#!/usr/bin/env python3
"""Read-only, aggregate writer-quiescence check for reservation recovery."""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Sequence

from telegram_kol_research.bound_close_writer_quiescence import (
    _MAX_INSPECTED_ROWS_PER_TABLE,
    _WriterQuiescenceError,
    _build_result,
    inspect_bound_close_writer_quiescence,
)


# Compatibility for reviewed callers while the library becomes the single
# implementation authority.
inspect_writer_quiescence = inspect_bound_close_writer_quiescence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 1:
            raise _WriterQuiescenceError("writer_quiescence_arguments_invalid")
        result = inspect_bound_close_writer_quiescence(arguments[0])
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
