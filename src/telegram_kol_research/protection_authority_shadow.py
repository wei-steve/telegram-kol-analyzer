"""Run the phase-6 protection chain beside production, deciding nothing.

The first of the two deployments phase 6 task 1 is split into. This pass
resolves, for every live position, which protection orders
:mod:`protection_authority` says the position owns, and compares that with what
``protection_attribution.match_position_protection`` -- the matcher production
still uses -- says about the same position. It writes one
``execution_events`` row per position **per change** and returns counters for
the reconcile round log. It issues no exchange write, and nothing here feeds a
decision.

Why compare at all, rather than just switch: the two answers differ in a way
that matters and in a way that does not, and only production traffic can say
which is which.

* ``agreed`` -- both name the same order id set. This is the **positive
  observation**: without it, "no disagreements" is indistinguishable from "no
  positions to disagree about" (ARCHITECTURE section 6). It is counted and
  persisted, not merely absent.
* ``chain_resolved_legacy_ambiguous`` -- the improvement being bought. One
  unowned pending row makes the legacy matcher call the whole instrument
  ambiguous; the chain places that row by ``TU`` and keeps going.
* ``chain_resolved_legacy_absent`` -- the same improvement by the other route:
  the legacy matcher reports ``absent`` because nothing local can attribute the
  order, so its verdict is "this position has no protection" while the exchange
  holds one. Kept apart from ``set_mismatch`` because reading it as a
  disagreement would frame the fix as a defect.
* ``set_mismatch`` -- the two disagree about a concrete order id. This is the
  finding that would stop the switch, so it is recorded with both sets.
* ``chain_frozen`` -- the chain refuses where the legacy matcher did not. Safe
  by construction, but it costs a write that used to happen, so it is counted
  separately rather than folded into agreement.
* ``unbound_position`` -- a live position no verified leg owns. Counted only:
  nothing is written down about a position this system does not own.

Rows are appended only when a position's comparison *changes*, the same
change-append discipline ``position_reconciliation_observations`` uses -- one
row per position per round would bury the finding under its own repetitions.
The counters are per round and go to the journal line, so a window can be
summed from the journal even if nothing changed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.protection_attribution import match_position_protection
from telegram_kol_research.protection_authority import (
    FREEZE_POSITION_NOT_VERIFIED,
    evaluate_cancel_precheck,
    ledger_drift,
    resolve_protection_authority,
    summarize_authority,
)
from telegram_kol_research.protection_ledger import (
    list_verified_account_ledger_rows,
)

logger = logging.getLogger(__name__)

SHADOW_EVENT_ACTION = "protection_authority_shadow"

VERDICT_AGREED = "agreed"
VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS = "chain_resolved_legacy_ambiguous"
VERDICT_SET_MISMATCH = "set_mismatch"
#: The chain named this position's protection and the legacy matcher answered
#: ``absent`` -- it has no ownership source for an order nothing local records.
#: Same family as ``chain_resolved_legacy_ambiguous``: an improvement, not a
#: disagreement about a concrete order. Observed in production 2026-09-10 on
#: both positions of that afternoon, where the legacy view was that two live
#: positions had no protection at all while the exchange held a stop for each.
VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT = "chain_resolved_legacy_absent"
VERDICT_CHAIN_FROZEN = "chain_frozen"
VERDICT_BOTH_EMPTY = "both_empty"
#: A live position no verified entry leg owns -- somebody's manual position, or
#: one this system never opened. Phase 6 never writes to it, so it is counted
#: and nothing about it is written down: a row naming a manual position is the
#: beginning of claiming it, which phase 3's offline check forbids outright.
VERDICT_UNBOUND_POSITION = "unbound_position"

SHADOW_VERDICTS = (
    VERDICT_AGREED,
    VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS,
    VERDICT_SET_MISMATCH,
    VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT,
    VERDICT_CHAIN_FROZEN,
    VERDICT_BOTH_EMPTY,
    VERDICT_UNBOUND_POSITION,
)


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    pos_id: str
    instrument_id: str
    verdict: str
    chain_status: str
    chain_reason_code: str | None
    chain_order_ids: tuple[str, ...]
    legacy_status: str
    legacy_order_ids: tuple[str, ...]
    adopted_order_ids: tuple[str, ...]
    excluded_order_ids: tuple[str, ...]
    cancel_precheck: Mapping[str, str]
    ledger_drift: Mapping[str, str]
    detail: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        return json.dumps(
            {
                "pos_id": self.pos_id,
                "verdict": self.verdict,
                "chain_status": self.chain_status,
                "chain_reason_code": self.chain_reason_code,
                "chain_order_ids": sorted(self.chain_order_ids),
                "legacy_status": self.legacy_status,
                "legacy_order_ids": sorted(self.legacy_order_ids),
                "adopted_order_ids": sorted(self.adopted_order_ids),
                "excluded_order_ids": sorted(self.excluded_order_ids),
                "cancel_precheck": dict(sorted(self.cancel_precheck.items())),
                "ledger_drift": dict(sorted(self.ledger_drift.items())),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def compare_position_protection(
    session,
    *,
    venue: str,
    position: Mapping[str, Any],
    instrument_id: str,
    pending_rows: Sequence[Mapping[str, Any]] | None,
    all_positions: Sequence[Mapping[str, Any]],
    recheck_rows: Sequence[Mapping[str, Any]] | None = None,
) -> ShadowComparison | None:
    """Resolve one position both ways and name the difference. Read-only."""

    pos_id = _first_text(position, "posId", "pos_id", "id")
    if not pos_id:
        return None
    side = _first_text(position, "posSide", "pos_side", "side") or ""
    authority = resolve_protection_authority(
        session,
        venue=venue,
        pos_id=pos_id,
        instrument_id=instrument_id,
        side=side,
        pending_rows=pending_rows,
    )
    ledger_rows = list_verified_account_ledger_rows(session, venue=venue)
    exact_order_position_ids = {
        str(row.order_id): str(row.pos_id)
        for row in ledger_rows
        if str(row.order_id or "").strip()
    }
    legacy = match_position_protection(
        [dict(row) for row in all_positions],
        [dict(row) for row in (pending_rows or [])],
        evidence_available=pending_rows is not None,
        exact_order_position_ids=exact_order_position_ids,
    ).by_pos_id.get(pos_id)
    legacy_status = legacy.status if legacy is not None else "absent"
    legacy_order_ids = tuple(legacy.order_ids) if legacy is not None else ()
    chain_order_ids = authority.order_ids

    if authority.reason_code == FREEZE_POSITION_NOT_VERIFIED:
        verdict = VERDICT_UNBOUND_POSITION
    elif not authority.resolved:
        verdict = VERDICT_CHAIN_FROZEN
    elif set(chain_order_ids) == set(legacy_order_ids):
        verdict = VERDICT_BOTH_EMPTY if not chain_order_ids else VERDICT_AGREED
    elif legacy_status == "present_but_ambiguous" and chain_order_ids:
        verdict = VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS
    elif legacy_status == "absent" and chain_order_ids:
        verdict = VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT
    else:
        verdict = VERDICT_SET_MISMATCH

    return ShadowComparison(
        pos_id=pos_id,
        instrument_id=instrument_id,
        verdict=verdict,
        chain_status=authority.status,
        chain_reason_code=authority.reason_code,
        chain_order_ids=chain_order_ids,
        legacy_status=legacy_status,
        legacy_order_ids=legacy_order_ids,
        adopted_order_ids=tuple(item.order_id for item in authority.adoptions),
        excluded_order_ids=tuple(authority.excluded_pending_entry_order_ids),
        # Phase 6b, shadow half: for every order the chain says this position
        # owns, ask the exact question a cancel would ask -- is this still the
        # same order, by id, instrument, side, trigger and size -- without
        # cancelling anything. A cancel only happens when something asks for
        # one, which may be never in a given window; this asks the question on
        # every live protection order instead.
        #
        # ``recheck_rows`` is a *second* read, taken after the one the
        # authority was resolved from. Evaluating the check against the same
        # read it was built from would compare a read with itself and could
        # only ever answer "match" -- a counter that cannot fail is not an
        # observation. The two reads are what makes this the real question.
        cancel_precheck={
            order_id: (
                evaluate_cancel_precheck(
                    authority,
                    recheck_rows if recheck_rows is not None else pending_rows,
                    order_id,
                )
                or "match"
            )
            for order_id in authority.order_ids
        },
        ledger_drift=ledger_drift(authority),
        detail={
            "authority": summarize_authority(authority),
            "legacy_evidence": dict(getattr(legacy, "evidence", {}) or {}),
        },
    )


def run_protection_authority_shadow_pass(
    session_factory,
    *,
    deepcoin_client: Any,
    now: datetime,
    venue: str = "deepcoin",
) -> dict[str, Any]:
    """Compare both answers for every live position. No exchange write.

    A failed read is recorded as a read failure and leaves that instrument's
    positions uncompared; it is never turned into "no pending orders", which
    would manufacture agreement out of a network error.
    """

    counts = {verdict: 0 for verdict in SHADOW_VERDICTS}
    read_failures: list[str] = []
    recorded = 0
    excluded_pending_entry_stops = 0
    cancel_precheck_counts: dict[str, int] = {}
    ledger_drift_count = 0
    would_adopt_count = 0
    try:
        positions = deepcoin_client.list_positions()
    except Exception:
        logger.warning("protection shadow pass could not read positions", exc_info=True)
        return {
            "positions_seen": 0,
            "counts_by_verdict": counts,
            "excluded_pending_entry_stops": 0,
            "cancel_precheck": {},
            "ledger_drift": 0,
            "would_adopt": 0,
            "rows_recorded": 0,
            "read_failures": ["positions"],
        }
    live = [row for row in positions if isinstance(row, Mapping) and _has_size(row)]
    pending_by_instrument: dict[str, list[dict[str, Any]] | None] = {}
    recheck_by_instrument: dict[str, list[dict[str, Any]] | None] = {}
    for row in live:
        instrument_id = (_first_text(row, "instId", "inst_id", "instrument_id") or "").upper()
        if not instrument_id or instrument_id in pending_by_instrument:
            continue
        try:
            pending_by_instrument[instrument_id] = deepcoin_client.list_trigger_orders_pending(
                inst_id=instrument_id
            )
        except Exception:
            logger.warning(
                "protection shadow pass could not read pending trigger orders inst_id=%s",
                instrument_id,
                exc_info=True,
            )
            pending_by_instrument[instrument_id] = None
            recheck_by_instrument[instrument_id] = None
            read_failures.append(instrument_id)
            continue
        try:
            # The second read the cancel path would take. Its cost is one GET
            # per instrument per round; without it the pre-cancel check has
            # nothing to compare against but itself.
            recheck_by_instrument[instrument_id] = (
                deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
            )
        except Exception:
            logger.warning(
                "protection shadow pass could not re-read pending trigger orders inst_id=%s",
                instrument_id,
                exc_info=True,
            )
            recheck_by_instrument[instrument_id] = None
            read_failures.append(f"{instrument_id}:recheck")

    with session_factory() as session:
        for row in live:
            instrument_id = (
                _first_text(row, "instId", "inst_id", "instrument_id") or ""
            ).upper()
            if not instrument_id:
                continue
            comparison = compare_position_protection(
                session,
                venue=venue,
                position=row,
                instrument_id=instrument_id,
                pending_rows=pending_by_instrument.get(instrument_id),
                all_positions=live,
                recheck_rows=recheck_by_instrument.get(instrument_id),
            )
            if comparison is None:
                continue
            counts[comparison.verdict] = counts.get(comparison.verdict, 0) + 1
            excluded_pending_entry_stops += len(comparison.excluded_order_ids)
            for outcome in comparison.cancel_precheck.values():
                cancel_precheck_counts[outcome] = (
                    cancel_precheck_counts.get(outcome, 0) + 1
                )
            ledger_drift_count += len(comparison.ledger_drift)
            # Phase 6e, shadow half. The chain already works out which orders it
            # would have to write into the ledger before it may act on them;
            # until now that plan was only recorded incidentally, inside the row
            # of a position whose comparison happened to change. Counting it is
            # what makes "how many protection orders is the ledger missing"
            # answerable without writing anything.
            would_adopt_count += len(comparison.adopted_order_ids)
            if comparison.verdict == VERDICT_UNBOUND_POSITION:
                continue
            if _record_when_changed(
                session_factory,
                comparison=comparison,
                venue=venue,
                now=now,
            ):
                recorded += 1
    return {
        "positions_seen": len(live),
        "counts_by_verdict": counts,
        # The positive observation paired with ``chain_frozen``: every time a
        # resting entry's own stop is stepped over instead of freezing the
        # position, it is counted here. Without it, a freeze count that fell to
        # zero could equally mean "the exclusion worked" or "no entry was
        # resting" (ARCHITECTURE section 6).
        "excluded_pending_entry_stops": excluded_pending_entry_stops,
        # ``match`` is the positive observation for the cancel path: a window
        # with no mismatches proves nothing on its own, because a window with
        # no protection orders looks exactly the same.
        "cancel_precheck": cancel_precheck_counts,
        # Not a gate -- see ``protection_authority.ledger_drift``. Counted so a
        # future decision about keying the cancel gate on the ledger rests on a
        # measurement rather than on an assumption that drift is rare.
        "ledger_drift": ledger_drift_count,
        "would_adopt": would_adopt_count,
        "rows_recorded": recorded,
        "read_failures": read_failures,
    }


def _record_when_changed(
    session_factory,
    *,
    comparison: ShadowComparison,
    venue: str,
    now: datetime,
) -> bool:
    """Append one row only when this position's comparison changed."""

    with session_factory() as session:
        previous = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.venue == venue.lower())
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .filter(ExecutionEvent.pos_id == comparison.pos_id)
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        if previous is not None:
            try:
                stored = json.loads(previous.after_json or "{}")
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict) and stored.get("fingerprint") == comparison.fingerprint:
                return False
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action=SHADOW_EVENT_ACTION,
            venue=venue,
            status="observed",
            pos_id=comparison.pos_id,
            symbol=comparison.instrument_id,
            reason=comparison.verdict,
            before={
                "legacy_status": comparison.legacy_status,
                "legacy_order_ids": list(comparison.legacy_order_ids),
            },
            after={
                "fingerprint": comparison.fingerprint,
                "excluded_pending_entry_order_ids": list(
                    comparison.excluded_order_ids
                ),
                "cancel_precheck": dict(comparison.cancel_precheck),
                "ledger_drift": dict(comparison.ledger_drift),
                "chain_status": comparison.chain_status,
                "chain_reason_code": comparison.chain_reason_code,
                "chain_order_ids": list(comparison.chain_order_ids),
                "adopted_order_ids": list(comparison.adopted_order_ids),
                "detail": dict(comparison.detail),
            },
            created_at=now,
        ),
    )
    return True


def _has_size(row: Mapping[str, Any]) -> bool:
    for key in ("pos", "sz", "size", "availPos", "Po"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return abs(float(str(value))) > 0
        except (TypeError, ValueError):
            continue
    return False


def _first_text(row: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return None
