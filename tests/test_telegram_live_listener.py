import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.ai_recognition_config import AiModelConfig, AiRecognitionConfig, save_ai_recognition_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import RawMessage
from telegram_kol_research.message_recognition import MessageRecognitionResult
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_live_listener import persist_live_message_event


class _FakeSender:
    def __init__(self, first_name: str, last_name: str = "") -> None:
        self.first_name = first_name
        self.last_name = last_name


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


def test_persist_live_message_event_triggers_strategy_alert_processor(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    asyncio.run(
        persist_live_message_event(
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


def test_persist_live_message_event_loads_ai_config_path_per_message(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    config_path = tmp_path / "ai_recognition.yaml"
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="First prompt.",
            mimo_direct_prompt="First MiMo prompt.",
        ),
    )
    seen_prompts: list[str] = []

    def fake_recognize_message_now(session_factory, *, raw_message_id, ai_recognition_config):
        seen_prompts.append(ai_recognition_config.recognition_prompt)
        return None

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.recognize_message_now",
        fake_recognize_message_now,
    )

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config_path=config_path,
        )
    )
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="Second prompt.",
            mimo_direct_prompt="Second MiMo prompt.",
        ),
    )
    second_event = _FakeEvent()
    second_event.message.id = 43
    asyncio.run(
        persist_live_message_event(
            event=second_event,
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config_path=config_path,
        )
    )

    assert seen_prompts[0].startswith("First prompt.")
    assert seen_prompts[1].startswith("Second prompt.")


def test_persist_live_message_event_runs_mimo_side_channel(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    config = AiRecognitionConfig(
        recognition_prompt="DeepSeek rules.",
        mimo_direct_prompt="MiMo rules.",
        ai_models=[
            AiModelConfig(
                id="mimo-v2.5",
                label="MiMo V2.5",
                base_url="https://api.xiaomimimo.com/v1",
                api_key="mimo-key",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
            )
        ],
    )
    recognized: list[int] = []
    compared: list[tuple[int, str]] = []

    def fake_recognize_message_now(session_factory, *, raw_message_id, ai_recognition_config):
        recognized.append(raw_message_id)
        return None

    def fake_run_mimo_direct_for_message(
        session_factory,
        *,
        raw_message_id,
        ai_recognition_config,
        media_root,
    ):
        compared.append((raw_message_id, ai_recognition_config.mimo_direct_prompt))
        return None

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.recognize_message_now",
        fake_recognize_message_now,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.run_mimo_direct_for_message",
        fake_run_mimo_direct_for_message,
    )

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config=config,
        )
    )

    assert len(recognized) == 1
    assert compared == [(recognized[0], "MiMo rules.")]


def test_persist_live_message_event_sends_system_review_on_ai_disagreement(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    config = AiRecognitionConfig(
        ai_models=[
            AiModelConfig(
                id="mimo-v2.5",
                label="MiMo V2.5",
                base_url="https://api.xiaomimimo.com/v1",
                api_key="mimo-key",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
            )
        ],
    )
    system_messages: list[dict] = []
    alert_calls: list[dict] = []
    auto_trade_calls: list[int] = []

    def fake_recognize_message_now(session_factory, *, raw_message_id, ai_recognition_config):
        return MessageRecognitionResult(
            raw_message_id=raw_message_id,
            status="非策略",
            reason="DeepSeek 未识别为策略",
            parse_source="text_ai",
        )

    def fake_run_mimo_direct_for_message(
        session_factory,
        *,
        raw_message_id,
        ai_recognition_config,
        media_root,
    ):
        return SimpleNamespace(
            status="是策略",
            reason="MiMo 判断为 BTC 开仓策略",
            confidence=0.91,
            strategy_json='{"symbol":"BTC","side":"long"}',
            error_message=None,
            input_kind="text",
        )

    async def fake_system_sender(**kwargs):
        system_messages.append(kwargs)

    async def fake_strategy_alert_processor(**kwargs):
        alert_calls.append(kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.recognize_message_now",
        fake_recognize_message_now,
    )
    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.run_mimo_direct_for_message",
        fake_run_mimo_direct_for_message,
    )

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="VIP BTC Room",
            ai_recognition_config=config,
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="system-token",
                chat_id="system-chat",
            ),
            system_operator_conflict_sender=fake_system_sender,
            auto_trade_executor=auto_trade_calls.append,
        )
    )

    assert len(system_messages) == 1
    assert system_messages[0]["config"].chat_id == "system-chat"
    payload = system_messages[0]["payload"]
    assert payload["chat_title"] == "VIP BTC Room"
    assert payload["message_id"] == 42
    assert payload["deepseek"]["kind"] == "non_strategy"
    assert payload["mimo"]["kind"] == "strategy_related"
    assert alert_calls == []
    assert auto_trade_calls == []


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
        await persist_live_message_event(
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
        await persist_live_message_event(
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
        persist_live_message_event(
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


def test_authoritative_mimo_failure_still_alerts_position_management_text(
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

    text = "移动保本损 剩余30%挂65000全部止盈 我怕后半夜搞事情"
    asyncio.run(
        persist_live_message_event(
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
    )

    assert len(sent) == 1
    assert sent[0]["payload"]["agreement_status"] == "authoritative_failed"
    assert sent[0]["payload"]["text"] == text
    assert [row["notification_status"] for row in audit] == [
        "scheduled",
        "sent",
    ]


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
        await persist_live_message_event(
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
