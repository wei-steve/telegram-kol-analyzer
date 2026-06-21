from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import RawMessage, SignalCandidate, StrategyLifecycle
from telegram_kol_research.telegram_bot_commands import (
    format_holding_positions_message,
    format_pending_positions_message,
    split_telegram_message,
)


def test_format_holding_positions_message_lists_groups_and_positions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=7,
            posted_at=datetime(2026, 6, 20, 8, 0, tzinfo=UTC),
            text="BTC long",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
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
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=7,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=datetime(2026, 6, 20, 8, 5, tzinfo=UTC),
                entry_range_low=68000,
                entry_range_high=68200,
                entry_price_actual=68100,
                stop_loss=67500,
                take_profit="69000/70000",
            )
        )
        session.commit()

    text = format_holding_positions_message(
        session_factory=session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="Raw Title",
                    chat_id=100,
                    custom_group_label="VIP BTC Room",
                )
            ]
        ),
    )

    assert "【当前持仓策略】" in text
    assert "共 1 条，涉及 1 个群组" in text
    assert "VIP BTC Room" in text
    assert "BTC 多" in text
    assert "入场: 68100" in text
    assert "止盈: 69000/70000" in text
    assert "止损: 67500" in text


def test_format_holding_positions_message_handles_empty_list(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    text = format_holding_positions_message(
        session_factory=session_factory,
        group_config=GroupConfig(),
    )

    assert text == "【当前持仓策略】\n暂无持仓中的 KOL 策略。"


def test_format_pending_positions_message_lists_groups_and_strategies(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=200,
            message_id=17,
            posted_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
            text="ETH short pending",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
            entry_text="2330-2350",
            stop_loss_text="2380",
            take_profit_text="2250/2200",
            parse_source="text_ai",
            confidence=0.9,
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=200,
                message_id=17,
                symbol="ETH",
                side="short",
                lifecycle_status="pending_entry",
                signal_at=raw_message.posted_at,
                entry_range_low=2330,
                entry_range_high=2350,
                stop_loss=2380,
                take_profit="2250/2200",
            )
        )
        session.commit()

    text = format_pending_positions_message(
        session_factory=session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="Raw Title",
                    chat_id=200,
                    custom_group_label="VIP ETH Room",
                )
            ]
        ),
    )

    assert "【当前待入场策略】" in text
    assert "共 1 条，涉及 1 个群组" in text
    assert "VIP ETH Room" in text
    assert "ETH 空" in text
    assert "挂单/入场区间: 2330-2350" in text
    assert "止盈: 2250/2200" in text
    assert "止损: 2380" in text


def test_format_pending_positions_message_handles_empty_list(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    text = format_pending_positions_message(
        session_factory=session_factory,
        group_config=GroupConfig(),
    )

    assert text == "【当前待入场策略】\n暂无待入场且未入场的 KOL 策略。"


def test_split_telegram_message_keeps_chunks_under_limit():
    text = "\n".join(f"line {index}" for index in range(20))

    chunks = split_telegram_message(text, max_chars=30)

    assert len(chunks) > 1
    assert all(len(chunk) <= 30 for chunk in chunks)
