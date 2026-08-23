import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from PIL import Image
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.models import (
    MediaAsset,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SyncCheckpoint,
    utc_now,
)
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_live_listener import (
    _is_usable_downloaded_media_path,
    run_live_listener,
    run_reconcile_once,
)


class _FakeClient:
    pass


def test_media_replay_accepts_usable_legacy_windows_style_path(tmp_path):
    image_path = tmp_path / "media" / "9001" / "77.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1)).save(image_path, format="JPEG")

    assert _is_usable_downloaded_media_path(
        "data\\media\\9001\\77.jpg",
        media_root=tmp_path / "media",
    )


class _FakeListenerClient:
    def __init__(self):
        self.connect_calls = 0
        self.handlers = []
        self.run_until_disconnected_calls = 0

    async def connect(self):
        self.connect_calls += 1

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    async def run_until_disconnected(self):
        self.run_until_disconnected_calls += 1


async def _fake_discover_dialogs(client):
    return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]


async def _fake_fetch_dialog_messages(client, dialog, limit, media_root="data/media"):
    return [
        {
            "chat_id": 9001,
            "message_id": 77,
            "sender_id": 501,
            "sender_name": "VIP BTC Room",
            "text": "already seen",
            "posted_at": "2026-04-10T08:30:00+00:00",
            "media": None,
        },
        {
            "chat_id": 9001,
            "message_id": 78,
            "sender_id": 501,
            "sender_name": "VIP BTC Room",
            "text": "fresh message",
            "posted_at": "2026-04-10T08:45:00+00:00",
            "media": None,
        },
    ]


def test_run_reconcile_once_persists_only_messages_newer_than_checkpoint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    with session_factory() as session:
        raw_messages = session.query(RawMessage).order_by(RawMessage.message_id).all()
        checkpoint = (
            session.query(SyncCheckpoint)
            .filter(
                SyncCheckpoint.chat_id == 9001,
                SyncCheckpoint.sync_kind == "history",
            )
            .one()
        )

    assert stats["inserted_messages"] == 1
    assert [message.message_id for message in raw_messages] == [78]
    assert checkpoint.last_message_id == 78


def test_run_reconcile_once_bounds_dialog_discovery_to_archived_folder(tmp_path):
    calls = []

    async def discover_archived_dialogs(_client, *, archived_only=False):
        calls.append(archived_only)
        return []

    stats = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=create_session_factory(tmp_path / "research.db"),
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=discover_archived_dialogs,
        )
    )

    assert calls == [True]
    assert stats["matched_dialogs"] == 0


def test_history_reconcile_without_authority_persists_raw_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "authority-required.db")

    stats = asyncio.run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=None,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert stats["inserted_messages"] == 2
    assert stats["inserted_candidates"] == 0
    assert stats["inserted_trade_ideas"] == 0
    assert stats["recognition_status"] == "authoritative_processor_required"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_reconcile_processes_each_new_message_authoritatively_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed: list[int] = []

    def authoritative_processor(raw_message_id):
        processed.append(raw_message_id)
        with session_factory() as session:
            session.add(
                RecognitionDecision(
                    raw_message_id=raw_message_id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="非策略",
                    authoritative_payload_json="{}",
                    agreement_status="agreed",
                    differences_json="[]",
                    prompt_versions_json="{}",
                    comparison_status="completed",
                )
            )
            session.commit()
        return SimpleNamespace(
            recognition=SimpleNamespace(status="非策略"),
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="非策略",
                    payload={},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "group_not_auto_trade"},
        )

    first = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )
    second = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert first["inserted_messages"] == 2
    assert second["inserted_messages"] == 0
    assert len(processed) == 2
    with session_factory() as session:
        processed_message_ids = {
            session.get(RawMessage, raw_message_id).message_id
            for raw_message_id in processed
        }
    assert processed_message_ids == {77, 78}


