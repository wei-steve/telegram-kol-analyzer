from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.instruction_execution_contracts import (
    load_or_create_instruction_execution_contract,
)
from telegram_kol_research.instruction_execution_management_adapter import (
    ManagementExecutionContractBlocked,
    converge_unknown_management_instruction_contracts,
    project_management_instruction_contract,
    project_linked_management_batch_contract,
    project_revision_instruction_contract,
    resolve_management_instruction_mirror,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.strategy_management_worker import (
    run_strategy_management_worker_tick,
)
from telegram_kol_research.message_instruction_items import (
    finish_message_instruction_item,
)
from telegram_kol_research.models import (
    EntryRevisionReplacement,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    PositionMutationIntent,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
)


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)


def _persist_management_chain(
    session_factory,
    *,
    candidate_action: str = "partial_take_profit",
    batch_action: str = "partial_close",
    batch_status: str = "succeeded",
    leg_statuses: tuple[str, ...] = ("succeeded",),
    create_contract: bool = True,
    strategy_instance_id: str = "deepcoin:700:70:BTC:long",
    message_offset: int = 0,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=700,
            message_id=71 + message_offset,
            text="management",
            posted_at=NOW,
        )
        lifecycle = StrategyLifecycle(
            chat_id=700,
            message_id=70 + message_offset,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        session.add_all([raw, lifecycle])
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id=strategy_instance_id,
            kol_id="fixture",
            chat_id=700,
            message_id=70 + message_offset,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="pos-1",
            status="active",
            last_exchange_status="positions_verified",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="close_signal",
            management_action=candidate_action,
            target_lifecycle_id=lifecycle.id,
            recognition_generation="fixture-generation",
            parse_source="mimo_authoritative",
        )
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        session.add_all([candidate, decision])
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            strategy_instance_id=binding.strategy_instance_id,
            idempotency_key=f"item-{raw.id}-{candidate.id}",
            status="executing",
        )
        session.add(item)
        session.flush()
        batch = StrategyManagementBatch(
            idempotency_fingerprint=(f"{raw.id:064d}")[-64:],
            raw_message_id=raw.id,
            recognition_decision_id=decision.id,
            recognition_generation="fixture-generation",
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id,
            intent=candidate_action,
            effective_action=batch_action,
            execution_mode="live",
            status=batch_status,
            target_fingerprint="b" * 64,
            target_snapshot_json="{}",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        management_legs = []
        for index, status in enumerate(leg_statuses):
            entry = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
            leg_index=index,
            purpose="entry",
            order_kind="market",
            order_id=f"entry-{message_offset}-{index}",
            pos_id=f"pos-{message_offset}-{index + 1}",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json="{}",
                status="active",
            )
            session.add(entry)
            session.flush()
            leg = StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=entry.id,
                pos_id=f"pos-{message_offset}-{index + 1}",
                leg_index=index,
                status=status,
                client_order_id=f"management-{batch.id}-{index}",
                exchange_order_id=(
                    f"close-{index}"
                    if status in {"submitted", "succeeded", "confirmed"}
                    else None
                ),
            )
            session.add(leg)
            management_legs.append(leg)
        session.commit()
        ids = item.id, batch.id, tuple(leg.id for leg in management_legs)
    contract_id = None
    if create_contract:
        contract = load_or_create_instruction_execution_contract(
            session_factory,
            message_instruction_item_id=ids[0],
            projected_at=NOW,
        )
        contract_id = contract.id
    return (*ids, contract_id)


