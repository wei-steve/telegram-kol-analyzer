from datetime import UTC, datetime

import httpx
import pytest

from telegram_kol_research.ai_recognition_config import AiProviderConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import (
    _apply_lifecycle_event_decision,
    _ensure_lifecycle_record,
    _result_from_ai_payload,
    _upsert_ai_signal_candidate,
    recognize_message_now,
)
from telegram_kol_research.models import (
    MediaAsset,
    MessageRecognition,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeIdea,
)


def _mock_deepseek_lifecycle_event(monkeypatch, payload, *, seen_requests=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if seen_requests is not None:
                seen_requests.append(json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(payload, ensure_ascii=False)
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)


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

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "是策略"
    assert "BTC long" in result.summary
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert candidate.entry_text == "68000-68200"
    assert recognition.status == "是策略"


def test_ai_strategy_payload_normalizes_targets_and_backfills_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = {
        "recognition_result": "是策略",
        "reason": "has entry, stop loss and take profits",
        "strategy": {
            "symbol": "btc",
            "side": "long",
            "entry": "Entry: 62400 nearby",
            "stop_loss": "SL: 60800",
            "take_profit": ["TP: 63600", "64800"],
        },
        "confidence": 0.91,
    }

    result = _result_from_ai_payload(
        raw_message_id=1,
        payload=payload,
        parse_source="text_ai",
    )

    assert "BTC long" in (result.summary or "")
    assert "Entry 62400nearby" in (result.summary or "")
    assert "TP 63600/64800" in (result.summary or "")

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9,
            posted_at=datetime(2026, 6, 14, tzinfo=UTC),
            text="BTC long Entry 62400 nearby SL 60800 TP 63600/64800",
        )
        session.add(raw_message)
        session.flush()
        candidate = _upsert_ai_signal_candidate(
            session,
            raw_message,
            strategy=payload["strategy"],
            confidence=0.91,
            parse_source="text_ai",
        )
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=raw_message.chat_id,
            message_id=raw_message.message_id,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=raw_message.posted_at,
        )
        session.add(lifecycle)
        session.flush()

        _ensure_lifecycle_record(session, raw_message, candidate)

        assert candidate.symbol == "BTC"
        assert candidate.side == "long"
        assert candidate.entry_text == "62400nearby"
        assert candidate.stop_loss_text == "60800"
        assert candidate.take_profit_text == "63600/64800"
        assert lifecycle.entry_range_low == 62400
        assert lifecycle.entry_range_high == 62400
        assert lifecycle.stop_loss == 60800
        assert lifecycle.take_profit == "63600/64800"


