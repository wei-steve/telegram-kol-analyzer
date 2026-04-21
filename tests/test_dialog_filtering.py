from telegram_kol_research.telegram_client import filter_target_dialogs


def test_filter_target_dialogs_keeps_only_enabled_group_titles():
    dialogs = [
        {"title": "VIP BTC Room", "archived": True},
        {"title": "Friends", "archived": False},
    ]
    titles = {"VIP BTC Room"}
    filtered = filter_target_dialogs(dialogs, titles)
    assert filtered == [{"title": "VIP BTC Room", "archived": True}]
