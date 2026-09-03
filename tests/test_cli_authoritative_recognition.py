import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.cli import (
    SyncMode,
    _process_raw_messages_with_mimo_authority,
    _run_telegram_sync,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import AuthoritativeExecutionAttempt, RawMessage


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
    assert calls[0]["execution_owner"] is None
    assert calls[0]["execution_registry"] is None


def test_cli_parse_uses_execution_lease_when_exact_schema_is_installed(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=11, text="BTC long")
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

    asyncio.run(
        _process_raw_messages_with_mimo_authority(
            session_factory,
            raw_message_ids=[raw_id],
            ai_recognition_config_path=Path("ai.yaml"),
            media_root=tmp_path / "media",
        )
    )

    assert calls[0]["execution_owner"].runtime_role == "all"
    assert calls[0]["execution_registry"] is not None


def test_cli_parse_fails_closed_when_execution_schema_is_partially_installed(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]
    AuthoritativeExecutionAttempt.__table__.create(engine)
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=12, text="BTC long")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("processor must not run with partial schema")
        ),
    )

    with pytest.raises(RuntimeError, match="schema is incomplete or invalid"):
        asyncio.run(
            _process_raw_messages_with_mimo_authority(
                session_factory,
                raw_message_ids=[raw_id],
                ai_recognition_config_path=Path("ai.yaml"),
                media_root=tmp_path / "media",
            )
        )


def test_cli_authoritative_result_leaves_semantic_review_pending(
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
            agreement_status="pending",
            differences=[],
            mimo=SimpleNamespace(
                model="mimo-v2.5",
                status="非策略",
                payload={"reason": "立即出局"},
                error_message=None,
            ),
            deepseek_payload=None,
        ),
        automation={"status": "skipped", "reason": "auto_trade_not_configured"},
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        lambda *args, **kwargs: processing_result,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.send_ai_recognition_conflict_review",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("CLI parse must not run semantic review inline")
        ),
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

    assert processing_result.assessment.agreement_status == "pending"


def test_cli_mimo_failure_notification_does_not_block_later_messages(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        first = RawMessage(chat_id=1, message_id=20, sender_name="峰哥", text="first")
        second = RawMessage(chat_id=1, message_id=21, sender_name="峰哥", text="second")
        session.add_all([first, second])
        session.commit()
        raw_ids = [first.id, second.id]

    processed: list[int] = []
    sent: list[int] = []
    audit: list[tuple[int, str]] = []

    def fake_processor(*args, **kwargs):
        raw_message_id = kwargs["raw_message_id"]
        processed.append(raw_message_id)
        return SimpleNamespace(
            recognition=SimpleNamespace(status="识别失败"),
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="识别失败",
                    payload={},
                    error_message="timeout",
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def delayed_sender(*, config, payload):
        if payload["message_id"] == 20:
            while raw_ids[1] not in processed:
                await asyncio.sleep(0)
        if payload["message_id"] == 21:
            raise RuntimeError("notification unavailable")
        sent.append(payload["message_id"])

    def record_audit(*args, **kwargs):
        audit.append((kwargs["raw_message_id"], kwargs["notification_status"]))

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        fake_processor,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.update_recognition_execution_outcome",
        record_audit,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.send_ai_recognition_conflict_review",
        delayed_sender,
    )

    asyncio.run(
        asyncio.wait_for(
            _process_raw_messages_with_mimo_authority(
                session_factory,
                raw_message_ids=raw_ids,
                ai_recognition_config_path=Path("ai.yaml"),
                media_root=tmp_path / "media",
                system_operator_bot_config=SimpleNamespace(
                    bot_token="token",
                    chat_id="chat",
                ),
            ),
            timeout=0.5,
        )
    )

    assert processed == raw_ids
    assert sent == [20]
    assert set(audit) == {
        (raw_ids[0], "scheduled"),
        (raw_ids[0], "sent"),
        (raw_ids[1], "scheduled"),
        (raw_ids[1], "failed"),
    }


def test_cli_drains_scheduled_failure_alert_when_later_processing_raises(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        first = RawMessage(chat_id=1, message_id=30, sender_name="峰哥", text="first")
        second = RawMessage(chat_id=1, message_id=31, sender_name="峰哥", text="second")
        session.add_all([first, second])
        session.commit()
        raw_ids = [first.id, second.id]

    audit: list[str] = []
    sent: list[int] = []

    def fake_processor(*args, **kwargs):
        if kwargs["raw_message_id"] == raw_ids[1]:
            raise ValueError("second processing failed")
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="识别失败",
                    payload={},
                    error_message="timeout",
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def slow_sender(*, config, payload):
        await asyncio.sleep(0.05)
        sent.append(payload["message_id"])

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.process_authoritative_message",
        fake_processor,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.send_ai_recognition_conflict_review",
        slow_sender,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.update_recognition_execution_outcome",
        lambda *args, **kwargs: audit.append(kwargs["notification_status"]),
    )

    with pytest.raises(ValueError, match="second processing failed"):
        asyncio.run(
            _process_raw_messages_with_mimo_authority(
                session_factory,
                raw_message_ids=raw_ids,
                ai_recognition_config_path=Path("ai.yaml"),
                media_root=tmp_path / "media",
                system_operator_bot_config=SimpleNamespace(
                    bot_token="token",
                    chat_id="chat",
                ),
            )
        )

    assert sent == [30]
    assert audit == ["scheduled", "sent"]


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
