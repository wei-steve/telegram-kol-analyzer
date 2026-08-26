from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import logging
import math
import sys
import threading
import time
import traceback
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_SAMPLES = 7200
DEFAULT_STALL_THRESHOLD_MS = 3000.0
DEFAULT_STALL_LOG_INTERVAL_SECONDS = 60.0
DEFAULT_WATCHDOG_POLL_SECONDS = 0.25
DEFAULT_MAX_CAPTURES = 5
DEFAULT_MAX_STACK_FRAMES = 25


@dataclass(frozen=True)
class LoopLagSample:
    """One observed difference between a requested and an actual sleep."""

    monotonic_at: float
    lag_ms: float


@dataclass(frozen=True)
class StallCapture:
    """One stack captured while the event loop was unable to check in."""

    at: str
    blocked_ms: float
    stack: tuple[str, ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "at": self.at,
            "blocked_ms": round(self.blocked_ms, 3),
            "stack": list(self.stack),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class LoopStallAttributor:
    """Capture what the event loop thread is executing while it is blocked.

    The loop cannot report on itself while it is blocked — that is the whole
    problem, and it is why the loop-health endpoint times out during a stall.
    So this observer is a plain OS thread. It watches for the loop to stop
    checking in and, once the gap crosses the threshold, reads that thread's
    frame out of ``sys._current_frames()`` and formats its stack.

    Pure observation: it never touches the database, the exchange, or any
    trading path, and it never interrupts the thread it is watching.
    """

    def __init__(
        self,
        *,
        stall_threshold_ms: float = DEFAULT_STALL_THRESHOLD_MS,
        poll_interval_seconds: float = DEFAULT_WATCHDOG_POLL_SECONDS,
        capture_interval_seconds: float = DEFAULT_STALL_LOG_INTERVAL_SECONDS,
        max_captures: int = DEFAULT_MAX_CAPTURES,
        max_stack_frames: int = DEFAULT_MAX_STACK_FRAMES,
        monotonic: Callable[[], float] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        frame_provider: Callable[[], dict[int, Any]] | None = None,
    ) -> None:
        self.stall_threshold_seconds = max(0.0, float(stall_threshold_ms)) / 1000.0
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.capture_interval_seconds = max(0.0, float(capture_interval_seconds))
        self.max_captures = max(1, int(max_captures))
        self.max_stack_frames = max(1, int(max_stack_frames))
        self._monotonic = monotonic or time.perf_counter
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._frame_provider = frame_provider or sys._current_frames
        self._lock = threading.RLock()
        self._captures: deque[StallCapture] = deque(maxlen=self.max_captures)
        self._loop_thread_id: int | None = None
        self._last_checkin: float | None = None
        self._checkin_generation = 0
        self._captured_this_episode = False
        self._last_capture_monotonic: float | None = None
        self._capture_count = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def attach_loop_thread(self, thread_id: int) -> None:
        """Record which thread runs the event loop, and start the clock."""

        with self._lock:
            self._loop_thread_id = int(thread_id)
            self._last_checkin = self._monotonic()
            self._checkin_generation += 1
            self._captured_this_episode = False

    def note_checkin(self) -> None:
        """Called from the loop each iteration to prove it is still running."""

        with self._lock:
            self._last_checkin = self._monotonic()
            self._checkin_generation += 1
            self._captured_this_episode = False

    def poll_once(self) -> StallCapture | None:
        """Capture a stack if the loop has been silent past the threshold."""

        now = self._monotonic()
        with self._lock:
            last_checkin = self._last_checkin
            thread_id = self._loop_thread_id
            if last_checkin is None or thread_id is None:
                return None
            blocked_seconds = now - last_checkin
            if blocked_seconds < self.stall_threshold_seconds:
                return None
            if self._captured_this_episode:
                return None
            last_capture = self._last_capture_monotonic
            if (
                last_capture is not None
                and now - last_capture < self.capture_interval_seconds
            ):
                # Still rate limited, but this episode is spoken for: do not
                # spin capturing on every poll for the rest of it.
                self._captured_this_episode = True
                return None
            self._captured_this_episode = True
            self._last_capture_monotonic = now
            checkin_generation = self._checkin_generation
            stack, reason = self._format_loop_stack(thread_id)
            recovered_before_capture = (
                self._checkin_generation != checkin_generation
            )
            if recovered_before_capture:
                stack = ()
                reason = (
                    "event loop recovered before stack capture; "
                    "sampled stack discarded"
                )
            capture = StallCapture(
                at=self._now_provider().isoformat(),
                blocked_ms=blocked_seconds * 1000.0,
                stack=stack,
                reason=reason,
            )
            self._captures.append(capture)
            self._capture_count += 1
        logger.warning(
            "event loop stall stack after %.1f ms%s:\n%s",
            capture.blocked_ms,
            "" if reason is None else f" ({reason})",
            "\n".join(capture.stack) or "<no frames>",
        )
        return capture

    def _format_loop_stack(self, thread_id: int) -> tuple[tuple[str, ...], str | None]:
        try:
            frames = self._frame_provider()
        except Exception as exc:  # pragma: no cover - defensive
            return (), f"frame provider failed: {exc!r}"
        frame = frames.get(thread_id)
        if frame is None:
            return (), (
                "event loop thread not present in sys._current_frames; the lag "
                "is not this thread being busy"
            )
        try:
            formatted = traceback.format_stack(frame, limit=self.max_stack_frames)
        except Exception as exc:  # pragma: no cover - defensive
            return (), f"stack formatting failed: {exc!r}"
        lines = [line.rstrip("\n") for line in formatted]
        return tuple(lines), None

    def start(self) -> None:
        """Start the watchdog thread. Idempotent."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run_forever,
                name="loop-stall-watchdog",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - the watchdog must not die
                logger.exception("loop stall watchdog poll failed")
            self._stop.wait(self.poll_interval_seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            captures = list(self._captures)
            count = self._capture_count
            attached = self._loop_thread_id is not None
        return {
            "stall_captures": count,
            "watchdog_attached": attached,
            "recent_stall_stacks": [capture.as_dict() for capture in captures],
        }


class LoopLagMonitor:
    """Observe how long the asyncio event loop is unavailable to callbacks.

    The monitor sleeps a fixed interval in a loop and records the overshoot as
    a lag sample. It is pure observation: it never touches the database, the
    exchange, or any trading path, so it stays answerable while the loop is
    degraded.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        stall_threshold_ms: float = DEFAULT_STALL_THRESHOLD_MS,
        stall_log_interval_seconds: float = DEFAULT_STALL_LOG_INTERVAL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Any] | None = None,
        attributor: "LoopStallAttributor | None" = None,
    ) -> None:
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.max_samples = max(1, int(max_samples))
        self.stall_threshold_ms = max(0.0, float(stall_threshold_ms))
        self.stall_log_interval_seconds = max(
            0.0, float(stall_log_interval_seconds)
        )
        self._monotonic = monotonic or time.perf_counter
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or asyncio.sleep
        self._lock = threading.Lock()
        self._samples: deque[LoopLagSample] = deque(maxlen=self.max_samples)
        self._stall_count = 0
        self._last_stall_at: datetime | None = None
        self._worst_stall_ms = 0.0
        self._last_stall_log_monotonic: float | None = None
        self._started_monotonic = self._monotonic()
        self.attributor = (
            attributor
            if attributor is not None
            else LoopStallAttributor(
                stall_threshold_ms=self.stall_threshold_ms,
                capture_interval_seconds=self.stall_log_interval_seconds,
                monotonic=self._monotonic,
                now_provider=self._now_provider,
            )
        )

    async def run(self) -> None:
        """Sample loop lag forever; cancellation is owned by the caller."""

        self.attributor.attach_loop_thread(threading.get_ident())
        self.attributor.start()
        try:
            while True:
                started = self._monotonic()
                await self._sleeper(self.interval_seconds)
                elapsed = self._monotonic() - started
                self.attributor.note_checkin()
                self.record_lag(
                    max(0.0, elapsed - self.interval_seconds) * 1000.0
                )
        finally:
            self.attributor.stop()

    def record_lag(self, lag_ms: float) -> None:
        """Record one lag sample and account for a stall if it crossed."""

        observed_ms = max(0.0, float(lag_ms))
        observed_at = self._monotonic()
        stalled = observed_ms >= self.stall_threshold_ms
        should_log = False
        with self._lock:
            self._samples.append(
                LoopLagSample(monotonic_at=observed_at, lag_ms=observed_ms)
            )
            if stalled:
                self._stall_count += 1
                self._last_stall_at = self._now_provider()
                self._worst_stall_ms = max(self._worst_stall_ms, observed_ms)
                last_log = self._last_stall_log_monotonic
                if (
                    last_log is None
                    or observed_at - last_log >= self.stall_log_interval_seconds
                ):
                    self._last_stall_log_monotonic = observed_at
                    should_log = True
        if should_log:
            logger.warning(
                "event loop stalled for %.1f ms (threshold %.1f ms)",
                observed_ms,
                self.stall_threshold_ms,
            )

    def uptime_seconds(self) -> float:
        return round(max(0.0, self._monotonic() - self._started_monotonic), 3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            stall_count = self._stall_count
            last_stall_at = self._last_stall_at
            worst_stall_ms = self._worst_stall_ms
        lags = sorted(sample.lag_ms for sample in samples)
        window_seconds = (
            round(samples[-1].monotonic_at - samples[0].monotonic_at, 3)
            if len(samples) >= 2
            else 0.0
        )
        return {
            "samples": len(lags),
            "max_ms": round(lags[-1], 3) if lags else 0.0,
            "p50_ms": _percentile(lags, 0.50),
            "p95_ms": _percentile(lags, 0.95),
            "p99_ms": _percentile(lags, 0.99),
            "stall_count": stall_count,
            "last_stall_at": (
                last_stall_at.isoformat() if last_stall_at is not None else None
            ),
            "worst_stall_ms": round(worst_stall_ms, 3),
            "window_seconds": window_seconds,
            "interval_seconds": self.interval_seconds,
            "stall_threshold_ms": self.stall_threshold_ms,
            "max_samples": self.max_samples,
            **self.attributor.snapshot(),
        }


def _percentile(sorted_lags: list[float], quantile: float) -> float:
    if not sorted_lags:
        return 0.0
    rank = math.ceil(quantile * len(sorted_lags))
    index = min(len(sorted_lags) - 1, max(0, rank - 1))
    return round(sorted_lags[index], 3)
