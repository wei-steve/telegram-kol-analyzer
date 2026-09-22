"""The ladder's durable side: order-level evidence in, derived level out.

Two jobs, and deliberately no third:

1. :func:`reconcile_take_profit_fill_levels` reads one reconcile round's
   exchange snapshot and writes **order-level** evidence -- a reached rung's
   protection-ledger row becomes ``filled`` and carries
   ``evidence_json["take_profit_fill"]`` with its level, its evidence form and
   what was looked at.  It writes nothing to the exchange, creates no intent,
   and raises nothing: a rung it cannot prove is counted and left alone.
2. :func:`derive_filled_tp_level` answers "which stage has this strategy
   really reached" from that evidence, every time, from scratch.  Nothing is
   cached and ``strategy_lifecycles.filled_tp_index`` is not touched -- it is
   the simulator's "price traded through here", which is a different question
   (design section 1.1).  Deriving instead of storing is what makes the answer
   identical across a restart, and what makes a KOL's changed take profit move
   the whole ladder with it rather than leaving a stale number behind.

**No schema change.**  Everything lands in columns that already exist:
``position_protection_ledger.status`` (``String(32)``, no constraint) and its
``evidence_json``.  The readers of that status were audited in
``docs/composite-upstream-fix-status.md`` section 3 before ``filled`` was
first written there: no active set contains it, so a proven fill becomes
invisible to every automatic path -- which is the intended meaning, since a
take profit that traded must not be cancelled, replaced or re-placed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from telegram_kol_research.models import (
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionReconciliationObservation,
)
from telegram_kol_research.stop_ladder import (
    EVIDENCE_FORM_POSITION_DECREASE,
    LEDGER_FILLED_STATUS,
    TAKE_PROFIT_LEDGER_PURPOSES,
    LadderRung,
    RungVerdict,
    filled_level_for_position,
    rung_reached,
    rungs_for_position,
    strategy_filled_level,
)

logger = logging.getLogger(__name__)

#: ``position_mutation_intents.operation`` values that mean "we asked for this
#: order to go away".  An order that left the exchange after one of these did
#: not fill, whatever else the snapshot says.
CANCEL_INTENT_OPERATIONS = ("cancel_position_sltp", "cancel_trigger_order")

#: The evidence key both writers use on a ledger row.
TAKE_PROFIT_FILL_EVIDENCE_KEY = "take_profit_fill"


@dataclass(frozen=True, slots=True)
class PositionLadder:
    """One position's rungs and the level its own evidence proves."""

    pos_id: str
    side: str
    rungs: tuple[LadderRung, ...] = ()
    filled_level: int = 0
    reached_order_ids: tuple[str, ...] = ()
    evidence_forms: Mapping[str, str] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "pos_id": self.pos_id,
            "side": self.side,
            "filled_level": self.filled_level,
            "rungs": [rung.as_evidence() for rung in self.rungs],
            "reached_order_ids": list(self.reached_order_ids),
            "evidence_forms": dict(self.evidence_forms),
        }


@dataclass(frozen=True, slots=True)
class StrategyLadder:
    """The strategy-level answer: the highest level any live leg reached."""

    execution_binding_id: int | None
    filled_level: int = 0
    positions: tuple[PositionLadder, ...] = ()

    @property
    def rungs_by_position(self) -> dict[str, tuple[LadderRung, ...]]:
        return {position.pos_id: position.rungs for position in self.positions}

    @property
    def levels_by_position(self) -> dict[str, int]:
        return {position.pos_id: position.filled_level for position in self.positions}

    def as_evidence(self) -> dict[str, Any]:
        return {
            "execution_binding_id": self.execution_binding_id,
            "filled_level": self.filled_level,
            "positions": [position.as_evidence() for position in self.positions],
        }


