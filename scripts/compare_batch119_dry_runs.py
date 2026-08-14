#!/usr/bin/env python3
"""Fail-closed semantic comparison for two batch-119 dry-run files."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from typing import Any


MAX_FILE_BYTES = 1_048_576
MAX_NODES = 20_000
MAX_DEPTH = 64
MAX_STRING_BYTES = 16_384
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_CODE_RE = re.compile(r"[a-z0-9_]{1,96}")
DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
OUTER_KEYS = {"mode", "plan"}
PLAN_KEYS = {
    "batch_id",
    "status",
    "reason_code",
    "position",
    "source_fingerprint",
    "exchange_snapshot_fingerprint",
    "evidence_fingerprint",
    "evidence",
    "production_writes",
    "exchange_calls",
}
POSITION_KEYS = {
    "disposition",
    "current_size",
    "close_delta",
    "effective_remaining_size",
}


class ComparisonRefused(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ComparisonRefused("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ComparisonRefused("non-finite number")


def _bounded_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ComparisonRefused("file unavailable")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ComparisonRefused("file too large")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError):
        raise ComparisonRefused("invalid JSON") from None

    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ComparisonRefused("JSON bounds exceeded")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ComparisonRefused("invalid key")
                try:
                    key_size = len(key.encode("utf-8"))
                except UnicodeError:
                    raise ComparisonRefused("invalid key") from None
                if key_size > 256:
                    raise ComparisonRefused("key too long")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            try:
                string_size = len(current.encode("utf-8"))
            except UnicodeError:
                raise ComparisonRefused("invalid string") from None
            if string_size > MAX_STRING_BYTES:
                raise ComparisonRefused("string too long")
        elif current is not None and not isinstance(
            current, (bool, int, float)
        ):
            raise ComparisonRefused("invalid JSON value")
    if not isinstance(value, dict):
        raise ComparisonRefused("document must be object")
    return value


def _strict_decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise ComparisonRefused("invalid decimal")
    if DECIMAL_RE.fullmatch(value) is None:
        raise ComparisonRefused("invalid decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ComparisonRefused("invalid decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise ComparisonRefused("invalid decimal")
    return parsed


def _validate_position(position: Any) -> None:
    if not isinstance(position, dict) or set(position) != POSITION_KEYS:
        raise ComparisonRefused("invalid position")
    disposition = position.get("disposition")
    current = _strict_decimal(position.get("current_size"))
    close = _strict_decimal(position.get("close_delta"))
    remaining = _strict_decimal(position.get("effective_remaining_size"))
    target = Decimal("19")
    valid = {
        "position_absent": current == close == remaining == 0,
        "resume_to_target": (
            current > target
            and close == current - target
            and remaining == target
        ),
        "protection_only_at_target": (
            current == target and close == 0 and remaining == target
        ),
        "protection_only_below_target": (
            0 < current < target and close == 0 and remaining == current
        ),
    }
    if disposition not in valid or not valid[disposition]:
        raise ComparisonRefused("invalid disposition")


def _validate_document(document: dict[str, Any]) -> None:
    if set(document) != OUTER_KEYS or document.get("mode") != "dry_run":
        raise ComparisonRefused("invalid envelope")
    plan = document.get("plan")
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise ComparisonRefused("invalid plan keys")
    if (
        plan.get("batch_id") != 119
        or isinstance(plan.get("batch_id"), bool)
        or plan.get("status") != "ready"
        or not isinstance(plan.get("reason_code"), str)
        or SAFE_CODE_RE.fullmatch(plan["reason_code"]) is None
        or plan.get("production_writes") != 0
        or isinstance(plan.get("production_writes"), bool)
        or plan.get("exchange_calls") != 0
        or isinstance(plan.get("exchange_calls"), bool)
        or not isinstance(plan.get("evidence"), dict)
    ):
        raise ComparisonRefused("unsafe plan")
    for key in (
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
    ):
        if not isinstance(plan.get(key), str) or SHA256_RE.fullmatch(
            plan[key]
        ) is None:
            raise ComparisonRefused("invalid fingerprint")
    _validate_position(plan.get("position"))


def _emit(payload: dict[str, str]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise ComparisonRefused("two files required")
        left = _bounded_json(Path(argv[0]))
        right = _bounded_json(Path(argv[1]))
        _validate_document(left)
        _validate_document(right)
        if left["plan"] != right["plan"]:
            raise ComparisonRefused("semantic drift")
    except Exception:
        _emit(
            {
                "reason_code": "dry_run_comparison_refused",
                "status": "refused",
            }
        )
        return 2
    _emit({"status": "stable"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
