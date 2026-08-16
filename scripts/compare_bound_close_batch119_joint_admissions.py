#!/usr/bin/env python3
"""Strictly compare two private joint-recovery admission documents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence


MAX_ADMISSION_BYTES = 65_536
MAX_ADMISSION_NODES = 128
MAX_ADMISSION_DEPTH = 8
MAX_ADMISSION_STRING_BYTES = 256
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
_PHASES = frozenset(
    {"joint_diagnostic", "bound_apply_pre", "bound_apply_post"}
)
_KEYS = frozenset(
    {
        "batch119_incident_count",
        "batch119_material_fingerprint",
        "blocking_writer_count",
        "capture_completed_at",
        "capture_id",
        "capture_started_at",
        "material_fingerprint",
        "phase",
        "reason_code",
        "reservation_count",
        "schema_version",
        "status",
    }
)
_STABLE = '{"status":"stable"}\n'
_REFUSED = '{"status":"refused"}\n'


class JointAdmissionRefused(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise JointAdmissionRefused("invalid object")
        result[key] = value
    return result


def _bounded_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_ADMISSION_BYTES:
        raise JointAdmissionRefused("invalid bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                JointAdmissionRefused("invalid number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError):
        raise JointAdmissionRefused("invalid json") from None
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_ADMISSION_NODES or depth > MAX_ADMISSION_DEPTH:
            raise JointAdmissionRefused("invalid shape")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_ADMISSION_STRING_BYTES:
                raise JointAdmissionRefused("invalid string")
        elif current is not None and type(current) not in {bool, int, float}:
            raise JointAdmissionRefused("invalid value")
    if type(value) is not dict:
        raise JointAdmissionRefused("invalid document")
    return value


def _utc_timestamp(value: Any) -> datetime:
    if type(value) is not str or _UTC_TIME.fullmatch(value) is None:
        raise JointAdmissionRefused("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        raise JointAdmissionRefused("invalid timestamp") from None
    if parsed.utcoffset() != timedelta(0):
        raise JointAdmissionRefused("invalid timestamp")
    return parsed.astimezone(timezone.utc)


def _parse_document_bytes(raw: bytes) -> tuple[dict[str, Any], datetime, datetime]:
    value = _bounded_json(raw)
    if set(value) != _KEYS:
        raise JointAdmissionRefused("invalid fields")
    exact_ints = {
        "schema_version": 1,
        "reservation_count": 29,
        "batch119_incident_count": 1,
        "blocking_writer_count": 0,
    }
    if any(
        type(value[field]) is not int or value[field] != expected
        for field, expected in exact_ints.items()
    ):
        raise JointAdmissionRefused("invalid counts")
    if (
        value["status"] != "ready"
        or value["reason_code"] is not None
        or type(value["phase"]) is not str
        or value["phase"] not in _PHASES
        or type(value["capture_id"]) is not str
        or _HEX_64.fullmatch(value["capture_id"]) is None
        or type(value["material_fingerprint"]) is not str
        or _HEX_64.fullmatch(value["material_fingerprint"]) is None
        or type(value["batch119_material_fingerprint"]) is not str
        or _HEX_64.fullmatch(value["batch119_material_fingerprint"]) is None
    ):
        raise JointAdmissionRefused("invalid authority")
    started = _utc_timestamp(value["capture_started_at"])
    completed = _utc_timestamp(value["capture_completed_at"])
    if completed <= started:
        raise JointAdmissionRefused("invalid window")
    return value, started, completed


def _read_private_file(
    path_text: str,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    path = Path(path_text)
    before = path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size <= 0
        or before.st_size > MAX_ADMISSION_BYTES
    ):
        raise JointAdmissionRefused("invalid file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise JointAdmissionRefused("invalid file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(16_384, MAX_ADMISSION_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ADMISSION_BYTES:
                raise JointAdmissionRefused("invalid file")
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) != opened.st_size
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise JointAdmissionRefused("unstable file")
        return raw, (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
    finally:
        os.close(descriptor)


def _revalidate_private_path(
    path_text: str,
    expected: tuple[int, int, int, int, int, int],
) -> None:
    current = Path(path_text).lstat()
    observed = (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    if (
        observed != expected
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) != 0o600
    ):
        raise JointAdmissionRefused("file changed after read")


def _admissions_are_stable(paths: Sequence[str]) -> bool:
    if type(paths) not in {list, tuple} or len(paths) != 2:
        raise JointAdmissionRefused("exactly two files required")
    first_raw, first_identity = _read_private_file(paths[0])
    second_raw, second_identity = _read_private_file(paths[1])
    if first_identity[:2] == second_identity[:2]:
        raise JointAdmissionRefused("files repeated")
    first, _first_started, first_completed = _parse_document_bytes(first_raw)
    second, second_started, second_completed = _parse_document_bytes(second_raw)
    if (
        first["capture_id"] == second["capture_id"]
        or first_completed >= second_started
        or second_completed < second_started
        or first["phase"] != second["phase"]
        or first["material_fingerprint"] != second["material_fingerprint"]
        or first["batch119_material_fingerprint"]
        != second["batch119_material_fingerprint"]
        or any(
            first[field] != second[field]
            for field in (
                "reservation_count",
                "batch119_incident_count",
                "blocking_writer_count",
                "status",
                "reason_code",
            )
        )
    ):
        raise JointAdmissionRefused("admissions drifted")
    _revalidate_private_path(paths[0], first_identity)
    _revalidate_private_path(paths[1], second_identity)
    return True


def _bound_apply_transition_is_stable(paths: Sequence[str]) -> bool:
    if type(paths) not in {list, tuple} or len(paths) != 2:
        raise JointAdmissionRefused("exactly two files required")
    first_raw, first_identity = _read_private_file(paths[0])
    second_raw, second_identity = _read_private_file(paths[1])
    if first_identity[:2] == second_identity[:2]:
        raise JointAdmissionRefused("files repeated")
    first, _first_started, first_completed = _parse_document_bytes(first_raw)
    second, second_started, second_completed = _parse_document_bytes(second_raw)
    if (
        first["phase"] != "bound_apply_pre"
        or second["phase"] != "bound_apply_post"
        or first["capture_id"] == second["capture_id"]
        or first_completed >= second_started
        or second_completed < second_started
        or first["material_fingerprint"] == second["material_fingerprint"]
        or first["batch119_material_fingerprint"]
        != second["batch119_material_fingerprint"]
        or any(
            first[field] != second[field]
            for field in (
                "reservation_count",
                "batch119_incident_count",
                "blocking_writer_count",
                "status",
                "reason_code",
            )
        )
    ):
        raise JointAdmissionRefused("bound apply authority drifted")
    _revalidate_private_path(paths[0], first_identity)
    _revalidate_private_path(paths[1], second_identity)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    try:
        if paths[:1] == ["--bound-apply-transition"]:
            stable = _bound_apply_transition_is_stable(paths[1:])
        else:
            stable = _admissions_are_stable(paths)
    except (OSError, TypeError, ValueError, OverflowError, RecursionError):
        stable = False
    sys.stdout.write(_STABLE if stable else _REFUSED)
    return 0 if stable else 2


if __name__ == "__main__":
    raise SystemExit(main())
