import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Query

import telegram_kol_research.message_processing_worker as worker_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.context_resolution_worker import (
    run_context_resolution_once,
    schedule_context_reanalysis,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.message_processing_worker import (
    claim_message_processing_jobs,
    process_message_job,
    run_message_processing_worker_loop,
    run_message_processing_worker_tick,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    claim_authoritative_execution,
    save_pending_authoritative_decision,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _processing_result():
    return SimpleNamespace(
        recognition=SimpleNamespace(status="非策略"),
        assessment=SimpleNamespace(agreement_status="agreed"),
        automation={"status": "skipped", "reason": "mimo_no_action"},
    )


def _authoritative_failure_result(reason):
    return SimpleNamespace(
        recognition=SimpleNamespace(status="识别失败", reason=reason),
        assessment=SimpleNamespace(
            agreement_status="authoritative_failed",
            mimo=SimpleNamespace(error_message=reason),
        ),
        automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
    )


def test_process_message_job_runs_the_post_persist_chain_from_raw_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "worker.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=123,
            message_id=42,
            sender_name="Alice Trader",
            text="BTC 多",
            posted_at=NOW,
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)
        raw_message_id = raw.id

    processed = []
    scheduled = []
    context_runs = []

    result = asyncio.run(
        process_message_job(
            session_factory,
            raw_message_id=raw_message_id,
            authoritative_processor=lambda candidate_id: (
                processed.append(candidate_id) or _processing_result()
            ),
            context_resolution_scheduler=lambda **event: scheduled.append(event),
            context_resolution_worker=lambda: context_runs.append("ran"),
        )
    )

    assert processed == [raw_message_id]
    assert result.recognition.status == "非策略"
    assert [event["event_type"] for event in scheduled] == [
        "next_same_chat_message",
        "evidence_version_changed",
    ]
    assert context_runs == ["ran"]


def test_legacy_nested_context_reanalysis_failure_is_re_raised_to_outer_job(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "nested-context-job.db")
    outer_raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    with session_factory() as session:
        nested_raw = RawMessage(
            chat_id=2,
            message_id=2,
            text="nested message",
            posted_at=NOW,
        )
        session.add(nested_raw)
        session.flush()
        nested_raw_id = int(nested_raw.id)
        session.add(
            ContextResolutionAttempt(
                raw_message_id=nested_raw_id,
                context_fingerprint="sha256:old",
                model="deepseek",
                prompt_versions_json="{}",
                request_summary_json="{}",
                decision_json=(
                    '{"decision":"unresolved","reanalysis_triggers":'
                    '["message_edited"]}'
                ),
                status="completed",
                reanalysis_triggers_json='["message_edited"]',
                attempts=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=nested_raw_id,
        occurred_at=NOW,
    )

    def reanalyze(message_id, _fingerprint, **_):
        saved = save_pending_authoritative_decision(
            session_factory,
            RecognitionDecisionRecord(
                raw_message_id=message_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload={"recognition_result": "非策略"},
                auxiliary_model=None,
                auxiliary_status=None,
                auxiliary_payload=None,
                agreement_status="agreed",
                differences=[],
            ),
        )
        assert claim_authoritative_execution(
            session_factory,
            raw_message_id=message_id,
            authoritative_generation=str(saved.comparison_claim_token),
        )
        raise RuntimeError("nested execution failed after claim")

    def context_worker():
        return run_context_resolution_once(
            session_factory,
            context_fingerprint_factory=lambda _: "sha256:new",
            reanalyze=reanalyze,
            now=NOW,
        )

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: _processing_result(),
                "context_resolution_worker": context_worker,
            },
        )
    )

    assert result.succeeded == 0
    assert result.retried == 1
    assert result.failed == 0
    with session_factory() as session:
        outer_job = session.query(MessageProcessingJob).filter_by(
            raw_message_id=outer_raw_id
        ).one()
        nested_decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=nested_raw_id
        ).one()
        assert outer_job.status == "pending"
        assert outer_job.last_reason == "processing_error:RuntimeError"
        assert nested_decision.comparison_status == "execution_running"


