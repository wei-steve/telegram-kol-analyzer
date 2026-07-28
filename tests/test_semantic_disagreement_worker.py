import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

import httpx
import pytest

from telegram_kol_research.ai_recognition_config import AiProviderConfig, AiRecognitionConfig
from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.recognition_decisions import claim_next_semantic_review
import telegram_kol_research.semantic_disagreement_review as review_module
from telegram_kol_research.semantic_disagreement_review import (
    SemanticReviewDecision,
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


def test_worker_retry_exhaustion_is_adapted_after_failed_state_commits(
    tmp_path,
    monkeypatch,
):
    factory, raw_id, _ = _setup(tmp_path)
    calls = []

    def capture(adapter, *args, **kwargs):
        with factory() as session:
            assert session.query(RecognitionDecision).one().comparison_status == "failed"
        calls.append((adapter, args, kwargs))

    monkeypatch.setattr(
        review_module,
        "capture_runtime_incident_best_effort",
        capture,
    )

    asyncio.run(
        run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=None,
            reviewer=lambda *args, **kwargs: (_ for _ in ()).throw(
                TimeoutError("provider timeout")
            ),
            now=NOW,
            now_provider=lambda: NOW,
            max_attempts=1,
        )
    )

    assert calls == [
        (
            review_module.capture_provider_failure,
            (factory,),
            {
                "source_kind": "semantic_review",
                "source_record_id": str(raw_id),
                "provider_status": "retry_exhausted",
                "error_type": "TimeoutError",
                "occurred_at": NOW,
            },
        )
    ]


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

    second_factory, second_id, second_mimo = _setup(tmp_path / "none")
    asyncio.run(run_semantic_review_once(
        second_factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs["raw_message_id"]),
        reviewer=lambda *args, **kwargs: _run(second_id, second_mimo),
        now=NOW,
    ))
    assert notified == [raw_id]


def test_worker_does_not_notify_final_normal_review(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path)
    run = SemanticReviewRun(
        raw_message_id=raw_id,
        model="deepseek-review",
        review_payload=_review_payload(),
        auxiliary_payload={"recognition_result": "非策略"},
        decision=SemanticReviewDecision(
            agreement_status="disagreed",
            severity="normal",
            conflict_types=("wording_only",),
            differences=("reason",),
            reason="non-material wording difference",
        ),
        prompt_versions={"trading.disagreement.semantic_review": 8},
    )
    notified = []

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs),
        reviewer=lambda *args, **kwargs: run,
        now=NOW,
    ))

    assert notified == []
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.disagreement_severity == "normal"
        assert row.notification_status is None


def test_worker_does_not_notify_failed_review(tmp_path):
    factory, _, _ = _setup(tmp_path)
    notified = []

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs),
        reviewer=lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("invalid semantic response")
        ),
        now=NOW,
        max_attempts=1,
    ))

    assert notified == []
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.comparison_status == "failed"
        assert row.notification_status is None

def test_worker_persists_sent_status_and_canonical_fingerprint(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)
    observed = []

    def notifier(**kwargs):
        with factory() as session:
            row = session.query(RecognitionDecision).one()
            observed.append((row.notification_status, row.automation_status))
        assert kwargs["payload"]["text"] == "全部出局"
        assert kwargs["payload"]["conflict_types"] == [
            "actionability",
            "urgent_exit_missed",
        ]

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=notifier,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW,
    ))

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert observed == [("scheduled", "submitted")]
        assert row.notification_status == "sent"
        assert row.notification_error is None
        assert row.notification_fingerprint == hashlib.sha256(
            row.notification_payload_json.encode("utf-8")
        ).hexdigest()
        persisted_payload = json.loads(row.notification_payload_json)
        assert persisted_payload["comparison"]["payload"]["conflict_types"] == [
            "actionability",
            "urgent_exit_missed",
        ]
        assert row.automation_status == "submitted"
        assert row.automation_reason == "preserve me"


@pytest.mark.parametrize("existing_status", ["scheduled", "sent"])
def test_worker_recovery_never_resends_claimed_notification(tmp_path, existing_status):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        row.notification_fingerprint = "existing-claim"
        row.notification_status = existing_status
        session.commit()
    notified = []

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs),
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW,
    ))

    assert notified == []
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_fingerprint == "existing-claim"
        assert row.notification_status == existing_status
        assert row.automation_status == "submitted"
        assert row.automation_reason == "preserve me"


def test_worker_failed_notification_is_final_and_does_not_change_automation(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)

    def fail_notification(**kwargs):
        raise TimeoutError("telegram timeout")

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=fail_notification,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW,
    ))

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_status == "failed"
        assert row.notification_error == "TimeoutError"
        assert row.automation_status == "submitted"
        assert row.automation_reason == "preserve me"

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        row.comparison_status = "pending"
        row.comparison_next_attempt_at = None
        session.commit()
    notified = []
    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=lambda **kwargs: notified.append(kwargs),
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW + timedelta(minutes=1),
    ))
    assert notified == []


def test_worker_notification_failure_is_adapted_after_delivery_state_commits(
    tmp_path,
    monkeypatch,
):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)
    calls = []

    def capture(adapter, *args, **kwargs):
        with factory() as session:
            assert session.query(RecognitionDecision).one().notification_status == "failed"
        calls.append((adapter, args, kwargs))

    monkeypatch.setattr(
        review_module,
        "capture_runtime_incident_best_effort",
        capture,
    )

    asyncio.run(
        run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=lambda **kwargs: (_ for _ in ()).throw(
                TimeoutError("telegram timeout")
            ),
            reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
            now=NOW,
        )
    )

    assert calls == [
        (
            review_module.capture_notification_failure,
            (factory,),
            {
                "source_kind": "semantic_review_notification",
                "source_record_id": str(raw_id),
                "error_type": "TimeoutError",
                "occurred_at": NOW,
            },
        )
    ]


