from datetime import UTC, datetime, timedelta
import json

import pytest

import telegram_kol_research.context_resolution_worker as context_worker_module
from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.context_resolution_worker import (
    build_redacted_exchange_state,
    build_context_state_fingerprint,
    claim_next_context_reanalysis,
    run_context_resolution_once,
    schedule_context_reanalysis,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    PositionProtectionLedger,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
    StrategyLifecycle,
    StrategyThread,
)


NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)


def _persist_unresolved(
    session_factory,
    *,
    chat_id=100,
    message_id=10,
    fingerprint="sha256:old",
    triggers=(
        "reply_target_available",
        "exchange_state_changed",
        "strategy_state_changed",
        "message_edited",
        "evidence_version_changed",
    ),
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="更新策略",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint=fingerprint,
            model="deepseek",
            prompt_versions_json="{}",
            request_summary_json="{}",
            decision_json=json.dumps(
                {
                    "decision": "unresolved",
                    "target_thread_ids": [],
                    "management_action": None,
                    "confidence": 0.4,
                    "supporting_message_ids": [message_id],
                    "opposing_message_ids": [],
                    "conflict_types": ["target_ambiguous"],
                    "risk_reducing_fanout_allowed": False,
                    "reanalysis_triggers": list(triggers),
                    "reason": "等待更多上下文",
                }
            ),
            status="completed",
            reanalysis_triggers_json=json.dumps(list(triggers)),
            attempts=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(attempt)
        session.commit()
        return raw.id, attempt.id


@pytest.mark.parametrize(
    ("event_type", "expected_trigger"),
    [
        ("reply_target_available", "reply_target_available"),
        ("entry_leg_status_changed", "strategy_state_changed"),
        ("exchange_snapshot_changed", "exchange_state_changed"),
        ("message_edited", "message_edited"),
        ("evidence_version_changed", "evidence_version_changed"),
    ],
)
def test_reanalysis_is_scheduled_for_supported_context_changes(
    tmp_path,
    event_type,
    expected_trigger,
):
    session_factory = create_session_factory(tmp_path / f"{event_type}.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)

    scheduled = schedule_context_reanalysis(
        session_factory,
        event_type=event_type,
        raw_message_id=raw_id,
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert scheduled == 1
    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        assert attempt.status == "pending_reanalysis"
        assert json.loads(attempt.trigger_event_json)["trigger"] == expected_trigger


def test_next_same_chat_message_schedules_unresolved_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "same-chat.db")
    _, attempt_id = _persist_unresolved(session_factory, chat_id=88)

    scheduled = schedule_context_reanalysis(
        session_factory,
        event_type="next_same_chat_message",
        chat_id=88,
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert scheduled == 1
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "pending_reanalysis"
        )


def test_concurrent_workers_claim_one_generation_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "claim.db")
    raw_id, _ = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    first = claim_next_context_reanalysis(
        session_factory,
        now=NOW,
        stale_before=NOW - timedelta(minutes=5),
    )
    second = claim_next_context_reanalysis(
        session_factory,
        now=NOW,
        stale_before=NOW - timedelta(minutes=5),
    )

    assert first is not None
    assert second is None
    assert first.raw_message_id == raw_id


def test_unchanged_fingerprint_does_not_call_ai(tmp_path):
    session_factory = create_session_factory(tmp_path / "same-fingerprint.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:old",
        reanalyze=lambda *_: (_ for _ in ()).throw(
            AssertionError("unchanged context must not call AI")
        ),
        now=NOW,
    )

    assert result["status"] == "context_unchanged"
    with session_factory() as session:
        assert session.get(ContextResolutionAttempt, attempt_id).status == "completed"


def test_disabled_or_removed_chat_is_never_reanalyzed(tmp_path):
    session_factory = create_session_factory(tmp_path / "disabled.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    calls = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: calls.append("resolver") or {"status": "completed"},
        now=NOW,
        is_eligible=lambda _: False,
    )

    assert result["status"] == "blocked_disabled"
    assert calls == []
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "blocked_disabled"
        )


def test_new_fingerprint_runs_once_and_supersedes_old_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "new-fingerprint.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="exchange_snapshot_changed",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    calls = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda message_id, fingerprint: calls.append(
            (message_id, fingerprint)
        )
        or {"status": "completed"},
        now=NOW,
    )

    assert result["status"] == "completed"
    assert calls == [(raw_id, "sha256:new")]
    with session_factory() as session:
        assert session.get(ContextResolutionAttempt, attempt_id).status == "superseded"


def test_unchanged_exchange_snapshot_does_not_force_a_new_generation(tmp_path):
    session_factory = create_session_factory(tmp_path / "exchange-generation.db")
    raw_id, _attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="exchange_snapshot_changed",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:old",
        reanalyze=lambda *_: (_ for _ in ()).throw(
            AssertionError("unchanged exchange state must not call AI")
        ),
        now=NOW,
    )

    assert result["status"] == "context_unchanged"


