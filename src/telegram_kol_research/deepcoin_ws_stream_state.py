"""Connection state machine, ordering guard and backoff for the Deepcoin private stream.

Phase 2 of the REST+WebSocket program. This module is deliberately pure: it
holds no database session, performs no I/O, and knows nothing about SQLAlchemy.
Everything here is decided from values the caller passes in, which is what makes
the ordering and state rules testable offline against real recorded frames.

Three hard rules from ``docs/rest-ws-trading-status.md`` are enforced here:

* rule 4 -- ``disconnected`` and ``resyncing`` may only ever produce *unknown*.
  Nothing in this module returns an empty collection meaning "there is none";
  the only question it answers about coverage is
  :meth:`DeepcoinWsStreamStateMachine.permits_new_entry`, which fails closed.
* rule 6 -- frames repeat and arrive out of order. :class:`WsEntityStateTracker`
  keeps the newest state per entity and refuses to let an older frame overwrite
  it.
* rule 12 -- a restart loses frames exactly like a network drop does, so
  ``healthy`` is only reachable through ``resyncing``.

**There is no sequence number.** The Deepcoin private stream publishes no
continuous sequence and guarantees no replay after a disconnect (the public
market stream's ``ResumeNo`` does not apply to the private stream). Gap
detection is therefore "time watermark + REST resync" and nothing else. Do not
invent a sequence number here.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# The four state names of the handoff document's chain
# ``connecting -> healthy -> disconnected -> resyncing -> healthy``.
# Not one more, not one fewer.
WS_STATE_CONNECTING = "connecting"
WS_STATE_HEALTHY = "healthy"
WS_STATE_DISCONNECTED = "disconnected"
WS_STATE_RESYNCING = "resyncing"

WS_STATES = (
    WS_STATE_CONNECTING,
    WS_STATE_HEALTHY,
    WS_STATE_DISCONNECTED,
    WS_STATE_RESYNCING,
)

# Legal transitions. The handoff chain draws ``connecting -> healthy`` directly;
# this implementation routes that edge through ``resyncing`` instead, because
# hard rule 12 requires a process restart to re-align over REST before its
# observation may be called complete. That is a stricter path over the same four
# state names, never an extra state.
WS_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    WS_STATE_CONNECTING: frozenset({WS_STATE_RESYNCING, WS_STATE_DISCONNECTED}),
    WS_STATE_RESYNCING: frozenset({WS_STATE_HEALTHY, WS_STATE_DISCONNECTED}),
    WS_STATE_HEALTHY: frozenset({WS_STATE_DISCONNECTED}),
    WS_STATE_DISCONNECTED: frozenset({WS_STATE_CONNECTING}),
}

TRIGGER_ORDER_DEFAULT_UNIT = "default"

_HOUR_MS = 3_600_000


class DeepcoinWsStateTransitionError(RuntimeError):
    """Raised when code asks for a transition the chain does not contain."""


def compute_backoff_delay(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 60.0,
    jitter_ratio: float = 0.25,
    rng: Callable[[], float] = random.random,
) -> float:
    """Return the delay before reconnect attempt ``attempt`` (0-based).

    Exponential from ``base_seconds``, capped at ``cap_seconds``, with symmetric
    jitter so that a fleet of reconnecting clients does not retry in lockstep.
    The result never exceeds the cap and never drops below zero.
    """

    if attempt < 0:
        attempt = 0
    exponent = min(attempt, 32)
    raw = base_seconds * (2.0**exponent)
    if raw > cap_seconds:
        raw = cap_seconds
    spread = raw * jitter_ratio
    # rng() in [0, 1) -> factor in [-1, 1)
    delay = raw + spread * (2.0 * rng() - 1.0)
    if delay < 0.0:
        return 0.0
    if delay > cap_seconds:
        return cap_seconds
    return delay


@dataclass(frozen=True)
class WsEntityKey:
    """Identity of one observable exchange entity inside one channel."""

    channel: str
    identity: str


def entity_key_for_row(row: dict[str, Any]) -> WsEntityKey | None:
    """Return the ordering identity of one decoded inbox row, or ``None``.

    Identity per channel follows the phase 2 plan:

    ==============  ===========================================
    ``Order``       ``order_sys_id`` (short key ``OS``)
    ``Trade``       ``order_sys_id`` plus a per-fill identifier
    ``Position``    ``position_id`` (short key ``PI``)
    ``TriggerOrder````order_sys_id`` (short key ``OS``)
    ==============  ===========================================

    The per-fill short key is **not** documented and was not present in the
    recorded experiment, so it is not guessed: the frame's own ``payload_hash``
    stands in as the fill identifier. A fill is an append-only fact rather than
    a mutable state, so giving every distinct fill frame its own identity is the
    correct behaviour, not a workaround -- it means fills accumulate and never
    overwrite one another.
    """

    channel = str(row.get("channel") or "").strip()
    if not channel:
        return None
    order_sys_id = _clean(row.get("order_sys_id"))
    position_id = _clean(row.get("position_id"))
    if channel == "Position":
        return None if position_id is None else WsEntityKey(channel, position_id)
    if channel == "Trade":
        if order_sys_id is None:
            return None
        fill_id = _clean(row.get("payload_hash")) or ""
        return WsEntityKey(channel, f"{order_sys_id}:{fill_id}")
    if channel in {"Order", "TriggerOrder"}:
        return None if order_sys_id is None else WsEntityKey(channel, order_sys_id)
    return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class WsEntityState:
    """Newest known state of one entity, as observed from the stream."""

    key: WsEntityKey
    exchange_time_ms: int | None = None
    received_ms: int = 0
    trade_unit_id: str | None = None
    position_id: str | None = None
    instrument_raw: str | None = None
    last_event_id: int | None = None
    update_count: int = 0

    def ordering_tuple(self) -> tuple[int, int]:
        """Sort key: exchange time first, receive time as the tie-break."""

        exchange = -1 if self.exchange_time_ms is None else self.exchange_time_ms
        return (exchange, self.received_ms)


@dataclass
class WsApplyResult:
    """What :meth:`WsEntityStateTracker.apply` did with one row."""

    key: WsEntityKey | None
    applied: bool
    out_of_order: bool
    reason: str


class WsEntityStateTracker:
    """Newest-state-per-entity view of the stream. Older frames never win.

    Cross-channel ordering is never assumed. A ``Trade`` may arrive before its
    ``Order`` and a ``Position`` before either; nothing here requires one
    channel to have been seen before another is accepted.
    """

    def __init__(self, *, out_of_order_window_ms: int = _HOUR_MS) -> None:
        self._states: dict[WsEntityKey, WsEntityState] = {}
        self._out_of_order_window_ms = out_of_order_window_ms
        self._out_of_order_events: list[int] = []
        self.applied_count = 0
        self.out_of_order_count = 0
        self.unidentified_count = 0

    def state_for(self, key: WsEntityKey) -> WsEntityState | None:
        return self._states.get(key)

    def entity_count(self) -> int:
        return len(self._states)

    def out_of_order_count_since(self, floor_ms: int) -> int:
        return sum(1 for stamp in self._out_of_order_events if stamp >= floor_ms)

    def apply(self, row: dict[str, Any]) -> WsApplyResult:
        """Fold one decoded inbox row into the newest-state view.

        Returns without touching state when the row is older than what is
        already known. The one field that is *never* rolled back even by a
        newer-looking frame is ``trade_unit_id``: see :meth:`_merge_trade_unit`.
        """

        key = entity_key_for_row(row)
        if key is None:
            self.unidentified_count += 1
            return WsApplyResult(None, False, False, "unidentified")

        received_ms = int(row.get("received_ms") or 0)
        exchange_time_ms = row.get("exchange_time_ms")
        exchange_time_ms = (
            None if exchange_time_ms is None else int(exchange_time_ms)
        )
        incoming_order = (
            -1 if exchange_time_ms is None else exchange_time_ms,
            received_ms,
        )

        known = self._states.get(key)
        if known is None:
            state = WsEntityState(key=key)
            self._states[key] = state
            self._write(state, row, exchange_time_ms, received_ms)
            self.applied_count += 1
            return WsApplyResult(key, True, False, "first_observation")

        if incoming_order < known.ordering_tuple():
            self.out_of_order_count += 1
            self._record_out_of_order(received_ms)
            # The stale frame still contributes its one-way fields, which is how
            # a late frame can add information without rolling anything back.
            self._merge_one_way_fields(known, row)
            return WsApplyResult(key, False, True, "older_than_known_state")

        self._write(known, row, exchange_time_ms, received_ms)
        self.applied_count += 1
        return WsApplyResult(key, True, False, "advanced")

    def _record_out_of_order(self, received_ms: int) -> None:
        self._out_of_order_events.append(received_ms)
        floor_ms = received_ms - self._out_of_order_window_ms
        if len(self._out_of_order_events) > 4096:
            self._out_of_order_events = [
                stamp for stamp in self._out_of_order_events if stamp >= floor_ms
            ]

    def _write(
        self,
        state: WsEntityState,
        row: dict[str, Any],
        exchange_time_ms: int | None,
        received_ms: int,
    ) -> None:
        state.exchange_time_ms = exchange_time_ms
        state.received_ms = received_ms
        state.update_count += 1
        event_id = row.get("event_id")
        if event_id is not None:
            state.last_event_id = int(event_id)
        instrument = _clean(row.get("instrument_raw"))
        if instrument is not None:
            state.instrument_raw = instrument
        self._merge_one_way_fields(state, row)

    def _merge_one_way_fields(
        self, state: WsEntityState, row: dict[str, Any]
    ) -> None:
        state.trade_unit_id = self._merge_trade_unit(
            state.trade_unit_id, _clean(row.get("trade_unit_id"))
        )
        position_id = _clean(row.get("position_id"))
        if position_id is not None:
            state.position_id = position_id

    @staticmethod
    def _merge_trade_unit(known: str | None, incoming: str | None) -> str | None:
        """``TriggerOrder.TU`` moves ``default -> <posId>`` once and never back.

        The recorded experiment showed ``TU`` flipping from ``default`` to the
        real split ``posId`` while ``TS`` moved ``0 -> 1``. That direction is
        one-way. A late frame still carrying ``default`` -- even one whose
        timestamps look newer, because the two fields are not written
        atomically by the exchange -- must not restore ``default``. This is the
        single easiest thing in phase 2 to get wrong, which is why it is a
        separate function with its own test.
        """

        if incoming is None:
            return known
        if known is None:
            return incoming
        if known == incoming:
            return known
        if incoming == TRIGGER_ORDER_DEFAULT_UNIT:
            return known
        return incoming


@dataclass
class ResyncOutcome:
    """Result of one five-step REST resync."""

    converged: bool
    reason: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    step_durations_ms: dict[str, int] = field(default_factory=dict)
    replayed_events: int = 0
    rest_objects_before: int = 0
    rest_objects_after: int = 0
    advanced_entities: int = 0
    incomplete_reads: tuple[str, ...] = ()
    unresolved_instruments: tuple[str, ...] = ()


class DeepcoinWsStreamStateMachine:
    """The stream's four-state lifecycle, plus the counters the health endpoint shows.

    ``healthy`` is reachable **only** from ``resyncing`` and only when the
    resync converged. Every other situation -- including the very first
    ``connecting`` -- is treated as unknown coverage.
    """

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime],
        monotonic_ms_provider: Callable[[], int],
    ) -> None:
        self._now = now_provider
        self._now_ms = monotonic_ms_provider
        self.state = WS_STATE_CONNECTING
        self.state_since = self._now()
        self.state_since_ms = self._now_ms()
        self.reconnect_count = 0
        self.consecutive_failures = 0
        self.last_frame_at: datetime | None = None
        self.last_frame_ms: int | None = None
        self.last_resync_at: datetime | None = None
        self.last_resync_outcome: str | None = None
        self.last_resync_step_durations_ms: dict[str, int] = {}
        self.transitions: list[tuple[str, str, str]] = []

    def transition(self, new_state: str, *, reason: str) -> None:
        if new_state not in WS_STATES:
            raise DeepcoinWsStateTransitionError(f"unknown state {new_state!r}")
        allowed = WS_STATE_TRANSITIONS[self.state]
        if new_state == self.state:
            return
        if new_state not in allowed:
            raise DeepcoinWsStateTransitionError(
                f"illegal transition {self.state} -> {new_state}"
            )
        if new_state == WS_STATE_CONNECTING and self.state == WS_STATE_DISCONNECTED:
            self.reconnect_count += 1
        self.transitions.append((self.state, new_state, reason))
        if len(self.transitions) > 512:
            del self.transitions[:-256]
        self.state = new_state
        self.state_since = self._now()
        self.state_since_ms = self._now_ms()

    def mark_frame_received(self) -> None:
        self.last_frame_at = self._now()
        self.last_frame_ms = self._now_ms()

    def mark_connection_attempt_failed(self) -> None:
        self.consecutive_failures += 1

    def record_resync(self, outcome: ResyncOutcome) -> None:
        self.last_resync_at = outcome.finished_at or self._now()
        self.last_resync_outcome = (
            "converged" if outcome.converged else f"not_converged:{outcome.reason}"
        )
        self.last_resync_step_durations_ms = dict(outcome.step_durations_ms)
        if outcome.converged:
            self.consecutive_failures = 0

    def permits_new_entry(self, *, open_gap_count: int | None) -> tuple[bool, str]:
        """Fail-closed answer to "is my observation of the exchange complete?".

        Phase 2 only exposes this. Nothing calls it from the entry path yet --
        wiring it in is phase 5's job, and doing it here would change trading
        behaviour in a phase that is not allowed to.
        """

        if open_gap_count is None:
            return False, "gap_state_unknown"
        if self.state != WS_STATE_HEALTHY:
            return False, self.state
        if open_gap_count > 0:
            return False, "open_gap"
        if self.last_resync_outcome != "converged":
            return False, "no_converged_resync"
        return True, ""


def ws_observation_permits_new_entry(
    machine: DeepcoinWsStreamStateMachine | None,
    *,
    open_gap_count: int | None,
) -> tuple[bool, str]:
    """Module-level form of :meth:`DeepcoinWsStreamStateMachine.permits_new_entry`.

    ``(True, "")`` only when the stream is ``healthy`` with no unconverged gap.
    Everything else, ``connecting`` included, is ``(False, <reason>)``. With no
    state machine at all -- the stream never started, or this is not the worker
    role -- the answer is ``False``, never "probably fine".
    """

    if machine is None:
        return False, "unavailable"
    return machine.permits_new_entry(open_gap_count=open_gap_count)
