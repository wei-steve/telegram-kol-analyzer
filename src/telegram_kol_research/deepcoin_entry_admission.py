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
from typing import Any

from telegram_kol_research.deepcoin_ws_stream_state import (
    ws_observation_permits_new_entry,
)

logger = logging.getLogger(__name__)

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