def test_real_leased_nested_failure_is_classified_and_outer_job_is_not_succeeded(
    tmp_path, monkeypatch
):
    from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
    from telegram_kol_research.authoritative_execution_attempts import (
        ExecutionOwnerIdentity,
    )
    from telegram_kol_research.authoritative_execution_schema import (
        apply_recognition_execution_schema,
        build_recognition_execution_schema_plan,
    )
    from telegram_kol_research.authoritative_recognition import (
        AuthoritativeAssessment,
        process_authoritative_message,
    )
    from telegram_kol_research.models import AuthoritativeExecutionAttempt
    from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult

    session_factory = create_session_factory(tmp_path / "nested-leased-failure.db")
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    outer_raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    with session_factory() as session:
        nested_raw = RawMessage(
            chat_id=2,
            message_id=2,
            text="nested message",
            posted_at=NOW,
        )
        session.add(nested_raw)
        session.flush()
        nested_raw_id = int(nested_raw.id)
        session.add(
            ContextResolutionAttempt(
                raw_message_id=nested_raw_id,
                context_fingerprint="sha256:old",
                model="deepseek",
                prompt_versions_json="{}",
                request_summary_json="{}",
                decision_json=(
                    '{"decision":"unresolved","reanalysis_triggers":'
                    '["message_edited"]}'
                ),
                status="completed",
                reanalysis_triggers_json='["message_edited"]',
                attempts=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    schedule_context_reanalysis(
        session_factory,
        event_type="message_edited",
        raw_message_id=nested_raw_id,
        occurred_at=NOW,
    )
    original = RuntimeError("apply failed after durable claim")

    def assess(_session_factory, *, raw_message_id, **_kwargs):
        saved = save_pending_authoritative_decision(
            session_factory,
            RecognitionDecisionRecord(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload={"recognition_result": "非策略"},
                auxiliary_model=None,
                auxiliary_status=None,
                auxiliary_payload=None,
                agreement_status="pending",
                differences=[],
            ),
        )
        return AuthoritativeAssessment(
            raw_message_id=raw_message_id,
            mimo=MimoAuthoritativeResult(
                raw_message_id=raw_message_id,
                payload={"recognition_result": "非策略"},
                input_kind="text",
                model="mimo-v2.5",
                status="非策略",
            ),
            deepseek_payload=None,
            agreement_status="pending",
            differences=[],
            semantic_review_status="execution_pending",
            authoritative_generation=str(saved.comparison_claim_token),
        )

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.assess_message_authoritatively",
        assess,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: (_ for _ in ()).throw(original),
    )
    owner = ExecutionOwnerIdentity("worker", "instance", 123, "boot", "456")

    def context_worker():
        return run_context_resolution_once(
            session_factory,
            context_fingerprint_factory=lambda _: "sha256:new",
            reanalyze=lambda message_id, _fingerprint, **_: process_authoritative_message(
                session_factory,
                raw_message_id=message_id,
                ai_recognition_config=AiRecognitionConfig(),
                media_root=tmp_path,
                auto_trade_executor=lambda _: pytest.fail("adapter must not run"),
                execution_owner=owner,
            ),
            now=NOW,
        )

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: _processing_result(),
                "context_resolution_worker": context_worker,
            },
        )
    )

    assert result.succeeded == 0
    assert result.retried == 1
    with session_factory() as session:
        outer_job = session.query(MessageProcessingJob).filter_by(
            raw_message_id=outer_raw_id
        ).one()
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=nested_raw_id
        ).one()
        attempt = session.query(AuthoritativeExecutionAttempt).one()
        assert outer_job.status == "pending"
        assert decision.comparison_status == "completed"
        assert decision.automation_reason == (
            "authoritative_execution_abandoned_before_side_effect"
        )
        assert attempt.status == "failed_safe"


def _add_job(session_factory, *, chat_id, message_id, posted_at=NOW):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=f"message {message_id}",
            posted_at=posted_at,
        )
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=chat_id,
                status="pending",
                shadow=False,
                enqueued_at=NOW,
            )
        )
        session.commit()
        return raw.id


