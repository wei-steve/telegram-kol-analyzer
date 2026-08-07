import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    AuthoritativeAssessment,
    _resolved_mimo_result,
    _authorizes_exact_context_risk_reduction_from_db,
    apply_authoritative_assessment,
    assess_message_authoritatively,
    process_authoritative_message,
    requires_context_resolution,
)
from telegram_kol_research.context_resolution import (
    ContextResolutionDecision,
    resolve_contextual_strategy,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageInstructionItem,
    MessageEvidenceVersion,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyMessageLink,
    StrategyThread,
    PositionMutationIntent,
)
from telegram_kol_research.message_evidence import save_message_evidence_version
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    claim_authoritative_execution,
    finalize_authoritative_automation_outcome,
    save_pending_authoritative_decision,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)
from telegram_kol_research.strategy_thread_candidates import StrategyThreadCandidate


def _context_candidate(
    *,
    thread_id: int,
    lifecycle_id: int,
    root_message_id: int,
    risk_state: str,
) -> StrategyThreadCandidate:
    return StrategyThreadCandidate(
        thread_id=thread_id,
        lifecycle_id=lifecycle_id,
        root_message_id=root_message_id,
        symbol="BTC",
        side="long",
        status="entered",
        score=320,
        reasons=("same_chat", "same_symbol", "same_side"),
        lifecycle_summary={"id": lifecycle_id},
        binding_summary={"id": lifecycle_id},
        verified_leg_summaries=(),
        risk_state=risk_state,
        live_verified_pos_ids=(f"pos-{lifecycle_id}",) if risk_state == "current_risk" else (),
        pending_entry_leg_ids=(),
        uncertain_entry_leg_ids=(lifecycle_id,) if risk_state == "uncertain_risk" else (),
    )


def _context_exit_decision(
    *,
    thread_ids=(63,),
    action="exit_full",
    confidence=0.62,
    supporting=(4167, 4168),
) -> ContextResolutionDecision:
    return ContextResolutionDecision(
        decision="exit_thread",
        target_thread_ids=tuple(thread_ids),
        management_action=action,
        confidence=confidence,
        supporting_message_ids=tuple(supporting),
        opposing_message_ids=(),
        conflict_types=("multiple_candidates",),
        risk_reducing_fanout_allowed=len(thread_ids) > 1,
        reanalysis_triggers=(),
        reason="63100没站稳，求稳就找机会出局",
    )


def test_resolved_multi_target_partial_profit_preserves_exact_target_metadata():
    first = _context_candidate(
        thread_id=12,
        lifecycle_id=22,
        root_message_id=3463,
        risk_state="current_risk",
    )
    second = replace(
        first,
        thread_id=13,
        lifecycle_id=23,
        root_message_id=3464,
        symbol="ETH",
        live_verified_pos_ids=("pos-23",),
    )
    mimo = MimoAuthoritativeResult(
        raw_message_id=99,
        payload={
            "recognition_result": "是策略",
            "strategy": {},
            "lifecycle_event": {"event_type": "position_update"},
        },
        input_kind="text",
        model="mimo-v2.5",
        status="是策略",
    )
    decision = ContextResolutionDecision(
        decision="manage_thread",
        target_thread_ids=(12, 13),
        management_action="partial_take_profit",
        confidence=0.96,
        supporting_message_ids=(3463, 3464, 3465),
        opposing_message_ids=(),
        conflict_types=(),
        risk_reducing_fanout_allowed=True,
        reanalysis_triggers=(),
        reason="explicit BTC and ETH partial profit",
    )

    result = _resolved_mimo_result(
        mimo,
        decision,
        (first, second),
        current_message_id=3465,
        exact_risk_reduction_authorized=True,
    )

    assert result.payload["lifecycle_event"]["targets"] == [
        {"target_lifecycle_id": 22, "symbol": "BTC", "side": "long"},
        {"target_lifecycle_id": 23, "symbol": "ETH", "side": "long"},
    ]


def test_dabiaoke_4168_projects_exact_exit_despite_ghost_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "dabiaoke-4168.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=4168,
            text="63100没站稳，求稳就找机会出局",
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:4167:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=4167,
            symbol="BTC",
            side="long",
            status="active",
            pos_id="pos-4167",
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4167,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 3, 7, 50, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        raw_id = raw.id
        lifecycle_id = lifecycle.id

    mimo = MimoAuthoritativeResult(
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {"event_type": "none", "confidence": 0.0},
            "confidence": 0.62,
        },
        input_kind="text",
        model="mimo-v2.5",
        status="非策略",
    )
    current = _context_candidate(
        thread_id=63,
        lifecycle_id=lifecycle_id,
        root_message_id=4167,
        risk_state="current_risk",
    )
    ghost = _context_candidate(
        thread_id=51,
        lifecycle_id=4139,
        root_message_id=4139,
        risk_state="no_current_risk",
    )
    decision = _context_exit_decision()
    resolved = _resolved_mimo_result(
        mimo,
        decision,
        (current, ghost),
        current_message_id=4168,
    )
    assessment = AuthoritativeAssessment(
        raw_message_id=raw_id,
        mimo=resolved,
        deepseek_payload=None,
        agreement_status="pending",
        differences=[],
        authoritative_generation="dabiaoke-4168",
        context_resolution=decision,
    )

    result = apply_authoritative_assessment(session_factory, assessment)

    assert result.status == "非策略"
    assert resolved.payload["lifecycle_event"]["confidence"] == 0.62
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
    assert candidate.target_lifecycle_id == lifecycle_id
    assert candidate.management_action == "full_exit"
    assert candidate.confidence == pytest.approx(0.62)
    assert item.instruction_kind == "management"


