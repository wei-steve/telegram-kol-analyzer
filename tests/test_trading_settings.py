from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.group_config import TrackedSenderConfig
from telegram_kol_research.trading_settings import apply_trading_settings_to_group_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trading_settings import TradingSettings


def test_load_trading_settings_returns_safe_defaults(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.auto_trade_enabled is False
    assert settings.default_max_loss_usdt == 20.0
    assert settings.allowed_symbols == ["BTC", "ETH"]
    assert settings.entry_range_order_style == "eager"
    assert settings.nearby_entry_market_deviation_pct == 0.15
    assert settings.take_profit_allocations == [50.0, 30.0, 20.0]
    assert settings.allow_vision_auto_trade is True


def test_save_trading_settings_normalizes_user_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": "120",
            "allowed_symbols": "btc, eth, sol",
            "nearby_entry_market_deviation_pct": "1.2",
            "take_profit_allocations": "50,25,25",
            "entry_range_order_style": "eager",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert saved.auto_trade_enabled is True
    assert reloaded.default_max_loss_usdt == 120.0
    assert reloaded.allowed_symbols == ["BTC", "ETH", "SOL"]
    assert reloaded.nearby_entry_market_deviation_pct == 1.2
    assert reloaded.take_profit_allocations == [50.0, 25.0, 25.0]
    assert reloaded.entry_range_order_style == "eager"
    assert reloaded.allow_vision_auto_trade is True


def test_apply_trading_settings_to_group_config_preserves_sender_overrides():
    config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="vip",
                chat_id=100,
                trading_mode="auto_trade",
                max_loss_usdt=50,
                symbol_whitelist=["BTC"],
                tracked_senders=[
                    TrackedSenderConfig(
                        display_name="alice",
                        max_loss_usdt=25,
                    )
                ],
            )
        ]
    )

    runtime_config = apply_trading_settings_to_group_config(
        config,
        TradingSettings(default_max_loss_usdt=120, allowed_symbols=["SOL", "BTC"]),
    )

    assert runtime_config.groups[0].max_loss_usdt == 120.0
    assert runtime_config.groups[0].symbol_whitelist == ["SOL", "BTC"]
    assert runtime_config.groups[0].tracked_senders[0].max_loss_usdt == 25
