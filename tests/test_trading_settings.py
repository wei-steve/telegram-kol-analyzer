from decimal import Decimal
import json

import pytest

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.group_config import TrackedSenderConfig
from telegram_kol_research.trading_settings import SymbolEntryThresholds
from telegram_kol_research.trading_settings import apply_trading_settings_to_group_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trading_settings import TradingSettings
from telegram_kol_research.trading_settings import trading_settings_from_payload
from telegram_kol_research.models import TradingSetting


def test_load_trading_settings_returns_safe_defaults(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.auto_trade_enabled is False
    assert settings.default_max_loss_usdt == 20.0
    assert settings.max_concurrent_positions == 4
    assert settings.allowed_symbols == ["BTC", "ETH"]
    assert settings.entry_thresholds_for_symbol("BTC") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("200"),
        first_limit_offset=Decimal("90"),
        second_limit_offset=Decimal("90"),
    )
    assert settings.entry_thresholds_for_symbol("ETH") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("4"),
        first_limit_offset=Decimal("2"),
        second_limit_offset=Decimal("2"),
    )
    assert settings.entry_range_order_style == "eager"
    assert settings.nearby_entry_market_deviation_pct == 0.15
    assert settings.take_profit_allocations == [40.0, 30.0, 30.0]
    assert settings.allow_vision_auto_trade is True
    assert settings.context_resolution_enabled is False
    assert settings.context_resolution_live_chat_ids == []
    assert settings.context_resolution_enabled_for_chat(100) is False
    assert settings.entry_preamble_mode == "disabled"
    assert settings.entry_message_assembly_v2_mode == "disabled"
    assert settings.entry_revision_v2_mode == "disabled"
    assert not hasattr(settings, "entry_preamble_live_chat_ids")


def test_entry_preamble_rollout_settings_ignore_legacy_allowlist(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "entry_preamble_mode": " LIVE ",
            "entry_preamble_live_chat_ids": [-1002, -1001, -1002],
        },
    )

    assert saved.entry_preamble_mode == "live"
    assert not hasattr(saved, "entry_preamble_live_chat_ids")
    assert load_trading_settings(session_factory).entry_preamble_mode == "live"


@pytest.mark.parametrize("value", ["unsafe", True, [], {}, 1, None])
def test_entry_preamble_mode_fails_closed(value):
    with pytest.raises(ValueError, match="entry_preamble_mode"):
        trading_settings_from_payload({"entry_preamble_mode": value})


@pytest.mark.parametrize(
    "field_name",
    ["entry_message_assembly_v2_mode", "entry_revision_v2_mode"],
)
def test_adjacent_entry_modes_round_trip_live(tmp_path, field_name):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(session_factory, {field_name: " LIVE "})

    assert getattr(saved, field_name) == "live"
    assert getattr(load_trading_settings(session_factory), field_name) == "live"


@pytest.mark.parametrize(
    "field_name",
    ["entry_message_assembly_v2_mode", "entry_revision_v2_mode"],
)
@pytest.mark.parametrize("value", ["unsafe", True, [], {}, 1, None])
def test_adjacent_entry_modes_fail_closed(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        trading_settings_from_payload({field_name: value})


@pytest.mark.parametrize("value", ["-1001", [0], [-1001, "-1002"], {}])
def test_entry_preamble_legacy_allowlist_is_ignored(value):
    settings = trading_settings_from_payload({"entry_preamble_live_chat_ids": value})

    assert not hasattr(settings, "entry_preamble_live_chat_ids")


def test_saving_settings_preserves_legacy_entry_preamble_allowlist_in_storage(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            TradingSetting(
                key="global",
                value_json=json.dumps(
                    {
                        "entry_preamble_mode": "disabled",
                        "entry_preamble_live_chat_ids": [-1002, -1001],
                    }
                ),
            )
        )
        session.commit()

    save_trading_settings(session_factory, {"auto_trade_enabled": False})

    with session_factory() as session:
        stored = json.loads(session.query(TradingSetting).filter_by(key="global").one().value_json)
    assert stored["entry_preamble_live_chat_ids"] == [-1002, -1001]


def test_saving_only_entry_preamble_mode_preserves_all_other_settings(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 77,
            "allowed_symbols": ["SOL"],
            "management_execution_mode": "live",
        },
    )

    updated = save_trading_settings(
        session_factory,
        {"entry_preamble_mode": "live"},
    )

    assert updated.entry_preamble_mode == "live"
    assert updated.auto_trade_enabled is True
    assert updated.default_max_loss_usdt == 77
    assert updated.allowed_symbols == ["SOL"]
    assert updated.management_execution_mode == "live"