async def _wait_until(predicate, *, turns: int = 500):
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _claimed_job_count(session_factory) -> int:
    with session_factory() as session:
        return (
            session.query(MessageProcessingJob)
            .filter(MessageProcessingJob.status == "claimed")
            .count()
        )


def test_worker_activity_snapshot_tracks_three_active_chat_lanes():
    activity = worker_module.MessageProcessingActivity()

    activity.apply_limit(3, applied_at=NOW)
    for chat_id in (1, 2, 3):
        activity.enter(chat_id)
    activity.note_refill(3)

    snapshot = activity.snapshot()
    assert snapshot == {
        "configured_max_parallel_chats": 3,
        "active_chat_lanes": 3,
        "peak_active_chat_lanes_since_limit_change": 3,
        "last_refill_claimed": 3,
        "total_started": 3,
        "limit_applied_at": NOW.isoformat(),
    }
    assert "chat_ids" not in snapshot

    for chat_id in (1, 2, 3):
        activity.leave(chat_id)
    assert activity.snapshot()["active_chat_lanes"] == 0


def test_worker_loop_never_exceeds_three_active_chat_lanes(tmp_path):
    session_factory = create_session_factory(tmp_path / "three-lanes.db")
    raw_to_chat = {
        _add_job(session_factory, chat_id=chat_id, message_id=1): chat_id
        for chat_id in range(1, 7)
    }
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    active = 0
    peak = 0
    three_started = asyncio.Event()
    release_all = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        nonlocal active, peak
        assert raw_message_id in raw_to_chat
        active += 1
        peak = max(peak, active)
        if active >= 3:
            three_started.set()
        try:
            await release_all.wait()
        finally:
            active -= 1

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(three_started.wait(), timeout=5.0)
            await _wait_until(lambda: _claimed_job_count(session_factory) >= 3)
            assert _claimed_job_count(session_factory) == 3
            assert peak == 3
        finally:
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            release_all.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())

    assert peak == 3


def test_slow_lane_does_not_prevent_two_free_slots_from_refilling(tmp_path):
    session_factory = create_session_factory(tmp_path / "slow-lane-refill.db")
    raw_to_chat = {
        _add_job(session_factory, chat_id=chat_id, message_id=1): chat_id
        for chat_id in range(1, 6)
    }
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    started: list[int] = []
    active = 0
    peak = 0
    slow_started = asyncio.Event()
    initial_three_started = asyncio.Event()
    later_started = asyncio.Event()
    release_slow = asyncio.Event()
    release_initial_fast = asyncio.Event()
    release_later = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        nonlocal active, peak
        chat_id = raw_to_chat[raw_message_id]
        started.append(chat_id)
        active += 1
        peak = max(peak, active)
        if chat_id == 1:
            slow_started.set()
        if {1, 2, 3}.issubset(started):
            initial_three_started.set()
        if 4 in started and 5 in started:
            later_started.set()
        try:
            if chat_id == 1:
                await release_slow.wait()
            elif chat_id in {2, 3}:
                await release_initial_fast.wait()
            else:
                await release_later.wait()
        finally:
            active -= 1

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(slow_started.wait(), timeout=5.0)
            await asyncio.wait_for(initial_three_started.wait(), timeout=5.0)
            await _wait_until(lambda: _claimed_job_count(session_factory) >= 3)
            assert _claimed_job_count(session_factory) == 3
            assert peak == 3
            release_initial_fast.set()
            await asyncio.wait_for(later_started.wait(), timeout=5.0)
            assert not release_slow.is_set()
            assert peak == 3
        finally:
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            release_slow.set()
            release_initial_fast.set()
            release_later.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())

    assert set(started) == {1, 2, 3, 4, 5}
    assert peak == 3


