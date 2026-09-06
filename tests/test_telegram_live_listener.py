import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import (
    ExecutionEvent,
    MessageInstructionItem,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    TradeSignal,
)
from telegram_kol_research.message_processing_worker import process_message_job
from telegram_kol_research.message_recognition import MessageRecognitionResult
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_terminal_authoritative_decision,
)
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_live_listener import (
    _schedule_authoritative_notification,
    persist_live_message_event,
)
from telegram_kol_research.telegram_live_listener import run_live_listener


class _FakeSender:
    def __init__(self, first_name: str, last_name: str = "") -> None:
        self.first_name = first_name
        self.last_name = last_name


def test_authoritative_notification_failure_captures_independent_incident(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "notification-failure.db")
    captures = []
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.capture_runtime_incident_best_effort",
        lambda *args, **kwargs: captures.append(kwargs),
        raising=False,
    )
    outcome_updates = []
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: outcome_updates.append(kwargs),
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.claim_authoritative_failure_notification",
        lambda *args, **kwargs: True,
    )

    async def scenario():
        async def failing_sender(**kwargs):
            raise TimeoutError("secret provider response")

        notification_task = _schedule_authoritative_notification(
            session_factory=session_factory,
            raw_message_id=41,
            sender=failing_sender,
            config=object(),
            payload={"automation": {"status": "failed", "reason": "context_exhausted"}},
        )
        await notification_task

    asyncio.run(scenario())

    assert len(captures) == 1
    assert captures[0]["source_kind"] == "authoritative_notification"
    assert captures[0]["source_record_id"] == "raw_message_41"
    assert captures[0]["error_type"] == "TimeoutError"
    assert outcome_updates[-1]["notification_error"] == "TimeoutError"


def test_authoritative_notification_is_once_only_after_sent(tmp_path):
    session_factory = create_session_factory(tmp_path / "notification-once.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=123, message_id=41, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    save_terminal_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="识别失败",
            authoritative_payload={},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[],
            prompt_versions={"mimo": {}},
        ),
    )
    deliveries = []

    async def scenario():
        async def sender(**kwargs):
            deliveries.append(kwargs)

        first = _schedule_authoritative_notification(
            session_factory=session_factory,
            raw_message_id=raw_id,
            sender=sender,
            config=object(),
            payload={
                "automation": {
                    "status": "skipped",
                    "reason": "mimo_authoritative_failed",
                }
            },
        )
        assert first is not None
        await first
        second = _schedule_authoritative_notification(
            session_factory=session_factory,
            raw_message_id=raw_id,
            sender=sender,
            config=object(),
            payload={
                "automation": {
                    "status": "skipped",
                    "reason": "mimo_authoritative_failed",
                }
            },
        )
        assert second is None

    asyncio.run(scenario())

    assert len(deliveries) == 1
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
    assert row.notification_status == "sent"