@pytest.mark.parametrize(
    ("decision", "competitor_risk"),
    [
        (_context_exit_decision(confidence=0.59), "no_current_risk"),
        (_context_exit_decision(thread_ids=(63, 64)), "no_current_risk"),
        (_context_exit_decision(), "current_risk"),
        (_context_exit_decision(), "uncertain_risk"),
        (_context_exit_decision(action="exit_partial"), "no_current_risk"),
        (_context_exit_decision(supporting=(4168,)), "no_current_risk"),
        (_context_exit_decision(supporting=(4167,)), "no_current_risk"),
    ],
)
def test_exact_context_exit_rejects_every_broader_low_confidence_case(
    decision,
    competitor_risk,
):
    mimo = MimoAuthoritativeResult(
        raw_message_id=900,
        payload={
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "exit_position",
                "_exact_context_risk_reduction_authorized": True,
                "confidence": 0.99,
            },
        },
        input_kind="text",
        model="mimo-v2.5",
        status="非策略",
    )
    candidates = (
        _context_candidate(
            thread_id=63,
            lifecycle_id=693,
            root_message_id=4167,
            risk_state="current_risk",
        ),
        _context_candidate(
            thread_id=64,
            lifecycle_id=694,
            root_message_id=4139,
            risk_state=competitor_risk,
        ),
    )

    resolved = _resolved_mimo_result(
        mimo,
        decision,
        candidates,
        current_message_id=4168,
    )

    assert resolved.payload["lifecycle_event"]["event_type"] == "none"
    assert "_exact_context_risk_reduction_authorized" not in resolved.payload[
        "lifecycle_event"
    ]


def test_exact_context_exit_scans_all_same_chat_risk_without_model_filters(tmp_path):
    session_factory = create_session_factory(tmp_path / "exact-risk-inventory.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=4168, text="求稳就找机会出局")
        session.add(raw)
        target_thread_id = None
        for index in range(22):
            root_message_id = 4100 + index
            symbol = "BTC" if index < 21 else "ETH"
            thread = StrategyThread(
                chat_id=88, root_message_id=root_message_id,
                symbol=symbol, side="long", status="active",
            )
            session.add(thread)
            session.flush()
            binding = None
            if index in {0, 21}:
                binding = ExecutionBinding(
                    strategy_instance_id=f"strategy-{index}", kol_id="group:88",
                    chat_id=88, message_id=root_message_id, symbol=symbol,
                    side="long", venue="deepcoin", status="active",
                )
                session.add(binding)
                session.flush()
            lifecycle = StrategyLifecycle(
                strategy_thread_id=thread.id,
                execution_binding_id=(binding.id if binding is not None else None),
                chat_id=88, message_id=root_message_id, symbol=symbol, side="long",
                lifecycle_status="entered", signal_at=datetime(2026, 8, 1, index),
            )
            session.add(lifecycle)
            session.flush()
            thread.current_lifecycle_id = lifecycle.id
            if binding is not None:
                session.add(ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=0, purpose="entry", order_kind="market",
                    order_id=f"order-{index}", pos_id=f"pos-{index}",
                    attribution_status="verified", status="active",
                ))
            if index == 0:
                target_thread_id = thread.id
        session.commit()
        raw_id = raw.id

    decision = _context_exit_decision(
        thread_ids=(target_thread_id,), supporting=(4100, 4168)
    )

    assert _authorizes_exact_context_risk_reduction_from_db(
        session_factory,
        raw_message_id=raw_id,
        decision=decision,
        current_message_id=4168,
    ) is False

    with session_factory() as session:
        competitor = session.query(ExecutionBinding).filter_by(symbol="ETH").one()
        competitor.status = "closed"
        competitor_leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=competitor.id
        ).one()
        competitor_leg.status = "manually_closed"
        session.commit()
    assert _authorizes_exact_context_risk_reduction_from_db(
        session_factory,
        raw_message_id=raw_id,
        decision=decision,
        current_message_id=4168,
    ) is True
from telegram_kol_research.source_message_deletion import record_source_message_deleted


