"""Phase 6i. A conditional entry the exchange no longer has anywhere.

**The gap this closes.** ``_refresh_exact_entry_leg_states`` is the only live
path that judges an entry leg against the venue. Its "gone from the exchange"
branch reads::

    if history is None:
        if str(leg.status or "").lower() in {"open", "submitted"}:
            _set_entry_leg_exchange_state(leg, status="unknown", ...)
        continue

``pending`` is not in that set -- and ``pending`` is the status *that same
function writes* when the order **is** on the venue. So the state could be
written and never un-written: once a conditional entry went to ``pending`` it
stayed there, whatever the exchange later said. The other path that ends an
entry leg, ``_apply_recorded_terminal_entry_events``, fires only on a cancel
**we** recorded, so an order that left the venue without us cancelling it had
nothing at all to collect it.

Measured on 2026-09-11: leg 582 (ordId ``1001125122023573``, submitted
2026-09-04, accepted with ``sCode 0``) was in neither ``trigger-orders-pending``
nor ``trigger-order-history`` -- the latter paged to exhaustion, twelve pages,
1176 rows, 2026-06-29 to 2026-09-11 with no gap. Its binding therefore sat at
``open`` forever, because ``_derive_binding_from_entry_legs`` archives only when
every entry leg is terminal.

**Why the proof has to be an exhausted search, not a page.** The snapshot's
``trigger_history`` is one page of a hundred rows. Absence from it means only
"not in the newest hundred". Worse, the obvious targeted read is a trap the
repository already measured: ``list_trigger_order_history_by_order_id`` returns
``[]`` for *every* order id because the exchange ignores the ``ordId`` filter
(A-5b, 2026-09-08), so a caller that trusted it would collect live orders. The
one honest reader is ``find_trigger_order_history_rows``, which pages the
unfiltered endpoint and returns ``(matches, searched_to_the_end)``. This module
acts only on ``([], True)``.

**Every other answer holds.** A page budget that ran out, a read that raised, a
missing instrument id, an order younger than a day -- each is its own named
hold, never a collect. That direction is deliberate: collecting is writing a
terminal state onto something that may still be resting on the exchange, and
the cost of holding is one more round.

**This module decides; it does not write.** The caller applies the verdict and
records the incident, so what gets written is visibly the thing that was
judged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import Any

#: How long an absence must persist before it counts. A conditional entry can
#: be missing from both lists for a moment around its own conversion -- it
#: leaves the pending list before the resulting order reaches history -- so the
#: window has to be far wider than any such gap. A day is not tuned; it is
#: simply far past the point where "still settling" is a possible explanation.
MIN_ABSENCE_AGE = timedelta(hours=24)

#: Written to ``ExecutionOrderLeg.terminal_reason``. It names the evidence, not
#: a guess about what the exchange did: this system does not know whether the
#: order was cancelled, expired, or never rested at all -- only that the venue
#: has no record of it in either place, and that the search reached the end.
TERMINAL_REASON = "absent_from_pending_and_exhausted_history"

#: Raised whatever the environment whitelist lists. A conditional entry that
#: the exchange acknowledged and then has no record of is a discrepancy between
#: our books and the venue; the sweep tidies our side, and a person still has to
#: be told that the two disagreed.
INCIDENT_TYPE = "conditional_entry_absent_from_exchange"

#: How many pages of trigger-order history the exhaustive search may read.
#:
#: **The default was the whole reason phase 6i never fired.**
#: ``find_trigger_order_history_rows`` defaults to ``max_pages=5``, which is 500
#: rows. Measured on 2026-09-11: this account's entire trigger-order history was
#: **1176 rows over 13 pages**, so the search ran out of budget every round and
#: returned ``([], False)`` -- "did not finish looking". The module did exactly
#: what it promised and held; leg 582 stayed ``pending`` and the binding stayed
#: ``open``, with the sweep deployed and running. A correct refusal, forever.
#:
#: 40 rather than 13. 13 is today's history length, and a budget pinned to the
#: measurement is a budget that expires silently the next time the account
#: trades. 40 leaves roughly three times the present history, and the hold it
#: eventually produces is now logged every round rather than silent, so the
#: next person finds out by reading rather than by going to look.
HISTORY_SEARCH_MAX_PAGES = 40

_PENDING_STATUS = "pending"


@dataclass(frozen=True)
class AbsentEntryVerdict:
    """One leg, one decision, and the reason in the reason's own words."""

    leg_id: int
    status: str  # "collect" or "hold"
    reason: str
    order_id: str = ""
    instrument_id: str = ""
    age_seconds: int | None = None
    searched_to_the_end: bool | None = None
    history_matches: int | None = None

    @property
    def collects(self) -> bool:
        return self.status == "collect"


