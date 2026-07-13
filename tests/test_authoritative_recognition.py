import json
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    AuthoritativeAssessment,
    apply_authoritative_assessment,
    assess_message_authoritatively,
    process_authoritative_message,
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


def test_fengge_exit_applies_mimo_while_semantic_review_is_pending(tmp_path, monkeypatch):
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
            prompt_versions={
                "trading.analysis.shared": 11,
                "trading.analysis.mimo_vision": 12,
            },
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must not run in the authoritative path")
        ),
        raising=False,
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.agreement_status == "pending"
    assert assessment.deepseek_payload is None
    assert assessment.differences == []
    assert result.parse_source == "mimo_authoritative"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        assert candidate.event_type == "close_signal"
        assert candidate.symbol == "BTC"
        assert candidate.side == "short"
        assert candidate.parse_source == "mimo_authoritative"
        decision = session.query(RecognitionDecision).one()
        assert decision.authoritative_model == "mimo-v2.5"
        assert decision.agreement_status == "pending"
        assert decision.comparison_status == "pending"
        assert json.loads(decision.prompt_versions_json) == {
            "mimo": {
                "trading.analysis.mimo_vision": 12,
                "trading.analysis.shared": 11,
            },
        }
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert lifecycle.exit_signal_message_id == 8401
        assert lifecycle.management_action == "exit_requested"


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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must not run when MiMo fails")
        ),
        raising=False,
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
        decision = session.query(RecognitionDecision).one()
        assert decision.agreement_status == "authoritative_failed"
        assert decision.comparison_status == "completed"


def test_mimo_failure_after_pending_rerecognition_cancels_stale_review(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=22, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    results = iter(
        [
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={"recognition_result": "是策略"},
                input_kind="text",
                model="mimo-v2.5",
                status="是策略",
                prompt_versions={"trading.analysis.shared": 11},
            ),
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={},
                input_kind="text",
                model="mimo-v2.5",
                status="识别失败",
                error_message="timeout",
                prompt_versions={"trading.analysis.shared": 12},
            ),
        ]
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: next(results),
    )

    assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )

    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.agreement_status == "authoritative_failed"
        assert decision.comparison_status == "completed"
        assert json.loads(decision.prompt_versions_json) == {
            "mimo": {"trading.analysis.shared": 12}
        }


def test_unchanged_rerecognition_reports_completed_without_requeueing_or_deepseek(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=23, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    mimo_result = MimoAuthoritativeResult(
        raw_message_id=raw_id,
        payload={"recognition_result": "非策略"},
        input_kind="text",
        model="mimo-v2.5",
        status="非策略",
        prompt_versions={"trading.analysis.shared": 11},
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: mimo_result,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must remain deferred on re-recognition")
        ),
        raising=False,
    )

    first = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        decision.agreement_status = "agreed"
        decision.comparison_status = "completed"
        decision.auxiliary_payload_json = '{"recognition_result":"非策略"}'
        session.commit()

    second = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )

    assert first.semantic_review_status == "pending"
    assert second.agreement_status == "agreed"
    assert second.semantic_review_status == "completed"
    assert second.deepseek_payload is None
    with session_factory() as session:
        assert session.query(RecognitionDecision).one().comparison_status == "completed"


def test_process_authoritative_message_persists_pending_before_mimo_and_auto_trade(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=3, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    events: list[str] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: events.append("mimo")
        or MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={
                "recognition_result": "非策略",
                "lifecycle_event": {"event_type": "exit_position"},
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
            prompt_versions={"trading.analysis.shared": 11},
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek auxiliary was invoked synchronously")
        ),
        raising=False,
    )
    from telegram_kol_research import authoritative_recognition

    real_save_pending = authoritative_recognition.save_pending_authoritative_decision

    def save_pending(*args, **kwargs):
        events.append("persist_pending")
        return real_save_pending(*args, **kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.save_pending_authoritative_decision",
        save_pending,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: events.append("apply_mimo")
        or SimpleNamespace(status="非策略"),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._has_current_mimo_candidate",
        lambda *args, **kwargs: True,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=lambda message_id: events.append("auto_trade")
        or {"status": "executed", "reason": "close_submitted"},
    )

    assert events == ["mimo", "persist_pending", "apply_mimo", "auto_trade"]
    assert result.assessment.agreement_status == "pending"
    assert result.assessment.deepseek_payload is None
    assert result.automation == {"status": "executed", "reason": "close_submitted"}


def test_process_authoritative_message_skips_auto_trade_when_mimo_fails(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=4, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    assessment = AuthoritativeAssessment(
        raw_message_id=raw_id,
        mimo=MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="timeout",
        ),
        deepseek_payload=None,
        agreement_status="authoritative_failed",
        differences=[],
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.assess_message_authoritatively",
        lambda *args, **kwargs: assessment,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )
    auto_trade_calls: list[int] = []

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=auto_trade_calls.append,
    )

    assert auto_trade_calls == []
    assert result.automation == {
        "status": "skipped",
        "reason": "mimo_authoritative_failed",
    }


def test_mimo_non_strategy_never_executes_stale_deepseek_candidate(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=5, text="只是复盘，不是新策略")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                event_type="entry_signal",
                symbol="BTC",
                side="long",
                parse_source="text_ai",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = raw.id

    payload = {
        "recognition_result": "非策略",
        "reason": "MiMo判定为复盘",
        "strategy": {},
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "只是复盘，不是新策略", "image_quality": "none"},
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=payload,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale DeepSeek candidates must not be refreshed")
        ),
        raising=False,
    )
    auto_trade_calls: list[int] = []

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=auto_trade_calls.append,
    )

    assert auto_trade_calls == []
    assert result.automation == {"status": "skipped", "reason": "mimo_no_action"}