def test_authoritative_apply_rechecks_deleted_source_before_auto_trade(
    tmp_path,
    monkeypatch,
    stub_mimo_authoritative_model,
):
    session_factory = create_session_factory(tmp_path / "deleted-authoritative.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=101,
            message_id=3428,
            text="BTC long entry 68000 stop 67500",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id
    auto_trade_calls = []
    original_apply = apply_authoritative_assessment

    def delete_source_during_authoritative_apply(factory, assessment):
        record_source_message_deleted(
            factory,
            chat_id=101,
            message_id=3428,
        )
        return original_apply(factory, assessment)

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        delete_source_during_authoritative_apply,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path / "media",
        auto_trade_executor=lambda message_id: auto_trade_calls.append(message_id),
    )

    assert result.automation == {
        "status": "blocked",
        "reason": "source_message_deleted",
    }
    assert auto_trade_calls == []
    with session_factory() as session:
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        assert decision.automation_status == "blocked"
        assert decision.automation_reason == "source_message_deleted"
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == 101)
            .one()
        )
        assert deletion_exit.target_lifecycle_id == lifecycle.id


def test_reanalysis_reuses_saved_mimo_evidence_without_model_call(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "reuse-evidence.db")
    from telegram_kol_research.trading_settings import save_trading_settings

    save_trading_settings(
        session_factory,
        {"entry_preamble_mode": "shadow"},
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=101, message_id=1462, text="更新上面的计划")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    save_message_evidence_version(
        session_factory,
        raw_message_id=raw_id,
        input_fingerprint="sha256:saved",
        model="mimo-v2.5",
        prompt_versions={"mimo": 3},
        extraction_status="completed",
        confidence=0.92,
        text_evidence={"observed_text": "更新上面的计划", "fields": {}},
        image_evidence={"images": [{"asset_id": 7, "fields": {"entry": "65100"}}]},
        normalized_evidence={
            "recognition_result": "非策略",
            "reason": "management update",
            "summary": "update",
            "confidence": 0.92,
            "strategy": {},
            "lifecycle_event": {"event_type": "none"},
            "conflicts": [],
            "entry_context": {
                "kind": "entry_preamble",
                "symbol": "BTC",
                "side": "short",
                "risk_multiplier": "0.5",
                "confidence": 0.95,
                "reason": "半仓操作",
            },
        },
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("MiMo must not run during contextual reanalysis")
        ),
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path / "media",
        reuse_current_evidence=True,
    )

    assert assessment.mimo.model == "mimo-v2.5"
    assert assessment.mimo.input_kind == "text+image"
    assert assessment.mimo.payload["evidence"]["images"][0]["asset_id"] == 7
    assert assessment.mimo.payload["entry_context"]["risk_multiplier"] == "0.5"
    with session_factory() as session:
        from telegram_kol_research.models import EntryPreamble

        preamble = session.query(EntryPreamble).one()
        assert preamble.message_id == 1462
        assert preamble.risk_multiplier == "0.5"


def test_context_resolution_triggers_are_closed_and_auditable():
    required, reasons = requires_context_resolution(
        first_pass_payload={
            "recognition_result": "是策略",
            "strategy": {"symbol": "BTC", "side": "long"},
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": None,
            },
        },
        evidence={"conflicts": [{"field": "side"}]},
        context_window={
            "current": {"text": "更新 BTC 多单，有入场的保护成本"},
            "reply_chain": [],
        },
        candidates=[
            {"thread_id": 12, "lifecycle_id": 22},
            {"thread_id": 13, "lifecycle_id": 23},
        ],
    )

    assert required is True
    assert reasons == (
        "revision_language",
        "entered_holder_language",
        "management_without_exact_target",
        "multiple_same_source_candidates",
        "text_image_conflict",
        "apparent_entry_may_be_revision",
    )


def test_unambiguous_independent_entry_does_not_require_second_resolution():
    required, reasons = requires_context_resolution(
        first_pass_payload={
            "recognition_result": "是策略",
            "strategy": {"symbol": "SOL", "side": "long"},
            "lifecycle_event": {"event_type": "none"},
        },
        evidence={"conflicts": []},
        context_window={
            "current": {"text": "SOL 新多单，市价进，止损 180，止盈 200"},
            "reply_chain": [],
        },
        candidates=[],
    )

    assert required is False
    assert reasons == ()


def test_unambiguous_entry_skips_injected_context_resolver(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "independent-entry.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1600,
            text="SOL 新多单，市价进，止损 180，止盈 200",
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id
    payload = {
        "recognition_result": "是策略",
        "strategy": {
            "symbol": "SOL",
            "side": "long",
            "entry": "市价进",
            "stop_loss": "180",
            "take_profit": "200",
        },
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=payload,
            input_kind="text",
            model="mimo-v2.5",
            status="是策略",
        ),
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("independent entry must not call the context resolver")
        ),
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.context_resolution is None
    assert assessment.context_resolution_triggers == ()
    assert result.status == "是策略"


