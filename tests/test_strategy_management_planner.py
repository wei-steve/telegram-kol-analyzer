from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime

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
)


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
                    if intent == "partial_take_profit"
                    and management_fraction == "default"
                    else management_fraction
                    if intent == "partial_take_profit"
                    else None
                ),
                recognition_generation="generation-b",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        return management.id, lifecycle_b.id, binding_b.id if binding_b else None


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
    )

    assert result.status == "blocked"
    assert result.reason_code == reason
    assert result.batch.status == "blocked"
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
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_protection_not_verified"
    assert result.batch.status == "blocked"
    assert result.batch.legs == ()


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
    assert result.reason_code == "target_protection_order_identity_unavailable"


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
    assert result.reason_code == "target_protection_order_identity_unavailable"


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
    assert result.reason_code == "target_protection_order_identity_unavailable"


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


def test_unqualified_partial_fraction_remains_unset_for_task5():
    planner = _planner()

    assert (
        planner.normalize_requested_management_fraction(
            "partial_take_profit", None
        )
        is None
    )


def test_unqualified_partial_plan_persists_no_effective_fraction_or_close_size(
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
    assert result.batch.effective_fraction is None
    assert [leg.planned_close_size for leg in result.batch.legs] == [None]


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
