#!/usr/bin/env python3
"""Compare two private bound-close-reservation dry-run captures."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Sequence

from telegram_kol_research.bound_close_reservation_recovery import (
    MAX_RECOVERY_PLAN_BYTES,
    _parse_bound_close_reservation_dry_run_document,
)


_STABLE_OUTPUT = '{"status":"stable"}\n'
_REFUSED_OUTPUT = (
    '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
)


class _ComparisonRefused(ValueError):
    pass


def _read_exact_private_regular_file(path_text: str) -> tuple[bytes, tuple[int, int]]:
    if type(path_text) is not str or not path_text:
        raise _ComparisonRefused("invalid path")
    path = Path(path_text)
    try:
        before = path.lstat()
    except (OSError, ValueError) as exc:
        raise _ComparisonRefused("capture unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _ComparisonRefused("capture is not a regular file")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise _ComparisonRefused("capture permissions are not exact 0600")
    if before.st_size <= 0 or before.st_size > MAX_RECOVERY_PLAN_BYTES:
        raise _ComparisonRefused("capture violates its byte bound")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _ComparisonRefused("opened capture is not regular")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise _ComparisonRefused("opened capture permissions changed")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _ComparisonRefused("capture identity changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(16_384, MAX_RECOVERY_PLAN_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RECOVERY_PLAN_BYTES:
                raise _ComparisonRefused("capture violates its byte bound")
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise _ComparisonRefused("capture changed while reading")
        raw = b"".join(chunks)
        if len(raw) != opened.st_size:
            raise _ComparisonRefused("capture was not read completely")
        return raw, (opened.st_dev, opened.st_ino)
    except OSError as exc:
        raise _ComparisonRefused("capture read failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _captures_are_stable(paths: Sequence[str]) -> bool:
    if type(paths) not in {list, tuple} or len(paths) != 2:
        raise _ComparisonRefused("exactly two capture files are required")
    first_raw, first_file_identity = _read_exact_private_regular_file(paths[0])
    second_raw, second_file_identity = _read_exact_private_regular_file(paths[1])
    if first_file_identity == second_file_identity:
        raise _ComparisonRefused("capture files must be distinct")
    try:
        first = _parse_bound_close_reservation_dry_run_document(first_raw)
        second = _parse_bound_close_reservation_dry_run_document(second_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _ComparisonRefused("capture plan is invalid") from exc
    if first.plan.status != "ready" or second.plan.status != "ready":
        raise _ComparisonRefused("both captures must be ready")
    if first.capture_identity == second.capture_identity:
        raise _ComparisonRefused("capture identity was repeated")
    if first.semantic_json != second.semantic_json:
        raise _ComparisonRefused("capture semantics drifted")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    try:
        stable = _captures_are_stable(paths)
    except (OSError, TypeError, ValueError, OverflowError):
        stable = False
    sys.stdout.write(_STABLE_OUTPUT if stable else _REFUSED_OUTPUT)
    return 0 if stable else 2


if __name__ == "__main__":
    raise SystemExit(main())
