from datetime import datetime, timedelta, timezone

from telegram_kol_research.backfill import compute_backfill_start


def test_compute_backfill_start_defaults_to_90_days():
    now = datetime(2026, 4, 7, tzinfo=timezone.utc)
    start = compute_backfill_start(now=now, days=90)
    assert start == now - timedelta(days=90)
