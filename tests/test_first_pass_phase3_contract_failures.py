"""First-pass phase 3, batch 2 (docs/plans/2026-09-29-first-pass-phase3-
implementation-plan.md section 3): contract failures are not asked twice.

* R14 (section 3.1): a fatal ``message_classes`` violation is a failed first-pass
  answer. The same model is not asked again; the chain fallback rule is
  unchanged; when every tried model violated, the message ends terminal and
  fail-closed as ``skipped / first_pass_contract_violation`` -- not queued for
  the message processing job's backoff, not blocking an adjacent entry.
* R13 (section 3.2): the contextual second pass makes exactly one request per
  fingerprint for anything but ``network_error``; the failure ends terminal
  ``skipped / context_contract_failed`` and is alerted, after 3d's dead-and-flat
  no-op check (raw 19490) has had its turn.

Sample texts are production raw messages (contact footers stripped).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.authoritative_recognition import (
    _alert_recognition_not_applied,
    process_authoritative_message,
)
from telegram_kol_research.context_resolution import (
    ContextResolutionError,
    resolve_contextual_strategy,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_assembly_admission import (
    _decision_is_terminal_no_action,
)
from telegram_kol_research.message_classification import (
    NON_FATAL_VIOLATIONS,
    fatal_violations,
    parse_message_classes,
)
from telegram_kol_research.message_processing_worker import (
    AuthoritativeProcessingFailed,
    MessageProcessingClaim,
    TerminalAuthoritativeProcessingFailed,
    process_message_job,
    run_message_processing_worker_tick,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageProcessingJob,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
)
from telegram_kol_research.recognition_experiments import (
    FIRST_PASS_CONTRACT_VIOLATION_PREFIX,
    MessageClassesContractViolation,
    _call_mimo_authoritative_over_chain,
    _validate_authoritative_payload,
    first_pass_contract_violation_codes,
    is_first_pass_contract_violation,
)
from telegram_kol_research.recognition_failure_attribution import (
    ALERTED_REASONS,
    CONTEXT_CONTRACT_FAILED,
    FIRST_PASS_CONTRACT_VIOLATION,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)

from test_terminal_target_cancel_noop import (
    _cancel_first_pass,
    _install_repost_incident,
)

CHAT_ID = -1002337721508
_PATCH = "telegram_kol_research.recognition_experiments._call_mimo_direct_model"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _chatter_payload(**overrides):
    payload = {
        "recognition_result": "非策略",
        "reason": "闲聊",
        "strategy": None,
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "闲聊", "image_quality": "none"},
        "confidence": 0.9,
        "message_classes": [{"class": "闲话", "target": None}],
    }
    payload.update(overrides)
    return payload


#: ``target_required`` -- a 仓位管理 element without a target (fatal).
_FATAL_CLASSES = [{"class": "仓位管理", "target": None}]

_EXACT_5 = {
    "resolution": "exact",
    "lifecycle_id": 5,
    "symbol": "BTC",
    "side": "long",
}
_ETH_STRATEGY = {
    "symbol": "ETH",
    "side": "long",
    "entry": "3120",
    "stop_loss": "3040",
    "take_profit": None,
    "leverage": None,
    "order_type": "limit",
}


def _two_models():
    return [
        AiModelConfig(
            id=model_id,
            label=model_id,
            base_url=f"https://{model_id}.example/v1",
            api_key="key",
            model=model_id,
            supports_text=True,
            supports_image=True,
        )
        for model_id in ("model-a", "model-b")
    ]


def _run_chain(tmp_path, chain, fake_call):
    return _call_mimo_authoritative_over_chain(
        chain,
        raw_message=RawMessage(id=1, chat_id=100, message_id=1, text="随便聊聊"),
        media_assets=[],
        prompt="prompt",
        media_root=tmp_path,
        context_text="",
        retry_delay_seconds=0,
    )


# ---------------------------------------------------------------------------
# R14 -- validation
# ---------------------------------------------------------------------------


def test_fatal_violation_raises_with_the_codes():
    payload = _chatter_payload(message_classes=_FATAL_CLASSES)

    with pytest.raises(MessageClassesContractViolation) as raised:
        _validate_authoritative_payload(payload)

    assert raised.value.codes == ("target_required",)
    assert str(raised.value) == FIRST_PASS_CONTRACT_VIOLATION_PREFIX + "target_required"
    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        # duplicate_class_target
        (
            _chatter_payload(
                message_classes=[
                    {"class": "仓位管理", "target": _EXACT_5},
                    {"class": "仓位管理", "target": _EXACT_5},
                ]
            ),
            "duplicate_class_target",
        ),
        # class_order_violation: 新策略 before a management element
        (
            _chatter_payload(
                recognition_result="是策略",
                strategy=_ETH_STRATEGY,
                message_classes=[
                    {"class": "新策略", "target": None},
                    {"class": "仓位管理", "target": _EXACT_5},
                ],
            ),
            "class_order_violation",
        ),
        # strategy_not_allowed: a strategy object, no 新策略 element (the
        # deliberately inconsistent "TP without SL is still 是策略" shape)
        (
            _chatter_payload(
                recognition_result="是策略",
                strategy=_ETH_STRATEGY,
                message_classes=[{"class": "仓位管理", "target": _EXACT_5}],
            ),
            "strategy_not_allowed",
        ),
    ],
)
def test_non_fatal_only_violations_are_accepted(payload, expected_code):
    parsed = parse_message_classes(payload)
    assert expected_code in parsed.violations  # the precondition of the case
    assert fatal_violations(parsed) == ()
    assert set(parsed.violations) <= NON_FATAL_VIOLATIONS

    _validate_authoritative_payload(payload)  # does not raise


def test_missing_message_classes_is_accepted_v8_rollback():
    payload = _chatter_payload()
    payload.pop("message_classes")

    _validate_authoritative_payload(payload)


def test_a_valid_message_classes_field_is_accepted():
    _validate_authoritative_payload(_chatter_payload())


# ---------------------------------------------------------------------------
# R14 -- chain behaviour
# ---------------------------------------------------------------------------


def test_same_model_is_asked_once_on_a_fatal_violation(tmp_path, monkeypatch):
    """Before the change the same model was called a second time."""

    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(kwargs["model_config"].id)
        return _chatter_payload(message_classes=_FATAL_CLASSES)

    monkeypatch.setattr(_PATCH, fake_call)

    payload, error, _telemetry, used, records = _run_chain(
        tmp_path, _two_models()[:1], fake_call
    )

    assert calls == ["model-a"]
    assert used is None and payload == {}
    assert error == FIRST_PASS_CONTRACT_VIOLATION_PREFIX + "target_required"
    assert len(records) == 1 and records[0].succeeded is False


def test_second_model_is_tried_and_can_answer(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(kwargs["model_config"].id)
        if kwargs["model_config"].id == "model-a":
            return _chatter_payload(message_classes=_FATAL_CLASSES)
        return _chatter_payload()

    monkeypatch.setattr(_PATCH, fake_call)

    payload, error, _telemetry, used, records = _run_chain(
        tmp_path, _two_models(), fake_call
    )

    assert calls == ["model-a", "model-b"]  # one request each, no same-model retry
    assert error is None and used is not None and used.id == "model-b"
    assert [record.succeeded for record in records] == [False, True]


def test_every_model_violating_is_a_first_pass_contract_violation(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(kwargs["model_config"].id)
        return _chatter_payload(message_classes=_FATAL_CLASSES)

    monkeypatch.setattr(_PATCH, fake_call)

    payload, error, _telemetry, used, records = _run_chain(
        tmp_path, _two_models(), fake_call
    )

    assert calls == ["model-a", "model-b"]
    assert used is None and payload == {}

    class _Result:
        error_message = error
        model_attempts = records

    assert is_first_pass_contract_violation(_Result) is True
    assert first_pass_contract_violation_codes(_Result) == ("target_required",)


def test_a_violation_followed_by_a_provider_failure_is_an_ordinary_failure(
    tmp_path, monkeypatch
):
    """Only "every tried model violated" is terminal; a healthy provider might
    still be able to read the message, so the job's own retry stays."""

    def fake_call(**kwargs):
        if kwargs["model_config"].id == "model-a":
            return _chatter_payload(message_classes=_FATAL_CLASSES)
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(_PATCH, fake_call)

    _payload, error, _telemetry, _used, records = _run_chain(
        tmp_path, _two_models(), fake_call
    )

    class _Result:
        error_message = error
        model_attempts = records

    assert error is not None
    assert is_first_pass_contract_violation(_Result) is False


