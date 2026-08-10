import asyncio
import sqlite3
from datetime import UTC, datetime

import httpx

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import MessageRecognitionResult
from telegram_kol_research.models import AiPromptInvocation, RawMessage, SignalCandidate, StrategyAlert, StrategyLifecycle
from telegram_kol_research.prompt_defaults import DEFAULT_STRATEGY_ALERT_PROMPT
from telegram_kol_research.raw_ingest import NormalizedMessageRecord, persist_normalized_messages
from telegram_kol_research.strategy_alerts import (
    AlertDecision,
    StrategyAlertConfig,
    build_strategy_alert_prompt,
    format_strategy_alert_message,
    format_structured_strategy_alert_message,
    parse_alert_decision,
    process_strategy_alert_for_record,
)


def test_structured_alert_reports_entry_revision_truthfully_without_secrets():
    event = __import__(
        "telegram_kol_research.strategy_alerts", fromlist=["StrategyAlertEvent"]
    ).StrategyAlertEvent(
        alert_type="entry", strategy_kind="entry", is_strategy=True,
        confidence=0.9, reason_short="", symbol="BTCUSDT", side="long",
        order_type="limit", management_action=None, entry_price="64000",
        take_profit="65000", stop_loss="63000", posted_at=None,
        original_text="BTC long",
    )
    rendered = format_structured_strategy_alert_message(
        chat_title="test", event=event, message_id=9902,
        entry_assembly={"state_label": "相邻仓位/补仓方案已组装", "risk_calculation": "配置20U × 50% = 实际风险预算10U"},
        entry_revision={"label": "等待执行入场修订", "orders_changed": False, "reason_code": "planned", "raw": "secret"},
    )
    assert "配置20U × 50% = 实际风险预算10U" in rendered
    assert "等待执行入场修订" in rendered
    assert "订单已变更" not in rendered
    assert "secret" not in rendered


def _record(*, text: str | None = "Trader A\nBTC long now") -> NormalizedMessageRecord:
    return NormalizedMessageRecord(
        chat_id=100,
        message_id=55,
        sender_id=7,
        sender_name="Alice",
        text=text,
        reply_to_message_id=None,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=True,
        posted_at=datetime(2026, 5, 10, 8, 0, tzinfo=UTC),
        edit_date=None,
        raw_payload="{}",
    )


def _config() -> StrategyAlertConfig:
    return StrategyAlertConfig(
        llm_base_url="http://llm.test",
        llm_api_key="llm-key",
        llm_model="cheap-model",
        timeout_seconds=5,
        bot_token="bot-token",
        alert_chat_id="123456",
        confidence_threshold=0.6,
        max_chars=1200,
    )


def test_build_strategy_alert_prompt_keeps_context_short_and_first_line_visible():
    long_text = "Trader Zhang\n" + ("BTC long " * 400)

    prompt = build_strategy_alert_prompt(
        template=DEFAULT_STRATEGY_ALERT_PROMPT,
        chat_title="VIP BTC Room",
        sender_name="Alice",
        text=long_text,
        max_chars=80,
    )

    assert "VIP BTC Room" in prompt
    assert "first_line=Trader Zhang" in prompt
    assert len(prompt) < 1000
    assert "Return compact JSON only" in prompt
    assert "confidence must be a number from 0 to 1" in prompt


def test_parse_alert_decision_handles_strategy_and_non_strategy_json():
    strategy = parse_alert_decision(
        '{"is_strategy":true,"strategy_kind":"entry","confidence":0.72,"kol_label":"Trader A","reason_short":"entry range"}'
    )
    non_strategy = parse_alert_decision(
        '{"is_strategy":false,"strategy_kind":"other","confidence":0.2,"kol_label":"","reason_short":"chat"}'
    )

    assert strategy == AlertDecision(
        is_strategy=True,
        strategy_kind="entry",
        confidence=0.72,
        kol_label="Trader A",
        reason_short="entry range",
    )
    assert non_strategy.is_strategy is False
    assert non_strategy.strategy_kind == "other"


def test_parse_alert_decision_maps_textual_confidence_to_number():
    decision = parse_alert_decision(
        '{"is_strategy":true,"strategy_kind":"entry","confidence":"中","kol_label":"舒琴","reason_short":"entry setup"}'
    )

    assert decision.confidence == 0.65


def test_parse_alert_decision_treats_invalid_json_as_error_decision():
    decision = parse_alert_decision("not json")

    assert decision.is_strategy is False
    assert decision.strategy_kind == "error"
    assert decision.confidence == 0.0
    assert "Invalid JSON" in decision.reason_short


def test_format_strategy_alert_message_keeps_original_text_under_group_note():
    message = format_strategy_alert_message(
        chat_title="VIP BTC Room",
        decision=AlertDecision(
            is_strategy=True,
            strategy_kind="exit",
            confidence=0.81,
            kol_label="",
            reason_short="close position",
        ),
        original_text="close BTC long",
    )

    assert message.startswith("KOL群组：VIP BTC Room")
    assert "类型：离场" in message
    assert "原文：\nclose BTC long" in message
    assert "KOL/交易员" not in message
    assert "置信度" not in message


