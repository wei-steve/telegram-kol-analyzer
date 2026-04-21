from datetime import UTC, datetime

from telegram_kol_research.reconcile import build_reconcile_window


def test_build_reconcile_window_replays_small_safety_window_after_checkpoint():
    start_at, end_at = build_reconcile_window(
        checkpoint_message_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        now=datetime(2026, 4, 17, 9, 0, tzinfo=UTC),
        safety_minutes=15,
    )

    assert start_at.isoformat().startswith("2026-04-17T07:45")
    assert end_at.isoformat().startswith("2026-04-17T09:00")
