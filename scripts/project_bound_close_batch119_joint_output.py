#!/usr/bin/env python3
"""Project a private joint admission onto fixed operator-safe counts."""

from __future__ import annotations

import json
import sys
from typing import Sequence

from compare_bound_close_batch119_joint_admissions import (
    MAX_ADMISSION_BYTES,
    _parse_document_bytes,
)


_REFUSED = '{"status":"refused"}\n'


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments:
            raise ValueError("arguments refused")
        raw = sys.stdin.buffer.read(MAX_ADMISSION_BYTES + 1)
        payload, _started, _completed = _parse_document_bytes(raw)
        projection = {
            "batch119_incident_count": payload["batch119_incident_count"],
            "blocking_writer_count": payload["blocking_writer_count"],
            "reservation_count": payload["reservation_count"],
            "schema_version": payload["schema_version"],
            "status": payload["status"],
        }
    except (
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        sys.stdout.write(_REFUSED)
        return 2
    sys.stdout.write(
        json.dumps(
            projection,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