def test_worker_loop_reloads_parallel_limit_before_each_refill(tmp_path):
    session_factory = create_session_factory(tmp_path / "reload-cap.db")
    raw_to_chat = {
        _add_job(session_factory, chat_id=chat_id, message_id=1): chat_id
        for chat_id in range(1, 4)
    }
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 1,
        },
    )
    started: list[int] = []
    first_started = asyncio.Event()
    all_started = asyncio.Event()
    release_all = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        chat_id = raw_to_chat[raw_message_id]
        started.append(chat_id)
        if chat_id == 1:
            first_started.set()
        if len(started) == 3:
            all_started.set()
        await release_all.wait()

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(first_started.wait(), timeout=5.0)
            await _wait_until(lambda: _claimed_job_count(session_factory) >= 1)
            assert _claimed_job_count(session_factory) == 1
            assert started == [1]
            save_trading_settings(
                session_factory,
                {"message_processing_max_parallel_chats": 3},
            )
            await asyncio.wait_for(all_started.wait(), timeout=5.0)
        finally:
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            release_all.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())

    assert started == [1, 2, 3]


def test_lowered_limit_stops_new_claims_without_cancelling_inflight(tmp_path):
    session_factory = create_session_factory(tmp_path / "lower-cap.db")
    raw_to_chat = {
        _add_job(session_factory, chat_id=chat_id, message_id=1): chat_id
        for chat_id in range(1, 6)
    }
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    started: list[int] = []
    release = {chat_id: asyncio.Event() for chat_id in range(1, 6)}
    first_three_started = asyncio.Event()
    next_started = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        chat_id = raw_to_chat[raw_message_id]
        started.append(chat_id)
        if len(started) == 3:
            first_three_started.set()
        if chat_id in {4, 5}:
            next_started.set()
        await release[chat_id].wait()

    def first_three_settled() -> bool:
        with session_factory() as session:
            statuses = {
                int(job.chat_id): job.status
                for job in session.query(MessageProcessingJob).all()
            }
        return statuses.get(2) == "succeeded" and statuses.get(3) == "succeeded"

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(first_three_started.wait(), timeout=5.0)
            await _wait_until(lambda: _claimed_job_count(session_factory) >= 3)
            assert _claimed_job_count(session_factory) == 3
            assert set(started) == {1, 2, 3}
            save_trading_settings(
                session_factory,
                {"message_processing_max_parallel_chats": 1},
            )
            release[2].set()
            release[3].set()
            await _wait_until(first_three_settled)
            await asyncio.sleep(0)
            assert not next_started.is_set()

            release[1].set()
            await asyncio.wait_for(next_started.wait(), timeout=5.0)
            assert len([chat for chat in started if chat in {4, 5}]) == 1
        finally:
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            for gate in release.values():
                gate.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())


def test_lowered_limit_prevents_unclaimed_old_slots_from_exceeding_new_cap(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "lower-unclaimed-cap.db")
    for chat_id in (1, 2):
        _add_job(session_factory, chat_id=chat_id, message_id=1)
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    activity = worker_module.MessageProcessingActivity()
    lowered_cap_applied = asyncio.Event()
    real_apply_limit = activity.apply_limit

    def track_applied_limit(limit, *, applied_at):
        real_apply_limit(limit, applied_at=applied_at)
        if limit == 1:
            lowered_cap_applied.set()

    monkeypatch.setattr(activity, "apply_limit", track_applied_limit)
    call_lock = threading.Lock()
    old_slots_ready = threading.Event()
    release_first_claim = threading.Event()
    release_pending_claims = threading.Event()
    claim_calls = 0
    centralized_claim_seen = False
    real_claim = worker_module.claim_message_processing_jobs

    def gated_claim(*args, limit, **kwargs):
        nonlocal claim_calls, centralized_claim_seen
        with call_lock:
            claim_calls += 1
            call_number = claim_calls
            if limit > 1:
                centralized_claim_seen = True
                old_slots_ready.set()
            elif not centralized_claim_seen and claim_calls >= 3:
                old_slots_ready.set()
            uses_centralized_claim = centralized_claim_seen

        if limit > 1:
            assert release_first_claim.wait(timeout=3.0)
            return []
        if not uses_centralized_claim:
            if call_number == 1:
                assert release_first_claim.wait(timeout=3.0)
                return []
            assert release_pending_claims.wait(timeout=3.0)
        return real_claim(*args, limit=limit, **kwargs)

    monkeypatch.setattr(
        worker_module,
        "claim_message_processing_jobs",
        gated_claim,
    )
    active = 0
    peak = 0
    first_started = asyncio.Event()
    two_started = asyncio.Event()
    release_processing = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        first_started.set()
        if active >= 2:
            two_started.set()
        try:
            await release_processing.wait()
        finally:
            active -= 1

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
                activity=activity,
            )
        )
        try:
            await _wait_until(old_slots_ready.is_set)
            save_trading_settings(
                session_factory,
                {"message_processing_max_parallel_chats": 1},
            )
            release_first_claim.set()
            await asyncio.wait_for(lowered_cap_applied.wait(), timeout=5.0)
            release_pending_claims.set()
            await asyncio.wait_for(first_started.wait(), timeout=5.0)
            try:
                await asyncio.wait_for(two_started.wait(), timeout=0.25)
            except TimeoutError:
                pass

            snapshot = activity.snapshot()
            assert snapshot["configured_max_parallel_chats"] == 1
            assert snapshot["active_chat_lanes"] <= 1
            assert peak <= 1
        finally:
            save_trading_settings(
                session_factory,
                {"message_pipeline_mode": "inline"},
            )
            release_first_claim.set()
            release_pending_claims.set()
            release_processing.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())