def test_process_strategy_alert_skips_empty_text_without_calling_ai(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record = _record(text=None)
    persist_normalized_messages(session_factory, [record])

    async def fail_llm(*args, **kwargs):
        raise AssertionError("LLM should not be called for empty text")

    result = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            llm_requester=fail_llm,
        )
    )

    assert result["status"] == "skipped_empty_text"
    with session_factory() as session:
        stored = session.query(StrategyAlert).one()
    assert stored.status == "skipped_empty_text"


def test_process_strategy_alert_forwards_once_and_persists_ai_result(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record = _record()
    persist_normalized_messages(session_factory, [record])
    sent_messages: list[str] = []

    captured_prompts: list[str] = []

    async def llm_requester(*args, **kwargs):
        captured_prompts.append(kwargs["prompt"])
        return '{"is_strategy":true,"strategy_kind":"entry","confidence":0.75,"kol_label":"Trader A","reason_short":"entry"}'

    async def bot_sender(*, config, text):
        sent_messages.append(text)

    first = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            llm_requester=llm_requester,
            bot_sender=bot_sender,
        )
    )
    second = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            llm_requester=llm_requester,
            bot_sender=bot_sender,
        )
    )

    assert first["status"] == "sent"
    assert second["status"] == "already_sent"
    assert len(sent_messages) == 1
    assert "chat_title=VIP BTC Room" in captured_prompts[0]
    assert sent_messages[0].startswith("KOL群组：VIP BTC Room")
    assert "原文：\nTrader A\nBTC long now" in sent_messages[0]
    with session_factory() as session:
        stored = session.query(StrategyAlert).one()
        invocation = session.query(AiPromptInvocation).one()
    assert stored.ai_confidence == 0.75
    assert stored.strategy_kind == "entry"
    assert stored.forwarded_at is not None
    assert invocation.feature == "strategy_alert"
    assert invocation.model == "cheap-model"
    assert "strategy.alert.classifier" in invocation.prompt_versions_json


def test_process_strategy_alert_retries_ai_and_bot_failures(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record = _record()
    persist_normalized_messages(session_factory, [record])
    llm_attempts = 0
    bot_attempts = 0

    async def llm_requester(*args, **kwargs):
        nonlocal llm_attempts
        llm_attempts += 1
        if llm_attempts == 1:
            raise httpx.ConnectError("temporary llm outage")
        return '{"is_strategy":true,"strategy_kind":"exit","confidence":0.66,"kol_label":"Trader B","reason_short":"exit"}'

    async def bot_sender(*, config, text):
        nonlocal bot_attempts
        bot_attempts += 1
        if bot_attempts == 1:
            raise httpx.ConnectError("temporary bot outage")

    result = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            llm_requester=llm_requester,
            bot_sender=bot_sender,
        )
    )

    assert result["status"] == "sent"
    assert llm_attempts == 2
    assert bot_attempts == 2


def test_process_strategy_alert_forwards_ai_confirmed_strategy_even_when_confidence_is_low(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record = _record()
    persist_normalized_messages(session_factory, [record])
    sent_messages: list[str] = []

    async def llm_requester(*args, **kwargs):
        return '{"is_strategy":true,"strategy_kind":"entry","confidence":0.5,"kol_label":"Trader A","reason_short":"explicit entry"}'

    async def bot_sender(*, config, text):
        sent_messages.append(text)

    result = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            llm_requester=llm_requester,
            bot_sender=bot_sender,
        )
    )

    assert result["status"] == "sent"
    assert len(sent_messages) == 1


