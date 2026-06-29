from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.group_config import update_group_automation_settings


def test_group_config_loads_target_groups(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    enabled: true\n"
        "    tracked_senders:\n"
        "      - display_name: Alice\n"
    )

    config = load_group_config(config_path)
    assert config.groups[0].chat_title == "VIP BTC Room"
    assert config.groups[0].tracked_senders[0].display_name == "Alice"


def test_group_config_defaults_to_notify_only_trading_mode(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    tracked_senders:\n"
        "      - display_name: Alice\n"
    )

    config = load_group_config(config_path)
    group = config.groups[0]
    sender = group.tracked_senders[0]
    assert group.trading_mode == "notify_only"
    assert group.ai_strategy_enabled is False
    assert group.max_loss_usdt == 20.0
    assert group.symbol_whitelist == ["BTC", "ETH"]
    assert sender.trading_mode == "notify_only"
    assert sender.max_loss_usdt is None
    assert sender.symbol_whitelist is None


def test_group_config_loads_explicit_sender_auto_trade_settings(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    trading_mode: notify_only\n"
        "    max_loss_usdt: 50\n"
        "    symbol_whitelist: [BTC]\n"
        "    tracked_senders:\n"
        "      - display_name: Alice\n"
        "        trading_mode: auto_trade\n"
        "        max_loss_usdt: 25\n"
        "        symbol_whitelist: [ETH]\n"
    )

    config = load_group_config(config_path)
    group = config.groups[0]
    sender = group.tracked_senders[0]
    assert group.trading_mode == "notify_only"
    assert group.ai_strategy_enabled is False
    assert group.max_loss_usdt == 50.0
    assert group.symbol_whitelist == ["BTC"]
    assert sender.trading_mode == "auto_trade"
    assert sender.max_loss_usdt == 25.0
    assert sender.symbol_whitelist == ["ETH"]


def test_group_config_loads_optional_chat_id_for_runtime_matching(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    chat_id: 9001\n"
    )

    config = load_group_config(config_path)

    assert config.groups[0].chat_id == 9001


def test_group_config_loads_and_updates_group_automation_settings(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    chat_id: 9001\n"
        "    ai_strategy_enabled: false\n"
        "    trading_mode: notify_only\n",
        encoding="utf-8",
    )

    updated = update_group_automation_settings(
        config_path,
        chat_id=9001,
        ai_strategy_enabled=True,
        auto_trade_enabled=True,
    )
    group = updated.groups[0]
    reloaded = load_group_config(config_path).groups[0]

    assert group.ai_strategy_enabled is True
    assert group.trading_mode == "auto_trade"
    assert reloaded.ai_strategy_enabled is True
    assert reloaded.trading_mode == "auto_trade"


def test_group_config_adds_missing_group_when_toggling_from_web(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text("groups: []\n", encoding="utf-8")

    updated = update_group_automation_settings(
        config_path,
        chat_id=9002,
        chat_title="New Room",
        ai_strategy_enabled=True,
        auto_trade_enabled=False,
    )

    assert updated.groups[0].chat_title == "New Room"
    assert updated.groups[0].chat_id == 9002
    assert updated.groups[0].enabled is True
    assert updated.groups[0].ai_strategy_enabled is True
    assert updated.groups[0].trading_mode == "notify_only"
