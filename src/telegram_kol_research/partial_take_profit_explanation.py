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
3. the exchange says that order actually executed.

**A-5c widened criterion 3, and only criterion 3.** It was originally "the
order appears in ``trigger-orders-history`` with a non-zero ``triggerTime``",
and A-5b found that no TPSL order created after 2026-09-08T01:23Z ever enters
that endpoint at all: the newest row it will return is ``1001125172997119``
(BTC) / ``1001125172457033`` (ETH), and asking for anything newer with
``before=`` returns zero rows, so it is not a paging artefact. Convergence
222's TP1 *is* in there (``triggerTime=1788510883``), which is why A-5 looked
correct when it shipped -- and why A-5's observation window recorded "no
sample" rather than a failure. The consequence was that criterion 3 could no
longer be satisfied for anything recent, so the explanation never fired.

Criterion 3 is therefore satisfied by either:

* **(i) trigger history** -- the order id appears in ``trigger-orders-history``
  with a non-zero ``triggerTime`` and no error code; or
* **(ii) order history** -- a *filled* order exists that closes this exact
  stage: opposite side to the position, size exactly the stage's planned size,
  average fill price within two contract ticks of the stage's trigger price,
  created after the stage was placed and no later than the moment the reduction
  was observed, and not already used to explain another stage.

Deliberately absent: any inference from symbol, side alone, price proximity
alone, time proximity alone, or "nothing else was running". Two stages of the
same size stay ambiguous unless exactly one of them is corroborated -- form
(ii)'s price match is what can break that tie, because two stages of equal size
still have different trigger prices. A reduction this module cannot name is a
reduction a person has to look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


PARTIAL_TAKE_PROFIT_FILLED = "partial_take_profit_filled"

# ``triggerTime`` values that mean "never triggered". Deepcoin sends the field
# on every pending trigger order, so its presence proves nothing on its own.
_UNTRIGGERED_TIMES = frozenset({"0", "0.0", "0.00", ""})

_CLEAN_ERROR_CODES = frozenset({"", "0", "00000"})

#: A take-profit plan is at most five stages (``take_profit_plan``), so the
#: subset search is bounded at 2**5. The guard is here so a corrupted ladder
#: cannot turn this into an exponential walk.
_MAX_STAGES = 8

#: Price tolerance for form (ii). Two ticks alone is too tight on a four-digit
#: instrument: ETH's tick is 0.01, so it allows 0.008% of slippage, and
#: convergence 237's TP1 filled 0.03 away from its 2470 trigger -- three ticks,
#: but only 1.2 basis points. A trigger price is the condition; the fill is a
#: market order that follows it, and market orders slip. Two basis points is
#: the floor, two ticks the floor for instruments whose tick is coarse.
#: Slippage is accepted in both directions: the exact size match and the time
#: window already pin the fill down, and a favourable fill is no less this
#: stage's fill than an unfavourable one.
_PRICE_TOLERANCE_BPS = Decimal("0.0002")


@dataclass(frozen=True, slots=True)
class PartialTakeProfitExplanation:
    """The verdict plus every field it was read from, never just the verdict."""

    explained: bool
    reason_code: str
    order_id: str | None = None
    filled_size: str | None = None
    remaining_size: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    #: ``(order_id, evidence)`` for every stage this reduction explains. One
    #: reduction can cover several stages when they filled between two
    #: observations -- convergence 237's TP1 and TP2 both filled before A-5
    #: existed, so the first look saw one 1.6-lot drop, not two.
    explained_orders: tuple[tuple[str, dict[str, Any]], ...] = ()


