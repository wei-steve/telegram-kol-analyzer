"""Explain one partial position reduction as an owned take-profit fill.

Convergence 222 is the reason this module exists. Three staged take-profit
orders were built for a ten-lot position; TP1 (five lots, trigger 81100) filled
at 2026-09-04T08:34:43Z, and eight seconds later the convergence audit saw a
five-lot position against a ten-lot plan, called it
``convergence_partial_position_unexplained``, and froze. The stop order and the
protection ledger both stayed at ten. Nothing was wrong except that the audit
had no way to say "that reduction is the take-profit we ourselves placed".

The judgement here is deliberately narrow. All three of the following must hold
at once, and any one of them missing keeps the freeze:

1. the reduction is *exactly* the size of one take-profit order,
2. that order's ``ordId`` is in this binding's own take-profit ledger, and
3. the exchange's ``trigger-orders-history`` shows the order actually
   triggered (non-zero ``triggerTime``, no error code).

Deliberately absent: any inference from symbol, side, price proximity, time
proximity, or "nothing else was running". A reduction this module cannot name
is a reduction a person has to look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


PARTIAL_TAKE_PROFIT_FILLED = "partial_take_profit_filled"

# ``triggerTime`` values that mean "never triggered". Deepcoin sends the field
# on every pending trigger order, so its presence proves nothing on its own.
_UNTRIGGERED_TIMES = frozenset({"0", "0.0", "0.00", ""})

_CLEAN_ERROR_CODES = frozenset({"", "0", "00000"})


@dataclass(frozen=True, slots=True)
class PartialTakeProfitExplanation:
    """The verdict plus every field it was read from, never just the verdict."""

    explained: bool
    reason_code: str
    order_id: str | None = None
    filled_size: str | None = None
    remaining_size: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def explain_partial_position_reduction(
    *,
    execution_binding_id: int,
    pos_id: str,
    planned_size: Decimal,
    live_size: Decimal,
    take_profit_orders: Iterable[Any],
    trigger_history: Iterable[Mapping[str, Any]],
) -> PartialTakeProfitExplanation:
    """Return whether one owned take-profit order explains the whole reduction."""

    live = _decimal(live_size)
    planned = _decimal(planned_size)
    if planned is None or live is None:
        return _refuse("partial_reduction_size_unreadable")
    reduction = planned - live
    base_evidence: dict[str, Any] = {
        "execution_binding_id": int(execution_binding_id),
        "pos_id": str(pos_id or ""),
        "planned_size": _text(planned),
        "live_size": _text(live),
        "reduction_size": _text(reduction),
    }
    if reduction <= 0:
        return _refuse("partial_reduction_not_positive", evidence=base_evidence)

    rows = list(take_profit_orders)
    ledger_order_ids = sorted(
        {
            str(getattr(row, "order_id", "") or "").strip()
            for row in rows
            if str(getattr(row, "order_id", "") or "").strip()
        }
    )
    base_evidence["binding_take_profit_order_ids"] = ledger_order_ids

    # Criterion 1 + 2 together: the matching order must be one of *ours*, and
    # exactly one of ours may match. Two same-sized take-profit orders make the
    # reduction ambiguous, and an ambiguous reduction is an unexplained one.
    candidates = [
        row
        for row in rows
        if int(getattr(row, "execution_binding_id", -1) or -1)
        == int(execution_binding_id)
        and str(getattr(row, "pos_id", "") or "").strip() == str(pos_id or "").strip()
        and str(getattr(row, "order_id", "") or "").strip()
        and _decimal(getattr(row, "size_text", None)) == reduction
    ]
    if not candidates:
        return _refuse(
            "partial_reduction_size_matches_no_owned_take_profit",
            evidence={
                **base_evidence,
                "owned_take_profit_sizes": [
                    _text(_decimal(getattr(row, "size_text", None)))
                    for row in rows
                ],
            },
        )
    if len(candidates) > 1:
        return _refuse(
            "partial_reduction_take_profit_ambiguous",
            evidence={
                **base_evidence,
                "candidate_order_ids": sorted(
                    str(row.order_id).strip() for row in candidates
                ),
            },
        )
    candidate = candidates[0]
    order_id = str(candidate.order_id).strip()
    evidence = {
        **base_evidence,
        "order_id": order_id,
        "order_size_text": str(candidate.size_text),
        "order_status": str(getattr(candidate, "status", "") or ""),
        "order_trigger_price": str(getattr(candidate, "trigger_price", "") or ""),
    }

    # Criterion 3: the exchange has to say the order fired. A pending order of
    # the same size is not evidence of anything.
    history_rows = [
        row
        for row in trigger_history
        if isinstance(row, Mapping) and _row_order_id(row) == order_id
    ]
    if not history_rows:
        return _refuse(
            "partial_reduction_trigger_history_missing",
            order_id=order_id,
            evidence=evidence,
        )
    if len(history_rows) > 1:
        return _refuse(
            "partial_reduction_trigger_history_ambiguous",
            order_id=order_id,
            evidence={**evidence, "history_row_count": len(history_rows)},
        )
    history_row = history_rows[0]
    trigger_time = _row_text(history_row, "triggerTime", "trigger_time")
    error_code = _row_text(history_row, "errorCode", "error_code", "sCode")
    history_pos_id = _row_text(
        history_row, "closePosId", "posId", "pos_id", "positionId"
    )
    evidence["trigger_history"] = {
        "triggerTime": trigger_time,
        "errorCode": error_code,
        "posId": history_pos_id,
        "state": _row_text(history_row, "state", "status", "ordState"),
    }
    if trigger_time in _UNTRIGGERED_TIMES:
        return _refuse(
            "partial_reduction_take_profit_not_triggered",
            order_id=order_id,
            evidence=evidence,
        )
    if error_code not in _CLEAN_ERROR_CODES:
        return _refuse(
            "partial_reduction_take_profit_trigger_failed",
            order_id=order_id,
            evidence=evidence,
        )
    if history_pos_id and history_pos_id != str(pos_id or "").strip():
        return _refuse(
            "partial_reduction_trigger_history_position_conflict",
            order_id=order_id,
            evidence=evidence,
        )

    return PartialTakeProfitExplanation(
        explained=True,
        reason_code=PARTIAL_TAKE_PROFIT_FILLED,
        order_id=order_id,
        filled_size=_text(reduction),
        remaining_size=_text(live),
        evidence=evidence,
    )


def _refuse(
    reason_code: str,
    *,
    order_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> PartialTakeProfitExplanation:
    return PartialTakeProfitExplanation(
        explained=False,
        reason_code=reason_code,
        order_id=order_id,
        evidence=dict(evidence or {}),
    )


def _row_order_id(row: Mapping[str, Any]) -> str:
    return _row_text(row, "ordId", "orderId", "order_id", "id")


def _row_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized == "-0" else normalized
