"""Let a Deepcoin private-stream frame wake the existing REST reconciliation.

Phase 3 of the REST+WebSocket program. It changes exactly one thing: **when**
the worker's ``deepcoin_reconcile`` loop runs. What that loop then does -- which
calls it makes, which criteria it applies, which ledgers it writes, what it
concludes -- is untouched, and a wake-driven run is byte-for-byte the same run a
timer would have produced. The value is the latency: production measured the
stream reporting a fill four to five seconds before the next REST poll would
have looked, and this closes the ``outcome_unknown`` window from "up to thirty
seconds" to "as soon as the frame lands".

Three properties this module exists to guarantee:

* **The timer is never removed or lengthened.** Waking is additive. With the
  stream down, disconnected, or simply silent, the loop keeps its original
  thirty-second cadence and behaves exactly as it did before phase 3.
* **A burst cannot become a REST flood.** A fill produces a rapid run of frames;
  waking on each one would issue dozens of reconciliations in a second and
  invite rate limiting. Wakes are debounced to one per
  :data:`WAKE_MIN_INTERVAL_SECONDS`, merged inside that interval, and hard
  capped at :data:`WAKE_MAX_PER_MINUTE`. Past the cap the loop falls back to
  pure polling and says so on the health endpoint.
* **The stream never writes anything.** A frame's entire authority here is to
  say "look now". Verification stays with REST, which is hard rule 5.

The primitive is an in-process :class:`asyncio.Event`. Both ends live in the
``worker`` process -- the stream reader and the reconcile loop are two singleton
tasks of the same event loop -- so this is not the cross-process lock that
``docs/ARCHITECTURE.md`` section 4.5 rules out. It protects nothing and
serialises nothing; it only carries a nudge between two tasks that already share
a loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from telegram_kol_research.deepcoin_ws_stream_state import WsApplyResult

# Two wake-driven reconciliations are never closer together than this. Frames
# arriving inside the interval do not queue up: they merge into the single wake
# waiting at the end of it. Two seconds is well under the thirty-second timer it
# rides alongside, and well over the sub-second burst a single fill produces.
WAKE_MIN_INTERVAL_SECONDS = 2.0

# Hard ceiling on wake-driven runs per rolling minute. At the debounce interval
# above the natural ceiling is thirty, so twenty is a real limit rather than a
# restatement: reaching it means something is producing frames faster than a
# fill does, and the safe response is to stop adding REST load and let the timer
# carry the loop.
WAKE_MAX_PER_MINUTE = 20

TRIGGER_TIMER = "by_timer"
TRIGGER_WAKE = "by_wake"

# Which frames are worth waking for, per channel.
#
# ``Trade`` wakes on every frame: a fill is the event whose verification cannot
# wait, and there is no "unchanged fill" -- each one is a new fact.
#
# The other three wake only when the frame actually moved the field that would
# change a reconciliation's answer. Repeats and no-op refreshes are persisted
# like any other frame; they simply do not justify an extra REST round trip.
WAKE_RELEVANT_FIELDS: dict[str, frozenset[str] | None] = {
    "Trade": None,
    "Order": frozenset({"order_status"}),
    # ``TU`` moving from ``default`` to the real split posId is the moment a
    # protection order becomes attributable, so it counts alongside ``TS``.
    "TriggerOrder": frozenset({"trigger_status", "trade_unit_id"}),
    "Position": frozenset({"position_qty"}),
}

_HOUR_SECONDS = 3600.0
_MINUTE_SECONDS = 60.0


def wake_channel_for_result(result: WsApplyResult) -> str | None:
    """Return the channel that should wake a reconciliation, or ``None``.

    The decision uses only the tracker's own verdict on one row, which is phase
    2's already-maintained newest-known-state view. It issues no query: asking
    the database "did this change?" on every frame would put a read on the hot
    path to save a read on the cold one.

    A row that did not pass de-duplication and ordering never wakes anything. A
    duplicate carries no new information by definition, and a frame older than
    what is already known is, at best, news that already arrived -- the timer
    still covers whatever it might have implied.
    """

    if result.key is None or not result.applied:
        return None
    channel = result.key.channel
    if channel not in WAKE_RELEVANT_FIELDS:
        return None
    fields = WAKE_RELEVANT_FIELDS[channel]
    if fields is None:
        return channel
    return channel if result.changed_fields & fields else None


class DeepcoinReconcileWakeSignal:
    """The nudge between the stream reader and the reconcile loop.

    One instance per worker process, created before either task starts and
    passed to both. Nothing else may hold a reference: a second instance would
    silently wake nobody.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = WAKE_MIN_INTERVAL_SECONDS,
        max_wakes_per_minute: int = WAKE_MAX_PER_MINUTE,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_provider: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._min_interval_seconds = float(min_interval_seconds)
        self._max_wakes_per_minute = int(max_wakes_per_minute)
        self._now = now_provider
        self._monotonic = monotonic_provider
        self._pending_channel: str | None = None
        self._admitted: list[float] = []
        self._last_gate_at: float | None = None
        self._wake_history: list[datetime] = []
        self._throttled_history: list[datetime] = []
        self._run_history: list[tuple[datetime, str]] = []
        self._failure_history: list[dict[str, Any]] = []
        self.requests_seen = 0
        self.last_wake_at: datetime | None = None
        self.last_wake_channel: str | None = None
        self.last_request_at: datetime | None = None

    # ------------------------------------------------------------ producing

    def request(self, *, channel: str) -> None:
        """Ask for a reconciliation. Safe to call from a worker thread.

        The stream persists frames on a thread (frames run to two megabytes and
        parsing them on the loop would stall the socket's keepalive), so this
        hops back onto the loop rather than touching the event directly.
        Requesting is cheap and always allowed; whether it becomes a run is
        decided by :meth:`wait_for_next_run`, which is the only place the
        debounce and the cap live.
        """

        self.requests_seen += 1
        self._pending_channel = channel
        self.last_request_at = self._now()
        loop = self._loop
        if loop is None:
            # No task has ever waited, so there is no waiter to schedule and
            # setting the flag is just a boolean write. The first waiter sees it
            # immediately and binds the loop for every later request.
            self._event.set()
            return
        try:
            loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The loop is closing. A missed wake costs at most one timer period.
            pass

    # ------------------------------------------------------------ consuming

    async def wait_for_next_run(self, *, timeout: float) -> str:
        """Wait for the next reconciliation and report what triggered it.

        Returns :data:`TRIGGER_TIMER` or :data:`TRIGGER_WAKE`. The timer half is
        an absolute deadline, not a series of sleeps, so no amount of waking,
        debouncing or throttling can push the plain thirty-second cadence out.
        That is what makes "the timer is unchanged" a property of the code
        rather than a claim about it.
        """

        loop = asyncio.get_running_loop()
        self._loop = loop
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._event.clear()
                return TRIGGER_TIMER
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                self._event.clear()
                return TRIGGER_TIMER
            self._event.clear()

            gate_wait = self._debounce_wait()
            if gate_wait > 0:
                timer_remaining = deadline - loop.time()
                if gate_wait >= timer_remaining:
                    # The timer would fire first anyway. Wait it out and report
                    # it honestly as a timer run instead of relabelling it.
                    await asyncio.sleep(max(0.0, timer_remaining))
                    self._event.clear()
                    return TRIGGER_TIMER
                # Everything that arrives during this sleep merges into the one
                # wake at the end of it.
                await asyncio.sleep(gate_wait)
                self._event.clear()

            # The gate advances whether or not the wake is admitted, so a frame
            # storm cannot spin this loop faster than one pass per interval.
            self._last_gate_at = self._monotonic()
            if not self._admit():
                self._throttled_history.append(self._now())
                self._trim()
                continue
            now = self._now()
            self.last_wake_at = now
            self.last_wake_channel = self._pending_channel
            self._wake_history.append(now)
            self._trim()
            return TRIGGER_WAKE

    def _debounce_wait(self) -> float:
        if self._last_gate_at is None:
            return 0.0
        return (
            self._last_gate_at + self._min_interval_seconds - self._monotonic()
        )

    def _admit(self) -> bool:
        now = self._monotonic()
        self._admitted = [
            stamp for stamp in self._admitted if now - stamp < _MINUTE_SECONDS
        ]
        if len(self._admitted) >= self._max_wakes_per_minute:
            return False
        self._admitted.append(now)
        return True

    @property
    def wake_throttled(self) -> bool:
        """Is the per-minute cap currently reached?

        ``True`` means wake-driven runs are being refused right now and the loop
        is back on pure polling. It is a live reading, not a memory of one.
        """

        now = self._monotonic()
        return (
            sum(1 for stamp in self._admitted if now - stamp < _MINUTE_SECONDS)
            >= self._max_wakes_per_minute
        )

    # ----------------------------------------------------------- accounting

    def record_reconcile_run(self, trigger: str) -> None:
        self._run_history.append((self._now(), trigger))
        self._trim()

    def record_reconcile_failure(
        self,
        exc: BaseException,
        *,
        call: str,
        http_status: int | None = None,
    ) -> None:
        """Record one failed reconciliation attempt, attributably.

        Call name, HTTP status and exception type only. The response body is
        never recorded and neither is anything derived from credentials: phase
        2's open-ended observation left ``incomplete_rest_read`` unattributable
        precisely because it recorded a conclusion and no detail, and the fix is
        three fields, not a log of the payload.
        """

        self._failure_history.append(
            {
                "at": self._now().isoformat(),
                "call": call,
                "exception_type": type(exc).__name__,
                "http_status": http_status,
            }
        )
        if len(self._failure_history) > 256:
            del self._failure_history[:-128]

    def _trim(self) -> None:
        floor = self._now().timestamp() - _HOUR_SECONDS
        self._wake_history = [
            stamp for stamp in self._wake_history if stamp.timestamp() >= floor
        ]
        self._throttled_history = [
            stamp for stamp in self._throttled_history if stamp.timestamp() >= floor
        ]
        self._run_history = [
            entry for entry in self._run_history if entry[0].timestamp() >= floor
        ]

    def health_snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Counters and times only. Carries no payload content."""

        moment = now or self._now()
        floor = moment.timestamp() - _HOUR_SECONDS
        by_timer = sum(
            1
            for stamp, trigger in self._run_history
            if stamp.timestamp() >= floor and trigger == TRIGGER_TIMER
        )
        by_wake = sum(
            1
            for stamp, trigger in self._run_history
            if stamp.timestamp() >= floor and trigger == TRIGGER_WAKE
        )
        failures = [
            entry
            for entry in self._failure_history
            if datetime.fromisoformat(entry["at"]).timestamp() >= floor
        ]
        return {
            "wakes_last_hour": sum(
                1 for stamp in self._wake_history if stamp.timestamp() >= floor
            ),
            "wakes_throttled_last_hour": sum(
                1 for stamp in self._throttled_history if stamp.timestamp() >= floor
            ),
            "last_wake_at": (
                None if self.last_wake_at is None else self.last_wake_at.isoformat()
            ),
            "last_wake_channel": self.last_wake_channel,
            "wake_throttled": self.wake_throttled,
            "wake_requests_seen": self.requests_seen,
            "wake_min_interval_seconds": self._min_interval_seconds,
            "wake_max_per_minute": self._max_wakes_per_minute,
            "reconcile_runs_last_hour": {
                "by_timer": by_timer,
                "by_wake": by_wake,
                "total": by_timer + by_wake,
            },
            "reconcile_failures_last_hour": len(failures),
            "last_reconcile_failure": failures[-1] if failures else None,
        }
