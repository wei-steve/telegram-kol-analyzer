"""2026-09-28 chen-btc-repost design (docs/plans/2026-09-28-chen-btc-expired-
repost-and-queue-block-design.md §3.3, 3a + 3d).

陈哥's raw 19490 (msg 10791, chat -1002337721508, 2026-09-28 03:13:35 UTC)
posted "止盈止损有调整我删了重新发。" -- a cancellation of lifecycle 1327
(strategy_thread 696, BTC long), which had already expired at 09-26 01:32
with no exchange leg ever placed. 5 of its 7 context-resolution calls raised
``target_outside_candidate_set`` (the model most likely wrote the lifecycle id
1327 where a thread_id belonged); the message processing job retried five
times over ten minutes before going ``failed``, blocking the corrected entry
19491 behind it for 25 minutes.

3a: ``rejected_response_diagnostic_json`` now records which ids were rejected
and whether they match a candidate's lifecycle id, so that theory can be
checked against data instead of only guessed at.

3d: a context-resolution failure whose first pass named an exact, risk-
reducing target that is already terminal and holds nothing ends as a benign,
non-retried ``skipped`` / ``target_terminal_noop`` instead of the retried
``mimo_authoritative_failed``. A real instruction on a still-live position
(raw 17972 "止损改为2600", raw 18501 "全部仓位止盈出局") is untouched by this
and keeps failing loudly.
"""

import asyncio
import json
from datetime import UTC, datetime

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    _context_resolution_failure_terminal_noop_target,
    assess_message_authoritatively,
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
from telegram_kol_research.message_processing_worker import process_message_job
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
)
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.strategy_thread_candidates import StrategyThreadCandidate
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)

CHEN_CHAT_ID = -1002337721508
LIFECYCLE_1327 = 1327
THREAD_696 = 696


# ---------------------------------------------------------------------------
# 3a: the diagnostic records the rejected ids, at the ``context_resolution``
# level -- no need for the authoritative layer to reproduce the bug.
# ---------------------------------------------------------------------------


def test_rejected_target_id_diagnostic_records_lifecycle_id_confusion(tmp_path):
    session_factory = create_session_factory(tmp_path / "raw-19490-diagnostic.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=CHEN_CHAT_ID,
            message_id=10791,
            text="止盈止损有调整我删了重新发。",
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    def model_caller(**kwargs):
        # The model's own words on the real 19490 responses: it named the
        # lifecycle id (1327), not the allowed thread_id (696).
        return {
            "decision": "cancel_thread",
            "target_thread_ids": [LIFECYCLE_1327],
            "management_action": "cancel_pending_entry",
            "confidence": 0.95,
            "supporting_message_ids": [],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": f"策略{LIFECYCLE_1327}(thread_id {THREAD_696})",
        }

    try:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 10791}, "messages": []},
            candidates=[{"thread_id": THREAD_696, "lifecycle_id": LIFECYCLE_1327}],
            first_pass_payload={},
            exchange_state={},
            model_caller=model_caller,
        )
        raised = False
    except ContextResolutionError as exc:
        raised = True
        assert exc.code == "target_outside_candidate_set"

    assert raised is True
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "exhausted"
    diagnostic = json.loads(attempt.rejected_response_diagnostic_json)
    assert diagnostic["error_class"] == "target_outside_candidate_set"
    assert diagnostic["rejected_target_ids"] == [LIFECYCLE_1327]
    assert diagnostic["rejected_ids_matching_candidate_lifecycle"] == [LIFECYCLE_1327]


def test_rejected_target_id_diagnostic_is_empty_when_ids_match_nothing(tmp_path):
    """A rejected id that is neither a thread_id nor any candidate's lifecycle
    id is still recorded, but flagged as not matching a lifecycle -- the
    diagnostic should not manufacture a match that is not there."""

    session_factory = create_session_factory(tmp_path / "no-match-diagnostic.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=CHEN_CHAT_ID, message_id=1, text="回顾")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    def model_caller(**kwargs):
        return {
            "decision": "hold",
            "target_thread_ids": [999999],
            "management_action": None,
            "confidence": 0.9,
            "supporting_message_ids": [],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": "unrelated id",
        }

    try:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 1}, "messages": []},
            candidates=[{"thread_id": THREAD_696, "lifecycle_id": LIFECYCLE_1327}],
            first_pass_payload={},
            exchange_state={},
            model_caller=model_caller,
        )
    except ContextResolutionError:
        pass

    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    diagnostic = json.loads(attempt.rejected_response_diagnostic_json)
    assert diagnostic["rejected_target_ids"] == [999999]
    assert diagnostic["rejected_ids_matching_candidate_lifecycle"] == []


