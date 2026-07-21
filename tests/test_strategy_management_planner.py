from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


PLANNED_AT = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def _planner():
    assert importlib.util.find_spec(
        "telegram_kol_research.strategy_management_planner"
    ) is not None, "strategy management planner is missing"
    return importlib.import_module(
        "telegram_kol_research.strategy_management_planner"
    )


class _ContractSpecs:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _ReadOnlyDeepcoin:
    def __init__(self, positions, *, tpsl_orders=None):
        self.positions = list(positions)
        self.tpsl_orders = list(tpsl_orders or [])
        self.position_reads = []
        self.tpsl_reads = []
        self.write_calls = []

    def list_positions(self, *, inst_id=None):
        self.position_reads.append(inst_id)
        if inst_id is None:
            return list(self.positions)
        return [row for row in self.positions if row.get("instId") == inst_id]

    def list_trigger_orders_pending(self, *, inst_id):
        self.tpsl_reads.append(inst_id)
        return [row for row in self.tpsl_orders if row.get("instId") == inst_id]

    def place_order(self, payload):
        self.write_calls.append(("place_order", payload))
        raise AssertionError("planning must not write to Deepcoin")

    def cancel_trigger_order(self, payload):
        self.write_calls.append(("cancel_trigger_order", payload))
        raise AssertionError("planning must not write to Deepcoin")

    def set_position_sltp(self, payload):
        self.write_calls.append(("set_position_sltp", payload))
        raise AssertionError("planning must not write to Deepcoin")


def _position(pos_id="pos-b", *, size="10", avg_px="62000", side="short"):
    return {
        "instId": "BTC-USDT-SWAP",
        "posId": pos_id,
        "posSide": side,
        "pos": size,
        "avgPx": avg_px,
        "mgnMode": "cross",
        "posMode": "split",
        "cTime": "1721000000000",
    }


