from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.telegram_client import TelegramAuthConfig


def test_sync_command_reports_archived_target_groups(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: FakeClient(),
    )

    async def fake_discover_dialogs(client):
        return [
            {"title": "VIP BTC Room", "archived": True},
            {"title": "Friends", "archived": False},
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    async def fake_fetch_dialog_messages(client, dialog, limit):
        return []

    monkeypatch.setattr(
        "telegram_kol_research.cli.fetch_dialog_messages",
        fake_fetch_dialog_messages,
    )

    result = CliRunner().invoke(
        app,
        ["sync", "--config-path", str(config_path), "--database-path", str(database_path)],
    )

    assert result.exit_code == 0
    assert "Discovered 1 archived target group" in result.stdout


def test_sync_discover_reports_enabled_groups_that_did_not_match_any_dialog(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[
                TargetGroupConfig(chat_title="VIP BTC Room", enabled=True),
                TargetGroupConfig(chat_title="Missing Room", enabled=True),
            ]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: FakeClient(),
    )

    async def fake_discover_dialogs(client):
        return [
            {"title": "VIP BTC Room", "archived": True},
            {"title": "Friends", "archived": False},
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)

    async def fail_fetch_dialog_messages(client, dialog, limit):
        raise AssertionError("discover mode should not fetch messages")

    monkeypatch.setattr(
        "telegram_kol_research.cli.fetch_dialog_messages",
        fail_fetch_dialog_messages,
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--mode",
            "discover",
        ],
    )

    assert result.exit_code == 0
    assert "Configured groups not currently matched:" in result.stdout
    assert "- Missing Room" in result.stdout
