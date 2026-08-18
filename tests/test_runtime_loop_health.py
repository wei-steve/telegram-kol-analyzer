from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import logging

from telegram_kol_research.runtime_loop_health import LoopLagMonitor


class FakeClock:
    """Deterministic monotonic clock; tests advance it explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def _monitor(clock: FakeClock, **kwargs) -> LoopLagMonitor:
    base = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    return LoopLagMonitor(
        monotonic=clock,
        now_provider=lambda: base + timedelta(seconds=clock.value),
        **kwargs,
    )


def test_empty_snapshot_is_well_formed():
    snapshot = _monitor(FakeClock()).snapshot()

    assert snapshot["samples"] == 0
    assert snapshot["max_ms"] == 0.0
    assert snapshot["p50_ms"] == 0.0
    assert snapshot["p95_ms"] == 0.0
    assert snapshot["p99_ms"] == 0.0
    assert snapshot["stall_count"] == 0
    assert snapshot["last_stall_at"] is None
    assert snapshot["worst_stall_ms"] == 0.0
    assert snapshot["window_seconds"] == 0.0


def test_run_records_sleep_overshoot_as_lag_without_sleeping():
    clock = FakeClock()
    overshoots = iter([0.0, 0.25, 1.5])

    async def fake_sleeper(seconds):
        clock.advance(seconds)
        try:
            clock.advance(next(overshoots))
        except StopIteration:
            raise asyncio.CancelledError from None

    monitor = _monitor(clock, interval_seconds=0.5, sleeper=fake_sleeper)

    async def exercise():
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

    snapshot = monitor.snapshot()
    assert snapshot["samples"] == 3
    assert snapshot["max_ms"] == 1500.0
    assert snapshot["p50_ms"] == 250.0


def test_percentiles_use_nearest_rank_over_recorded_samples():
    monitor = _monitor(FakeClock())
    for value in range(1, 101):
        monitor.record_lag(float(value))

    snapshot = monitor.snapshot()
    assert snapshot["samples"] == 100
    assert snapshot["p50_ms"] == 50.0
    assert snapshot["p95_ms"] == 95.0
    assert snapshot["p99_ms"] == 99.0
    assert snapshot["max_ms"] == 100.0


def test_ring_buffer_evicts_oldest_samples_at_the_cap():
    monitor = _monitor(FakeClock(), max_samples=5)
    for value in range(1, 21):
        monitor.record_lag(float(value))

    snapshot = monitor.snapshot()
    assert snapshot["samples"] == 5
    # Only 16..20 survive, so the smallest retained sample is 16.
    assert snapshot["p50_ms"] == 18.0
    assert snapshot["max_ms"] == 20.0


def test_window_seconds_spans_the_retained_samples():
    clock = FakeClock()
    monitor = _monitor(clock, max_samples=3)
    for _ in range(6):
        monitor.record_lag(1.0)
        clock.advance(0.5)

    assert monitor.snapshot()["window_seconds"] == 1.0


def test_stalls_are_counted_with_last_and_worst_recorded():
    clock = FakeClock()
    monitor = _monitor(clock, stall_threshold_ms=3000.0)

    monitor.record_lag(2999.0)
    assert monitor.snapshot()["stall_count"] == 0

    monitor.record_lag(5000.0)
    clock.advance(120.0)
    monitor.record_lag(4000.0)

    snapshot = monitor.snapshot()
    assert snapshot["stall_count"] == 2
    assert snapshot["worst_stall_ms"] == 5000.0
    assert snapshot["last_stall_at"] == "2026-08-18T12:02:00+00:00"


def test_stall_warnings_are_rate_limited():
    # The application logger disables propagation, so capture at the source
    # rather than through caplog's root handler.
    records: list[logging.LogRecord] = []

    class CollectingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    clock = FakeClock()
    monitor = _monitor(
        clock,
        stall_threshold_ms=3000.0,
        stall_log_interval_seconds=60.0,
    )

    handler = CollectingHandler(level=logging.WARNING)
    module_logger = logging.getLogger(
        "telegram_kol_research.runtime_loop_health"
    )
    previous_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.WARNING)
    try:
        for _ in range(10):
            monitor.record_lag(4000.0)
            clock.advance(1.0)
        clock.advance(60.0)
        monitor.record_lag(4000.0)
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous_level)

    assert len(records) == 2
    assert "4000.0 ms" in records[0].getMessage()
    assert monitor.snapshot()["stall_count"] == 11


def test_uptime_seconds_measures_time_since_construction():
    clock = FakeClock()
    monitor = _monitor(clock)
    clock.advance(42.5)

    assert monitor.uptime_seconds() == 42.5