def test_live_claim_blocks_later_same_chat_job_while_other_chats_progress(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-claim-order.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    second = _add_job(session_factory, chat_id=1, message_id=2)
    other = _add_job(session_factory, chat_id=2, message_id=1)
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    started: list[int] = []
    first_started = asyncio.Event()
    other_finished = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        started.append(raw_message_id)
        if raw_message_id == first:
            first_started.set()
            await release_first.wait()
        elif raw_message_id == other:
            other_finished.set()
        elif raw_message_id == second:
            second_started.set()

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(first_started.wait(), timeout=5.0)
            await asyncio.wait_for(other_finished.wait(), timeout=5.0)
            await asyncio.sleep(0)
            assert not second_started.is_set()
            release_first.set()
            await asyncio.wait_for(second_started.wait(), timeout=5.0)
        finally:
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            release_first.set()
            await asyncio.wait_for(loop_task, timeout=5.0)

    asyncio.run(scenario())

    assert started == [first, other, second]


def test_retry_not_due_blocks_later_same_chat_job_while_other_chats_progress(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "retry-lane-order.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    second = _add_job(session_factory, chat_id=1, message_id=2)
    other = _add_job(session_factory, chat_id=2, message_id=1)
    with session_factory() as session:
        first_job = (
            session.query(MessageProcessingJob)
            .filter(MessageProcessingJob.raw_message_id == first)
            .one()
        )
        first_job.next_attempt_at = (NOW + timedelta(minutes=1)).replace(
            tzinfo=None
        )
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 3,
        },
    )
    processed: list[int] = []
    other_finished = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        processed.append(raw_message_id)
        if raw_message_id == other:
            other_finished.set()

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        try:
            await asyncio.wait_for(other_finished.wait(), timeout=5.0)
            save_trading_settings(
                session_factory, {"message_pipeline_mode": "inline"}
            )
            await asyncio.wait_for(loop_task, timeout=5.0)
        finally:
            if not loop_task.done():
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)

    asyncio.run(scenario())

    assert processed == [other]
    assert first not in processed
    assert second not in processed


def test_cancelled_loop_leaves_claim_for_stale_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "cancelled-loop.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 1,
        },
    )
    processor_started = asyncio.Event()
    processor_cancelled = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        assert raw_message_id == raw_id
        processor_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            processor_cancelled.set()

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        await asyncio.wait_for(processor_started.wait(), timeout=5.0)
        with session_factory() as session:
            claimed = session.query(MessageProcessingJob).one()
            original_token = claimed.claim_token
            assert claimed.status == "claimed"
            assert original_token
            assert claimed.attempt_count == 0

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        assert processor_cancelled.is_set()

        with session_factory() as session:
            claimed = session.query(MessageProcessingJob).one()
            assert claimed.status == "claimed"
            assert claimed.claim_token == original_token
            assert claimed.attempt_count == 0

    asyncio.run(scenario())