def test_authoritative_notification_can_retry_after_send_failure(tmp_path):
    session_factory = create_session_factory(tmp_path / "notification-retry.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=123, message_id=42, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    save_terminal_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="识别失败",
            authoritative_payload={},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[],
            prompt_versions={"mimo": {}},
        ),
    )
    attempts = 0

    async def scenario():
        nonlocal attempts

        async def sender(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("temporary delivery failure")

        payload = {
            "automation": {
                "status": "skipped",
                "reason": "mimo_authoritative_failed",
            }
        }
        first = _schedule_authoritative_notification(
            session_factory=session_factory,
            raw_message_id=raw_id,
            sender=sender,
            config=object(),
            payload=payload,
        )
        assert first is not None
        await first
        with session_factory() as session:
            assert session.query(RecognitionDecision).one().notification_status == "failed"
        second = _schedule_authoritative_notification(
            session_factory=session_factory,
            raw_message_id=raw_id,
            sender=sender,
            config=object(),
            payload=payload,
        )
        assert second is not None
        await second

    asyncio.run(scenario())

    assert attempts == 2
    with session_factory() as session:
        row = session.query(RecognitionDecision).one()
    assert row.notification_status == "sent"
    assert row.notification_error is None


class _FakeMessage:
    def __init__(self, text: str = "live hello") -> None:
        self.id = 42
        self.sender_id = 7
        self.message = text
        self.reply_to_msg_id = None
        self.date = datetime(2026, 4, 19, 10, 0, tzinfo=UTC)
        self.edit_date = None
        self.media = None
        self.photo = None
        self.document = None

    async def get_sender(self):
        return _FakeSender("Alice", "Trader")


class _FakeEvent:
    def __init__(self, text: str = "live hello") -> None:
        self.chat_id = 123
        self.message = _FakeMessage(text=text)


class _FakeListenerClient:
    def __init__(self) -> None:
        self.handlers = []
        self.connect_calls = 0
        self.run_until_disconnected_calls = 0

    async def connect(self):
        self.connect_calls += 1

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    async def run_until_disconnected(self):
        self.run_until_disconnected_calls += 1


class _FakeDeletedEvent:
    def __init__(self, *, chat_id, deleted_ids):
        self.chat_id = chat_id
        self.deleted_ids = deleted_ids


async def _persist_then_process(**kwargs):
    """Persist through ingest, then run the worker's post-persist chain.

    The queue pipeline splits what used to be one call: ``ingest`` persists and
    enqueues, and ``worker`` runs recognition, alerting and notification. Tests
    that assert on the second half go through this helper so they exercise the
    same chain the worker runs, with the same arguments.
    """

    persist_kwargs = {
        key: kwargs.pop(key)
        for key in ("event", "session_factory", "broker", "media_root")
        if key in kwargs
    }
    kwargs.pop("ai_recognition_config", None)
    session_factory = persist_kwargs["session_factory"]
    event = persist_kwargs["event"]
    stats = await persist_live_message_event(**persist_kwargs)
    with session_factory() as session:
        raw_message_id = (
            session.query(RawMessage.id)
            .filter(
                RawMessage.chat_id == int(event.chat_id),
                RawMessage.message_id == int(event.message.id),
            )
            .scalar()
        )
    await process_message_job(
        session_factory,
        raw_message_id=int(raw_message_id),
        **kwargs,
    )
    return stats


def test_persist_live_message_event_writes_db_and_broker_event(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
        )
    )

    with session_factory() as session:
        stored = session.query(RawMessage).filter(RawMessage.chat_id == 123).one()

    assert stored.message_id == 42
    assert stored.sender_name == "Alice Trader"
    assert stored.text == "live hello"
    assert broker.published_events[-1]["chat_id"] == 123
    assert broker.published_events[-1]["message_id"] == 42


def test_live_intake_makes_no_decision_of_its_own(tmp_path):
    """Ingest persists and enqueues; nothing downstream of it runs here."""

    session_factory = create_session_factory(tmp_path / "authority-required.db")
    broker = LiveUpdateBroker()
    authoritative_calls: list[int] = []

    # The ingest persist path has no execution parameter left to pass.
    signature = inspect.signature(persist_live_message_event)
    assert "auto_trade_executor" not in signature.parameters
    assert "strategy_alert_processor" not in signature.parameters

    stats = asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_calls.append,
        )
    )

    assert stats["inserted_messages"] == 1
    assert authoritative_calls == []
    with session_factory() as session:
        assert session.query(MessageProcessingJob).one().status == "pending"
        assert session.query(RecognitionDecision).count() == 0
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0
        assert session.query(TradeSignal).count() == 0
        assert session.query(ExecutionEvent).count() == 0