# ---------------------------------------------------------------------------
# 3d: the pure admission check. Direct unit tests of
# ``_context_resolution_failure_terminal_noop_target``, following the same
# hand-built ``StrategyThreadCandidate`` style as
# ``test_expired_repost_is_new_entry.py``'s ``_repost_candidate``.
# ---------------------------------------------------------------------------


def _cancel_mimo(
    *,
    event_type: str = "cancel_entry",
    management_action: str | None = "cancel_and_repost_after_stop_loss_take_profit_adjustment",
    target_lifecycle_id: int | None = LIFECYCLE_1327,
    message_classes: list | None = None,
) -> MimoAuthoritativeResult:
    lifecycle_event: dict = {"event_type": event_type, "confidence": 0.9}
    if management_action is not None:
        lifecycle_event["management_action"] = management_action
    if target_lifecycle_id is not None:
        lifecycle_event["target_lifecycle_id"] = target_lifecycle_id
    payload = {
        "recognition_result": "是策略",
        "reason": "止盈止损有调整我删了重新发",
        "strategy": {},
        "lifecycle_event": lifecycle_event,
    }
    if message_classes is not None:
        payload["message_classes"] = message_classes
    return MimoAuthoritativeResult(
        raw_message_id=19490,
        payload=payload,
        input_kind="text",
        model="mimo-v2.5",
        status="是策略",
    )


def _dead_flat_candidate(
    *,
    status: str = "expired",
    lifecycle_id: int = LIFECYCLE_1327,
    thread_id: int = THREAD_696,
    execution_binding_id=None,
    binding_summary=None,
    risk_state: str = "no_current_risk",
    live_verified_pos_ids: tuple = (),
    pending_entry_leg_ids: tuple = (),
    uncertain_entry_leg_ids: tuple = (),
) -> StrategyThreadCandidate:
    lifecycle_summary: dict = {"id": lifecycle_id}
    if execution_binding_id is not None:
        lifecycle_summary["execution_binding_id"] = execution_binding_id
    return StrategyThreadCandidate(
        thread_id=thread_id,
        lifecycle_id=lifecycle_id,
        root_message_id=10758,
        symbol="BTC",
        side="long",
        status=status,
        score=60,
        reasons=("same_chat", "same_symbol", "same_side"),
        lifecycle_summary=lifecycle_summary,
        binding_summary=binding_summary,
        verified_leg_summaries=(),
        risk_state=risk_state,
        live_verified_pos_ids=live_verified_pos_ids,
        pending_entry_leg_ids=pending_entry_leg_ids,
        uncertain_entry_leg_ids=uncertain_entry_leg_ids,
    )


def test_terminal_noop_target_returns_the_lifecycle_id_via_lifecycle_event():
    mimo = _cancel_mimo()
    candidate = _dead_flat_candidate()

    assert (
        _context_resolution_failure_terminal_noop_target(mimo, (candidate,))
        == LIFECYCLE_1327
    )


def test_terminal_noop_target_returns_the_lifecycle_id_via_message_classes():
    """No ``lifecycle_event.target_lifecycle_id``, but an exact management
    class target names the same lifecycle -- either source is accepted."""

    mimo = _cancel_mimo(
        target_lifecycle_id=None,
        message_classes=[
            {
                "class": "策略管理",
                "target": {
                    "lifecycle_id": LIFECYCLE_1327,
                    "resolution": "exact",
                    "symbol": "BTC",
                    "side": "long",
                },
            }
        ],
    )
    candidate = _dead_flat_candidate()

    assert (
        _context_resolution_failure_terminal_noop_target(mimo, (candidate,))
        == LIFECYCLE_1327
    )


def test_terminal_noop_target_none_when_target_still_live():
    """Negative: raw 17972-shaped case with a live target -- keeps failing."""

    mimo = _cancel_mimo()
    candidate = _dead_flat_candidate(status="entered")

    assert _context_resolution_failure_terminal_noop_target(mimo, (candidate,)) is None


def test_terminal_noop_target_none_when_unsettled_exchange_leg():
    mimo = _cancel_mimo()
    candidate = _dead_flat_candidate(
        status="expired",
        execution_binding_id=55,
        binding_summary={"id": 55},
        risk_state="current_risk",
        live_verified_pos_ids=("pos-55",),
    )

    assert _context_resolution_failure_terminal_noop_target(mimo, (candidate,)) is None


def test_terminal_noop_target_none_when_not_risk_reducing():
    """Negative: raw 17972 "止损改为2600" -- a stop move, not a cancellation."""

    mimo = _cancel_mimo(
        event_type="risk_update",
        management_action="move_stop_to_protect",
    )
    candidate = _dead_flat_candidate()

    assert _context_resolution_failure_terminal_noop_target(mimo, (candidate,)) is None