@pytest.mark.parametrize(
    (
        "candidate_action",
        "batch_action",
        "batch_status",
        "leg_statuses",
        "expected_state",
        "expected_terminal",
        "expected_scope",
    ),
    [
        (
            "partial_take_profit",
            "partial_close",
            "succeeded",
            ("succeeded",),
            "verified",
            "verified_management",
            "full",
        ),
        (
            "cancel_entry",
            "full_exit",
            "succeeded",
            ("succeeded",),
            "verified",
            "verified_cancel",
            "full",
        ),
        (
            "full_exit",
            "full_exit",
            "succeeded",
            ("succeeded",),
            "verified",
            "verified_exit",
            "full",
        ),
        (
            "adjust_stop_loss",
            "adjust_stop_loss",
            "succeeded",
            ("succeeded",),
            "verified",
            "verified_management",
            "full",
        ),
        (
            "full_exit",
            "full_exit",
            "blocked",
            ("planned",),
            "verified",
            "verified_refusal",
            "full",
        ),
        (
            "partial_take_profit",
            "partial_close",
            "partial_failed",
            ("succeeded", "failed"),
            "verified",
            "verified_management",
            "partial",
        ),
        (
            "full_exit",
            "full_exit",
            "recovery_required",
            ("recovery_required",),
            "submit_unknown",
            None,
            None,
        ),
        (
            "full_exit",
            "full_exit",
            "reconciling",
            ("submit_unknown",),
            "submit_unknown",
            None,
            None,
        ),
    ],
)
def test_management_batch_projects_each_terminal_kind(
    tmp_path,
    candidate_action,
    batch_action,
    batch_status,
    leg_statuses,
    expected_state,
    expected_terminal,
    expected_scope,
):
    session_factory = create_session_factory(tmp_path / "management-terminal.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        candidate_action=candidate_action,
        batch_action=batch_action,
        batch_status=batch_status,
        leg_statuses=leg_statuses,
    )

    projected = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == expected_state
    assert projected.terminal_kind == expected_terminal
    assert projected.completion_scope == expected_scope


