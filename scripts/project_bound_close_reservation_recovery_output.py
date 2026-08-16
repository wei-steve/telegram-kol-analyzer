#!/usr/bin/env python3
"""Project private recovery JSON onto the operator-safe aggregate fields."""

from __future__ import annotations

import json
import re
import sys
from typing import Sequence

from telegram_kol_research.bound_close_reservation_recovery import (
    MAX_RECOVERY_PLAN_BYTES,
    _parse_bound_close_reservation_dry_run_document,
)


_APPLY_KEYS = frozenset(
    {
        "action_count",
        "audit_event_id",
        "evidence_fingerprint",
        "mode",
        "schema_version",
        "status",
    }
)
_APPLY_STATUSES = frozenset(
    {"applied", "applied_after_deadline_verified", "already_applied"}
)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_CAPTURE_SAFE_KEYS = (
    "action_count",
    "counts",
    "database_writes",
    "evidence_fingerprint",
    "exchange_snapshot_fingerprint",
    "exchange_writes",
    "history_replays",
    "source_fingerprint",
    "status",
)


class _ProjectionRefused(ValueError):
    pass


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _ProjectionRefused("duplicate or invalid key")
        result[key] = value
    return result


def _read_bounded_stdin() -> bytes:
    raw = sys.stdin.buffer.read(MAX_RECOVERY_PLAN_BYTES + 1)
    if not raw or len(raw) > MAX_RECOVERY_PLAN_BYTES:
        raise _ProjectionRefused("input byte bound violated")
    return raw


def _capture_projection(raw: bytes) -> dict[str, object]:
    _parse_bound_close_reservation_dry_run_document(raw)
    payload = json.loads(
        raw,
        object_pairs_hook=_closed_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            _ProjectionRefused("non-finite number")
        ),
    )
    if type(payload) is not dict:
        raise _ProjectionRefused("capture shape invalid")
    return {key: payload[key] for key in _CAPTURE_SAFE_KEYS}


def _apply_projection(raw: bytes) -> dict[str, object]:
    payload = json.loads(
        raw,
        object_pairs_hook=_closed_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            _ProjectionRefused("non-finite number")
        ),
    )
    if type(payload) is not dict or set(payload) != _APPLY_KEYS:
        raise _ProjectionRefused("apply result shape invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["mode"] != "apply"
    ):
        raise _ProjectionRefused("apply result contract invalid")
    if type(payload["status"]) is not str or payload["status"] not in _APPLY_STATUSES:
        raise _ProjectionRefused("apply result status invalid")
    if type(payload["action_count"]) is not int or payload["action_count"] <= 0:
        raise _ProjectionRefused("apply result count invalid")
    if type(payload["audit_event_id"]) is not int or payload["audit_event_id"] <= 0:
        raise _ProjectionRefused("apply result audit invalid")
    fingerprint = payload["evidence_fingerprint"]
    if type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise _ProjectionRefused("apply result fingerprint invalid")
    return {
        "action_count": payload["action_count"],
        "evidence_fingerprint": fingerprint,
        "status": payload["status"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments not in (["capture"], ["apply-result"]):
            raise _ProjectionRefused("projection kind invalid")
        raw = _read_bounded_stdin()
        projected = (
            _capture_projection(raw)
            if arguments[0] == "capture"
            else _apply_projection(raw)
        )
    except (
        KeyError,
        RecursionError,
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        sys.stdout.write('{"status":"refused"}\n')
        return 2
    sys.stdout.write(
        json.dumps(
            projected,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