def test_cancelled_first_job_keeps_second_same_chat_blocked(tmp_path):
    session_factory = create_session_factory(tmp_path / "cancelled-same-chat.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    second = _add_job(session_factory, chat_id=1, message_id=2)
    save_trading_settings(
        session_factory,
        {
            "message_pipeline_mode": "queue",
            "message_processing_max_parallel_chats": 1,
        },
    )
    first_started = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        assert raw_message_id == first
        first_started.set()
        await asyncio.Event().wait()

    async def scenario():
        loop_task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                now=NOW,
                job_processor=processor,
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=5.0)
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

    asyncio.run(scenario())

    assert claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW + timedelta(seconds=1),
        limit=1,
    ) == []
    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id.asc())
            .all()
        )
    assert [job.raw_message_id for job in jobs] == [first, second]
    assert [job.status for job in jobs] == ["claimed", "pending"]
    assert jobs[0].claim_token
    assert jobs[1].claim_token is None
    assert [job.attempt_count for job in jobs] == [0, 0]


def test_stale_recovery_processes_first_once_then_releases_second(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-recovery-order.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    second = _add_job(session_factory, chat_id=1, message_id=2)
    initial_claim = claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW,
        stale_after=timedelta(minutes=5),
        limit=1,
    )
    assert [claim.raw_message_id for claim in initial_claim] == [first]
    original_token = initial_claim[0].claim_token
    processed: list[int] = []

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        processed.append(raw_message_id)

    reclaimed = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(minutes=6),
            stale_after=timedelta(minutes=5),
            limit=1,
            job_processor=processor,
        )
    )
    released = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(minutes=6, seconds=1),
            stale_after=timedelta(minutes=5),
            limit=1,
            job_processor=processor,
        )
    )

    assert reclaimed.claimed == 1
    assert reclaimed.succeeded == 1
    assert released.claimed == 1
    assert released.succeeded == 1
    assert processed == [first, second]
    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id.asc())
            .all()
        )
    assert [job.status for job in jobs] == ["succeeded", "succeeded"]
    assert [job.claim_token for job in jobs] == [None, None]
    assert [job.attempt_count for job in jobs] == [0, 0]
    assert original_token not in {job.claim_token for job in jobs}


def test_second_worker_cannot_duplicate_a_live_claim(tmp_path):
    session_factory = create_session_factory(tmp_path / "second-worker.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    first_invocations: list[int] = []
    second_invocations: list[int] = []

    async def first_processor(_session_factory, *, raw_message_id, **_kwargs):
        first_invocations.append(raw_message_id)
        first_started.set()
        await release_first.wait()

    async def second_processor(_session_factory, *, raw_message_id, **_kwargs):
        second_invocations.append(raw_message_id)

    async def scenario():
        first_task = asyncio.create_task(
            run_message_processing_worker_tick(
                session_factory,
                now=NOW,
                limit=1,
                job_processor=first_processor,
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=5.0)
        second_result = await run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=1),
            limit=1,
            job_processor=second_processor,
        )
        assert second_result.claimed == 0
        release_first.set()
        first_result = await asyncio.wait_for(first_task, timeout=5.0)
        assert first_result.succeeded == 1

    asyncio.run(scenario())

    assert first_invocations == [raw_id]
    assert second_invocations == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.attempt_count == 0
    assert job.claim_token is None


def test_claim_is_atomic_and_only_claims_one_ordered_job_per_chat(tmp_path):
    session_factory = create_session_factory(tmp_path / "atomic.db")
    first = _add_job(session_factory, chat_id=1, message_id=1)
    _add_job(session_factory, chat_id=1, message_id=2)
    other_chat = _add_job(session_factory, chat_id=2, message_id=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: claim_message_processing_jobs(
                    session_factory,
                    claimed_at=NOW,
                    limit=10,
                ),
                range(2),
            )
        )

    claimed_ids = [claim.raw_message_id for result in results for claim in result]
    assert sorted(claimed_ids) == sorted([first, other_chat])
    assert len(claimed_ids) == len(set(claimed_ids))