def test_terminal_noop_target_none_when_no_exact_target():
    mimo = _cancel_mimo(target_lifecycle_id=None)
    candidate = _dead_flat_candidate()

    assert _context_resolution_failure_terminal_noop_target(mimo, (candidate,)) is None


def test_terminal_noop_target_none_when_target_not_among_candidates():
    """Fail-closed: an exact target this module cannot verify is left alone."""

    mimo = _cancel_mimo()

    assert _context_resolution_failure_terminal_noop_target(mimo, ()) is None


# ---------------------------------------------------------------------------
# 3d, end to end: 19490 replayed through ``assess_message_authoritatively``,
# ``process_authoritative_message``, and the message processing job.
# ---------------------------------------------------------------------------


def _install_repost_incident(session_factory):
    """Lifecycle 1327 / thread 696, exactly as the design's §1 timeline has it:
    signal 09-25 14:07:58, expired 09-26 01:32, no exchange leg ever placed."""

    with session_factory() as session:
        root = RawMessage(
            chat_id=CHEN_CHAT_ID,
            message_id=10758,
            text="BTC，83000-83300附近，做多 止损预计：81500 止盈预计：85800-87000",
            posted_at=datetime(2026, 9, 25, 14, 7, 58, tzinfo=UTC),
        )
        current = RawMessage(
            chat_id=CHEN_CHAT_ID,
            message_id=10791,
            text="止盈止损有调整我删了重新发。",
            posted_at=datetime(2026, 9, 28, 3, 13, 35, tzinfo=UTC),
        )
        lifecycle = StrategyLifecycle(
            id=LIFECYCLE_1327,
            chat_id=CHEN_CHAT_ID,
            message_id=10758,
            symbol="BTC",
            side="long",
            lifecycle_status="expired",
            signal_at=datetime(2026, 9, 25, 14, 7, 58, tzinfo=UTC),
            entry_range_low=83000,
            entry_range_high=83300,
            stop_loss=81500,
        )
        session.add_all([root, current, lifecycle])
        session.commit()
        root_id = root.id
        current_id = current.id
        lifecycle_id = lifecycle.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=thread.id,
        raw_message_id=root_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    return current_id, lifecycle_id, thread.id


def _cancel_first_pass(lifecycle_id: int) -> MimoAuthoritativeResult:
    return MimoAuthoritativeResult(
        raw_message_id=19490,
        payload={
            "recognition_result": "是策略",
            "reason": "撤销并调整止盈止损后重新发布",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "cancel_entry",
                "target_lifecycle_id": lifecycle_id,
                "management_action": (
                    "cancel_and_repost_after_stop_loss_take_profit_adjustment"
                ),
                "confidence": 0.9,
            },
            "message_classes": [
                {
                    "class": "策略管理",
                    "target": {
                        "lifecycle_id": lifecycle_id,
                        "resolution": "exact",
                        "symbol": "BTC",
                        "side": "long",
                    },
                }
            ],
            "confidence": 0.9,
        },
        input_kind="text",
        model="mimo-v2.5",
        status="是策略",
    )


def test_19490_replay_ends_terminal_noop_not_authoritative_failed(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "raw-19490-replay.db")
    current_id, lifecycle_id, thread_id = _install_repost_incident(session_factory)
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _cancel_first_pass(lifecycle_id),
    )

    def model_caller(**kwargs):
        # The real 19490 responses: the model names the lifecycle id, which
        # is not in ``allowed_thread_ids`` ({thread_id}).
        return {
            "decision": "cancel_thread",
            "target_thread_ids": [lifecycle_id],
            "management_action": "cancel_pending_entry",
            "confidence": 0.95,
            "supporting_message_ids": [10758, 10791],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": f"策略{lifecycle_id}(thread_id {thread_id})",
        }

    def context_resolver(**kwargs):
        return resolve_contextual_strategy(**kwargs, model_caller=model_caller)

    executor_calls = []
    result = process_authoritative_message(
        session_factory,
        raw_message_id=current_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=executor_calls.append,
        context_resolver=context_resolver,
    )

    assert result.assessment.agreement_status != "authoritative_failed"
    assert result.recognition.status == "非策略"
    assert result.automation == {"status": "skipped", "reason": "target_terminal_noop"}
    assert executor_calls == []
    with session_factory() as session:
        attempt = (
            session.query(ContextResolutionAttempt)
            .filter(ContextResolutionAttempt.raw_message_id == current_id)
            .one()
        )
        decision_row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == current_id)
            .one()
        )
    diagnostic = json.loads(attempt.rejected_response_diagnostic_json)
    assert diagnostic["rejected_target_ids"] == [lifecycle_id]
    assert diagnostic["rejected_ids_matching_candidate_lifecycle"] == [lifecycle_id]
    assert decision_row.agreement_status != "authoritative_failed"
    assert decision_row.automation_reason == "target_terminal_noop"