def _persist_exact_management_target(
    session_factory,
    *,
    intent="full_exit",
    selected_has_binding=True,
    selected_points_to_other_binding=False,
    second_active_binding=False,
    leg_status="active",
    attribution_status="verified",
    attribution_evidence=None,
    pos_ids=("pos-b",),
    management_fraction="default",
):
    with session_factory() as session:
        entry_a = RawMessage(
            chat_id=100,
            message_id=10,
            posted_at=PLANNED_AT,
            text="BTC short A",
        )
        entry_b = RawMessage(
            chat_id=100,
            message_id=20,
            posted_at=PLANNED_AT,
            text="BTC short B",
        )
        management = RawMessage(
            chat_id=100,
            message_id=30,
            posted_at=PLANNED_AT,
            text="B strategy exit",
        )
        session.add_all([entry_a, entry_b, management])
        session.flush()
        lifecycle_a = StrategyLifecycle(
            chat_id=100,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=PLANNED_AT,
        )
        lifecycle_b = StrategyLifecycle(
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=PLANNED_AT,
        )
        session.add_all([lifecycle_a, lifecycle_b])
        session.flush()
        binding_a = ExecutionBinding(
            strategy_instance_id="deepcoin:100:10:BTC:short",
            kol_id="alice",
            chat_id=100,
            message_id=10,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-a",
            status="active",
        )
        session.add(binding_a)
        session.flush()
        lifecycle_a.execution_binding_id = binding_a.id
        if selected_points_to_other_binding:
            lifecycle_b.execution_binding_id = binding_a.id

        binding_b = None
        if selected_has_binding:
            binding_b = ExecutionBinding(
                strategy_instance_id="deepcoin:100:20:BTC:short",
                kol_id="alice",
                chat_id=100,
                message_id=20,
                symbol="BTC",
                side="short",
                venue="deepcoin",
                pos_id=pos_ids[0],
                status="active",
            )
            session.add(binding_b)
            session.flush()
            lifecycle_b.execution_binding_id = binding_b.id
            for index, pos_id in enumerate(pos_ids):
                session.add(
                    ExecutionOrderLeg(
                        execution_binding_id=binding_b.id,
                        strategy_instance_id="deepcoin:100:20:BTC:short",
                        leg_index=index,
                        purpose="entry",
                        order_kind="market",
                        order_id=pos_id,
                        pos_id=pos_id,
                        venue="deepcoin",
                        attribution_status=attribution_status,
                        attribution_evidence_json=json.dumps(
                            attribution_evidence
                            or {"policy_version": 2, "source": "direct_order_identity"}
                        ),
                        status=leg_status,
                    )
                )
            if second_active_binding:
                session.add(
                    ExecutionBinding(
                        strategy_instance_id="deepcoin:100:20:BTC:short",
                        kol_id="alice",
                        chat_id=100,
                        message_id=21,
                        symbol="BTC",
                        side="short",
                        venue="deepcoin",
                        status="active",
                    )
                )

        session.add(
            RecognitionDecision(
                raw_message_id=management.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=management.id,
                symbol="BTC",
                side="short",
                event_type="close_signal" if intent == "full_exit" else "position_update",
                target_lifecycle_id=lifecycle_b.id,
                management_action=intent,
                management_fraction=(
                    0.3
                    if intent in {"partial_take_profit", "partial_then_break_even"}
                    and management_fraction == "default"
                    else management_fraction
                    if intent in {"partial_take_profit", "partial_then_break_even"}
                    else None
                ),
                recognition_generation="generation-b",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        return management.id, lifecycle_b.id, binding_b.id if binding_b else None


def _persist_partial_filled_range_management_target(session_factory, *, both_pending=False):
    with session_factory() as session:
        entry = RawMessage(
            chat_id=200,
            message_id=504,
            posted_at=PLANNED_AT,
            text="BTC long 64100-63800",
        )
        management = RawMessage(
            chat_id=200,
            message_id=506,
            posted_at=PLANNED_AT,
            text="BTC long take profit half and move stop",
        )
        session.add_all([entry, management])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=200,
            message_id=504,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=PLANNED_AT,
            entered_at=PLANNED_AT,
            entry_range_low=63800,
            entry_range_high=64100,
            stop_loss=62800,
            take_profit="65600",
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:200:504:BTC:long",
            kol_id="miya",
            chat_id=200,
            message_id=504,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-live,order-pending",
            pos_id=None if both_pending else "pos-live",
            status="open" if both_pending else "active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="order-live",
                    client_order_id="TKMYA504E1",
                    pos_id=None if both_pending else "pos-live",
                    venue="deepcoin",
                    attribution_status="unassigned" if both_pending else "verified",
                    attribution_evidence_json=None
                    if both_pending
                    else json.dumps(
                        {"policy_version": 2, "source": "direct_order_identity"}
                    ),
                    status="pending" if both_pending else "active",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="order-pending",
                    client_order_id="TKMYA504E2",
                    pos_id=None,
                    venue="deepcoin",
                    attribution_status="unassigned",
                    status="pending",
                ),
            ]
        )
        decision = RecognitionDecision(
            raw_message_id=management.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        session.add(decision)
        session.add(
            SignalCandidate(
                raw_message_id=management.id,
                symbol="BTC",
                side="long",
                event_type="position_update",
                target_lifecycle_id=lifecycle.id,
                management_action="partial_then_break_even",
                management_fraction=0.5,
                recognition_generation="generation-miya",
                parse_source="mimo_authoritative",
                confidence=0.9,
            )
        )
        session.commit()
        return management.id, lifecycle.id, binding.id


def _disable_reconciliation(monkeypatch, planner):
    calls = []
    monkeypatch.setattr(
        planner,
        "reconcile_deepcoin_execution_bindings",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def test_selected_lifecycle_cannot_borrow_same_symbol_binding(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, _ = _persist_exact_management_target(
        session_factory, selected_has_binding=False
    )
    calls = _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position("pos-a")]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_strategy_binding_not_found"
    assert result.batch is None
    assert result.target_lifecycle_id == lifecycle_id
    assert len(calls) == 1


def test_cross_chat_management_raw_cannot_target_lifecycle(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    with session_factory() as session:
        session.get(RawMessage, raw_id).chat_id = 999
        session.commit()
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin([_position()])

    result = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id, deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_source_identity_mismatch"
    assert client.write_calls == []


def test_cross_chat_lifecycle_binding_is_blocked(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(session_factory)
    with session_factory() as session:
        session.get(ExecutionBinding, binding_id).chat_id = 999
        session.commit()
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin([_position()])

    result = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id, deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert client.write_calls == []


def test_management_candidate_strategy_fields_must_match_target(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    with session_factory() as session:
        session.query(SignalCandidate).filter_by(raw_message_id=raw_id).one().symbol = "ETH"
        session.commit()
    _disable_reconciliation(monkeypatch, planner)
    result = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT,
    )
    assert result.status == "blocked"
    assert result.reason_code == "target_source_identity_mismatch"


def test_selected_lifecycle_rejects_stale_pointer_to_other_lifecycle_binding(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, _ = _persist_exact_management_target(
        session_factory,
        selected_has_binding=False,
        selected_points_to_other_binding=True,
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position("pos-a")]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_strategy_binding_not_found"
    assert result.target_lifecycle_id == lifecycle_id


def test_exact_identity_chain_creates_all_verified_targets(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory, pos_ids=("pos-b-1", "pos-b-2")
    )
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin([_position("pos-b-1"), _position("pos-b-2")])

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.target_lifecycle_id == lifecycle_id
    assert result.batch.execution_binding_id == binding_id
    assert result.batch.strategy_instance_id == "deepcoin:100:20:BTC:short"
    assert [leg.pos_id for leg in result.batch.legs] == ["pos-b-1", "pos-b-2"]
    assert client.position_reads == [None]


def test_real_reconciliation_and_planner_share_one_position_snapshot(tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    client = _ReadOnlyDeepcoin([_position()])

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert client.position_reads == [None]


def test_two_active_bindings_for_strategy_blocks_whole_batch(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, second_active_binding=True
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_strategy_binding_not_unique"


@pytest.mark.parametrize(
    ("attribution_status", "leg_status", "reason"),
    [
        ("unassigned", "active", "target_position_ownership_not_verified"),
        ("attribution_conflict", "active", "target_position_ownership_conflict"),
        ("evidence_unavailable", "active", "target_position_evidence_unavailable"),
        ("verified", "cancelled", "target_position_ownership_terminal"),
    ],
)
def test_unsafe_entry_leg_blocks_whole_batch(
    monkeypatch, tmp_path, attribution_status, leg_status, reason
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory,
        attribution_status=attribution_status,
        leg_status=leg_status,
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
        shadow_only=True,
    )

    assert result.status == "blocked"
    assert result.reason_code == reason
    assert result.batch.status == "blocked"
    assert result.batch.execution_mode == "shadow"
    assert result.batch.legs == ()


def test_reconciliation_happens_before_identity_rows_are_reloaded(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "stale-pos"
        leg = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).one()
        leg.pos_id = None
        leg.order_id = "conditional-order"
        leg.order_kind = "trigger"
        leg.attribution_status = "unassigned"
        session.commit()

    def reconcile(*args, **kwargs):
        with session_factory() as session:
            binding = session.get(ExecutionBinding, binding_id)
            binding.pos_id = "resolved-pos"
            leg = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).one()
            leg.pos_id = "resolved-pos"
            leg.order_id = "resolved-pos"
            leg.attribution_status = "verified"
            leg.status = "active"
            session.commit()

    monkeypatch.setattr(planner, "reconcile_deepcoin_execution_bindings", reconcile)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position("resolved-pos")]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert [leg.pos_id for leg in result.batch.legs] == ["resolved-pos"]


def test_partial_filled_range_entry_manages_verified_live_leg(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_partial_filled_range_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [
                _position(
                    "pos-live",
                    size="7",
                    avg_px="64103.8",
                    side="long",
                )
            ],
            tpsl_orders=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "tpTriggerPx": "65600",
                    "sz": "0",
                    "ordId": "tp-live",
                    "cTime": "1721000000000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": "62800",
                    "sz": "0",
                    "ordId": "sl-live",
                    "cTime": "1721000000000",
                },
            ],
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.status == "ready"
    assert result.batch.intent == "partial_then_break_even"
    assert result.batch.effective_action == "partial_then_break_even"
    assert [(leg.pos_id, leg.preflight_size, leg.planned_close_size) for leg in result.batch.legs] == [
        ("pos-live", "7", "3")
    ]


def test_pending_only_range_entry_still_blocks_management(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_partial_filled_range_management_target(
        session_factory, both_pending=True
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_position_ownership_not_verified"
    assert result.batch.legs == ()


@pytest.mark.parametrize(
    ("status", "attribution_status", "pos_id", "reason_code"),
    [
        ("partially_filled", "unassigned", None, "target_position_ownership_not_verified"),
        ("pending", "unassigned", "pos-unsafe", "target_position_ownership_not_verified"),
        ("pending", "attribution_conflict", None, "target_position_ownership_conflict"),
        ("pending", "evidence_unavailable", None, "target_position_evidence_unavailable"),
    ],
)
def test_partial_filled_range_entry_blocks_unsafe_deferred_leg(
    monkeypatch, tmp_path, status, attribution_status, pos_id, reason_code
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_partial_filled_range_management_target(
        session_factory
    )
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, leg_index=2)
            .one()
        )
        leg.status = status
        leg.attribution_status = attribution_status
        leg.pos_id = pos_id
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [_position("pos-live", size="7", avg_px="64103.8", side="long")]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == reason_code
    assert result.batch.legs == ()


def test_changed_target_snapshot_fails_immutable_fingerprint_check(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    changed = deepcopy(result.batch.target_snapshot)
    changed["positions"][0]["size"] = "9"

    with pytest.raises(planner.ManagementTargetChangedError):
        planner.require_unchanged_target_fingerprint(
            result.batch.target_fingerprint, changed
        )


def test_local_identity_change_during_planning_blocks_before_freeze(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    original = planner.canonical_live_position_economics

    def mutate_after_snapshot(*args, **kwargs):
        economics = original(*args, **kwargs)
        with session_factory() as session:
            leg = session.query(ExecutionOrderLeg).filter_by(
                execution_binding_id=binding_id
            ).one()
            leg.pos_id = "pos-new"
            leg.order_id = "pos-new"
            session.commit()
        return economics

    monkeypatch.setattr(planner, "canonical_live_position_economics", mutate_after_snapshot)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position(), _position("pos-new")]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_identity_changed_during_planning"


def test_protection_ambiguity_blocks_every_target(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        pos_ids=("pos-b-1", "pos-b-2"),
    )
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin(
        [_position("pos-b-1"), _position("pos-b-2")],
        tpsl_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "63000",
                "sz": "0",
                "cTime": "1721000000000",
                "ordId": "ambiguous-sl",
            }
        ],
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
        shadow_only=True,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_ambiguous_global_assignment"
    assert result.batch.status == "blocked"
    assert result.batch.execution_mode == "shadow"
    assert result.batch.legs == ()


def test_ledger_backed_unscoped_protection_allows_plan(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        pos_ids=("pos-b-1", "pos-b-2"),
    )
    _disable_reconciliation(monkeypatch, planner)
    with session_factory() as session:
        first_leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id="pos-b-1")
            .one()
        )
        second_leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id="pos-b-2")
            .one()
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=first_leg.id,
            strategy_instance_id=first_leg.strategy_instance_id,
            pos_id="pos-b-1",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="ledger-sl-1",
            purpose="stop_loss",
            trigger_price="63000",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=PLANNED_AT,
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=second_leg.id,
            strategy_instance_id=second_leg.strategy_instance_id,
            pos_id="pos-b-2",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="ledger-sl-2",
            purpose="stop_loss",
            trigger_price="63000",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=PLANNED_AT,
        )
        session.commit()

    client = _ReadOnlyDeepcoin(
        [_position("pos-b-1"), _position("pos-b-2")],
        tpsl_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "63000",
                "sz": "0",
                "cTime": "1721000000000",
                "ordId": "ledger-sl-1",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "63000",
                "sz": "0",
                "cTime": "1721000000000",
                "ordId": "ledger-sl-2",
            }
        ],
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
        shadow_only=True,
    )

    assert result.status == "ready"
    assert result.batch.execution_mode == "shadow"
    assert len(result.batch.legs) == 2
    first_leg = next(leg for leg in result.batch.legs if leg.pos_id == "pos-b-1")
    second_leg = next(leg for leg in result.batch.legs if leg.pos_id == "pos-b-2")
    assert first_leg.old_tpsl["order_ids"] == ["ledger-sl-1"]
    assert first_leg.old_tpsl["evidence"]["match"] == "ledger_confirmed_current_order"
    assert second_leg.old_tpsl["order_ids"] == ["ledger-sl-2"]
    assert second_leg.old_tpsl["evidence"]["match"] == "ledger_confirmed_current_order"