@dataclass(frozen=True, slots=True)
class StopLadderReconcileResult:
    """What one reconcile round decided.  Counters only -- nothing alerts."""

    filled: int = 0
    unproven: int = 0
    positions_seen: int = 0
    reasons: Mapping[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "filled": self.filled,
            "unproven": self.unproven,
            "positions_seen": self.positions_seen,
            "reasons": dict(sorted(self.reasons.items())),
        }


def reconcile_take_profit_fill_levels(
    session,
    *,
    positions: Sequence[Mapping[str, Any]],
    pending_orders: Sequence[Mapping[str, Any]],
    trigger_history: Sequence[Mapping[str, Any]],
    pending_snapshot_complete_by_instrument: Mapping[str, bool] | None = None,
    snapshot_errors: Mapping[str, Any] | None = None,
    observed_at: datetime,
    venue: str = "deepcoin",
) -> StopLadderReconcileResult:
    """Write order-level fill evidence for every rung this round can prove.

    Read-only against the exchange: the caller has already made the three
    reads, and this function only compares them with what the ledger says it
    owns.  A rung it cannot prove is counted under its reason code and left
    exactly as it is -- there is no alert, by the account owner's explicit
    instruction, and no state that has to be cleaned up later.
    """

    live_positions = {
        pos_id: row
        for row in positions or ()
        if isinstance(row, Mapping)
        and (pos_id := _text(row.get("posId") or row.get("pos_id") or row.get("id")))
        and _nonzero(row)
    }
    if not live_positions:
        return StopLadderReconcileResult()

    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == venue)
        .filter(PositionProtectionLedger.pos_id.in_(sorted(live_positions)))
        .filter(
            PositionProtectionLedger.purpose.in_(sorted(TAKE_PROFIT_LEDGER_PURPOSES))
        )
        .all()
    )
    if not ledger_rows:
        return StopLadderReconcileResult(positions_seen=len(live_positions))

    rows_by_pos: dict[str, list[PositionProtectionLedger]] = {}
    for row in ledger_rows:
        rows_by_pos.setdefault(str(row.pos_id), []).append(row)

    pending_ids = _pending_order_ids(pending_orders)
    completeness = {
        str(key).upper(): bool(value)
        for key, value in (pending_snapshot_complete_by_instrument or {}).items()
    }
    snapshot_healthy = not bool(snapshot_errors)
    cancel_intents = _cancel_intent_order_ids(
        session,
        venue=venue,
        order_ids={str(row.order_id or "").strip() for row in ledger_rows},
    )

    filled = 0
    unproven = 0
    reasons: dict[str, int] = {}
    for pos_id, rows in sorted(rows_by_pos.items()):
        side = _text(rows[0].side)
        instrument_id = _text(rows[0].instrument_id).upper()
        rungs = rungs_for_position(rows, side=side)
        if not rungs:
            continue
        complete = bool(snapshot_healthy and completeness.get(instrument_id, False))
        decrease = _position_decreased(session, venue=venue, pos_id=pos_id)
        row_by_order_id = {str(row.order_id or "").strip(): row for row in rows}
        for rung in rungs:
            row = row_by_order_id.get(rung.order_id)
            if row is None or str(row.status or "").lower() == LEDGER_FILLED_STATUS:
                continue
            verdict = rung_reached(
                rung=rung,
                pending_order_ids=pending_ids,
                pending_snapshot_complete=complete,
                trigger_history=trigger_history or (),
                cancel_intent_order_ids=cancel_intents,
                position_decrease_proven=decrease,
            )
            if verdict.reached:
                _write_fill_evidence(
                    row,
                    rung=rung,
                    verdict=verdict,
                    observed_at=observed_at,
                    observation_ids=_recent_observation_ids(
                        session, venue=venue, pos_id=pos_id
                    ),
                )
                filled += 1
                continue
            if verdict.reason_code:
                reasons[verdict.reason_code] = reasons.get(verdict.reason_code, 0) + 1
            if rung.order_id not in pending_ids:
                # Gone from the exchange and not proven: this is the count the
                # spec asks for. Never an incident, never an alert.
                unproven += 1
    if filled or unproven:
        logger.info(
            "stop_ladder_fill_levels filled=%s unproven=%s positions=%s",
            filled,
            unproven,
            len(live_positions),
        )
    return StopLadderReconcileResult(
        filled=filled,
        unproven=unproven,
        positions_seen=len(live_positions),
        reasons=reasons,
    )


