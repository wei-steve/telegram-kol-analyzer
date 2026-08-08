from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_assembly_fingerprint_repair import (
    LEGACY_FINALIZED_RECONCILIATION_POLICY,
    RECONCILIATION_ACTION,
    RECONCILIATION_POLICY,
    apply_entry_assembly_fingerprint_repair_plan,
    build_entry_assembly_fingerprint_repair_plan,
    canonical_fingerprint,
    derive_pre_finalization_fingerprint,
    _json_object,
)
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
    TradeSignal,
)
from telegram_kol_research.production_safety_monitor import (
    _read_reconciliation_json,
    read_entry_preamble_invariants,
)
from telegram_kol_research.recovery_live_submit import (
    build_deepcoin_trigger_order_payload,
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
            "symbol": "BTC",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 63000,
            "take_profit_legs": [{"price": 66000, "allocation_pct": 100}],
            "risk_budget_usdt": 10,
            "contract_spec": {
                "contract_value": 0.001,
                "quantity_step": 1,
                "min_quantity": 1,
            },
            "source": {
                "kol_id": "group:-1001",
                "kol_code": "group-a",
                "chat_id": -1001,
                "message_id": 55,
            },
            "selected_entry_leg_indices": [1, 2],
            "selected_entry_leg_count": 2,
            "order_legs": [
                {
                    "price": 64000,
                    "order_type": "limit",
                    "allocation_pct": 60,
                    "risk_budget_usdt": 6,
                    "quantity": 10,
                    "base_asset_estimate": 0.01,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 6,
                    "client_order_id": "entry-1",
                    "side": "buy",
                    "position_side": "long",
                    "take_profit_leg": {"price": 66000, "allocation_pct": 60},
                },
                {
                    "price": 63800,
                    "order_type": "limit",
                    "allocation_pct": 40,
                    "risk_budget_usdt": 4,
                    "quantity": 5,
                    "base_asset_estimate": 0.005,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 4,
                    "client_order_id": "entry-2",
                    "side": "buy",
                    "position_side": "long",
                    "take_profit_leg": {"price": 66000, "allocation_pct": 40},
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
            RawMessage(
                id=10,
                chat_id=-1001,
                message_id=55,
                text="BTC long entry",
                archived_target_group=True,
                created_at=NOW,
            )
        )
        session.add(
            SignalCandidate(
                id=20,
                raw_message_id=10,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                parse_source="mimo_authoritative",
                confidence=0.95,
                review_status="pending",
                created_at=NOW,
            )
        )
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
                processed_at=NOW,
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
                    order_kind="trigger_limit",
                    order_id=f"order-{index}",
                    client_order_id=leg["client_order_id"],
                    venue="deepcoin",
                    status="open",
                    request_json=_canonical_json(
                        {
                            "instId": "BTC-USDT-SWAP",
                            "productGroup": "Swap",
                            "posSide": "long",
                            "side": "buy",
                            "tdMode": "cross",
                            "mrgPosition": "split",
                            "price": str(leg["price"]),
                            "triggerPrice": str(leg["price"]),
                            "triggerPxType": "last",
                            "isCrossMargin": "1",
                            "orderType": leg["order_type"],
                            "sz": str(leg["quantity"]),
                            "clOrdId": leg["client_order_id"],
                            "slTriggerPx": "63000",
                            "slTriggerPxType": "last",
                            "slOrdPx": "-1",
                        }
                    ),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()
    return database_path, session_factory


def _convert_to_production_legacy_finalized_case(session_factory) -> None:
    """Match the pre-fix schema observed for assembly 2 / binding 266."""

    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        binding = session.get(ExecutionBinding, 266)
        signal = session.get(TradeSignal, 398)
        evidence = json.loads(assembly.evidence_json)
        binding_payload = json.loads(binding.payload_json)
        original_draft = binding_payload["draft"]
        full_draft = {
            "blocking_reason_codes": [],
            "contract_spec": {
                **original_draft["contract_spec"],
                "instrument_id": "BTC-USDT-SWAP",
                "price_tick": 0.1,
            },
            "dry_run_only": False,
            "entry_preamble_assembly": None,
            "executable": True,
            "instrument_id": original_draft["instrument_id"],
            "margin_mode": original_draft["margin_mode"],
            "notes": [],
            "order_legs": [
                {
                    key: leg[key]
                    for key in (
                        "allocation_pct",
                        "base_asset_estimate",
                        "client_order_id",
                        "estimated_stop_loss_usdt",
                        "order_type",
                        "position_side",
                        "price",
                        "quantity",
                        "quantity_unit",
                        "risk_budget_usdt",
                        "side",
                    )
                }
                for leg in original_draft["order_legs"]
            ],
            "position_mode": original_draft["position_mode"],
            "risk_budget_usdt": original_draft["risk_budget_usdt"],
            "source": deepcopy(original_draft["source"]),
            "stop_loss": original_draft["stop_loss"],
            "strategy_instance_id": original_draft["strategy_instance_id"],
            "symbol": original_draft["symbol"],
            "take_profit_legs": [
                {
                    "allocation_pct": 100,
                    "index": 1,
                    "order_type": "limit",
                    "price": 66000,
                }
            ],
            "venue": "deepcoin",
        }
        legacy_snapshot = {
            "strategy_instance_id": full_draft["strategy_instance_id"],
            "instrument_id": full_draft["instrument_id"],
            "stop_loss": full_draft["stop_loss"],
            "take_profit_legs": deepcopy(full_draft["take_profit_legs"]),
            "risk_budget_usdt": full_draft["risk_budget_usdt"],
            "contract_spec": {
                key: full_draft["contract_spec"][key]
                for key in ("contract_value", "quantity_step", "min_quantity")
            },
            "order_legs": [
                {
                    key: leg[key]
                    for key in (
                        "price",
                        "order_type",
                        "allocation_pct",
                        "risk_budget_usdt",
                        "quantity",
                        "quantity_unit",
                        "estimated_stop_loss_usdt",
                        "client_order_id",
                    )
                }
                for leg in full_draft["order_legs"]
            ],
        }
        evidence["order_draft_snapshot"] = legacy_snapshot
        evidence["final_entry_leg_count"] = len(legacy_snapshot["order_legs"])
        final_fingerprint = canonical_fingerprint(evidence)
        old_fingerprint = derive_pre_finalization_fingerprint(evidence)
        stale = {
            "assembly_id": 2,
            "strategy_instance_id": STRATEGY_ID,
            "assembly_fingerprint": old_fingerprint,
        }
        full_draft["entry_preamble_assembly"] = deepcopy(stale)
        signal_draft = deepcopy(full_draft)
        signal.payload_json = _canonical_json(
            {
                "deepcoin_order_draft": signal_draft,
                "source": {
                    "chat_id": -1001,
                    "message_id": 55,
                    "side": "long",
                    "symbol": "BTC",
                },
            }
        )
        assembly.evidence_json = _canonical_json(evidence)
        assembly.fingerprint = final_fingerprint
        submitted_orders = []
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "pending"
            request = build_deepcoin_trigger_order_payload(
                full_draft, full_draft["order_legs"][leg.leg_index - 1]
            )
            leg.request_json = _canonical_json(request)
            submitted_orders.append(
                {
                    "client_order_id": leg.client_order_id,
                    "execution_type": "trigger_limit",
                    "leg_index": leg.leg_index,
                    "order_id": leg.order_id,
                    "pos_id": None,
                    "protection_request": {
                        "slOrdPx": request["slOrdPx"],
                        "slTriggerPx": request["slTriggerPx"],
                    },
                    "protection_response": {
                        "code": "0",
                        "data": {"attached_on_trigger_order": True},
                    },
                    "request": request,
                    "response": {
                        "code": "0",
                        "data": {
                            "clOrdId": "",
                            "ordId": leg.order_id,
                            "sCode": "0",
                            "sMsg": "",
                            "tag": "",
                        },
                        "msg": "",
                    },
                }
            )
        binding.payload_json = _canonical_json(
            {"draft": full_draft, "submitted_orders": submitted_orders}
        )
        binding.order_id = "order-1,order-2"
        binding.client_order_id = "entry-1,entry-2"
        binding.pos_id = None
        binding.status = "open"
        binding.last_exchange_status = "entry_order_pending"
        session.commit()


def _plan(session_factory):
    return build_entry_assembly_fingerprint_repair_plan(
        session_factory, assembly_id=2, execution_binding_id=266
    )


def _transition_legacy_binding_lifecycle(session, state: str) -> None:
    binding = session.get(ExecutionBinding, 266)
    legs = session.query(ExecutionOrderLeg).order_by(
        ExecutionOrderLeg.leg_index
    ).all()
    if state == "active":
        binding.status = "active"
        binding.last_exchange_status = "position_ownership_verified"
        binding.pos_id = "pos-1,pos-2"
        for index, leg in enumerate(legs, 1):
            leg.status = "active"
            leg.pos_id = f"pos-{index}"
            leg.attribution_status = "verified"
    elif state == "closed":
        binding.status = "closed"
        binding.last_exchange_status = "entry_legs_terminal"
        binding.pos_id = None
        for leg in legs:
            leg.status = "closed"
            leg.pos_id = None
    else:
        raise AssertionError(f"unsupported lifecycle state: {state}")


def _add_legacy_action_event(
    session,
    *,
    action: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    pos_id: str | None = None,
    related_order_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    request: dict | None = None,
    response: dict | None = None,
) -> None:
    session.add(
        ExecutionEvent(
            execution_binding_id=266,
            strategy_instance_id=STRATEGY_ID,
            venue="deepcoin",
            action=action,
            status="submitted",
            order_id=order_id,
            client_order_id=client_order_id,
            pos_id=pos_id,
            related_order_id=related_order_id,
            before_json=_canonical_json(before) if before is not None else None,
            after_json=_canonical_json(after) if after is not None else None,
            request_json=_canonical_json(request) if request is not None else None,
            response_json=(
                _canonical_json(response) if response is not None else None
            ),
            created_at=NOW,
        )
    )


def _rewrite_all_draft_copies(session, mutate) -> None:
    assembly = session.get(EntryStrategyAssembly, 2)
    evidence = json.loads(assembly.evidence_json)
    mutate(evidence["order_draft_snapshot"])
    assembly.evidence_json = _canonical_json(evidence)
    assembly.fingerprint = canonical_fingerprint(evidence)
    binding = session.get(ExecutionBinding, 266)
    binding_payload = json.loads(binding.payload_json)
    mutate(binding_payload["draft"])
    binding.payload_json = _canonical_json(binding_payload)
    signal = session.get(TradeSignal, 398)
    signal_payload = json.loads(signal.payload_json)
    mutate(signal_payload["deepcoin_order_draft"])
    signal.payload_json = _canonical_json(signal_payload)


def _convert_first_selected_leg_to_market(session) -> ExecutionOrderLeg:
    def mutate(draft):
        draft["selected_entry_leg_indices"] = [1]
        draft["selected_entry_leg_count"] = 1
        draft["order_legs"][0]["order_type"] = "market"

    _rewrite_all_draft_copies(session, mutate)
    legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all()
    session.delete(legs[1])
    legs[0].order_kind = "market"
    legs[0].request_json = _canonical_json(
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "buy",
            "posSide": "long",
            "ordType": "market",
            "sz": "10",
            "clOrdId": "entry-1",
            "mrgPosition": "split",
        }
    )
    return legs[0]


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


def test_build_plan_accepts_exact_production_legacy_finalized_snapshot(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    before = database_path.read_bytes()

    plan = _plan(session_factory)

    assert plan.conflicts == ()
    assert plan.action is not None
    assert plan.action.policy_version == LEGACY_FINALIZED_RECONCILIATION_POLICY
    assert plan.action.trade_signal_id == 398
    with session_factory() as session:
        requests = [
            json.loads(row.request_json)
            for row in session.query(ExecutionOrderLeg)
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        ]
    assert len(requests) == 2
    assert all(len(request) == 16 for request in requests)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        assert binding.order_id == "order-1,order-2"
        assert binding.client_order_id == "entry-1,entry-2"
        assert binding.pos_id is None
        assert binding.status == "open"
        assert binding.last_exchange_status == "entry_order_pending"
    assert database_path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "full_draft_extra",
        "full_draft_missing",
        "contract_extra",
        "source_extra",
        "leg_extra",
        "take_profit_extra",
        "wrong_boolean_type",
        "normalized_missing_take_profit",
    ],
)
def test_legacy_policy_rejects_any_schema_tolerance(tmp_path, mutation):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        binding = session.get(ExecutionBinding, 266)
        signal = session.get(TradeSignal, 398)
        binding_payload = json.loads(binding.payload_json)
        signal_payload = json.loads(signal.payload_json)
        binding_draft = binding_payload["draft"]
        signal_draft = signal_payload["deepcoin_order_draft"]
        if mutation == "full_draft_extra":
            binding_draft["unexpected"] = signal_draft["unexpected"] = True
        elif mutation == "full_draft_missing":
            binding_draft.pop("blocking_reason_codes")
            signal_draft.pop("blocking_reason_codes")
        elif mutation == "contract_extra":
            binding_draft["contract_spec"]["unexpected"] = True
            signal_draft["contract_spec"]["unexpected"] = True
        elif mutation == "source_extra":
            binding_draft["source"]["unexpected"] = True
            signal_draft["source"]["unexpected"] = True
        elif mutation == "leg_extra":
            binding_draft["order_legs"][0]["unexpected"] = True
            signal_draft["order_legs"][0]["unexpected"] = True
        elif mutation == "take_profit_extra":
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["take_profit_legs"][0][
                "unexpected"
            ] = True
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
            binding_draft["take_profit_legs"][0]["unexpected"] = True
            signal_draft["take_profit_legs"][0]["unexpected"] = True
            old_fp = derive_pre_finalization_fingerprint(evidence)
            binding_draft["entry_preamble_assembly"]["assembly_fingerprint"] = old_fp
            signal_draft["entry_preamble_assembly"]["assembly_fingerprint"] = old_fp
        elif mutation == "wrong_boolean_type":
            binding_draft["executable"] = signal_draft["executable"] = 1
        else:
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["take_profit_legs"] = []
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
            binding_draft.pop("take_profit_legs")
            signal_draft.pop("take_profit_legs")
            old_fp = derive_pre_finalization_fingerprint(evidence)
            binding_draft["entry_preamble_assembly"]["assembly_fingerprint"] = old_fp
            signal_draft["entry_preamble_assembly"]["assembly_fingerprint"] = old_fp
        binding.payload_json = _canonical_json(binding_payload)
        signal.payload_json = _canonical_json(signal_payload)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert set(plan.conflicts) & {
        "assembly_finalization_fields_missing",
        "assembly_legacy_snapshot_binding_mismatch",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "binding_extra",
        "binding_missing_submitted",
        "signal_extra",
        "signal_missing_source",
        "signal_source_extra",
        "signal_source_missing",
        "submitted_extra",
        "submitted_missing",
        "submitted_order_id",
        "response_client_alias",
        "coordinated_one_leg",
    ],
)
def test_legacy_policy_rejects_wrapper_or_submitted_order_drift(
    tmp_path, mutation
):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        signal = session.get(TradeSignal, 398)
        binding_payload = json.loads(binding.payload_json)
        signal_payload = json.loads(signal.payload_json)
        if mutation == "binding_extra":
            binding_payload["unexpected"] = True
        elif mutation == "binding_missing_submitted":
            binding_payload.pop("submitted_orders")
        elif mutation == "signal_extra":
            signal_payload["unexpected"] = True
        elif mutation == "signal_missing_source":
            signal_payload.pop("source")
        elif mutation == "signal_source_extra":
            signal_payload["source"]["unexpected"] = True
        elif mutation == "signal_source_missing":
            signal_payload["source"].pop("side")
        elif mutation == "submitted_extra":
            binding_payload["submitted_orders"][0]["unexpected"] = True
        elif mutation == "submitted_missing":
            binding_payload["submitted_orders"][0].pop("response")
        elif mutation == "submitted_order_id":
            binding_payload["submitted_orders"][0]["order_id"] = "other-order"
        elif mutation == "response_client_alias":
            binding_payload["submitted_orders"][0]["response"]["data"][
                "clOrdId"
            ] = "other-client"
        else:
            assembly = session.get(EntryStrategyAssembly, 2)
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["order_legs"].pop()
            evidence["final_entry_leg_count"] = 1
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
            binding_payload["draft"]["order_legs"].pop()
            binding_payload["submitted_orders"].pop()
            signal_payload["deepcoin_order_draft"]["order_legs"].pop()
            old_fp = derive_pre_finalization_fingerprint(evidence)
            binding_payload["draft"]["entry_preamble_assembly"][
                "assembly_fingerprint"
            ] = old_fp
            signal_payload["deepcoin_order_draft"]["entry_preamble_assembly"][
                "assembly_fingerprint"
            ] = old_fp
            session.delete(
                session.query(ExecutionOrderLeg).filter_by(leg_index=2).one()
            )
        binding.payload_json = _canonical_json(binding_payload)
        signal.payload_json = _canonical_json(signal_payload)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert set(plan.conflicts) & {
        "assembly_legacy_snapshot_binding_mismatch",
        "binding_draft_identity_mismatch",
        "trade_signal_evidence_mismatch",
        "execution_leg_identity_mismatch",
    }