def test_candidate_thread_lifecycle_change_updates_state_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "candidate-state.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=1462, text="有入场的更新")
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=1460,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
        )
        session.add_all([raw, lifecycle])
        session.flush()
        thread = StrategyThread(
            chat_id=100,
            root_message_id=1460,
            symbol="BTC",
            side="long",
            status="active",
            current_lifecycle_id=lifecycle.id,
        )
        session.add(thread)
        session.commit()
        raw_id = raw.id
        thread_id = thread.id

    before = build_context_state_fingerprint(
        session_factory,
        raw_id,
        candidate_thread_ids={thread_id},
    )
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        lifecycle.lifecycle_status = "entered"
        session.commit()
    after = build_context_state_fingerprint(
        session_factory,
        raw_id,
        candidate_thread_ids={thread_id},
    )

    assert after != before


def test_redacted_exchange_state_uses_confirmed_db_rows_without_raw_payloads(tmp_path):
    session_factory = create_session_factory(tmp_path / "exchange-state.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=1462, text="有入场的")
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:1460:BTC:long",
            kol_id="group:100",
            chat_id=100,
            message_id=1460,
            symbol="BTC",
            side="long",
            pos_id="secret-pos-id",
            status="open",
            payload_json='{"secret":"must-not-leak"}',
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=1460,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        thread = StrategyThread(
            chat_id=100,
            root_message_id=1460,
            symbol="BTC",
            side="long",
            status="active",
            current_lifecycle_id=lifecycle.id,
        )
        session.add(thread)
        session.flush()
        lifecycle.strategy_thread_id = thread.id
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            pos_id="secret-pos-id",
            attribution_status="verified",
            status="filled",
            response_json='{"secret":"must-not-leak"}',
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionLedger(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="secret-pos-id",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="sl-redacted",
                purpose="stop_loss",
                trigger_price="64000",
                status="verified",
                evidence_source="test",
                evidence_json='{"secret":"must-not-leak"}',
            )
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=raw.id,
                context_fingerprint="sha256:context",
                state_fingerprint="sha256:state",
                model="deepseek",
                prompt_versions_json="{}",
                request_summary_json=json.dumps(
                    {"candidate_strategy_threads": [{"thread_id": thread.id}]}
                ),
                decision_json='{"decision":"unresolved"}',
                status="completed",
                reanalysis_triggers_json='["exchange_state_changed"]',
            )
        )
        session.commit()
        raw_id = raw.id

    state = build_redacted_exchange_state(session_factory, raw_id)
    rendered = json.dumps(state, ensure_ascii=False)

    assert state["entry_legs"][0]["status"] == "filled"
    assert state["protection"][0]["trigger_price"] == "64000"
    assert state["bindings"][0]["position_id_hash"].startswith("sha256:")
    assert "secret-pos-id" not in rendered
    assert "must-not-leak" not in rendered


def test_worker_prefers_persisted_thread_projection_and_falls_back_only_on_null(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "thread-projection.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=1550, text="更新策略")
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:1545:ETH:short",
            kol_id="group:100",
            chat_id=100,
            message_id=1545,
            symbol="ETH",
            side="short",
            status="open",
        )
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=1545,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            execution_binding_id=None,
        )
        session.add_all([raw, binding, lifecycle])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        old_thread = StrategyThread(
            chat_id=100,
            root_message_id=1540,
            symbol="BTC",
            side="long",
            status="active",
        )
        new_thread = StrategyThread(
            chat_id=100,
            root_message_id=1545,
            symbol="ETH",
            side="short",
            status="active",
            current_lifecycle_id=lifecycle.id,
        )
        session.add_all([old_thread, new_thread])
        session.flush()
        lifecycle.strategy_thread_id = new_thread.id
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint="sha256:projection",
            model="deepseek",
            prompt_versions_json="{}",
            request_summary_json=json.dumps(
                {"candidate_strategy_threads": [{"thread_id": old_thread.id}]}
            ),
            candidate_thread_ids_json=json.dumps([new_thread.id]),
            status="completed",
            reanalysis_triggers_json="[]",
        )
        session.add(attempt)
        session.commit()
        raw_id = raw.id
        old_thread_id = old_thread.id
        new_thread_id = new_thread.id
        binding_id = binding.id
        attempt_id = attempt.id

    actual = build_context_state_fingerprint(session_factory, raw_id)
    expected_new = build_context_state_fingerprint(
        session_factory,
        raw_id,
        candidate_thread_ids={new_thread_id},
    )
    expected_old = build_context_state_fingerprint(
        session_factory,
        raw_id,
        candidate_thread_ids={old_thread_id},
    )
    assert actual == expected_new
    assert actual != expected_old
    assert build_redacted_exchange_state(session_factory, raw_id)["bindings"] == [
        {
            "binding_id": binding_id,
            "status": "open",
            "last_exchange_status": None,
            "position_id_hash": None,
        }
    ]

    with session_factory() as session:
        session.get(ContextResolutionAttempt, attempt_id).candidate_thread_ids_json = None
        session.commit()

    assert build_context_state_fingerprint(session_factory, raw_id) == expected_old
    assert build_redacted_exchange_state(session_factory, raw_id)["bindings"] == []


