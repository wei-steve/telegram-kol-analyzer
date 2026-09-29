"""``take_profit_adjust_mode``: default shadow, three values, nothing else."""

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import (
    TradingSettings,
    load_trading_settings,
    save_trading_settings,
    trading_settings_from_payload,
)


def test_default_is_shadow():
    assert TradingSettings().take_profit_adjust_mode == "shadow"
    assert trading_settings_from_payload({}).take_profit_adjust_mode == "shadow"


@pytest.mark.parametrize("mode", ["disabled", "shadow", "live", " LIVE "])
def test_modes_round_trip(tmp_path, mode):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"take_profit_adjust_mode": mode})

    assert load_trading_settings(session_factory).take_profit_adjust_mode == mode.strip().lower()


@pytest.mark.parametrize("value", ["on", "", None, 1])
def test_unknown_values_are_refused(value):
    with pytest.raises(ValueError):
        trading_settings_from_payload({"take_profit_adjust_mode": value})