def derive_filled_tp_level(
    session,
    *,
    execution_binding_id: int | None = None,
    pos_ids: Iterable[str] | None = None,
    venue: str = "deepcoin",
) -> StrategyLadder:
    """Derive, never recall: the level each position's own evidence proves.

    ``pos_ids`` narrows the answer to the legs that still hold a position,
    which is what both the message column and the shadow want; without it the
    whole binding's ledger is read.
    """

    query = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.venue == venue
    )
    wanted = {str(value).strip() for value in (pos_ids or ()) if str(value).strip()}
    if execution_binding_id is not None:
        query = query.filter(
            PositionProtectionLedger.execution_binding_id == int(execution_binding_id)
        )
    if wanted:
        query = query.filter(PositionProtectionLedger.pos_id.in_(sorted(wanted)))
    if execution_binding_id is None and not wanted:
        return StrategyLadder(execution_binding_id=None)
    rows = query.filter(
        PositionProtectionLedger.purpose.in_(sorted(TAKE_PROFIT_LEDGER_PURPOSES))
    ).all()

    rows_by_pos: dict[str, list[PositionProtectionLedger]] = {}
    for row in rows:
        rows_by_pos.setdefault(str(row.pos_id), []).append(row)

    ladders: list[PositionLadder] = []
    for pos_id in sorted(rows_by_pos):
        position_rows = rows_by_pos[pos_id]
        side = _text(position_rows[0].side)
        rungs = rungs_for_position(position_rows, side=side)
        row_by_order_id = {
            str(row.order_id or "").strip(): row for row in position_rows
        }
        verdicts: list[RungVerdict] = []
        reached_order_ids: list[str] = []
        evidence_forms: dict[str, str] = {}
        for rung in rungs:
            row = row_by_order_id.get(rung.order_id)
            evidence = _fill_evidence(row)
            if evidence is None:
                continue
            reached_order_ids.append(rung.order_id)
            form = _text(evidence.get("evidence_form")) or None
            if form:
                evidence_forms[rung.order_id] = form
            verdicts.append(
                RungVerdict(
                    order_id=rung.order_id,
                    level=rung.level,
                    reached=True,
                    evidence_form=form,
                    evidence=evidence,
                )
            )
        ladders.append(
            PositionLadder(
                pos_id=pos_id,
                side=side,
                rungs=rungs,
                filled_level=filled_level_for_position(verdicts),
                reached_order_ids=tuple(reached_order_ids),
                evidence_forms=evidence_forms,
            )
        )
    return StrategyLadder(
        execution_binding_id=(
            int(execution_binding_id) if execution_binding_id is not None else None
        ),
        filled_level=strategy_filled_level(
            {ladder.pos_id: ladder.filled_level for ladder in ladders}
        ),
        positions=tuple(ladders),
    )


# ------------------------------------------------------------------ helpers


def _write_fill_evidence(
    row: PositionProtectionLedger,
    *,
    rung: LadderRung,
    verdict: RungVerdict,
    observed_at: datetime,
    observation_ids: tuple[int, ...],
) -> None:
    """One writer for the ledger's ``filled``, shared with ``protection_health``."""

    from telegram_kol_research.protection_health import (
        record_take_profit_ledger_fill,
    )

    evidence: dict[str, Any] = {
        "level": rung.level,
        "evidence_form": verdict.evidence_form,
        "decided_at": observed_at.isoformat(),
        **{
            key: value
            for key, value in dict(verdict.evidence).items()
            if key != "order_id"
        },
    }
    if verdict.evidence_form == EVIDENCE_FORM_POSITION_DECREASE and observation_ids:
        evidence["observation_ids"] = list(observation_ids)
    record_take_profit_ledger_fill(
        row,
        order_id=rung.order_id,
        evidence=evidence,
        observed_at=observed_at,
    )


