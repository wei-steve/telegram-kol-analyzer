"""MiMo provider active watch (step-18, step 4): streaks and the daily probe.

Step 1 alerts an outage from the first unavailable failure. Two silences were
left: the same error again and again when it is *not* an outage -- 08-26 and
09-06 had streaks of 400 Bad Request up to 24 long, which the provider answers
and step 1 therefore calls "up" -- and nothing that asks the provider directly
whether it can answer now. These tests pin both.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from telegram_kol_research import mimo_provider_health as health
from telegram_kol_research import mimo_provider_probe as probe_module
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
from telegram_kol_research.models import RawMessage, RuntimeIncident
from telegram_kol_research.runtime_incident_adapters import (
    capture_mimo_provider_failure_streak,
    capture_mimo_provider_probe_failed,
    capture_mimo_provider_unavailable,
)
from telegram_kol_research.system_operator_bot import (
    format_runtime_incident_notification,
)
from telegram_kol_research.telegram_live_listener import (
    run_authoritative_gap_recovery_loop,
)


T0 = datetime(2026, 9, 13, 10, 0, 0)  # naive UTC, as SQLite returns it
CONFIG = RuntimeIncidentConfig(
    capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES),
    telegram_notifications_enabled=True,
)
REJECTED = "mimo_request_rejected.http_400"
BALANCE = "mimo_provider_unavailable.insufficient_balance.http_402"
TIMEOUT = "mimo_provider_unavailable.timeout"
SECRET_KEY = "sk-step4-probe-key-must-never-leave-the-header-0123456789"


def _factory(tmp_path, name="step4.db"):
    session_factory = create_session_factory(tmp_path / name)
    with session_factory() as session:
        session.add(RawMessage(id=1, chat_id=100, message_id=1, text="BTC 多", posted_at=T0))
        session.commit()
    return session_factory


def _attempt(
    session_factory,
    *,
    completed_at,
    status="http_error",
    error_code=None,
    provider_request_count=2,
    started_at=None,
):
    run = start_mimo_run(
        session_factory,
        raw_message_id=1,
        run_kind="v1_authoritative",
        contract_version="v1",
        model="mimo-v2.5",
        input_kind="text",
        input_fingerprint="fp",
        prompt_versions={},
        started_at=started_at or completed_at,
    )
    record_mimo_attempt(
        session_factory,
        run_id=run.id,
        ordinal=1,
        status=status,
        error_code=error_code,
        error_message=None if status == "completed" else "failed",
        duration_ms=0,
        started_at=started_at or completed_at,
        completed_at=completed_at,
        attempt_phase="v1_authoritative",
        provider_request_count=provider_request_count,
    )


def _incidents(session_factory, incident_type):
    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == incident_type)
            .order_by(RuntimeIncident.id.asc())
            .all()
        )


def _streak_tick(session_factory, now):
    return health.run_mimo_failure_streak_tick(
        session_factory,
        now=now.replace(tzinfo=UTC),
        capture=lambda factory, **kwargs: capture_mimo_provider_failure_streak(
            factory, config=CONFIG, **kwargs
        ),
    )


def _worker_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_ROLE", "worker")
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED", "true")


# --------------------------------------------------------------------------
# Streaks
# --------------------------------------------------------------------------


def test_five_rejected_requests_in_a_row_alert_once_and_say_why(tmp_path):
    session_factory = _factory(tmp_path)
    for index in range(4):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)
    assert _streak_tick(session_factory, T0 + timedelta(minutes=4))["state"] == "no_streak"

    _attempt(session_factory, completed_at=T0 + timedelta(minutes=4), error_code=REJECTED)
    assert _streak_tick(session_factory, T0 + timedelta(minutes=5))["state"] == "streak_alerted"
    # The same streak growing is the same streak.
    _attempt(session_factory, completed_at=T0 + timedelta(minutes=5), error_code=REJECTED)
    assert _streak_tick(session_factory, T0 + timedelta(minutes=6))["state"] == (
        "streak_already_alerted"
    )

    alerts = _incidents(session_factory, "mimo_provider_failure_streak")
    assert len(alerts) == 1
    summary = json.loads(alerts[0].redacted_summary)
    assert summary["consecutive_failures"] == 5
    assert summary["reason_code"] == "request_rejected"
    text_out = format_runtime_incident_notification(alerts[0])
    assert "MiMo 识别连续失败" in text_out
    assert "请求被供应商拒绝" in text_out
    assert "HTTP 400" in text_out
    assert "同一错误连续: 5 次" in text_out
    assert "2026-09-13T10:00Z" in text_out


def test_a_success_in_the_middle_starts_the_count_again(tmp_path):
    session_factory = _factory(tmp_path)
    for index in range(4):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)
    _attempt(session_factory, completed_at=T0 + timedelta(minutes=4), status="completed")
    for index in range(5, 9):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)

    assert _streak_tick(session_factory, T0 + timedelta(minutes=9))["state"] == "no_streak"
    assert _incidents(session_factory, "mimo_provider_failure_streak") == []


def test_a_different_code_that_reached_the_provider_breaks_the_run(tmp_path):
    session_factory = _factory(tmp_path)
    codes = [REJECTED] * 3 + ["mimo_response_invalid"] + [REJECTED] * 2
    for index, code in enumerate(codes):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=code)

    assert _streak_tick(session_factory, T0 + timedelta(minutes=7))["state"] == "no_streak"


def test_attempts_that_never_reached_the_provider_neither_count_nor_break(tmp_path):
    session_factory = _factory(tmp_path)
    for index in range(3):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)
    # An empty message in the middle: no request was made.
    for index in (3, 4):
        _attempt(
            session_factory,
            completed_at=T0 + timedelta(minutes=index),
            error_code="v1_authoritative_failed",
            provider_request_count=0,
        )
    assert _streak_tick(session_factory, T0 + timedelta(minutes=5))["state"] == "no_streak"
    for index in (5, 6):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)

    assert _streak_tick(session_factory, T0 + timedelta(minutes=7))["state"] == "streak_alerted"
    (alert,) = _incidents(session_factory, "mimo_provider_failure_streak")
    assert json.loads(alert.redacted_summary)["consecutive_failures"] == 5


def test_legacy_rows_without_a_request_count_are_counted(tmp_path):
    """Production has 173 failed rows from before the column existed."""

    session_factory = _factory(tmp_path)
    for index in range(5):
        _attempt(
            session_factory,
            completed_at=T0 + timedelta(minutes=index),
            error_code="v1_authoritative_failed",
            provider_request_count=None,
        )

    assert _streak_tick(session_factory, T0 + timedelta(minutes=5))["state"] == "streak_alerted"
    (alert,) = _incidents(session_factory, "mimo_provider_failure_streak")
    assert "未能分类的失败" in format_runtime_incident_notification(alert)


def test_the_run_is_read_in_completion_order_not_id_order(tmp_path):
    session_factory = _factory(tmp_path)
    # Ids 1..5 are the rejected requests, id 6 is a success -- but the success
    # completed in the middle of them.
    for minute in (0, 1, 2, 4, 5):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=minute), error_code=REJECTED)
    _attempt(session_factory, completed_at=T0 + timedelta(minutes=3), status="completed")
    assert _streak_tick(session_factory, T0 + timedelta(minutes=6))["state"] == "no_streak"

    # And the other way round: ids out of time order still make one run.
    other = _factory(tmp_path, name="other.db")
    for minute in (4, 0, 3, 1, 2):
        _attempt(other, completed_at=T0 + timedelta(minutes=minute), error_code=REJECTED)
    assert _streak_tick(other, T0 + timedelta(minutes=6))["state"] == "streak_alerted"
    (alert,) = _incidents(other, "mimo_provider_failure_streak")
    assert json.loads(alert.redacted_summary)["episode_started_at"] == "2026-09-13T10:00Z"


def test_a_streak_from_before_the_freshness_window_is_not_news(tmp_path):
    session_factory = _factory(tmp_path)
    for index in range(6):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)

    assert _streak_tick(session_factory, T0 + timedelta(minutes=5, seconds=1) + timedelta(minutes=30))[
        "state"
    ] == "no_streak"
    assert _incidents(session_factory, "mimo_provider_failure_streak") == []


def test_an_outage_step_1_already_announced_is_not_announced_twice(tmp_path):
    session_factory = _factory(tmp_path)
    for index in range(5):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=BALANCE)
    health.run_mimo_provider_health_tick(
        session_factory,
        now=(T0 + timedelta(minutes=5)).replace(tzinfo=UTC),
        capture_unavailable=lambda factory, **kwargs: capture_mimo_provider_unavailable(
            factory, config=CONFIG, **kwargs
        ),
    )
    assert len(_incidents(session_factory, "mimo_provider_unavailable")) == 1

    assert _streak_tick(session_factory, T0 + timedelta(minutes=5))["state"] == (
        "covered_by_outage_alert"
    )
    assert _incidents(session_factory, "mimo_provider_failure_streak") == []


def test_an_unavailable_streak_nobody_was_told_about_is_alerted(tmp_path):
    """If step 1's alert could not be recorded, the streak is the fallback."""

    session_factory = _factory(tmp_path)
    for index in range(5):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=BALANCE)

    assert _streak_tick(session_factory, T0 + timedelta(minutes=5))["state"] == "streak_alerted"
    (alert,) = _incidents(session_factory, "mimo_provider_failure_streak")
    assert "余额不足" in format_runtime_incident_notification(alert)


