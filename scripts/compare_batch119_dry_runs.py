#!/usr/bin/env python3
"""Fail-closed semantic comparison for two batch-119 dry-run files."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
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
EVIDENCE_KEYS = {
    "schema_version",
    "batch_id",
    "decision",
    "reason_code",
    "source_fingerprint",
    "exchange_snapshot_fingerprint",
    "immutable_target",
    "position",
    "durable",
    "exchange",
    "proposed_transition",
    "natural_stop",
}
INSTRUCTION_DISPOSITIONS = {
    "approved_historical_pending_frozen",
    "historical_unknown_frozen",
    "target_incident_frozen",
    "verified_terminal_mirror",
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
    if position.get("disposition") != "position_absent":
        raise ComparisonRefused("invalid disposition")
    if position.get("current_size") is not None:
        raise ComparisonRefused("invalid absent size")
    close = _strict_decimal(position.get("close_delta"))
    remaining = _strict_decimal(position.get("effective_remaining_size"))
    if close != 0 or remaining != 0:
        raise ComparisonRefused("invalid absent economics")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _canonical_fingerprint(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ComparisonRefused("invalid fingerprint payload") from None
    return sha256(encoded).hexdigest()


def _validate_natural_stop(value: Any) -> None:
    expected_keys = {
        "purpose",
        "trigger_status",
        "position_status",
        "time_relation",
        "trigger_count",
        "closed_position_count",
        "order_ref",
        "position_ref",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ComparisonRefused("invalid natural stop")
    if not (
        value.get("purpose") in {"stop_loss", "backup_stop"}
        and value.get("trigger_status") == "successful_terminal"
        and value.get("position_status") == "closed"
        and value.get("time_relation") == "trigger_not_after_close"
        and value.get("trigger_count") == 1
        and not isinstance(value.get("trigger_count"), bool)
        and value.get("closed_position_count") == 1
        and not isinstance(value.get("closed_position_count"), bool)
        and _is_sha256(value.get("order_ref"))
        and _is_sha256(value.get("position_ref"))
    ):
        raise ComparisonRefused("invalid natural stop")


def _validate_instruction_population(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "total_count",
        "counts",
        "digest",
    }:
        raise ComparisonRefused("invalid instruction population")
    total = value.get("total_count")
    counts = value.get("counts")
    if not (
        value.get("schema_version") == 1
        and isinstance(total, int)
        and not isinstance(total, bool)
        and 1 <= total <= 4_096
        and isinstance(counts, dict)
        and set(counts) == INSTRUCTION_DISPOSITIONS
        and all(
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for count in counts.values()
        )
        and sum(counts.values()) == total
        and counts.get("target_incident_frozen") == 1
        and _is_sha256(value.get("digest"))
    ):
        raise ComparisonRefused("invalid instruction population")


def _validate_evidence(plan: dict[str, Any]) -> None:
    evidence = plan.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise ComparisonRefused("invalid evidence keys")
    if not (
        evidence.get("schema_version") == 1
        and evidence.get("batch_id") == 119
        and evidence.get("decision") == "repair_false_legacy_submission"
        and evidence.get("reason_code") == "false_legacy_submission_proven"
        and evidence.get("source_fingerprint")
        == plan.get("source_fingerprint")
        and evidence.get("exchange_snapshot_fingerprint")
        == plan.get("exchange_snapshot_fingerprint")
        and evidence.get("position") == plan.get("position")
    ):
        raise ComparisonRefused("evidence identity mismatch")

    target = evidence.get("immutable_target")
    if not isinstance(target, dict) or set(target) != {
        "instrument_id",
        "side",
        "trusted_start_size",
        "target_remaining_size",
        "quantity_step",
        "min_quantity",
    }:
        raise ComparisonRefused("invalid immutable target")
    if not (
        target.get("instrument_id") == "BTC-USDT-SWAP"
        and target.get("side") == "long"
        and target.get("trusted_start_size") == "38"
        and target.get("target_remaining_size") == "19"
        and _strict_decimal(target.get("quantity_step")) > 0
        and _strict_decimal(target.get("min_quantity")) > 0
    ):
        raise ComparisonRefused("invalid immutable target")

    durable = evidence.get("durable")
    if not isinstance(durable, dict) or set(durable) != {
        "batch_status",
        "leg_status",
        "component_statuses",
        "component_attempt_counts",
        "component_count",
        "close_submission_evidence_count",
        "instruction_population",
    }:
        raise ComparisonRefused("invalid durable evidence")
    statuses = durable.get("component_statuses")
    attempts = durable.get("component_attempt_counts")
    if not (
        isinstance(durable.get("batch_status"), str)
        and SAFE_CODE_RE.fullmatch(durable["batch_status"]) is not None
        and isinstance(durable.get("leg_status"), str)
        and SAFE_CODE_RE.fullmatch(durable["leg_status"]) is not None
        and isinstance(statuses, list)
        and len(statuses) == 3
        and all(
            isinstance(status, str)
            and SAFE_CODE_RE.fullmatch(status) is not None
            for status in statuses
        )
        and isinstance(attempts, list)
        and len(attempts) == 3
        and all(
            isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and attempt >= 0
            for attempt in attempts
        )
        and durable.get("component_count") == 3
        and not isinstance(durable.get("component_count"), bool)
        and durable.get("close_submission_evidence_count") == 0
        and not isinstance(
            durable.get("close_submission_evidence_count"), bool
        )
    ):
        raise ComparisonRefused("invalid durable evidence")
    _validate_instruction_population(durable.get("instruction_population"))

    exchange = evidence.get("exchange")
    if not isinstance(exchange, dict) or set(exchange) != {
        "snapshot_complete",
        "exact_position_count",
        "regular_close_evidence_count",
        "owned_protection_count",
    }:
        raise ComparisonRefused("invalid exchange evidence")
    owned_count = exchange.get("owned_protection_count")
    if not (
        exchange.get("snapshot_complete") is True
        and exchange.get("exact_position_count") == 0
        and not isinstance(exchange.get("exact_position_count"), bool)
        and exchange.get("regular_close_evidence_count") == 0
        and not isinstance(
            exchange.get("regular_close_evidence_count"), bool
        )
        and isinstance(owned_count, int)
        and not isinstance(owned_count, bool)
        and 2 <= owned_count <= 100
    ):
        raise ComparisonRefused("invalid exchange evidence")

    if evidence.get("proposed_transition") != {
        "batch_status": "resolved",
        "batch_reason_code": "composite_recovery_exact_position_absent",
        "leg_status": "failed",
        "component_statuses": [
            "safely_skipped",
            "safely_skipped",
            "safely_skipped",
        ],
        "exchange_call_possible": False,
    }:
        raise ComparisonRefused("invalid transition")
    _validate_natural_stop(evidence.get("natural_stop"))
    if _canonical_fingerprint(evidence) != plan.get("evidence_fingerprint"):
        raise ComparisonRefused("evidence fingerprint mismatch")


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
        or plan.get("reason_code") != "false_legacy_submission_proven"
        or plan.get("production_writes") != 0
        or isinstance(plan.get("production_writes"), bool)
        or plan.get("exchange_calls") != 0
        or isinstance(plan.get("exchange_calls"), bool)
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
    _validate_evidence(plan)


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