def test_non_fatal_only_payload_is_accepted_without_a_retry(tmp_path, monkeypatch):
    calls: list[str] = []
    payload = _chatter_payload(
        message_classes=[
            {"class": "仓位管理", "target": _EXACT_5},
            {"class": "仓位管理", "target": _EXACT_5},
        ]
    )

    def fake_call(**kwargs):
        calls.append(kwargs["model_config"].id)
        return dict(payload)

    monkeypatch.setattr(_PATCH, fake_call)

    _result, error, _telemetry, used, _records = _run_chain(
        tmp_path, _two_models(), fake_call
    )

    assert calls == ["model-a"]
    assert error is None and used is not None and used.id == "model-a"


# ---------------------------------------------------------------------------
# R14 -- end to end: terminal, not retried, admission terminal, wakeups run
# ---------------------------------------------------------------------------


def _first_pass_env(tmp_path, monkeypatch, *, payload_for_model):
    session_factory = create_session_factory(tmp_path / "first-pass-violation.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=CHAT_ID,
            message_id=1,
            text="今晚比特涨到86左右我们多单应该就要准备平仓了",
            posted_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id
    chain = _two_models()
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments.resolve_authoritative_chain",
        lambda config: chain,
    )
    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(kwargs["model_config"].id)
        return payload_for_model(kwargs["model_config"].id)

    monkeypatch.setattr(_PATCH, fake_call)
    wakeups: list[int] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._run_entry_assembly_wakeups",
        lambda *args, **kwargs: wakeups.append(kwargs["completed_raw_message_id"]),
    )
    return session_factory, raw_id, calls, wakeups