def test_ai_text_recognition_preserves_labeled_entry_price_when_model_returns_market_only(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    source_text = (
        "\U0001f3c6 \u300e1000u\u51b2\u523a100w\u5343\u500d\u7ffb\u4ed3\u300f \U0001f3c6\n"
        "\u4ea4\u6613\u6807\u7684\uff1aEth(\u5e02\u4ef7\u8fdb\u573a)\n"
        "\u8fdb\u573a\u65b9\u5411\uff1a\u7a7a\n"
        "\u8fdb\u573a\u70b9\u4f4d\uff1a1730\u9644\u8fd1\n"
        "\u6b62\u76c8\u9884\u8ba1\uff1a1650\n"
        "\u6b62\u635f\u9884\u8ba1\uff1a1765"
    )
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2167,
            posted_at=datetime(2026, 6, 22, 11, 2, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    payload = {
        "recognition_result": "\u662f\u7b56\u7565",
        "reason": "\u660e\u786e\u4ea4\u6613\u6807\u7684ETH\u3001\u505a\u7a7a\u65b9\u5411\u3001\u5e02\u4ef7\u8fdb\u573a\u3001\u6b62\u635f1765\u3001\u6b62\u76c81650",
        "strategy": {
            "symbol": "ETH",
            "side": "short",
            "entry": "\u5e02\u4ef7\u8fdb\u573a",
            "stop_loss": "1765",
            "take_profit": "1650",
            "leverage": None,
            "order_type": "market",
        },
        "confidence": 0.95,
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(payload, ensure_ascii=False)
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    expected_entry = "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1"
    assert result.status == "\u662f\u7b56\u7565"
    assert f"Entry {expected_entry}" in (result.summary or "")
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        lifecycle = session.query(StrategyLifecycle).one()
        recognition = session.query(MessageRecognition).one()

    assert candidate.entry_text == expected_entry
    assert lifecycle.entry_range_low == 1730
    assert lifecycle.entry_range_high == 1730
    assert f"Entry {expected_entry}" in (recognition.summary or "")


def test_ensure_lifecycle_record_deduplicates_recent_active_same_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_payload = {
        "symbol": "ETH",
        "side": "short",
        "entry": "1710/1788",
        "stop_loss": "1850",
        "take_profit": "第一止盈1673（70%仓位），第二止盈1618",
    }
    duplicate_payload = {
        "symbol": "ETH",
        "side": "short",
        "entry": "1710-1788",
        "stop_loss": "1850",
        "take_profit": "1673/1618",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=12918,
            posted_at=datetime(2026, 6, 19, 1, 30, 50, tzinfo=UTC),
            text="ETH 1710市价直接空 再挂1788 止盈1673/1618 止损1850",
        )
        duplicate_message = RawMessage(
            chat_id=88,
            message_id=12924,
            posted_at=datetime(2026, 6, 19, 13, 46, 42, tzinfo=UTC),
            text="ETH 1710市价直接空 再挂1788 止盈1673/1618 止损1850",
        )
        session.add_all([first_message, duplicate_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=first_payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        first_lifecycle.entry_price_actual = 1710
        duplicate_candidate = _upsert_ai_signal_candidate(
            session,
            duplicate_message,
            strategy=duplicate_payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        duplicate_lifecycle = _ensure_lifecycle_record(
            session,
            duplicate_message,
            duplicate_candidate,
        )
        session.flush()

        assert duplicate_lifecycle.id == first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 1
        assert duplicate_candidate.event_type == "duplicate_entry_signal"
        assert "Duplicate active strategy lifecycle" in duplicate_candidate.review_note


def test_ensure_lifecycle_record_applies_active_entry_correction(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64600-69000",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }
    correction_payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64600-64900",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9079,
            posted_at=datetime(2026, 6, 22, 11, 57, 47, tzinfo=UTC),
            text="BTC 64600-69000附近做空 止损66100 止盈62300/61200",
        )
        correction_message = RawMessage(
            chat_id=88,
            message_id=9080,
            posted_at=datetime(2026, 6, 22, 12, 18, 46, tzinfo=UTC),
            text="BTC 64600-64900附近做空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, correction_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=first_payload,
            confidence=0.95,
            parse_source="glm_ocr_image",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        correction_candidate = _upsert_ai_signal_candidate(
            session,
            correction_message,
            strategy=correction_payload,
            confidence=0.95,
            parse_source="glm_ocr_image",
        )
        correction_lifecycle = _ensure_lifecycle_record(
            session,
            correction_message,
            correction_candidate,
        )
        session.flush()

        assert correction_lifecycle.id == first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 1
        assert first_lifecycle.entry_range_low == 64600
        assert first_lifecycle.entry_range_high == 64900
        assert first_lifecycle.management_signal_message_id == 9080
        assert first_lifecycle.management_action == "strategy_correction"
        assert "64600-69000" in (first_lifecycle.management_note or "")
        assert "64600-64900" in (first_lifecycle.management_note or "")
        assert correction_candidate.event_type == "strategy_correction"
        assert "Strategy correction" in (correction_candidate.review_note or "")


def test_ensure_lifecycle_record_allows_reentry_after_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64900",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9080,
            posted_at=datetime(2026, 6, 22, 12, 18, 46, tzinfo=UTC),
            text="BTC 64900做空 止损66100 止盈62300/61200",
        )
        reentry_message = RawMessage(
            chat_id=88,
            message_id=9090,
            posted_at=datetime(2026, 6, 22, 15, 0, tzinfo=UTC),
            text="价格又反弹到64900 继续空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, reentry_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "exited"
        first_lifecycle.exit_reason = "take_profit"
        first_lifecycle.entered_at = first_message.posted_at
        first_lifecycle.exited_at = datetime(2026, 6, 22, 13, 0, tzinfo=UTC)
        session.flush()
        reentry_candidate = _upsert_ai_signal_candidate(
            session,
            reentry_message,
            strategy=payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        reentry_lifecycle = _ensure_lifecycle_record(
            session,
            reentry_message,
            reentry_candidate,
        )
        session.flush()

        assert reentry_lifecycle.id != first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 2
        assert reentry_lifecycle.lifecycle_status == "pending_entry"
        assert reentry_candidate.event_type == "entry_signal"
        assert reentry_candidate.review_note is None


def test_ensure_lifecycle_record_keeps_distinct_active_entry_range(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9100,
            posted_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
            text="BTC 64600-64900做空 止损66100 止盈62300/61200",
        )
        second_message = RawMessage(
            chat_id=88,
            message_id=9101,
            posted_at=datetime(2026, 6, 22, 12, 30, tzinfo=UTC),
            text="BTC 65000-65300做空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, second_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy={
                "symbol": "BTC",
                "side": "short",
                "entry": "64600-64900",
                "stop_loss": "66100",
                "take_profit": "62300/61200",
            },
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        second_candidate = _upsert_ai_signal_candidate(
            session,
            second_message,
            strategy={
                "symbol": "BTC",
                "side": "short",
                "entry": "65000-65300",
                "stop_loss": "66100",
                "take_profit": "62300/61200",
            },
            confidence=0.95,
            parse_source="text_ai",
        )
        second_lifecycle = _ensure_lifecycle_record(
            session,
            second_message,
            second_candidate,
        )
        session.flush()

        assert second_lifecycle.id != first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 2
        assert second_candidate.event_type == "entry_signal"
        assert second_candidate.review_note is None


def test_recognize_message_now_marks_plain_text_as_not_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=2, text="今天市场波动很大")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

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

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

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

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

    assert result.status == "非策略"
    assert result.reason == "未识别到可执行新入场策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_closes_matching_short_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        trade_idea = TradeIdea(
            chat_id=88,
            symbol="BTC",
            side="short",
            status="open",
            created_at=datetime(2026, 6, 15, 13, 43, tzinfo=UTC),
        )
        session.add(trade_idea)
        session.flush()
        lifecycle = StrategyLifecycle(
            trade_idea_id=trade_idea.id,
            chat_id=88,
            message_id=430,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 15, 13, 43, tzinfo=UTC),
            entered_at=datetime(2026, 6, 16, 0, 32, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=435,
            posted_at=datetime(2026, 6, 17, 1, 32, tzinfo=UTC),
            text="当前价格接近成本价：65540，空单全部平仓！整体亏损170点左右吧！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id
        trade_idea_id = trade_idea.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    assert result.reason == "本地规则识别到明确入场/取消/离场消息，已更新匹配的策略状态。"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        trade_idea = session.get(TradeIdea, trade_idea_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 435
    assert trade_idea.status == "closed"
    assert candidate.event_type == "close_signal"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"


def test_recognize_message_now_cancels_recent_pending_limit_order(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            text="取消限价，等我后续信号！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert lifecycle.exit_signal_message_id == 376
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "cancel_heuristic"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"


def test_ai_lifecycle_event_cancels_pending_order(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            text="取消限价，等我后续信号！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "cancel_entry",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.92,
            "reason": "当前消息取消前面的限价挂单",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert lifecycle.exit_signal_message_id == 376
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_confirms_market_entry(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="BTC 现价 63320 入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "entry_confirm",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": 63320,
            "exit_price": None,
            "confidence": 0.93,
            "reason": "当前消息确认按现价入场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual == 63320
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_exits_entered_position(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            entry_price_actual=63320,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=378,
            posted_at=datetime(2026, 6, 19, 10, 5, tzinfo=UTC),
            text="先临时离场，等下一步通知",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.9,
            "reason": "当前消息要求临时离场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 378
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_treats_breakeven_exit_as_position_exit(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9024,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 18, 15, 57, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 15, 58, tzinfo=UTC),
            entry_price_actual=62400,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9030,
            posted_at=datetime(2026, 6, 19, 11, 15, tzinfo=UTC),
            text="目前还在成本附近，保本出局。",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.91,
            "reason": "当前消息说明成本附近保本出局，应关闭已有持仓策略",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 9030
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_records_partial_take_profit_update(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1395,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 17, 10, 26, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
            entry_price_actual=63794.4,
            stop_loss=61000,
            take_profit="65500/66500/67500",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, tzinfo=UTC),
            text="大饼反弹一般，现价64500附近提前止盈一半带保护，整体思路还是高抛低吸为主",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "management_action": "partial_take_profit",
            "confidence": 0.92,
            "reason": "当前消息要求提前止盈一半并带保护，属于持仓管理更新",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id == 1400
    assert lifecycle.management_action == "partial_take_profit"
    assert lifecycle.stop_loss == 63794.4
    assert "提前止盈一半" in lifecycle.management_note
    assert "止损已调整到成本保护价 63794.4" in lifecycle.management_note
    assert candidate.event_type == "position_update"
    assert candidate.parse_source == "lifecycle_ai"
    assert candidate.stop_loss_text == "63794.4"


def test_ai_lifecycle_event_explicit_stop_overrides_protection_price(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2176,
            symbol="ETH",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 22, 12, 54, 36, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 12, 54, 41, tzinfo=UTC),
            entry_price_actual=1760,
            stop_loss=1760,
            take_profit="1845",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2182,
            posted_at=datetime(2026, 6, 22, 15, 50, 42, tzinfo=UTC),
            text="设置好止盈止损持仓过夜！止盈位：1845！！！止损位：1725！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "ETH",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "stop_loss": "1725",
            "take_profit": "1845",
            "management_action": "risk_update",
            "confidence": 0.94,
            "reason": "当前消息明确要求设置止盈1845、止损1725并持仓过夜，属于持仓风控更新",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id == 2182
    assert lifecycle.management_action == "risk_update"
    assert lifecycle.stop_loss == 1725
    assert lifecycle.take_profit == "1845"
    assert "止损已按 KOL 明确指令调整为 1725" in lifecycle.management_note
    assert candidate.event_type == "position_update"
    assert candidate.stop_loss_text == "1725"
    assert candidate.take_profit_text == "1845"


def test_ai_lifecycle_event_extracts_explicit_stop_from_management_text(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9118,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 23, 8, 20, 54, tzinfo=UTC),
            entered_at=datetime(2026, 6, 23, 8, 24, tzinfo=UTC),
            entry_price_actual=62214,
            stop_loss=62214,
            take_profit="66500",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9123,
            posted_at=datetime(2026, 6, 23, 16, 11, 37, tzinfo=UTC),
            text="目前已经东八区凌晨12点，做短线收益700点可以全部止盈出局，剩余仓位过夜持仓做好成本保护，止损修改入场价62000附近。",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "stop_loss": None,
            "take_profit": None,
            "management_action": "risk_update",
            "confidence": 0.94,
            "reason": "当前消息要求剩余仓位继续持有并做成本保护，属于持仓风险更新。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id == 9123
    assert lifecycle.management_action == "risk_update"
    assert lifecycle.stop_loss == 62000
    assert candidate.event_type == "position_update"
    assert candidate.stop_loss_text == "62000"


def test_lifecycle_event_ignores_stop_update_after_protective_stop_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2176,
            symbol="ETH",
            side="long",
            lifecycle_status="exited",
            exit_reason="stop_loss",
            signal_at=datetime(2026, 6, 22, 12, 54, 36, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 12, 54, 41, tzinfo=UTC),
            exited_at=datetime(2026, 6, 22, 14, 20, tzinfo=UTC),
            entry_price_actual=1760,
            exit_price_actual=1760,
            stop_loss=1760,
            take_profit="1845",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2182,
            posted_at=datetime(2026, 6, 22, 15, 50, 42, tzinfo=UTC),
            text="设置好止盈止损持仓过夜！止盈位：1845！！！止损位：1725！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "ETH",
                "side": "long",
                "stop_loss": "1725",
                "take_profit": "1845",
                "management_action": "risk_update",
                "confidence": 0.94,
                "reason": "当前消息明确要求设置止盈1845、止损1725并持仓过夜",
            },
        )

        assert applied is False
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "stop_loss"
        assert lifecycle.stop_loss == 1760
        assert lifecycle.exited_at == datetime(2026, 6, 22, 14, 20, tzinfo=UTC)
        assert lifecycle.management_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_ai_lifecycle_event_records_scaled_take_profit_percentage_update(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
            entry_price_actual=1705,
            stop_loss=1740,
            take_profit="1620",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2131,
            posted_at=datetime(2026, 6, 19, 10, 12, tzinfo=UTC),
            text="现目前空单获利16个点！\n持仓收益达到100％！\n分批止盈30％！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "ETH",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "management_action": "partial_take_profit",
            "confidence": 0.93,
            "reason": "当前消息说明空单已盈利并要求分批止盈30%，属于已有持仓的部分止盈管理。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id == 2131
    assert lifecycle.management_action == "partial_take_profit"
    assert "分批止盈30%" in lifecycle.management_note
    assert candidate.event_type == "position_update"
    assert candidate.parse_source == "lifecycle_ai"


def test_recognize_message_now_confirms_recent_pending_market_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="BTC 现价 63320 入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at == datetime(2026, 6, 19, 9, 40)
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual == 63320
    assert candidate.event_type == "entry_signal"
    assert candidate.parse_source == "entry_confirm_heuristic"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"
    assert candidate.entry_text == "63320"


def test_recognize_message_now_confirms_unique_pending_entry_without_symbol(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="现价入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual is None
    assert candidate.parse_source == "entry_confirm_heuristic"


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

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

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

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

    assert result.status == "待识别"
    assert result.reason == "图片识别等待 OCR/AI 接入"


def test_glm_ocr_caption_message_falls_back_to_text_strategy(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    image_path = tmp_path / "chart.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    source_text = (
        "以太币 1555-1535 这里 可以考虑做多\n"
        "止损：15分钟有效跌破1520\n"
        "止盈：1575-1600-1625-1640-1675\n"
        "今天策略已经都盈利了，正常所长就不做单了，给各位一个参考"
    )
    seen_chat_inputs: list[str] = []

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=6699,
            sender_name="币圈所长会员群-11分组",
            posted_at=datetime(2026, 6, 26, 14, 4, 27, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if url.endswith("/layout_parsing"):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "md_results": (
                            "<table><tr><td>1641.54</td></tr>"
                            "<tr><td>1624.78</td></tr></table>"
                        )
                    },
                )
            seen_chat_inputs.append(json["messages"][1]["content"])
            if len(seen_chat_inputs) == 1:
                payload = {
                    "recognition_result": "非策略",
                    "reason": "OCR 表格缺少方向和完整入场说明",
                    "strategy": {},
                    "confidence": 0.3,
                }
            else:
                payload = {
                    "recognition_result": "是策略",
                    "reason": "caption 含 ETH 做多、入场、止损和止盈",
                    "strategy": {
                        "symbol": "ETH",
                        "side": "long",
                        "entry": "1555-1535",
                        "stop_loss": "1520",
                        "take_profit": "1575/1600/1625/1640/1675",
                        "order_type": "limit",
                    },
                    "confidence": 0.91,
                }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(
                                    payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="http://deepseek.test",
                model="deepseek-chat",
                timeout_seconds=10,
            ),
            image_provider=AiProviderConfig(
                base_url="http://glm.test",
                model="glm-ocr",
                timeout_seconds=10,
            ),
        ),
    )

    assert result.status == "是策略"
    assert result.parse_source == "text_ai"
    assert len(seen_chat_inputs) == 2
    assert "<table>" in seen_chat_inputs[0]
    assert seen_chat_inputs[1] == source_text
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
        media_asset = session.query(MediaAsset).one()

    assert candidate.symbol == "ETH"
    assert candidate.side == "long"
    assert candidate.entry_text == "1555-1535"
    assert candidate.parse_source == "text_ai"
    assert recognition.status == "是策略"
    assert media_asset.ocr_text.startswith("<table>")


def test_glm_ocr_recap_caption_does_not_import_old_screenshot_strategy(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    image_path = tmp_path / "hype.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    source_text = (
        "💵💵 HYPE 会员空单 盈利 各位也可以做个参考，"
        "目前看四小时这个阴线只要无法突破，那么三日线传导的双顶部"
        "是还有可能继续下跌的。"
    )
    seen_chat_inputs: list[str] = []

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=6694,
            sender_name="币圈所长会员群-11分组",
            posted_at=datetime(2026, 6, 26, 13, 31, 23, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if url.endswith("/layout_parsing"):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "md_results": (
                            "HYPE 现在63.6附近开空\n"
                            "止损：站上65.5\n"
                            "止盈；62-60-58-56"
                        )
                    },
                )
            seen_chat_inputs.append(json["messages"][1]["content"])
            payload = {
                "recognition_result": "是策略",
                "reason": "合并文本包含 HYPE 做空、入场、止损和止盈",
                "strategy": {
                    "symbol": "HYPE",
                    "side": "short",
                    "entry": "63.6附近",
                    "stop_loss": "65.5",
                    "take_profit": "62/60/58/56",
                    "order_type": "market",
                },
                "confidence": 0.95,
            }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(
                                    payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="http://deepseek.test",
                model="deepseek-chat",
                timeout_seconds=10,
            ),
            image_provider=AiProviderConfig(
                base_url="http://glm.test",
                model="glm-ocr",
                timeout_seconds=10,
            ),
        ),
    )

    assert result.status == "非策略"
    assert "历史截图" in (result.reason or "")
    assert len(seen_chat_inputs) == 1
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(StrategyLifecycle).count() == 0
        recognition = session.query(MessageRecognition).one()
        media_asset = session.query(MediaAsset).one()

    assert recognition.status == "非策略"
    assert "HYPE 现在63.6附近开空" in (media_asset.ocr_text or "")


def test_recognize_message_now_raises_for_missing_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with pytest.raises(LookupError, match="raw message not found"):
        recognize_message_now(session_factory, raw_message_id=999)