def test_commentary_target_contract_corrects_hold_without_business_writes(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "commentary-contract.db")
    with session_factory() as session:
        root = RawMessage(chat_id=92, message_id=1600, text="ETH short plan")
        current = RawMessage(
            chat_id=92,
            message_id=1601,
            text="继续持有此前 ETH 空单，仅作复盘，没有新指令",
        )
        lifecycle = StrategyLifecycle(
            chat_id=92,
            message_id=1600,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 7, 2, 37, tzinfo=UTC),
            entry_range_low=1900,
            entry_range_high=1900,
            stop_loss=1938,
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
    first_pass = {
        "recognition_result": "非策略",
        "reason": "commentary matching a recent active ETH short",
        "strategy": {},
        "lifecycle_event": {"event_type": "none", "confidence": 0.8},
        "confidence": 0.8,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=current_id,
            payload=first_pass,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    context_prompts = []

    def model_caller(**kwargs):
        context_prompts.append(kwargs["system_prompt"])
        corrected = "上一次响应违反 target_not_allowed" in kwargs["system_prompt"]
        return {
            "decision": "hold",
            "target_thread_ids": [] if corrected else [thread.id],
            "management_action": None,
            "confidence": 0.9,
            "supporting_message_ids": [1600, 1601],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": "commentary only",
        }

    def context_resolver(**kwargs):
        return resolve_contextual_strategy(
            **kwargs,
            model_caller=model_caller,
        )

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
    assert result.assessment.context_resolution.decision == "hold"
    assert result.recognition.status == "非策略"
    assert result.automation == {"status": "skipped", "reason": "mimo_no_action"}
    assert executor_calls == []
    assert len(context_prompts) == 2
    assert "上一次响应违反 target_not_allowed" not in context_prompts[0]
    assert "上一次响应违反 target_not_allowed" in context_prompts[1]
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 0
        assert session.query(PositionMutationIntent).count() == 0
        assert session.query(ExecutionEvent).count() == 0


def test_revision_is_resolved_before_instruction_projection(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "context-revision.db")
    with session_factory() as session:
        root = RawMessage(chat_id=90, message_id=1460, text="BTC 多单")
        current = RawMessage(
            chat_id=90,
            message_id=1462,
            text="更新 BTC 多单，入场 65100-65400",
        )
        lifecycle = StrategyLifecycle(
            chat_id=90,
            message_id=1460,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
            entry_range_low=65000,
            entry_range_high=65500,
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
    mimo_payload = {
        "recognition_result": "是策略",
        "reason": "看起来是完整入场参数",
        "strategy": {
            "symbol": "BTC",
            "side": "long",
            "entry": "65100-65400",
            "stop_loss": "64500",
            "take_profit": "66000",
        },
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=current_id,
            payload=mimo_payload,
            input_kind="text",
            model="mimo-v2.5",
            status="是策略",
        ),
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs)
        return ContextResolutionDecision(
            decision="revise_thread",
            target_thread_ids=(thread.id,),
            management_action="replace_entry",
            confidence=0.94,
            supporting_message_ids=(1460, 1462),
            opposing_message_ids=(),
            conflict_types=("entry_or_revision",),
            risk_reducing_fanout_allowed=False,
            reanalysis_triggers=(),
            reason="explicit update",
        )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=current_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=resolver,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert len(calls) == 1
    assert assessment.context_resolution.decision == "revise_thread"
    assert result.status == "非策略"
    with session_factory() as session:
        item = session.query(MessageInstructionItem).one()
        candidate = session.get(SignalCandidate, item.signal_candidate_id)
        assert item.instruction_kind == "management"
        assert candidate.event_type == "strategy_revision"
        assert candidate.target_lifecycle_id == lifecycle_id
        assert candidate.management_action == "replace_entry"
        assert candidate.entry_text == "65100-65400"
        assert candidate.stop_loss_text == "64500"
        assert candidate.take_profit_text == "66000"
        link = (
            session.query(StrategyMessageLink)
            .filter(StrategyMessageLink.raw_message_id == current_id)
            .one()
        )
    assert link.strategy_thread_id == thread.id
    assert link.relation_kind == "revision"


def test_context_cancel_targets_exact_thread_before_projection(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "context-cancel.db")
    with session_factory() as session:
        root = RawMessage(chat_id=93, message_id=1700, text="BTC 限价多单")
        current = RawMessage(
            chat_id=93,
            message_id=1701,
            text="策略先取消",
            reply_to_message_id=1700,
        )
        lifecycle = StrategyLifecycle(
            chat_id=93,
            message_id=1700,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
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
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=current_id,
            payload={
                "recognition_result": "非策略",
                "strategy": {},
                "lifecycle_event": {"event_type": "none", "confidence": 0.0},
                "confidence": 0.8,
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=current_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: ContextResolutionDecision(
            decision="cancel_thread",
            target_thread_ids=(thread.id,),
            management_action="cancel_pending_entry",
            confidence=0.96,
            supporting_message_ids=(1700, 1701),
            opposing_message_ids=(),
            conflict_types=(),
            risk_reducing_fanout_allowed=False,
            reanalysis_triggers=(),
            reason="explicit reply cancellation",
        ),
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        item = session.query(MessageInstructionItem).one()
        link = (
            session.query(StrategyMessageLink)
            .filter(
                StrategyMessageLink.raw_message_id == current_id,
                StrategyMessageLink.relation_kind == "cancellation",
            )
            .one()
        )
    assert lifecycle.lifecycle_status == "exited"
    assert item.instruction_kind == "management"
    assert link.strategy_thread_id == thread.id


def test_fengge_exit_applies_mimo_while_execution_gate_is_pending(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        entry = RawMessage(chat_id=-1002409877375, message_id=8398, text="BTC short")
        exit_message = RawMessage(
            chat_id=-1002409877375,
            message_id=8401,
            text="",
            posted_at=datetime(2026, 7, 13, 4, 21, 50, tzinfo=UTC),
        )
        session.add_all([entry, exit_message])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-1002409877375,
            message_id=8398,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 1, 59, tzinfo=UTC),
            entered_at=datetime(2026, 7, 13, 1, 59, 30, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:-1002409877375",
            chat_id=-1002409877375,
            message_id=8398,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="1001124071572031",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.commit()
        raw_id = exit_message.id
        lifecycle_id = lifecycle.id

    mimo_payload = {
        "recognition_result": "非策略",
        "reason": "当前消息要求出局",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "exit_price": 62800,
            "confidence": 0.95,
            "reason": "当前图片是对已有BTC空单的仓位管理",
        },
        "input_reading": {
            "observed_text": "BTC空单，目前成本价附近，出局吧",
            "image_quality": "none",
        },
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=mimo_payload,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
            prompt_versions={
                "trading.analysis.shared": 11,
                "trading.analysis.mimo_vision": 12,
            },
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must not run in the authoritative path")
        ),
        raising=False,
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.agreement_status == "pending"
    assert assessment.deepseek_payload is None
    assert assessment.differences == []
    assert result.parse_source == "mimo_authoritative"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
        assert candidate.event_type == "close_signal"
        assert candidate.symbol == "BTC"
        assert candidate.side == "short"
        assert candidate.parse_source == "mimo_authoritative"
        assert candidate.target_lifecycle_id == lifecycle_id
        assert candidate.management_action == "full_exit"
        assert candidate.management_fraction is None
        assert candidate.recognition_generation == assessment.authoritative_generation
        assert item.instruction_kind == "management"
        assert item.signal_candidate_id == candidate.id
        decision = session.query(RecognitionDecision).one()
        assert decision.authoritative_model == "mimo-v2.5"
        assert decision.agreement_status == "pending"
        assert decision.comparison_status == "execution_pending"
        assert json.loads(decision.prompt_versions_json) == {
            "mimo": {
                "trading.analysis.mimo_vision": 12,
                "trading.analysis.shared": 11,
            },
        }
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert lifecycle.exit_signal_message_id == 8401
        assert lifecycle.management_action == "exit_requested"


def test_authoritative_assessment_persists_separate_text_and_image_evidence(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=12,
            text="ETH 做空，按图中止损",
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    payload = {
        "recognition_result": "是策略",
        "reason": "图文策略",
        "strategy": {"symbol": "ETH", "side": "short", "stop_loss": "2100"},
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "evidence": {
            "text": {
                "observed_text": "ETH 做空，按图中止损",
                "fields": {
                    "symbol": {
                        "value": "ETH",
                        "source": "text",
                        "confidence": 0.99,
                    }
                },
            },
            "images": [
                {
                    "asset_id": 91,
                    "image_type": "strategy_screenshot",
                    "fields": {
                        "stop_loss": {
                            "value": "2100",
                            "source": "image",
                            "confidence": 0.92,
                        }
                    },
                    "confidence": 0.92,
                }
            ],
            "conflicts": [],
        },
        "confidence": 0.93,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=payload,
            input_kind="text+image",
            model="mimo-v2.5",
            status="是策略",
            prompt_versions={"trading.analysis.mimo_vision": 12},
        ),
    )

    assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )

    with session_factory() as session:
        evidence = session.query(MessageEvidenceVersion).one()
    assert evidence.extraction_status == "completed"
    assert json.loads(evidence.text_evidence_json)["fields"]["symbol"]["value"] == "ETH"
    assert json.loads(evidence.image_evidence_json)["images"][0]["fields"][
        "stop_loss"
    ]["value"] == "2100"
    assert json.loads(evidence.normalized_evidence_json)["conflicts"] == []


def test_mimo_cancel_entry_for_entered_strategy_creates_full_exit_candidate(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        entry = RawMessage(chat_id=9001, message_id=101, text="BTC short limit")
        cancel = RawMessage(
            chat_id=9001,
            message_id=102,
            text="限价单先取消，等我后续信号！",
            posted_at=datetime(2026, 7, 22, 3, 0, tzinfo=UTC),
        )
        session.add_all([entry, cancel])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=9001,
            message_id=101,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 2, 0, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:9001",
            chat_id=9001,
            message_id=101,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-entered",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.commit()
        cancel_raw_message_id = cancel.id
        lifecycle_id = lifecycle.id

    payload = {
        "recognition_result": "非策略",
        "reason": "取消之前的限价空单",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "cancel_entry",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "confidence": 0.95,
            "reason": "明确取消前一条限价策略",
        },
    }
    assessment = AuthoritativeAssessment(
        raw_message_id=cancel_raw_message_id,
        mimo=MimoAuthoritativeResult(
            raw_message_id=cancel_raw_message_id,
            payload=payload,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
            prompt_versions={},
        ),
        deepseek_payload=None,
        agreement_status="pending",
        differences=[],
        authoritative_generation="generation-1",
    )

    result = apply_authoritative_assessment(session_factory, assessment)

    assert result.status == "非策略"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert candidate.event_type == "close_signal"
    assert candidate.management_action == "full_exit"
    assert candidate.target_lifecycle_id == lifecycle_id
    assert candidate.recognition_generation == "generation-1"
    assert item.signal_candidate_id == candidate.id
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_action == "exit_requested"


def test_mimo_failure_never_applies_deepseek_action(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="timeout",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must not run when MiMo fails")
        ),
        raising=False,
    )

    assessment = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    result = apply_authoritative_assessment(session_factory, assessment)

    assert assessment.agreement_status == "authoritative_failed"
    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        decision = session.query(RecognitionDecision).one()
        assert decision.agreement_status == "authoritative_failed"
        assert decision.comparison_status == "completed"


def test_mimo_failure_after_pending_rerecognition_cancels_stale_review(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=22, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    results = iter(
        [
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={"recognition_result": "是策略"},
                input_kind="text",
                model="mimo-v2.5",
                status="是策略",
                prompt_versions={"trading.analysis.shared": 11},
            ),
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={},
                input_kind="text",
                model="mimo-v2.5",
                status="识别失败",
                error_message="timeout",
                prompt_versions={"trading.analysis.shared": 12},
            ),
        ]
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: next(results),
    )

    assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )

    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.agreement_status == "authoritative_failed"
        assert decision.comparison_status == "completed"
        assert json.loads(decision.prompt_versions_json) == {
            "mimo": {"trading.analysis.shared": 12}
        }