def test_five_isolated_timeouts_in_a_row_are_a_streak_even_though_no_outage(tmp_path):
    """Rule A calls each of these isolated -- a quicker request succeeded while
    each one hung -- so step 1 opens no outage. Five in a row is still news."""

    session_factory = _factory(tmp_path)
    _attempt(
        session_factory,
        completed_at=T0 + timedelta(minutes=2),
        started_at=T0 + timedelta(minutes=1),
        status="completed",
    )
    for index in range(5):
        _attempt(
            session_factory,
            completed_at=T0 + timedelta(minutes=10, seconds=index),
            started_at=T0,
            error_code=TIMEOUT,
        )
    assert health.load_latest_provider_outage(session_factory) is None

    assert _streak_tick(session_factory, T0 + timedelta(minutes=11))["state"] == "streak_alerted"
    (alert,) = _incidents(session_factory, "mimo_provider_failure_streak")
    assert "请求超时" in format_runtime_incident_notification(alert)


def test_the_pure_streak_derivation_matches_the_database_one(tmp_path):
    session_factory = _factory(tmp_path)
    seeded = [(minute, REJECTED, 2) for minute in range(5)] + [(6, "v1_authoritative_failed", 0)]
    for minute, code, count in seeded:
        _attempt(
            session_factory,
            completed_at=T0 + timedelta(minutes=minute),
            error_code=code,
            provider_request_count=count,
        )
    rows = health._load_streak_rows(session_factory, scan_limit=200)

    pure = health.derive_failure_streaks(
        [
            (index + 1, "http_error", code, count, T0 + timedelta(minutes=minute))
            for index, (minute, code, count) in enumerate(seeded)
        ]
    )

    assert pure == health.derive_failure_streaks(rows)
    assert [(s.error_code, s.failures, s.first_attempt_id) for s in pure] == [(REJECTED, 5, 1)]