def test_ledger_backed_protection_requires_current_order_id(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
    )
    _disable_reconciliation(monkeypatch, planner)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ).one()
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-b",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="ledger-sl-missing",
            purpose="stop_loss",
            trigger_price="63000",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=PLANNED_AT,
        )
        session.commit()

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=[]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
        shadow_only=True,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_missing_cancellable_order_id"


def test_incomplete_pending_tpsl_snapshot_blocks_without_planning_legs(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="adjust_stop_loss"
    )
    _disable_reconciliation(monkeypatch, planner)

    class PaginatedPendingClient(_ReadOnlyDeepcoin):
        def read_trigger_orders_pending(self, *, inst_id):
            return {"code": "0", "data": [], "nextCursor": "unknown"}

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=PaginatedPendingClient([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_protection_snapshot_incomplete"
    assert result.batch is not None
    assert result.batch.legs == ()
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, result.batch.id)
        assert batch.visibility_next_attempt_at == (
            PLANNED_AT + timedelta(seconds=5)
        ).replace(tzinfo=None)


def test_missing_ledger_order_replans_when_it_becomes_visible_within_five_minutes(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(session_factory, intent="adjust_stop_loss")
    _disable_reconciliation(monkeypatch, planner)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).one()
        upsert_protection_ledger_row(session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg.id, strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-b", instrument_id="BTC-USDT-SWAP", side="short", order_id="sl-1",
            purpose="stop_loss", trigger_price="63000", size_text="0", status="verified",
            evidence_source="entry_protection_response", evidence={}, seen_at=PLANNED_AT)
        session.commit()
    blocked = planner.plan_strategy_management_batch(session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=[]), contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT)
    recovered = planner.plan_strategy_management_batch(session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=[{"instId":"BTC-USDT-SWAP","posSide":"short","triggerOrderType":"TPSL","slTriggerPx":"63000","sz":"0","ordId":"sl-1"}]), contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT + timedelta(seconds=5))
    assert blocked.status == "blocked"
    assert recovered.status == "ready"
    assert recovered.batch.id == blocked.batch.id