def test_live_listener_registers_and_records_each_deleted_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=123,
                    message_id=7,
                    text="first",
                    archived_target_group=True,
                ),
                RawMessage(
                    chat_id=123,
                    message_id=8,
                    text="second",
                    archived_target_group=True,
                ),
            ]
        )
        session.commit()
    client = _FakeListenerClient()
    recorded = []

    def recorder(session_factory, **kwargs):
        recorded.append(kwargs)

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP"},
            source_deletion_recorder=recorder,
        )
        assert len(client.handlers) == 2
        deleted_handler = next(
            handler
            for handler, event_filter in client.handlers
            if type(event_filter).__name__ == "MessageDeleted"
        )
        await deleted_handler(
            _FakeDeletedEvent(chat_id=123, deleted_ids=[7, 8])
        )

    asyncio.run(scenario())

    assert [item["message_id"] for item in recorded] == [7, 8]
    assert {item["chat_id"] for item in recorded} == {123}
    assert all(item["telegram_event"]["deleted_ids"] == [7, 8] for item in recorded)


def test_live_listener_deletion_waits_for_its_own_chat_lock(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=123,
                message_id=7,
                text="first",
                archived_target_group=True,
            )
        )
        session.commit()
    client = _FakeListenerClient()
    recorded = []

    async def scenario():
        registry = KeyedAsyncLockRegistry()
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP"},
            source_deletion_recorder=lambda _factory, **kwargs: recorded.append(kwargs),
            operation_lock=registry,
        )
        deleted_handler = next(
            handler
            for handler, event_filter in client.handlers
            if type(event_filter).__name__ == "MessageDeleted"
        )
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_chat_123():
            async with registry.lock(123):
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(hold_chat_123())
        await holder_entered.wait()
        task = asyncio.create_task(
            deleted_handler(_FakeDeletedEvent(chat_id=123, deleted_ids=[7]))
        )
        await asyncio.sleep(0)
        assert recorded == []
        release_holder.set()
        await asyncio.wait_for(asyncio.gather(holder, task), timeout=5.0)

    asyncio.run(scenario())
    assert [item["message_id"] for item in recorded] == [7]


def test_live_listener_does_not_target_deletion_without_chat_id(
    tmp_path, monkeypatch
):
    import telegram_kol_research.telegram_live_listener as listener_module

    session_factory = create_session_factory(tmp_path / "research.db")
    client = _FakeListenerClient()
    recorded = []
    warnings = []
    monkeypatch.setattr(
        listener_module.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )

    async def scenario():
        await run_live_listener(
            client=client,
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP"},
            source_deletion_recorder=lambda _factory, **kwargs: recorded.append(kwargs),
        )
        deleted_handler = next(
            handler
            for handler, event_filter in client.handlers
            if type(event_filter).__name__ == "MessageDeleted"
        )
        await deleted_handler(_FakeDeletedEvent(chat_id=None, deleted_ids=[7]))

    asyncio.run(scenario())

    assert recorded == []
    assert any("without exact chat_id" in message for message in warnings)


def test_persist_live_message_event_triggers_strategy_alert_processor(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    asyncio.run(
        _persist_then_process(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="VIP BTC Room",
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
        )
    )

    assert len(processed) == 1
    assert processed[0]["chat_title"] == "VIP BTC Room"
    assert processed[0]["record"].chat_id == 123
    assert processed[0]["record"].message_id == 42


def test_authoritative_live_path_returns_without_starting_semantic_review(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    events: list[str] = []
    reviewer_started = asyncio.Event()
    semantic_review_blocker = asyncio.Event()
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )

    def authoritative_processor(raw_message_id):
        events.extend(["apply_mimo", "auto_trade"])
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    status="非策略",
                    payload={
                        "reason": "MiMo识别为立即出局",
                        "lifecycle_event": {"event_type": "exit_position"},
                    },
                    model="mimo-v2.5",
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="非策略",
                reason="MiMo识别为立即出局",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "executed", "reason": "close_submitted"},
        )

    async def blocked_semantic_reviewer(**kwargs):
        reviewer_started.set()
        await semantic_review_blocker.wait()

    async def scenario():
        await _persist_then_process(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="峰哥",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=blocked_semantic_reviewer,
        )
        assert events == ["apply_mimo", "auto_trade"]
        assert not reviewer_started.is_set()

    asyncio.run(scenario())


