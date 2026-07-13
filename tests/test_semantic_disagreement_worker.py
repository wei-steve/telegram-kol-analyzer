import asyncio
import json
from datetime import datetime, timedelta

import pytest

from telegram_kol_research.ai_recognition_config import AiProviderConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import claim_next_semantic_review
import telegram_kol_research.semantic_disagreement_review as review_module
from telegram_kol_research.semantic_disagreement_review import (
    SemanticReviewRun,
    decide_semantic_severity,
    run_semantic_review_loop,
    run_semantic_review_once,
)


NOW = datetime(2026, 7, 13, 12, 0)


def _review_payload(*, action_type="none", critical=False):
    return {
        "independent_action": {
            "action_type": action_type,
            "target_lifecycle_id": None,
            "symbol": None,
            "side": None,
            "stop_loss": None,
            "take_profit": None,
            "management_action": None,
        },
        "evidence": ["全部出局"] if critical else [],
        "conflict_types": ["urgent_exit_missed"] if critical else [],
        "material_disagreement": critical,
        "suggested_severity": "critical" if critical else "none",
        "confidence": 0.95,
        "reason": "reviewed",
    }


def _setup(tmp_path, *, text="BTC short", automation_status="submitted"):
    factory = create_session_factory(tmp_path / "research.db")
    mimo = {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {"event_type": "none"},
        "input_reading": {"observed_text": text, "image_quality": "none"},
    }
    with factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text=text)
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(mimo, ensure_ascii=False),
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status="pending",
                automation_status=automation_status,
                automation_reason="preserve me",
            )
        )
        session.commit()
        return factory, raw.id, mimo


def _run(raw_id, mimo, payload=None):
    payload = payload or _review_payload()
    decision = decide_semantic_severity(
        mimo_payload=mimo,
        review_payload=payload,
        automation={"status": "submitted", "reason": "preserve me"},
        input_kind="text",
        current_message_text="BTC short",
    )
    return SemanticReviewRun(
        raw_message_id=raw_id,
        model="deepseek-review",
        review_payload=payload,
        auxiliary_payload={"recognition_result": "非策略"},
        decision=decision,
        prompt_versions={"trading.disagreement.semantic_review": 8},
    )


def test_worker_completes_pending_review(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path)

    assert asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=None,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo),
        now=NOW,
    )) is True

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "completed"
        assert row.comparison_model == "deepseek-review"
        assert row.automation_status == "submitted"
        assert row.automation_reason == "preserve me"


def test_worker_retries_timeout_without_touching_automation(tmp_path):
    factory, _, _ = _setup(tmp_path)
    failure_clock = [NOW]

    def fail_after_elapsed(*args, **kwargs):
        failure_clock[0] += timedelta(seconds=100)
        raise TimeoutError("slow")

    async def run_at(when):
        return await run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=None,
            reviewer=fail_after_elapsed,
            now=when,
            now_provider=lambda: failure_clock[0],
            retry_delay_seconds=10,
        )

    assert asyncio.run(run_at(NOW)) is True
    assert asyncio.run(run_at(NOW + timedelta(seconds=110))) is True
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "pending"
        assert row.comparison_attempts == 2
        assert row.comparison_next_attempt_at == NOW + timedelta(seconds=220)
        assert row.automation_status == "submitted"
        assert row.automation_reason == "preserve me"


def test_worker_marks_invalid_json_failed_after_three_attempts(tmp_path):
    factory, _, _ = _setup(tmp_path)

    for when in (NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=3)):
        assert asyncio.run(run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=None,
            reviewer=lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid JSON")),
            now=when,
            now_provider=lambda when=when: when,
            retry_delay_seconds=1,
            max_attempts=3,
        )) is True

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "failed"
        assert row.comparison_attempts == 3
        assert row.comparison_next_attempt_at is None


def test_worker_recovers_stale_running_claim(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path)
    old = claim_next_semantic_review(
        factory, now=NOW, stale_before=NOW - timedelta(minutes=5)
    )
    assert old is not None

    assert asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=None,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo),
        now=NOW + timedelta(minutes=10),
        stale_after=timedelta(minutes=5),
    )) is True

    with factory() as session:
        assert session.query(RecognitionDecision).one().comparison_status == "completed"


def test_worker_notifies_only_critical(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    notified = []
    critical_payload = _review_payload(action_type="exit_full", critical=True)
    critical_run = _run(raw_id, mimo, critical_payload)

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs["raw_message_id"]),
        reviewer=lambda *args, **kwargs: critical_run,
        now=NOW,
    ))
    assert notified == [raw_id]

    second_factory, second_id, second_mimo = _setup(tmp_path / "normal")
    asyncio.run(run_semantic_review_once(
        second_factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs["raw_message_id"]),
        reviewer=lambda *args, **kwargs: _run(second_id, second_mimo),
        now=NOW,
    ))
    assert notified == [raw_id]


def test_worker_does_not_duplicate_claimed_notification(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    notified = []
    payload = _review_payload(action_type="exit_full", critical=True)

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs["raw_message_id"]),
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW,
    ))
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        row.comparison_status = "pending"
        row.comparison_next_attempt_at = None
        session.commit()
    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs["raw_message_id"]),
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW + timedelta(minutes=1),
    ))
    assert notified == [raw_id]


def test_loop_reloads_config_and_survives_one_item_failure(tmp_path, monkeypatch):
    factory, _, _ = _setup(tmp_path)
    loads = []
    attempts = []

    def fake_load(path):
        loads.append(path)
        return AiRecognitionConfig()

    async def fake_once(*args, **kwargs):
        attempts.append(kwargs["config"])
        if len(attempts) == 1:
            raise ValueError("one bad item")
        raise asyncio.CancelledError

    monkeypatch.setattr(review_module, "load_ai_recognition_config", fake_load)
    monkeypatch.setattr(review_module, "run_semantic_review_once", fake_once)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_semantic_review_loop(
                session_factory=factory,
                config_path=tmp_path / "ai.yaml",
                notifier=None,
                poll_interval_seconds=0,
            )
        )

    assert len(attempts) == 2
    assert loads == [tmp_path / "ai.yaml", tmp_path / "ai.yaml"]