def test_management_projection_maps_components_mutations_and_events(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-artifacts.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("planned",),
    )
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = session.get(StrategyManagementLeg, leg_ids[0])
        component = StrategyManagementComponent(
            management_batch_id=batch.id,
            strategy_management_leg_id=leg.id,
            strategy_management_leg_scope=leg.id,
            component_kind="protection",
            sequence=0,
            status="operator_required",
            idempotency_key=f"component-{batch.id}-{leg.id}",
            desired_json="{}",
            evidence_json="[]",
            created_at=NOW,
            updated_at=NOW,
        )
        mutation = PositionMutationIntent(
            idempotency_key=f"management:{batch.id}:{leg.id}:close:fixture",
            venue="deepcoin",
            operation="close_position",
            strategy_instance_id=batch.strategy_instance_id,
            execution_binding_id=batch.execution_binding_id,
            execution_order_leg_id=leg.execution_order_leg_id,
            pos_id=leg.pos_id,
            authority_fingerprint="a" * 64,
            request_fingerprint="r" * 64,
            status="recovery_required",
            request_json="{}",
            reserved_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        event = ExecutionEvent(
            execution_binding_id=batch.execution_binding_id,
            strategy_instance_id=batch.strategy_instance_id,
            venue="deepcoin",
            action="strategy_management_close_submit",
            status="submit_unknown",
            source_message_id=batch.raw_message_id,
            pos_id=leg.pos_id,
            reason="submission_outcome_unknown",
            request_json=json.dumps({"managementBatchId": batch.id}),
            created_at=NOW,
        )
        session.add_all([component, mutation, event])
        session.commit()

    projected = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == "submit_unknown"
    refs = json.loads(projected.evidence_refs_json)
    assert {ref["kind"] for ref in refs} >= {
        "management_batch",
        "management_component",
        "position_mutation_intent",
        "execution_event",
    }


def test_management_projection_rejects_cross_target_batch_collision(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-collision.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        create_contract=False,
    )
    with session_factory() as session:
        candidate = (
            session.query(SignalCandidate)
            .join(
                MessageInstructionItem,
                MessageInstructionItem.signal_candidate_id == SignalCandidate.id,
            )
            .filter(MessageInstructionItem.id == item_id)
            .one()
        )
        candidate.target_lifecycle_id += 1000
        session.commit()

    with pytest.raises(
        ManagementExecutionContractBlocked,
        match="management_batch_target_mismatch",
    ):
        project_management_instruction_contract(
            session_factory,
            message_instruction_item_id=item_id,
            management_batch_id=batch_id,
            projected_at=NOW,
            mode="live",
        )


def test_management_projection_rejects_old_recognition_generation(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-generation.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        create_contract=False,
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        candidate = session.get(SignalCandidate, item.signal_candidate_id)
        candidate.recognition_generation = "stale-generation"
        session.commit()

    with pytest.raises(
        ManagementExecutionContractBlocked,
        match="management_batch_target_mismatch",
    ):
        project_management_instruction_contract(
            session_factory,
            message_instruction_item_id=item_id,
            management_batch_id=batch_id,
            projected_at=NOW,
            mode="live",
        )


def test_management_projection_ignores_stale_same_leg_mutation(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-stale-mutation.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = session.get(StrategyManagementLeg, leg_ids[0])
        session.add(
            PositionMutationIntent(
                idempotency_key=f"management:{batch.id + 100}:stale:close",
                venue="deepcoin",
                operation="close_position",
                strategy_instance_id=batch.strategy_instance_id,
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                authority_fingerprint="a" * 64,
                request_fingerprint="s" * 64,
                status="recovery_required",
                request_json="{}",
                reserved_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    projected = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == "verified"


@pytest.mark.parametrize("historical_status", ["reserved", "submit_unknown"])
def test_terminal_management_allows_superseded_historical_event(
    tmp_path,
    historical_status,
):
    session_factory = create_session_factory(tmp_path / "management-event-history.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = session.get(StrategyManagementLeg, leg_ids[0])
        session.add(
            ExecutionEvent(
                execution_binding_id=batch.execution_binding_id,
                strategy_instance_id=batch.strategy_instance_id,
                venue="deepcoin",
                action="strategy_management_protection_precancel",
                status=historical_status,
                source_message_id=batch.raw_message_id,
                pos_id=leg.pos_id,
                request_json=json.dumps({"managementBatchId": batch.id}),
                created_at=NOW,
            )
        )
        session.commit()

    projected = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == "verified"


def test_unclassified_attempted_management_fails_to_submit_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-attempted.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        batch_status="ready",
        leg_statuses=("reserved",),
    )

    projected = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == "submit_unknown"
    assert projected.attempted_exchange_write is True


def test_shadow_management_mirror_records_divergence_without_enforcement(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-shadow.db")
    item_id, _, _, _ = _persist_management_chain(
        session_factory,
        batch_status="ready",
        leg_statuses=("planned",),
    )

    mirror = resolve_management_instruction_mirror(
        session_factory,
        message_instruction_item_id=item_id,
        requested_status="succeeded",
        mode="shadow",
    )

    assert mirror.effective_status == "succeeded"
    assert mirror.evidence["divergence"] is True
    assert mirror.evidence["expected_item_status"] == "failed"


def test_live_finish_uses_verified_management_contract_truth(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-live-finish.db")
    item_id, batch_id, _, _ = _persist_management_chain(session_factory)
    project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="live",
    )

    finish_message_instruction_item(
        session_factory,
        item_id=item_id,
        status="succeeded",
        result={"status": "succeeded", "batch_id": batch_id},
        now=NOW,
        execution_contract_mode="live",
    )

    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "submitted"
        evidence = json.loads(item.result_json)["instruction_execution_contract"]
        assert evidence["terminal_kind"] == "verified_management"


@pytest.mark.parametrize(
    ("batch_status", "leg_status", "replacement_status", "expected_state"),
    [
        ("succeeded", "cancelled", "verified", "verified"),
        ("recovery_required", "cancel_submitting", "submit_reserved", "submit_unknown"),
        ("planned", "cancel_submitting", "planned", "submit_unknown"),
    ],
)
def test_entry_revision_projects_exact_durable_outcome(
    tmp_path,
    batch_status,
    leg_status,
    replacement_status,
    expected_state,
):
    session_factory = create_session_factory(tmp_path / f"revision-{batch_status}.db")
    item_id, _, management_leg_ids, _ = _persist_management_chain(session_factory)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        candidate = session.get(SignalCandidate, item.signal_candidate_id)
        candidate.event_type = "strategy_revision"
        management_leg = session.get(StrategyManagementLeg, management_leg_ids[0])
        lifecycle = session.get(StrategyLifecycle, candidate.target_lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        thread = StrategyThread(
            chat_id=binding.chat_id,
            root_message_id=binding.message_id,
            symbol=binding.symbol,
            side=binding.side,
            status="active",
            current_lifecycle_id=lifecycle.id,
        )
        session.add(thread)
        session.flush()
        lifecycle.strategy_thread_id = thread.id
        batch = StrategyRevisionBatch(
            idempotency_fingerprint=("revision-" + batch_status).ljust(64, "x"),
            raw_message_id=item.raw_message_id,
            strategy_thread_id=thread.id,
            target_lifecycle_id=lifecycle.id,
            execution_binding_id=binding.id,
            revision_kind="replacement",
            status=batch_status,
            replacement_json="{}",
            reason_code=(
                "entry_revision_outcome_unknown"
                if batch_status == "recovery_required"
                else None
            ),
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        revision_leg = StrategyRevisionLeg(
            revision_batch_id=batch.id,
            execution_order_leg_id=management_leg.execution_order_leg_id,
            action="cancel_pending",
            prior_status="active",
            status=leg_status,
            created_at=NOW,
            updated_at=NOW,
        )
        replacement = EntryRevisionReplacement(
            revision_batch_id=batch.id,
            leg_index=0,
            desired_json="{}",
            status=replacement_status,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([revision_leg, replacement])
        session.commit()
        revision_batch_id = batch.id

    projected = project_revision_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        revision_batch_id=revision_batch_id,
        projected_at=NOW,
        mode="live",
    )

    assert projected.state == expected_state


def test_linked_management_projection_obeys_future_item_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-watermark.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        create_contract=False,
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_management_after_item_id": item_id,
        },
    )

    assert project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    ) is None
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).count() == 0

    save_trading_settings(
        session_factory,
        {"instruction_execution_management_after_item_id": 0},
    )
    linked = project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    )
    assert linked is not None
    assert linked.contract.state == "verified"


@pytest.mark.parametrize(
    ("scenario", "candidate_action", "batch_action"),
    [
        ("chen", "cancel_entry", "full_exit"),
        ("miya", "partial_then_break_even", "partial_then_break_even"),
        ("sanjie", "partial_take_profit", "partial_close"),
        ("feiyang", "full_exit", "full_exit"),
    ],
)
def test_captured_management_shapes_have_explainable_shadow_projection(
    tmp_path,
    scenario,
    candidate_action,
    batch_action,
):
    session_factory = create_session_factory(tmp_path / f"{scenario}-shadow.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        candidate_action=candidate_action,
        batch_action=batch_action,
        strategy_instance_id=f"deepcoin:{scenario}:BTC:long",
    )
    fake_writer_calls: list[dict] = []

    project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=item_id,
        management_batch_id=batch_id,
        projected_at=NOW,
        mode="shadow",
    )
    mirror = resolve_management_instruction_mirror(
        session_factory,
        message_instruction_item_id=item_id,
        requested_status="submitted",
        mode="shadow",
    )

    assert fake_writer_calls == []
    assert mirror.effective_status == "submitted"
    assert mirror.divergence is False


@pytest.mark.parametrize("persist_unknown_mirror", [False, True])
def test_unknown_management_item_converges_from_readback_without_writer_retry(
    tmp_path,
    persist_unknown_mirror,
):
    session_factory = create_session_factory(tmp_path / "management-converges.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("recovery_required",),
        create_contract=False,
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_management_after_item_id": 0,
        },
    )
    linked = project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    )
    assert linked.contract.state == "submit_unknown"
    if persist_unknown_mirror:
        finish_message_instruction_item(
            session_factory,
            item_id=item_id,
            status="unknown",
            result={"status": "recovery_required", "batch_id": batch_id},
            now=NOW,
            execution_contract_mode="live",
        )
    else:
        with session_factory() as session:
            item = session.get(MessageInstructionItem, item_id)
            item.updated_at = NOW - timedelta(minutes=10)
            session.commit()
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = session.get(StrategyManagementLeg, leg_ids[0])
        batch.status = "succeeded"
        batch.reason_code = "exchange_readback_verified"
        leg.status = "succeeded"
        session.commit()

    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("unknown convergence must not create a venue client")
        ),
        max_batches=1,
        allow_execution=False,
        batch_lister=lambda *_args, **_kwargs: [],
        processed_at=NOW,
    )

    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        contract = session.query(InstructionExecutionContract).one()
        assert contract.state == "verified"
        assert item.status == "submitted"


