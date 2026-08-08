from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_assembly_fingerprint_repair import (
    RECONCILIATION_ACTION,
    apply_entry_assembly_fingerprint_repair_plan,
    build_entry_assembly_fingerprint_repair_plan,
    canonical_fingerprint,
    derive_pre_finalization_fingerprint,
)
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    TradeSignal,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
STRATEGY_ID = "deepcoin:-1001:55:BTC:long"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _final_evidence() -> dict[str, object]:
    evidence: dict[str, object] = {
        "chat_id": -1001,
        "strategy_raw_message_id": 10,
        "strategy_message_id": 55,
        "signal_candidate_id": 20,
        "symbol": "BTC",
        "side": "long",
        "fragment_ids": [30],
        "legacy_preamble_ids": [],
        "risk_multiplier": "0.5",
        "allocations": ["0.6", "0.4"],
        "supplemental_prices": [],
        "cutoff": ["2026-08-08T11:59:00+00:00", 55, 10],
        "planned_entry_leg_count": 2,
        "order_draft_snapshot": {
            "strategy_instance_id": STRATEGY_ID,
            "instrument_id": "BTC-USDT-SWAP",
            "stop_loss": 63000,
            "take_profit_legs": [{"price": 66000, "allocation_pct": 100}],
            "risk_budget_usdt": 10,
            "contract_spec": {
                "contract_value": 0.001,
                "quantity_step": 1,
                "min_quantity": 1,
            },
            "order_legs": [
                {
                    "price": 64000,
                    "order_type": "limit",
                    "allocation_pct": 60,
                    "risk_budget_usdt": 6,
                    "quantity": 10,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 6,
                    "client_order_id": "entry-1",
                },
                {
                    "price": 63800,
                    "order_type": "limit",
                    "allocation_pct": 40,
                    "risk_budget_usdt": 4,
                    "quantity": 5,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 4,
                    "client_order_id": "entry-2",
                },
            ],
        },
        "final_entry_leg_count": 2,
    }
    return evidence


