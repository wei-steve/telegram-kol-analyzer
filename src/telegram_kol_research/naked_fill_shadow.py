"""What the naked-fill net would catch if it could fire, and it cannot.

Phase 6 follow-up, shadow half. ``naked_fill_stop_net`` is the safety net
approved on 2026-09-09 for the case "a market entry filled, its attribution
never resolved, so the position is open with no stop". Two independent facts,
both measured on 2026-09-11, say it has never been able to fire:

* its precondition requires ``attribution_status == "unverified"``, and that
  value **has never been written** -- zero occurrences across every attribution
  transition in ``position_attribution_audits``. The only writer sets it when
  ``pos_id`` is truthy, while both producers of that status return
  ``pos_id=None``, so the two halves are mutually exclusive by construction;
* its precondition also requires ``order_kind == "market"``, and the condition
  "submitted, sixty seconds on, still no ``pos_id``, not verified" has occurred
  nineteen times in production -- **every one a limit entry, never a market
  one**.

Either alone keeps it silent. So this module records what a widened net would
decide, and **writes nothing**: no exchange call, no ledger row, no mutation
intent. Its purpose is to find out whether the situation the net was built for
happens at all before anything is changed to make the net fire.

**Fill evidence is required, and that is not a detail.** The obvious trigger --
"submitted, still no position id" -- is satisfied by every resting limit order
that was later cancelled, and all nineteen historical matches are in fact
terminal (``cancelled`` 11, ``manually_cancelled`` 1, ``manually_closed`` 7).
An order that never filled has no position and cannot be naked. A shadow that
counted those would produce samples that look like the thing being studied and
are not, and the release condition downstream -- "at least one sample whose
verdict matches the design" -- would then be satisfiable by a false one. So a
candidate must carry evidence that it filled, and the record says where that
evidence came from. That evidence is the venue's order history and nothing
else: a WS frame was the obvious second source and is deliberately not used,
because phase 1 keeps ``deepcoin_ws_events`` off every decision path and phase
6e had already settled that a push is not evidence on its own.

The decision itself is **not** reimplemented here: it calls
``evaluate_naked_fill`` with a widened precondition (a), so there is one
implementation of "is there exactly one unclaimed position of the right size"
rather than two that drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.naked_fill_stop_net import evaluate_naked_fill
from telegram_kol_research.open_order_action_guard import REGULAR_ORDER_LEG_KINDS

logger = logging.getLogger(__name__)

SHADOW_EVENT_ACTION = "naked_fill_would_place"

#: Statuses that mean the leg is no longer live. A terminal leg cannot be a
#: naked position no matter what its other fields say.
TERMINAL_LEG_STATUSES = frozenset(
    {
        "cancelled",
        "exchange_cancelled",
        "manually_cancelled",
        "closed",
        "manually_closed",
        "expired",
        "failed_terminal",
    }
)

#: The same grace the live net gives the identity equation to resolve itself.
GRACE = timedelta(seconds=60)

FILL_EVIDENCE_ORDER_HISTORY = "orders_history"
FILL_EVIDENCE_NONE = "none"


@dataclass(frozen=True, slots=True)
class NakedFillShadowRow:
    leg_id: int
    order_id: str
    order_kind: str
    attribution_status: str
    fill_evidence: str
    fill_evidence_detail: str | None = None
    action: str | None = None
    reason: str | None = None
    pos_id: str | None = None
    stop_loss: str | None = None
    preconditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NakedFillShadowResult:
    legs_examined: int = 0
    candidates_with_fill_evidence: int = 0
    skipped_no_fill_evidence: int = 0
    rows: tuple[NakedFillShadowRow, ...] = ()
    read_failures: tuple[str, ...] = ()
    counts_by_reason: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "legs_examined": self.legs_examined,
            "candidates_with_fill_evidence": self.candidates_with_fill_evidence,
            "skipped_no_fill_evidence": self.skipped_no_fill_evidence,
            "counts_by_reason": dict(self.counts_by_reason),
            "read_failures": list(self.read_failures),
            "rows": [
                {
                    "leg_id": row.leg_id,
                    "order_id": row.order_id,
                    "order_kind": row.order_kind,
                    "attribution_status": row.attribution_status,
                    "fill_evidence": row.fill_evidence,
                    "fill_evidence_detail": row.fill_evidence_detail,
                    "action": row.action,
                    "reason": row.reason,
                    "pos_id": row.pos_id,
                    "stop_loss": row.stop_loss,
                    "preconditions": list(row.preconditions),
                }
                for row in self.rows
            ],
        }


def run_naked_fill_shadow_pass(
    session_factory,
    *,
    deepcoin_client: Any,
    now: datetime,
    venue: str = "deepcoin",
) -> NakedFillShadowResult:
    """Record, for every live unattributed entry leg, what a widened net would do."""

    with session_factory() as session:
        legs = [
            {
                "id": int(leg.id),
                "order_id": str(leg.order_id or ""),
                "order_kind": str(leg.order_kind or ""),
                "status": str(leg.status or ""),
                "attribution_status": str(leg.attribution_status or ""),
                "instrument_id": _instrument_of(leg),
                "updated_at": leg.updated_at or leg.created_at,
            }
            for leg in (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.purpose == "entry")
                .filter(ExecutionOrderLeg.venue == venue)
                .filter(ExecutionOrderLeg.order_kind.in_(sorted(REGULAR_ORDER_LEG_KINDS)))
                .filter(
                    (ExecutionOrderLeg.pos_id.is_(None))
                    | (ExecutionOrderLeg.pos_id == "")
                )
                # The NULL arm is defensive, not the live case: the column is
                # NOT NULL DEFAULT 'unassigned', so a failed attribution --
                # which passes None -- lands as "unassigned" rather than NULL.
                # Written this way because SQL's `!=` drops NULL silently, and
                # if that default is ever removed the filter would otherwise
                # start excluding exactly the legs it exists to find while
                # still reporting a confident zero.
                .filter(
                    (ExecutionOrderLeg.attribution_status.is_(None))
                    | (ExecutionOrderLeg.attribution_status != "verified")
                )
                .all()
            )
            if str(leg.status or "") not in TERMINAL_LEG_STATUSES
            and str(leg.order_id or "")
        ]

    rows: list[NakedFillShadowRow] = []
    read_failures: list[str] = []
    no_evidence = 0
    positions_by_instrument: dict[str, Any] = {}
    pending_by_instrument: dict[str, Any] = {}

    for leg in legs:
        updated = leg["updated_at"]
        if updated is not None and _aware(now) - _aware(updated) < GRACE:
            continue
        evidence, detail = _fill_evidence(
            session_factory, deepcoin_client, leg=leg, venue=venue
        )
        if evidence == FILL_EVIDENCE_NONE:
            # A resting or cancelled order is not a naked position. Counted so
            # the window can show how much of the raw match is this.
            no_evidence += 1
            continue
        # The instrument comes from the leg's own submitted payload, not from
        # whichever evidence source happened to answer -- a WS frame carries no
        # instId, and deriving it from the evidence would make the read depend
        # on which of the two sources fired.
        instrument_id = str(leg["instrument_id"] or "").upper()
        if instrument_id and instrument_id not in positions_by_instrument:
            positions_by_instrument[instrument_id] = _read(
                deepcoin_client, "list_positions", instrument_id, read_failures
            )
            pending_by_instrument[instrument_id] = _read(
                deepcoin_client,
                "list_trigger_orders_pending",
                instrument_id,
                read_failures,
            )
        decision = evaluate_naked_fill(
            session_factory,
            leg_id=leg["id"],
            live_positions=positions_by_instrument.get(instrument_id),
            now=now,
            venue=venue,
            pending_orders=pending_by_instrument.get(instrument_id),
            # The widening this shadow exists to measure. The live net keeps
            # its own defaults; nothing here changes what it does.
            entry_order_kinds=frozenset(REGULAR_ORDER_LEG_KINDS),
            require_unverified_attribution=False,
        )
        rows.append(
            NakedFillShadowRow(
                leg_id=leg["id"],
                order_id=leg["order_id"],
                order_kind=leg["order_kind"],
                attribution_status=leg["attribution_status"] or "(null)",
                fill_evidence=evidence,
                fill_evidence_detail=_detail_text(detail),
                action=getattr(decision, "status", None),
                reason=getattr(decision, "reason", None),
                pos_id=getattr(decision, "pos_id", None),
                stop_loss=getattr(decision, "stop_loss", None),
                preconditions=tuple(getattr(decision, "preconditions", ()) or ()),
            )
        )

    counts: dict[str, int] = {}
    for row in rows:
        key = row.reason or row.action or "unknown"
        counts[key] = counts.get(key, 0) + 1
    result = NakedFillShadowResult(
        legs_examined=len(legs),
        candidates_with_fill_evidence=len(rows),
        skipped_no_fill_evidence=no_evidence,
        rows=tuple(rows),
        read_failures=tuple(read_failures),
        counts_by_reason=counts,
    )
    for row in rows:
        _record(session_factory, row=row, venue=venue, now=now)
    return result


def _record(session_factory, *, row: NakedFillShadowRow, venue: str, now: datetime) -> None:
    """One durable row per candidate, carrying where the fill evidence came from.

    Without that last part a reader months later cannot tell a real candidate
    from a resting order, which is the distinction this whole module turns on.
    """

    try:
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=SHADOW_EVENT_ACTION,
                venue=venue,
                status="observed",
                order_id=row.order_id,
                pos_id=row.pos_id,
                reason=row.reason,
                after={
                    "leg_id": row.leg_id,
                    "order_kind": row.order_kind,
                    "attribution_status": row.attribution_status,
                    "fill_evidence": row.fill_evidence,
                    "fill_evidence_detail": row.fill_evidence_detail,
                    "action": row.action,
                    "reason": row.reason,
                    "candidate_pos_id": row.pos_id,
                    "intended_stop_loss": row.stop_loss,
                    "preconditions": list(row.preconditions),
                    "shadow_only": True,
                },
                created_at=now,
            ),
        )
    except Exception:  # pragma: no cover - a record must never break the pass
        logger.warning(
            "naked fill shadow could not record leg_id=%s", row.leg_id, exc_info=True
        )


def _fill_evidence(session_factory, deepcoin_client, *, leg, venue: str):
    """Did this order actually fill? ``none`` means no evidence, not "no".

    **One source, and deliberately not two.** The obvious second source is a
    ``Trade``/``Order`` frame in ``deepcoin_ws_events``, and the first version
    of this module used it. A static guard refused that module
    (``test_no_production_module_reads_the_phase_one_inbox_table``), and the
    guard is right twice over: phase 1 keeps that table off every decision
    path, and this repository already decided in phase 6e that a push is not
    evidence on its own -- an adopted order had to appear in the REST pending
    list as well before the ledger would name it. Treating a frame as proof of
    a fill would have been the same mistake with the confirmation removed.

    So the only source is the venue's own order history for that exact order
    id, and the record names it, so a reader can go back to the same place.
    ``session_factory`` and ``venue`` stay in the signature: the corroborating
    read this may grow later belongs here, not at the caller.
    """

    del session_factory, venue  # see docstring

    order_id = leg["order_id"]
    history = getattr(deepcoin_client, "list_order_history", None)
    if callable(history):
        try:
            for row in history() or ():
                if not isinstance(row, Mapping):
                    continue
                if str(row.get("ordId") or row.get("orderId") or "") != order_id:
                    continue
                state = str(row.get("state") or row.get("status") or "").lower()
                filled = _positive(row.get("accFillSz") or row.get("fillSz"))
                if state in {"filled", "partially_filled"} or filled:
                    return FILL_EVIDENCE_ORDER_HISTORY, dict(row)
        except Exception:
            logger.warning(
                "naked fill shadow could not read order history order_id=%s",
                order_id,
                exc_info=True,
            )

    return FILL_EVIDENCE_NONE, None


def _instrument_of(leg) -> str:
    """The instrument the leg's own submitted payload named."""

    import json

    try:
        request = json.loads(str(leg.request_json or "{}"))
    except (TypeError, ValueError):
        return ""
    if not isinstance(request, dict):
        return ""
    return str(request.get("instId") or "").upper()