def _hold(leg_id: int, reason: str, **extra: Any) -> AbsentEntryVerdict:
    return AbsentEntryVerdict(leg_id=leg_id, status="hold", reason=reason, **extra)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    # SQLite hands back naive datetimes; every timestamp in this repository is
    # stored in UTC, so a naive one is UTC rather than local time. Reading it as
    # local would move the 24-hour boundary by the host's offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def instrument_id_for_leg(leg: Any) -> str:
    """The instrument this order was actually sent for, or "".

    Read from the submitted request rather than rebuilt from the binding's
    symbol. The rebuilt form (``f"{symbol}-USDT-SWAP"``) is a guess that happens
    to be right today; if it is ever wrong the search would page a different
    instrument's history and find nothing, which this module would then read as
    absence. An order whose request does not say what it was for is held.
    """

    request = getattr(leg, "request_json", None)
    if isinstance(request, str) and request.strip():
        try:
            parsed = json.loads(request)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            value = str(parsed.get("instId") or "").strip()
            if value:
                return value
    request = getattr(leg, "request", None)
    if isinstance(request, dict):
        value = str(request.get("instId") or "").strip()
        if value:
            return value
    return ""


def snapshot_read_failed_for(
    errors: dict[str, str] | None, *, instrument_id: str
) -> str:
    """The snapshot error that makes this instrument's absence unreadable, or "".

    Both lists matter. If the pending read failed, "absent from pending" was
    never established; if the history page read failed, the snapshot's absence
    is equally uninformed. Either one means this round cannot conclude anything,
    which is hard rule 4 applied to a pair of reads instead of one.
    """

    if not errors:
        return ""
    watched = ("pending_trigger_orders", "trigger_history")
    for key, message in sorted(errors.items()):
        source, _, scope = key.partition(":")
        if source not in watched:
            continue
        if scope and instrument_id and scope != instrument_id:
            continue
        return f"{key}={message}"
    return ""


def evaluate_absent_conditional_entry(
    leg: Any,
    *,
    absent_from_pending: bool,
    absent_from_history_page: bool,
    client: Any,
    snapshot_errors: dict[str, str] | None,
    now: datetime,
    min_absence_age: timedelta = MIN_ABSENCE_AGE,
    history_search_max_pages: int = HISTORY_SEARCH_MAX_PAGES,
) -> AbsentEntryVerdict:
    """Decide whether this pending conditional entry may be collected.

    Returns a ``hold`` for every uncertainty, including the ones that look like
    bookkeeping (no instrument id, no order id). The only ``collect`` is the one
    backed by a search that reached the end of the history and found nothing.
    """

    leg_id = int(getattr(leg, "id", 0) or 0)

    if str(getattr(leg, "purpose", "") or "") != "entry":
        return _hold(leg_id, "not_an_entry_leg")
    if not str(getattr(leg, "order_kind", "") or "").startswith("trigger"):
        return _hold(leg_id, "not_a_conditional_entry")
    if str(getattr(leg, "status", "") or "").lower() != _PENDING_STATUS:
        return _hold(leg_id, "not_pending")
    if not absent_from_pending:
        return _hold(leg_id, "still_on_the_pending_list")
    if not absent_from_history_page:
        return _hold(leg_id, "present_in_history_page")

    order_id = str(getattr(leg, "order_id", "") or "").strip()
    if not order_id:
        return _hold(leg_id, "no_order_id")

    instrument_id = instrument_id_for_leg(leg)
    if not instrument_id:
        return _hold(leg_id, "no_instrument_id", order_id=order_id)

    submitted_at = _as_utc(getattr(leg, "created_at", None))
    if submitted_at is None:
        return _hold(
            leg_id, "no_submitted_at", order_id=order_id, instrument_id=instrument_id
        )
    # Both sides through the same normaliser. ``recovered_at`` reaches here
    # naive from some callers and aware from others; subtracting one from the
    # other raises, and "compare whichever two datetimes turned up" is how a
    # 24-hour boundary silently becomes a 16-hour one on a UTC+8 host.
    reference = _as_utc(now)
    if reference is None:
        return _hold(
            leg_id, "no_reference_time", order_id=order_id, instrument_id=instrument_id
        )
    age = reference - submitted_at
    age_seconds = int(age.total_seconds())
    if age < min_absence_age:
        return _hold(
            leg_id,
            "too_recent",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
        )

    read_failure = snapshot_read_failed_for(
        snapshot_errors, instrument_id=instrument_id
    )
    if read_failure:
        return _hold(
            leg_id,
            "snapshot_read_error",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
        )

    finder = getattr(client, "find_trigger_order_history_rows", None)
    if finder is None:
        # Without the paging reader there is no way to tell absence from
        # "fell off page one". The single-page snapshot is not a substitute
        # and the ordId-filtered endpoint is known to lie.
        return _hold(
            leg_id,
            "no_exhaustive_history_reader",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
        )
    try:
        matches, searched_to_the_end = finder(
            inst_id=instrument_id,
            order_id=order_id,
            max_pages=int(history_search_max_pages),
        )
    except Exception:
        return _hold(
            leg_id,
            "history_search_failed",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
        )

    match_count = len(matches or [])
    if match_count:
        # The order is in history after all, past the snapshot's first page.
        # Classifying its state is the existing path's job, not this one's.
        return _hold(
            leg_id,
            "present_in_exhausted_history",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
            searched_to_the_end=bool(searched_to_the_end),
            history_matches=match_count,
        )
    if not searched_to_the_end:
        return _hold(
            leg_id,
            "history_not_exhausted",
            order_id=order_id,
            instrument_id=instrument_id,
            age_seconds=age_seconds,
            searched_to_the_end=False,
            history_matches=0,
        )

    return AbsentEntryVerdict(
        leg_id=leg_id,
        status="collect",
        reason=TERMINAL_REASON,
        order_id=order_id,
        instrument_id=instrument_id,
        age_seconds=age_seconds,
        searched_to_the_end=True,
        history_matches=0,
    )