def test_visibility_recovery_cannot_become_ready_at_five_minute_deadline(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "deadline.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory, intent="adjust_stop_loss"
    )
    _disable_reconciliation(monkeypatch, planner)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ).one()
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg.id, strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-b", instrument_id="BTC-USDT-SWAP", side="short",
            order_id="sl-1", purpose="stop_loss", trigger_price="63000",
            size_text="0", status="verified", evidence_source="entry", evidence={},
            seen_at=PLANNED_AT,
        )
        session.commit()
    blocked = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=[]),
        contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT,
    )
    deadline = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=[{
            "instId": "BTC-USDT-SWAP", "posSide": "short", "triggerOrderType": "TPSL",
            "slTriggerPx": "63000", "sz": "0", "ordId": "sl-1",
        }]), contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT + timedelta(minutes=5),
    )
    assert blocked.status == "blocked"
    assert deadline.status == "blocked"
    assert deadline.reason_code == "protection_missing_cancellable_order_id"


def test_ledger_backed_protection_requires_matching_price(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
    )
    _disable_reconciliation(monkeypatch, planner)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ).one()
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-b",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="ledger-sl-price-drift",
            purpose="stop_loss",
            trigger_price="63000",
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=PLANNED_AT,
        )
        session.commit()

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [_position()],
            tpsl_orders=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": "62900",
                    "sz": "0",
                    "ordId": "ledger-sl-price-drift",
                }
            ],
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
        shadow_only=True,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_price_or_size_mismatch"


