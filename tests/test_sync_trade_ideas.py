from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import TradeIdea, TradeUpdate
from telegram_kol_research.telegram_client import TelegramAuthConfig


def test_sync_command_persists_trade_ideas_from_related_candidates(monkeypatch, tmp_path):
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
            {"id": 9001, "title": "VIP BTC Room", "archived": True},
        ]

    async def fake_fetch_dialog_messages(client, dialog, limit):
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "sender_name": "Alice Trader",
                "text": "BTC long 68000-68200, SL 67500, TP 69000 / 70000",
                "posted_at": "2026-04-07T00:00:00+00:00",
                "media": None,
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "sender_name": "Alice Trader",
                "text": "BTC long SL moved to 68050",
                "reply_to_msg_id": 77,
                "posted_at": "2026-04-07T01:00:00+00:00",
                "media": None,
            },
        ]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fake_fetch_dialog_messages)

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0

    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        trade_ideas = session.query(TradeIdea).all()
        trade_updates = session.query(TradeUpdate).all()

    assert len(trade_ideas) == 1
    assert trade_ideas[0].symbol == "BTC"
    assert trade_ideas[0].side == "long"
    assert trade_ideas[0].source_id is not None
    assert len(trade_updates) == 1
    assert trade_updates[0].update_type == "stop_loss_update"