def test_retired_management_item_is_never_linked_to_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-retired.db")
    item_id, batch_id, _, _ = _persist_management_chain(
        session_factory,
        create_contract=False,
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.retired_at = NOW
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_management_after_item_id": 0,
        },
    )

    assert project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    ) is None
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).count() == 0


def test_terminal_contract_recovers_item_after_projection_finish_crash(
    tmp_path,
    monkeypatch,
):
    from telegram_kol_research import message_instruction_items

    session_factory = create_session_factory(tmp_path / "management-mirror-crash.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("recovery_required",),
        create_contract=False,
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_management_after_item_id": 0,
        },
    )
    project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    )
    finish_message_instruction_item(
        session_factory,
        item_id=item_id,
        status="unknown",
        result={"status": "recovery_required"},
        now=NOW,
        execution_contract_mode="live",
    )
    with session_factory() as session:
        session.get(StrategyManagementBatch, batch_id).status = "succeeded"
        session.get(StrategyManagementLeg, leg_ids[0]).status = "succeeded"
        session.commit()

    original_finish = message_instruction_items.finish_message_instruction_item
    monkeypatch.setattr(
        message_instruction_items,
        "finish_message_instruction_item",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    assert converge_unknown_management_instruction_contracts(
        session_factory,
        converged_at=NOW,
        limit=1,
    ) == 0
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).one().state == "verified"
        assert session.get(MessageInstructionItem, item_id).status == "unknown"

    monkeypatch.setattr(
        message_instruction_items,
        "finish_message_instruction_item",
        original_finish,
    )
    assert converge_unknown_management_instruction_contracts(
        session_factory,
        converged_at=NOW,
        limit=1,
    ) == 1
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "submitted"