def test_unchanged_rerecognition_preserves_completed_review_through_execution_gate(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=23, text="BTC short now")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    mimo_result = MimoAuthoritativeResult(
        raw_message_id=raw_id,
        payload={"recognition_result": "非策略"},
        input_kind="text",
        model="mimo-v2.5",
        status="非策略",
        prompt_versions={"trading.analysis.shared": 11},
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: mimo_result,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek must remain deferred on re-recognition")
        ),
        raising=False,
    )

    first = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        decision.agreement_status = "agreed"
        decision.comparison_status = "completed"
        decision.auxiliary_payload_json = '{"recognition_result":"非策略"}'
        session.commit()

    second = assess_message_authoritatively(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
    )

    assert first.semantic_review_status == "execution_pending"
    assert second.agreement_status == "agreed"
    assert second.semantic_review_status == "execution_pending"
    assert second.deepseek_payload is None
    assert claim_authoritative_execution(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=second.authoritative_generation,
    )
    finalized = finalize_authoritative_automation_outcome(
        session_factory,
        raw_message_id=raw_id,
        authoritative_generation=second.authoritative_generation,
        automation_status="skipped",
        automation_reason="test",
    )
    assert finalized.comparison_status == "completed"
    with session_factory() as session:
        assert session.query(RecognitionDecision).one().comparison_status == "completed"