def test_reconcile_recovers_persisted_message_without_authoritative_decision(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-gap.db")
    with session_factory() as session:
        now = utc_now()
        raw_message = RawMessage(
            chat_id=9001,
            message_id=88,
            text="BTC 空单出局",
            posted_at=now,
        )
        session.add(raw_message)
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=88,
                last_message_at=now,
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    processed: list[int] = []

    def authoritative_processor(raw_message_id):
        processed.append(raw_message_id)
        with session_factory() as session:
            session.add(
                RecognitionDecision(
                    raw_message_id=raw_message_id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="非策略",
                    authoritative_payload_json="{}",
                    agreement_status="agreed",
                    differences_json="[]",
                    prompt_versions_json="{}",
                    comparison_status="completed",
                )
            )
            session.commit()
        return SimpleNamespace(
            recognition=SimpleNamespace(status="非策略"),
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="非策略",
                    payload={},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "mimo_no_action"},
        )

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    for _ in range(2):
        __import__("asyncio").run(
            run_reconcile_once(
                client=_FakeClient(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=authoritative_processor,
                discover_dialogs_fn=_fake_discover_dialogs,
                fetch_dialog_messages_fn=no_messages,
            )
        )

    assert processed == [raw_message_id]


def test_reconcile_does_not_recover_old_missing_authoritative_decision(tmp_path):
    session_factory = create_session_factory(tmp_path / "old-recovery-gap.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=88,
                text="stale BTC entry",
                posted_at=datetime(2026, 4, 10, 9, 0),
            )
        )
        session.commit()

    processed: list[int] = []

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=processed.append,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=no_messages,
        )
    )

    assert processed == []
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
    assert decision.authoritative_model == "recovery_guard"
    assert decision.automation_reason == "authoritative_gap_recovery_expired"


def test_reconcile_suppresses_operator_notification_for_expired_gap(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired-notification.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=88,
                text="NVDA 900 附近观察",
                posted_at=datetime(2026, 4, 10, 9, 0),
            )
        )
        session.commit()

    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs["payload"])

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    async def scenario():
        await run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=lambda raw_message_id: None,
            notification_bot_config=SystemOperatorBotConfig(
                bot_token="notification-token",
                chat_id="notification-chat",
            ),
            system_operator_conflict_sender=sender,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=no_messages,
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert sent == []
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
    assert decision.authoritative_model == "recovery_guard"
    assert decision.automation_status == "skipped"
    assert decision.automation_reason == "authoritative_gap_recovery_expired"
    assert decision.notification_status == "suppressed_expired_recovery"


def test_reconcile_keeps_expired_gap_notification_suppressed_on_second_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired-notification-repeat.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=88,
                text="NVDA 900 附近观察",
                posted_at=datetime(2026, 4, 10, 9, 0),
            )
        )
        session.commit()

    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs["payload"])

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    async def scenario():
        for _ in range(2):
            await run_reconcile_once(
                client=_FakeClient(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=lambda raw_message_id: None,
                system_operator_bot_config=SystemOperatorBotConfig(
                    bot_token="system-token",
                    chat_id="system-chat",
                ),
                system_operator_conflict_sender=sender,
                discover_dialogs_fn=_fake_discover_dialogs,
                fetch_dialog_messages_fn=no_messages,
            )
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert sent == []
    with session_factory() as session:
        decisions = session.query(RecognitionDecision).all()
    assert len(decisions) == 1
    assert decisions[0].notification_status == "suppressed_expired_recovery"


def test_reconcile_continues_after_one_missing_decision_recovery_fails(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-failure.db")
    with session_factory() as session:
        now = utc_now()
        session.add_all(
            [
                RawMessage(
                    chat_id=9001,
                    message_id=88,
                    text="first",
                    posted_at=now,
                ),
                RawMessage(
                    chat_id=9001,
                    message_id=89,
                    text="second",
                    posted_at=now,
                ),
            ]
        )
        session.commit()
        raw_message_ids = [
            row.id
            for row in session.query(RawMessage).order_by(RawMessage.message_id).all()
        ]

    processed: list[int] = []

    def authoritative_processor(raw_message_id):
        processed.append(raw_message_id)
        if raw_message_id == raw_message_ids[0]:
            raise RuntimeError("temporary recognition failure")
        with session_factory() as session:
            session.add(
                RecognitionDecision(
                    raw_message_id=raw_message_id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="非策略",
                    authoritative_payload_json="{}",
                    agreement_status="agreed",
                    differences_json="[]",
                    prompt_versions_json="{}",
                    comparison_status="completed",
                )
            )
            session.commit()
        return SimpleNamespace(
            recognition=SimpleNamespace(status="非策略"),
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="非策略",
                    payload={},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "mimo_no_action"},
        )

    async def no_messages(client, dialog, limit, media_root="data/media"):
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=no_messages,
        )
    )

    assert processed == raw_message_ids


