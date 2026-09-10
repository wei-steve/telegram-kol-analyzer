"""Write down the protection the exchange holds and our ledger never recorded.

Phase 6e. A migrated limit entry carries ``slTriggerPx`` on the order itself,
so the exchange arms that stop the moment the entry fills -- and its order id
never appears in the submission receipt. The two paths that put entry
protection into ``position_protection_ledger`` both miss it:
``recovery_live_submit``'s ``entry_protection_response`` reads the receipt, and
``execution_bindings``' adoption path requires ``order_kind == "trigger_limit"``
*and* a request carrying both ``tpTriggerPx`` and ``slTriggerPx``. Production
consequence, observed 2026-09-10: two live positions each with a stop on the
exchange, no ledger row for either, the legacy matcher reporting ``absent``, and
``backup_stop_blocked`` filed twice because the primary stop could not be
verified.

**This does not relax the existing gate.** Why that gate demanded both triggers
is not recorded anywhere -- not in its commit, not in a comment, not in a test
-- and the convenient explanation ("SL-only did not exist yet") is false:
SL-only trigger-limit entries were already in production eleven days before it
was written. Relaxing a predicate whose reason nobody knows would silently
re-admit the 74 legs its author excluded. So this is a *second*, parallel route
whose admission rests on phase 6's evidence rather than on the shape of the
request:

    ``TU == posId`` on a ``TriggerOrder`` frame
      + that exact ordId present in ``trigger-orders-pending``
      + instrument and ``posSide`` agree
      + ``triggerOrderType == "TPSL"``

which is *stronger* than what the old path asked for, and treats ``limit`` and
``trigger_limit``, SL-only and combined, alike. The old path is left exactly as
it is; it has produced nothing since 2026-07-24 and keeping it costs nothing.

**It writes a ledger row and nothing else.** No exchange call is made here, and
the adopted row is marked ``exchange_adopted_by_tu`` so the one consumer that
would turn a ledger row into an exchange write --
``trigger_backup_stop_executor`` -- can hold back until a person has agreed to
that separately (phase 6f). A ledger write that silently becomes an order is
the failure mode this module is most likely to cause, so the marker exists
before the need for it does.
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
from telegram_kol_research.protection_authority import (
    ADOPTION_EVIDENCE_SOURCE,
    adopt_protection_orders,
    resolve_protection_authority,
    summarize_authority,
)

logger = logging.getLogger(__name__)

ADOPTION_EVENT_ACTION = "protection_adopted_from_exchange"
ADOPTION_REFUSAL_EVENT_ACTION = "protection_adoption_refused"


@dataclass(frozen=True, slots=True)
class ProtectionAdoptionResult:
    positions_seen: int = 0
    adopted_rows: int = 0
    refused_positions: int = 0
    read_failures: tuple[str, ...] = ()


def run_protection_adoption_pass(
    session_factory,
    *,
    deepcoin_client: Any,
    now: datetime,
    venue: str = "deepcoin",
) -> ProtectionAdoptionResult:
    """Adopt, for every live position, the protection only the exchange knows.

    Idempotent by construction: an order the ledger already names is not an
    adoption candidate, so a second pass over an unchanged account adopts
    nothing and writes nothing.
    """

    try:
        positions = deepcoin_client.list_positions()
    except Exception:
        logger.warning("protection adoption could not read positions", exc_info=True)
        return ProtectionAdoptionResult(read_failures=("positions",))
    live = [row for row in positions if isinstance(row, Mapping) and _has_size(row)]
    pending_by_instrument: dict[str, list[dict[str, Any]] | None] = {}
    read_failures: list[str] = []
    for row in live:
        instrument_id = (_first_text(row, "instId", "inst_id") or "").upper()
        if not instrument_id or instrument_id in pending_by_instrument:
            continue
        try:
            pending_by_instrument[instrument_id] = (
                deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
            )
        except Exception:
            logger.warning(
                "protection adoption could not read pending trigger orders inst_id=%s",
                instrument_id,
                exc_info=True,
            )
            pending_by_instrument[instrument_id] = None
            read_failures.append(instrument_id)

    adopted_rows = 0
    refused = 0
    for row in live:
        instrument_id = (_first_text(row, "instId", "inst_id") or "").upper()
        pos_id = _first_text(row, "posId", "pos_id", "id")
        if not instrument_id or not pos_id:
            continue
        side = _first_text(row, "posSide", "pos_side", "side") or ""
        pending_rows = pending_by_instrument.get(instrument_id)
        with session_factory() as session:
            authority = resolve_protection_authority(
                session,
                venue=venue,
                pos_id=pos_id,
                instrument_id=instrument_id,
                side=side,
                pending_rows=pending_rows,
            )
        if not authority.resolved:
            # A refusal is recorded only when there was something to adopt --
            # a position the chain cannot resolve at all is the ordinary
            # steady state for anything this system does not own.
            if authority.reason_code and authority.unattributable_order_ids:
                _record_refusal(
                    session_factory, authority=authority, venue=venue, now=now
                )
                refused += 1
            continue
        if not authority.adoptions:
            continue
        try:
            with session_factory() as session:
                written = adopt_protection_orders(
                    session, authority=authority, venue=venue, adopted_at=now
                )
                session.commit()
        except Exception:
            logger.warning(
                "protection adoption failed to write ledger rows pos_id=%s",
                authority.pos_id,
                exc_info=True,
            )
            _record_refusal(
                session_factory,
                authority=authority,
                venue=venue,
                now=now,
                reason_code="protection_adoption_write_failed",
            )
            refused += 1
            continue
        adopted_rows += written
        _record_adoption(
            session_factory, authority=authority, venue=venue, now=now
        )
    return ProtectionAdoptionResult(
        positions_seen=len(live),
        adopted_rows=adopted_rows,
        refused_positions=refused,
        read_failures=tuple(read_failures),
    )


def _record_adoption(
    session_factory, *, authority: Any, venue: str, now: datetime
) -> None:
    """One durable row per adoption, naming the evidence it rested on.

    An operator reading this months later has to be able to answer "why does
    the ledger claim an order this system never submitted", and the answer is
    the ``TU`` frame plus the pending row -- so both go in the record rather
    than a summary of them.
    """

    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action=ADOPTION_EVENT_ACTION,
            venue=venue,
            status="adopted",
            execution_binding_id=authority.execution_binding_id,
            strategy_instance_id=authority.strategy_instance_id,
            pos_id=authority.pos_id,
            symbol=authority.instrument_id,
            side=authority.side,
            order_id=",".join(item.order_id for item in authority.adoptions),
            reason=ADOPTION_EVIDENCE_SOURCE,
            after={
                "adopted": [
                    {
                        "order_id": item.order_id,
                        "purpose": item.purpose,
                        "trigger_price": item.trigger_price,
                        "size_text": item.size_text,
                        "evidence": dict(item.evidence),
                    }
                    for item in authority.adoptions
                ],
                "authority": summarize_authority(authority),
                "execution_order_leg_id": authority.execution_order_leg_id,
            },
            created_at=now,
        ),
    )
    _capture_incident(authority=authority, now=now, session_factory=session_factory)


def _record_refusal(
    session_factory,
    *,
    authority: Any,
    venue: str,
    now: datetime,
    reason_code: str | None = None,
) -> None:
    """Why a position was left alone, with the order ids that could not be placed."""

    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action=ADOPTION_REFUSAL_EVENT_ACTION,
            venue=venue,
            status="refused",
            pos_id=authority.pos_id,
            symbol=authority.instrument_id,
            side=authority.side,
            reason=reason_code or authority.reason_code,
            after={
                "reason_code": reason_code or authority.reason_code,
                "unattributable_order_ids": list(
                    authority.unattributable_order_ids
                ),
                "authority": summarize_authority(authority),
            },
            created_at=now,
        ),
    )


def _capture_incident(*, authority: Any, now: datetime, session_factory) -> None:
    """Tell a person that the ledger grew a row for an order we never sent."""

    try:
        from telegram_kol_research.config import load_runtime_incident_config
        from telegram_kol_research.runtime_incident_adapters import (
            capture_protection_adopted_from_exchange,
        )

        capture_protection_adopted_from_exchange(
            session_factory,
            config=load_runtime_incident_config(),
            pos_id=authority.pos_id,
            instrument_id=authority.instrument_id,
            adopted=[
                {
                    "order_id": item.order_id,
                    "purpose": item.purpose,
                    "trigger_price": item.trigger_price,
                    "size_text": item.size_text,
                }
                for item in authority.adoptions
            ],
            occurred_at=now,
        )
    except Exception:  # pragma: no cover - an alert must never break the caller
        logger.warning(
            "protection adoption incident capture failed pos_id=%s",
            authority.pos_id,
            exc_info=True,
        )


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