def test_live_intake_fetches_a_missing_reply_target_before_enqueueing(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "reply-recovery.db")
    broker = LiveUpdateBroker()
    events: list[str] = []
    event = _FakeEvent("取消上面这单")
    event.message.reply_to_msg_id = 4004
    event.client = object()

    async def fake_fetch_missing_reply_target(
        telegram_client,
        *,
        session_factory,
        chat_id,
        message_id,
        media_root,
        broker,
    ):
        assert telegram_client is event.client
        assert chat_id == 123
        assert message_id == 4004
        with session_factory() as session:
            session.add(
                RawMessage(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="BTC 多单，止损见图",
                )
            )
            session.commit()
        events.append("fetch_reply")
        return True

    def reply_evidence_processor(raw_message_id):
        assert raw_message_id > 0
        events.append("reply_evidence")

    def authoritative_processor(raw_message_id):
        assert raw_message_id > 0
        events.append("authoritative")
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    status="非策略",
                    payload={},
                    model="mimo-v2.5",
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="非策略",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "skipped", "reason": "test"},
        )

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.fetch_missing_reply_target",
        fake_fetch_missing_reply_target,
        raising=False,
    )

    asyncio.run(
        persist_live_message_event(
            event=event,
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            reply_evidence_processor=reply_evidence_processor,
        )
    )

    # Ingest recovers the reply target and its evidence before enqueueing;
    # the authoritative processor itself runs in the worker, not here.
    assert events == ["fetch_reply", "reply_evidence"]


def test_authoritative_live_path_delivers_instruction_summary_once_after_completion(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "instruction-summary.db")
    broker = LiveUpdateBroker()
    deliveries: list[dict] = []

    async def fake_deliver(*args, **kwargs):
        kwargs["session_factory"] = args[0]
        deliveries.append(kwargs)
        return True

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener."
        "deliver_message_instruction_summary_notification",
        fake_deliver,
        raising=False,
    )

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    status="是策略",
                    payload={},
                    model="mimo-v2.5",
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="是策略",
                parse_source="mimo_authoritative",
            ),
            automation={
                "status": "completed",
                "items": [
                    {
                        "item_id": 1,
                        "sequence": 0,
                        "instruction_kind": "entry",
                        "strategy_instance_id": "deepcoin:123:42:BTC:long",
                        "status": "submitted",
                    }
                ],
            },
        )

    asyncio.run(
        _persist_then_process(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="VIP BTC Room",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            notification_bot_config=SystemOperatorBotConfig(
                bot_token="notification-token",
                chat_id="notification-chat",
            ),
        )
    )

    assert len(deliveries) == 1
    assert deliveries[0]["raw_message_id"] > 0
    assert deliveries[0]["chat_title"] == "VIP BTC Room"
    assert deliveries[0]["config"].bot_token == "notification-token"


