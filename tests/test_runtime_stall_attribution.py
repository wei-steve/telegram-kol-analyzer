"""The watchdog that captures the event loop's stack while it is blocked.

Every test drives the clock and the frame table by injection, so nothing here
sleeps and nothing depends on producing a real stall.
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime

from telegram_kol_research.runtime_loop_health import (
    LoopLagMonitor,
    LoopStallAttributor,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
LOOP_THREAD_ID = 4242


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _a_frame():
    """A real frame object, so traceback.format_stack has something to format."""

    return sys._getframe()


def _attributor(clock, *, frames=None, **kwargs):
    return LoopStallAttributor(
        stall_threshold_ms=kwargs.pop("stall_threshold_ms", 3000.0),
        capture_interval_seconds=kwargs.pop("capture_interval_seconds", 60.0),
        monotonic=clock,
        now_provider=lambda: NOW,
        frame_provider=lambda: (
            frames if frames is not None else {LOOP_THREAD_ID: _a_frame()}
        ),
        **kwargs,
    )


def test_no_capture_before_a_thread_is_attached():
    clock = FakeClock()
    attributor = _attributor(clock)

    clock.advance(60.0)

    assert attributor.poll_once() is None
    assert attributor.snapshot()["watchdog_attached"] is False


def test_no_capture_while_the_loop_keeps_checking_in():
    clock = FakeClock()
    attributor = _attributor(clock)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    for _ in range(20):
        clock.advance(0.5)
        attributor.note_checkin()
        assert attributor.poll_once() is None

    assert attributor.snapshot()["stall_captures"] == 0


def test_capture_once_the_gap_crosses_the_threshold():
    clock = FakeClock()
    attributor = _attributor(clock)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(2.9)
    assert attributor.poll_once() is None

    clock.advance(0.2)
    capture = attributor.poll_once()

    assert capture is not None
    assert capture.reason is None
    assert capture.blocked_ms == 3100.0
    assert capture.at == NOW.isoformat()
    assert capture.stack
    assert any("test_runtime_stall_attribution.py" in line for line in capture.stack)


def test_only_one_capture_per_stall_episode():
    clock = FakeClock()
    attributor = _attributor(clock)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(4.0)
    assert attributor.poll_once() is not None
    for _ in range(10):
        clock.advance(0.25)
        assert attributor.poll_once() is None

    assert attributor.snapshot()["stall_captures"] == 1


def test_rate_limit_holds_across_episodes():
    clock = FakeClock()
    attributor = _attributor(clock, capture_interval_seconds=60.0)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(4.0)
    assert attributor.poll_once() is not None

    # A second episode inside the rate-limit window is skipped.
    attributor.note_checkin()
    clock.advance(4.0)
    assert attributor.poll_once() is None

    # Once the window passes, the next episode captures again.
    attributor.note_checkin()
    clock.advance(61.0)
    assert attributor.poll_once() is not None
    assert attributor.snapshot()["stall_captures"] == 2


def test_a_missing_loop_thread_is_recorded_as_a_reason_not_an_exception():
    clock = FakeClock()
    attributor = _attributor(clock, frames={})
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(5.0)
    capture = attributor.poll_once()

    assert capture is not None
    assert capture.stack == ()
    assert "not present in sys._current_frames" in (capture.reason or "")


def test_a_failing_frame_provider_is_recorded_as_a_reason():
    clock = FakeClock()

    def explode():
        raise RuntimeError("frames unavailable")

    attributor = LoopStallAttributor(
        stall_threshold_ms=3000.0,
        monotonic=clock,
        now_provider=lambda: NOW,
        frame_provider=explode,
    )
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(5.0)
    capture = attributor.poll_once()

    assert capture is not None
    assert "frame provider failed" in (capture.reason or "")


def test_captures_are_bounded_and_exposed_in_the_snapshot():
    clock = FakeClock()
    attributor = _attributor(clock, capture_interval_seconds=0.0, max_captures=3)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    for _ in range(5):
        attributor.note_checkin()
        clock.advance(4.0)
        assert attributor.poll_once() is not None

    snapshot = attributor.snapshot()

    assert snapshot["stall_captures"] == 5
    assert len(snapshot["recent_stall_stacks"]) == 3
    entry = snapshot["recent_stall_stacks"][0]
    assert set(entry) >= {"at", "blocked_ms", "stack"}


def test_stack_depth_is_bounded():
    clock = FakeClock()
    attributor = _attributor(clock, max_stack_frames=2)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    clock.advance(5.0)
    capture = attributor.poll_once()

    assert capture is not None
    assert 0 < len(capture.stack) <= 2


def test_watchdog_thread_is_a_daemon_and_start_is_idempotent():
    clock = FakeClock()
    attributor = _attributor(clock)
    attributor.attach_loop_thread(LOOP_THREAD_ID)

    attributor.start()
    first = attributor._thread
    attributor.start()

    try:
        assert first is attributor._thread
        assert first is not None and first.daemon is True
        assert first.name == "loop-stall-watchdog"
    finally:
        attributor.stop()
        first.join(timeout=5.0)

    assert not first.is_alive()


def test_monitor_attaches_its_own_thread_and_checks_in_each_iteration():
    import asyncio

    clock = FakeClock()
    attributor = _attributor(clock)
    monitor = LoopLagMonitor(
        interval_seconds=0.01,
        monotonic=clock,
        now_provider=lambda: NOW,
        attributor=attributor,
    )
    checkins: list[int] = []
    original = attributor.note_checkin

    def counting_checkin() -> None:
        checkins.append(1)
        original()

    attributor.note_checkin = counting_checkin  # type: ignore[method-assign]

    async def scenario():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(checkins) >= 2
    assert attributor.snapshot()["watchdog_attached"] is True
    assert attributor._loop_thread_id is not None


def test_monitor_snapshot_carries_the_attribution_keys():
    monitor = LoopLagMonitor(now_provider=lambda: NOW)
    monitor.record_lag(4500.0)

    snapshot = monitor.snapshot()

    for key in ("stall_captures", "watchdog_attached", "recent_stall_stacks"):
        assert key in snapshot
    assert snapshot["stall_count"] == 1