def test_shadow_unknown_convergence_never_rewrites_item_fields(tmp_path):
    session_factory = create_session_factory(tmp_path / "management-shadow-unknown.db")
    item_id, batch_id, leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("recovery_required",),
        create_contract=False,
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "shadow",
            "instruction_execution_management_after_item_id": 0,
        },
    )
    project_linked_management_batch_contract(
        session_factory,
        management_batch_id=batch_id,
        projected_at=NOW,
    )
    finish_message_instruction_item(
        session_factory,
        item_id=item_id,
        status="unknown",
        result={"status": "recovery_required", "original": True},
        now=NOW,
        execution_contract_mode="shadow",
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        before = (item.status, item.result_json, item.error_json, item.updated_at)
        session.get(StrategyManagementBatch, batch_id).status = "succeeded"
        session.get(StrategyManagementLeg, leg_ids[0]).status = "succeeded"
        session.commit()

    assert converge_unknown_management_instruction_contracts(
        session_factory,
        converged_at=NOW,
        limit=1,
    ) == 0
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert (item.status, item.result_json, item.error_json, item.updated_at) == before
        assert session.query(InstructionExecutionContract).one().state == "verified"


def test_unknown_scan_skips_bad_old_item_and_converges_next_with_limit_one(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "management-scan-rotation.db")
    first_item, first_batch, first_leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("recovery_required",),
        create_contract=False,
        strategy_instance_id="deepcoin:first:BTC:long",
    )
    second_item, second_batch, second_leg_ids, _ = _persist_management_chain(
        session_factory,
        batch_status="recovery_required",
        leg_statuses=("recovery_required",),
        create_contract=False,
        strategy_instance_id="deepcoin:second:BTC:long",
        message_offset=100,
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_management_after_item_id": 0,
        },
    )
    for item_id, batch_id in (
        (first_item, first_batch),
        (second_item, second_batch),
    ):
        project_linked_management_batch_contract(
            session_factory,
            management_batch_id=batch_id,
            projected_at=NOW,
        )
        finish_message_instruction_item(
            session_factory,
            item_id=item_id,
            status="unknown",
            result={"status": "recovery_required"},
            now=NOW,
            execution_contract_mode="live",
        )
    with session_factory() as session:
        session.get(StrategyManagementBatch, first_batch).status = "succeeded"
        session.get(StrategyManagementLeg, first_leg_ids[0]).status = "planned"
        session.get(StrategyManagementBatch, second_batch).status = "succeeded"
        session.get(StrategyManagementLeg, second_leg_ids[0]).status = "succeeded"
        session.commit()

    assert converge_unknown_management_instruction_contracts(
        session_factory,
        converged_at=NOW,
        limit=1,
    ) == 1
    with session_factory() as session:
        assert session.get(MessageInstructionItem, first_item).status == "unknown"
        assert session.get(MessageInstructionItem, second_item).status == "submitted"