def test_process_strategy_alert_uses_recognition_candidate_for_unified_strategy_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record = _record(text="BTC long 68000-68200 SL 67500 TP 69000/70000")
    persist_normalized_messages(session_factory, [record])
    with session_factory() as session:
        raw_message = session.query(RawMessage).one()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000/70000",
                parse_source="text_ai",
                confidence=0.91,
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=record.chat_id,
                message_id=record.message_id,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=record.posted_at,
                entry_range_low=68000,
                entry_range_high=68200,
                stop_loss=67500,
                take_profit="69000/70000",
            )
        )
        session.commit()
    sent_messages: list[str] = []

    async def fail_llm(*args, **kwargs):
        raise AssertionError("alert should reuse message recognition instead of calling LLM")

    async def bot_sender(*, config, text):
        sent_messages.append(text)

    result = asyncio.run(
        process_strategy_alert_for_record(
            session_factory=session_factory,
            record=record,
            chat_title="VIP BTC Room",
            config=_config(),
            recognition_result=MessageRecognitionResult(
                raw_message_id=1,
                status="是策略",
                reason="AI recognized strategy",
                parse_source="text_ai",
            ),
            llm_requester=fail_llm,
            bot_sender=bot_sender,
        )
    )

    assert result["status"] == "sent"
    assert len(sent_messages) == 1
    assert "【KOL策略提醒】" in sent_messages[0]
    assert "信号类型: 限价入场" in sent_messages[0]
    assert "策略类别: entry" in sent_messages[0]
    assert "群组: VIP BTC Room" in sent_messages[0]
    assert "交易对: BTC" in sent_messages[0]
    assert "方向: 多" in sent_messages[0]
    assert "入场方式: 限价" in sent_messages[0]
    assert "入场价格: 68000-68200" in sent_messages[0]
    assert "止盈价格: 69000/70000" in sent_messages[0]
    assert "止损价格: 67500" in sent_messages[0]
    assert "置信度: 0.91" in sent_messages[0]
    assert "原文:\nBTC long 68000-68200 SL 67500 TP 69000/70000" in sent_messages[0]
    with session_factory() as session:
        stored = session.query(StrategyAlert).one()
    assert stored.status == "sent"
    assert stored.strategy_kind == "entry"
    assert stored.ai_confidence == 0.91


def test_process_strategy_alert_formats_lifecycle_events(tmp_path):
    cases = [
        ("entry_signal", "lifecycle_ai", "entered", None, None, "临时入场", "entry"),
        ("close_signal", "lifecycle_ai", "exited", "kol_signal", None, "临时离场", "exit"),
        ("close_signal", "lifecycle_ai", "exited", "cancelled", None, "取消挂单", "cancel_entry"),
        ("position_update", "lifecycle_ai", "entered", None, "partial_take_profit", "部分止盈", "position_update"),
        ("strategy_correction", "text_ai", "entered", None, "strategy_correction", "策略参数调整", "strategy_correction"),
    ]

    for index, (event_type, parse_source, lifecycle_status, exit_reason, management_action, alert_type, strategy_kind) in enumerate(cases, start=1):
        session_factory = create_session_factory(tmp_path / f"research_{index}.db")
        record = _record(text=f"event {index}")
        record.message_id = 100 + index
        persist_normalized_messages(session_factory, [record])
        with session_factory() as session:
            raw_message = session.query(RawMessage).one()
            session.add(
                SignalCandidate(
                    raw_message_id=raw_message.id,
                    symbol="ETH",
                    side="short",
                    event_type=event_type,
                    entry_text="2330",
                    stop_loss_text="2380",
                    take_profit_text="2200",
                    parse_source=parse_source,
                    confidence=0.88,
                )
            )
            lifecycle = StrategyLifecycle(
                chat_id=record.chat_id,
                message_id=10,
                symbol="ETH",
                side="short",
                lifecycle_status=lifecycle_status,
                exit_reason=exit_reason,
                signal_at=record.posted_at,
                entry_range_low=2300,
                entry_range_high=2330,
                stop_loss=2380,
                take_profit="2200",
                entry_price_actual=2330,
            )
            if event_type == "entry_signal":
                lifecycle.entry_signal_message_id = record.message_id
            elif event_type == "close_signal":
                lifecycle.exit_signal_message_id = record.message_id
            else:
                lifecycle.management_signal_message_id = record.message_id
                lifecycle.management_action = management_action
            session.add(lifecycle)
            session.commit()
        sent_messages: list[str] = []

        async def bot_sender(*, config, text):
            sent_messages.append(text)

        result = asyncio.run(
            process_strategy_alert_for_record(
                session_factory=session_factory,
                record=record,
                chat_title="ETH Room",
                config=_config(),
                recognition_result=MessageRecognitionResult(
                    raw_message_id=1,
                    status="非策略",
                    reason="lifecycle event",
                    parse_source="lifecycle_ai",
                ),
                bot_sender=bot_sender,
            )
        )

        assert result["status"] == "sent"
        assert f"信号类型: {alert_type}" in sent_messages[0]
        assert "方向: 空" in sent_messages[0]
        assert "交易对: ETH" in sent_messages[0]
        if index == 1:
            assert "执行状态: 价格触发，未提交交易所订单" in sent_messages[0]
        with session_factory() as session:
            assert session.query(StrategyAlert).one().strategy_kind == strategy_kind


def test_database_bootstrap_creates_strategy_alerts_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        session.add(
            StrategyAlert(
                chat_id=1,
                message_id=2,
                chat_title="VIP",
                sender_name="Alice",
                original_text="BTC long",
                status="ignored_low_confidence",
            )
        )
        session.commit()

    conn = sqlite3.connect(tmp_path / "research.db")
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_alerts)").fetchall()
    }
    indexes = [
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_alerts)").fetchall()
    ]
    conn.close()

    assert "ai_confidence" in columns
    assert any("chat_id" in index or "message" in index for index in indexes)