def test_claim_candidate_fetch_is_bounded_by_limit_with_large_backlog(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "bounded-claim-fetch.db")
    oldest_same_chat = _add_job(session_factory, chat_id=1, message_id=1)
    for message_id in range(2, 102):
        _add_job(session_factory, chat_id=1, message_id=message_id)
    oldest_other_chats = [
        _add_job(session_factory, chat_id=chat_id, message_id=1)
        for chat_id in range(2, 14)
    ]

    candidate_row_counts: list[int] = []
    original_execute = session_factory.class_.execute

    class RecordingResult:
        def __init__(self, result):
            self._result = result

        def all(self):
            rows = self._result.all()
            candidate_row_counts.append(len(rows))
            return rows

    def recording_execute(session, statement, params=None, **kwargs):
        result = original_execute(session, statement, params=params, **kwargs)
        normalized_sql = " ".join(str(statement).lower().split())
        if (
            normalized_sql.startswith("with ")
            and "message_processing_jobs" in normalized_sql
            and "claim_limit" in normalized_sql
        ):
            return RecordingResult(result)
        return result

    monkeypatch.setattr(session_factory.class_, "execute", recording_execute)

    claims = claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW,
        limit=3,
    )

    assert candidate_row_counts == [3]
    assert [claim.raw_message_id for claim in claims] == [
        oldest_same_chat,
        *oldest_other_chats[:2],
    ]


def test_claim_selection_does_not_use_unbounded_message_job_query_all(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "no-unbounded-all.db")
    for chat_id in range(1, 6):
        _add_job(session_factory, chat_id=chat_id, message_id=1)

    original_all = Query.all

    def reject_message_job_all(query):
        if any(
            description.get("entity") is MessageProcessingJob
            for description in query.column_descriptions
        ):
            raise AssertionError(
                "claim selection must not load the unbounded job query"
            )
        return original_all(query)

    monkeypatch.setattr(Query, "all", reject_message_job_all)

    claims = claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW,
        limit=2,
    )

    assert len(claims) == 2


def test_stale_claim_is_reclaimed_and_completed(tmp_path):
    session_factory = create_session_factory(tmp_path / "reclaim.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    first = claim_message_processing_jobs(
        session_factory,
        claimed_at=NOW,
        stale_after=timedelta(seconds=30),
    )
    assert first[0].raw_message_id == raw_id

    processed = []

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        processed.append(raw_message_id)

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=31),
            stale_after=timedelta(seconds=30),
            job_processor=processor,
        )
    )

    assert result.succeeded == 1
    assert processed == [raw_id]
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.claim_token is None


def test_retry_backoff_is_durable_and_max_attempts_are_terminal(tmp_path):
    session_factory = create_session_factory(tmp_path / "retry.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    notifications = []

    async def failing_processor(*_args, **_kwargs):
        raise RuntimeError("transient")

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )
    assert first.retried == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        assert job.status == "pending"
        assert job.attempt_count == 1
        assert job.next_attempt_at == (NOW + timedelta(seconds=10)).replace(tzinfo=None)

    before_due = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=9),
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
        )
    )
    assert before_due.claimed == 0

    terminal = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=10),
            max_attempts=2,
            retry_base_seconds=10,
            job_processor=failing_processor,
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )
    assert terminal.failed == 1
    assert len(notifications) == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "failed"
    assert job.attempt_count == 2
    assert job.next_attempt_at is None


