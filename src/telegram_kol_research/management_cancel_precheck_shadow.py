"""Watch what the management path is about to cancel, and change nothing.

Phase 6g, shadow half. Two places in ``strategy_management_executor`` cancel
protection by order id and neither looks at the order again first:

* ``_cancel_old_protection_after_replacement`` -- the ordinary replacement,
  which places the new stop before cancelling the old (the A-5e order); it
  reads back the **new** order, never the old one it is about to remove;
* ``_cancel_exact_risk_reduction_protection_before_close`` -- the partial-close
  flow, which strips the whole TPSL set first because the old orders are sized
  to the whole position and would over-close afterwards.

Between establishing an order and cancelling it the venue can have replaced,
filled or resized it, and cancelling on the older read is how a live stop gets
removed by accident. This module asks whether that happened **and does nothing
with the answer except write it down**.

**Two things it deliberately records that the existing tables cannot say.**

*The verdict.* Whether the order is still what the ledger says it is -- same
instrument, ``posSide``, trigger price and size -- with the disagreeing
attribute named rather than a bare "changed".

It does **not** reuse ``protection_authority.evaluate_cancel_precheck``, and
the reason is worth stating because reusing it was the first attempt. That
function compares an order against the ``ProtectionAuthority`` it is given,
and building that authority here means resolving it from the pending list this
module has just read -- so both sides of the comparison come from one read and
it can only ever answer "they match". Phase 6b built exactly that counter
once, and the tests below caught this one: a resized order still read as
unchanged. The comparison here is genuinely two-sourced -- the ledger row,
written when the order was established, against the venue, read now.

*A real clock.* Every intent a management batch writes carries the batch's
single ``executed_at`` in ``created_at``, ``reserved_at`` and ``submitted_at``,
because the call sites pass ``now_provider=lambda: executed_at``. So the
database cannot say how long the partial-close flow leaves a position without
a stop -- measured across every batch the interval reads as 0.0 seconds, which
is an artifact and not a measurement. Each observation here stamps the wall
clock at the moment it runs, so the interval becomes answerable after the
fact. Until then it is unknown, which is not the same as zero.

**It must never affect the path it watches.** Every failure is swallowed; a
shadow that breaks a cancel would be worse than the defect it is measuring.
It issues no exchange write and creates no mutation intent -- its only reads
are the ones needed to form the verdict.

**And it has to announce itself.** A management replacement happens about once
every four days (five of seventeen batches over twenty-one days, measured
2026-09-11), so nothing about a thirty-minute window will ever contain one,
and an observable nobody is prompted to look at is one nobody looks at. The
first observation, and every mismatching one after it, raises an incident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any, Mapping

from telegram_kol_research.deepcoin_trigger_rows import (
    stop_trigger_price,
    take_profit_trigger_price,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import ExecutionEvent, PositionProtectionLedger

logger = logging.getLogger(__name__)

SHADOW_EVENT_ACTION = "management_cancel_precheck_shadow"
INCIDENT_TYPE = "management_cancel_precheck_observed"

#: Which of the two cancel paths an observation came from. The idempotency key
#: already distinguishes them in production (``:cancel:`` versus
#: ``:precancel:``); this repeats it on the observation so a reader does not
#: have to parse keys to group the evidence.
PATH_AFTER_REPLACEMENT = "after_replacement"
PATH_RISK_REDUCTION_PRECANCEL = "risk_reduction_precancel"

#: The verdict when the order is still exactly what the ledger says it is.
VERDICT_UNCHANGED = "unchanged"
#: The ledger has no row for this order id -- the path is about to cancel
#: something this system never recorded owning.
VERDICT_NOT_IN_LEDGER = "not_in_ledger"
#: The exchange no longer lists it. "Already gone" is the caller's to
#: interpret; it is not "still the same order".
VERDICT_ABSENT_FROM_EXCHANGE = "absent_from_exchange"
#: The pending list could not be read. Unknown is never "unchanged"
#: (hard rule 4).
VERDICT_PENDING_UNREADABLE = "pending_unreadable"
#: One of the four attributes disagrees; the suffix names which.
VERDICT_CHANGED_PREFIX = "changed"


@dataclass(frozen=True, slots=True)
class CancelPrecheckObservation:
    pos_id: str
    order_id: str
    path: str
    verdict: str
    observed_at: datetime
    recorded: bool = False


def observe_cancel_precheck(
    session_factory,
    *,
    deepcoin_client: Any,
    pos_id: str,
    instrument_id: str,
    side: str,
    order_id: str,
    path: str,
    batch_id: int | None = None,
    leg_id: int | None = None,
    venue: str = "deepcoin",
    now: datetime | None = None,
) -> CancelPrecheckObservation | None:
    """Record whether this order is still what was resolved. Never blocks.

    ``now`` exists for tests; production leaves it unset so the observation
    carries the real clock rather than a batch-wide ``executed_at``.
    """

    observed_at = now or datetime.now(UTC)
    try:
        # Two sources, on purpose. The ledger row was written when this order
        # was established; the pending list is read now. Comparing a fresh read
        # against an authority built from that same fresh read can only ever
        # answer "they match" -- the tautological counter phase 6b already
        # produced once and had to be rebuilt.
        pending_rows = _read_pending(deepcoin_client, instrument_id)
        with session_factory() as session:
            row = (
                session.query(PositionProtectionLedger)
                .filter(PositionProtectionLedger.venue == venue)
                .filter(PositionProtectionLedger.pos_id == str(pos_id))
                .filter(PositionProtectionLedger.order_id == str(order_id))
                .order_by(PositionProtectionLedger.id.desc())
                .first()
            )
            ledger = (
                None
                if row is None
                else {
                    "purpose": str(row.purpose or ""),
                    "trigger_price": str(row.trigger_price or ""),
                    "size_text": str(row.size_text or ""),
                    "instrument_id": str(row.instrument_id or ""),
                    "side": str(row.side or "").lower(),
                }
            )
        verdict = _verdict(
            ledger=ledger, pending_rows=pending_rows, order_id=str(order_id)
        )
        observation = CancelPrecheckObservation(
            pos_id=str(pos_id),
            order_id=str(order_id),
            path=str(path),
            verdict=verdict,
            observed_at=observed_at,
        )
    except Exception:
        logger.warning(
            "management cancel precheck shadow could not form a verdict "
            "pos_id=%s order_id=%s",
            pos_id,
            order_id,
            exc_info=True,
        )
        return None

    try:
        first = _is_first_observation(session_factory)
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=SHADOW_EVENT_ACTION,
                venue=venue,
                status="observed",
                pos_id=str(pos_id),
                symbol=instrument_id,
                side=str(side or "").lower(),
                order_id=str(order_id),
                reason=observation.verdict,
                after={
                    "verdict": observation.verdict,
                    "path": observation.path,
                    # The wall clock, named so it cannot be confused with the
                    # batch-wide executed_at that every intent column carries.
                    "observed_at_wall": observed_at.isoformat(),
                    "batch_id": batch_id,
                    "leg_id": leg_id,
                    "ledger_known": ledger is not None,
                    "shadow_only": True,
                },
                created_at=observed_at,
            ),
        )
    except Exception:
        logger.warning(
            "management cancel precheck shadow could not record pos_id=%s order_id=%s",
            pos_id,
            order_id,
            exc_info=True,
        )
        return observation

    # The first observation ever, and every mismatch, tells a person. An
    # observable that surfaces once every few days has no natural moment at
    # which anyone would go and look for it.
    if first or observation.verdict != VERDICT_UNCHANGED:
        _capture_incident(session_factory, observation=observation, first=first)
    return CancelPrecheckObservation(
        pos_id=observation.pos_id,
        order_id=observation.order_id,
        path=observation.path,
        verdict=observation.verdict,
        observed_at=observation.observed_at,
        recorded=True,
    )


def _verdict(*, ledger, pending_rows, order_id: str) -> str:
    """Compare what the ledger says this order is against what the venue lists.

    Four attributes, and the one that disagrees is named -- "changed" alone
    would say a cancel was about to remove something unexpected without saying
    what, which is the difference between a record a person can act on and one
    they have to re-derive.
    """

    if ledger is None:
        return VERDICT_NOT_IN_LEDGER
    if pending_rows is None:
        return VERDICT_PENDING_UNREADABLE
    row = None
    for candidate in pending_rows:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("ordId") or candidate.get("orderId") or "") == order_id:
            row = candidate
            break
    if row is None:
        return VERDICT_ABSENT_FROM_EXCHANGE

    venue_price = (
        take_profit_trigger_price(row)
        if ledger["purpose"] == "take_profit"
        else stop_trigger_price(row)
    )
    checks = (
        ("instrument", str(row.get("instId") or "").upper(),
         ledger["instrument_id"].upper()),
        ("side", str(row.get("posSide") or "").lower(), ledger["side"]),
    )
    for name, seen, expected in checks:
        if expected and seen != expected:
            return f"{VERDICT_CHANGED_PREFIX}_{name}"
    if not _decimal_equal(venue_price, ledger["trigger_price"]):
        return f"{VERDICT_CHANGED_PREFIX}_trigger_price"
    if ledger["size_text"] and not _decimal_equal(
        row.get("sz"), ledger["size_text"]
    ):
        return f"{VERDICT_CHANGED_PREFIX}_size"
    return VERDICT_UNCHANGED


def _decimal_equal(left: Any, right: Any) -> bool:
    """Prices are numbers; comparing them as text rejected 27 positions (6f-1)."""

    if left is None or right in (None, ""):
        return False
    try:
        a = Decimal(str(left))
        b = Decimal(str(right))
    except (DecimalException, ValueError):
        return False
    if not (a.is_finite() and b.is_finite()):
        return False
    return a == b


def _is_first_observation(session_factory) -> bool:
    with session_factory() as session:
        return (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .first()
            is None
        )


def _read_pending(deepcoin_client: Any, instrument_id: str):
    """``None`` on a failed read -- unknown is never permission (hard rule 4)."""

    lister = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if not callable(lister):
        return None
    try:
        return list(lister(inst_id=instrument_id))
    except Exception:
        logger.warning(
            "management cancel precheck shadow could not read pending inst_id=%s",
            instrument_id,
            exc_info=True,
        )
        return None


def _capture_incident(
    session_factory, *, observation: CancelPrecheckObservation, first: bool
) -> None:
    try:
        from telegram_kol_research.config import load_runtime_incident_config
        from telegram_kol_research.runtime_incident_adapters import (
            capture_management_cancel_precheck_observed,
        )

        capture_management_cancel_precheck_observed(
            session_factory,
            config=load_runtime_incident_config(),
            pos_id=observation.pos_id,
            order_id=observation.order_id,
            path=observation.path,
            verdict=observation.verdict,
            first_ever=first,
            occurred_at=observation.observed_at,
        )
    except Exception:  # pragma: no cover - an alert must never break the caller
        logger.warning(
            "management cancel precheck shadow incident capture failed pos_id=%s",
            observation.pos_id,
            exc_info=True,
        )


__all__ = [
    "CancelPrecheckObservation",
    "INCIDENT_TYPE",
    "PATH_AFTER_REPLACEMENT",
    "PATH_RISK_REDUCTION_PRECANCEL",
    "SHADOW_EVENT_ACTION",
    "VERDICT_ABSENT_FROM_EXCHANGE",
    "VERDICT_NOT_IN_LEDGER",
    "VERDICT_PENDING_UNREADABLE",
    "VERDICT_UNCHANGED",
    "observe_cancel_precheck",
]
