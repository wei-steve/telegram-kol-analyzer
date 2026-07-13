from datetime import UTC, datetime

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    apply_authoritative_assessment,
    assess_message_authoritatively,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult


def test_fengge_exit_uses_mimo_when_deepseek_disagrees(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        entry = RawMessage(chat_id=-1002409877375, message_id=8398, text="BTC short")
        exit_message = RawMessage(
            chat_id=-1002409877375,
            message_id=8401,
            text="现价62800附近出局，空仓等待。",
            posted_at=datetime(2026, 7, 13, 4, 21, 50, tzinfo=UTC),
        )
        session.add_all([entry, exit_message])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-1002409877375,
            message_id=8398,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 1, 59, tzinfo=UTC),
            entered_at=datetime(2026, 7, 13, 1, 59, 30, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:-1002409877375",
            chat_id=-1002409877375,
            message_id=8398,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="1001124071572031",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.commit()
        raw_id = exit_message.id
        lifecycle_id = lifecycle.id

    mimo_payload = {
        "recognition_result": "非策略",
        "reason": "当前消息要求出局",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "exit_price": 62800,
            "confidence": 0.95,
            "reason": "现价出局",
        },
        "input_reading": {
            "observed_text": "现价62800附近出局，空仓等待。",
            "image_quality": "none",
        },
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=mimo_payload,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: {
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {"event_type": "none", "confidence": 0.1},
            "confidence": 0.8,
        },
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.agreement_status == "disagreed"
    assert "lifecycle_event.event_type" in assessment.differences
    assert result.parse_source == "mimo_authoritative"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        assert candidate.event_type == "close_signal"
        assert candidate.symbol == "BTC"
        assert candidate.side == "short"
        assert candidate.parse_source == "mimo_authoritative"
        decision = session.query(RecognitionDecision).one()
        assert decision.authoritative_model == "mimo-v2.5"
        assert decision.agreement_status == "disagreed"


def test_mimo_failure_never_applies_deepseek_action(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="timeout",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: {
            "recognition_result": "是策略",
            "strategy": {"symbol": "BTC", "side": "short", "entry": "market"},
            "lifecycle_event": {"event_type": "none"},
            "confidence": 0.99,
        },
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.agreement_status == "authoritative_failed"
    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
