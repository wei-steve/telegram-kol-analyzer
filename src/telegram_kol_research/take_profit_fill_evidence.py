"""Fail-closed proof that an exact first take-profit order actually filled."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class TakeProfitFillEvidence:
    proven: bool
    evidence_tier: str | None
    trigger_order_id: str | None
    filled_size: str | None
    reason_code: str
    evidence: dict[str, Any]


def prove_first_take_profit_fill(
    *,
    tp_order: Any,
    protection_leg: Any,
    expected_side: str,
    previous_observation: Any | None,
    current_observation: Any | None,
    trigger_history: Iterable[Mapping[str, Any]],
    order_history: Iterable[Mapping[str, Any]],
    trade_fills: Iterable[Mapping[str, Any]],
    conflicting_mutations: Iterable[Any],
) -> TakeProfitFillEvidence:
    """Prove a TP1 fill from exact history or a complete position-size transition."""

    order_id = _text(_value(tp_order, "order_id"))
    pos_id = _text(_value(tp_order, "pos_id"))
    size = _positive_decimal_text(_value(tp_order, "size_text"))
    if not order_id or not pos_id or size is None:
        return _failure("take_profit_identity_invalid")
    normalized_side = _text(expected_side)
    if normalized_side is None or normalized_side.lower() not in {"long", "short"}:
        return _failure("take_profit_side_invalid", order_id=order_id)
    if (
        _text(_value(protection_leg, "role")) != "take_profit"
        or _integer(_value(protection_leg, "leg_index")) != 1
    ):
        return _failure("take_profit_leg_not_first", order_id=order_id)
    if (
        _text(_value(protection_leg, "exchange_order_id")) != order_id
        or _text(_value(protection_leg, "pos_id")) != pos_id
        or _positive_decimal_text(_value(protection_leg, "planned_size")) != size
    ):
        return _failure("take_profit_ownership_conflict", order_id=order_id)

    exact_result = _prove_exact_terminal(
        order_id=order_id,
        pos_id=pos_id,
        size=size,
        side=normalized_side,
        sources=(
            ("trigger_history", trigger_history, False),
            ("order_history", order_history, False),
            ("trade_fills", trade_fills, True),
        ),
    )
    if exact_result is not None:
        return exact_result

    conflicts = list(conflicting_mutations)
    if conflicts:
        return _failure(
            "tp1_conflicting_mutation",
            order_id=order_id,
            evidence={"conflict_count": len(conflicts)},
        )
    previous = _normalize_observation(previous_observation)
    current = _normalize_observation(current_observation)
    if previous is None or current is None:
        return _failure("tp1_observation_missing", order_id=order_id)
    if not previous["snapshot_complete"] or not current["snapshot_complete"]:
        return _failure("tp1_snapshot_incomplete", order_id=order_id)
    if previous["pos_id"] != pos_id or current["pos_id"] != pos_id:
        return _failure("tp1_position_identity_changed", order_id=order_id)
    if (
        str(previous["side"] or "").lower() != normalized_side.lower()
        or str(current["side"] or "").lower() != normalized_side.lower()
    ):
        return _failure("tp1_position_side_changed", order_id=order_id)

    previous_orders = {
        row["order_id"]: row for row in previous["pending_tpsl"]
    }
    current_orders = {row["order_id"]: row for row in current["pending_tpsl"]}
    previous_tp = previous_orders.get(order_id)
    if previous_tp is None or previous_tp["size_text"] != size:
        return _failure("tp1_previous_order_not_verified", order_id=order_id)
    if order_id in current_orders:
        return _failure("tp1_order_still_pending", order_id=order_id)
    previous_remaining = {
        key: value for key, value in previous_orders.items() if key != order_id
    }
    if previous_remaining != current_orders:
        return _failure("tp1_remaining_orders_changed", order_id=order_id)

    previous_size = _positive_decimal(previous["size_text"])
    current_size = _positive_decimal(current["size_text"])
    expected_delta = _positive_decimal(size)
    if (
        previous_size is None
        or current_size is None
        or expected_delta is None
        or current_size >= previous_size
        or previous_size - current_size != expected_delta
    ):
        return _failure("tp1_size_delta_mismatch", order_id=order_id)
    return TakeProfitFillEvidence(
        proven=True,
        evidence_tier="exchange_position_delta",
        trigger_order_id=order_id,
        filled_size=size,
        reason_code="tp1_fill_proven",
        evidence={
            "source": "position_reconciliation_observations",
            "previous_size": _format_decimal(previous_size),
            "current_size": _format_decimal(current_size),
            "removed_order_id": order_id,
            "remaining_order_ids": sorted(current_orders),
        },
    )


def _prove_exact_terminal(
    *,
    order_id: str,
    pos_id: str,
    size: str,
    side: str | None,
    sources: Iterable[tuple[str, Iterable[Mapping[str, Any]], bool]],
) -> TakeProfitFillEvidence | None:
    matching_rows: list[tuple[str, Mapping[str, Any], bool]] = []
    for source, rows, fill_source in sources:
        for row in rows:
            if _row_order_id(row) == order_id:
                matching_rows.append((source, row, fill_source))
    if not matching_rows:
        return None
    for source, row, fill_source in matching_rows:
        row_pos_id = _first_text(row, "posId", "pos_id", "closePosId")
        row_side = _first_text(row, "posSide", "pos_side")
        row_size = _positive_decimal_text(
            _first_value(row, "fillSz", "actualSz", "sz", "size")
        )
        if row_pos_id is None or row_side is None or row_size is None:
            return _failure(
                "tp1_exact_history_incomplete",
                order_id=order_id,
                evidence={"source": source},
            )
        if (
            row_pos_id != pos_id
            or side is None
            or row_side.lower() != side.lower()
            or row_size != size
        ):
            return _failure(
                "tp1_exact_history_conflict",
                order_id=order_id,
                evidence={"source": source},
            )
        status = _first_text(row, "state", "status", "ordState")
        is_filled = fill_source or str(status or "").lower() in {
            "filled",
            "success",
            "executed",
        }
        if is_filled:
            return TakeProfitFillEvidence(
                proven=True,
                evidence_tier="exact_order_terminal",
                trigger_order_id=order_id,
                filled_size=size,
                reason_code="tp1_fill_proven",
                evidence={"source": source, "order_id": order_id},
            )
    return _failure(
        "tp1_exact_not_filled",
        order_id=order_id,
        evidence={"sources": sorted({item[0] for item in matching_rows})},
    )


def _normalize_observation(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    pending = _value(value, "pending_tpsl", None)
    if pending is None:
        pending = _value(value, "pending_tpsl_json", "[]")
    if isinstance(pending, str):
        try:
            pending = json.loads(pending)
        except json.JSONDecodeError:
            return None
    if not isinstance(pending, list):
        return None
    normalized_orders = []
    for row in pending:
        if not isinstance(row, Mapping):
            return None
        order_id = _text(row.get("order_id") or row.get("ordId"))
        if not order_id:
            return None
        normalized_orders.append(
            {
                "order_id": order_id,
                "pos_id": _text(row.get("pos_id") or row.get("posId")),
                "position_side": _text(
                    row.get("position_side") or row.get("posSide")
                ),
                "size_text": _positive_decimal_text(
                    row.get("size_text") or row.get("sz")
                ),
                "trigger_price": _decimal_text_or_none(
                    row.get("trigger_price") or row.get("triggerPx")
                ),
            }
        )
    normalized_orders.sort(key=lambda row: row["order_id"])
    return {
        "pos_id": _text(_value(value, "pos_id")),
        "instrument_id": _text(_value(value, "instrument_id")),
        "side": _text(_value(value, "side")),
        "size_text": _positive_decimal_text(_value(value, "size_text")),
        "snapshot_complete": bool(_value(value, "snapshot_complete", False)),
        "pending_tpsl": normalized_orders,
    }


def _observation_text(value: Any | None, field: str) -> str | None:
    return _text(_value(value, field)) if value is not None else None


def _failure(
    reason: str,
    *,
    order_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> TakeProfitFillEvidence:
    return TakeProfitFillEvidence(
        proven=False,
        evidence_tier=None,
        trigger_order_id=order_id,
        filled_size=None,
        reason_code=reason,
        evidence=evidence or {},
    )


def _value(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _row_order_id(row: Mapping[str, Any]) -> str | None:
    return _first_text(row, "ordId", "orderId", "order_id", "id")


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(payload, *keys)
    return _text(value)


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_decimal_text(value: Any) -> str | None:
    parsed = _positive_decimal(value)
    return _format_decimal(parsed) if parsed is not None else None


def _decimal_text_or_none(value: Any) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return _format_decimal(parsed)


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized == "-0" else normalized
