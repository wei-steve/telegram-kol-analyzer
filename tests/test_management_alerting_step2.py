"""A-2 focused tests: alerting selectors, uncertain incidents, task supervision.

The 2026-09-07 management-instruction incident had three silent failures:
a delivery whitelist that named none of the management failure types, an
``uncertain`` authoritative execution that produced no incident at all, and a
bot command task that died on one network error and stayed dead for 6h48m.
Each test below fails if its part of that silence comes back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research import web_app as web_app_module
from telegram_kol_research import system_operator_bot as operator_bot_module
from telegram_kol_research.config import (
    MANDATORY_RUNTIME_INCIDENT_TYPES,
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_incidents import record_runtime_incident
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig


NOW = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Task 1: the delivery and capture selectors
# --------------------------------------------------------------------------


def test_management_failure_types_are_folded_into_a_configured_whitelist():
    """The exact production selector, plus the baseline it was missing."""

    config = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": (
                "management_partial_failed,severe_protection_incident"
            ),
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES": (
                "management_partial_failed,severe_protection_incident"
            ),
        },
        env_file_paths=[],
    )

    for incident_type in (
        "management_recovery_required",
        "management_submit_unknown",
        "context_worker_exhausted",
        "authoritative_execution_uncertain",
        "background_task_restart_exhausted",
    ):
        assert config.notifies(incident_type) is True, incident_type
        assert config.captures(incident_type) is True, incident_type
    # The operator's own choices survive untouched.
    assert config.notifies("severe_protection_incident") is True


def test_an_explicitly_empty_selector_stays_a_complete_kill_switch():
    config = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": "",
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES": "",
        },
        env_file_paths=[],
    )

    assert config.telegram_notification_types == frozenset()
    assert config.capture_types == frozenset()
    for incident_type in sorted(MANDATORY_RUNTIME_INCIDENT_TYPES):
        assert config.notifies(incident_type) is False
        assert config.captures(incident_type) is False


def test_an_absent_selector_still_means_every_type():
    config = load_runtime_incident_config(
        environ={"TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "true"},
        env_file_paths=[],
    )

    assert config.telegram_notification_types is None
    assert config.notifies("management_recovery_required") is True


@pytest.mark.parametrize(
    ("summary", "deliverable"),
    [
        ({"operation": "raw_message_15201"}, True),
        ({"operation": "context_backfill_scan"}, False),
        ({"operation": ""}, False),
        ({}, True),
        (None, True),
    ],
)
def test_context_worker_exhausted_delivers_only_per_message_operations(
    summary, deliverable
):
    class _Incident:
        incident_type = "context_worker_exhausted"
        redacted_summary = (
            "not json" if summary is None else json.dumps(summary, sort_keys=True)
        )

    assert (
        operator_bot_module.runtime_incident_payload_is_deliverable(_Incident())
        is deliverable
    )


def test_payload_filter_never_touches_other_incident_types():
    class _Incident:
        incident_type = "management_recovery_required"
        redacted_summary = json.dumps({"operation": "context_backfill_scan"})

    assert (
        operator_bot_module.runtime_incident_payload_is_deliverable(_Incident())
        is True
    )


def _seed(session_factory, **overrides):
    values = {
        "source_kind": "context_resolution_attempt",
        "source_record_id": "41",
        "incident_type": "context_worker_exhausted",
        "severity": "high",
        "fingerprint": "a" * 64,
        "redacted_summary": json.dumps(
            {"operation": "raw_message_15201"}, sort_keys=True
        ),
        "occurred_at": NOW,
        "feature_policy_version": "runtime-incident-phase-2-v1",
        "prompt_version": "none",
        "tool_policy_version": "none",
    }
    values.update(overrides)
    return record_runtime_incident(session_factory, **values)


def test_backfill_context_incidents_are_suppressed_rather_than_delivered(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "suppress.db")
    noisy = _seed(
        session_factory,
        source_record_id="4801",
        fingerprint="b" * 64,
        redacted_summary=json.dumps({"operation": "context_backfill_scan"}),
    )
    real = _seed(session_factory, source_record_id="4865", fingerprint="c" * 64)
    deliveries: list[str] = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(
                telegram_notifications_enabled=True,
                telegram_notification_types=frozenset(
                    {"context_worker_exhausted"}
                ),
            ),
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert len(deliveries) == 1
    with session_factory() as session:
        assert (
            session.get(RuntimeIncident, noisy.id).notification_status
            == "suppressed"
        )
        assert (
            session.get(RuntimeIncident, real.id).notification_status
            == "delivered"
        )


# --------------------------------------------------------------------------
# Task 2: an uncertain authoritative execution now leaves an incident
# --------------------------------------------------------------------------


def _prepared_attempt(tmp_path):
    """Build the exact pre-boundary state the production path reaches."""

    from telegram_kol_research import authoritative_execution_attempts as attempts
    from telegram_kol_research import authoritative_execution_schema as schema
    from telegram_kol_research.models import RawMessage
    from telegram_kol_research.recognition_decisions import (
        RecognitionDecisionRecord,
        save_pending_authoritative_decision,
    )

    session_factory = create_session_factory(tmp_path / "uncertain.db")
    plan = schema.build_recognition_execution_schema_plan(
        session_factory.kw["bind"]
    )
    schema.apply_recognition_execution_schema(
        session_factory.kw["bind"], expected_plan_sha256=plan.plan_sha256
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="平掉一半")
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    decision = save_pending_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="管理",
            authoritative_payload={"recognition_result": "管理"},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="agreed",
            differences=[],
        ),
    )
    generation = str(decision.comparison_claim_token)
    claim = attempts.claim_authoritative_execution_attempt(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        owner=attempts.ExecutionOwnerIdentity(
            runtime_role="worker",
            instance_id="i",
            pid=1,
            boot_id="b",
            process_start_ticks="1",
        ),
        claimed_at=NOW,
        lease_expires_at=datetime(2026, 9, 8, 5, 0, tzinfo=UTC),
    )
    assert attempts.mark_authoritative_side_effect_started(
        session_factory,
        attempt_id=claim.attempt_id,
        raw_message_id=raw_id,
        authoritative_generation=generation,
        claim_token=claim.claim_token,
        started_at=NOW,
    ) is True
    return attempts, session_factory, raw_id, claim


def test_uncertain_authoritative_execution_records_a_high_incident(
    tmp_path, monkeypatch
):
    attempts, session_factory, raw_id, claim = _prepared_attempt(tmp_path)
    monkeypatch.setattr(
        "telegram_kol_research.config.load_runtime_incident_config",
        lambda *a, **k: RuntimeIncidentConfig(
            capture_types=frozenset({"authoritative_execution_uncertain"})
        ),
    )

    assert attempts.mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        uncertain_at=NOW,
        error_class="DeepcoinRequestOutcomeUnknown",
        error_summary="read timed out after submit",
    ) is True

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "authoritative_execution_uncertain"
        assert row.severity == "high"
        assert row.source_kind == "authoritative_execution_attempt"
        assert row.source_record_id == str(claim.attempt_id)
        summary = json.loads(row.redacted_summary)
        assert summary["raw_message_id"] == raw_id
        assert summary["attempt_id"] == claim.attempt_id
        assert summary["error_summary"] == "read timed out after submit"
        assert summary["operation"] == f"raw_message_{raw_id}"
    # The type is in the baseline, so a configured whitelist delivers it.
    assert load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": (
                "management_partial_failed"
            ),
        },
        env_file_paths=[],
    ).notifies("authoritative_execution_uncertain") is True


def test_new_summary_fields_only_ever_carry_integers_or_safe_labels(tmp_path):
    """The summary field set is closed on purpose; A-2 widened it by five keys.

    Widening a redaction contract is only safe while every value going through
    the new keys is either an integer or a ``_safe_label`` result. This drives
    both new adapters with hostile input and checks what actually lands.
    """

    import re

    from telegram_kol_research.runtime_incident_adapters import (
        capture_authoritative_execution_uncertain,
        capture_background_task_restart_exhausted,
    )

    session_factory = create_session_factory(tmp_path / "redaction.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset(
            {
                "authoritative_execution_uncertain",
                "background_task_restart_exhausted",
            }
        )
    )
    capture_authoritative_execution_uncertain(
        session_factory,
        config=config,
        attempt_id=372,
        raw_message_id=15201,
        occurred_at=NOW,
        error_class="DeepcoinRequestOutcomeUnknown",
        error_summary="Authorization: bearer sk-live-abcdef0123456789 leaked",
    )
    capture_background_task_restart_exhausted(
        session_factory,
        config=config,
        task_name="system operator/bot command\u0000task",
        consecutive_failures=10,
        error_type="ReadTimeout",
        occurred_at=NOW,
    )

    safe_label = re.compile(r"[A-Za-z0-9._-]*\Z")
    # error_summary goes through _safe_sentence, which keeps word breaks as
    # spaces so an ordinary message is not mistaken for one opaque token.
    safe_sentence = re.compile(r"[A-Za-z0-9._ -]*\Z")
    new_keys = {
        "attempt_id",
        "consecutive_failures",
        "error_summary",
        "raw_message_id",
        "task_name",
    }
    seen: set[str] = set()
    with session_factory() as session:
        rows = session.query(RuntimeIncident).all()
        assert len(rows) == 2
        for row in rows:
            summary = json.loads(row.redacted_summary)
            for key in new_keys & set(summary):
                seen.add(key)
                value = summary[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    assert isinstance(value, str), (key, value)
                    pattern = (
                        safe_sentence if key == "error_summary" else safe_label
                    )
                    assert pattern.fullmatch(value), (key, value)
                    assert len(value) <= 256, (key, value)

    assert seen == new_keys
    # The credential in the error summary must not have survived at all.
    with session_factory() as session:
        blob = " ".join(
            row.redacted_summary
            for row in session.query(RuntimeIncident).all()
        )
    assert "bearer" not in blob.lower()
    assert "sk-live" not in blob


@pytest.mark.parametrize(
    "error_summary",
    [
        "Deepcoin returned sCode 51004 for ordId 1001125163581378",
        "httpx.ReadTimeout: read timed out while awaiting response headers",
        "DeepcoinRequestOutcomeUnknown: POST /deepcoin/trade/order timed out",
        "ConnectionResetError errno 104 reset by peer during set_position_sltp",
    ],
)
def test_a_realistic_error_summary_still_produces_an_incident(
    tmp_path, error_summary
):
    """The apparent-secret heuristic must not swallow ordinary error text.

    ``_safe_label`` used to join every word with an underscore, which made a
    normal message look like one long mixed-class token -- the exact shape
    ``runtime_incidents`` rejects. ``_capture`` swallows that rejection, so the
    alert would silently never exist.
    """

    from telegram_kol_research.runtime_incident_adapters import (
        capture_authoritative_execution_uncertain,
    )

    session_factory = create_session_factory(tmp_path / "opaque.db")
    capture_authoritative_execution_uncertain(
        session_factory,
        config=RuntimeIncidentConfig(
            capture_types=frozenset({"authoritative_execution_uncertain"})
        ),
        attempt_id=372,
        raw_message_id=15201,
        occurred_at=NOW,
        error_class="DeepcoinRequestOutcomeUnknown",
        error_summary=error_summary,
    )

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        summary = json.loads(row.redacted_summary)
        assert summary["error_summary"], summary
        assert summary["raw_message_id"] == 15201


def test_a_refused_detailed_summary_falls_back_to_a_fixed_label_one(tmp_path):
    """An incident that cannot describe itself is still better than silence."""

    from telegram_kol_research import runtime_incident_adapters as adapters

    session_factory = create_session_factory(tmp_path / "fallback.db")
    calls: list[str] = []

    def recorder(factory, **kwargs):
        calls.append(kwargs["redacted_summary"])
        if "error_summary" in kwargs["redacted_summary"]:
            raise adapters_bounds_error("refused")
        from telegram_kol_research.runtime_incidents import (
            record_runtime_incident,
        )

        return record_runtime_incident(factory, **kwargs)

    from telegram_kol_research.runtime_incidents import (
        RuntimeIncidentBoundsError as adapters_bounds_error,
    )

    adapters.capture_authoritative_execution_uncertain(
        session_factory,
        config=RuntimeIncidentConfig(
            capture_types=frozenset({"authoritative_execution_uncertain"})
        ),
        attempt_id=372,
        raw_message_id=15201,
        occurred_at=NOW,
        error_class="DeepcoinRequestOutcomeUnknown",
        error_summary="anything at all",
        recorder=recorder,
    )

    assert len(calls) == 2
    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        summary = json.loads(row.redacted_summary)
        assert "error_summary" not in summary
        assert summary["raw_message_id"] == 15201
        assert summary["attempt_id"] == 372
        assert summary["operation"] == "raw_message_15201"


def test_uncertain_freeze_still_commits_when_capture_is_disabled(tmp_path):
    """Capture is opt-in; the freeze itself must not depend on it."""

    attempts, session_factory, _raw_id, claim = _prepared_attempt(tmp_path)

    assert attempts.mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=claim.attempt_id,
        claim_token=claim.claim_token,
        uncertain_at=NOW,
        error_class="ProcessLost",
        error_summary="outcome unknown",
    ) is True
    snapshot = attempts.load_authoritative_execution_attempt(
        session_factory, attempt_id=claim.attempt_id
    )
    assert snapshot.status == "uncertain"


def test_incident_capture_failure_never_undoes_the_freeze(tmp_path, monkeypatch):
    """The freeze is committed first; the ledger is best effort after it."""

    from telegram_kol_research import authoritative_execution_attempts as attempts

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(
        "telegram_kol_research.runtime_incident_adapters."
        "capture_authoritative_execution_uncertain",
        _boom,
    )
    attempts._capture_uncertain_incident(
        object(),
        attempt_id=1,
        raw_message_id=2,
        occurred_at=NOW,
        error_class="X",
        error_summary="y",
    )


# --------------------------------------------------------------------------
# Task 3: background task supervision
# --------------------------------------------------------------------------


def _supervision(name="system_operator_bot_command_task"):
    return web_app_module.BackgroundTaskSupervision(name)


def test_supervisor_restarts_with_capped_exponential_backoff(monkeypatch):
    slept: list[float] = []
    attempts = {"count": 0}

    async def fake_sleep(delay):
        slept.append(delay)

    async def always_failing():
        attempts["count"] += 1
        raise RuntimeError("long poll dropped")

    monkeypatch.setattr(web_app_module.asyncio, "sleep", fake_sleep)
    supervision = _supervision()
    asyncio.run(
        web_app_module._supervise_restartable_background_task(
            "system_operator_bot_command_task",
            always_failing,
            supervision=supervision,
        )
    )

    assert attempts["count"] == 10
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert max(slept) == web_app_module._SUPERVISED_RESTART_MAX_DELAY_SECONDS
    assert supervision.consecutive_failures == 10
    assert supervision.restarts == 9
    assert supervision.stopped_at is not None


def test_supervisor_captures_a_critical_incident_when_it_gives_up(monkeypatch):
    captured: list[dict] = []

    async def fake_sleep(delay):
        return None

    async def always_failing():
        raise RuntimeError("long poll dropped")

    def fake_capture(session_factory, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(web_app_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        web_app_module, "capture_background_task_restart_exhausted", fake_capture
    )
    asyncio.run(
        web_app_module._supervise_restartable_background_task(
            "system_operator_bot_command_task",
            always_failing,
            session_factory=object(),
            runtime_config=RuntimeIncidentConfig(
                capture_types=frozenset({"background_task_restart_exhausted"})
            ),
            supervision=_supervision(),
        )
    )

    assert len(captured) == 1
    assert captured[0]["task_name"] == "system_operator_bot_command_task"
    assert captured[0]["consecutive_failures"] == 10
    assert captured[0]["error_type"] == "RuntimeError"


def test_supervisor_resets_the_ladder_after_a_healthy_run(monkeypatch):
    """A blip every few days must not eventually retire the task for good."""

    slept: list[float] = []
    clock = {"value": 0.0}
    runs = {"count": 0}

    async def fake_sleep(delay):
        slept.append(delay)

    async def flaky():
        runs["count"] += 1
        # Every run survives well past the healthy threshold before failing.
        clock["value"] += (
            web_app_module._SUPERVISED_RESTART_HEALTHY_RUN_SECONDS + 1
        )
        if runs["count"] >= 25:
            return None
        raise RuntimeError("long poll dropped")

    monkeypatch.setattr(web_app_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        web_app_module.time, "monotonic", lambda: clock["value"]
    )
    supervision = _supervision()
    asyncio.run(
        web_app_module._supervise_restartable_background_task(
            "system_operator_bot_command_task",
            flaky,
            supervision=supervision,
        )
    )

    assert runs["count"] == 25
    assert supervision.stopped_at is None
    assert set(slept) == {1.0}


def test_supervisor_lets_cancellation_stop_the_task_at_once():
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(3600)

    async def scenario():
        task = asyncio.create_task(
            web_app_module._supervise_restartable_background_task(
                "telegram_bot_command_task",
                forever,
                supervision=_supervision("telegram_bot_command_task"),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_supervisor_does_not_restart_a_clean_return():
    runs = {"count": 0}

    async def one_shot():
        runs["count"] += 1

    supervision = _supervision()
    asyncio.run(
        web_app_module._supervise_restartable_background_task(
            "runtime_incident_notification_task",
            one_shot,
            supervision=supervision,
        )
    )

    assert runs["count"] == 1
    assert supervision.restarts == 0
    assert supervision.stopped_at is None


# --------------------------------------------------------------------------
# Task 4: the health projection
# --------------------------------------------------------------------------


def test_deployment_identity_health_reports_the_two_alerting_tasks():
    from telegram_kol_research.runtime_deployment_identity import (
        build_runtime_deployment_identity,
    )

    async def scenario():
        async def forever():
            await asyncio.sleep(3600)

        alive = asyncio.create_task(forever())
        await asyncio.sleep(0)
        payload = build_runtime_deployment_identity(
            runtime_role="worker",
            module_path=__file__,
            expected_commit="",
            expected_manifest_sha256="",
            tasks={"runtime_incident_notification": alive},
            last_runtime_incident_notified_at="2026-09-08T03:00:00+00:00",
            background_task_supervision={
                "system_operator_bot_command_task": {"restarts": 2}
            },
        )
        alive.cancel()
        return payload

    payload = asyncio.run(scenario())
    health = payload["health"]

    assert health["runtime_incident_notification"] is True
    assert health["system_operator_bot_command"] is False
    assert health["runtime_incident_last_notified_at"] == (
        "2026-09-08T03:00:00+00:00"
    )
    assert health["background_task_supervision"][
        "system_operator_bot_command_task"
    ] == {"restarts": 2}
    # Notification tasks hold no authority and must never grant capability.
    assert payload["capabilities"]["global_exchange_authority"] is False


def test_health_last_notified_comes_from_process_state_not_the_database():
    """The endpoint's no-database contract predates A-2 and still holds."""

    from fastapi.testclient import TestClient

    app = web_app_module.create_web_app(
        database_path=":memory:", runtime_role="worker"
    )
    with TestClient(app) as client:
        app.state.runtime_incident_last_notified_at = (
            "2026-09-08T03:30:00+00:00"
        )

        def failing_session_factory():
            raise AssertionError("deployment identity must not touch the database")

        app.state.session_factory = failing_session_factory
        payload = client.get("/api/runtime/deployment-identity").json()

    assert payload["health"]["runtime_incident_last_notified_at"] == (
        "2026-09-08T03:30:00+00:00"
    )
    assert "runtime_incident_notification" in payload["health"]
    assert "system_operator_bot_command" in payload["health"]