def explain_partial_position_reduction(
    *,
    execution_binding_id: int,
    pos_id: str,
    planned_size: Decimal,
    live_size: Decimal,
    take_profit_orders: Iterable[Any],
    trigger_history: Iterable[Mapping[str, Any]],
    order_history: Iterable[Mapping[str, Any]] = (),
    position_side: str | None = None,
    price_tick: Any = None,
    reduction_observed_at_ms: int | None = None,
    used_close_order_ids: Iterable[str] = (),
) -> PartialTakeProfitExplanation:
    """Return whether one owned take-profit order explains the whole reduction.

    ``order_history`` / ``position_side`` / ``price_tick`` /
    ``reduction_observed_at_ms`` are what form (ii) of criterion 3 needs. Leave
    them out and the judgement falls back to form (i) alone, which is exactly
    A-5's behaviour -- so an old caller keeps its old semantics rather than
    silently losing the check.

    ``used_close_order_ids`` names closing orders already spent explaining an
    earlier stage; one fill can only ever explain one stage.
    """

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

    # Criterion 2: only stages this binding owns for this position are even
    # looked at. Criterion 1 is no longer "one stage equals the reduction" but
    # "some set of stages sums to it exactly" -- see the module docstring.
    stages = [
        row
        for row in rows
        if int(getattr(row, "execution_binding_id", -1) or -1)
        == int(execution_binding_id)
        and str(getattr(row, "pos_id", "") or "").strip() == str(pos_id or "").strip()
        and str(getattr(row, "order_id", "") or "").strip()
        and (_decimal(getattr(row, "size_text", None)) or Decimal("0")) > 0
    ]
    if not stages:
        return _refuse(
            "partial_reduction_size_matches_no_owned_take_profit",
            evidence={**base_evidence, "owned_take_profit_sizes": []},
        )
    if len(stages) > _MAX_STAGES:
        return _refuse(
            "partial_reduction_too_many_stages",
            evidence={**base_evidence, "stage_count": len(stages)},
        )

    trigger_rows = [row for row in trigger_history if isinstance(row, Mapping)]
    close_rows = [row for row in order_history if isinstance(row, Mapping)]
    spent = {str(item).strip() for item in used_close_order_ids if str(item).strip()}
    size_counts: dict[str, int] = {}
    for stage in stages:
        key = _text(_decimal(getattr(stage, "size_text", None))) or ""
        size_counts[key] = size_counts.get(key, 0) + 1

    proven: list[tuple[Any, dict[str, Any]]] = []
    refusals: dict[str, tuple[str, dict[str, Any]]] = {}
    for stage in stages:
        order_id = str(stage.order_id).strip()
        stage_size = _decimal(getattr(stage, "size_text", None))
        evidence = {
            **base_evidence,
            "order_id": order_id,
            "order_size_text": str(stage.size_text),
            "order_status": str(getattr(stage, "status", "") or ""),
            "order_trigger_price": str(getattr(stage, "trigger_price", "") or ""),
        }
        verdict = _criterion_three(
            candidate=stage,
            order_id=order_id,
            pos_id=str(pos_id or "").strip(),
            evidence=evidence,
            trigger_rows=trigger_rows,
            close_rows=close_rows,
            position_side=position_side,
            price_tick=price_tick,
            reduction_observed_at_ms=reduction_observed_at_ms,
            spent_close_order_ids=spent,
        )
        if not isinstance(verdict, dict):
            refusals[order_id] = verdict
            continue
        if (
            verdict.get("evidence_form") == "trigger_orders_history"
            and size_counts.get(_text(stage_size) or "", 0) > 1
        ):
            # Another stage of this binding has the same size. Trigger history
            # is incomplete for recent orders, so "the sibling is not in it"
            # says nothing about whether the sibling fired -- only form (ii),
            # which tests the fill price, can tell equal-sized stages apart.
            refusals[order_id] = (
                "partial_reduction_take_profit_ambiguous",
                {**verdict, "ambiguous_with_equal_sized_stage": True},
            )
            continue
        proven.append((stage, verdict))

    combinations = _combinations_summing_to(proven, reduction)
    if len(combinations) > 1:
        return _refuse(
            "partial_reduction_take_profit_ambiguous",
            evidence={
                **base_evidence,
                "candidate_order_ids": sorted(
                    str(stage.order_id).strip() for stage, _ in proven
                ),
                "matching_combinations": [
                    sorted(str(stage.order_id).strip() for stage, _ in combo)
                    for combo in combinations
                ],
            },
        )
    if not combinations:
        exact = [
            stage
            for stage in stages
            if _decimal(getattr(stage, "size_text", None)) == reduction
        ]
        if len(exact) == 1:
            order_id = str(exact[0].order_id).strip()
            refusal = refusals.get(order_id)
            if refusal is not None:
                return _refuse(refusal[0], order_id=order_id, evidence=refusal[1])
        if exact:
            return _refuse(
                "partial_reduction_take_profit_ambiguous",
                evidence={
                    **base_evidence,
                    "candidate_order_ids": sorted(
                        str(stage.order_id).strip() for stage in exact
                    ),
                    "per_candidate_reason_codes": sorted(
                        {
                            refusals[str(stage.order_id).strip()][0]
                            for stage in exact
                            if str(stage.order_id).strip() in refusals
                        }
                    ),
                },
            )
        return _refuse(
            "partial_reduction_size_matches_no_owned_take_profit",
            evidence={
                **base_evidence,
                "owned_take_profit_sizes": [
                    _text(_decimal(getattr(stage, "size_text", None)))
                    for stage in stages
                ],
                "per_stage_reason_codes": sorted(
                    {reason for reason, _ in refusals.values()}
                ),
            },
        )

    combo = combinations[0]
    explained_orders = tuple(
        (str(stage.order_id).strip(), evidence) for stage, evidence in combo
    )
    summary = dict(explained_orders[0][1]) if len(explained_orders) == 1 else {
        **base_evidence,
        "evidence_form": "multi_stage",
        "stages": [
            {
                "order_id": order_id,
                "evidence_form": evidence.get("evidence_form"),
                "order_size_text": evidence.get("order_size_text"),
                "order_trigger_price": evidence.get("order_trigger_price"),
                "close_order": evidence.get("close_order"),
                "trigger_history": evidence.get("trigger_history"),
            }
            for order_id, evidence in explained_orders
        ],
    }
    return PartialTakeProfitExplanation(
        explained=True,
        reason_code=PARTIAL_TAKE_PROFIT_FILLED,
        order_id=explained_orders[0][0] if len(explained_orders) == 1 else None,
        filled_size=_text(reduction),
        remaining_size=_text(live),
        evidence=summary,
        explained_orders=explained_orders,
    )