def _seed_case(tmp_path):
    database_path = tmp_path / "repair.db"
    session_factory = create_session_factory(database_path)
    final_evidence = _final_evidence()
    final_fingerprint = canonical_fingerprint(final_evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(final_evidence)
    stale_assembly = {
        "assembly_id": 2,
        "strategy_instance_id": STRATEGY_ID,
        "assembly_fingerprint": old_fingerprint,
    }
    draft = deepcopy(final_evidence["order_draft_snapshot"])
    draft["entry_preamble_assembly"] = deepcopy(stale_assembly)
    signal_payload = {
        "entry_preamble_assembly": deepcopy(stale_assembly),
        "deepcoin_order_draft": deepcopy(draft),
    }
    with session_factory() as session:
        session.add(
            EntryStrategyAssembly(
                id=2,
                entry_preamble_id=None,
                strategy_raw_message_id=10,
                signal_candidate_id=20,
                strategy_instance_id=STRATEGY_ID,
                risk_multiplier="0.5",
                evidence_json=_canonical_json(final_evidence),
                fingerprint=final_fingerprint,
                created_at=NOW,
            )
        )
        session.add(
            TradeSignal(
                id=398,
                signal_uid="repair-signal",
                strategy_instance_id=STRATEGY_ID,
                source_type="recovery",
                venue="deepcoin",
                kol_id="group:-1001",
                chat_id=-1001,
                message_id=55,
                symbol="BTC",
                side="long",
                action="open_position",
                status="submitted",
                payload_json=_canonical_json(signal_payload),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionBinding(
                id=266,
                strategy_instance_id=STRATEGY_ID,
                kol_id="group:-1001",
                chat_id=-1001,
                message_id=55,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                payload_json=_canonical_json({"draft": draft}),
                status="open",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        for index, leg in enumerate(final_evidence["order_draft_snapshot"]["order_legs"], 1):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=266,
                    strategy_instance_id=STRATEGY_ID,
                    leg_index=index,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"order-{index}",
                    client_order_id=leg["client_order_id"],
                    venue="deepcoin",
                    status="open",
                    request_json=_canonical_json(
                        {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "price": str(leg["price"]),
                            "orderType": leg["order_type"],
                            "sz": str(leg["quantity"]),
                            "clOrdId": leg["client_order_id"],
                        }
                    ),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()
    return database_path, session_factory


def _plan(session_factory):
    return build_entry_assembly_fingerprint_repair_plan(
        session_factory, assembly_id=2, execution_binding_id=266
    )


def test_build_plan_returns_one_stable_action_without_writing(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    before = database_path.read_bytes()

    first = _plan(session_factory)
    second = _plan(session_factory)

    assert first == second
    assert first.conflicts == ()
    assert first.action is not None
    assert first.action.assembly_id == 2
    assert first.action.execution_binding_id == 266
    assert first.action.trade_signal_id == 398
    assert first.action.strategy_instance_id == STRATEGY_ID
    assert first.action.old_fingerprint == derive_pre_finalization_fingerprint(
        _final_evidence()
    )
    assert first.action.final_fingerprint == canonical_fingerprint(_final_evidence())
    assert len(first.action.repair_fingerprint) == 64
    assert len(first.fingerprint) == 64
    assert database_path.read_bytes() == before


@pytest.mark.parametrize(
    ("mutation", "expected_conflict"),
    [
        ("wrong_strategy", "binding_strategy_mismatch"),
        ("old_not_derivable", "binding_old_fingerprint_not_derivable"),
        ("draft_identity", "binding_draft_identity_mismatch"),
        ("leg_identity", "execution_leg_identity_mismatch"),
        ("missing_finalization", "assembly_finalization_fields_missing"),
        ("prior_event", "reconciliation_event_conflict"),
    ],
)
def test_build_plan_rejects_unproven_or_conflicting_state(
    tmp_path, mutation, expected_conflict
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        binding = session.get(ExecutionBinding, 266)
        if mutation == "wrong_strategy":
            binding.strategy_instance_id = "other-strategy"
        elif mutation == "old_not_derivable":
            payload = json.loads(binding.payload_json)
            payload["draft"]["entry_preamble_assembly"]["assembly_fingerprint"] = "f" * 64
            binding.payload_json = _canonical_json(payload)
        elif mutation == "draft_identity":
            payload = json.loads(binding.payload_json)
            payload["draft"]["order_legs"][0]["price"] = 1
            binding.payload_json = _canonical_json(payload)
        elif mutation == "leg_identity":
            leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
            request = json.loads(leg.request_json)
            request["price"] = 1
            leg.request_json = _canonical_json(request)
        elif mutation == "missing_finalization":
            evidence = json.loads(assembly.evidence_json)
            evidence.pop("final_entry_leg_count")
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation == "prior_event":
            session.add(
                ExecutionEvent(
                    execution_binding_id=266,
                    trade_signal_id=398,
                    strategy_instance_id=STRATEGY_ID,
                    action=RECONCILIATION_ACTION,
                    status="resolved",
                    before_json="{}",
                    after_json="{}",
                    notification_fingerprint="e" * 64,
                    created_at=NOW,
                )
            )
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert expected_conflict in plan.conflicts


def test_apply_requires_exact_plan_fingerprint_and_is_idempotent(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    plan = _plan(session_factory)
    with session_factory() as session:
        immutable_before = {
            "assembly": session.get(EntryStrategyAssembly, 2).evidence_json,
            "binding": session.get(ExecutionBinding, 266).payload_json,
            "signal": session.get(TradeSignal, 398).payload_json,
            "legs": tuple(
                (row.id, row.request_json, row.response_json)
                for row in session.query(ExecutionOrderLeg)
                .order_by(ExecutionOrderLeg.id)
                .all()
            ),
        }
    with pytest.raises(ValueError, match="repair_plan_fingerprint_mismatch"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint="0" * 64,
            applied_at=NOW,
        )
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0

    event_id = apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    repeated_id = apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )

    assert repeated_id == event_id
    with session_factory() as session:
        events = session.query(ExecutionEvent).all()
        assert len(events) == 1
        event = events[0]
        assert event.action == RECONCILIATION_ACTION
        assert event.status == "resolved"
        assert event.notification_status is None
        assert event.notification_fingerprint == plan.action.repair_fingerprint
        assert json.loads(event.before_json) == {
            "assembly_id": 2,
            "execution_binding_id": 266,
            "trade_signal_id": 398,
            "strategy_instance_id": STRATEGY_ID,
            "assembly_fingerprint": plan.action.old_fingerprint,
            "policy": "entry-assembly-fingerprint-reconciliation-v1",
        }
        assert json.loads(event.after_json)["assembly_fingerprint"] == (
            plan.action.final_fingerprint
        )
        assert session.query(EntryStrategyAssembly).count() == 1
        assert session.query(ExecutionBinding).count() == 1
        assert session.query(TradeSignal).count() == 1
        assert session.query(ExecutionOrderLeg).count() == 2
        assert session.get(EntryStrategyAssembly, 2).evidence_json == immutable_before["assembly"]
        assert session.get(ExecutionBinding, 266).payload_json == immutable_before["binding"]
        assert session.get(TradeSignal, 398).payload_json == immutable_before["signal"]
        assert tuple(
            (row.id, row.request_json, row.response_json)
            for row in session.query(ExecutionOrderLeg)
            .order_by(ExecutionOrderLeg.id)
            .all()
        ) == immutable_before["legs"]


def test_apply_rejects_existing_reconciliation_with_extra_evidence(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    plan = _plan(session_factory)
    common = {
        "policy": "entry-assembly-fingerprint-reconciliation-v1",
        "assembly_id": 2,
        "execution_binding_id": 266,
        "trade_signal_id": 398,
        "strategy_instance_id": STRATEGY_ID,
    }
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=266,
                trade_signal_id=398,
                strategy_instance_id=STRATEGY_ID,
                venue="deepcoin",
                action=RECONCILIATION_ACTION,
                status="resolved",
                reason="entry-assembly-fingerprint-reconciliation-v1",
                notification_fingerprint=plan.action.repair_fingerprint,
                before_json=_canonical_json(
                    {**common, "assembly_fingerprint": plan.action.old_fingerprint}
                ),
                after_json=_canonical_json(
                    {
                        **common,
                        "assembly_fingerprint": plan.action.final_fingerprint,
                        "repair_fingerprint": plan.action.repair_fingerprint,
                    }
                ),
                request_json='{"unexpected":true}',
                created_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="repair_plan_not_actionable"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=plan.fingerprint,
            applied_at=NOW,
        )


def test_apply_rejects_unique_fingerprint_collision(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    plan = _plan(session_factory)
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                action="unrelated_action",
                status="resolved",
                notification_fingerprint=plan.action.repair_fingerprint,
                created_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="repair_event_fingerprint_collision"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=plan.fingerprint,
            applied_at=NOW,
        )