def test_process_authoritative_message_persists_pending_before_mimo_and_auto_trade(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=3, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    events: list[str] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: events.append("mimo")
        or MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={
                "recognition_result": "非策略",
                "lifecycle_event": {"event_type": "exit_position"},
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
            prompt_versions={"trading.analysis.shared": 11},
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DeepSeek auxiliary was invoked synchronously")
        ),
        raising=False,
    )
    from telegram_kol_research import authoritative_recognition

    real_save_pending = authoritative_recognition.save_pending_authoritative_decision

    def save_pending(*args, **kwargs):
        events.append("persist_pending")
        return real_save_pending(*args, **kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.save_pending_authoritative_decision",
        save_pending,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: events.append("apply_mimo")
        or SimpleNamespace(status="非策略"),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.claim_authoritative_execution",
        lambda *args, **kwargs: events.append("claim_execution") or True,
        raising=False,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.finalize_authoritative_automation_outcome",
        lambda *args, **kwargs: events.append("persist_automation")
        or SimpleNamespace(
            agreement_status="pending",
            differences_json="[]",
            comparison_status="pending",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._has_current_mimo_candidate",
        lambda *args, **kwargs: True,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=lambda message_id: events.append("auto_trade")
        or {"status": "executed", "reason": "close_submitted"},
    )

    assert events == [
        "mimo",
        "persist_pending",
        "claim_execution",
        "apply_mimo",
        "auto_trade",
        "persist_automation",
    ]
    assert result.assessment.agreement_status == "pending"
    assert result.assessment.deepseek_payload is None
    assert result.automation == {"status": "executed", "reason": "close_submitted"}


def test_superseded_generation_never_applies_or_auto_trades(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=33, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    from telegram_kol_research import authoritative_recognition

    real_claim = authoritative_recognition.claim_authoritative_execution
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={"recognition_result": "非策略", "reason": "generation-a"},
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    applied: list[str] = []
    auto_trade_calls: list[int] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: applied.append("applied"),
    )

    def supersede_before_claim(*args, **kwargs):
        save_pending_authoritative_decision(
            session_factory,
            RecognitionDecisionRecord(
                raw_message_id=raw_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload={
                    "recognition_result": "非策略",
                    "reason": "generation-b",
                },
                auxiliary_model=None,
                auxiliary_status=None,
                auxiliary_payload=None,
                agreement_status="pending",
                differences=[],
                prompt_versions={"mimo": {}},
            ),
        )
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.claim_authoritative_execution",
        supersede_before_claim,
    )

    with pytest.raises(RuntimeError, match="stale|claim"):
        process_authoritative_message(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            auto_trade_executor=auto_trade_calls.append,
        )

    assert applied == []
    assert auto_trade_calls == []
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.comparison_status == "execution_pending"
        assert json.loads(decision.authoritative_payload_json)["reason"] == "generation-b"