def _combinations_summing_to(
    proven: list[tuple[Any, dict[str, Any]]],
    reduction: Decimal,
) -> list[tuple[tuple[Any, dict[str, Any]], ...]]:
    """Every set of proven stages that sums to the reduction exactly.

    More than one such set means the reduction cannot be attributed, which is a
    freeze -- picking the "obvious" one would be exactly the guessing this
    module exists to avoid. A closing order may appear in at most one stage of
    a set: one fill closes one stage.
    """

    found: list[tuple[tuple[Any, dict[str, Any]], ...]] = []
    total = len(proven)
    for mask in range(1, 1 << total):
        chosen = [proven[index] for index in range(total) if mask & (1 << index)]
        if sum(
            (_decimal(getattr(stage, "size_text", None)) or Decimal("0"))
            for stage, _ in chosen
        ) != reduction:
            continue
        close_ids = [
            str((evidence.get("close_order") or {}).get("ordId") or "")
            for _, evidence in chosen
        ]
        used = [item for item in close_ids if item]
        if len(used) != len(set(used)):
            continue
        found.append(tuple(chosen))
        if len(found) > 1:
            return found
    return found


def _criterion_three(
    *,
    candidate: Any,
    order_id: str,
    pos_id: str,
    evidence: dict[str, Any],
    trigger_rows: list[Mapping[str, Any]],
    close_rows: list[Mapping[str, Any]],
    position_side: str | None,
    price_tick: Any,
    reduction_observed_at_ms: int | None,
    spent_close_order_ids: set[str],
) -> dict[str, Any] | tuple[str, dict[str, Any]]:
    """Either form of "the exchange says this stage executed", or why not."""

    history_verdict = _trigger_history_evidence(
        order_id=order_id, pos_id=pos_id, evidence=evidence, trigger_rows=trigger_rows
    )
    if isinstance(history_verdict, dict):
        return history_verdict
    close_verdict = _close_order_evidence(
        candidate=candidate,
        order_id=order_id,
        evidence=evidence,
        close_rows=close_rows,
        position_side=position_side,
        price_tick=price_tick,
        reduction_observed_at_ms=reduction_observed_at_ms,
        spent_close_order_ids=spent_close_order_ids,
    )
    if isinstance(close_verdict, dict):
        return close_verdict
    # Form (i) is the more direct proof, so its refusal is the one reported
    # unless it never had a row to judge at all.
    if history_verdict[0] != "partial_reduction_trigger_history_missing":
        return history_verdict
    return close_verdict


def _trigger_history_evidence(
    *,
    order_id: str,
    pos_id: str,
    evidence: dict[str, Any],
    trigger_rows: list[Mapping[str, Any]],
) -> dict[str, Any] | tuple[str, dict[str, Any]]:
    rows = [row for row in trigger_rows if _row_order_id(row) == order_id]
    if not rows:
        return ("partial_reduction_trigger_history_missing", dict(evidence))
    if len(rows) > 1:
        return (
            "partial_reduction_trigger_history_ambiguous",
            {**evidence, "history_row_count": len(rows)},
        )
    row = rows[0]
    trigger_time = _row_text(row, "triggerTime", "trigger_time")
    error_code = _row_text(row, "errorCode", "error_code", "sCode")
    history_pos_id = _row_text(row, "closePosId", "posId", "pos_id", "positionId")
    detail = {
        **evidence,
        "evidence_form": "trigger_orders_history",
        "trigger_history": {
            "triggerTime": trigger_time,
            "errorCode": error_code,
            "posId": history_pos_id,
            "state": _row_text(row, "state", "status", "ordState"),
        },
    }
    if trigger_time in _UNTRIGGERED_TIMES:
        return ("partial_reduction_take_profit_not_triggered", detail)
    if error_code not in _CLEAN_ERROR_CODES:
        return ("partial_reduction_take_profit_trigger_failed", detail)
    if history_pos_id and history_pos_id != pos_id:
        return ("partial_reduction_trigger_history_position_conflict", detail)
    return detail