def _process(session_factory, raw_id, tmp_path, executor_calls):
    return process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=executor_calls.append,
    )


def test_all_models_violating_ends_terminal_and_fail_closed(tmp_path, monkeypatch):
    session_factory, raw_id, calls, wakeups = _first_pass_env(
        tmp_path,
        monkeypatch,
        payload_for_model=lambda _model: _chatter_payload(
            message_classes=_FATAL_CLASSES
        ),
    )
    executor_calls: list[int] = []

    result = _process(session_factory, raw_id, tmp_path, executor_calls)

    assert calls == ["model-a", "model-b"]  # one request per model
    assert result.assessment.agreement_status == "authoritative_failed"
    assert result.assessment.terminal_failure_reason == FIRST_PASS_CONTRACT_VIOLATION
    assert result.automation == {
        "status": "skipped",
        "reason": "first_pass_contract_violation",
    }
    assert executor_calls == []  # nothing reached execution
    assert wakeups == [raw_id]  # blocked entries are woken, as for a completed run
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.automation_status == "skipped"
        assert decision.automation_reason == "first_pass_contract_violation"
        # Entry admission reads any skipped reason but the retryable one as
        # terminal -- even while its job is still nominally live.
        assert _decision_is_terminal_no_action(decision, job_status="claimed") is True
        assert _decision_is_terminal_no_action(decision, job_status=None) is True
        # The codes stay in the run / attempt audit (no schema change).
        run = session.query(MimoRecognitionRun).one()
        assert run.status == "failed"
        assert "target_required" in (run.final_error_message or "")
        attempt_messages = [
            row.error_message
            for row in session.query(MimoRecognitionAttempt)
            .order_by(MimoRecognitionAttempt.ordinal)
            .all()
        ]
        assert len(attempt_messages) == 2
        assert all("target_required" in (message or "") for message in attempt_messages)


