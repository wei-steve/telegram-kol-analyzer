import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram_kol_research.cli import (
    SyncMode,
    _process_raw_messages_with_mimo_authority,
    _run_telegram_sync,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


def test_cli_parse_uses_authoritative_processor_without_auto_trade(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="BTC long")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    calls: list[dict] = []
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        lambda *args, **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            assessment=SimpleNamespace(agreement_status="agreed"),
            automation={"status": "skipped", "reason": "test"},
        ),
    )

    inserted = asyncio.run(
        _process_raw_messages_with_mimo_authority(
            session_factory,
            raw_message_ids=[raw_id],
            ai_recognition_config_path=Path("ai.yaml"),
            media_root=tmp_path / "media",
        )
    )

    assert inserted == 0
    assert len(calls) == 1
    assert calls[0]["raw_message_id"] == raw_id
    assert calls[0]["auto_trade_executor"] is None


def test_cli_authoritative_disagreement_sends_operator_notification(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, sender_name="峰哥", text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    processing_result = SimpleNamespace(
        recognition=SimpleNamespace(status="非策略"),
        assessment=SimpleNamespace(
            agreement_status="disagreed",
            differences=["lifecycle_event.event_type"],
            mimo=SimpleNamespace(
                model="mimo-v2.5",
                status="非策略",
                payload={"reason": "立即出局"},
                error_message=None,
            ),
            deepseek_payload={"recognition_result": "非策略"},
        ),
        automation={"status": "skipped", "reason": "auto_trade_not_configured"},
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        lambda *args, **kwargs: processing_result,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_authoritative_notification_payload",
        lambda **kwargs: {"message_id": 2, "automation": processing_result.automation},
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )

    async def fake_sender(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.cli.send_ai_recognition_conflict_review",
        fake_sender,
    )
    bot_config = SimpleNamespace(bot_token="token", chat_id="chat")

    asyncio.run(
        _process_raw_messages_with_mimo_authority(
            session_factory,
            raw_message_ids=[raw_id],
            ai_recognition_config_path=Path("ai.yaml"),
            media_root=tmp_path / "media",
            system_operator_bot_config=bot_config,
        )
    )

    assert len(sent) == 1
    assert sent[0]["payload"]["message_id"] == 2


def test_cli_sync_passes_custom_media_root_to_telegram_download(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    captured: dict = {}

    async def fake_login(*args, **kwargs):
        return None

    async def fake_discover(client):
        return [{"id": 1, "title": "峰哥", "archived": True}]

    async def fake_fetch(client, dialog, *, limit, media_root):
        captured["media_root"] = media_root
        return []

    monkeypatch.setattr("telegram_kol_research.cli.ensure_telegram_login", fake_login)
    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fake_fetch)
    custom_media_root = tmp_path / "custom-media"

    asyncio.run(
        _run_telegram_sync(
            client=object(),
            session_factory=session_factory,
            target_titles={"峰哥"},
            windows_by_title={},
            message_limit=10,
            mode=SyncMode.backfill,
            ai_recognition_config_path=Path("ai.yaml"),
            media_root=custom_media_root,
        )
    )

    assert captured["media_root"] == custom_media_root