def test_inline_protection_without_exact_order_id_blocks_plan(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="adjust_stop_loss"
    )
    _disable_reconciliation(monkeypatch, planner)
    position = _position()

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [position],
            tpsl_orders=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "short",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": "63000",
                    "sz": "0",
                    "cTime": "1721000000000",
                }
            ],
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_missing_cancellable_order_id"


def test_any_protection_row_without_exact_unique_id_blocks_plan(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="adjust_stop_loss"
    )
    _disable_reconciliation(monkeypatch, planner)
    common = {
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-b",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "sz": "0",
        "cTime": "1721000000000",
    }
    client = _ReadOnlyDeepcoin(
        [_position()],
        tpsl_orders=[
            {**common, "ordId": "exact-sl", "slTriggerPx": "63000"},
            {**common, "tpTriggerPx": "60000"},
        ],
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_missing_cancellable_order_id"


def test_selected_protection_order_id_must_be_unique_across_global_snapshot(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="adjust_stop_loss"
    )
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin(
        [_position()],
        tpsl_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-b",
                "posSide": "short",
                "triggerOrderType": "TPSL",
                "ordId": "duplicate-global-id",
                "slTriggerPx": "63000",
                "sz": "0",
                "cTime": "1721000000000",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "unmatched-long-position",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "ordId": "duplicate-global-id",
                "slTriggerPx": "61000",
                "sz": "0",
                "cTime": "1721000000000",
            },
        ],
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "protection_ambiguous_global_assignment"