def test_delivery_observer_records_the_instant_a_round_sent_something(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "observer.db")
    _seed(session_factory)
    observed: list[datetime] = []

    async def capture(**kwargs):
        return None

    async def stop_after_one_round(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    monkeypatch.setattr(
        operator_bot_module.asyncio, "sleep", stop_after_one_round
    )

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await operator_bot_module.run_runtime_incident_notification_loop(
                session_factory=session_factory,
                config=SystemOperatorBotConfig("token", "chat"),
                runtime_config=RuntimeIncidentConfig(
                    telegram_notifications_enabled=True,
                    telegram_notification_types=frozenset(
                        {"context_worker_exhausted"}
                    ),
                ),
                delivery_observer=observed.append,
            )

    asyncio.run(scenario())

    assert len(observed) == 1


def test_last_notified_probe_returns_none_on_an_empty_ledger(tmp_path):
    session_factory = create_session_factory(tmp_path / "empty.db")

    assert (
        web_app_module.latest_runtime_incident_notified_at(session_factory)
        is None
    )


def test_last_notified_probe_reports_the_newest_delivery(tmp_path):
    session_factory = create_session_factory(tmp_path / "notified.db")
    incident = _seed(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.notification_status = "delivered"
        row.notified_at = datetime(2026, 9, 8, 3, 30)
        session.commit()

    assert web_app_module.latest_runtime_incident_notified_at(
        session_factory
    ).startswith("2026-09-08T03:30:00")


def test_last_notified_probe_fails_open_when_the_database_is_unusable():
    def broken_factory():
        raise RuntimeError("database locked")

    assert (
        web_app_module.latest_runtime_incident_notified_at(broken_factory)
        is None
    )