def test_19490_replay_job_succeeds_without_retry(tmp_path, monkeypatch):
    """Before the fix this raised ``AuthoritativeProcessingFailed`` and the
    job retried five times over ten minutes, blocking 19491 behind it."""

    session_factory = create_session_factory(tmp_path / "raw-19490-job.db")
    current_id, lifecycle_id, thread_id = _install_repost_incident(session_factory)
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _cancel_first_pass(lifecycle_id),
    )

    def model_caller(**kwargs):
        return {
            "decision": "cancel_thread",
            "target_thread_ids": [lifecycle_id],
            "management_action": "cancel_pending_entry",
            "confidence": 0.95,
            "supporting_message_ids": [10758, 10791],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": f"策略{lifecycle_id}(thread_id {thread_id})",
        }

    def authoritative_processor(raw_id):
        return process_authoritative_message(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            context_resolver=lambda **kwargs: resolve_contextual_strategy(
                **kwargs, model_caller=model_caller
            ),
        )

    result = asyncio.run(
        process_message_job(
            session_factory,
            raw_message_id=current_id,
            authoritative_processor=authoritative_processor,
        )
    )

    assert result.assessment.agreement_status != "authoritative_failed"
    assert result.automation == {"status": "skipped", "reason": "target_terminal_noop"}


def test_19490_replay_negative_live_target_keeps_failing_loudly(tmp_path, monkeypatch):
    """If lifecycle 1327 were still ``entered`` (a live position), the same
    failure must stay retried, not become a silent no-op."""

    session_factory = create_session_factory(tmp_path / "raw-19490-live-target.db")
    with session_factory() as session:
        root = RawMessage(
            chat_id=CHEN_CHAT_ID,
            message_id=10758,
            text="BTC，83000-83300附近，做多",
            posted_at=datetime(2026, 9, 25, 14, 7, 58, tzinfo=UTC),
        )
        current = RawMessage(
            chat_id=CHEN_CHAT_ID,
            message_id=10791,
            text="止盈止损有调整我删了重新发。",
            posted_at=datetime(2026, 9, 28, 3, 13, 35, tzinfo=UTC),
        )
        lifecycle = StrategyLifecycle(
            id=LIFECYCLE_1327,
            chat_id=CHEN_CHAT_ID,
            message_id=10758,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 9, 25, 14, 7, 58, tzinfo=UTC),
            entry_range_low=83000,
            entry_range_high=83300,
        )
        session.add_all([root, current, lifecycle])
        session.commit()
        root_id = root.id
        current_id = current.id
        lifecycle_id = lifecycle.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=thread.id,
        raw_message_id=root_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: _cancel_first_pass(lifecycle_id),
    )

    def model_caller(**kwargs):
        return {
            "decision": "cancel_thread",
            "target_thread_ids": [lifecycle_id],
            "management_action": "cancel_pending_entry",
            "confidence": 0.95,
            "supporting_message_ids": [10758, 10791],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": f"策略{lifecycle_id}(thread_id {thread.id})",
        }

    result = process_authoritative_message(
        session_factory,
        raw_message_id=current_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: resolve_contextual_strategy(
            **kwargs, model_caller=model_caller
        ),
    )

    assert result.assessment.agreement_status == "authoritative_failed"
    assert result.automation == {"status": "skipped", "reason": "mimo_authoritative_failed"}


# ---------------------------------------------------------------------------
# Entry admission: a ``target_terminal_noop`` decision must not block a
# following entry the way a still-retryable ``mimo_authoritative_failed``
# does (design §3.3, "make sure the entry admission barrier treats
# target_terminal_noop as terminal").
# ---------------------------------------------------------------------------


def test_admission_barrier_treats_target_terminal_noop_as_terminal():
    decision = RecognitionDecision(
        raw_message_id=19490,
        input_kind="text",
        authoritative_model="mimo",
        authoritative_status="非策略",
        authoritative_payload_json="{}",
        agreement_status="agree",
        differences_json="[]",
        prompt_versions_json="{}",
        automation_status="skipped",
        automation_reason="target_terminal_noop",
    )

    # Terminal regardless of the blocking message's own job status: unlike
    # ``mimo_authoritative_failed``, this reason is never retried, so there is
    # no "job not yet failed" case where it should still be treated as
    # possibly arriving.
    assert _decision_is_terminal_no_action(decision, job_status=None) is True
    assert _decision_is_terminal_no_action(decision, job_status="failed") is True