def test_live_listener_waits_for_shared_telegram_operation_lock(monkeypatch):
    client = _FakeListenerClient()
    processed: list[object] = []

    async def fake_persist_live_message_event(**kwargs):
        processed.append(kwargs["event"])
        return {}

    class Event:
        message = object()

        async def get_chat(self):
            return SimpleNamespace(title="VIP BTC Room")

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.persist_live_message_event",
        fake_persist_live_message_event,
    )

    async def scenario():
        operation_lock = asyncio.Lock()
        await run_live_listener(
            client=client,
            session_factory=None,
            broker=None,
            target_titles={"VIP BTC Room"},
            operation_lock=operation_lock,
        )
        handler, _ = client.handlers[0]
        await operation_lock.acquire()
        task = asyncio.create_task(handler(Event()))
        await asyncio.sleep(0)
        assert processed == []
        operation_lock.release()
        await task

    asyncio.run(scenario())


def test_reconcile_delivers_completed_instruction_summaries(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "summary.db")
    delivered: list[int] = []

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            recognition=SimpleNamespace(status="是策略"),
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="是策略",
                    payload={},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation={
                "status": "completed",
                "items": [
                    {
                        "item_id": raw_message_id,
                        "sequence": 0,
                        "instruction_kind": "entry",
                        "strategy_instance_id": f"strategy-{raw_message_id}",
                        "status": "submitted",
                    }
                ],
            },
        )

    async def fake_deliver(*args, **kwargs):
        delivered.append(kwargs["raw_message_id"])
        return True

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener."
        "deliver_message_instruction_summary_notification",
        fake_deliver,
    )

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            notification_bot_config=SystemOperatorBotConfig(
                bot_token="notification-token",
                chat_id="notification-chat",
            ),
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert len(delivered) == 2


def test_reconcile_retries_failed_summary_without_new_messages(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "summary-retry.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=9001, message_id=88, text="ETH long")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
            )
        )
        session.flush()
        item = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw.id,
        )[0]
        item.status = "succeeded"
        item.result_json = '{"status":"completed"}'
        item.summary_notification_status = "failed"
        raw_id = raw.id
        session.commit()

    delivered: list[int] = []

    async def fake_deliver(*args, **kwargs):
        delivered.append(kwargs["raw_message_id"])
        return True

    monkeypatch.setattr(
        "telegram_kol_research.system_operator_bot."
        "deliver_message_instruction_summary_notification",
        fake_deliver,
    )

    async def no_dialogs(client):
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            notification_bot_config=SystemOperatorBotConfig(
                bot_token="notification-token",
                chat_id="notification-chat",
            ),
            discover_dialogs_fn=no_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert delivered == [raw_id]