@pytest.mark.parametrize(
    "value",
    [
        False,
        True,
        0,
        -0.1,
        1,
        1.1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "0.5",
    ],
)
def test_partial_fraction_boundary_rejects_invalid_or_corrupt_values(value):
    planner = _planner()

    with pytest.raises(planner.ManagementFractionError):
        planner.normalize_requested_management_fraction(
            "partial_take_profit", value
        )


def test_unqualified_partial_fraction_remains_unset_until_policy_resolution():
    planner = _planner()

    assert (
        planner.normalize_requested_management_fraction(
            "partial_take_profit", None
        )
        is None
    )


def test_unqualified_first_partial_plan_defaults_to_half(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=None,
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.requested_fraction is None
    assert result.batch.effective_action == "partial_close"
    assert result.batch.effective_fraction == 0.5
    assert result.batch.partial_round_before == 0
    assert [leg.planned_close_size for leg in result.batch.legs] == ["5"]


def test_partial_plan_accepts_legacy_comma_separated_binding_pos_ids(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=None,
        pos_ids=("pos-b", "pos-c"),
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-b,pos-c"
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [
                _position("pos-b", size="10", avg_px="62000"),
                _position("pos-c", size="8", avg_px="62100"),
            ]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.effective_action == "partial_close"
    assert result.batch.effective_fraction == 0.5
    assert [leg.pos_id for leg in result.batch.legs] == ["pos-b", "pos-c"]
    assert [leg.planned_close_size for leg in result.batch.legs] == ["5", "4"]


def test_partial_plan_rejects_binding_summary_with_unknown_pos_id(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=None,
        pos_ids=("pos-b", "pos-c"),
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-b,pos-x"
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [
                _position("pos-b", size="10", avg_px="62000"),
                _position("pos-c", size="8", avg_px="62100"),
            ]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_binding_position_mismatch"
    assert result.batch is not None
    assert result.batch.legs == ()


def test_partial_then_break_even_plans_durable_close_and_protection_phases(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory,
        intent="partial_then_break_even",
        management_fraction=None,
    )
    _disable_reconciliation(monkeypatch, planner)
    tpsl = [
        {
            "triggerOrderType": "TPSL",
            "ordId": "tp-old",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "posId": "pos-b",
            "tpTriggerPx": "61000",
            "sz": "10",
            "cTime": "1721000000000",
        },
        {
            "triggerOrderType": "TPSL",
            "ordId": "sl-old",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "posId": "pos-b",
            "slTriggerPx": "63000",
            "sz": "0",
            "cTime": "1721000000000",
        },
    ]

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()], tpsl_orders=tpsl),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.intent == "partial_then_break_even"
    assert result.batch.effective_action == "partial_then_break_even"
    assert result.batch.effective_fraction == 0.5
    assert result.batch.legs[0].planned_close_size == "5"
    assert result.batch.legs[0].planned_tpsl == {
        "intent": "partial_then_break_even",
        "stop_loss_text": None,
    }
    assert result.batch.legs[0].old_tpsl["order_ids"] == ["tp-old", "sl-old"]


def _persist_prior_partial_batch(
    session_factory,
    *,
    raw_id,
    lifecycle_id,
    binding_id,
    status,
    reconciled,
    leg_statuses=("confirmed",),
):
    with session_factory() as session:
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )
        batch = StrategyManagementBatch(
            idempotency_fingerprint=f"prior-{status}-{len(leg_statuses)}",
            raw_message_id=raw_id,
            recognition_decision_id=decision.id,
            recognition_generation="prior-generation",
            target_lifecycle_id=lifecycle_id,
            strategy_instance_id="deepcoin:100:20:BTC:short",
            execution_binding_id=binding_id,
            intent="partial_take_profit",
            effective_action="partial_close",
            requested_fraction=None,
            effective_fraction=0.5,
            partial_round_before=0,
            status=status,
            target_fingerprint="f" * 64,
            target_snapshot_json="{}",
            planned_at=PLANNED_AT,
            reconciled_at=PLANNED_AT if reconciled else None,
            completed_at=PLANNED_AT if status == "succeeded" else None,
        )
        session.add(batch)
        session.flush()
        for index, leg_status in enumerate(leg_statuses):
            entry_leg = entry_legs[min(index, len(entry_legs) - 1)]
            session.add(
                StrategyManagementLeg(
                    management_batch_id=batch.id,
                    execution_order_leg_id=entry_leg.id,
                    pos_id=str(entry_leg.pos_id),
                    leg_index=index,
                    status=leg_status,
                    preflight_size="10",
                    planned_close_size="5",
                    quantity_step="1",
                )
            )
        session.commit()
        return batch.id


