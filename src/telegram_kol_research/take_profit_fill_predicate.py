"""Did this exact take-profit order actually fill?  One answer, one place.

Three modules had each written their own version of this question and all three
got the same thing wrong.  Deepcoin's ``GET /trade/trigger-orders-history``
**has no ``state`` field** -- the documented column list is
``instType instId ordId px sz triggerPx triggerPxType ordType side posSide
tdMode lever triggerTime uTime cTime errorCode errorMsg`` plus the SL/TP price
columns (``docs/2026-09-05-deepcoin-api-deterministic-link-research.md`` line
117).  Whether a trigger fired is ``triggerTime``; whether it failed is
``errorCode``.  Reading ``state`` therefore always yields ``None``, and the
three readers each turned that into a different wrong conclusion:

* ``protection_health._successful_close`` decided every filled take profit was
  ``protection_missing`` and raised a critical incident for it (production leg
  579, eight seconds after TP1 filled);
* ``strategy_management_take_profit_consumption._terminal_state`` decided the
  stage's outcome was unknown and refused the whole composite instruction;
* the test fixtures for both invented ``{"state": "filled"}`` rows, so neither
  defect could be seen from the suite.

So the predicate lives here, is pure, and takes only what a caller can actually
produce.  Two forms of proof, both fail-closed:

1. **The recorded order status.**  ``position_take_profit_orders.status`` is
   written by the reconciliation round from
   ``take_profit_fill_evidence.prove_first_take_profit_fill`` -- an exact
   terminal row, or a complete two-observation position-delta proof.  It is
   durable, so it is preferred.
2. **A clean trigger-history row**: ``triggerTime`` is not zero and
   ``errorCode`` is one of ``""``/``"0"``/``"00000"``, *and* the pending read
   that showed the order gone was complete.  This is the same reading
   ``partial_take_profit_explanation`` form (i) already uses.

3. **A position decrease under form B** (stop-ladder phase 1 spec 2.2), and
   only when the caller has already established the whole conjunction the
   account owner approved: the order is absent from a **complete** pending
   read, its ledger row is not ``retired``/``cancelled``/``superseded``, we
   hold no cancel intent for it, and the trigger history says nothing about it
   at all.  The caller answers "did the position get smaller between two
   complete observations" as a boolean -- ``position_decrease_proven`` -- and
   quantity is never compared, because a partially filled stage would
   otherwise be indistinguishable from an unexplained one.

**A smaller position is not proof on its own.**  A position shrinks for a
manual close, a liquidation, another strategy's leg or a stop, so form B is
available only to a caller that has ruled those out by the conjunction above;
passing nothing (the default ``None``) keeps every caller that predates it on
forms 1 and 2 exactly as it was.  A history row that exists always decides:
a failed trigger is a refusal and an untriggered one is a refusal, never
rescued by a size change.

The stop-ladder work (``docs/plans/2026-09-21-stop-ladder-design.md`` section
1) needs exactly this question answered -- "which take-profit stage has really
filled" -- so it imports from here rather than growing a fourth reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


#: ``position_take_profit_orders.status`` values that mean the reconciliation
#: round proved a fill.  ``expired``, ``cancelled`` and ``active`` are not
#: fills and are deliberately absent.
PROVEN_FILL_ORDER_STATUSES = frozenset({"filled"})

#: ``triggerTime`` values that mean "this order never fired".
UNTRIGGERED_TRIGGER_TIMES = frozenset({"", "0", "0.0", "0.00"})

#: ``errorCode`` values that mean "the trigger did not fail".  The venue writes
#: the empty string for success on some rows and ``"0"``/``"00000"`` on others.
CLEAN_ERROR_CODES = frozenset({"", "0", "00000"})

TIER_RECORDED_ORDER_STATUS = "recorded_take_profit_order_status"
TIER_TRIGGER_HISTORY = "trigger_history_clean_trigger"
#: Form B. The history is silent and the position got smaller between two
#: complete observations, with every exclusion already established by the
#: caller (stop-ladder phase 1 spec 2.2).
TIER_POSITION_DECREASE = "position_decrease_between_complete_observations"

#: What the two forms are called in order-level evidence.
EVIDENCE_FORM_TRIGGER_HISTORY = "trigger_history"
EVIDENCE_FORM_POSITION_DECREASE = "position_decrease"

REASON_ORDER_IDENTITY_MISSING = "take_profit_order_identity_missing"
REASON_HISTORY_ABSENT = "take_profit_trigger_history_absent"
REASON_HISTORY_AMBIGUOUS = "take_profit_trigger_history_ambiguous"
REASON_TRIGGER_FAILED = "take_profit_trigger_failed"
REASON_NOT_TRIGGERED = "take_profit_not_triggered"
REASON_SNAPSHOT_INCOMPLETE = "take_profit_pending_snapshot_incomplete"
REASON_POSITION_NOT_DECREASED = "take_profit_position_not_decreased"

#: Which evidence form each proving tier is recorded as.
EVIDENCE_FORM_BY_TIER = {
    TIER_RECORDED_ORDER_STATUS: EVIDENCE_FORM_TRIGGER_HISTORY,
    TIER_TRIGGER_HISTORY: EVIDENCE_FORM_TRIGGER_HISTORY,
    TIER_POSITION_DECREASE: EVIDENCE_FORM_POSITION_DECREASE,
}


@dataclass(frozen=True, slots=True)
class TakeProfitFillVerdict:
    """Whether the fill is proven, and -- when it is not -- exactly why not."""

    proven: bool
    evidence_tier: str | None = None
    reason_code: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


def take_profit_fill_proven(
    *,
    order_id: str | None,
    recorded_order_statuses: Iterable[str] = (),
    trigger_history: Iterable[Mapping[str, Any]] = (),
    pending_snapshot_complete: bool = False,
    position_decrease_proven: bool | None = None,
) -> TakeProfitFillVerdict:
    """Prove that ``order_id`` filled, from durable record, history or delta.

    ``recorded_order_statuses`` are the ``position_take_profit_orders.status``
    values recorded for this exact order id -- usually none or one.
    ``pending_snapshot_complete`` says whether the ``trigger-orders-pending``
    read that showed the order gone actually succeeded; an incomplete read
    means "unknown", never "gone".

    ``position_decrease_proven`` is form B and is only consulted when the
    history has nothing to say about this order: ``True`` means the caller
    compared two consecutive complete observations of this exact position and
    the size went down, having already excluded a retired/cancelled ledger row
    and any cancel intent of our own.  ``None``, the default, means the caller
    offers no such evidence and the verdict is unchanged from before form B
    existed.
    """

    identity = str(order_id or "").strip()
    if not identity:
        return TakeProfitFillVerdict(
            proven=False, reason_code=REASON_ORDER_IDENTITY_MISSING
        )

    statuses = sorted(
        {
            normalized
            for value in recorded_order_statuses
            if (normalized := str(value or "").strip().lower())
        }
    )
    proven_statuses = [
        value for value in statuses if value in PROVEN_FILL_ORDER_STATUSES
    ]
    if proven_statuses:
        return TakeProfitFillVerdict(
            proven=True,
            evidence_tier=TIER_RECORDED_ORDER_STATUS,
            evidence={"order_id": identity, "recorded_status": proven_statuses[0]},
        )

    rows = [
        row
        for row in trigger_history
        if isinstance(row, Mapping) and _row_order_id(row) == identity
    ]
    failed = [row for row in rows if trigger_row_failed(row)]
    if failed:
        return TakeProfitFillVerdict(
            proven=False,
            reason_code=REASON_TRIGGER_FAILED,
            evidence={
                "order_id": identity,
                "error_code": _text(failed[0], "errorCode", "error_code", "sCode"),
            },
        )
    if not rows:
        if position_decrease_proven is None:
            return TakeProfitFillVerdict(
                proven=False,
                reason_code=REASON_HISTORY_ABSENT,
                evidence={"order_id": identity, "recorded_statuses": statuses},
            )
        if not pending_snapshot_complete:
            # The order is missing from a read nobody can vouch for, so
            # "gone" is not established and neither is anything that follows
            # from it.
            return TakeProfitFillVerdict(
                proven=False,
                reason_code=REASON_SNAPSHOT_INCOMPLETE,
                evidence={"order_id": identity},
            )
        if not position_decrease_proven:
            return TakeProfitFillVerdict(
                proven=False,
                reason_code=REASON_POSITION_NOT_DECREASED,
                evidence={"order_id": identity},
            )
        return TakeProfitFillVerdict(
            proven=True,
            evidence_tier=TIER_POSITION_DECREASE,
            evidence={
                "order_id": identity,
                "evidence_form": EVIDENCE_FORM_POSITION_DECREASE,
            },
        )
    if len(rows) > 1:
        return TakeProfitFillVerdict(
            proven=False,
            reason_code=REASON_HISTORY_AMBIGUOUS,
            evidence={"order_id": identity, "history_row_count": len(rows)},
        )
    if not trigger_row_fired(rows[0]):
        return TakeProfitFillVerdict(
            proven=False,
            reason_code=REASON_NOT_TRIGGERED,
            evidence={"order_id": identity},
        )
    if not pending_snapshot_complete:
        # The order is absent from a read we cannot vouch for. Absent from an
        # incomplete read is unknown, and unknown is never a fill.
        return TakeProfitFillVerdict(
            proven=False,
            reason_code=REASON_SNAPSHOT_INCOMPLETE,
            evidence={"order_id": identity},
        )
    return TakeProfitFillVerdict(
        proven=True,
        evidence_tier=TIER_TRIGGER_HISTORY,
        evidence={
            "order_id": identity,
            "trigger_time": _text(rows[0], "triggerTime", "trigger_time"),
            "error_code": _text(rows[0], "errorCode", "error_code", "sCode"),
        },
    )


def trigger_row_fired(row: Mapping[str, Any]) -> bool:
    """Did this trigger-order-history row actually trigger?"""

    return _text(row, "triggerTime", "trigger_time") not in UNTRIGGERED_TRIGGER_TIMES


def trigger_row_failed(row: Mapping[str, Any]) -> bool:
    """Did this row trigger and then fail?  An untriggered row has not failed."""

    return trigger_row_fired(row) and (
        _text(row, "errorCode", "error_code", "sCode") not in CLEAN_ERROR_CODES
    )


def _row_order_id(row: Mapping[str, Any]) -> str:
    return _text(row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id")


def _text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""