def test_terminal_reason_is_alerted_not_lossy_silent(tmp_path):
    assert FIRST_PASS_CONTRACT_VIOLATION in ALERTED_REASONS
    assert CONTEXT_CONTRACT_FAILED in ALERTED_REASONS
    session_factory = create_session_factory(tmp_path / "alert.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=CHAT_ID, message_id=1, text="x")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    captured: list[dict] = []

    alerted = _alert_recognition_not_applied(
        session_factory,
        raw_message_id=raw_id,
        automation={"status": "skipped", "reason": "first_pass_contract_violation"},
        group_trading_mode_provider=lambda _chat: "auto_trade",
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert alerted == "first_pass_contract_violation"
    assert captured[0]["reason_code"] == "first_pass_contract_violation"


def test_the_job_does_not_retry_a_first_pass_contract_violation(
    tmp_path, monkeypatch
):
    session_factory, raw_id, _calls, _wakeups = _first_pass_env(
        tmp_path,
        monkeypatch,
        payload_for_model=lambda _model: _chatter_payload(
            message_classes=_FATAL_CLASSES
        ),
    )

    def authoritative_processor(raw_message_id):
        return _process(session_factory, raw_message_id, tmp_path, [])

    with pytest.raises(TerminalAuthoritativeProcessingFailed) as raised:
        asyncio.run(
            process_message_job(
                session_factory,
                raw_message_id=raw_id,
                authoritative_processor=authoritative_processor,
                retry_authoritative_failure=False,
            )
        )

    assert (
        raised.value.queue_reason
        == "terminal_authoritative_failure:first_pass_contract_violation"
    )


def test_the_worker_settles_the_job_instead_of_queueing_a_retry(
    tmp_path, monkeypatch
):
    session_factory, raw_id, _calls, _wakeups = _first_pass_env(
        tmp_path,
        monkeypatch,
        payload_for_model=lambda _model: _chatter_payload(
            message_classes=_FATAL_CLASSES
        ),
    )
    with session_factory() as session:
        job = MessageProcessingJob(
            raw_message_id=raw_id,
            chat_id=CHAT_ID,
            status="claimed",
            attempt_count=0,
            claim_token="tok",
            claimed_at=datetime(2026, 9, 29, 12, 0, 1),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=datetime(2026, 9, 29, 12, 0, 2, tzinfo=UTC),
            job_processor=process_message_job,
            process_kwargs={
                "authoritative_processor": lambda rid: _process(
                    session_factory, rid, tmp_path, []
                )
            },
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            _preclaimed_jobs=[
                MessageProcessingClaim(
                    job_id=job_id,
                    raw_message_id=raw_id,
                    chat_id=CHAT_ID,
                    attempt_count=0,
                    claim_token="tok",
                    source_reason="worker_claimed",
                )
            ],
        )
    )

    assert (result.succeeded, result.retried, result.failed) == (1, 0, 0)
    with session_factory() as session:
        job = session.get(MessageProcessingJob, job_id)
        assert job.status == "succeeded"
        assert job.next_attempt_at is None
        assert job.last_reason == (
            "terminal_authoritative_failure:first_pass_contract_violation"
        )


def test_an_ordinary_authoritative_failure_is_still_retried(tmp_path, monkeypatch):
    session_factory, raw_id, _calls, _wakeups = _first_pass_env(
        tmp_path,
        monkeypatch,
        payload_for_model=lambda _model: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    def authoritative_processor(raw_message_id):
        return _process(session_factory, raw_message_id, tmp_path, [])

    with pytest.raises(AuthoritativeProcessingFailed):
        asyncio.run(
            process_message_job(
                session_factory,
                raw_message_id=raw_id,
                authoritative_processor=authoritative_processor,
                retry_authoritative_failure=False,
            )
        )
    with session_factory() as session:
        reason = session.query(RecognitionDecision.automation_reason).scalar()
    assert reason == "mimo_authoritative_failed"


# ---------------------------------------------------------------------------
# R13 -- context resolution
# ---------------------------------------------------------------------------


def _outside_candidate_answer(lifecycle_id: int, decision: str = "revise_thread"):
    """The production shape: the model names a lifecycle id, not a thread id."""

    return {
        "decision": decision,
        "target_thread_ids": [lifecycle_id],
        "management_action": (
            "move_stop_to_protect" if decision != "exit_thread" else "exit_full"
        ),
        "confidence": 0.95,
        "supporting_message_ids": [],
        "opposing_message_ids": [],
        "conflict_types": [],
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": [],
        "reason": "生产形状：写了生命周期 id",
    }


def test_a_target_outside_the_candidate_set_is_asked_once_per_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "one-request.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=CHAT_ID, message_id=10, text="止损改为2600")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = 0

    def model_caller(**kwargs):
        nonlocal calls
        calls += 1
        return _outside_candidate_answer(1252)

    for _ in range(3):  # what the job's five backoff retries used to be
        with pytest.raises(ContextResolutionError) as raised:
            resolve_contextual_strategy(
                session_factory,
                raw_message_id=raw_id,
                ai_recognition_config=AiRecognitionConfig(),
                evidence={},
                context_window={"current": {"message_id": 10}, "messages": []},
                candidates=[{"thread_id": 621, "lifecycle_id": 1252}],
                first_pass_payload={},
                exchange_state={},
                model_caller=model_caller,
            )
        assert raised.value.code == "target_outside_candidate_set"

    assert calls == 1
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert (attempt.status, attempt.attempts) == ("exhausted", 1)


def test_network_error_is_still_retried(tmp_path):
    session_factory = create_session_factory(tmp_path / "network.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=CHAT_ID, message_id=10, text="止损改为2600")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    def model_caller(**kwargs):
        raise ConnectionError("provider down")

    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 10}, "messages": []},
            candidates=[{"thread_id": 621, "lifecycle_id": 1252}],
            first_pass_payload={},
            exchange_state={},
            model_caller=model_caller,
        )

    assert raised.value.code == "network_error"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "retry_pending"  # not exhausted: a retry is coming
    assert attempt.attempts == 1


