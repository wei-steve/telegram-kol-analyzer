"""Whether this process's observation of the exchange is complete enough to
submit a new entry.

Phase 2 built ``ws_observation_permits_new_entry()`` and deliberately wired it
to nothing. Phase 5 wires it in, because from here on the entry -> position link
is the identity equation confirmed by a ``Position`` frame, and phase 4's
retest 12 established that **the Deepcoin private stream does not replay on
reconnect -- it only pushes changes.** Miss the frame and there is no second
chance and no REST substitute. So an entry submitted while the stream is
unsubscribed or has an unconverged gap would be an entry that can never be
verified.

The pause is "do not submit", never "submit and then cancel", and it always
carries a reason code: :class:`DeepcoinEntryAdmissionBlocked` names the exact
stream state that refused, so a paused entry is visible in the failure the
caller records rather than silently dropped.

**Scope is decided by the role, and the role is recorded, not guessed.** The
private WS stream runs in exactly one runtime role, and only that role executes
entries; ``web`` and ``ingest`` reach the exchange through
``worker_command_jobs``. The web application records its resolved role here at
startup (:func:`set_entry_admission_runtime_role`), so a worker process always
arrives at this gate having declared itself -- and a worker that declared itself
but never published an inbox is refused, not waved through. A process that never
records a role at all is not the production worker; it is a CLI tool, a
migration script or a test, and this gate is not the thing standing between it
and the exchange.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import update

from telegram_kol_research.deepcoin_ws_stream_state import (
    ws_observation_permits_new_entry,
)

logger = logging.getLogger(__name__)

# The defer reason one held entry carries while the stream is not vouching.
# Registered in ``instruction_execution_outcomes.VISIBILITY_DEFER_REASONS``, so
# the existing instruction-item defer path and A-3d's
# ``entry_admission_reconciler`` both recognise it without a second mechanism.
WS_OBSERVATION_DEFER_REASON = "ws_observation_pending"

# The runtime roles that run ``deepcoin_private_ws`` and therefore must hold a
# healthy stream before entering. Kept as a constant rather than a setting: this
# is not a knob, and phase 5 forbids a runtime mode switch.
ROLES_REQUIRING_WS_OBSERVATION = frozenset({"worker", "all"})

_LOCK = threading.RLock()
_runtime_role: str | None = None
_inbox_provider: Callable[[], Any] | None = None


class DeepcoinEntryAdmissionBlocked(RuntimeError):
    """A new entry was not submitted because the stream could not vouch for it.

    ``reason`` is the stream's own reason code -- ``disconnected``,
    ``connecting``, ``resyncing``, ``open_gap``, ``no_converged_resync``,
    ``gap_state_unknown``, ``unavailable`` -- or ``ws_inbox_unavailable`` when
    the role that owns the stream has not published an inbox at all.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"ws_observation_blocked_new_entry:{reason}")


def set_entry_admission_runtime_role(role: str | None) -> None:
    """Record the role this process resolved at startup."""

    global _runtime_role
    with _LOCK:
        _runtime_role = None if role is None else str(role).strip().lower() or None


def set_entry_admission_inbox_provider(provider: Callable[[], Any] | None) -> None:
    """Publish (or withdraw) the getter for this process's live WS inbox."""

    global _inbox_provider
    with _LOCK:
        _inbox_provider = provider


def entry_admission_runtime_role() -> str | None:
    with _LOCK:
        return _runtime_role


def ws_observation_admits_new_entry() -> tuple[bool, str]:
    """``(True, "")`` only when this process may submit a new entry now.

    Fail-closed within the role that owns the stream: a missing inbox, an
    unreadable gap count and every state but converged-``healthy`` all refuse.
    """

    with _LOCK:
        role = _runtime_role
        provider = _inbox_provider
    if role is None or role not in ROLES_REQUIRING_WS_OBSERVATION:
        return True, ""
    if provider is None:
        return False, "ws_inbox_unavailable"
    try:
        inbox = provider()
    except Exception:
        logger.exception("Deepcoin WS inbox provider failed during entry admission")
        return False, "ws_inbox_unavailable"
    if inbox is None:
        return False, "ws_inbox_unavailable"
    machine = getattr(inbox, "state_machine", None)
    open_gap_count = None
    counter = getattr(inbox, "_open_gap_count", None)
    if callable(counter):
        try:
            open_gap_count = counter()
        except Exception:
            logger.exception("Deepcoin WS open gap count failed during entry admission")
            open_gap_count = None
    return ws_observation_permits_new_entry(machine, open_gap_count=open_gap_count)


