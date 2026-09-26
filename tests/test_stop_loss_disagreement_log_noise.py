"""The "verified stops disagree" warning is throttled, the decision is not.

``docs/plans/2026-09-26-server-disk-usage-analysis.md`` section 5.2: the planner
runs every reconcile round, and one unchanged disagreement produced about 660
identical ``WARNING`` lines an hour. These tests pin the throttle (first sight
and a changed count speak at once, an unchanged repeat at most hourly with the
number it stood for) and that the planner still skips the position every time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research import stop_loss_size_convergence as convergence
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.stop_loss_size_convergence import (
    DISAGREEMENT_REPORT_MIN_INTERVAL,
    plan_stop_loss_resizes,
    reset_stop_disagreement_report_throttle,
    should_report_stop_disagreement,
)
from tests.test_management_reliability_step5 import _binding_fixture


NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_throttle():
    reset_stop_disagreement_report_throttle()
    yield
    reset_stop_disagreement_report_throttle()


def test_interval_is_one_hour():
    assert DISAGREEMENT_REPORT_MIN_INTERVAL == timedelta(hours=1)


def test_first_sight_is_reported():
    assert should_report_stop_disagreement("pos-1", 2, moment=NOW) == (True, 0)


def test_unchanged_repeat_inside_the_interval_is_held_back():
    should_report_stop_disagreement("pos-1", 2, moment=NOW)

    assert should_report_stop_disagreement(
        "pos-1", 2, moment=NOW + timedelta(seconds=5)
    ) == (False, 1)
    assert should_report_stop_disagreement(
        "pos-1",
        2,
        moment=NOW + DISAGREEMENT_REPORT_MIN_INTERVAL - timedelta(seconds=1),
    ) == (False, 2)


def test_interval_expiry_reports_with_the_suppressed_count_and_resets_it():
    should_report_stop_disagreement("pos-1", 2, moment=NOW)
    for second in range(1, 4):
        should_report_stop_disagreement(
            "pos-1", 2, moment=NOW + timedelta(seconds=second)
        )

    later = NOW + DISAGREEMENT_REPORT_MIN_INTERVAL
    assert should_report_stop_disagreement("pos-1", 2, moment=later) == (True, 3)
    assert should_report_stop_disagreement(
        "pos-1", 2, moment=later + timedelta(seconds=1)
    ) == (False, 1)


def test_a_changed_count_is_reported_immediately():
    should_report_stop_disagreement("pos-1", 2, moment=NOW)
    should_report_stop_disagreement("pos-1", 2, moment=NOW + timedelta(seconds=1))

    assert should_report_stop_disagreement(
        "pos-1", 3, moment=NOW + timedelta(seconds=2)
    ) == (True, 0)
    # And the new count is now the one being throttled.
    assert should_report_stop_disagreement(
        "pos-1", 3, moment=NOW + timedelta(seconds=3)
    ) == (False, 1)


def test_positions_are_throttled_independently():
    should_report_stop_disagreement("pos-1", 2, moment=NOW)

    assert should_report_stop_disagreement(
        "pos-2", 2, moment=NOW + timedelta(seconds=1)
    ) == (True, 0)


def test_naive_moment_is_read_as_utc():
    should_report_stop_disagreement("pos-1", 2, moment=NOW)

    assert should_report_stop_disagreement(
        "pos-1", 2, moment=(NOW + timedelta(minutes=5)).replace(tzinfo=None)
    ) == (False, 1)


def _add_second_verified_stop(session_factory):
    with session_factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                strategy_instance_id="strategy-222",
                pos_id="pos-222",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="stop-3",
                purpose="stop_loss",
                trigger_price="78000",
                size_text="8",
                status="verified",
                evidence_source="test",
            )
        )
        session.commit()


def test_planner_still_skips_every_round_but_logs_once(tmp_path, monkeypatch):
    session_factory = _binding_fixture(tmp_path)
    _add_second_verified_stop(session_factory)

    # Record through a stand-in logger rather than caplog: other tests in the
    # full suite reconfigure logging, and this must not depend on test order.
    lines: list[str] = []

    class _Recorder:
        def warning(self, message, *args):
            lines.append(message % args)

    monkeypatch.setattr(convergence, "logger", _Recorder())
    for _ in range(5):
        plans = plan_stop_loss_resizes(
            session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
        )
        assert plans == []

    assert lines == [
        "stop-loss resize skipped: 2 verified stops disagree pos_id=pos-222"
        " suppressed_repeats=0"
    ]
