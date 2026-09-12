"""MiMo provider outages: classified, noticed, re-alerted, recovered (step-18).

2026-09-12 03:00Z-17:42Z the provider answered ``402 Payment Required`` 494
times. Every failure was recorded as ``v1_authoritative_failed``, nothing
counted them, and nobody was told. These tests encode the three things that
were missing: the failure is named from the exception, the outage is derived
from the audit on a clock of its own, and a person hears about it -- first
immediately, then every 30 minutes, then once when it ends.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from telegram_kol_research import mimo_provider_health as health
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.config import (
    ALWAYS_NOTIFIED_INCIDENT_TYPES,
    RuntimeIncidentConfig,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_recognition_runs import (
    record_mimo_attempt,
    start_mimo_run,
)
from telegram_kol_research.models import (
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RuntimeIncident,
)
from telegram_kol_research.recognition_experiments import (
    MimoAuthoritativeResult,
    MimoProviderAttemptTelemetry,
    _call_mimo_authoritative_with_retry,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_mimo_provider_recovered,
    capture_mimo_provider_unavailable,
)
from telegram_kol_research.system_operator_bot import (
    format_runtime_incident_notification,
)
from telegram_kol_research.telegram_live_listener import (
    run_authoritative_gap_recovery_loop,
)


OUTAGE_START = datetime(2026, 9, 12, 3, 0, 25)  # naive UTC, as SQLite returns it
CONFIG = RuntimeIncidentConfig(
    capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES),
    telegram_notifications_enabled=True,
)


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


# --------------------------------------------------------------------------
# Classification: from the exception, never from the text
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,kind",
    [
        (402, "insufficient_balance"),
        (401, "auth_rejected"),
        (403, "auth_rejected"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_a_provider_refusal_is_named_by_its_status(code, kind):
    assert health.classify_provider_failure(_status_error(code)) == (
        health.ProviderFailure("provider_unavailable", kind, code)
    )


@pytest.mark.parametrize("code", [400, 404, 413, 422])
def test_a_request_the_provider_rejected_is_not_an_outage(code):
    assert health.classify_provider_failure(_status_error(code)) == (
        health.ProviderFailure("request_rejected", None, code)
    )


def test_timeouts_and_transport_errors_are_outages_and_validation_is_not():
    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")

    assert health.classify_provider_failure(
        httpx.ReadTimeout("read timed out", request=request)
    ).kind == "timeout"
    assert health.classify_provider_failure(
        httpx.ConnectError("connection refused", request=request)
    ).kind == "network_error"
    assert health.classify_provider_failure(
        ValueError("MiMo response missing strategy")
    ) is None


def test_the_words_402_payment_required_alone_are_not_evidence():
    """A string that merely mentions 402 must not open an outage."""

    assert health.classify_provider_failure(
        RuntimeError("Client error '402 Payment Required' for url")
    ) is None


# --------------------------------------------------------------------------
# The retry loop records the classification; the audit code follows it
# --------------------------------------------------------------------------


class _Payload(dict):
    pass


def _retry(monkeypatch, tmp_path, outcomes, *, max_attempts=2):
    def fake_call(**kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        payload = _Payload(outcome)
        payload.mimo_provider_attempt_telemetry = MimoProviderAttemptTelemetry()
        return payload

    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        fake_call,
    )
    return _call_mimo_authoritative_with_retry(
        raw_message=RawMessage(id=1, chat_id=100, message_id=9, text="BTC 多"),
        media_assets=[],
        model_config=object(),
        prompt="",
        media_root=tmp_path,
        context_text="",
        max_attempts=max_attempts,
        retry_delay_seconds=0,
    )


def test_two_402s_are_recorded_as_an_empty_balance(monkeypatch, tmp_path):
    payload, error, attempts = _retry(
        monkeypatch, tmp_path, [_status_error(402), _status_error(402)]
    )

    assert payload == {}
    assert "402" in (error or "")
    assert [attempt.failure_kind for attempt in attempts] == [
        "insufficient_balance",
        "insufficient_balance",
    ]
    assert health.v1_failure_error_code(attempts) == (
        "mimo_provider_unavailable.insufficient_balance.http_402"
    )


def test_a_timeout_then_a_402_is_still_an_outage(monkeypatch, tmp_path):
    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    _, _, attempts = _retry(
        monkeypatch,
        tmp_path,
        [httpx.ReadTimeout("timed out", request=request), _status_error(402)],
    )

    assert health.v1_failure_error_code(attempts) == (
        "mimo_provider_unavailable.insufficient_balance.http_402"
    )


def test_one_answered_request_proves_the_provider_was_up(monkeypatch, tmp_path):
    """402 then 400: the provider answered the second time, so no outage."""

    _, _, attempts = _retry(
        monkeypatch, tmp_path, [_status_error(402), _status_error(400)]
    )

    assert health.v1_failure_error_code(attempts) == "mimo_request_rejected.http_400"


def test_an_invalid_payload_is_the_provider_answering(monkeypatch, tmp_path):
    _, error, attempts = _retry(
        monkeypatch, tmp_path, [{"recognition_result": "非策略"}], max_attempts=1
    )

    assert "missing" in (error or "")
    assert health.v1_failure_error_code(attempts) == "mimo_response_invalid"


def test_a_failure_before_any_request_names_nothing(monkeypatch, tmp_path):
    error = _status_error(402)
    error.mimo_provider_attempt_telemetry = MimoProviderAttemptTelemetry(
        provider_request_made=False
    )

    _, _, attempts = _retry(monkeypatch, tmp_path, [error], max_attempts=1)

    assert attempts[0].failure_class is None
    assert health.v1_failure_error_code(attempts) is None


def _session_factory(tmp_path, name="health.db"):
    session_factory = create_session_factory(tmp_path / name)
    with session_factory() as session:
        session.add(
            RawMessage(
                id=1, chat_id=100, message_id=1, text="BTC 多", posted_at=OUTAGE_START
            )
        )
        session.commit()
    return session_factory


@pytest.mark.parametrize(
    "telemetry,expected",
    [
        (
            (
                MimoProviderAttemptTelemetry(
                    failure_class="provider_unavailable",
                    failure_kind="insufficient_balance",
                    http_status=402,
                ),
            ),
            "mimo_provider_unavailable.insufficient_balance.http_402",
        ),
        ((MimoProviderAttemptTelemetry(),), "v1_authoritative_failed"),
    ],
)
def test_the_v1_audit_row_carries_the_classified_code(
    monkeypatch, tmp_path, telemetry, expected
):
    from telegram_kol_research import authoritative_recognition

    session_factory = _session_factory(tmp_path)
    monkeypatch.setattr(
        authoritative_recognition,
        "run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=1,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="MiMo failed after 2 attempts: 402 Payment Required",
            provider_attempt_telemetry=telemetry,
        ),
    )

    authoritative_recognition._run_v1_authority_with_audit(
        session_factory,
        raw_message_id=1,
        ai_recognition_config=SimpleNamespace(
            image_provider=SimpleNamespace(model="mimo-v2.5")
        ),
        media_root=tmp_path,
        context_text="",
        input_fingerprint="fp",
        run_kind="v1_authoritative",
    )

    with session_factory() as session:
        attempt = session.query(MimoRecognitionAttempt).one()
        run = session.query(MimoRecognitionRun).one()
        assert attempt.error_code == expected
        assert run.final_error_code == expected


# --------------------------------------------------------------------------
# Deriving the outage, and the alert cadence
# --------------------------------------------------------------------------


def _attempt(session_factory, *, at, status="http_error", error_code=None):
    run = start_mimo_run(
        session_factory,
        raw_message_id=1,
        run_kind="v1_authoritative",
        contract_version="v1",
        model="mimo-v2.5",
        input_kind="text",
        input_fingerprint="fp",
        prompt_versions={},
        started_at=at,
    )
    record_mimo_attempt(
        session_factory,
        run_id=run.id,
        ordinal=1,
        status=status,
        error_code=error_code,
        error_message=None if status == "completed" else "failed",
        duration_ms=0,
        started_at=at,
        completed_at=at,
        attempt_phase="v1_authoritative",
    )


BALANCE = "mimo_provider_unavailable.insufficient_balance.http_402"


def _tick(session_factory, now):
    def capture(adapter):
        return lambda factory, **kwargs: adapter(factory, config=CONFIG, **kwargs)

    return health.run_mimo_provider_health_tick(
        session_factory,
        now=now.replace(tzinfo=UTC),
        capture_unavailable=capture(capture_mimo_provider_unavailable),
        capture_recovered=capture(capture_mimo_provider_recovered),
    )


def _incidents(session_factory, incident_type):
    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == incident_type)
            .order_by(RuntimeIncident.id.asc())
            .all()
        )


def test_the_first_failure_alerts_then_every_thirty_minutes_then_once_on_recovery(
    tmp_path,
):
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, error_code=BALANCE)

    assert _tick(session_factory, OUTAGE_START + timedelta(seconds=15))["state"] == (
        "unavailable_alerted"
    )
    first = _incidents(session_factory, "mimo_provider_unavailable")
    assert len(first) == 1
    assert first[0].notification_status == "pending"

    # Still inside the first half hour, with more failures: no second message.
    _attempt(session_factory, at=OUTAGE_START + timedelta(minutes=9), error_code=BALANCE)
    _tick(session_factory, OUTAGE_START + timedelta(minutes=10))
    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 1

    # Next bucket: a reminder.
    _tick(session_factory, OUTAGE_START + timedelta(minutes=31))
    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 2

    # No new traffic at all -- step-18's quiet stretch -- still reminds.
    _tick(session_factory, OUTAGE_START + timedelta(minutes=65))
    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 3

    _attempt(session_factory, at=OUTAGE_START + timedelta(minutes=70), status="completed")
    assert _tick(session_factory, OUTAGE_START + timedelta(minutes=70, seconds=20))[
        "state"
    ] == "recovery_announced"
    assert _tick(session_factory, OUTAGE_START + timedelta(minutes=71))["state"] == (
        "recovery_already_announced"
    )
    recovered = _incidents(session_factory, "mimo_provider_recovered")
    assert len(recovered) == 1
    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 3

    alert_text = format_runtime_incident_notification(first[0])
    assert "MiMo 识别供应商不可用" in alert_text
    assert "余额不足" in alert_text
    assert "HTTP 402" in alert_text
    assert "每 30 分钟" in alert_text
    summary = json.loads(first[0].redacted_summary)
    assert summary["episode_started_at"] == "2026-09-12T03:00Z"
    assert summary["consecutive_failures"] == 1

    recovery_text = format_runtime_incident_notification(recovered[0])
    assert "MiMo 识别供应商已恢复" in recovery_text
    assert "2026-09-12T03:00Z 至 2026-09-12T04:10Z" in recovery_text
    assert json.loads(recovered[0].redacted_summary)["consecutive_failures"] == 2


def test_a_failure_that_never_reached_the_provider_does_not_end_the_outage(tmp_path):
    """An empty message mid-outage is neutral, not a recovery."""

    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, error_code=BALANCE)
    _attempt(
        session_factory,
        at=OUTAGE_START + timedelta(minutes=5),
        error_code="v1_authoritative_failed",
    )

    outage = health.load_latest_provider_outage(session_factory)

    assert outage is not None
    assert outage.recovered_at is None
    assert outage.failures == 1
    assert _tick(session_factory, OUTAGE_START + timedelta(minutes=6))["state"] == (
        "unavailable_alerted"
    )
    assert _incidents(session_factory, "mimo_provider_recovered") == []


def test_a_rejected_request_after_an_outage_is_a_recovery(tmp_path):
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, error_code=BALANCE)
    _attempt(
        session_factory,
        at=OUTAGE_START + timedelta(minutes=5),
        error_code="mimo_request_rejected.http_400",
    )

    outage = health.load_latest_provider_outage(session_factory)

    assert outage is not None
    assert outage.recovered_at == (OUTAGE_START + timedelta(minutes=5)).replace(tzinfo=UTC)


def test_an_outage_nobody_was_told_about_gets_no_recovery_notice(tmp_path):
    """Deploying after 2026-09-12 must not announce the end of an outage
    that was never announced."""

    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, error_code=BALANCE)
    _attempt(session_factory, at=OUTAGE_START + timedelta(hours=15), status="completed")

    assert _tick(session_factory, OUTAGE_START + timedelta(hours=16))["state"] == (
        "recovered_outage_never_alerted"
    )
    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 0


def test_a_healthy_provider_sends_nothing(tmp_path):
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, status="completed")

    assert _tick(session_factory, OUTAGE_START + timedelta(minutes=1)) == {
        "state": "healthy",
        "rows_read": 1,
    }
    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 0


# --------------------------------------------------------------------------
# Wiring: the production path, not a path each test builds for itself
# --------------------------------------------------------------------------


def test_the_three_types_can_never_be_silenced_by_an_env_line():
    for incident_type in (
        "mimo_provider_unavailable",
        "mimo_provider_recovered",
        "mimo_provider_health_check_failed",
    ):
        assert incident_type in ALWAYS_NOTIFIED_INCIDENT_TYPES, incident_type


def _worker_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_ROLE", "worker")
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED", "true")


def test_the_default_capture_records_under_a_production_like_whitelist(
    monkeypatch, tmp_path
):
    """No injected capture: the real best-effort path with a narrow env list."""

    _worker_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, error_code=BALANCE)

    health.run_mimo_provider_health_tick(
        session_factory, now=(OUTAGE_START + timedelta(seconds=20)).replace(tzinfo=UTC)
    )

    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 1


def test_the_gap_recovery_loop_runs_the_health_tick_by_default():
    parameter = inspect.signature(run_authoritative_gap_recovery_loop).parameters[
        "provider_health_tick"
    ]
    assert parameter.default is health.run_mimo_provider_health_tick
    # The production call site passes nothing, so the default is what runs --
    # and nobody has switched it off there.
    assert "provider_health_tick" not in inspect.getsource(web_app_module)


def test_one_loop_iteration_alerts_without_any_message_arriving(monkeypatch, tmp_path):
    _worker_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=datetime.now(UTC).replace(tzinfo=None), error_code=BALANCE)

    async def run_briefly():
        task = asyncio.create_task(
            run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=None,
                chat_titles_by_id_provider=lambda: {},
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 1


def test_a_failing_health_tick_is_itself_raised_and_does_not_stop_the_loop(
    monkeypatch, tmp_path
):
    _worker_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    calls = {"tick": 0, "expire": 0}

    def broken_tick(factory):
        calls["tick"] += 1
        if calls["tick"] > 5:
            raise asyncio.CancelledError()
        raise RuntimeError("database is locked")

    def counting_expire(factory):
        calls["expire"] += 1

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.expire_stale_deferred_instructions",
        counting_expire,
    )

    async def run_until_tick_cancels():
        with pytest.raises(asyncio.CancelledError):
            await run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=None,
                chat_titles_by_id_provider=lambda: {},
                interval_seconds=0,
                provider_health_tick=broken_tick,
            )

    asyncio.run(run_until_tick_cancels())

    assert calls["expire"] == 5
    failed = _incidents(session_factory, "mimo_provider_health_check_failed")
    assert len(failed) == 1
    assert json.loads(failed[0].redacted_summary)["consecutive_failures"] == 3
    assert "健康检查本身失败" in format_runtime_incident_notification(failed[0])


def test_the_pure_derivation_matches_the_database_one(tmp_path):
    """Step 5 replays production rows through the pure function; it must be
    the same derivation the tick runs, not a second one that could drift."""

    session_factory = _session_factory(tmp_path)
    seeded = [
        (OUTAGE_START, "http_error", BALANCE),
        (OUTAGE_START + timedelta(minutes=5), "http_error", "v1_authoritative_failed"),
        (OUTAGE_START + timedelta(minutes=9), "http_error", BALANCE),
        (OUTAGE_START + timedelta(minutes=70), "completed", None),
    ]
    for at, status, code in seeded:
        _attempt(session_factory, at=at, status=status, error_code=code)

    pure = health.derive_provider_outage(
        [(status, code, at) for at, status, code in reversed(seeded)]
    )

    assert pure == health.load_latest_provider_outage(session_factory)
    assert pure.failures == 2
    assert pure.started_at == OUTAGE_START.replace(tzinfo=UTC)
    assert pure.recovered_at == (OUTAGE_START + timedelta(minutes=70)).replace(tzinfo=UTC)


def test_a_healthy_tick_says_so_in_the_log_with_a_row_count(
    monkeypatch, tmp_path, caplog
):
    """A healthy provider sends nothing; the log line is the only evidence the
    check runs at all, and ``rows_read`` is what makes it able to fail."""

    _worker_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    _attempt(session_factory, at=OUTAGE_START, status="completed")
    caplog.set_level("INFO", logger="telegram_kol_research.telegram_live_listener")

    async def run_briefly():
        task = asyncio.create_task(
            run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=None,
                chat_titles_by_id_provider=lambda: {},
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    heartbeats = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("mimo provider health tick state=")
    ]
    assert heartbeats, "no heartbeat line"
    assert heartbeats[0] == "mimo provider health tick state=healthy rows_read=1 ticks=1"
