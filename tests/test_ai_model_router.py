"""The first model does not work, so the second one answers.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §4/§6.

Two things are being pinned here at once. One is the routing itself: what
counts as a reason to change model, what does not, and what the audit says
afterwards. The other is that the MiMo provider health line keeps meaning what
it meant -- a backup answering is not the primary recovering, and the person
whose balance ran out still hears about it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from telegram_kol_research import mimo_provider_health as health
from telegram_kol_research import recognition_experiments as experiments
from telegram_kol_research.ai_model_router import (
    MIN_REMAINING_SECONDS,
    ModelFailure,
    RouterResult,
    request_reached_provider,
    resolve_stage_chain,
    run_with_fallback,
)
from telegram_kol_research.ai_recognition_config import (
    AiModel,
    AiModelConfig,
    AiProvider,
    AiRecognitionConfig,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_recognition_runs import load_mimo_attempts
from telegram_kol_research.models import (
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
)
from telegram_kol_research.recognition_experiments import (
    MimoProviderAttemptTelemetry,
    _find_mimo_model,
    infer_mimo_authoritative_v2,
    resolve_authoritative_chain,
    run_mimo_authoritative_for_message,
)


PRIMARY = "mimo-v2.5"
BACKUP = "backup-vision"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _chain_config(*model_ids: str) -> AiRecognitionConfig:
    """A v2 config whose authoritative stage is bound to ``model_ids``."""

    return AiRecognitionConfig(
        providers=[
            AiProvider(
                id="mimo",
                base_url="https://api.xiaomimimo.com/v1",
                api_key="primary-key",
            ),
            AiProvider(
                id="spare",
                base_url="https://spare.example.com/v1",
                api_key="spare-key",
            ),
        ],
        models=[
            AiModel(
                id=PRIMARY,
                provider_id="mimo",
                model=PRIMARY,
                supports_text=True,
                supports_image=True,
                provider=AiProvider(
                    id="mimo",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="primary-key",
                ),
            ),
            AiModel(
                id=BACKUP,
                provider_id="spare",
                model=BACKUP,
                supports_text=True,
                supports_image=True,
                provider=AiProvider(
                    id="spare",
                    base_url="https://spare.example.com/v1",
                    api_key="spare-key",
                ),
            ),
        ],
        stages={"authoritative_recognition": list(model_ids)},
    )


def _model(model_id: str) -> AiModelConfig:
    return AiModelConfig(
        id=model_id,
        label=model_id,
        base_url=f"https://{model_id}.example.com/v1",
        api_key="k",
        model=model_id,
        supports_text=True,
        supports_image=True,
    )


def _message(factory, *, text: str = "BTC 偏多观点") -> int:
    with factory() as session:
        row = RawMessage(chat_id=900, message_id=17, text=text)
        session.add(row)
        session.commit()
        return int(row.id)


def _v2_payload(observed_text: str = "BTC 偏多观点") -> dict:
    return {
        "contract_version": "mimo-authoritative-v2",
        "summary": "普通市场观点",
        "confidence": 0.82,
        "intents": [
            {
                "intent_type": "market_commentary",
                "action": None,
                "reason": "没有完整交易动作",
                "confidence": 0.82,
                "evidence_refs": ["text:observed_text"],
            }
        ],
        "evidence": {
            "text": {"observed_text": observed_text, "fields": {}},
            "images": [],
            "conflicts": [],
        },
    }


def _status_error(code: int) -> RuntimeError:
    """Exactly the shape ``_call_mimo_direct_model`` raises for an HTTP error."""

    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    response = httpx.Response(code, request=request, text="provider body")
    try:
        try:
            raise httpx.HTTPStatusError(
                f"Client error '{code}' for url", request=request, response=response
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"{exc}; response_body=provider body") from exc
    except RuntimeError as wrapped:
        return wrapped


def _by_model(outcomes: dict[str, object]):
    """A requester that answers according to which model it was handed."""

    calls: list[str] = []

    def request(**kwargs):
        model = kwargs["model_config"].model
        calls.append(model)
        outcome = outcomes[model]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    request.calls = calls
    return request


def _attempt_rows(factory) -> list[tuple[int, str | None, str]]:
    with factory() as session:
        return [
            (row.ordinal, row.model, row.status)
            for row in session.query(MimoRecognitionAttempt)
            .order_by(MimoRecognitionAttempt.ordinal.asc())
            .all()
        ]


def _run(factory) -> MimoRecognitionRun:
    with factory() as session:
        return (
            session.query(MimoRecognitionRun)
            .order_by(MimoRecognitionRun.id.desc())
            .first()
        )


# ---------------------------------------------------------------------------
# The router in isolation
# ---------------------------------------------------------------------------


def test_the_chain_is_the_stage_binding_in_order():
    config = _chain_config(PRIMARY, BACKUP)

    chain = resolve_stage_chain(config, "authoritative_recognition")

    assert [item.id for item in chain] == [PRIMARY, BACKUP]
    assert chain[0].base_url == "https://api.xiaomimimo.com/v1"
    assert chain[1].api_key == "spare-key"


def test_the_second_model_answers_when_the_first_fails():
    result = run_with_fallback(
        [_model("a"), _model("b")],
        lambda model, deadline_seconds=None: (
            "ok" if model.id == "b" else (_ for _ in ()).throw(RuntimeError("402"))
        ),
    )

    assert result.succeeded is True
    assert result.model.id == "b"
    assert result.value == "ok"
    assert result.fallback_from == ("a",)
    assert [failure.model_id for failure in result.failures] == ["a"]


def test_a_failure_before_the_request_left_does_not_change_model():
    def attempt(model, deadline_seconds=None):
        error = RuntimeError("payload assembly failed")
        error.mimo_provider_attempt_telemetry = MimoProviderAttemptTelemetry(
            provider_request_made=False
        )
        raise error

    result = run_with_fallback(
        [_model("a"), _model("b")],
        attempt,
        classify=request_reached_provider,
    )

    assert result.succeeded is False
    assert [failure.model_id for failure in result.failures] == ["a"]
    assert result.fallback_from == ()


def test_the_next_model_is_not_started_below_the_minimum_remaining_budget():
    clock = iter([0.0, 0.0, 240.0 - MIN_REMAINING_SECONDS + 1.0])
    started: list[str] = []

    def attempt(model, deadline_seconds=None):
        started.append(model.id)
        raise RuntimeError("402")

    result = run_with_fallback(
        [_model("a"), _model("b")],
        attempt,
        budget_seconds=240.0,
        monotonic=lambda: next(clock),
    )

    assert started == ["a"]
    assert result.skipped_for_budget == ("b",)
    assert result.succeeded is False


def test_every_request_gets_what_is_left_of_the_one_shared_budget():
    clock = iter([0.0, 0.0, 100.0])
    deadlines: list[float | None] = []

    def attempt(model, deadline_seconds=None):
        deadlines.append(deadline_seconds)
        raise RuntimeError("402")

    run_with_fallback(
        [_model("a"), _model("b")],
        attempt,
        budget_seconds=240.0,
        monotonic=lambda: next(clock),
    )

    assert deadlines == [240.0, 140.0]


def test_one_model_reports_its_own_error_and_two_report_both():
    single = RouterResult(
        succeeded=False, failures=(ModelFailure("a", "a", "boom"),)
    )
    pair = RouterResult(
        succeeded=False,
        failures=(ModelFailure("a", "a", "boom"), ModelFailure("b", "b", "bang")),
    )

    assert single.error_message == "boom"
    assert pair.error_message == "model a: boom | model b: bang"


# ---------------------------------------------------------------------------
# The chain budget stays inside the job claim lease
# ---------------------------------------------------------------------------


def test_the_whole_chain_plus_one_blocked_read_fits_inside_the_claim_lease():
    """The chain shares the 240 s; it does not get 240 s per model."""

    from telegram_kol_research.message_processing_worker import (
        DEFAULT_CLAIM_STALE_AFTER,
    )

    per_read_timeout = AiModelConfig(id=PRIMARY, label="MiMo").timeout_seconds
    assert (
        experiments.MIMO_REQUEST_TOTAL_DEADLINE_SECONDS + per_read_timeout
        <= DEFAULT_CLAIM_STALE_AFTER.total_seconds()
    )
    assert MIN_REMAINING_SECONDS == 20.0


def test_a_slow_first_attempt_does_not_hand_its_retry_a_fresh_deadline():
    """The model's own retries come out of the slice it was given."""

    import time as time_module

    now = time_module.monotonic()

    assert experiments._remaining_deadline(240.0, now) == pytest.approx(240.0, abs=1)
    assert experiments._remaining_deadline(240.0, now - 200.0) == pytest.approx(
        40.0, abs=1
    )
    # Never negative: a spent budget asks for zero, not for time already gone.
    assert experiments._remaining_deadline(240.0, now - 900.0) == 0.0
    assert experiments._remaining_deadline(None, now) is None