#: Reasons that mean "this leg is not the kind of thing 6i is about". A caller
#: logging every round must stay silent for these or it prints a line per leg
#: per round forever; everything else is a genuine hold and has to be visible.
QUIET_HOLD_REASONS = frozenset(
    {
        "not_an_entry_leg",
        "not_a_conditional_entry",
        "not_pending",
        "still_on_the_pending_list",
        "present_in_history_page",
    }
)


def format_verdict_for_log(verdict: AbsentEntryVerdict) -> str:
    """One line, every round, for any leg that actually reached the decision.

    Phase 6i shipped without this and the cost was immediate: the sweep ran for
    ninety minutes in production, refused every round for a reason nobody could
    see, and the only way to learn that was to go and ask it by hand. A hold
    that says nothing is indistinguishable from a sweep that is not running --
    and from a sweep that has nothing to do.
    """

    return (
        f"absent_conditional_entry leg={verdict.leg_id} "
        f"order={verdict.order_id or '-'} inst={verdict.instrument_id or '-'} "
        f"verdict={verdict.status} reason={verdict.reason} "
        f"age_hours={(verdict.age_seconds or 0) // 3600} "
        f"searched_to_the_end={verdict.searched_to_the_end} "
        f"history_matches={verdict.history_matches}"
    )


def incident_summary(verdict: AbsentEntryVerdict) -> dict[str, Any]:
    """What the alert says. Enough to act on without opening the database."""

    return {
        "component": "absent_conditional_entry",
        "reason_code": verdict.reason,
        "impact": (
            f"order={verdict.order_id} inst={verdict.instrument_id} "
            f"age_hours={(verdict.age_seconds or 0) // 3600} "
            "absent_from_pending_and_exhausted_history"
        ),
        "containment": "entry_leg_marked_exchange_cancelled_no_exchange_write",
    }


def incident_fingerprint(verdict: AbsentEntryVerdict) -> str:
    """One occurrence per order, not per round."""

    return f"absent-conditional-entry:{verdict.order_id}"


__all__ = [
    "HISTORY_SEARCH_MAX_PAGES",
    "INCIDENT_TYPE",
    "MIN_ABSENCE_AGE",
    "QUIET_HOLD_REASONS",
    "TERMINAL_REASON",
    "AbsentEntryVerdict",
    "evaluate_absent_conditional_entry",
    "format_verdict_for_log",
    "incident_fingerprint",
    "incident_summary",
    "instrument_id_for_leg",
    "snapshot_read_failed_for",
]