def test_the_streak_default_capture_records_under_a_production_like_whitelist(
    monkeypatch, tmp_path
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)
    for index in range(5):
        _attempt(session_factory, completed_at=T0 + timedelta(minutes=index), error_code=REJECTED)

    health.run_mimo_failure_streak_tick(
        session_factory, now=(T0 + timedelta(minutes=5)).replace(tzinfo=UTC)
    )

    assert len(_incidents(session_factory, "mimo_provider_failure_streak")) == 1


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------


#: The shape the production provider returned on 2026-09-13 for max_tokens=1
#: (keys and types from a real probe; ids and counts are placeholders).
REAL_PROBE_ANSWER = {
    "id": "probe-answer",
    "object": "chat.completion",
    "created": 1789321000,
    "model": "mimo-v2.5",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "", "tool_calls": None},
            "finish_reason": "length",
        }
    ],
    "usage": {
        "completion_tokens": 1,
        "prompt_tokens": 248,
        "total_tokens": 249,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 192},
    },
}

MODEL = SimpleNamespace(
    id="mimo-v2.5",
    model="mimo-v2.5",
    base_url="https://api.xiaomimimo.com/v1/",
    api_key=SECRET_KEY,
    timeout_seconds=60.0,
)


def _client_factory(respond, sent):
    class FakeClient:
        def __init__(self, *, timeout):
            sent.append({"timeout": timeout})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, *, headers, json):
            sent.append({"url": url, "headers": headers, "json": json})
            request = httpx.Request("POST", url)
            return respond(request)

    return FakeClient