def test_run_reconcile_once_limits_media_downloads_to_new_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    captured = {}

    async def fake_fetch_dialog_messages(client, dialog, **kwargs):
        captured.update(kwargs)
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "overlap image",
                "posted_at": "2026-04-10T08:30:00+00:00",
                "media": {"kind": "photo", "path": None},
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh image",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": {"kind": "photo", "path": "9001/78.jpg"},
            },
        ]

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    assert stats["inserted_messages"] == 1
    assert captured["media_download_min_message_id"] == 77
    assert captured["media_download_message_ids"] == set()


def test_run_reconcile_once_retries_zero_byte_media_within_overlap(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    media_root = tmp_path / "media"
    zero_byte_path = media_root / "9001" / "77.jpg"
    zero_byte_path.parent.mkdir(parents=True)
    zero_byte_path.write_bytes(b"")
    with session_factory() as session:
        message = RawMessage(chat_id=9001, message_id=77, text="cached image")
        session.add(message)
        session.flush()
        session.add(MediaAsset(raw_message_id=message.id, kind="photo", local_path="9001/77.jpg"))
        session.add(SyncCheckpoint(chat_id=9001, sync_kind="history", last_message_id=77))
        session.commit()

    captured = {}

    async def fake_fetch_dialog_messages(client, dialog, **kwargs):
        captured.update(kwargs)
        return []

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            media_root=media_root,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    assert captured["media_download_message_ids"] == {77}


def test_run_reconcile_once_does_not_expand_window_for_old_missing_media(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old_message = RawMessage(
            chat_id=9001,
            message_id=10,
            text="old cached image",
            posted_at=datetime(2026, 4, 1, 8, 30),
        )
        session.add(old_message)
        session.flush()
        session.add(MediaAsset(raw_message_id=old_message.id, kind="photo", local_path=None))
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=77,
                text="checkpoint message",
                posted_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.add(
            SyncCheckpoint(
                chat_id=9001,
                sync_kind="history",
                last_message_id=77,
                last_message_at=datetime(2026, 4, 10, 8, 30),
            )
        )
        session.commit()

    processed = []

    async def fake_fetch_dialog_messages(client, dialog, limit, media_root="data/media"):
        return [
            {
                "chat_id": 9001,
                "message_id": 10,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "old cached image replay",
                "posted_at": "2026-04-01T08:30:00+00:00",
                "media": {"kind": "photo", "path": "media/10.jpg"},
            },
            {
                "chat_id": 9001,
                "message_id": 78,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "fresh message",
                "posted_at": "2026-04-10T08:45:00+00:00",
                "media": None,
            },
        ]

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs["record"].message_id)
        return {"status": "sent"}

    stats = __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=fake_fetch_dialog_messages,
        )
    )

    with session_factory() as session:
        old_message = session.query(RawMessage).filter(RawMessage.message_id == 10).one()

    assert stats["inserted_messages"] == 1
    assert old_message.text == "old cached image"
    assert processed == [78]


def test_run_reconcile_once_triggers_strategy_alert_processor_for_fresh_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert len(processed) == 2
    assert [item["record"].message_id for item in processed] == [77, 78]
    assert {item["chat_title"] for item in processed} == {"VIP BTC Room"}


def test_run_reconcile_once_skips_strategy_alert_processor_when_group_ai_is_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    __import__("asyncio").run(
        run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            strategy_alert_config=object(),
            strategy_alert_enabled_for_title=lambda title: False,
            strategy_alert_processor=fake_strategy_alert_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_fake_fetch_dialog_messages,
        )
    )

    assert processed == []


def test_run_live_listener_connects_client_before_waiting_for_events():
    client = _FakeListenerClient()

    __import__("asyncio").run(
        run_live_listener(
            client=client,
            session_factory=None,
            broker=None,
            target_titles={"VIP BTC Room"},
        )
    )

    assert client.connect_calls == 1
    assert client.run_until_disconnected_calls == 1
    assert [type(event_filter).__name__ for _, event_filter in client.handlers] == [
        "NewMessage",
        "MessageDeleted",
    ]