# ---------------------------------------------------------------------------
# v2: the whole chain, audited
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "primary_failure",
    [
        pytest.param(_status_error(402), id="http_402"),
        pytest.param(TimeoutError("mimo timed out"), id="timeout"),
        pytest.param("not-json", id="bad_json"),
    ],
)
def test_v2_falls_back_to_the_backup_model_and_records_both(tmp_path, primary_failure):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    requester = _by_model({PRIMARY: primary_failure, BACKUP: _v2_payload()})

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        requester=requester,
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert result.succeeded is True
    assert result.model == BACKUP
    assert result.parsed_result.evidence.text.observed_text == "BTC 偏多观点"
    run = _run(factory)
    assert run.model == BACKUP
    assert run.status == "completed"
    rows = _attempt_rows(factory)
    assert rows[0][:2] == (1, PRIMARY)
    assert rows[0][2] != "completed"
    assert rows[1] == (2, BACKUP, "completed")
    assert run.selected_attempt_ordinal == 2
    assert requester.calls == [PRIMARY, BACKUP]


def test_v2_retries_the_primary_within_itself_before_changing_model(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    failures = iter([_status_error(500), _status_error(500)])

    def request(**kwargs):
        model = kwargs["model_config"].model
        if model == PRIMARY:
            raise next(failures)
        return _v2_payload()

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        requester=request,
        max_attempts=2,
        retry_delay_seconds=0,
    )

    assert result.model == BACKUP
    rows = _attempt_rows(factory)
    assert [(row[0], row[1]) for row in rows] == [
        (1, PRIMARY),
        (2, PRIMARY),
        (3, BACKUP),
    ]
    attempts = load_mimo_attempts(factory, run_id=_run(factory).id)
    # Only a repeat of the same model is a retry of the one before it.
    assert attempts[1].retry_of_ordinal == 1
    assert attempts[2].retry_of_ordinal is None


