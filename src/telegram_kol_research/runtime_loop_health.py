from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import logging
import math
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_SAMPLES = 7200
DEFAULT_STALL_THRESHOLD_MS = 3000.0
DEFAULT_STALL_LOG_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class LoopLagSample:
    """One observed difference between a requested and an actual sleep."""

    monotonic_at: float
    lag_ms: float


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

    async def run(self) -> None:
        """Sample loop lag forever; cancellation is owned by the caller."""

        while True:
            started = self._monotonic()
            await self._sleeper(self.interval_seconds)
            elapsed = self._monotonic() - started
            self.record_lag(max(0.0, elapsed - self.interval_seconds) * 1000.0)

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
        }


def _percentile(sorted_lags: list[float], quantile: float) -> float:
    if not sorted_lags:
        return 0.0
    rank = math.ceil(quantile * len(sorted_lags))
    index = min(len(sorted_lags) - 1, max(0, rank - 1))
    return round(sorted_lags[index], 3)