def test_authoritative_generation_supersedes_candidate_generation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=1,
            message_id=35,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 1, 0, tzinfo=UTC),
            entered_at=datetime(2026, 7, 13, 1, 1, tzinfo=UTC),
        )
        raw = RawMessage(chat_id=1, message_id=36, text="分批止盈")
        session.add_all([lifecycle, raw])
        session.commit()
        lifecycle_id = lifecycle.id
        raw_id = raw.id

    payload = {
        "recognition_result": "非策略",
        "lifecycle_event": {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "management_action": "partial_take_profit",
            "confidence": 0.95,
            "reason": "分批止盈",
        },
    }
    from telegram_kol_research.message_recognition import apply_authoritative_mimo_payload

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="generation-a",
    )
    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="generation-b",
    )

    with session_factory() as session:
        candidates = session.query(SignalCandidate).order_by(SignalCandidate.id).all()
        active_item = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.retired_at.is_(None))
            .one()
        )
        active_candidate = session.get(
            SignalCandidate,
            active_item.signal_candidate_id,
        )
        assert [candidate.recognition_generation for candidate in candidates] == [
            "generation-a",
            "generation-b",
        ]
        assert all(
            candidate.parse_source == "mimo_authoritative"
            and candidate.target_lifecycle_id == lifecycle_id
            for candidate in candidates
        )
        assert active_candidate is not None
        assert active_candidate.recognition_generation == "generation-b"


def test_running_generation_blocks_new_process_until_it_finalizes(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=34, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    from telegram_kol_research import authoritative_recognition

    results = iter(
        [
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={"recognition_result": "非策略", "reason": "generation-a"},
                input_kind="text",
                model="mimo-v2.5",
                status="非策略",
            ),
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={"recognition_result": "非策略", "reason": "generation-b"},
                input_kind="text",
                model="mimo-v2.5",
                status="非策略",
            ),
        ]
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: next(results),
    )
    applied: list[str] = []
    auto_trade_calls: list[int] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: applied.append("applied")
        or SimpleNamespace(status="非策略"),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._has_current_mimo_candidate",
        lambda *args, **kwargs: True,
    )
    real_claim = authoritative_recognition.claim_authoritative_execution
    nested_attempted = False

    def claim_then_attempt_new_process(*args, **kwargs):
        nonlocal nested_attempted
        assert real_claim(*args, **kwargs) is True
        if not nested_attempted:
            nested_attempted = True
            with pytest.raises(RuntimeError, match="in progress|running"):
                process_authoritative_message(
                    session_factory,
                    raw_message_id=raw_id,
                    ai_recognition_config=AiRecognitionConfig(),
                    media_root=tmp_path,
                    auto_trade_executor=auto_trade_calls.append,
                )
        return True

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.claim_authoritative_execution",
        claim_then_attempt_new_process,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=lambda message_id: auto_trade_calls.append(message_id)
        or {"status": "submitted", "reason": "generation-a"},
    )

    assert applied == ["applied"]
    assert auto_trade_calls == [raw_id]
    assert result.automation["reason"] == "generation-a"
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.comparison_status == "pending"
        assert decision.automation_reason == "generation-a"


