from telegram_kol_research.group_config import load_group_config


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