def test_notification_delivery_status_update_never_overwrites_newer_automation(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)

    def notifier(**kwargs):
        with factory() as session:
            row = session.query(RecognitionDecision).one()
            assert row.notification_status == "scheduled"
            row.automation_status = "reconciled"
            row.automation_reason = "position_absent"
            session.commit()

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=notifier,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
        now=NOW,
    ))

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_status == "sent"
        assert row.automation_status == "reconciled"
        assert row.automation_reason == "position_absent"


def test_worker_sends_generation_a_claimed_payload_after_generation_b_mutates_db(tmp_path):
    factory, raw_id, mimo = _setup(tmp_path, text="generation A source")
    review_payload = _review_payload(action_type="exit_full", critical=True)
    sent = []

    def notifier(**kwargs):
        with factory() as session:
            raw = session.get(RawMessage, raw_id)
            row = session.query(RecognitionDecision).one()
            raw.text = "generation B source"
            row.authoritative_payload_json = json.dumps(
                {"reason": "authority B"}, ensure_ascii=False, sort_keys=True
            )
            row.comparison_payload_json = json.dumps(
                {
                    "independent_action": {"action_type": "none"},
                    "conflict_types": ["symbol"],
                    "evidence": ["evidence B"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            row.automation_status = "skipped"
            row.automation_reason = "generation B"
            session.commit()
        sent.append(kwargs["payload"])

    asyncio.run(run_semantic_review_once(
        factory,
        config=AiRecognitionConfig(),
        notifier=notifier,
        reviewer=lambda *args, **kwargs: _run(raw_id, mimo, review_payload),
        now=NOW,
    ))

    assert sent[0]["text"] == "generation A source"
    assert sent[0]["deepseek"]["status"] == "exit_full"
    assert sent[0]["deepseek"]["evidence"] == ["全部出局"]
    assert sent[0]["conflict_types"] == [
        "actionability",
        "urgent_exit_missed",
    ]
    assert sent[0]["automation"] == {
        "status": "submitted",
        "reason": "preserve me",
    }
    with factory() as session:
        row = session.query(RecognitionDecision).one()
        stored = json.loads(row.notification_payload_json)
        assert stored["source"]["text"] == "generation A source"
        assert stored["comparison"]["payload"]["evidence"] == ["全部出局"]
        assert row.notification_fingerprint == hashlib.sha256(
            row.notification_payload_json.encode("utf-8")
        ).hexdigest()


def test_notifier_http_error_persists_and_logs_only_safe_summary(
    tmp_path, caplog, monkeypatch
):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)
    secret = "botSECRET_TOKEN"

    def fail_notification(**kwargs):
        request = httpx.Request(
            "POST", f"https://api.telegram.org/{secret}/sendMessage?chat_id=987"
        )
        response = httpx.Response(
            502,
            request=request,
            text=f"upstream body leaked {secret}",
            headers={"X-Secret": secret},
        )
        raise httpx.HTTPStatusError(
            f"request failed at {request.url} body={response.text}",
            request=request,
            response=response,
        )

    monkeypatch.setattr(review_module.logger, "propagate", True)
    caplog.set_level(logging.ERROR, logger=review_module.__name__)
    review_module.logger.addHandler(caplog.handler)
    try:
        asyncio.run(run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=fail_notification,
            reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
            now=NOW,
        ))
    finally:
        review_module.logger.removeHandler(caplog.handler)

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_status == "failed"
        assert row.notification_error == "HTTPStatusError status=502"
        assert secret not in (row.notification_error or "")
    assert secret not in caplog.text
    assert "chat_id=987" not in caplog.text
    assert "upstream body" not in caplog.text
    assert "HTTPStatusError status=502" in caplog.text


def test_notifier_secret_is_not_logged_if_failed_status_write_also_fails(
    tmp_path, caplog, monkeypatch
):
    factory, raw_id, mimo = _setup(tmp_path, text="全部出局")
    payload = _review_payload(action_type="exit_full", critical=True)
    secret = "botSECRET_TOKEN"

    def fail_notification(**kwargs):
        request = httpx.Request("POST", f"https://api.telegram.org/{secret}")
        response = httpx.Response(503, request=request, text=secret)
        raise httpx.HTTPStatusError(
            f"notifier leaked {secret}", request=request, response=response
        )

    def fail_status_write(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        review_module,
        "_update_critical_notification_delivery",
        fail_status_write,
    )
    monkeypatch.setattr(review_module.logger, "propagate", True)
    caplog.set_level(logging.ERROR, logger=review_module.__name__)
    review_module.logger.addHandler(caplog.handler)
    try:
        asyncio.run(run_semantic_review_once(
            factory,
            config=AiRecognitionConfig(),
            notifier=fail_notification,
            reviewer=lambda *args, **kwargs: _run(raw_id, mimo, payload),
            now=NOW,
        ))
    finally:
        review_module.logger.removeHandler(caplog.handler)

    with factory() as session:
        row = session.query(RecognitionDecision).one()
        assert row.notification_status == "scheduled"
    assert secret not in caplog.text
    assert "HTTPStatusError status=503" in caplog.text


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