def test_authoritative_mimo_failure_keeps_independent_nonblocking_alert(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    alert_started = asyncio.Event()
    release_alert = asyncio.Event()
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.claim_authoritative_failure_notification",
        lambda *args, **kwargs: True,
    )

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    status="识别失败",
                    payload={},
                    model="mimo-v2.5",
                    error_message="timeout",
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason="timeout",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def blocked_failure_sender(**kwargs):
        assert kwargs["payload"]["agreement_status"] == "authoritative_failed"
        alert_started.set()
        await release_alert.wait()

    async def scenario():
        await _persist_then_process(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="峰哥",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=blocked_failure_sender,
        )
        await asyncio.wait_for(alert_started.wait(), timeout=1)
        release_alert.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_authoritative_mimo_failure_suppresses_obvious_external_stock_noise(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    sent: list[dict] = []
    audit: list[dict] = []

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    status="识别失败",
                    payload={},
                    model="mimo-v2.5",
                    error_message="The read operation timed out",
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason="timeout",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def sender(**kwargs):
        sent.append(kwargs)

    def record_audit(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        record_audit,
    )

    text = "美光MU 800出头比如810附近还能再吃一次，850和880分批走。"
    asyncio.run(
        _persist_then_process(
            event=_FakeEvent(text=text),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="舒琴会员群-11分组",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=sender,
        )
    )

    assert sent == []
    assert [row["notification_status"] for row in audit] == [
        "suppressed_low_value"
    ]
    assert audit[0]["automation_reason"] == "mimo_authoritative_failed"


def test_authoritative_mimo_failure_suppresses_empty_input_noise(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    sent: list[dict] = []
    audit: list[dict] = []

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    status="识别失败",
                    payload={},
                    model="mimo-v2.5",
                    error_message="message has no readable text or image",
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason="message has no readable text or image",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def sender(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: audit.append(kwargs),
    )

    asyncio.run(
        _persist_then_process(
            event=_FakeEvent(text=""),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="比特币军长-11分组",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=sender,
        )
    )

    assert sent == []
    assert [row["notification_status"] for row in audit] == [
        "suppressed_empty_input"
    ]
    assert audit[0]["automation_reason"] == "mimo_authoritative_failed"


def test_authoritative_mimo_failure_still_alerts_position_management_text(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    sent: list[dict] = []
    audit: list[dict] = []
    notification_claims: list[dict] = []

    def authoritative_processor(raw_message_id):
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                differences=[],
                mimo=SimpleNamespace(
                    status="识别失败",
                    payload={},
                    model="mimo-v2.5",
                    error_message="The read operation timed out",
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason="timeout",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    async def sender(**kwargs):
        sent.append(kwargs)

    def record_audit(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        record_audit,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.claim_authoritative_failure_notification",
        lambda *args, **kwargs: notification_claims.append(kwargs) or True,
    )

    text = "移动保本损 剩余30%挂65000全部止盈 我怕后半夜搞事情"
    async def scenario():
        await _persist_then_process(
            event=_FakeEvent(text=text),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="峰哥高级会员群-11分组",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=sender,
        )
        for _ in range(100):
            if len(audit) >= 2:
                break
            await asyncio.sleep(0.001)

    asyncio.run(scenario())

    assert len(sent) == 1
    assert sent[0]["payload"]["agreement_status"] == "authoritative_failed"
    assert sent[0]["payload"]["text"] == text
    assert len(notification_claims) == 1
    assert notification_claims[0]["automation_reason"] == "mimo_authoritative_failed"
    assert [row["notification_status"] for row in audit] == ["sent"]


def test_authoritative_mimo_failure_retries_high_risk_message_after_alert(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    calls: list[int] = []
    sent: list[dict] = []
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.claim_authoritative_failure_notification",
        lambda *args, **kwargs: True,
    )

    def authoritative_processor(raw_message_id):
        calls.append(raw_message_id)
        if len(calls) == 1:
            return SimpleNamespace(
                assessment=SimpleNamespace(
                    agreement_status="authoritative_failed",
                    differences=[],
                    mimo=SimpleNamespace(
                        status="识别失败",
                        payload={},
                        model="mimo-v2.5",
                        error_message="The read operation timed out",
                    ),
                    deepseek_payload=None,
                ),
                recognition=MessageRecognitionResult(
                    raw_message_id=raw_message_id,
                    status="识别失败",
                    reason="timeout",
                    parse_source="mimo_authoritative",
                ),
                automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
            )
        return SimpleNamespace(
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    status="非策略",
                    payload={
                        "lifecycle_event": {
                            "event_type": "position_update",
                            "management_action": "move_stop_to_protect",
                        }
                    },
                    model="mimo-v2.5",
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            recognition=MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="非策略",
                reason="retry recovered",
                parse_source="mimo_authoritative",
            ),
            automation={"status": "completed", "reason": None},
        )

    async def sender(**kwargs):
        sent.append(kwargs)

    async def scenario():
        await _persist_then_process(
            event=_FakeEvent(text="移动保本损 剩余30%挂65000全部止盈"),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="峰哥高级会员群-11分组",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
            authoritative_failure_retry_delay_seconds=0,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=sender,
        )
        for _ in range(10):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(sent) == 1
    assert len(calls) == 2