def _config_loader():
    return SimpleNamespace(ai_models=[MODEL])


def _probe_with(respond, sent):
    return lambda model: probe_module.probe_mimo_provider(
        model, client_factory=_client_factory(respond, sent)
    )


def _run_probe(session_factory, respond, *, now=T0, sent=None, loader=_config_loader):
    sent = [] if sent is None else sent
    return probe_module.run_mimo_provider_probe(
        session_factory,
        config_loader=loader,
        now=now.replace(tzinfo=UTC),
        probe=_probe_with(respond, sent),
        capture=lambda factory, **kwargs: capture_mimo_provider_probe_failed(
            factory, config=CONFIG, **kwargs
        ),
    )


def _business_rows(session_factory):
    with session_factory() as session:
        return {
            table: session.execute(text(f"select count(*) from {table}")).scalar()
            for table in ("mimo_recognition_attempts", "mimo_recognition_runs", "ai_prompt_invocations")
        }


def _attach(caplog, name):
    """``configure_application_logging`` turns propagation off on the package
    logger once any earlier test ran it, so the root caplog handler may never
    see these lines; attach the handler to the module logger itself. And turn
    propagation off here too: when nothing configured logging, the same line
    would otherwise reach caplog twice and every count would be doubled."""

    target = logging.getLogger(name)
    previous = (target.level, target.propagate)
    target.addHandler(caplog.handler)
    target.setLevel(logging.INFO)
    target.propagate = False
    return target, previous


def _detach(caplog, target, previous):
    target.removeHandler(caplog.handler)
    target.setLevel(previous[0])
    target.propagate = previous[1]