def test_v2_reports_both_models_when_the_whole_chain_fails(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        requester=_by_model(
            {PRIMARY: _status_error(402), BACKUP: _status_error(503)}
        ),
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert result.succeeded is False
    assert f"model {PRIMARY}" in result.error_message
    assert f"model {BACKUP}" in result.error_message
    assert "402" in result.error_message and "503" in result.error_message
    run = _run(factory)
    assert run.status == "failed"
    # The run keeps the chain head: that is the model this message started on.
    assert run.model == PRIMARY
    assert [(row[0], row[1]) for row in _attempt_rows(factory)] == [
        (1, PRIMARY),
        (2, BACKUP),
    ]


def test_v2_does_not_change_model_for_an_unreadable_image(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    with factory() as session:
        row = RawMessage(chat_id=900, message_id=18, text="")
        session.add(row)
        session.commit()
        raw_id = int(row.id)
        from telegram_kol_research.models import MediaAsset

        session.add(
            MediaAsset(
                raw_message_id=raw_id,
                kind="photo",
                mime_type="image/jpeg",
                local_path="missing/never-written.jpg",
            )
        )
        session.commit()

    def never_called(**kwargs):
        raise AssertionError("no request should be made")

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        requester=never_called,
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert result.error_code == "image_unavailable"
    assert result.model == PRIMARY
    assert _attempt_rows(factory) == []


def test_v2_does_not_change_model_when_the_request_never_left(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    calls: list[str] = []

    def request(**kwargs):
        calls.append(kwargs["model_config"].model)
        error = RuntimeError("payload assembly failed")
        error.mimo_provider_attempt_telemetry = MimoProviderAttemptTelemetry(
            provider_request_made=False
        )
        raise error

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        requester=request,
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert result.succeeded is False
    assert calls == [PRIMARY]


def test_a_single_model_chain_records_exactly_one_attempt(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_id,
        config=_chain_config(PRIMARY),
        media_root=tmp_path,
        context_text="",
        requester=lambda **kwargs: _v2_payload(),
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert result.model == PRIMARY
    assert _attempt_rows(factory) == [(1, PRIMARY, "completed")]
    assert _run(factory).model == PRIMARY


# ---------------------------------------------------------------------------
# v1: same chain, same audit shape
# ---------------------------------------------------------------------------


def _v1_payload() -> dict:
    return {
        "recognition_result": "非策略",
        "reason": "只是观点",
        "strategy": {},
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "BTC 偏多观点", "image_quality": "none"},
        "confidence": 0.4,
    }


def test_v1_falls_back_and_reports_the_answering_model(tmp_path, monkeypatch):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    requester = _by_model({PRIMARY: _status_error(402), BACKUP: _v1_payload()})
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        requester,
    )

    result = run_mimo_authoritative_for_message(
        factory,
        raw_message_id=raw_id,
        ai_recognition_config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
    )

    assert result.error_message is None
    assert result.model == BACKUP
    assert [item.model_id for item in result.model_attempts] == [PRIMARY, BACKUP]
    assert [item.succeeded for item in result.model_attempts] == [False, True]
    assert requester.calls[0] == PRIMARY


def test_the_v1_audit_writes_one_row_per_model(tmp_path, monkeypatch):
    from telegram_kol_research.authoritative_recognition import (
        _run_v1_authority_with_audit,
    )

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        _by_model({PRIMARY: _status_error(402), BACKUP: _v1_payload()}),
    )

    _run_v1_authority_with_audit(
        factory,
        raw_message_id=raw_id,
        ai_recognition_config=_chain_config(PRIMARY, BACKUP),
        media_root=tmp_path,
        context_text="",
        input_fingerprint="fp",
        run_kind="v1_authoritative",
    )

    rows = _attempt_rows(factory)
    assert rows[0] == (1, PRIMARY, "http_error")
    assert rows[1] == (2, BACKUP, "completed")
    run = _run(factory)
    assert run.model == BACKUP
    assert run.selected_attempt_ordinal == 2
    attempts = load_mimo_attempts(factory, run_id=run.id)
    assert attempts[0].error_code == (
        "mimo_provider_unavailable.insufficient_balance.http_402"
    )


def test_the_v1_audit_of_a_single_model_chain_is_unchanged(tmp_path, monkeypatch):
    from telegram_kol_research.authoritative_recognition import (
        _run_v1_authority_with_audit,
    )

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        _by_model({PRIMARY: _v1_payload()}),
    )

    _run_v1_authority_with_audit(
        factory,
        raw_message_id=raw_id,
        ai_recognition_config=_chain_config(PRIMARY),
        media_root=tmp_path,
        context_text="",
        input_fingerprint="fp",
        run_kind="v1_authoritative",
    )

    assert _attempt_rows(factory) == [(1, PRIMARY, "completed")]
    run = _run(factory)
    assert run.model == PRIMARY
    assert run.selected_attempt_ordinal == 1
    attempts = load_mimo_attempts(factory, run_id=run.id)
    assert attempts[0].started_at is not None
    assert attempts[0].duration_ms >= 0