def require_ws_observation_permits_new_entry() -> None:
    """Raise :class:`DeepcoinEntryAdmissionBlocked` unless the stream vouches."""

    permitted, reason = ws_observation_admits_new_entry()
    if not permitted:
        raise DeepcoinEntryAdmissionBlocked(reason or "unavailable")


def ws_observation_entry_defer_result(
    session_factory,
    *,
    message_instruction_item_id: int | None,
    now: datetime,
) -> dict[str, Any] | None:
    """``None`` to proceed, or the defer result for one entry the stream cannot vouch for.

    Phase 5 refused such an entry outright, and the refusal was terminal: the
    item read ``failed`` and the intention was gone. Every ``tg-deploy``
    restart opens a gap of a few seconds, so an entry that arrived inside one
    died there. This returns the same refusal as a **deferral** instead --
    nothing is submitted either way, but the entry stays claimable and the
    reconciler retries it until the stream converges or the entry deadline
    passes.

    The gate itself is unchanged and still fail-closed: the two checkpoints in
    the writer (once before submitting, once inside the exchange-write gate)
    still refuse outright, and this only moves the *first* refusal earlier,
    before the execution contract has declared an imminent exchange write.

    Without an instruction item there is nothing durable to defer onto -- a CLI
    recovery submit, a revision replacement -- so this proceeds and leaves the
    refusal to those two checkpoints, exactly as before.
    """

    permitted, reason = ws_observation_admits_new_entry()
    if permitted:
        return None
    if message_instruction_item_id is None:
        return None
    _ensure_entry_admission_deadline(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        now=now,
    )
    return {
        "status": "deferred",
        "reason": WS_OBSERVATION_DEFER_REASON,
        "ws_observation_reason": str(reason or "unavailable"),
    }


def _ensure_entry_admission_deadline(
    session_factory,
    *,
    message_instruction_item_id: int,
    now: datetime,
) -> None:
    """Stamp the entry deadline once, never extend one the item already holds.

    An adjacent-context defer stamps this in ``_persist_attempt``; a stream gap
    reaches the item without ever building an attempt row, so the same deadline
    has to be stamped here or the reconciler would hold the entry forever. The
    ``IS NULL`` predicate is what keeps a repeated defer from sliding the
    deadline forward on every recheck.

    The execution contract gets the same deadline, because holding an entry
    creates a staleness this repository did not have before: a refusal used to
    be terminal within seconds. With the deadline on the contract,
    ``prepare_entry_submission_contract`` refuses a stale entry immediately
    before the writer no matter which recheck released it, so the deadline is
    enforced on the submit path itself and not only by the reconciler's timer.
    """

    from telegram_kol_research.entry_assembly_admission import (
        ENTRY_ADMISSION_EXECUTION_DEADLINE,
    )
    from telegram_kol_research.models import (
        InstructionExecutionContract,
        MessageInstructionItem,
    )

    deadline_at = now + ENTRY_ADMISSION_EXECUTION_DEADLINE
    with session_factory() as session:
        session.execute(
            update(MessageInstructionItem)
            .where(
                MessageInstructionItem.id == int(message_instruction_item_id),
                MessageInstructionItem.execution_deadline_at.is_(None),
            )
            .values(execution_deadline_at=deadline_at)
        )
        session.flush()
        item_deadline = session.query(
            MessageInstructionItem.execution_deadline_at
        ).filter(
            MessageInstructionItem.id == int(message_instruction_item_id)
        ).scalar()
        if item_deadline is not None:
            session.execute(
                update(InstructionExecutionContract)
                .where(
                    InstructionExecutionContract.message_instruction_item_id
                    == int(message_instruction_item_id),
                    InstructionExecutionContract.deadline_at.is_(None),
                )
                .values(deadline_at=item_deadline, updated_at=now)
            )
        session.commit()