def test_running_generation_rejects_nested_mimo_failure_without_overwrite(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=35, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    from telegram_kol_research import authoritative_recognition

    results = iter(
        [
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={"recognition_result": "非策略", "reason": "generation-a"},
                input_kind="text",
                model="mimo-v2.5",
                status="非策略",
            ),
            MimoAuthoritativeResult(
                raw_message_id=raw_id,
                payload={},
                input_kind="text",
                model="mimo-v2.5",
                status="识别失败",
                error_message="nested timeout",
            ),
        ]
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: next(results),
    )
    applied: list[str] = []
    auto_trade_calls: list[int] = []
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: applied.append("applied")
        or SimpleNamespace(status="非策略"),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._has_current_mimo_candidate",
        lambda *args, **kwargs: True,
    )
    real_claim = authoritative_recognition.claim_authoritative_execution
    running_generation: str | None = None

    def claim_then_attempt_failed_process(*args, **kwargs):
        nonlocal running_generation
        running_generation = kwargs["authoritative_generation"]
        assert real_claim(*args, **kwargs) is True
        with pytest.raises(RuntimeError, match="in progress|running"):
            process_authoritative_message(
                session_factory,
                raw_message_id=raw_id,
                ai_recognition_config=AiRecognitionConfig(),
                media_root=tmp_path,
                auto_trade_executor=auto_trade_calls.append,
            )
        with session_factory() as session:
            decision = session.query(RecognitionDecision).one()
            assert decision.comparison_status == "execution_running"
            assert decision.comparison_claim_token == running_generation
            assert decision.agreement_status == "pending"
            assert decision.authoritative_status == "非策略"
            assert json.loads(decision.authoritative_payload_json)["reason"] == "generation-a"
            assert decision.automation_status is None
        return True

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.claim_authoritative_execution",
        claim_then_attempt_failed_process,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=lambda message_id: auto_trade_calls.append(message_id)
        or {"status": "submitted", "reason": "generation-a"},
    )

    assert applied == ["applied"]
    assert auto_trade_calls == [raw_id]
    assert result.automation["reason"] == "generation-a"
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.comparison_status == "pending"
        assert decision.comparison_claim_token is None
        assert decision.automation_reason == "generation-a"


def test_pending_save_failure_prevents_mimo_apply_and_auto_trade(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=31, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={"recognition_result": "非策略"},
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.save_pending_authoritative_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MiMo must not apply after pending-save failure")
        ),
    )
    auto_trade_calls: list[int] = []

    with pytest.raises(RuntimeError, match="database unavailable"):
        process_authoritative_message(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            auto_trade_executor=auto_trade_calls.append,
        )

    assert auto_trade_calls == []


def test_outcome_persist_failure_after_submit_leaves_review_unclaimable(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=32, text="现价出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={"recognition_result": "非策略"},
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: SimpleNamespace(status="非策略"),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._has_current_mimo_candidate",
        lambda *args, **kwargs: True,
    )
    submitted: list[int] = []

    def fail_finalize(*args, **kwargs):
        raise RuntimeError("outcome commit failed")

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.finalize_authoritative_automation_outcome",
        fail_finalize,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="outcome commit failed"):
        process_authoritative_message(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            auto_trade_executor=lambda message_id: submitted.append(message_id)
            or {"status": "submitted", "reason": "close_submitted"},
        )

    assert submitted == [raw_id]
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.comparison_status == "execution_running"
        assert decision.automation_status is None


def test_process_authoritative_message_skips_auto_trade_when_mimo_fails(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=4, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    assessment = AuthoritativeAssessment(
        raw_message_id=raw_id,
        mimo=MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="timeout",
        ),
        deepseek_payload=None,
        agreement_status="authoritative_failed",
        differences=[],
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.assess_message_authoritatively",
        lambda *args, **kwargs: assessment,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )
    auto_trade_calls: list[int] = []

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=auto_trade_calls.append,
    )

    assert auto_trade_calls == []
    assert result.automation == {
        "status": "skipped",
        "reason": "mimo_authoritative_failed",
    }


def test_mimo_non_strategy_never_executes_stale_deepseek_candidate(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=5, text="只是复盘，不是新策略")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                event_type="entry_signal",
                symbol="BTC",
                side="long",
                parse_source="text_ai",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = raw.id

    payload = {
        "recognition_result": "非策略",
        "reason": "MiMo判定为复盘",
        "strategy": {},
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "只是复盘，不是新策略", "image_quality": "none"},
        "confidence": 0.95,
    }
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=payload,
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.infer_deepseek_auxiliary",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale DeepSeek candidates must not be refreshed")
        ),
        raising=False,
    )
    auto_trade_calls: list[int] = []

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=auto_trade_calls.append,
    )

    assert auto_trade_calls == []
    assert result.automation == {"status": "skipped", "reason": "mimo_no_action"}