def test_the_v1_run_model_is_the_chain_head_not_the_image_provider(tmp_path):
    """``image_provider`` on a v1 file is GLM-OCR; this path calls MiMo."""

    from telegram_kol_research.ai_recognition_config import (
        load_ai_recognition_config,
    )

    path = tmp_path / "ai_recognition.yaml"
    path.write_text(
        __import__("pathlib").Path("config/ai_recognition.example.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning):
        config = load_ai_recognition_config(path)

    assert config.image_provider.model == "glm-ocr"
    assert resolve_authoritative_chain(config)[0].model == PRIMARY
    assert _find_mimo_model(config).model == PRIMARY


# ---------------------------------------------------------------------------
# The provider health line still means what it meant
# ---------------------------------------------------------------------------


def _attempt(
    factory,
    *,
    at: datetime,
    model: str | None,
    status: str = "http_error",
    error_code: str | None = None,
    raw_message_id: int,
):
    from telegram_kol_research.mimo_recognition_runs import (
        record_mimo_attempt,
        start_mimo_run,
    )

    run = start_mimo_run(
        factory,
        raw_message_id=raw_message_id,
        run_kind="v1_authoritative",
        contract_version="v1",
        model=model or PRIMARY,
        input_kind="text",
        input_fingerprint="fp",
        prompt_versions={},
        started_at=at,
    )
    record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=1,
        status=status,
        model=model,
        error_code=error_code,
        error_message=None if status == "completed" else "failed",
        duration_ms=0,
        started_at=at,
        completed_at=at,
        attempt_phase="v1_authoritative",
    )


BALANCE = "mimo_provider_unavailable.insufficient_balance.http_402"
OUTAGE_START = datetime(2026, 9, 13, 3, 0, 0)


def test_a_backup_answering_is_not_the_primary_recovering(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=PRIMARY,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )
    _attempt(
        factory,
        at=OUTAGE_START + timedelta(seconds=5),
        model=BACKUP,
        status="completed",
        raw_message_id=raw_id,
    )

    outage = health.load_latest_provider_outage(
        factory, chain_head_model=PRIMARY
    )

    assert outage is not None
    assert outage.recovered_at is None
    assert outage.failures == 1


def test_the_primary_answering_on_the_next_message_is_a_recovery(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=PRIMARY,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )
    _attempt(
        factory,
        at=OUTAGE_START + timedelta(seconds=5),
        model=BACKUP,
        status="completed",
        raw_message_id=raw_id,
    )
    _attempt(
        factory,
        at=OUTAGE_START + timedelta(seconds=60),
        model=PRIMARY,
        status="completed",
        raw_message_id=raw_id,
    )

    outage = health.load_latest_provider_outage(
        factory, chain_head_model=PRIMARY
    )

    assert outage is not None
    assert outage.recovered_at is not None


def test_rows_written_before_the_column_existed_count_as_the_head(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=None,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )

    outage = health.load_latest_provider_outage(
        factory, chain_head_model=PRIMARY
    )

    assert outage is not None
    assert outage.failures == 1


def test_the_outage_alert_says_a_backup_is_carrying_recognition(tmp_path):
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_mimo_provider_unavailable,
    )

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=PRIMARY,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )
    _attempt(
        factory,
        at=OUTAGE_START + timedelta(seconds=5),
        model=BACKUP,
        status="completed",
        raw_message_id=raw_id,
    )
    config = RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES),
        telegram_notifications_enabled=True,
    )

    outcome = health.run_mimo_provider_health_tick(
        factory,
        now=(OUTAGE_START + timedelta(minutes=1)).replace(tzinfo=UTC),
        chain_head_model=PRIMARY,
        capture_unavailable=lambda session_factory, **kwargs: (
            capture_mimo_provider_unavailable(
                session_factory, config=config, **kwargs
            )
        ),
    )

    assert outcome["state"] == "unavailable_alerted"
    assert outcome["fallback_model"] == BACKUP
    with factory() as session:
        incident = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == "mimo_provider_unavailable")
            .one()
        )
    summary = json.loads(incident.redacted_summary)
    assert summary["fallback_note"] == f"已切换到备用模型 {BACKUP} 继续识别"
    assert summary["impact"] == "authoritative_recognition_on_fallback_model"
    assert summary["reason_code"] == "insufficient_balance"


