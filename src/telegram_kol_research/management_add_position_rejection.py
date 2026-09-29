"""Q1 patch (2026-09-29 Mia design, section 7.1): a stop worse than our own
fill, on a strategy where we already refused to add to the position once.

Design: `docs/plans/2026-09-29-mia-management-verification-design.md`, Q1's
ruling. The system deliberately never follows a KOL's add-position
instruction (`resolve_management_directive` returns
``risk_increasing_fanout_forbidden`` for ``add_position`` /
``increase_position``, unchanged by this module). That refusal is correct on
its own, but it leaves a gap: if the KOL's later management messages keep
talking about the position *as if* the add had happened -- naming a new,
worse-for-us stop derived from their own (higher, for a long) average price --
following that number verbatim would realize a loss the KOL never intended
and we never took the risk for. Binding 385 / lifecycle 1343's 2026-09-28
sequence is exactly this shape: a refused add at 83200 (raw 19514), and this
module exists so a later explicit stop worse than our 83800 fill on the same
lifecycle is caught rather than executed as written.

This module answers one question -- has this lifecycle had a rejected
add-position instruction, and is a given explicit price worse than our own
fill -- and decides nothing about execution. The caller (the planner, at the
one `adjust_stop_loss` site this is wired into) is responsible for redirecting
to the strategy break-even price and raising the incident; this keeps the
query and the price comparison independently testable and reusable by any
other site that needs the same judgement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.models import RawMessage, RecognitionDecision

#: The same two raw ``management_action`` values
#: ``resolve_management_directive`` treats as an add (management_directives.py
#: ``risk_increasing`` check), read back out of the stored payload rather than
#: re-run through that function -- this module never re-derives a directive,
#: it only asks whether one particular rejection already happened.
ADD_POSITION_MANAGEMENT_ACTIONS = ("add_position", "increase_position")

#: `recognition_failure_attribution.APPLY_FAILED`. Duplicated as a literal
#: rather than imported so this module never needs to import the attribution
#: layer just to read one constant string back off a column it already reads.
REJECTED_ADD_POSITION_AUTOMATION_REASON = "lifecycle_apply_failed"


@dataclass(frozen=True, slots=True)
class RejectedAddPositionEvidence:
    """One earlier rejected add-position instruction for this lifecycle."""

    raw_message_id: int
    management_action: str

    def as_evidence(self) -> dict[str, Any]:
        return {
            "raw_message_id": self.raw_message_id,
            "management_action": self.management_action,
        }


def find_rejected_add_position_before(
    session,
    *,
    chat_id: int,
    target_lifecycle_id: int,
    signal_at: Any,
) -> RejectedAddPositionEvidence | None:
    """The earliest refused add-position instruction for this lifecycle, if any.

    Query shape, chosen to stay index-backed: ``raw_messages`` is scanned by
    its own ``(chat_id, posted_at, message_id)`` index -- the same index every
    other per-chat time-window read in this codebase already uses -- joined to
    ``recognition_decisions`` by its unique ``raw_message_id`` foreign key.
    Neither column filtered so far can distinguish an add-position rejection
    from any other ``lifecycle_apply_failed`` row (there is no per-target
    index on the JSON payload, and this rejection never reaches
    ``signal_candidates`` or ``message_instruction_items`` -- nothing is
    persisted for an instruction that changed nothing), so only the handful of
    rows those two indexed filters leave are unmarshalled to confirm the
    action and the target. A strategy with a real add-position history is
    rare enough that this residual work is small; a strategy without one never
    reaches the JSON parse at all when there is no ``lifecycle_apply_failed``
    row in its window.
    """

    if signal_at is None or target_lifecycle_id is None:
        return None
    rows = (
        session.query(
            RecognitionDecision.raw_message_id,
            RecognitionDecision.authoritative_payload_json,
        )
        .join(RawMessage, RawMessage.id == RecognitionDecision.raw_message_id)
        .filter(
            RawMessage.chat_id == chat_id,
            RawMessage.posted_at >= signal_at,
            RecognitionDecision.automation_reason
            == REJECTED_ADD_POSITION_AUTOMATION_REASON,
        )
        .order_by(RawMessage.posted_at.asc())
        .all()
    )
    for raw_message_id, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        event = payload.get("lifecycle_event")
        if not isinstance(event, dict):
            continue
        action = str(event.get("management_action") or "").strip().lower()
        if not any(term in action for term in ADD_POSITION_MANAGEMENT_ACTIONS):
            continue
        try:
            event_target = int(event.get("target_lifecycle_id"))
        except (TypeError, ValueError):
            continue
        if event_target != int(target_lifecycle_id):
            continue
        return RejectedAddPositionEvidence(
            raw_message_id=int(raw_message_id), management_action=action,
        )
    return None


def explicit_stop_worse_than_fill(
    *, side: Any, stop_price: Any, avg_entry_price: Any
) -> bool:
    """Whether ``stop_price`` would realize a loss on our own fill.

    A long's stop below its average fill is a loss; a short's above its
    average fill is a loss. Anything unparseable, or a side this repository
    does not recognise, answers ``False`` -- never the trigger for a
    behaviour change, because this function is only ever consulted to
    *override* the message's number, and "cannot tell" must stay closer to
    "do nothing" than to "override".
    """

    try:
        stop = Decimal(str(stop_price))
        avg = Decimal(str(avg_entry_price))
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return False
    if not (stop.is_finite() and avg.is_finite()):
        return False
    normalized_side = str(side or "").strip().lower()
    if normalized_side == "long":
        return stop < avg
    if normalized_side == "short":
        return stop > avg
    return False