@pytest.mark.parametrize(
    "mutation",
    ["order_id", "client_order_id", "pos_id", "status", "last_exchange_status"],
)
def test_legacy_policy_rejects_binding_top_order_identity_drift(
    tmp_path, mutation
):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        setattr(
            binding,
            mutation,
            {
                "order_id": "other-order,order-2",
                "client_order_id": "other-client,entry-2",
                "pos_id": "pos-1",
                "status": "active",
                "last_exchange_status": "submitted",
            }[mutation],
        )
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "binding_top_order_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":1,"value":1}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
    ],
)
def test_repair_json_parser_rejects_duplicate_keys_and_nonfinite_values(raw):
    assert _json_object(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":1e999}',
        '{"value":-1e999}',
        '{"nested":[{"value":1e999}]}',
        '{"value":"\ud800"}',
    ],
)
def test_monitor_and_repair_json_parsers_reject_the_same_invalid_values(raw):
    assert _json_object(raw) is None
    assert _read_reconciliation_json(raw) is None


@pytest.mark.parametrize(
    "document",
    ["assembly", "binding", "signal", "request", "event"],
)
def test_duplicate_json_key_blocks_plan_and_apply(tmp_path, document):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    original_plan = _plan(session_factory)
    assert original_plan.action is not None
    if document == "event":
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=original_plan.fingerprint,
            applied_at=NOW,
        )
    with session_factory() as session:
        if document == "assembly":
            row = session.get(EntryStrategyAssembly, 2)
            row.evidence_json = row.evidence_json[:-1] + ',"chat_id":-1001}'
        elif document == "binding":
            row = session.get(ExecutionBinding, 266)
            value = json.loads(row.payload_json)["draft"]
            row.payload_json = (
                row.payload_json[:-1]
                + ',"draft":'
                + _canonical_json(value)
                + "}"
            )
        elif document == "signal":
            row = session.get(TradeSignal, 398)
            value = json.loads(row.payload_json)["deepcoin_order_draft"]
            row.payload_json = (
                row.payload_json[:-1]
                + ',"deepcoin_order_draft":'
                + _canonical_json(value)
                + "}"
            )
        elif document == "request":
            row = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
            row.request_json = row.request_json[:-1] + ',"side":"buy"}'
        else:
            row = session.query(ExecutionEvent).one()
            row.before_json = row.before_json[:-1] + ',"assembly_id":2}'
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    with pytest.raises(RuntimeError, match="repair_plan_not_actionable"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=original_plan.fingerprint,
            applied_at=NOW,
        )
    with session_factory() as session:
        expected_events = 1 if document == "event" else 0
        assert session.query(ExecutionEvent).count() == expected_events


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_binding_payload_blocks_plan_and_apply(tmp_path, constant):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.payload_json = (
            binding.payload_json[:-1] + f',"ignored":{constant}' + "}"
        )
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "binding_payload_invalid" in plan.conflicts
    with pytest.raises(RuntimeError, match="repair_plan_not_actionable"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=plan.fingerprint,
            applied_at=NOW,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_conflict"),
    [
        ("snapshot_field", "assembly_legacy_snapshot_binding_mismatch"),
        ("signal_top_present", "trade_signal_evidence_mismatch"),
        ("signal_full_draft", "trade_signal_evidence_mismatch"),
        ("binding_old_fingerprint", "binding_old_fingerprint_not_derivable"),
        ("missing_execution_leg", "execution_leg_identity_mismatch"),
        ("empty_order_id", "execution_leg_identity_mismatch"),
        ("unsubmitted_status", "execution_leg_identity_mismatch"),
    ],
)
def test_legacy_finalized_proof_rejects_any_unbound_or_unsubmitted_state(
    tmp_path, mutation, expected_conflict
):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        binding = session.get(ExecutionBinding, 266)
        signal = session.get(TradeSignal, 398)
        if mutation == "snapshot_field":
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["risk_budget_usdt"] = 11
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation == "signal_top_present":
            payload = json.loads(signal.payload_json)
            payload["entry_preamble_assembly"] = deepcopy(
                payload["deepcoin_order_draft"]["entry_preamble_assembly"]
            )
            signal.payload_json = _canonical_json(payload)
        elif mutation == "signal_full_draft":
            payload = json.loads(signal.payload_json)
            payload["deepcoin_order_draft"]["margin_mode"] = "isolated"
            signal.payload_json = _canonical_json(payload)
        elif mutation == "binding_old_fingerprint":
            payload = json.loads(binding.payload_json)
            payload["draft"]["entry_preamble_assembly"][
                "assembly_fingerprint"
            ] = "f" * 64
            binding.payload_json = _canonical_json(payload)
        elif mutation == "missing_execution_leg":
            session.delete(
                session.query(ExecutionOrderLeg).filter_by(leg_index=2).one()
            )
        else:
            leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
            if mutation == "empty_order_id":
                leg.order_id = ""
            else:
                leg.status = "planned"
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert expected_conflict in plan.conflicts