def test_the_telegram_alert_says_recognition_continued_on_the_backup(tmp_path):
    from types import SimpleNamespace

    from telegram_kol_research.system_operator_bot import (
        format_runtime_incident_notification,
    )

    incident = SimpleNamespace(
        id=7,
        incident_type="mimo_provider_unavailable",
        severity="critical",
        source_kind="mimo_provider",
        source_record_id="outage_1_b0",
        repeat_count=1,
        redacted_summary=json.dumps(
            {
                "component": "mimo_provider",
                "reason_code": "insufficient_balance",
                "error_code": "http_402",
                "episode_started_at": "2026-09-13T03:00Z",
                "last_failure_at": "2026-09-13T03:00Z",
                "consecutive_failures": 3,
                "impact": "authoritative_recognition_on_fallback_model",
                "fallback_note": f"已切换到备用模型 {BACKUP} 继续识别",
            },
            ensure_ascii=False,
        ),
    )

    text = format_runtime_incident_notification(incident)

    assert f"已切换到备用模型 {BACKUP} 继续识别" in text
    assert "新消息无法完成权威识别" not in text
    assert "MiMo 识别供应商不可用" in text


def test_without_a_backup_the_alert_says_recognition_is_unavailable(tmp_path):
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_mimo_provider_unavailable,
    )

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=PRIMARY,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )
    config = RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES),
        telegram_notifications_enabled=True,
    )

    health.run_mimo_provider_health_tick(
        factory,
        now=(OUTAGE_START + timedelta(minutes=1)).replace(tzinfo=UTC),
        chain_head_model=PRIMARY,
        capture_unavailable=lambda session_factory, **kwargs: (
            capture_mimo_provider_unavailable(
                session_factory, config=config, **kwargs
            )
        ),
    )

    with factory() as session:
        incident = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == "mimo_provider_unavailable")
            .one()
        )
    summary = json.loads(incident.redacted_summary)
    assert "fallback_note" not in summary
    assert summary["impact"] == "authoritative_recognition_unavailable"