def _fill_evidence(row: Any) -> dict[str, Any] | None:
    """The ``take_profit_fill`` evidence on this ledger row, if it has any.

    Either writer's shape counts, and so does a bare ``filled`` status: a row
    written by an earlier release carries no ladder level, and re-deriving the
    level from the sequence is exactly the point.
    """

    if row is None:
        return None
    payload = _json_object(getattr(row, "evidence_json", None)).get(
        TAKE_PROFIT_FILL_EVIDENCE_KEY
    )
    if isinstance(payload, Mapping):
        return dict(payload)
    if str(getattr(row, "status", "") or "").lower() == LEDGER_FILLED_STATUS:
        return {"evidence_form": None, "source": "ledger_status"}
    return None


def _pending_order_ids(pending_orders: Sequence[Mapping[str, Any]] | None) -> set[str]:
    from telegram_kol_research.position_take_profit_orders import (
        _order_identity_ids,
    )

    ids: set[str] = set()
    for row in pending_orders or ():
        if isinstance(row, Mapping):
            ids.update(_order_identity_ids(row))
    return ids


def _cancel_intent_order_ids(
    session, *, venue: str, order_ids: set[str]
) -> set[str]:
    named = {value for value in order_ids if value}
    if not named:
        return set()
    rows = (
        session.query(PositionMutationIntent.order_id)
        .filter(PositionMutationIntent.venue == venue)
        .filter(PositionMutationIntent.operation.in_(CANCEL_INTENT_OPERATIONS))
        .filter(PositionMutationIntent.order_id.in_(sorted(named)))
        .all()
    )
    return {str(value[0]).strip() for value in rows if str(value[0] or "").strip()}


def _complete_observations(
    session, *, venue: str, pos_id: str, limit: int = 2
) -> list[PositionReconciliationObservation]:
    return (
        session.query(PositionReconciliationObservation)
        .filter(PositionReconciliationObservation.venue == venue)
        .filter(PositionReconciliationObservation.pos_id == str(pos_id))
        .filter(PositionReconciliationObservation.snapshot_complete.is_(True))
        .order_by(
            PositionReconciliationObservation.observed_at.desc(),
            PositionReconciliationObservation.id.desc(),
        )
        .limit(limit)
        .all()
    )


def _position_decreased(session, *, venue: str, pos_id: str) -> bool | None:
    """Did this position get smaller between the two latest complete reads?

    ``None`` means the question cannot be answered -- fewer than two complete
    observations, or a size that will not parse -- and that is not ``False``:
    an unknown must never be spent as evidence in either direction.
    """

    observations = _complete_observations(session, venue=venue, pos_id=pos_id)
    if len(observations) < 2:
        return None
    current = _decimal(observations[0].size_text)
    previous = _decimal(observations[1].size_text)
    if current is None or previous is None:
        return None
    return current < previous


def _recent_observation_ids(session, *, venue: str, pos_id: str) -> tuple[int, ...]:
    return tuple(
        int(row.id)
        for row in _complete_observations(session, venue=venue, pos_id=pos_id)
    )


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(_text(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _nonzero(row: Mapping[str, Any]) -> bool:
    for key in ("pos", "size", "sz", "positionSize", "position_size"):
        if key not in row:
            continue
        number = _decimal(row.get(key))
        if number is None:
            continue
        return abs(number) > 0
    return True


__all__ = [
    "PositionLadder",
    "StopLadderReconcileResult",
    "StrategyLadder",
    "derive_filled_tp_level",
    "reconcile_take_profit_fill_levels",
]