def _close_order_evidence(
    *,
    candidate: Any,
    order_id: str,
    evidence: dict[str, Any],
    close_rows: list[Mapping[str, Any]],
    position_side: str | None,
    price_tick: Any,
    reduction_observed_at_ms: int | None,
    spent_close_order_ids: set[str],
) -> dict[str, Any] | tuple[str, dict[str, Any]]:
    """Form (ii): a filled close that matches this stage on every field."""

    side = str(position_side or "").strip().lower()
    tick = _decimal(price_tick)
    trigger_price = _decimal(getattr(candidate, "trigger_price", None))
    stage_size = _decimal(getattr(candidate, "size_text", None))
    created_ms = _epoch_ms(getattr(candidate, "created_at", None))
    if (
        side not in {"long", "short"}
        or tick is None
        or tick <= 0
        or trigger_price is None
        or stage_size is None
        or created_ms is None
        or reduction_observed_at_ms is None
    ):
        # Without every input the price and time tests are not decidable, and a
        # partly checked match is not a match.
        return (
            "partial_reduction_close_evidence_inputs_missing",
            {**evidence, "evidence_form": "orders_history"},
        )
    closing_side = "sell" if side == "long" else "buy"
    tolerance = max(tick * 2, abs(trigger_price) * _PRICE_TOLERANCE_BPS)
    matches: list[dict[str, Any]] = []
    for row in close_rows:
        row_id = _row_order_id(row)
        if not row_id or row_id in spent_close_order_ids:
            continue
        if _row_text(row, "posSide", "pos_side").lower() != side:
            continue
        if _row_text(row, "side").lower() != closing_side:
            continue
        state = _row_text(row, "state", "status", "ordState").lower()
        if state not in {"filled", "success", "executed"}:
            continue
        filled = _decimal(_row_first(row, "fillSz", "accFillSz", "sz", "size"))
        if filled is None or filled != stage_size:
            continue
        avg_price = _decimal(_row_first(row, "avgPx", "fillPx", "px"))
        if avg_price is None or abs(avg_price - trigger_price) > tolerance:
            continue
        created = _row_first(row, "cTime", "uTime", "fillTime")
        created_at_ms = _epoch_ms(created)
        if created_at_ms is None:
            continue
        if created_at_ms < created_ms or created_at_ms > reduction_observed_at_ms:
            continue
        reduce_only = _row_first(row, "reduceOnly", "reduce_only", "closePosition")
        if reduce_only is not None and str(reduce_only).strip().lower() in {
            "false",
            "0",
            "no",
        }:
            continue
        matches.append(
            {
                "ordId": row_id,
                "posSide": _row_text(row, "posSide", "pos_side"),
                "side": _row_text(row, "side"),
                "fill_size": _text(filled),
                "avg_price": _text(avg_price),
                "trigger_price": _text(trigger_price),
                "price_delta": _text(abs(avg_price - trigger_price)),
                "price_tick": _text(tick),
                "price_tolerance": _text(tolerance),
                "cTime": str(created),
                "state": state,
            }
        )
    detail = {
        **evidence,
        "evidence_form": "orders_history",
        "close_order_candidates": matches,
    }
    if not matches:
        return ("partial_reduction_close_order_missing", detail)
    if len(matches) > 1:
        return ("partial_reduction_close_order_ambiguous", detail)
    detail["close_order"] = matches[0]
    detail.pop("close_order_candidates", None)
    return detail


def _row_first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _epoch_ms(value: Any) -> int | None:
    """Milliseconds since the epoch from a Deepcoin timestamp or a datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(moment.timestamp() * 1000)
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(Decimal(text))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Deepcoin sends milliseconds on these endpoints; a ten-digit value is
    # seconds and would otherwise land in 1970.
    return number * 1000 if number < 100_000_000_000 else number


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