def test_a_backup_answer_does_not_break_the_primary_failure_streak(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    for index in range(health.STREAK_THRESHOLD):
        _attempt(
            factory,
            at=OUTAGE_START + timedelta(seconds=index * 2),
            model=PRIMARY,
            error_code="mimo_request_rejected.http_400",
            raw_message_id=raw_id,
        )
        _attempt(
            factory,
            at=OUTAGE_START + timedelta(seconds=index * 2 + 1),
            model=BACKUP,
            status="completed",
            raw_message_id=raw_id,
        )

    rows = health._load_streak_rows(
        factory, scan_limit=200, chain_head_model=PRIMARY
    )
    streaks = health.derive_failure_streaks(rows)

    assert [streak.failures for streak in streaks] == [health.STREAK_THRESHOLD]


def test_an_unreadable_config_counts_every_row_instead_of_none(tmp_path):
    def broken() -> AiRecognitionConfig:
        raise OSError("config is unreadable")

    assert health.resolve_chain_head_model(broken) is None


def test_the_chain_head_comes_from_the_stage_binding():
    assert (
        health.resolve_chain_head_model(lambda: _chain_config(BACKUP, PRIMARY))
        == BACKUP
    )


def test_a_message_answered_by_the_backup_is_never_replayed(tmp_path):
    """It was answered, so it is not one of the messages an outage lost."""

    from telegram_kol_research.provider_outage_replay import (
        select_replay_candidates,
    )
    from telegram_kol_research.recognition_decisions import (
        RecognitionDecisionRecord,
        save_terminal_authoritative_decision,
        update_recognition_execution_outcome,
    )

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _message(factory)
    _attempt(
        factory,
        at=OUTAGE_START,
        model=PRIMARY,
        error_code=BALANCE,
        raw_message_id=raw_id,
    )
    _attempt(
        factory,
        at=OUTAGE_START + timedelta(seconds=5),
        model=BACKUP,
        status="completed",
        raw_message_id=raw_id,
    )
    # The backup answered, so the message has a real verdict: its automation
    # reason is not one of the two "no decision was produced" outcomes.
    save_terminal_authoritative_decision(
        factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model=BACKUP,
            authoritative_status="非策略",
            authoritative_payload={},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_only",
            differences=[],
            prompt_versions={},
        ),
    )
    update_recognition_execution_outcome(
        factory,
        raw_message_id=raw_id,
        automation_status="skipped",
        automation_reason="not_a_strategy",
    )

    candidates = select_replay_candidates(
        factory,
        auto_trade_chat_ids=[900],
        since=OUTAGE_START.replace(tzinfo=UTC),
        recovered_at=(OUTAGE_START + timedelta(minutes=5)).replace(tzinfo=UTC),
    )

    assert candidates == []