@pytest.mark.parametrize(
    "terminal_status",
    ["submitted", "submit_unknown", "succeeded", "unknown", "reconciled"],
)
def test_terminal_instruction_is_never_replayed(tmp_path, terminal_status):
    session_factory = create_session_factory(tmp_path / f"{terminal_status}.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=raw_id,
            event_type="position_update",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw_id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                idempotency_key=f"terminal-{terminal_status}",
                status=terminal_status,
            )
        )
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: (_ for _ in ()).throw(
            AssertionError("terminal instruction must not replay")
        ),
        now=NOW,
    )

    assert result["status"] == "blocked_execution_terminal"
    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "blocked_execution_terminal"
        )


def test_final_failure_notifies_once_after_bounded_attempts(tmp_path):
    session_factory = create_session_factory(tmp_path / "failure.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        attempt.attempts = 3
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    notices = []

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")),
        notify_final_failure=lambda payload: notices.append(payload),
        max_attempts=3,
        now=NOW,
    )

    assert result["status"] == "exhausted"
    assert len(notices) == 1


def test_reanalysis_network_retry_uses_new_generation_without_duplicate_queue(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "delegated-retry.db")
    raw_id, source_attempt_id = _persist_unresolved(session_factory)
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )

    def persist_new_generation_then_fail(message_id, fingerprint):
        with session_factory() as session:
            session.add(
                ContextResolutionAttempt(
                    raw_message_id=message_id,
                    context_fingerprint=fingerprint,
                    model="deepseek",
                    prompt_versions_json="{}",
                    request_summary_json="{}",
                    decision_json=None,
                    status="retry_pending",
                    error_class="network_error",
                    attempts=1,
                    next_attempt_at=NOW + timedelta(seconds=5),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()
        raise RuntimeError("network retry persisted by resolver")

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=persist_new_generation_then_fail,
        now=NOW,
    )

    assert result["status"] == "retry_scheduled"
    with session_factory() as session:
        rows = session.query(ContextResolutionAttempt).order_by(
            ContextResolutionAttempt.id
        ).all()
        assert len(rows) == 2
        assert session.get(ContextResolutionAttempt, source_attempt_id).status == (
            "superseded"
        )
        assert rows[1].status == "retry_pending"
        assert rows[1].attempts == 1


def test_exhausted_worker_records_incident_only_after_source_state_commits(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "failure-incident.db")
    raw_id, attempt_id = _persist_unresolved(session_factory)
    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        attempt.attempts = 3
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=raw_id,
        occurred_at=NOW,
    )
    captured = []

    def capture_best_effort(adapter, *args, **kwargs):
        with session_factory() as session:
            source = session.get(ContextResolutionAttempt, attempt_id)
            assert source.status == "exhausted"
            source_snapshot = (
                source.status,
                source.last_error,
                source.attempts,
                source.updated_at,
            )
        for _ in range(3):
            adapter(
                session_factory,
                config=RuntimeIncidentConfig(
                    capture_types=frozenset({"context_worker_exhausted"})
                ),
                **kwargs,
            )
        with session_factory() as session:
            source = session.get(ContextResolutionAttempt, attempt_id)
            assert (
                source.status,
                source.last_error,
                source.attempts,
                source.updated_at,
            ) == source_snapshot
        captured.append((adapter, args, kwargs))

    monkeypatch.setattr(
        context_worker_module,
        "capture_runtime_incident_best_effort",
        capture_best_effort,
    )

    result = run_context_resolution_once(
        session_factory,
        context_fingerprint_factory=lambda _: "sha256:new",
        reanalyze=lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")),
        max_attempts=3,
        now=NOW,
    )

    assert result["status"] == "exhausted"
    assert captured == [
        (
            context_worker_module.capture_context_worker_state,
            (session_factory,),
            {
                "attempt_id": attempt_id,
                "raw_message_id": raw_id,
                "status": "exhausted",
                "occurred_at": NOW,
                "error_type": "RuntimeError",
            },
        )
    ]
    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "context_worker_exhausted"
        assert incident.generation == 1
        assert incident.repeat_count == 3