def test_context_resolution_requires_live_trading_and_allowlisted_chat():
    settings = trading_settings_from_payload(
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "context_resolution_enabled": True,
            "context_resolution_live_chat_ids": [100, 200],
        }
    )

    assert settings.context_resolution_enabled_for_chat(100) is True
    assert settings.context_resolution_enabled_for_chat(300) is False

    disabled = trading_settings_from_payload(
        {
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
            "context_resolution_enabled": True,
            "context_resolution_live_chat_ids": [100],
        }
    )
    assert disabled.context_resolution_enabled_for_chat(100) is False


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_context_resolution_enabled_is_strict_boolean(value):
    with pytest.raises(ValueError, match="context_resolution_enabled"):
        trading_settings_from_payload({"context_resolution_enabled": value})


@pytest.mark.parametrize("value", ["100", [0], [100, "200"], [100, 100], {}])
def test_context_resolution_chat_allowlist_fails_closed(value):
    with pytest.raises(ValueError, match="context_resolution_live_chat_ids"):
        trading_settings_from_payload({"context_resolution_live_chat_ids": value})


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


def test_composite_management_v2_mode_defaults_disabled():
    settings = TradingSettings()

    assert settings.composite_management_v2_mode == "disabled"
    assert settings.effective_composite_management_v2_mode == "disabled"


def test_composite_management_v2_live_requires_both_existing_live_gates():
    global_off = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
        }
    )
    management_off = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "shadow",
        }
    )
    enabled = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert global_off.effective_composite_management_v2_mode == "disabled"
    assert management_off.effective_composite_management_v2_mode == "disabled"
    assert enabled.effective_composite_management_v2_mode == "live"


def test_composite_management_v2_shadow_never_enables_exchange_writes():
    settings = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "shadow",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert settings.effective_composite_management_v2_mode == "shadow"


@pytest.mark.parametrize("value", [True, False, "unsafe", [], {}, 1, None])
def test_composite_management_v2_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="composite_management_v2_mode"):
        trading_settings_from_payload({"composite_management_v2_mode": value})


def test_trigger_protection_stop_rescue_defaults_disabled_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "rescue-settings.db")

    defaults = load_trading_settings(session_factory)
    saved = save_trading_settings(
        session_factory,
        {
            "trigger_protection_stop_rescue_mode": " SHADOW ",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert defaults.trigger_protection_stop_rescue_mode == "disabled"
    assert defaults.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert saved.trigger_protection_stop_rescue_mode == "shadow"
    assert reloaded.trigger_protection_stop_rescue_mode == "shadow"
    assert reloaded.effective_trigger_protection_stop_rescue_mode == "shadow"


def test_position_management_liveness_v2_defaults_disabled_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "liveness-v2-settings.db")

    defaults = load_trading_settings(session_factory)
    saved = save_trading_settings(
        session_factory,
        {"position_management_liveness_v2_mode": " SHADOW "},
    )
    reloaded = load_trading_settings(session_factory)

    assert defaults.position_management_liveness_v2_mode == "disabled"
    assert defaults.effective_position_management_liveness_v2_mode == "disabled"
    assert saved.position_management_liveness_v2_mode == "shadow"
    assert reloaded.effective_position_management_liveness_v2_mode == "shadow"


def test_live_position_management_liveness_v2_requires_both_execution_gates():
    global_off = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": False,
        "management_execution_mode": "live",
    })
    management_off = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": True,
        "management_execution_mode": "shadow",
    })
    enabled = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
    })

    assert global_off.effective_position_management_liveness_v2_mode == "disabled"
    assert management_off.effective_position_management_liveness_v2_mode == "disabled"
    assert enabled.effective_position_management_liveness_v2_mode == "live"


@pytest.mark.parametrize("value", [True, False, "unsafe", [], {}, 1, None])
def test_position_management_liveness_v2_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="position_management_liveness_v2_mode"):
        trading_settings_from_payload({"position_management_liveness_v2_mode": value})


def test_live_trigger_protection_stop_rescue_requires_both_execution_gates():
    disabled_globally = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
        }
    )
    disabled_management = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "shadow",
        }
    )
    enabled = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert disabled_globally.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert disabled_management.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert enabled.effective_trigger_protection_stop_rescue_mode == "live"