def _detail_text(detail) -> str | None:
    if not isinstance(detail, Mapping):
        return None
    keep = ("ordId", "instId", "state", "accFillSz", "channel", "received_at")
    return ", ".join(
        f"{k}={detail[k]}" for k in keep if detail.get(k) not in (None, "")
    ) or None


def _read(deepcoin_client, method: str, instrument_id: str, failures: list[str]):
    """``None`` on a failed read. Unknown is never a verdict (hard rule 4)."""

    fn = getattr(deepcoin_client, method, None)
    if not callable(fn):
        return None
    try:
        return list(fn(inst_id=instrument_id))
    except Exception:
        logger.warning(
            "naked fill shadow read failed method=%s inst_id=%s",
            method,
            instrument_id,
            exc_info=True,
        )
        failures.append(f"{method}:{instrument_id}")
        return None


def _positive(value: Any) -> bool:
    try:
        return abs(float(str(value))) > 0
    except (TypeError, ValueError):
        return False


def _aware(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "FILL_EVIDENCE_NONE",
    "FILL_EVIDENCE_ORDER_HISTORY",
    "NakedFillShadowResult",
    "NakedFillShadowRow",
    "SHADOW_EVENT_ACTION",
    "TERMINAL_LEG_STATUSES",
    "run_naked_fill_shadow_pass",
]
