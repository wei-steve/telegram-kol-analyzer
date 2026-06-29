from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import save_trading_settings


def test_load_trading_settings_returns_safe_defaults(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.auto_trade_enabled is False
    assert settings.default_max_loss_usdt == 100.0
    assert settings.allowed_symbols == ["BTC", "ETH"]
    assert settings.entry_range_order_style == "conservative"
    assert settings.take_profit_allocations == [50.0, 30.0, 20.0]


def test_save_trading_settings_normalizes_user_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": "120",
            "allowed_symbols": "btc, eth, sol",
            "take_profit_allocations": "50,25,25",
            "entry_range_order_style": "eager",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert saved.auto_trade_enabled is True
    assert reloaded.default_max_loss_usdt == 120.0
    assert reloaded.allowed_symbols == ["BTC", "ETH", "SOL"]
    assert reloaded.take_profit_allocations == [50.0, 25.0, 25.0]
    assert reloaded.entry_range_order_style == "eager"