@pytest.mark.parametrize("value", ["unsafe", [], {}, 1, None])
def test_trigger_protection_stop_rescue_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="trigger_protection_stop_rescue_mode"):
        trading_settings_from_payload(
            {"trigger_protection_stop_rescue_mode": value}
        )


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
            "symbol_entry_thresholds": {
                "btc": {
                    "market_leg_threshold": "200",
                    "first_limit_offset": "90",
                    "second_limit_offset": "90",
                },
                "doge": {
                    "market_leg_threshold": "0.01",
                    "first_limit_offset": "0.002",
                    "second_limit_offset": "0.003",
                },
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
    assert reloaded.to_dict()["symbol_entry_thresholds"]["DOGE"] == {
        "market_leg_threshold": "0.01",
        "first_limit_offset": "0.002",
        "second_limit_offset": "0.003",
    }
    assert reloaded.max_loss_for_symbol("btc") == 20.0
    assert reloaded.max_loss_for_symbol("SOL") == 120.0
    assert reloaded.nearby_entry_market_deviation_pct == 1.2
    assert reloaded.take_profit_allocations == [50.0, 25.0, 25.0]
    assert reloaded.entry_range_order_style == "eager"
    assert reloaded.allow_vision_auto_trade is True


def test_legacy_settings_seed_initial_fixed_entry_thresholds(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "allowed_symbols": ["BTC", "ETH", "SOL"],
            "symbol_max_loss_usdt": {"BTC": 20, "ETH": 15, "SOL": 10},
        },
    )

    assert settings.entry_thresholds_for_symbol("BTC") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("200"),
        first_limit_offset=Decimal("90"),
        second_limit_offset=Decimal("90"),
    )
    assert settings.entry_thresholds_for_symbol("ETH") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("4"),
        first_limit_offset=Decimal("2"),
        second_limit_offset=Decimal("2"),
    )
    assert settings.entry_thresholds_for_symbol("SOL") == SymbolEntryThresholds.zero()


def test_legacy_save_preserves_persisted_fixed_entry_thresholds(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "250",
                    "first_limit_offset": "100",
                    "second_limit_offset": "95",
                },
                "DOGE": {
                    "market_leg_threshold": "0.01",
                    "first_limit_offset": "0.002",
                    "second_limit_offset": "0.003",
                },
            },
        },
    )

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": False,
            "max_market_entry_deviation_pct": "0.2",
            "entry_range_order_style": "conservative",
        },
    )

    assert saved.auto_trade_enabled is False
    assert saved.symbol_entry_thresholds == {
        "BTC": {
            "market_leg_threshold": "250",
            "first_limit_offset": "100",
            "second_limit_offset": "95",
        },
        "DOGE": {
            "market_leg_threshold": "0.01",
            "first_limit_offset": "0.002",
            "second_limit_offset": "0.003",
        },
    }
    assert load_trading_settings(session_factory).symbol_entry_thresholds == (
        saved.symbol_entry_thresholds
    )


def test_symbol_entry_thresholds_preserve_small_decimals(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "symbol_entry_thresholds": {
                "PEPE": {
                    "market_leg_threshold": "0.000003",
                    "first_limit_offset": "0.000001",
                    "second_limit_offset": "0.000002",
                }
            }
        },
    )

    assert settings.to_dict()["symbol_entry_thresholds"]["PEPE"] == {
        "market_leg_threshold": "0.000003",
        "first_limit_offset": "0.000001",
        "second_limit_offset": "0.000002",
    }


@pytest.mark.parametrize("invalid", ["-1", -0.1, "nan", "inf", {}, []])
def test_symbol_entry_thresholds_reject_invalid_values(invalid):
    with pytest.raises(ValueError):
        trading_settings_from_payload(
            {
                "symbol_entry_thresholds": {
                    "BTC": {
                        "market_leg_threshold": invalid,
                        "first_limit_offset": "0",
                        "second_limit_offset": "0",
                    }
                }
            }
        )


def test_symbol_entry_thresholds_reject_magnitude_above_finite_float_max():
    with pytest.raises(ValueError, match="market_leg_threshold"):
        trading_settings_from_payload(
            {
                "symbol_entry_thresholds": {
                    "BTC": {
                        "market_leg_threshold": "1e1000",
                        "first_limit_offset": "0",
                        "second_limit_offset": "0",
                    }
                }
            }
        )


@pytest.mark.parametrize("value, expected", [
    ("40,20,20,20", [40.0, 20.0, 20.0, 20.0]),
    ("40,15,15,15,15", [40.0, 15.0, 15.0, 15.0, 15.0]),
])
def test_trading_settings_preserves_four_and_five_stage_allocations(tmp_path, value, expected):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(session_factory, {"take_profit_allocations": value})

    assert saved.take_profit_allocations == expected
    assert load_trading_settings(session_factory).take_profit_allocations == expected


@pytest.mark.parametrize("value", ["", "40,0,30", "20,20,20,20,20,20"])
def test_trading_settings_rejects_invalid_take_profit_allocation_shape(value):
    with pytest.raises(ValueError, match="take_profit_allocations"):
        trading_settings_from_payload({"take_profit_allocations": value})


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
def test_source_deletion_exit_defaults_dormant_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    defaults = load_trading_settings(session_factory)
    assert defaults.telegram_source_deletion_exit_enabled is False

    saved = save_trading_settings(
        session_factory,
        {"telegram_source_deletion_exit_enabled": True},
    )

    assert saved.telegram_source_deletion_exit_enabled is True
    assert (
        load_trading_settings(session_factory).telegram_source_deletion_exit_enabled
        is True
    )