def test_reconciled_succeeded_first_partial_promotes_second_partial_to_full_close(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=0.3,
    )
    _persist_prior_partial_batch(
        session_factory,
        raw_id=raw_id,
        lifecycle_id=lifecycle_id,
        binding_id=binding_id,
        status="succeeded",
        reconciled=True,
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.requested_fraction == 0.3
    assert result.batch.effective_action == "full_close"
    assert result.batch.effective_fraction == 1.0
    assert result.batch.partial_round_before == 1
    assert [leg.planned_close_size for leg in result.batch.legs] == ["10"]


def test_second_partial_then_break_even_promotes_to_full_close_without_protection(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_then_break_even",
        management_fraction=0.3,
    )
    _persist_prior_partial_batch(
        session_factory,
        raw_id=raw_id,
        lifecycle_id=lifecycle_id,
        binding_id=binding_id,
        status="succeeded",
        reconciled=True,
    )
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin([_position()])

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.effective_action == "full_close"
    assert result.batch.effective_fraction == 1.0
    assert result.batch.partial_round_before == 1
    assert result.batch.legs[0].planned_close_size == "10"
    assert result.batch.legs[0].planned_tpsl is None
    assert result.batch.legs[0].old_tpsl is None


@pytest.mark.parametrize(
    ("status", "reconciled", "leg_statuses"),
    [
        ("submitted", False, ("submitted",)),
        ("submit_unknown", False, ("submit_unknown",)),
        ("failed", False, ("failed",)),
        ("partial_failed", True, ("confirmed", "failed")),
        ("succeeded", True, ("submitted",)),
    ],
)
def test_unconfirmed_prior_partial_freezes_without_advancing_round(
    monkeypatch, tmp_path, status, reconciled, leg_statuses
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    pos_ids = ("pos-b-1", "pos-b-2") if len(leg_statuses) == 2 else ("pos-b",)
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        pos_ids=pos_ids,
    )
    _persist_prior_partial_batch(
        session_factory,
        raw_id=raw_id,
        lifecycle_id=lifecycle_id,
        binding_id=binding_id,
        status=status,
        reconciled=reconciled,
        leg_statuses=leg_statuses,
    )
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [_position(pos_id) for pos_id in pos_ids]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "prior_partial_batch_unresolved"
    assert result.batch is None


def test_duplicate_partial_message_returns_same_batch_without_advancing_round(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="partial_take_profit", management_fraction=None
    )
    _disable_reconciliation(monkeypatch, planner)
    client = _ReadOnlyDeepcoin([_position()])

    first = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    duplicate = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert duplicate.batch.id == first.batch.id
    assert duplicate.batch.partial_round_before == 0
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 1


def test_retryable_preflight_blocked_batch_replans_when_snapshot_recovers(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    missing_mode = dict(_position())
    missing_mode.pop("posMode")

    blocked = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([missing_mode]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    recovered = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert blocked.status == "blocked"
    assert blocked.reason_code == "target_live_position_mode_unavailable"
    assert recovered.status == "ready"
    assert recovered.batch.id == blocked.batch.id
    assert recovered.reason_code is None
    assert len(recovered.batch.legs) == 1
    assert recovered.batch.legs[0].pos_id == "pos-b"
    assert recovered.batch.target_snapshot["positions"][0]["position_mode"] == "split"
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 1
        assert session.query(StrategyManagementLeg).count() == 1


def test_preflight_blocked_batch_with_existing_leg_is_not_replanned(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    missing_mode = dict(_position())
    missing_mode.pop("posMode")
    blocked = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([missing_mode]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    with session_factory() as session:
        entry_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-b").one()
        session.add(
            StrategyManagementLeg(
                management_batch_id=blocked.batch.id,
                execution_order_leg_id=entry_leg.id,
                pos_id="pos-b",
                leg_index=0,
                status="planned",
            )
        )
        session.commit()

    repeated = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert repeated.status == "blocked"
    assert repeated.reason_code == "target_live_position_mode_unavailable"
    assert repeated.batch.id == blocked.batch.id
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 1
        assert session.query(StrategyManagementLeg).count() == 1


def test_partial_round_history_is_revalidated_inside_insert_transaction(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory, intent="partial_take_profit"
    )
    prior_batch_id = _persist_prior_partial_batch(
        session_factory,
        raw_id=raw_id,
        lifecycle_id=lifecycle_id,
        binding_id=binding_id,
        status="succeeded",
        reconciled=True,
    )
    _disable_reconciliation(monkeypatch, planner)
    original = planner.create_management_batch_in_session

    def mutate_round_then_create(session, *args, **kwargs):
        with session_factory() as other_session:
            prior = other_session.get(StrategyManagementBatch, prior_batch_id)
            prior.status = "resolved"
            other_session.commit()
        return original(session, *args, **kwargs)

    monkeypatch.setattr(
        planner, "create_management_batch_in_session", mutate_round_then_create
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_identity_changed_during_planning"
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 1


def test_corrupt_persisted_partial_fraction_blocks_without_batch(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(
        session_factory, intent="partial_take_profit"
    )
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE signal_candidates SET management_fraction = 'corrupt' "
                "WHERE raw_message_id = :raw_id"
            ),
            {"raw_id": raw_id},
        )
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "management_fraction_invalid"
    assert result.batch is None


def test_final_revalidation_runs_inside_insert_transaction(monkeypatch, tmp_path):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    original = planner.create_management_batch_in_session

    def mutate_then_create(session, *args, **kwargs):
        with session_factory() as other_session:
            candidate = (
                other_session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == raw_id)
                .one()
            )
            candidate.management_action = "partial_take_profit"
            other_session.commit()
        return original(session, *args, **kwargs)

    monkeypatch.setattr(
        planner, "create_management_batch_in_session", mutate_then_create
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_identity_changed_during_planning"
    assert result.batch is None
    with session_factory() as session:
        from telegram_kol_research.models import StrategyManagementBatch

        assert session.query(StrategyManagementBatch).count() == 0


def test_final_revalidation_blocks_raw_chat_change_inside_insert_transaction(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    original = planner.create_management_batch_in_session

    def mutate_then_create(session, *args, **kwargs):
        with session_factory() as other_session:
            other_session.get(RawMessage, raw_id).chat_id = 999
            other_session.commit()
        return original(session, *args, **kwargs)

    monkeypatch.setattr(planner, "create_management_batch_in_session", mutate_then_create)
    result = planner.plan_strategy_management_batch(
        session_factory, raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(), planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_identity_changed_during_planning"
    assert result.batch is None


def test_final_competing_owner_error_returns_blocked_without_leaking(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, _ = _persist_exact_management_target(session_factory)
    _disable_reconciliation(monkeypatch, planner)
    original = planner.require_verified_position_ownership
    calls = 0

    def competing_owner(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise planner.PositionAttributionError("position_ownership_not_unique")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        planner, "require_verified_position_ownership", competing_owner
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin([_position()]),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_identity_changed_during_planning"
    assert result.batch is None