def test_a_real_shaped_answer_is_ok_writes_nothing_and_logs_one_line(tmp_path, caplog):
    session_factory = _factory(tmp_path)
    sent = []
    target, previous = _attach(caplog, "telegram_kol_research.mimo_provider_probe")
    try:
        result = _run_probe(
            session_factory,
            lambda request: httpx.Response(200, json=REAL_PROBE_ANSWER, request=request),
            sent=sent,
        )
    finally:
        _detach(caplog, target, previous)

    assert result["state"] == "probe_ok"
    assert result["http_status"] == 200
    call = sent[1]
    assert call["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert call["json"]["max_tokens"] == 1
    assert call["json"]["model"] == "mimo-v2.5"
    assert call["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"
    assert sent[0]["timeout"] == probe_module.PROBE_TIMEOUT_SECONDS
    assert _business_rows(session_factory) == {
        "mimo_recognition_attempts": 0,
        "mimo_recognition_runs": 0,
        "ai_prompt_invocations": 0,
    }
    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 0
    assert "mimo provider probe ok http_status=200" in caplog.text
    assert SECRET_KEY not in caplog.text


def test_a_402_probe_alerts_once_a_day_in_words_and_never_leaks_the_key(tmp_path, caplog):
    session_factory = _factory(tmp_path)
    refuse = lambda request: httpx.Response(  # noqa: E731
        402, text=f"insufficient balance for {SECRET_KEY}", request=request
    )
    target, previous = _attach(caplog, "telegram_kol_research.mimo_provider_probe")
    try:
        assert _run_probe(session_factory, refuse)["state"] == "probe_failed_alerted"
        assert _run_probe(session_factory, refuse, now=T0 + timedelta(hours=6))["state"] == (
            "probe_failed_already_alerted"
        )
        assert _run_probe(session_factory, refuse, now=T0 + timedelta(days=1))["state"] == (
            "probe_failed_alerted"
        )
    finally:
        _detach(caplog, target, previous)

    alerts = _incidents(session_factory, "mimo_provider_probe_failed")
    assert len(alerts) == 2
    text_out = format_runtime_incident_notification(alerts[0])
    assert "MiMo 每日探测失败" in text_out
    assert "余额不足" in text_out
    assert "HTTP 402" in text_out
    assert "同一天只告警一次" in text_out
    assert "mimo provider probe failed failure_class=provider_unavailable" in caplog.text
    for alert in alerts:
        assert SECRET_KEY not in (alert.redacted_summary or "")
        assert SECRET_KEY not in format_runtime_incident_notification(alert)
    assert SECRET_KEY not in caplog.text
    assert _business_rows(session_factory)["mimo_recognition_attempts"] == 0


def test_a_probe_timeout_is_named_as_one(tmp_path):
    session_factory = _factory(tmp_path)

    def hang(request):
        raise httpx.ReadTimeout("timed out", request=request)

    assert _run_probe(session_factory, hang)["state"] == "probe_failed_alerted"
    (alert,) = _incidents(session_factory, "mimo_provider_probe_failed")
    assert "请求超时" in format_runtime_incident_notification(alert)


def test_an_answer_without_choices_is_an_invalid_answer_not_ok(tmp_path):
    session_factory = _factory(tmp_path)
    empty = {key: value for key, value in REAL_PROBE_ANSWER.items() if key != "choices"}

    result = _run_probe(
        session_factory, lambda request: httpx.Response(200, json=empty, request=request)
    )

    assert result == {
        "state": "probe_failed_alerted",
        "failure_class": "response_invalid",
        "recognition_ok": False,
    }
    (alert,) = _incidents(session_factory, "mimo_provider_probe_failed")
    assert "内容没通过校验" in format_runtime_incident_notification(alert)


def test_no_mimo_model_and_an_unreadable_config_are_failures_not_skips(tmp_path):
    session_factory = _factory(tmp_path)
    never = lambda request: pytest.fail("no request without a model")  # noqa: E731

    assert _run_probe(
        session_factory, never, loader=lambda: SimpleNamespace(ai_models=[])
    )["failure_class"] == "mimo_model_not_configured"

    def unreadable():
        raise FileNotFoundError("config/ai_recognition.yaml")

    other = _factory(tmp_path, name="other.db")
    assert _run_probe(other, never, loader=unreadable)["failure_class"] == "ai_config_unreadable"

    assert "找不到 mimo-v2.5" in format_runtime_incident_notification(
        _incidents(session_factory, "mimo_provider_probe_failed")[0]
    )
    assert "读不到识别配置" in format_runtime_incident_notification(
        _incidents(other, "mimo_provider_probe_failed")[0]
    )


def test_the_probe_default_capture_records_under_a_production_like_whitelist(
    monkeypatch, tmp_path
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)

    probe_module.run_mimo_provider_probe(
        session_factory,
        config_loader=_config_loader,
        now=T0.replace(tzinfo=UTC),
        probe=_probe_with(
            lambda request: httpx.Response(401, text="bad key", request=request), []
        ),
    )

    (alert,) = _incidents(session_factory, "mimo_provider_probe_failed")
    assert "鉴权被拒" in format_runtime_incident_notification(alert)


def test_a_failed_probe_says_so_when_recognition_is_answering(tmp_path):
    """Ruling of 2026-09-13: a probe failure next to working recognition must
    not read as an outage. Same probe, three recognition histories."""

    refuse = lambda request: httpx.Response(402, text="no", request=request)  # noqa: E731

    working = _factory(tmp_path, name="working.db")
    _attempt(working, completed_at=T0 - timedelta(minutes=10), status="completed")
    # Never reached the provider: says nothing either way.
    _attempt(
        working,
        completed_at=T0 - timedelta(minutes=5),
        error_code="v1_authoritative_failed",
        provider_request_count=0,
    )
    assert _run_probe(working, refuse)["recognition_ok"] is True
    (alert,) = _incidents(working, "mimo_provider_probe_failed")
    assert json.loads(alert.redacted_summary)["incident_state"] == "probe_failed_recognition_ok"
    text_ok = format_runtime_incident_notification(alert)
    assert "探测失败但识别正常" in text_ok
    assert "供应商此刻可能无法完成识别" not in text_ok

    failing = _factory(tmp_path, name="failing.db")
    _attempt(failing, completed_at=T0 - timedelta(minutes=10), status="completed")
    _attempt(failing, completed_at=T0 - timedelta(minutes=5), error_code=REJECTED)
    assert _run_probe(failing, refuse)["recognition_ok"] is False

    quiet = _factory(tmp_path, name="quiet.db")
    _attempt(quiet, completed_at=T0 - timedelta(minutes=40), status="completed")
    assert _run_probe(quiet, refuse)["recognition_ok"] is False

    for factory in (failing, quiet):
        (alert,) = _incidents(factory, "mimo_provider_probe_failed")
        assert json.loads(alert.redacted_summary)["incident_state"] == "probe_failed"
        text_out = format_runtime_incident_notification(alert)
        assert "识别正常" not in text_out
        assert "供应商此刻可能无法完成识别" in text_out


def test_both_types_can_never_be_silenced_by_an_env_line():
    for incident_type in ("mimo_provider_failure_streak", "mimo_provider_probe_failed"):
        assert incident_type in ALWAYS_NOTIFIED_INCIDENT_TYPES, incident_type


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_the_loop_runs_both_by_default_and_web_app_gives_the_probe_its_config():
    parameters = inspect.signature(run_authoritative_gap_recovery_loop).parameters
    assert parameters["provider_failure_streak_tick"].default is health.run_mimo_failure_streak_tick
    assert parameters["provider_probe"].default is probe_module.run_mimo_provider_probe
    assert parameters["provider_probe_interval_seconds"].default == 24 * 3600

    source = inspect.getsource(web_app_module)
    start = source.index("app.state.authoritative_gap_recovery_runner(")
    call = source[start : source.index("add_done_callback", start)]
    assert "ai_recognition_config_loader=" in call
    assert "provider_failure_streak_tick" not in call
    assert "provider_probe=" not in call


def _run_loop_for(seconds, **kwargs):
    options = {
        "authoritative_processor": None,
        "chat_titles_by_id_provider": lambda: {},
        "interval_seconds": 0.01,
        "provider_health_tick": None,
        "provider_outage_replay_tick": None,
        **kwargs,
    }

    async def run_briefly():
        task = asyncio.create_task(run_authoritative_gap_recovery_loop(**options))
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())


def test_one_loop_iteration_alerts_a_streak_without_any_message_arriving(
    monkeypatch, tmp_path
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    for index in range(5):
        _attempt(session_factory, completed_at=now - timedelta(seconds=10 - index), error_code=REJECTED)

    _run_loop_for(0.3, session_factory=session_factory, provider_probe=None)

    assert len(_incidents(session_factory, "mimo_provider_failure_streak")) == 1


def test_the_probe_runs_on_the_first_iteration_and_then_only_per_interval(tmp_path):
    session_factory = _factory(tmp_path)
    daily, frequent = [], []

    def recording(calls):
        def run(factory, **kwargs):
            calls.append(kwargs)
            return {"state": "probe_ok"}

        return run

    _run_loop_for(
        0.3,
        session_factory=session_factory,
        provider_failure_streak_tick=None,
        provider_probe=recording(daily),
        ai_recognition_config_loader=_config_loader,
    )
    _run_loop_for(
        0.3,
        session_factory=session_factory,
        provider_failure_streak_tick=None,
        provider_probe=recording(frequent),
        ai_recognition_config_loader=_config_loader,
        provider_probe_interval_seconds=0.05,
    )

    assert daily == [{"config_loader": _config_loader}]
    assert len(frequent) >= 3


def test_a_probe_without_a_config_loader_is_logged_once_not_run(tmp_path, caplog):
    session_factory = _factory(tmp_path)
    calls = []
    target, previous = _attach(caplog, "telegram_kol_research.telegram_live_listener")
    try:
        _run_loop_for(
            0.2,
            session_factory=session_factory,
            provider_failure_streak_tick=None,
            provider_probe=lambda factory, **kwargs: calls.append(kwargs),
            provider_probe_interval_seconds=0.01,
        )
    finally:
        _detach(caplog, target, previous)

    assert calls == []
    assert caplog.text.count("mimo provider probe skipped state=no_config_loader") == 1


def test_failing_checks_are_raised_naming_the_task_and_do_not_stop_the_loop(
    monkeypatch, tmp_path
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)

    def broken(factory, **kwargs):
        raise RuntimeError("database is locked")

    _run_loop_for(
        0.3,
        session_factory=session_factory,
        provider_health_tick=broken,
        provider_failure_streak_tick=broken,
        provider_probe=broken,
        ai_recognition_config_loader=_config_loader,
    )

    failed = _incidents(session_factory, "mimo_provider_health_check_failed")
    by_task = {json.loads(row.redacted_summary)["task_name"]: row for row in failed}
    assert set(by_task) == {
        "mimo_provider_health_tick",
        "mimo_provider_failure_streak_tick",
        "mimo_provider_probe_tick",
    }
    # One failing check cannot hide another, and the health tick keeps the
    # dedup key its production rows already carry.
    assert {task: row.source_record_id for task, row in by_task.items()} == {
        "mimo_provider_health_tick": "health_tick_failures_3",
        "mimo_provider_failure_streak_tick": "mimo_provider_failure_streak_tick_failures_3",
        "mimo_provider_probe_tick": "mimo_provider_probe_tick_failures_1",
    }
    health_text = format_runtime_incident_notification(by_task["mimo_provider_health_tick"])
    assert "供应商健康检查本身失败" in health_text
    assert "mimo provider health tick failed" in health_text
    assert json.loads(by_task["mimo_provider_failure_streak_tick"].redacted_summary)[
        "consecutive_failures"
    ] == 3
    assert json.loads(by_task["mimo_provider_probe_tick"].redacted_summary)[
        "consecutive_failures"
    ] == 1
    streak_text = format_runtime_incident_notification(by_task["mimo_provider_failure_streak_tick"])
    assert "连续失败检查本身失败" in streak_text
    assert "mimo provider failure streak tick failed" in streak_text
    probe_text = format_runtime_incident_notification(by_task["mimo_provider_probe_tick"])
    assert "每日探测本身失败" in probe_text
    assert "mimo provider probe tick failed" in probe_text
