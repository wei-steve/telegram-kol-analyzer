import pytest

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.group_config import TrackedSenderConfig
from telegram_kol_research.trading_settings import apply_trading_settings_to_group_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trading_settings import TradingSettings
from telegram_kol_research.trading_settings import trading_settings_from_payload


def test_load_trading_settings_returns_safe_defaults(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.auto_trade_enabled is False
    assert settings.default_max_loss_usdt == 20.0
    assert settings.max_concurrent_positions == 4
    assert settings.allowed_symbols == ["BTC", "ETH"]
    assert settings.entry_range_order_style == "eager"
    assert settings.nearby_entry_market_deviation_pct == 0.15
    assert settings.take_profit_allocations == [40.0, 30.0, 30.0]
    assert settings.allow_vision_auto_trade is True


def test_management_execution_mode_defaults_disabled_and_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.management_execution_mode == "disabled"
    assert settings.management_planning_enabled is False
    assert settings.live_management_execution_enabled is False


def test_management_execution_mode_shadow_plans_without_global_auto_trade():
    settings = trading_settings_from_payload(
        {
            "management_execution_mode": "shadow",
            "auto_trade_enabled": False,
        }
    )

    assert settings.management_planning_enabled is True
    assert settings.live_management_execution_enabled is False


def test_management_execution_mode_live_requires_global_auto_trade():
    gated = trading_settings_from_payload(
        {
            "management_execution_mode": "live",
            "auto_trade_enabled": False,
        }
    )
    enabled = trading_settings_from_payload(
        {
            "management_execution_mode": "live",
            "auto_trade_enabled": True,
        }
    )

    assert gated.management_planning_enabled is False
    assert gated.live_management_execution_enabled is False
    assert enabled.management_planning_enabled is True
    assert enabled.live_management_execution_enabled is True


def test_management_execution_mode_rejects_invalid_value():
    with pytest.raises(ValueError, match="management_execution_mode"):
        trading_settings_from_payload({"management_execution_mode": "unsafe"})


@pytest.mark.parametrize("value", ["false", "0", 0, 1])
def test_auto_trade_enabled_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="auto_trade_enabled"):
        trading_settings_from_payload(
            {
                "management_execution_mode": "live",
                "auto_trade_enabled": value,
            }
        )


@pytest.mark.parametrize("field", [
    "move_stop_to_breakeven_after_tp1",
    "allow_vision_auto_trade",
])
def test_trading_settings_boolean_fields_use_strict_validation(field):
    with pytest.raises(ValueError, match=field):
        trading_settings_from_payload({field: "false"})


@pytest.mark.parametrize("value", [[], {}, 1, None])
def test_management_execution_mode_rejects_non_string_values(value):
    with pytest.raises(ValueError, match="management_execution_mode"):
        trading_settings_from_payload({"management_execution_mode": value})


def test_management_execution_mode_normalizes_allowed_string():
    settings = trading_settings_from_payload(
        {"management_execution_mode": " LIVE ", "auto_trade_enabled": True}
    )

    assert settings.management_execution_mode == "live"
    assert settings.live_management_execution_enabled is True


def test_save_trading_settings_normalizes_user_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": "120",
            "allowed_symbols": "btc, eth, sol",
            "symbol_max_loss_usdt": {
                "btc": "20",
                "ETH": "15.5",
                "bad": "0",
                "empty": "",
            },
            "nearby_entry_market_deviation_pct": "1.2",
            "take_profit_allocations": "50,25,25",
            "entry_range_order_style": "eager",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert saved.auto_trade_enabled is True
    assert reloaded.default_max_loss_usdt == 120.0
    assert reloaded.allowed_symbols == ["BTC", "ETH", "SOL"]
    assert reloaded.symbol_max_loss_usdt == {"BTC": 20.0, "ETH": 15.5}
    assert reloaded.max_loss_for_symbol("btc") == 20.0
    assert reloaded.max_loss_for_symbol("SOL") == 120.0
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
        TradingSettings(
            default_max_loss_usdt=120,
            allowed_symbols=["SOL", "BTC"],
            symbol_max_loss_usdt={"SOL": 10.0},
        ),
    )

    assert runtime_config.groups[0].max_loss_usdt == 120.0
    assert runtime_config.groups[0].symbol_whitelist == ["SOL", "BTC"]
    assert runtime_config.groups[0].symbol_max_loss_usdt == {"SOL": 10.0}
    assert runtime_config.groups[0].tracked_senders[0].max_loss_usdt == 25