def test_chat_lanes_are_ordered_while_different_chats_run_concurrently(tmp_path):
    session_factory = create_session_factory(tmp_path / "lanes.db")
    chat_one_first = _add_job(session_factory, chat_id=1, message_id=1)
    chat_one_second = _add_job(session_factory, chat_id=1, message_id=2)
    chat_two = _add_job(session_factory, chat_id=2, message_id=1)
    started = []
    both_started = asyncio.Event()

    async def processor(_session_factory, *, raw_message_id, **_kwargs):
        started.append(raw_message_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            job_processor=processor,
        )
    )
    assert first.succeeded == 2
    assert set(started) == {chat_one_first, chat_two}
    assert chat_one_second not in started

    asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=1),
            job_processor=lambda *_args, **kwargs: asyncio.sleep(
                0, result=started.append(kwargs["raw_message_id"])
            ),
        )
    )
    assert started[-1] == chat_one_second


def test_expired_job_is_never_executed(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    _add_job(
        session_factory,
        chat_id=1,
        message_id=1,
        posted_at=NOW - timedelta(minutes=20),
    )
    processed = []

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            job_processor=lambda *_args, **kwargs: asyncio.sleep(
                0, result=processed.append(kwargs["raw_message_id"])
            ),
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
        )
    )

    assert result.expired == 1
    assert processed == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "expired"
    assert job.last_reason == "expired_stale_instruction"


def test_consumer_never_claims_dormant_shadow_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow-is-dormant.db")
    raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        job.shadow = True
        session.commit()

    claims = claim_message_processing_jobs(session_factory, claimed_at=NOW)

    assert claims == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.raw_message_id == raw_id
    assert job.status == "pending"


def test_returned_authoritative_failure_uses_durable_retry(tmp_path):
    session_factory = create_session_factory(tmp_path / "authoritative-failed.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    failed_result = _authoritative_failure_result("provider timeout")

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: failed_result,
            },
        )
    )

    assert result.retried == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.attempt_count == 1
    assert job.last_reason == "processing_error:AuthoritativeProcessingFailed"


def test_empty_input_authoritative_failure_settles_once_without_retry_or_notifier(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "empty-input-terminal.db")
    _add_job(session_factory, chat_id=1, message_id=1)
    failed_result = _authoritative_failure_result(
        "message has no readable text or image"
    )
    notifications = []
    downstream = []

    async def strategy_processor(**_kwargs):
        downstream.append("strategy")

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={
                "authoritative_processor": lambda _raw_id: failed_result,
                "strategy_alert_config": SimpleNamespace(),
                "strategy_alert_processor": strategy_processor,
                "context_resolution_worker": lambda: downstream.append("context"),
            },
            chat_title_provider=lambda _chat_id: "group",
            terminal_failure_notifier=lambda claim, reason: notifications.append(
                (claim.raw_message_id, reason)
            ),
        )
    )

    assert result.succeeded == 1
    assert result.retried == 0
    assert result.failed == 0
    assert notifications == []
    assert downstream == []
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "succeeded"
    assert job.attempt_count == 0
    assert job.next_attempt_at is None
    assert job.completed_at == NOW.replace(tzinfo=None)
    assert job.last_reason == "terminal_authoritative_failure:empty_input"


def test_empty_input_terminal_outcome_releases_same_chat_lane(tmp_path):
    session_factory = create_session_factory(tmp_path / "empty-input-lane.db")
    first_raw_id = _add_job(session_factory, chat_id=1, message_id=1)
    second_raw_id = _add_job(session_factory, chat_id=1, message_id=2)
    processed = []

    def processor(raw_message_id):
        processed.append(raw_message_id)
        if raw_message_id == first_raw_id:
            return _authoritative_failure_result(
                "message has no readable text or image"
            )
        return _processing_result()

    first = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW,
            process_kwargs={"authoritative_processor": processor},
        )
    )
    second = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=NOW + timedelta(seconds=1),
            process_kwargs={"authoritative_processor": processor},
        )
    )

    assert first.succeeded == 1
    assert first.retried == 0
    assert second.succeeded == 1
    assert processed == [first_raw_id, second_raw_id]
    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.id.asc())
            .all()
        )
    assert [job.status for job in jobs] == ["succeeded", "succeeded"]
    assert [job.last_reason for job in jobs] == [
        "terminal_authoritative_failure:empty_input",
        "worker_completed",
    ]