def _install_live_incident(session_factory, *, text: str):
    """An ``entered`` lifecycle (lifecycle 1252, thread renumbered by the db)."""

    with session_factory() as session:
        root = RawMessage(
            chat_id=CHAT_ID,
            message_id=9392,
            text="ETH 2650-2670 做多 止损 2640",
            posted_at=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
        )
        current = RawMessage(
            chat_id=CHAT_ID,
            message_id=9400,
            text=text,
            posted_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        )
        lifecycle = StrategyLifecycle(
            id=1252,
            chat_id=CHAT_ID,
            message_id=9392,
            symbol="ETH",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
            entry_range_low=2650,
            entry_range_high=2670,
            stop_loss=2640,
        )
        session.add_all([root, current, lifecycle])
        session.commit()
        root_id, current_id = root.id, current.id
    thread = create_strategy_thread_for_lifecycle(session_factory, lifecycle_id=1252)
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=thread.id,
        raw_message_id=root_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    return current_id


def _live_first_pass(payload_lifecycle_event, *, extra_unknown_management=True):
    from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult

    classes = [
        {
            "class": "仓位管理"
            if payload_lifecycle_event["event_type"] == "exit_position"
            else "策略管理",
            "target": {
                "lifecycle_id": 1252,
                "resolution": "exact",
                "symbol": "ETH",
                "side": "long",
            },
        }
    ]
    if extra_unknown_management:
        # Phase 3 removed the wording triggers; an unknown management element
        # is what still opens the second pass on a live exact target.
        classes.append(
            {"class": "仓位管理", "target": {"resolution": "unknown", "lifecycle_id": None}}
        )
    return MimoAuthoritativeResult(
        raw_message_id=1,
        payload={
            "recognition_result": "非策略",
            "reason": "production shape",
            "strategy": {},
            "lifecycle_event": payload_lifecycle_event,
            "message_classes": classes,
            "confidence": 0.98,
        },
        input_kind="text",
        model="mimo-v2.5",
        status="非策略",
    )


_LIVE_SHAPES = [
    (
        "17972",
        "止损改为2600",
        {
            "event_type": "position_update",
            "management_action": "adjust_stop_loss",
            "stop_loss": "2600",
            "target_lifecycle_id": 1252,
            "symbol": "ETH",
            "side": "long",
            "confidence": 0.98,
        },
    ),
    (
        "18501",
        "BTC现价86000，获利1300点，全部仓位止盈出局！",
        {
            "event_type": "exit_position",
            "management_action": "take_profit_exit",
            "exit_price": "86000",
            "target_lifecycle_id": 1252,
            "symbol": "ETH",
            "side": "long",
            "confidence": 0.99,
        },
    ),
    (
        "18897",
        "剩余仓位关注突破，做好成本保护止损统一修改83300。",
        {
            "event_type": "position_update",
            "management_action": "move_stop_to_protect",
            "stop_loss": "83300",
            "target_lifecycle_id": 1252,
            "symbol": "ETH",
            "side": "long",
            "confidence": 0.99,
        },
    ),
]


