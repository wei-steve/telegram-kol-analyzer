from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telegram_kol_research.time_utils import (
    normalize_to_utc_naive,
    parse_local_datetime_to_utc_naive,
    utc_naive_to_local,
)


def test_normalize_to_utc_naive_converts_aware_shanghai_datetime():
    value = datetime(2026, 6, 12, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert normalize_to_utc_naive(value) == datetime(2026, 6, 12, 8, 30)


def test_normalize_to_utc_naive_keeps_aware_utc_wall_clock_as_naive_utc():
    value = datetime(2026, 6, 12, 8, 30, tzinfo=UTC)

    assert normalize_to_utc_naive(value) == datetime(2026, 6, 12, 8, 30)


def test_parse_local_datetime_to_utc_naive_treats_naive_form_input_as_shanghai_time():
    value = parse_local_datetime_to_utc_naive("2026-06-12T16:30")

    assert value == datetime(2026, 6, 12, 8, 30)


def test_utc_naive_to_local_converts_storage_time_for_display():
    value = utc_naive_to_local(datetime(2026, 6, 12, 8, 30))

    assert value == datetime(2026, 6, 12, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
