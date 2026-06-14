from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import recognize_message_now
from telegram_kol_research.models import MediaAsset, MessageRecognition, RawMessage, SignalCandidate


def test_recognize_message_now_persists_text_strategy_candidate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=1,
            posted_at=datetime(2026, 6, 14, tzinfo=UTC),
            text="BTC long 68000-68200 SL 67500 TP 69000/70000 20x",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "是策略"
    assert "BTC long" in result.summary
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert candidate.entry_text == "68000-68200"
    assert recognition.status == "是策略"


def test_recognize_message_now_marks_plain_text_as_not_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=2, text="今天市场波动很大")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    assert result.summary is None
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        recognition = session.query(MessageRecognition).one()
    assert recognition.status == "非策略"
    assert recognition.reason == "未识别到可执行新入场策略"


def test_recognize_message_now_rejects_single_direction_hint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=6, text="多单继续持有，注意风险")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_rejects_position_management_update(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=7,
            text=(
                "🔥注意，现目前多单略微浮亏中…\n"
                "🔥多单继续持有，设置好止损点！\n"
                "@Tarderfengge QQ:158241758"
            ),
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    assert result.reason == "未识别到可执行新入场策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_rejects_trading_education_content(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=8,
            text=(
                "今天讲一个合约短线交易里面非常重要的知识点，不要逆势加仓。\n"
                "趋势对的时候可以考虑扩大盈利，趋势错的时候首先考虑控制风险。"
            ),
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_skips_video_media(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=3, text="视频复盘")
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediadocument",
                mime_type="video/mp4",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    assert result.reason == "视频消息默认跳过"


def test_recognize_message_now_keeps_image_pending_for_later_ocr(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=4, text="")
        session.add(raw_message)
        session.flush()
        session.add(MediaAsset(raw_message_id=raw_message.id, kind="messagemediaphoto"))
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "待识别"
    assert result.reason == "图片识别等待 OCR/AI 接入"


def test_recognize_message_now_raises_for_missing_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with pytest.raises(LookupError, match="raw message not found"):
        recognize_message_now(session_factory, raw_message_id=999)