@pytest.mark.parametrize(("raw_id_label", "text", "event"), _LIVE_SHAPES)
def test_live_target_contract_failure_ends_terminal_alerted_and_not_retried(
    tmp_path, monkeypatch, raw_id_label, text, event
):
    session_factory = create_session_factory(tmp_path / f"live-{raw_id_label}.db")
    current_id = _install_live_incident(session_factory, text=text)
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _live_first_pass(event),
    )
    requests = 0

    def model_caller(**kwargs):
        nonlocal requests
        requests += 1
        return _outside_candidate_answer(1252, "revise_thread")

    executor_calls: list[int] = []
    wakeups: list[int] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._run_entry_assembly_wakeups",
        lambda *args, **kwargs: wakeups.append(kwargs["completed_raw_message_id"]),
    )

    def authoritative_processor(raw_message_id):
        return process_authoritative_message(
            session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            auto_trade_executor=executor_calls.append,
            context_resolver=lambda **kwargs: resolve_contextual_strategy(
                **kwargs, model_caller=model_caller
            ),
        )

    with pytest.raises(TerminalAuthoritativeProcessingFailed) as raised:
        asyncio.run(
            process_message_job(
                session_factory,
                raw_message_id=current_id,
                authoritative_processor=authoritative_processor,
                retry_authoritative_failure=False,
            )
        )

    assert raised.value.queue_reason == (
        "terminal_authoritative_failure:context_contract_failed"
    )
    assert requests == 1  # one provider request, not two per attempt x five retries
    assert executor_calls == []
    assert wakeups == [current_id]
    with session_factory() as session:
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == current_id)
            .one()
        )
        assert (decision.automation_status, decision.automation_reason) == (
            "skipped",
            "context_contract_failed",
        )
        assert _decision_is_terminal_no_action(decision, job_status="claimed") is True

    captured: list[dict] = []
    assert (
        _alert_recognition_not_applied(
            session_factory,
            raw_message_id=current_id,
            automation={"status": "skipped", "reason": decision.automation_reason},
            group_trading_mode_provider=lambda _chat: "auto_trade",
            capture=lambda **kwargs: captured.append(kwargs),
        )
        == "context_contract_failed"
    )
    assert captured and captured[0]["reason_code"] == "context_contract_failed"


def test_19490_shape_still_ends_target_terminal_noop_with_one_request(
    tmp_path, monkeypatch
):
    """3d keeps its turn first: dead-and-flat exact target, cancel, no legs."""

    session_factory = create_session_factory(tmp_path / "19490.db")
    current_id, lifecycle_id, _thread_id = _install_repost_incident(session_factory)
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _cancel_first_pass(lifecycle_id),
    )
    requests = 0

    def model_caller(**kwargs):
        nonlocal requests
        requests += 1
        return _outside_candidate_answer(lifecycle_id, "cancel_thread")

    result = process_authoritative_message(
        session_factory,
        raw_message_id=current_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: resolve_contextual_strategy(
            **kwargs, model_caller=model_caller
        ),
    )

    assert requests == 1
    assert result.automation == {"status": "skipped", "reason": "target_terminal_noop"}
    assert result.assessment.terminal_failure_reason is None
    assert result.assessment.agreement_status != "authoritative_failed"


def test_network_error_keeps_the_retried_failure_path(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "live-network.db")
    current_id = _install_live_incident(session_factory, text="止损改为2600")
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _live_first_pass(_LIVE_SHAPES[0][2]),
    )

    def model_caller(**kwargs):
        raise ConnectionError("provider down")

    def authoritative_processor(raw_message_id):
        return process_authoritative_message(
            session_factory,
            raw_message_id=raw_message_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            context_resolver=lambda **kwargs: resolve_contextual_strategy(
                **kwargs, model_caller=model_caller
            ),
        )

    with pytest.raises(AuthoritativeProcessingFailed):
        asyncio.run(
            process_message_job(
                session_factory,
                raw_message_id=current_id,
                authoritative_processor=authoritative_processor,
                retry_authoritative_failure=False,
            )
        )
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
    assert decision.automation_reason == "mimo_authoritative_failed"
