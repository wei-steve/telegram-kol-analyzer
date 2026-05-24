import asyncio
import sqlite3
from datetime import UTC, datetime

import httpx

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import StrategyAlert
from telegram_kol_research.raw_ingest import NormalizedMessageRecord, persist_normalized_messages
from telegram_kol_research.strategy_alerts import (
    AlertDecision,
    StrategyAlertConfig,
    build_strategy_alert_prompt,
    format_strategy_alert_message,
    parse_alert_decision,
    process_strategy_alert_for_record,
)


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

    async def llm_requester(*args, **kwargs):
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
    assert sent_messages[0].startswith("KOL群组：VIP BTC Room")
    assert "原文：\nTrader A\nBTC long now" in sent_messages[0]
    with session_factory() as session:
        stored = session.query(StrategyAlert).one()
    assert stored.ai_confidence == 0.75
    assert stored.strategy_kind == "entry"
    assert stored.forwarded_at is not None


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