def test_monitor_accepts_legacy_reconciliation_only_after_exact_event(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )
    plan = _plan(session_factory)
    assert plan.action is not None

    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_event_preserves_initial_observed_state(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)

    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )

    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
        expected = {
            "binding": {
                "last_exchange_status": "entry_order_pending",
                "pos_id": None,
                "status": "open",
            },
            "entry_legs": [
                {"leg_index": 1, "pos_id": None, "status": "pending"},
                {"leg_index": 2, "pos_id": None, "status": "pending"},
            ],
        }
        assert json.loads(event.before_json)["observed_initial_state"] == expected
        assert json.loads(event.after_json)["observed_initial_state"] == expected
        initial_identity = json.loads(event.after_json)[
            "initial_submission_identity"
        ]
        assert initial_identity["order_id"] == "order-1,order-2"
        assert initial_identity["client_order_id"] == "entry-1,entry-2"
        assert set(initial_identity) == {
            "binding_payload_fingerprint",
            "client_order_id",
            "execution_leg_submissions_fingerprint",
            "order_id",
        }


@pytest.mark.parametrize("state", ["active", "partially_filled", "closed"])
def test_legacy_reconciliation_survives_lawful_binding_lifecycle(
    tmp_path, state
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    original_repair_fingerprint = plan.action.repair_fingerprint
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        if state == "partially_filled":
            _transition_legacy_binding_lifecycle(session, "active")
            binding = session.get(ExecutionBinding, 266)
            binding.pos_id = "pos-1"
            second_leg = session.query(ExecutionOrderLeg).filter_by(
                leg_index=2
            ).one()
            second_leg.status = "pending"
            second_leg.pos_id = None
            second_leg.attribution_status = "pending"
            session.query(ExecutionOrderLeg).filter_by(
                leg_index=1
            ).one().status = "partially_filled"
        else:
            _transition_legacy_binding_lifecycle(session, state)
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()
    with session_factory() as session:
        assert (
            session.query(ExecutionEvent).one().notification_fingerprint
            == original_repair_fingerprint
        )


@pytest.mark.parametrize(
    "state",
    [
        "management_selected_close_confirmed",
        "management_full_close_confirmed",
        "management_full_close_with_deferred_cancel",
        "pending_entry_leg_cancelled",
    ],
)
def test_legacy_reconciliation_survives_lawful_management_lifecycle(
    tmp_path, state
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        if state == "management_selected_close_confirmed":
            _transition_legacy_binding_lifecycle(session, "active")
            legs[0].status = "closed"
            legs[0].terminal_reason = "management_full_close_confirmed"
            binding.pos_id = "pos-2"
            binding.last_exchange_status = state
        elif state in {
            "management_full_close_confirmed",
            "management_full_close_with_deferred_cancel",
        }:
            _transition_legacy_binding_lifecycle(session, "active")
            for leg in legs:
                leg.status = "closed"
                leg.terminal_reason = "management_full_close_confirmed"
            if state == "management_full_close_with_deferred_cancel":
                legs[1].status = "cancelled"
                legs[1].terminal_reason = (
                    "management_full_close_cancelled_unfilled_entry_leg"
                )
                legs[1].pos_id = None
                legs[1].attribution_status = "pending"
            binding.status = "closed"
            binding.pos_id = None
            binding.last_exchange_status = "management_full_close_confirmed"
        else:
            legs[0].status = "cancelled"
            legs[0].terminal_reason = "operator_cancelled_unfilled_entry_leg"
            binding.order_id = "order-2"
            binding.client_order_id = "entry-2"
            binding.last_exchange_status = state
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


@pytest.mark.parametrize("state", ["active", "closed"])
def test_legacy_reconciliation_survives_cancel_then_lifecycle_transition(
    tmp_path, state
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        legs[0].status = "cancelled"
        legs[0].terminal_reason = "operator_cancelled_unfilled_entry_leg"
        binding.order_id = "order-2"
        binding.client_order_id = "entry-2"
        if state == "active":
            legs[1].status = "active"
            legs[1].pos_id = "pos-2"
            legs[1].attribution_status = "verified"
            binding.status = "active"
            binding.pos_id = "pos-2"
            binding.last_exchange_status = "position_ownership_verified"
        else:
            legs[1].status = "expired"
            legs[1].terminal_reason = "entry_order_expired"
            binding.status = "closed"
            binding.pos_id = None
            binding.last_exchange_status = "entry_legs_terminal"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_survives_cancel_then_management_full_close(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        legs[0].status = "cancelled"
        legs[0].terminal_reason = "operator_cancelled_unfilled_entry_leg"
        legs[1].status = "closed"
        legs[1].terminal_reason = "management_full_close_confirmed"
        legs[1].pos_id = "pos-2"
        legs[1].attribution_status = "verified"
        binding.order_id = "order-2"
        binding.client_order_id = "entry-2"
        binding.status = "closed"
        binding.pos_id = None
        binding.last_exchange_status = "management_full_close_confirmed"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_survives_management_subset_close(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        legs[0].status = "closed"
        legs[0].terminal_reason = "management_full_close_confirmed"
        binding.pos_id = "pos-2"
        binding.last_exchange_status = "management_subset_close_confirmed"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


@pytest.mark.parametrize("mutation", ["no_managed_close", "wrong_survivor_pos"])
def test_legacy_reconciliation_rejects_corrupt_management_subset_close(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        if mutation == "no_managed_close":
            legs[0].status = "cancelled"
            legs[0].pos_id = None
            legs[0].terminal_reason = "operator_cancelled_unfilled_entry_leg"
            binding.order_id = "order-2"
            binding.client_order_id = "entry-2"
        else:
            legs[0].status = "closed"
            legs[0].terminal_reason = "management_full_close_confirmed"
            binding.pos_id = "pos-1,pos-2"
        binding.last_exchange_status = "management_subset_close_confirmed"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("action", ["adjust_position_tpsl", "set_position_tpsl"])
@pytest.mark.parametrize("with_event", [True, False])
def test_legacy_reconciliation_requires_exact_tpsl_adjustment_event(
    tmp_path, with_event, action
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        binding = session.get(ExecutionBinding, 266)
        binding.last_exchange_status = "position_tpsl_adjusted"
        if with_event:
            _add_legacy_action_event(
                session,
                action=action,
                order_id="tpsl-1",
                pos_id="pos-1",
                before=(
                    {"rows": [{"ordId": "old-tpsl"}]}
                    if action == "adjust_position_tpsl"
                    else None
                ),
                after={"stop_loss": "63100"},
                request={
                    "rows": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "slTriggerPx": "63100",
                        }
                    ]
                },
                response={
                    "rows": [
                        {"code": "0", "data": {"ordId": "tpsl-1"}}
                    ]
                },
            )
        session.commit()

    expected = () if with_event else ("live_entry_preamble_binding_evidence_missing",)
    assert read_entry_preamble_invariants(database_path, now=NOW) == expected


def test_legacy_reconciliation_rejects_tpsl_event_for_wrong_instrument(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        session.get(ExecutionBinding, 266).last_exchange_status = (
            "position_tpsl_adjusted"
        )
        _add_legacy_action_event(
            session,
            action="adjust_position_tpsl",
            order_id="tpsl-1",
            pos_id="pos-1",
            before={"rows": [{"ordId": "old-tpsl"}]},
            after={"stop_loss": "63100"},
            request={
                "rows": [
                    {"instId": "ETH-USDT-SWAP", "slTriggerPx": "63100"}
                ]
            },
            response={
                "rows": [{"code": "0", "data": {"ordId": "tpsl-1"}}]
            },
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize(
    ("order_id", "response", "after"),
    [
        ("forged-tpsl", {"rows": [{"code": "0", "data": {"ordId": "tpsl-1"}}]}, {"stop_loss": "63100"}),
        ("tpsl-1", {"rows": [{"code": "1", "data": {"ordId": "tpsl-1"}}]}, {"stop_loss": "63100"}),
        ("tpsl-1", {"rows": [{"code": "0", "data": {"ordId": "tpsl-1"}}]}, {"take_profit": "99999"}),
    ],
)
def test_legacy_reconciliation_rejects_forged_tpsl_event_proof(
    tmp_path, order_id, response, after
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        session.get(ExecutionBinding, 266).last_exchange_status = (
            "position_tpsl_adjusted"
        )
        _add_legacy_action_event(
            session,
            action="adjust_position_tpsl",
            order_id=order_id,
            pos_id="pos-1",
            before={"stop_loss": "63000"},
            after=after,
            request={
                "rows": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "slTriggerPx": "63100",
                    }
                ]
            },
            response=response,
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("leg_status", ["cancelled", "exchange_cancelled"])
@pytest.mark.parametrize("action", ["cancel_trigger_entry", "cancel_regular_entry"])
def test_legacy_reconciliation_accepts_exact_full_entry_cancel_events(
    tmp_path, action, leg_status
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = action
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = leg_status
            leg.terminal_reason = (
                action if leg_status == "exchange_cancelled" else None
            )
            request = {"instId": "BTC-USDT-SWAP", "ordId": leg.order_id}
            request["clOrdId"] = leg.client_order_id
            if action == "cancel_regular_entry":
                request["mrgPosition"] = "split"
            _add_legacy_action_event(
                session,
                action=action,
                order_id=leg.order_id,
                client_order_id=leg.client_order_id,
                before={
                    "ordId": leg.order_id,
                    "clOrdId": leg.client_order_id,
                },
                request=request,
                response={"code": "0", "data": {"ordId": leg.order_id}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_rejects_forged_full_entry_cancel_state(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = "cancel_trigger_entry"
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        for leg in legs:
            leg.status = "cancelled"
        _add_legacy_action_event(
            session,
            action="cancel_trigger_entry",
            order_id=legs[0].order_id,
            client_order_id=legs[0].client_order_id,
            before={
                "ordId": legs[0].order_id,
                "clOrdId": legs[0].client_order_id,
            },
            request={
                "instId": "BTC-USDT-SWAP",
                "ordId": legs[0].order_id,
                "clOrdId": legs[0].client_order_id,
            },
            response={"code": "0", "data": {"ordId": legs[0].order_id}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_accepts_order_id_only_full_cancel_events(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = "cancel_trigger_entry"
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "cancelled"
            _add_legacy_action_event(
                session,
                action="cancel_trigger_entry",
                order_id=leg.order_id,
                before={"ordId": leg.order_id},
                request={"instId": "BTC-USDT-SWAP", "ordId": leg.order_id},
                response={"code": "0", "data": {"ordId": leg.order_id}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


@pytest.mark.parametrize("mutation", ["wrong_client", "failed_response", "wrong_response_order"])
def test_legacy_reconciliation_rejects_forged_full_cancel_event_proof(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = "cancel_trigger_entry"
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "cancelled"
            request_client_id = (
                "forged-client" if mutation == "wrong_client" else leg.client_order_id
            )
            response_code = "1" if mutation == "failed_response" else "0"
            response_order_id = (
                "forged-order" if mutation == "wrong_response_order" else leg.order_id
            )
            _add_legacy_action_event(
                session,
                action="cancel_trigger_entry",
                order_id=leg.order_id,
                client_order_id=leg.client_order_id,
                before={
                    "ordId": leg.order_id,
                    "clOrdId": leg.client_order_id,
                },
                request={
                    "instId": "BTC-USDT-SWAP",
                    "ordId": leg.order_id,
                    "clOrdId": request_client_id,
                },
                response={
                    "code": response_code,
                    "data": {"ordId": response_order_id},
                },
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("binding_status", ["open", "active"])
def test_legacy_reconciliation_accepts_exact_trigger_entry_recreation(
    tmp_path, binding_status
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        recreated = legs[0]
        recreated.order_id = "new-order-1"
        recreated.client_order_id = "new-client-1"
        recreated.status = "open"
        binding.order_id = "new-order-1"
        binding.client_order_id = "new-client-1"
        binding.status = binding_status
        binding.last_exchange_status = "trigger_entry_recreated"
        if binding_status == "active":
            legs[1].status = "active"
            legs[1].pos_id = "pos-2"
            legs[1].attribution_status = "verified"
            binding.pos_id = "pos-2"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={"stop_loss": "63000"},
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_rejects_forged_trigger_entry_recreation(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        leg.order_id = binding.order_id = "new-order-1"
        leg.client_order_id = binding.client_order_id = "new-client-1"
        leg.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="wrong-old-order",
            before={"stop_loss": "63000"},
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("mutation", ["wrong_before_order", "failed_response"])
def test_legacy_reconciliation_rejects_forged_trigger_recreation_event_proof(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        leg.order_id = binding.order_id = "new-order-1"
        leg.client_order_id = binding.client_order_id = "new-client-1"
        leg.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before=(
                {"rows": [{"ordId": "order-1"}]}
                if mutation == "wrong_before_order"
                else {"stop_loss": "63000"}
            ),
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={
                "code": "1" if mutation == "failed_response" else "0",
                "data": {"ordId": "new-order-1"},
            },
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_accepts_chained_trigger_entry_recreation(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        leg.order_id = binding.order_id = "new-order-2"
        leg.client_order_id = binding.client_order_id = "new-client-2"
        leg.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        for old_order, new_order, new_client in (
            ("order-1", "new-order-1", "new-client-1"),
            ("new-order-1", "new-order-2", "new-client-2"),
        ):
            _add_legacy_action_event(
                session,
                action="recreate_trigger_entry",
                order_id=new_order,
                related_order_id=old_order,
                before={"stop_loss": "63000"},
                after={"stop_loss": "63100"},
                request={
                    "instId": "BTC-USDT-SWAP",
                    "clOrdId": new_client,
                    "slTriggerPx": "63100",
                },
                response={"code": "0", "data": {"ordId": new_order}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


@pytest.mark.parametrize("with_tpsl", [False, True])
def test_legacy_reconciliation_composes_recreation_with_filled_lifecycle(
    tmp_path, with_tpsl
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "active"
        recreated.pos_id = binding.pos_id = "pos-1"
        recreated.attribution_status = "verified"
        binding.status = "active"
        binding.last_exchange_status = (
            "position_tpsl_adjusted"
            if with_tpsl
            else "position_ownership_verified"
        )
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={"stop_loss": "63000"},
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        if with_tpsl:
            _add_legacy_action_event(
                session,
                action="adjust_position_tpsl",
                order_id="tpsl-1",
                pos_id="pos-1",
                before={"stop_loss": "63100"},
                after={"stop_loss": "63200"},
                request={
                    "rows": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "slTriggerPx": "63200",
                        }
                    ]
                },
                response={
                    "rows": [
                        {"code": "0", "data": {"ordId": "tpsl-1"}}
                    ]
                },
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_rejects_forged_recreation_fill_lineage(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "forged-current-order"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "active"
        recreated.pos_id = binding.pos_id = "pos-1"
        recreated.attribution_status = "verified"
        binding.status = "active"
        binding.last_exchange_status = "position_ownership_verified"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={"stop_loss": "63000"},
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("recreated_leg_status", ["open", "pending"])
def test_legacy_reconciliation_validates_recreation_pending_reconciliation(
    tmp_path, recreated_leg_status
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = recreated_leg_status
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={"stop_loss": "63000"},
            after={"stop_loss": "63100"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    expected = (
        ()
        if recreated_leg_status == "open"
        else ("live_entry_preamble_binding_evidence_missing",)
    )
    assert read_entry_preamble_invariants(database_path, now=NOW) == expected


@pytest.mark.parametrize("before", [{}, {"stop_loss": "63000"}])
def test_legacy_reconciliation_accepts_recreation_with_writer_price_snapshot(
    tmp_path, before
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before=before,
            after={"stop_loss": "63100", "take_profit": "66000"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": "63100",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_rejects_forged_recreation_take_profit(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={"take_profit": "66000"},
            after={"take_profit": "65000"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_rejects_recreation_request_with_deferred_tp(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={},
            after={"take_profit": "66000"},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "tpTriggerPx": "66000",
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("forged_price", [{"x": 1}, [63100], True, -1, "bad"])
def test_legacy_reconciliation_rejects_non_scalar_recreation_price(
    tmp_path, forged_price
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        recreated = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        recreated.order_id = binding.order_id = "new-order-1"
        recreated.client_order_id = binding.client_order_id = "new-client-1"
        recreated.status = "open"
        binding.last_exchange_status = "trigger_entry_recreated"
        _add_legacy_action_event(
            session,
            action="recreate_trigger_entry",
            order_id="new-order-1",
            related_order_id="order-1",
            before={},
            after={"stop_loss": forged_price},
            request={
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "new-client-1",
                "slTriggerPx": forged_price,
            },
            response={"code": "0", "data": {"ordId": "new-order-1"}},
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_accepts_all_pending_legs_operator_cancelled(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.order_id = None
        binding.client_order_id = None
        binding.last_exchange_status = "pending_entry_leg_cancelled"
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "cancelled"
            leg.terminal_reason = "operator_cancelled_unfilled_entry_leg"
            _add_legacy_action_event(
                session,
                action="cancel_trigger_entry",
                order_id=leg.order_id,
                client_order_id=leg.client_order_id,
                before={"algoId": leg.order_id, "clOrdId": leg.client_order_id},
                request={
                    "instId": "BTC-USDT-SWAP",
                    "ordId": leg.order_id,
                    "clOrdId": leg.client_order_id,
                },
                response={"code": "0", "data": {"ordId": leg.order_id}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


@pytest.mark.parametrize("mutation", [None, "missing_event", "wrong_identity"])
def test_legacy_reconciliation_validates_all_absent_entry_recovery(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.order_id = None
        binding.client_order_id = None
        binding.last_exchange_status = "pending_entry_leg_cancelled"
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        for index, leg in enumerate(legs):
            leg.status = "cancelled"
            leg.terminal_reason = "pending_entry_order_absent_confirmed"
            if mutation == "missing_event" and index == 1:
                continue
            _add_legacy_action_event(
                session,
                action="cancel_entry_absent_confirmed",
                order_id=(
                    "forged-order"
                    if mutation == "wrong_identity" and index == 0
                    else leg.order_id
                ),
                client_order_id=leg.client_order_id,
            )
        session.commit()

    expected = (
        ()
        if mutation is None
        else ("live_entry_preamble_binding_evidence_missing",)
    )
    assert read_entry_preamble_invariants(database_path, now=NOW) == expected


@pytest.mark.parametrize("order_alias", ["algoId", "orderId", "order_id"])
def test_legacy_reconciliation_accepts_direct_cancel_exchange_order_alias(
    tmp_path, order_alias
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = "cancel_trigger_entry"
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "cancelled"
            _add_legacy_action_event(
                session,
                action="cancel_trigger_entry",
                order_id=leg.order_id,
                before={order_alias: leg.order_id},
                request={"instId": "BTC-USDT-SWAP", "ordId": leg.order_id},
                response={"code": "0", "data": {"ordId": leg.order_id}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == ()


def test_legacy_reconciliation_rejects_conflicting_cancel_order_aliases(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.status = "cancelled"
        binding.last_exchange_status = "cancel_trigger_entry"
        for leg in session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ):
            leg.status = "cancelled"
            _add_legacy_action_event(
                session,
                action="cancel_trigger_entry",
                order_id=leg.order_id,
                before={"algoId": leg.order_id, "orderId": "forged-order"},
                request={"instId": "BTC-USDT-SWAP", "ordId": leg.order_id},
                response={"code": "0", "data": {"ordId": leg.order_id}},
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_rejects_unjustified_reduced_identity_after_cancel(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        legs[0].status = "cancelled"
        legs[0].terminal_reason = "operator_cancelled_unfilled_entry_leg"
        legs[1].status = "active"
        legs[1].pos_id = "pos-2"
        legs[1].attribution_status = "verified"
        binding.order_id = "order-1"
        binding.client_order_id = "entry-1"
        binding.status = "active"
        binding.pos_id = "pos-2"
        binding.last_exchange_status = "position_ownership_verified"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "selected_close_wrong_pos",
        "selected_close_cancelled_leg",
        "selected_close_missing_managed_pos",
        "selected_close_survivor_bad_reason",
        "full_close_expired_legs",
        "full_close_missing_pos",
        "full_close_unverified_leg",
        "cancelled_leg_still_in_top_identity",
        "cancelled_leg_has_pos",
        "cancelled_leg_bad_reason",
    ],
)
def test_legacy_reconciliation_rejects_corrupt_management_lifecycle(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        legs = session.query(ExecutionOrderLeg).order_by(
            ExecutionOrderLeg.leg_index
        ).all()
        if mutation in {
            "selected_close_wrong_pos",
            "selected_close_cancelled_leg",
            "selected_close_missing_managed_pos",
            "selected_close_survivor_bad_reason",
        }:
            _transition_legacy_binding_lifecycle(session, "active")
            legs[0].status = (
                "cancelled"
                if mutation == "selected_close_cancelled_leg"
                else "closed"
            )
            legs[0].terminal_reason = (
                "operator_cancelled_unfilled_entry_leg"
                if mutation == "selected_close_cancelled_leg"
                else "management_full_close_confirmed"
            )
            binding.last_exchange_status = "management_selected_close_confirmed"
            if mutation == "selected_close_cancelled_leg":
                binding.pos_id = "pos-2"
            elif mutation == "selected_close_missing_managed_pos":
                legs[0].pos_id = None
                binding.pos_id = "pos-2"
            elif mutation == "selected_close_survivor_bad_reason":
                legs[1].terminal_reason = "unexpected"
                binding.pos_id = "pos-2"
        elif mutation in {
            "full_close_expired_legs",
            "full_close_missing_pos",
            "full_close_unverified_leg",
        }:
            for leg in legs:
                leg.status = (
                    "expired"
                    if mutation == "full_close_expired_legs"
                    else "closed"
                )
                leg.terminal_reason = "management_full_close_confirmed"
                leg.pos_id = f"pos-{leg.leg_index}"
                leg.attribution_status = "verified"
            if mutation == "full_close_unverified_leg":
                legs[0].attribution_status = "pending"
            elif mutation == "full_close_missing_pos":
                legs[0].pos_id = None
            binding.status = "closed"
            binding.last_exchange_status = "management_full_close_confirmed"
        else:
            legs[0].status = "cancelled"
            legs[0].terminal_reason = (
                "unexpected"
                if mutation == "cancelled_leg_bad_reason"
                else "operator_cancelled_unfilled_entry_leg"
            )
            if mutation == "cancelled_leg_has_pos":
                legs[0].pos_id = "pos-cancelled"
            if mutation == "cancelled_leg_still_in_top_identity":
                binding.order_id = "order-1"
                binding.client_order_id = "entry-1"
            else:
                binding.order_id = "order-2"
                binding.client_order_id = "entry-2"
            binding.last_exchange_status = "pending_entry_leg_cancelled"
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_rejects_immutable_drift_after_lifecycle_change(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        session.query(ExecutionOrderLeg).filter_by(leg_index=1).one().order_id = (
            "drifted-order"
        )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_reconciliation_fingerprint_binds_full_submitted_order_payload(
    tmp_path,
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        payload = json.loads(binding.payload_json)
        payload["submitted_orders"][0]["response"]["data"]["tag"] = "drift"
        binding.payload_json = _canonical_json(payload)
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("mutation", ["missing_binding_pos", "planned_live_leg"])
def test_legacy_reconciliation_rejects_illegal_lifecycle_state_combination(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        _transition_legacy_binding_lifecycle(session, "active")
        if mutation == "missing_binding_pos":
            session.get(ExecutionBinding, 266).pos_id = None
        else:
            session.query(ExecutionOrderLeg).filter_by(leg_index=1).one().status = (
                "planned"
            )
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "signal_top",
        "request",
        "leg_status",
        "source_lineage",
        "event_policy",
        "legacy_schema_extra",
        "binding_wrapper_extra",
        "signal_wrapper_extra",
        "submitted_order_id",
    ],
)
def test_monitor_rejects_legacy_event_when_durable_proof_drifts(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        if mutation == "signal_top":
            signal = session.get(TradeSignal, 398)
            payload = json.loads(signal.payload_json)
            payload["entry_preamble_assembly"] = deepcopy(
                payload["deepcoin_order_draft"]["entry_preamble_assembly"]
            )
            signal.payload_json = _canonical_json(payload)
        elif mutation == "request":
            leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
            request = json.loads(leg.request_json)
            request["side"] = "sell"
            leg.request_json = _canonical_json(request)
        elif mutation == "leg_status":
            session.query(ExecutionOrderLeg).filter_by(leg_index=1).one().status = (
                "planned"
            )
        elif mutation == "source_lineage":
            session.get(RawMessage, 10).message_id = 56
        elif mutation == "legacy_schema_extra":
            binding = session.get(ExecutionBinding, 266)
            signal = session.get(TradeSignal, 398)
            binding_payload = json.loads(binding.payload_json)
            signal_payload = json.loads(signal.payload_json)
            binding_payload["draft"]["unexpected"] = True
            signal_payload["deepcoin_order_draft"]["unexpected"] = True
            binding.payload_json = _canonical_json(binding_payload)
            signal.payload_json = _canonical_json(signal_payload)
        elif mutation == "binding_wrapper_extra":
            binding = session.get(ExecutionBinding, 266)
            payload = json.loads(binding.payload_json)
            payload["unexpected"] = True
            binding.payload_json = _canonical_json(payload)
        elif mutation == "signal_wrapper_extra":
            signal = session.get(TradeSignal, 398)
            payload = json.loads(signal.payload_json)
            payload["unexpected"] = True
            signal.payload_json = _canonical_json(payload)
        elif mutation == "submitted_order_id":
            binding = session.get(ExecutionBinding, 266)
            payload = json.loads(binding.payload_json)
            payload["submitted_orders"][0]["order_id"] = "other-order"
            binding.payload_json = _canonical_json(payload)
        else:
            event_row = session.query(ExecutionEvent).one()
            after = json.loads(event_row.after_json)
            after["policy_version"] = RECONCILIATION_POLICY
            event_row.after_json = _canonical_json(after)
        session.commit()

    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize("document", ["assembly", "binding", "signal", "event"])
def test_monitor_and_planner_reject_exponent_overflow_reconciliation_json(
    tmp_path, document
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        if document == "assembly":
            row = session.get(EntryStrategyAssembly, 2)
            row.evidence_json = row.evidence_json[:-1] + ',"overflow":1e999}'
        elif document == "binding":
            row = session.get(ExecutionBinding, 266)
            row.payload_json = row.payload_json[:-1] + ',"overflow":1e999}'
        elif document == "signal":
            row = session.get(TradeSignal, 398)
            row.payload_json = row.payload_json[:-1] + ',"overflow":-1e999}'
        else:
            row = session.query(ExecutionEvent).one()
            row.after_json = row.after_json[:-1] + ',"overflow":1e999}'
        session.commit()

    assert _plan(session_factory).action is None
    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


def test_legacy_monitor_rejects_unreviewable_signal_candidate(tmp_path):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        session.get(SignalCandidate, 20).review_status = "rejected"
        session.commit()

    assert _plan(session_factory).action is None
    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "order_id",
        "client_order_id",
        "pos_id",
        "status",
        "last_exchange_status",
        "coordinated_order_id",
    ],
)
def test_valid_legacy_event_does_not_hide_binding_top_identity_drift(
    tmp_path, mutation
):
    database_path, session_factory = _seed_case(tmp_path)
    _convert_to_production_legacy_finalized_case(session_factory)
    plan = _plan(session_factory)
    apply_entry_assembly_fingerprint_repair_plan(
        session_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        if mutation == "coordinated_order_id":
            payload = json.loads(binding.payload_json)
            legs = session.query(ExecutionOrderLeg).order_by(
                ExecutionOrderLeg.leg_index
            )
            forged = ["forged-1", "forged-2"]
            binding.order_id = ",".join(forged)
            for index, (leg, order_id) in enumerate(zip(legs, forged)):
                leg.order_id = order_id
                payload["submitted_orders"][index]["order_id"] = order_id
                payload["submitted_orders"][index]["response"]["data"][
                    "ordId"
                ] = order_id
            binding.payload_json = _canonical_json(payload)
        else:
            setattr(
                binding,
                mutation,
                {
                    "order_id": "other-order,order-2",
                    "client_order_id": "other-client,entry-2",
                    "pos_id": "pos-1",
                    "status": "active",
                    "last_exchange_status": "submitted",
                }[mutation],
            )
        session.commit()

    assert _plan(session_factory).action is None
    assert read_entry_preamble_invariants(database_path, now=NOW) == (
        "live_entry_preamble_binding_evidence_missing",
    )


@pytest.mark.parametrize(
    ("mutation", "expected_conflict"),
    [
        ("wrong_strategy", "binding_strategy_mismatch"),
        ("old_not_derivable", "binding_old_fingerprint_not_derivable"),
        ("draft_identity", "binding_draft_identity_mismatch"),
        ("leg_identity", "execution_leg_identity_mismatch"),
        ("missing_finalization", "assembly_finalization_fields_missing"),
        ("binding_malformed_leg", "binding_draft_identity_mismatch"),
        ("binding_leg_count", "binding_draft_identity_mismatch"),
        ("signal_malformed_leg", "trade_signal_evidence_mismatch"),
        ("signal_leg_count", "trade_signal_evidence_mismatch"),
        ("snapshot_malformed_leg", "assembly_finalization_fields_missing"),
        ("snapshot_leg_count", "assembly_finalization_fields_missing"),
        (
            "strategy_raw_message_id_drift",
            "assembly_strategy_raw_message_identity_mismatch",
        ),
        (
            "strategy_raw_message_id_bool",
            "assembly_strategy_raw_message_identity_mismatch",
        ),
        (
            "strategy_raw_message_id_zero",
            "assembly_strategy_raw_message_identity_mismatch",
        ),
        (
            "strategy_raw_message_id_string",
            "assembly_strategy_raw_message_identity_mismatch",
        ),
        (
            "signal_candidate_id_drift",
            "assembly_signal_candidate_identity_mismatch",
        ),
        (
            "signal_candidate_id_bool",
            "assembly_signal_candidate_identity_mismatch",
        ),
        (
            "signal_candidate_id_zero",
            "assembly_signal_candidate_identity_mismatch",
        ),
        (
            "signal_candidate_id_string",
            "assembly_signal_candidate_identity_mismatch",
        ),
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
        elif mutation == "binding_malformed_leg":
            payload = json.loads(binding.payload_json)
            payload["draft"]["order_legs"].append("malformed")
            binding.payload_json = _canonical_json(payload)
        elif mutation == "binding_leg_count":
            payload = json.loads(binding.payload_json)
            payload["draft"]["order_legs"].pop()
            binding.payload_json = _canonical_json(payload)
        elif mutation == "signal_malformed_leg":
            signal = session.get(TradeSignal, 398)
            payload = json.loads(signal.payload_json)
            payload["deepcoin_order_draft"]["order_legs"].append("malformed")
            signal.payload_json = _canonical_json(payload)
        elif mutation == "signal_leg_count":
            signal = session.get(TradeSignal, 398)
            payload = json.loads(signal.payload_json)
            payload["deepcoin_order_draft"]["order_legs"].pop()
            signal.payload_json = _canonical_json(payload)
        elif mutation == "snapshot_malformed_leg":
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["order_legs"].append("malformed")
            evidence["final_entry_leg_count"] += 1
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation == "snapshot_leg_count":
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["order_legs"].pop()
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation.startswith("strategy_raw_message_id_"):
            evidence = json.loads(assembly.evidence_json)
            evidence["strategy_raw_message_id"] = {
                "bool": True,
                "zero": 0,
                "string": "10",
                "drift": 11,
            }[mutation.rsplit("_", 1)[-1]]
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation.startswith("signal_candidate_id_"):
            evidence = json.loads(assembly.evidence_json)
            evidence["signal_candidate_id"] = {
                "bool": True,
                "zero": 0,
                "string": "20",
                "drift": 21,
            }[mutation.rsplit("_", 1)[-1]]
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


@pytest.mark.parametrize(
    ("mutation", "expected_conflict"),
    [
        ("legacy_assembly", "assembly_not_v2"),
        ("empty_strategy", "assembly_strategy_identity_invalid"),
        ("snapshot_strategy", "assembly_snapshot_strategy_mismatch"),
        ("binding_evidence_assembly", "binding_old_fingerprint_not_derivable"),
        ("binding_evidence_strategy", "binding_old_fingerprint_not_derivable"),
        ("binding_venue", "binding_venue_mismatch"),
        ("signal_strategy", "trade_signal_identity_missing"),
        ("signal_kol", "trade_signal_identity_missing"),
        ("signal_venue", "trade_signal_identity_missing"),
        ("signal_source", "trade_signal_identity_missing"),
        ("signal_action", "trade_signal_identity_missing"),
        ("signal_status", "trade_signal_identity_missing"),
        ("signal_unprocessed", "trade_signal_identity_missing"),
        ("signal_evidence_assembly", "trade_signal_evidence_mismatch"),
        ("signal_evidence_strategy", "trade_signal_evidence_mismatch"),
        ("leg_strategy", "execution_leg_identity_mismatch"),
        ("leg_venue", "execution_leg_identity_mismatch"),
        ("leg_status", "execution_leg_identity_mismatch"),
        ("leg_order_id", "execution_leg_identity_mismatch"),
        ("leg_kind", "execution_leg_identity_mismatch"),
        ("request_order_type", "execution_leg_identity_mismatch"),
    ],
)
def test_build_plan_enforces_exact_submitted_v2_identity(
    tmp_path, mutation, expected_conflict
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        binding = session.get(ExecutionBinding, 266)
        signal = session.get(TradeSignal, 398)
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        if mutation == "legacy_assembly":
            assembly.entry_preamble_id = 999
        elif mutation == "empty_strategy":
            assembly.strategy_instance_id = ""
        elif mutation == "snapshot_strategy":
            evidence = json.loads(assembly.evidence_json)
            evidence["order_draft_snapshot"]["strategy_instance_id"] = "other"
            assembly.evidence_json = _canonical_json(evidence)
            assembly.fingerprint = canonical_fingerprint(evidence)
        elif mutation.startswith("binding_evidence_"):
            payload = json.loads(binding.payload_json)
            evidence = payload["draft"]["entry_preamble_assembly"]
            if mutation.endswith("assembly"):
                evidence["assembly_id"] = 3
            else:
                evidence["strategy_instance_id"] = "other"
            binding.payload_json = _canonical_json(payload)
        elif mutation == "binding_venue":
            binding.venue = "other"
        elif mutation == "signal_strategy":
            signal.strategy_instance_id = "other"
        elif mutation == "signal_kol":
            signal.kol_id = "other"
        elif mutation == "signal_venue":
            signal.venue = "other"
        elif mutation == "signal_source":
            signal.source_type = "manual"
        elif mutation == "signal_action":
            signal.action = "close_position"
        elif mutation == "signal_status":
            signal.status = "pending"
        elif mutation == "signal_unprocessed":
            signal.processed_at = None
        elif mutation.startswith("signal_evidence_"):
            payload = json.loads(signal.payload_json)
            for evidence in (
                payload["entry_preamble_assembly"],
                payload["deepcoin_order_draft"]["entry_preamble_assembly"],
            ):
                if mutation.endswith("assembly"):
                    evidence["assembly_id"] = 3
                else:
                    evidence["strategy_instance_id"] = "other"
            signal.payload_json = _canonical_json(payload)
        elif mutation == "leg_strategy":
            leg.strategy_instance_id = "other"
        elif mutation == "leg_venue":
            leg.venue = "other"
        elif mutation == "leg_status":
            leg.status = "submitting"
        elif mutation == "leg_order_id":
            leg.order_id = None
        elif mutation == "leg_kind":
            leg.order_kind = "market"
        elif mutation == "request_order_type":
            request = json.loads(leg.request_json)
            request["orderType"] = "market"
            leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert expected_conflict in plan.conflicts


@pytest.mark.parametrize(
    "mutation",
    [
        "margin_mode",
        "position_mode",
        "symbol",
        "source_kol_id",
        "source_kol_code",
        "source_chat_id",
        "source_message_id",
        "leg_side",
        "leg_position_side",
        "leg_base_asset_estimate",
        "leg_take_profit_leg",
        "selected_entry_leg_indices",
        "selected_entry_leg_count",
        "request_side",
        "request_margin_mode",
    ],
)
def test_build_plan_proves_complete_finalized_and_execution_snapshot(
    tmp_path, mutation
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        payload = json.loads(binding.payload_json)
        draft = payload["draft"]
        if mutation == "margin_mode":
            draft["margin_mode"] = "isolated"
        elif mutation == "position_mode":
            draft["position_mode"] = "merge"
        elif mutation == "symbol":
            draft["symbol"] = "ETH"
        elif mutation.startswith("source_"):
            field = mutation.removeprefix("source_")
            draft["source"][field] = {
                "kol_id": "other",
                "kol_code": "other",
                "chat_id": -1002,
                "message_id": 56,
            }[field]
        elif mutation == "leg_side":
            draft["order_legs"][0]["side"] = "sell"
        elif mutation == "leg_position_side":
            draft["order_legs"][0]["position_side"] = "short"
        elif mutation == "leg_base_asset_estimate":
            draft["order_legs"][0]["base_asset_estimate"] = 0.02
        elif mutation == "leg_take_profit_leg":
            draft["order_legs"][0]["take_profit_leg"]["price"] = 67000
        elif mutation == "selected_entry_leg_indices":
            draft["selected_entry_leg_indices"] = [2, 1]
        elif mutation == "selected_entry_leg_count":
            draft["selected_entry_leg_count"] = 1
        elif mutation.startswith("request_"):
            leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
            request = json.loads(leg.request_json)
            if mutation == "request_side":
                request["side"] = "sell"
            else:
                request["tdMode"] = "isolated"
            leg.request_json = _canonical_json(request)
        if not mutation.startswith("request_"):
            binding.payload_json = _canonical_json(payload)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert (
        "binding_draft_identity_mismatch" in plan.conflicts
        or "execution_leg_identity_mismatch" in plan.conflicts
    )


def test_build_plan_maps_durable_legs_through_selected_entry_indices(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, 2)
        evidence = json.loads(assembly.evidence_json)
        evidence["order_draft_snapshot"]["selected_entry_leg_indices"] = [2]
        evidence["order_draft_snapshot"]["selected_entry_leg_count"] = 1
        assembly.evidence_json = _canonical_json(evidence)
        assembly.fingerprint = canonical_fingerprint(evidence)
        binding = session.get(ExecutionBinding, 266)
        binding_payload = json.loads(binding.payload_json)
        binding_payload["draft"]["selected_entry_leg_indices"] = [2]
        binding_payload["draft"]["selected_entry_leg_count"] = 1
        binding.payload_json = _canonical_json(binding_payload)
        signal = session.get(TradeSignal, 398)
        signal_payload = json.loads(signal.payload_json)
        signal_payload["deepcoin_order_draft"]["selected_entry_leg_indices"] = [2]
        signal_payload["deepcoin_order_draft"]["selected_entry_leg_count"] = 1
        signal.payload_json = _canonical_json(signal_payload)
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all()
        session.delete(legs[1])
        legs[0].client_order_id = "entry-2"
        request = json.loads(legs[0].request_json)
        request.update(
            {
                "price": "63800",
                "triggerPrice": "63800",
                "sz": "5",
                "clOrdId": "entry-2",
            }
        )
        legs[0].request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.conflicts == ()
    assert plan.action is not None


@pytest.mark.parametrize(
    "mutation",
    [
        "margin_mode",
        "position_mode",
        "symbol",
        "source_kol_id",
        "source_kol_code",
        "source_chat_id",
        "source_message_id",
        "leg_side",
        "leg_position_side",
        "leg_base_asset_estimate",
        "leg_take_profit_leg",
        "selected_entry_leg_indices",
        "selected_entry_leg_count",
    ],
)
def test_build_plan_proves_complete_submitted_signal_snapshot(tmp_path, mutation):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        signal = session.get(TradeSignal, 398)
        payload = json.loads(signal.payload_json)
        draft = payload["deepcoin_order_draft"]
        if mutation == "margin_mode":
            draft["margin_mode"] = "isolated"
        elif mutation == "position_mode":
            draft["position_mode"] = "merge"
        elif mutation == "symbol":
            draft["symbol"] = "ETH"
        elif mutation.startswith("source_"):
            field = mutation.removeprefix("source_")
            draft["source"][field] = {
                "kol_id": "other",
                "kol_code": "other",
                "chat_id": -1002,
                "message_id": 56,
            }[field]
        elif mutation == "leg_side":
            draft["order_legs"][0]["side"] = "sell"
        elif mutation == "leg_position_side":
            draft["order_legs"][0]["position_side"] = "short"
        elif mutation == "leg_base_asset_estimate":
            draft["order_legs"][0]["base_asset_estimate"] = 0.02
        elif mutation == "leg_take_profit_leg":
            draft["order_legs"][0]["take_profit_leg"]["price"] = 67000
        elif mutation == "selected_entry_leg_indices":
            draft["selected_entry_leg_indices"] = [2, 1]
        elif mutation == "selected_entry_leg_count":
            draft["selected_entry_leg_count"] = 1
        signal.payload_json = _canonical_json(payload)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "trade_signal_evidence_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    "mutation",
    [
        "strategy",
        "symbol",
        "instrument",
        "source_kol_id",
        "source_chat_id",
        "source_message_id",
    ],
)
def test_build_plan_binds_coordinated_snapshots_to_durable_identity(
    tmp_path, mutation
):
    _, session_factory = _seed_case(tmp_path)

    def mutate(draft):
        if mutation == "strategy":
            draft["strategy_instance_id"] = "other-strategy"
        elif mutation == "symbol":
            draft["symbol"] = "ETH"
            draft["instrument_id"] = "ETH-USDT-SWAP"
        elif mutation == "instrument":
            draft["instrument_id"] = "ETH-USDT-SWAP"
        elif mutation.startswith("source_"):
            field = mutation.removeprefix("source_")
            draft["source"][field] = {
                "kol_id": "other",
                "chat_id": -1002,
                "message_id": 56,
            }[field]

    with session_factory() as session:
        _rewrite_all_draft_copies(session, mutate)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "assembly_snapshot_durable_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize("mutation", ["direction", "margin_mode", "position_mode"])
def test_build_plan_rejects_coordinated_request_identity_mutation(tmp_path, mutation):
    _, session_factory = _seed_case(tmp_path)

    def mutate(draft):
        if mutation == "direction":
            for leg in draft["order_legs"]:
                leg["side"] = "sell"
                leg["position_side"] = "short"
        elif mutation == "margin_mode":
            draft["margin_mode"] = "isolated"
        else:
            draft["position_mode"] = "merge"

    with session_factory() as session:
        _rewrite_all_draft_copies(session, mutate)
        for leg in session.query(ExecutionOrderLeg).all():
            request = json.loads(leg.request_json)
            if mutation == "direction":
                request["side"] = "sell"
                request["posSide"] = "short"
            elif mutation == "margin_mode":
                request["tdMode"] = "isolated"
            else:
                request["mrgPosition"] = "merge"
            leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "assembly_snapshot_durable_identity_mismatch" in plan.conflicts


def test_build_plan_accepts_production_shaped_market_request(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        _convert_first_selected_leg_to_market(session)
        session.commit()

    plan = _plan(session_factory)

    assert plan.conflicts == ()
    assert plan.action is not None


@pytest.mark.parametrize(
    "mutation",
    [
        "limit_trigger_price",
        "limit_position_mode_missing",
        "limit_market_ord_type",
        "limit_market_px",
        "market_price_present",
        "market_wrong_order_key",
        "market_trigger_order_type",
        "market_order_price",
    ],
)
def test_build_plan_rejects_request_schema_drift(tmp_path, mutation):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        if mutation.startswith("market_"):
            leg = _convert_first_selected_leg_to_market(session)
        else:
            leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        request = json.loads(leg.request_json)
        if mutation == "limit_trigger_price":
            request["triggerPrice"] = "63999"
        elif mutation == "limit_position_mode_missing":
            request.pop("mrgPosition")
        elif mutation == "limit_market_ord_type":
            request["ordType"] = "market"
        elif mutation == "limit_market_px":
            request["px"] = "64000"
        elif mutation == "market_price_present":
            request["price"] = "64000"
        elif mutation == "market_wrong_order_key":
            request.pop("ordType")
            request["orderType"] = "market"
        elif mutation == "market_trigger_order_type":
            request["orderType"] = "limit"
        else:
            request["orderPrice"] = "64000"
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "execution_leg_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    "key",
    [
        "instId",
        "productGroup",
        "sz",
        "side",
        "posSide",
        "price",
        "isCrossMargin",
        "orderType",
        "triggerPrice",
        "triggerPxType",
        "mrgPosition",
        "tdMode",
        "clOrdId",
        "slTriggerPx",
        "slTriggerPxType",
        "slOrdPx",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "drift"])
def test_build_plan_rejects_trigger_request_endpoint_field_mismatch(
    tmp_path, key, mutation
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        request = json.loads(leg.request_json)
        if mutation == "missing":
            request.pop(key)
        else:
            request[key] = f"{request[key]}-drift"
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "execution_leg_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    "key",
    [
        "instId",
        "tdMode",
        "side",
        "posSide",
        "ordType",
        "sz",
        "clOrdId",
        "mrgPosition",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "drift"])
def test_build_plan_rejects_market_request_endpoint_field_mismatch(
    tmp_path, key, mutation
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        leg = _convert_first_selected_leg_to_market(session)
        request = json.loads(leg.request_json)
        if mutation == "missing":
            request.pop(key)
        else:
            request[key] = f"{request[key]}-drift"
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "execution_leg_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    ("order_type", "key", "value"),
    [
        ("limit", "ordType", None),
        ("limit", "px", ""),
        ("market", "orderType", None),
        ("market", "triggerPrice", ""),
        ("market", "productGroup", None),
        ("market", "slTriggerPx", ""),
    ],
)
def test_build_plan_rejects_foreign_request_keys_even_when_empty(
    tmp_path, order_type, key, value
):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        leg = (
            _convert_first_selected_leg_to_market(session)
            if order_type == "market"
            else session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        )
        request = json.loads(leg.request_json)
        request[key] = value
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "execution_leg_identity_mismatch" in plan.conflicts


def test_build_plan_allows_documented_persistence_only_request_metadata(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        request = json.loads(leg.request_json)
        request["merged_from_leg_indices"] = [1, 2]
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.conflicts == ()
    assert plan.action is not None


def test_build_plan_rejects_invalid_persistence_only_request_metadata(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(leg_index=1).one()
        request = json.loads(leg.request_json)
        request["merged_from_leg_indices"] = None
        leg.request_json = _canonical_json(request)
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert "execution_leg_identity_mismatch" in plan.conflicts


@pytest.mark.parametrize(
    ("target", "raw"),
    [
        ("assembly", '{"bad":"\\ud800"}'),
        ("binding", '{"bad":"\\ud800"}'),
        ("assembly", "{malformed"),
        ("binding", "{malformed"),
    ],
)
def test_build_plan_returns_fixed_conflict_for_invalid_json(tmp_path, target, raw):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        if target == "assembly":
            session.get(EntryStrategyAssembly, 2).evidence_json = raw
            expected = "assembly_evidence_invalid"
        else:
            session.get(ExecutionBinding, 266).payload_json = raw
            expected = "binding_payload_invalid"
        session.commit()

    plan = _plan(session_factory)

    assert plan.action is None
    assert expected in plan.conflicts


@pytest.mark.parametrize(
    ("mutation", "expected_conflict"),
    [
        ("missing_raw", "strategy_raw_message_not_found"),
        ("missing_candidate", "signal_candidate_not_found"),
        ("candidate_raw", "signal_candidate_raw_message_mismatch"),
        ("raw_chat", "strategy_raw_message_source_mismatch"),
        ("raw_message", "strategy_raw_message_source_mismatch"),
        ("candidate_event", "signal_candidate_not_entry_strategy"),
        ("candidate_symbol", "signal_candidate_not_entry_strategy"),
        ("candidate_symbol_case", "signal_candidate_not_entry_strategy"),
        ("candidate_side", "signal_candidate_not_entry_strategy"),
        ("candidate_side_case", "signal_candidate_not_entry_strategy"),
        ("candidate_status", "signal_candidate_not_entry_strategy"),
    ],
)
def test_build_plan_proves_exact_source_lineage(tmp_path, mutation, expected_conflict):
    _, session_factory = _seed_case(tmp_path)
    with session_factory() as session:
        raw = session.get(RawMessage, 10)
        candidate = session.get(SignalCandidate, 20)
        if mutation == "missing_raw":
            session.delete(raw)
        elif mutation == "missing_candidate":
            session.delete(candidate)
        elif mutation == "candidate_raw":
            candidate.raw_message_id = 11
        elif mutation == "raw_chat":
            raw.chat_id = -1002
        elif mutation == "raw_message":
            raw.message_id = 56
        elif mutation == "candidate_event":
            candidate.event_type = "position_update"
        elif mutation == "candidate_symbol":
            candidate.symbol = "ETH"
        elif mutation == "candidate_symbol_case":
            candidate.symbol = "btc"
        elif mutation == "candidate_side":
            candidate.side = "short"
        elif mutation == "candidate_side_case":
            candidate.side = "LONG"
        elif mutation == "candidate_status":
            candidate.review_status = "rejected"
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
            "policy_version": "entry-assembly-fingerprint-reconciliation-v1",
        }
        assert event.reason == "pre_finalization_payload_preserved"
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
        "policy_version": "entry-assembly-fingerprint-reconciliation-v1",
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
                reason="pre_finalization_payload_preserved",
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


def test_apply_rebuilds_proof_inside_the_single_write_transaction(tmp_path):
    _, base_factory = _seed_case(tmp_path)
    plan = _plan(base_factory)

    class CountingSessionFactory:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return base_factory()

    counting_factory = CountingSessionFactory()
    event_id = apply_entry_assembly_fingerprint_repair_plan(
        counting_factory,
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )

    assert event_id > 0
    assert counting_factory.calls == 1


def test_apply_refuses_state_drift_since_operator_plan(tmp_path):
    _, session_factory = _seed_case(tmp_path)
    plan = _plan(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        payload = json.loads(binding.payload_json)
        payload["draft"]["order_legs"][0]["price"] = 1
        binding.payload_json = _canonical_json(payload)
        session.commit()

    with pytest.raises(RuntimeError, match="repair_plan_not_actionable"):
        apply_entry_assembly_fingerprint_repair_plan(
            session_factory,
            assembly_id=2,
            execution_binding_id=266,
            expected_plan_fingerprint=plan.fingerprint,
            applied_at=NOW,
        )
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0


def test_apply_sqlite_lock_prevents_concurrent_drift_between_proof_and_event(
    tmp_path,
):
    _, base_factory = _seed_case(tmp_path)
    plan = _plan(base_factory)
    drift_attempted = threading.Event()
    drift_committed = threading.Event()
    drift_finished = threading.Event()
    committed_before_event: list[bool] = []
    workers: list[threading.Thread] = []

    def drift_binding() -> None:
        try:
            with base_factory() as drift_session:
                binding = drift_session.get(ExecutionBinding, 266)
                payload = json.loads(binding.payload_json)
                payload["draft"]["order_legs"][0]["price"] = 1
                binding.payload_json = _canonical_json(payload)
                drift_attempted.set()
                drift_session.commit()
                drift_committed.set()
        finally:
            drift_finished.set()

    def before_flush(*_args) -> None:
        worker = threading.Thread(target=drift_binding, daemon=True)
        workers.append(worker)
        worker.start()
        assert drift_attempted.wait(2)
        committed_before_event.append(drift_committed.wait(0.25))

    class HookedSessionFactory:
        def __call__(self):
            session = base_factory()
            event.listen(session, "before_flush", before_flush, once=True)
            return session

    event_id = apply_entry_assembly_fingerprint_repair_plan(
        HookedSessionFactory(),
        assembly_id=2,
        execution_binding_id=266,
        expected_plan_fingerprint=plan.fingerprint,
        applied_at=NOW,
    )

    assert event_id > 0
    assert committed_before_event == [False]
    assert drift_finished.wait(5)
    assert drift_committed.is_set()
    for worker in workers:
        worker.join(timeout=1)
