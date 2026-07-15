from datetime import UTC, datetime
import json
from threading import Event, Thread

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_actions import DeepcoinExecutionActionError
from telegram_kol_research.deepcoin_execution_actions import adjust_position_tpsl
from telegram_kol_research.deepcoin_execution_actions import close_bound_position_market
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord
from telegram_kol_research.execution_bindings import list_execution_order_legs
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_bindings import upsert_execution_order_leg
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg, StrategyLifecycle
from telegram_kol_research.position_attribution_repair import (
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings


class _FakeDeepcoinClient:
    def __init__(self):
        self.positions = [
            {
                "posId": "pos-1",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "pos": "0.1",
                "cTime": "1000",
            }
        ]
        self.trigger_pending = [
            {
                "triggerOrderType": "TPSL",
                "ordId": "tp-old",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-1",
                "tpTriggerPx": "1605.6",
                "sz": "0.1",
                "cTime": "1000",
            },
            {
                "triggerOrderType": "TPSL",
                "ordId": "sl-old",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-1",
                "slTriggerPx": "1567.52",
                "sz": "0.1",
                "cTime": "1000",
            },
        ]
        self.open_orders = []
        self.cancel_trigger_payloads = []
        self.cancel_order_payloads = []
        self.protection_payloads = []
        self.order_payloads = []
        self.trigger_payloads = []

    def list_positions(self, *, inst_id=None):
        return self.positions

    def list_trigger_orders_pending(self, *, inst_id):
        return self.trigger_pending

    def list_open_orders(self, *, inst_id=None):
        return self.open_orders

    def cancel_trigger_order(self, cancel_payload):
        self.cancel_trigger_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def cancel_order(self, cancel_payload):
        self.cancel_order_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"ordId": "tpsl-new"}}

    def place_order(self, order_payload):
        self.order_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "close-1"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "trigger-new"}}


def _binding(session_factory, **overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "ETH",
        "side": "long",
        "order_id": "entry-1",
        "client_order_id": "client-1",
        "pos_id": "pos-1",
        "status": "active",
        "strategy_instance_id": "deepcoin:100:55:ETH:long",
    }
    values.update(overrides)
    binding_id = upsert_execution_binding(session_factory, ExecutionBindingRecord(**values))
    pos_ids = [item.strip() for item in str(values.get("pos_id") or "").split(",") if item.strip()]
    order_ids = [
        item.strip() for item in str(values.get("order_id") or "").split(",") if item.strip()
    ]
    client_order_ids = [
        item.strip()
        for item in str(values.get("client_order_id") or "").split(",")
        if item.strip()
    ]
    for leg_index, pos_id in enumerate(pos_ids, start=1):
        upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                strategy_instance_id=values.get("strategy_instance_id"),
                leg_index=leg_index,
                purpose="entry",
                order_kind="market",
                order_id=order_ids[leg_index - 1] if leg_index <= len(order_ids) else None,
                client_order_id=(
                    client_order_ids[leg_index - 1]
                    if leg_index <= len(client_order_ids)
                    else None
                ),
                pos_id=pos_id,
                status="active",
                attribution_status="verified",
                attribution_evidence={"policy_version": 2, "source": "test_fixture"},
                last_verified_at=datetime.now(UTC),
            ),
        )
    return binding_id


def _persist_reviewed_equivalent_assignments(session_factory, *, mutate=None):
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all()
        binding = session.get(ExecutionBinding, legs[0].execution_binding_id)
        binding.payload_json = json.dumps(
            {
                "draft": {
                    "stop_loss": 1560.0,
                    "take_profit_legs": [{"price": 1600.0}],
                }
            }
        )
        for leg in legs:
            leg.request_json = json.dumps(
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0.1",
                    "px": "1580",
                }
            )
        leg_ids = [leg.id for leg in legs]
        position_ids = [str(leg.pos_id) for leg in legs]
        evidence = {
            "policy_version": 2,
            "evidence_type": "equivalent_permutation_assignment",
            "component_leg_ids": leg_ids,
            "component_position_ids": position_ids,
            "mapping_basis": "stable_sorted_canonicalization",
            "ownership_statement": (
                "binding owner proven; parent-child mapping canonicalized"
            ),
            "equivalence_signature": {
                "binding_id": legs[0].execution_binding_id,
                "strategy_instance_id": legs[0].strategy_instance_id,
                "venue": "deepcoin",
                "symbol": "ETH-USDT-SWAP",
                "side": "long",
                "requested_size": 0.1,
                "entry_price": 1580.0,
                "stop_loss": 1560.0,
                "take_profits": [1600.0],
                "protection_mutated": False,
                "margin_mode": "cross",
                "position_mode": "split",
                "order_kind": "market",
                "leg_population": [
                    {
                        "leg_id": leg.id,
                        "binding_id": leg.execution_binding_id,
                        "strategy_instance_id": leg.strategy_instance_id,
                        "venue": leg.venue,
                        "symbol": "ETH-USDT-SWAP",
                        "side": "long",
                        "requested_size": 0.1,
                        "entry_price": 1580.0,
                        "stop_loss": 1560.0,
                        "take_profits": [1600.0],
                        "margin_mode": "cross",
                        "position_mode": "split",
                        "order_kind": leg.order_kind,
                        "protection_mutated": False,
                    }
                    for leg in legs
                ],
                "position_population": [
                    {
                        "position_id": position_id,
                        "symbol": "ETH-USDT-SWAP",
                        "side": "long",
                        "size": 0.1,
                        "entry_price": 1580.0,
                        "stop_loss": 1560.0,
                        "take_profits": [1600.0],
                        "margin_mode": "cross",
                        "position_mode": "split",
                    }
                    for position_id in position_ids
                ],
            },
        }
        if mutate is not None:
            mutate(evidence, legs)
        for leg in legs:
            leg.attribution_evidence_json = json.dumps(evidence)
        session.commit()


def _signal(session_factory, *, action, payload=None, message_id=88, kol_id="alice", symbol="ETH", side="long"):
    return enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id=kol_id,
        chat_id=100,
        message_id=message_id,
        symbol=symbol,
        side=side,
        action=action,
        payload=payload or {},
        strategy_instance_id="deepcoin:100:55:ETH:long",
    )


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("close_position", lambda binding_id: {"binding_id": binding_id}),
        (
            "partial_close_and_move_stop_to_entry",
            lambda binding_id: {"targets": [{"binding_id": binding_id, "fraction": 0.5}]},
        ),
        (
            "adjust_stop_loss",
            lambda binding_id: {"binding_id": binding_id, "stop_loss": 1577.04},
        ),
        (
            "adjust_take_profit",
            lambda binding_id: {"binding_id": binding_id, "take_profit": 1610.0},
        ),
    ],
)
@pytest.mark.parametrize(
    "attribution_status", ["unassigned", "attribution_conflict", "evidence_unavailable"]
)
def test_position_mutations_require_verified_ownership(
    tmp_path, action, payload, attribution_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="entry-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
            attribution_status=attribution_status,
        ),
    )
    trade_signal = _signal(
        session_factory,
        action=action,
        payload=payload(binding_id),
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match=f"position_ownership_not_verified:{attribution_status}",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


@pytest.mark.parametrize(
    "attribution_status", ["unassigned", "attribution_conflict", "evidence_unavailable"]
)
def test_exact_bound_close_requires_verified_ownership(tmp_path, attribution_status):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="entry-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
            attribution_status=attribution_status,
        ),
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match=f"position_ownership_not_verified:{attribution_status}",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_exact_bound_close_rejects_legacy_weak_verified_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="entry-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
            attribution_status="verified",
            attribution_evidence={"evidence_type": "exact_regular_order_id"},
        ),
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match=(
            "position_ownership_evidence_not_authoritative|"
            "position_not_bound_to_exactly_one_active_binding"
        ),
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_exact_bound_close_accepts_explicit_legacy_manual_bind(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ).one()
        leg.order_kind = "manual_bind"
        leg.order_id = None
        leg.response_json = None
        leg.attribution_evidence_json = '{"source":"manual_operator_bind"}'
        session.commit()
    client = _FakeDeepcoinClient()

    result = close_bound_position_market(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
    )

    assert result["submitted"] is True
    assert len(client.order_payloads) == 1


def test_reviewed_equivalent_assignments_authorize_close_and_tpsl_management(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "avgPx": "1580",
            "slTriggerPx": "1560",
            "tpTriggerPx": "1600",
            "mgnMode": "cross",
            "mrgPosition": "split",
            "cTime": "1000",
        }
    ]

    close = close_bound_position_market(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
    )
    client.positions = [client.positions[0]]
    protection = adjust_position_tpsl(
        session_factory,
        trade_signal=_signal(
            session_factory,
            action="adjust_stop_loss",
            payload={"binding_id": binding_id, "stop_loss": 1577.04},
        ),
        deepcoin_client=client,
    )

    assert protection["submitted"] is True
    assert close["submitted"] is True
    assert close["pos_id"] == "pos-1"


def test_repair_produced_equivalent_evidence_authorizes_complete_live_management(
    tmp_path
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = None
        binding.payload_json = json.dumps(
            {
                "draft": {
                    "stop_loss": 1560.0,
                    "take_profit_legs": [{"price": 1600.0}],
                }
            }
        )
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        for leg in legs:
            leg.pos_id = None
            leg.status = "filled"
            leg.attribution_status = "attribution_conflict"
            leg.order_kind = "market"
            leg.request_json = json.dumps(
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0.1",
                    "px": "1580",
                }
            )
        session.commit()
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": pos_id,
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "avgPx": "1580",
            "slTriggerPx": "1560",
            "tpTriggerPx": "1600",
            "mgnMode": "cross",
            "mrgPosition": "split",
            "cTime": "1000",
        }
        for pos_id in ("pos-1", "pos-2")
    ]
    client.list_order_history = lambda *, inst_id=None: []
    client.list_trade_fills = lambda *, inst_id=None: []
    client.list_trigger_order_history = lambda *, inst_id: [
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": order_id,
            "clOrdId": client_order_id,
            "state": "filled",
            "posSide": "long",
            "sz": "0.1",
            "px": "1580",
            "triggerTime": "1000",
            "errorCode": "0",
        }
        for order_id, client_order_id in (
            ("entry-1", "client-1"),
            ("entry-2", "client-2"),
        )
    ]

    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    assert len(plan.actions) == 2
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    close = close_bound_position_market(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
    )
    client.positions = [client.positions[0]]
    protection = adjust_position_tpsl(
        session_factory,
        trade_signal=_signal(
            session_factory,
            action="adjust_stop_loss",
            payload={"binding_id": binding_id, "stop_loss": 1577.04},
        ),
        deepcoin_client=client,
    )

    assert close["submitted"] is True
    assert protection["submitted"] is True


@pytest.mark.parametrize(
    ("case", "drift"),
    [
        (
            "request_size",
            lambda session, binding, legs: setattr(
                legs[0],
                "request_json",
                json.dumps(
                    {
                        "instId": "ETH-USDT-SWAP",
                        "posSide": "long",
                        "sz": "0.2",
                        "px": "1580",
                    }
                ),
            ),
        ),
        (
            "binding_draft_stop",
            lambda session, binding, legs: setattr(
                binding,
                "payload_json",
                json.dumps(
                    {
                        "draft": {
                            "stop_loss": 1550.0,
                            "take_profit_legs": [{"price": 1600.0}],
                        }
                    }
                ),
            ),
        ),
        (
            "binding_mode",
            lambda session, binding, legs: setattr(
                binding, "position_mode", "merged"
            ),
        ),
        (
            "binding_strategy",
            lambda session, binding, legs: setattr(
                binding, "strategy_instance_id", "deepcoin:other:ETH:long"
            ),
        ),
        (
            "binding_venue",
            lambda session, binding, legs: setattr(binding, "venue", "other"),
        ),
    ],
)
def test_reviewed_equivalent_assignment_rebuilds_current_database_economics(
    tmp_path, case, drift
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        binding = session.get(ExecutionBinding, legs[0].execution_binding_id)
        drift(session, binding, legs)
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match=(
            "position_ownership_evidence_not_authoritative|"
            "position_not_bound_to_exactly_one_active_binding"
        ),
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_reviewed_equivalent_assignment_rebuilds_response_economics(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    with session_factory() as session:
        for leg in session.query(ExecutionOrderLeg).all():
            leg.request_json = json.dumps(
                {"instId": "ETH-USDT-SWAP", "posSide": "long"}
            )
            leg.response_json = json.dumps(
                {"data": {"sz": "0.1", "avgPx": "1580"}}
            )
        session.commit()
    client = _FakeDeepcoinClient()
    client.positions[0].update(
        {
            "avgPx": "1580",
            "slTriggerPx": "1560",
            "tpTriggerPx": "1600",
            "mgnMode": "cross",
            "mrgPosition": "split",
        }
    )

    result = close_bound_position_market(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
    )

    assert result["submitted"] is True


def test_reviewed_equivalent_assignment_rejects_response_economic_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).all()
        for leg in legs:
            leg.request_json = json.dumps(
                {"instId": "ETH-USDT-SWAP", "posSide": "long"}
            )
            leg.response_json = json.dumps(
                {"data": {"sz": "0.1", "avgPx": "1580"}}
            )
        legs[0].response_json = json.dumps(
            {"data": {"sz": "0.2", "avgPx": "1580"}}
        )
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_reviewed_equivalent_assignment_rejects_protection_mutation_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding_id,
            action="adjust_position_tpsl",
        ),
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        adjust_position_tpsl(
            session_factory,
            trade_signal=_signal(
                session_factory,
                action="adjust_stop_loss",
                payload={"binding_id": binding_id, "stop_loss": 1577.04},
            ),
            deepcoin_client=client,
        )

    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pos", "0.2"),
        ("avgPx", "1581"),
        ("slTriggerPx", "1550"),
        ("tpTriggerPx", "1610"),
        ("mgnMode", "isolated"),
        ("mrgPosition", "merge"),
        ("instId", "BTC-USDT-SWAP"),
        ("posSide", "short"),
        ("avgPx", None),
    ],
)
def test_reviewed_equivalent_assignment_rejects_live_position_economic_drift(
    tmp_path, field, value
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "avgPx": "1580",
            "slTriggerPx": "1560",
            "tpTriggerPx": "1600",
            "mgnMode": "cross",
            "mrgPosition": "split",
            "cTime": "1000",
            field: value,
        }
    ]

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="live_position_economics_changed|bound_position_not_found",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing_policy", lambda evidence, legs: evidence.pop("policy_version")),
        ("missing_legs", lambda evidence, legs: evidence.pop("component_leg_ids")),
        ("missing_positions", lambda evidence, legs: evidence.pop("component_position_ids")),
        ("missing_basis", lambda evidence, legs: evidence.pop("mapping_basis")),
        ("missing_statement", lambda evidence, legs: evidence.pop("ownership_statement")),
        ("reversed_pair", lambda evidence, legs: evidence["component_position_ids"].reverse()),
        ("missing_signature_symbol", lambda evidence, legs: evidence["equivalence_signature"].pop("symbol")),
        ("missing_leg_population", lambda evidence, legs: evidence["equivalence_signature"].pop("leg_population")),
        ("missing_position_population", lambda evidence, legs: evidence["equivalence_signature"].pop("position_population")),
        ("wrong_signature_binding", lambda evidence, legs: evidence["equivalence_signature"].update(binding_id=999)),
        ("wrong_signature_strategy", lambda evidence, legs: evidence["equivalence_signature"].update(strategy_instance_id="forged")),
        ("wrong_signature_venue", lambda evidence, legs: evidence["equivalence_signature"].update(venue="other")),
        ("wrong_signature_order_kind", lambda evidence, legs: evidence["equivalence_signature"].update(order_kind="limit")),
        ("cross_owner_leg", lambda evidence, legs: evidence["equivalence_signature"]["leg_population"][1].update(binding_id=999)),
        ("cross_strategy_leg", lambda evidence, legs: evidence["equivalence_signature"]["leg_population"][1].update(strategy_instance_id="forged")),
        ("other_position_symbol", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][1].update(symbol="BTC-USDT-SWAP")),
        ("current_position_side", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][0].update(side="short")),
        ("other_position_size", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][1].update(size=0.2)),
        ("current_position_entry", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][0].update(entry_price=1581.0)),
        ("other_position_margin", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][1].update(margin_mode="isolated")),
        ("current_position_mode", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][0].update(position_mode="merged")),
        ("other_position_stop", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][1].update(stop_loss=1550.0)),
        ("current_position_take_profit", lambda evidence, legs: evidence["equivalence_signature"]["position_population"][0].update(take_profits=[1610.0])),
        (
            "forged_member",
            lambda evidence, legs: (
                evidence["component_leg_ids"].append(999),
                evidence["component_position_ids"].append("pos-3"),
                evidence["equivalence_signature"]["leg_population"].append(
                    {
                        **evidence["equivalence_signature"]["leg_population"][0],
                        "leg_id": 999,
                    }
                ),
                evidence["equivalence_signature"]["position_population"].append(
                    {
                        **evidence["equivalence_signature"]["position_population"][0],
                        "position_id": "pos-3",
                    }
                ),
            ),
        ),
    ],
)
def test_reviewed_equivalent_assignment_fails_closed_when_schema_is_incomplete_or_stale(
    tmp_path, case, mutate
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory, mutate=mutate)
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_reviewed_equivalent_assignment_rejects_stale_other_component_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    with session_factory() as session:
        session.query(ExecutionOrderLeg).filter_by(leg_index=2).one().pos_id = (
            "stale-pos-2"
        )
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


@pytest.mark.parametrize("drift", ["binding", "strategy"])
def test_reviewed_equivalent_assignment_rejects_coordinated_cross_owner_component(
    tmp_path, drift
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        evidence = json.loads(legs[0].attribution_evidence_json)
        if drift == "binding":
            other_binding = ExecutionBinding(
                kol_id="other-owner",
                chat_id=200,
                message_id=99,
                symbol="ETH",
                side="long",
                status="active",
                strategy_instance_id="deepcoin:100:55:ETH:long",
            )
            session.add(other_binding)
            session.flush()
            legs[1].execution_binding_id = other_binding.id
            evidence["equivalence_signature"]["leg_population"][1][
                "binding_id"
            ] = other_binding.id
        else:
            legs[1].strategy_instance_id = "deepcoin:other:ETH:long"
            evidence["equivalence_signature"]["leg_population"][1][
                "strategy_instance_id"
            ] = "deepcoin:other:ETH:long"
        for leg in legs:
            leg.attribution_evidence_json = json.dumps(evidence)
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert client.order_payloads == []


def test_adjust_stop_loss_cancels_existing_position_tpsl_before_resetting(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()

    result = adjust_position_tpsl(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
    )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == ["tp-old", "sl-old"]
    assert client.protection_payloads == [
        {
            "instType": "SWAP",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "posId": "pos-1",
            "tpTriggerPx": "1605.6",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "slTriggerPx": "1577.04",
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
        }
    ]
    assert result["cancelled_tpsl_order_ids"] == ["tp-old", "sl-old"]
    assert result["before"] == {"take_profit": 1605.6, "stop_loss": 1567.52}
    assert result["after"] == {"take_profit": 1605.6, "stop_loss": 1577.04}

    events = list_execution_events(session_factory, execution_binding_id=binding_id)
    assert [event.action for event in events] == [
        "adjust_position_tpsl",
        "cancel_position_tpsl",
        "cancel_position_tpsl",
    ]
    assert events[0].related_order_id == "tp-old,sl-old"


def test_adjust_position_tpsl_refuses_to_append_when_existing_tpsl_is_missing(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = []

    with pytest.raises(DeepcoinExecutionActionError, match="no_existing_position_tpsl_to_adjust"):
        adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )
    assert client.protection_payloads == []


def test_adjust_position_tpsl_refuses_unattributed_pending_tpsl_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.positions.append(
        {
            "posId": "pos-other",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "cTime": "1000",
        }
    )
    for order in client.trigger_pending:
        order.pop("posId")

    with pytest.raises(DeepcoinExecutionActionError, match="ambiguous_pending_position_tpsl"):
        adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )

    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_adjust_position_tpsl_accepts_uniquely_attributed_unscoped_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    for order in client.trigger_pending:
        order.pop("posId")

    result = adjust_position_tpsl(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=client,
    )

    assert result["cancelled_tpsl_order_ids"] == ["tp-old", "sl-old"]
    assert [item["ordId"] for item in client.cancel_trigger_payloads] == [
        "tp-old",
        "sl-old",
    ]


def test_process_trade_signal_live_closes_bound_position_with_close_pos_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
        processed_at=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )

    assert client.order_payloads == [
        {
            "instId": "ETH-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": "0.1",
            "mrgPosition": "split",
            "closePosId": "pos-1",
        }
    ]
    assert result["action"] == "close_position"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "closed"
        assert binding.last_exchange_status == "close_position_submitted"


def test_process_trade_signal_live_closes_all_bound_position_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        status="active",
    )
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "cTime": "1000",
        },
        {
            "posId": "pos-2",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.2",
            "cTime": "1001",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert [payload["closePosId"] for payload in client.order_payloads] == ["pos-1", "pos-2"]
    assert [payload["sz"] for payload in client.order_payloads] == ["0.1", "0.2"]
    assert result["pos_id"] == "pos-1,pos-2"
    assert result["close_size"] == 0.30000000000000004
    assert result["full_close"] is True
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "closed"


def test_composite_breakeven_management_reduces_each_target_then_uses_its_own_average_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_binding_id = _binding(
        session_factory,
        kol_id="andy",
        symbol="BTC",
        side="short",
        pos_id="pos-1",
        strategy_instance_id="deepcoin:100:55:BTC:short",
    )
    second_binding_id = _binding(
        session_factory,
        kol_id="andy",
        message_id=56,
        symbol="BTC",
        side="short",
        pos_id="pos-2",
        strategy_instance_id="deepcoin:100:56:BTC:short",
    )
    signal = _signal(
        session_factory,
        action="partial_close_and_move_stop_to_entry",
        payload={
            "targets": [
                {"binding_id": first_binding_id, "fraction": 0.5},
                {"binding_id": second_binding_id, "fraction": 0.5},
            ]
        },
        kol_id="andy",
        symbol="BTC",
        side="short",
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "2", "avgPx": "64000", "cTime": "1000"},
        {"posId": "pos-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "4", "avgPx": "64500", "cTime": "1001"},
    ]
    client.trigger_pending = [
        {"triggerOrderType": "TPSL", "ordId": "tp-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-1", "tpTriggerPx": "63200", "sz": "2", "cTime": "1000"},
        {"triggerOrderType": "TPSL", "ordId": "sl-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-1", "slTriggerPx": "65200", "sz": "2", "cTime": "1000"},
        {"triggerOrderType": "TPSL", "ordId": "tp-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-2", "tpTriggerPx": "62500", "sz": "4", "cTime": "1001"},
        {"triggerOrderType": "TPSL", "ordId": "sl-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-2", "slTriggerPx": "65400", "sz": "4", "cTime": "1001"},
    ]

    result = execute_deepcoin_management_signal(
        session_factory,
        trade_signal=signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
    )

    assert [order["closePosId"] for order in client.order_payloads] == ["pos-1", "pos-2"]
    assert [order["sz"] for order in client.order_payloads] == ["1", "2"]
    assert [target["status"] for target in result["targets"]] == ["submitted", "submitted"], result["targets"]
    assert [payload["slTriggerPx"] for payload in client.protection_payloads] == ["64000.0", "64500.0"]
    assert [payload["tpTriggerPx"] for payload in client.protection_payloads] == ["63200.0", "62500.0"]
    assert [target["status"] for target in result["targets"]] == ["submitted", "submitted"]


def test_process_trade_signal_live_closes_only_requested_bound_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        status="active",
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="entry-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="entry-2",
            client_order_id="client-2",
            pos_id="pos-2",
            status="active",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id, "pos_id": "pos-2", "fraction": 0.5},
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "cTime": "1000",
        },
        {
            "posId": "pos-2",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.2",
            "cTime": "1001",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.order_payloads == [
        {
            "instId": "ETH-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": "0.1",
            "mrgPosition": "split",
            "closePosId": "pos-2",
        }
    ]
    assert result["pos_id"] == "pos-2"
    assert result["close_size"] == 0.1
    assert result["full_close"] is False
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "active"
        assert binding.last_exchange_status == "partial_close_submitted"
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.pos_id, leg.status) for leg in legs] == [
        (1, "pos-1", "active"),
        (2, "pos-2", "partial_closed"),
    ]


def test_bound_position_market_close_reserves_exact_position_before_concurrent_submission(tmp_path):
    class BlockingClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.listing_started = Event()
            self.release_listing = Event()

        def list_positions(self, *, inst_id=None):
            self.listing_started.set()
            assert self.release_listing.wait(timeout=5)
            return super().list_positions(inst_id=inst_id)

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(session_factory, pos_id="pos-1", status="active")
    client = BlockingClient()
    first_attempt_errors = []

    def first_attempt():
        try:
            close_bound_position_market(
                session_factory,
                pos_id="pos-1",
                deepcoin_client=client,
                executed_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
            )
        except Exception as exc:  # pragma: no cover - assertion below documents failures
            first_attempt_errors.append(exc)

    thread = Thread(target=first_attempt)
    thread.start()
    assert client.listing_started.wait(timeout=5)

    second_attempt_errors = []

    def second_attempt():
        try:
            close_bound_position_market(
                session_factory,
                pos_id="pos-1",
                deepcoin_client=client,
                executed_at=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
            )
        except Exception as exc:
            second_attempt_errors.append(exc)

    second_thread = Thread(target=second_attempt)
    second_thread.start()
    assert second_thread.is_alive()

    client.release_listing.set()
    thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert not thread.is_alive()
    assert not second_thread.is_alive()
    assert first_attempt_errors == []
    assert len(second_attempt_errors) == 1
    assert isinstance(second_attempt_errors[0], DeepcoinExecutionActionError)
    assert "close_already_reserved" in str(second_attempt_errors[0])
    assert [payload["closePosId"] for payload in client.order_payloads] == ["pos-1"]
    events = list_execution_events(session_factory, pos_id="pos-1")
    assert [(event.action, event.status) for event in events] == [
        ("close_bound_position_market", "submitted"),
        ("close_bound_position_reservation", "submitted"),
        ("close_bound_position_reservation", "reserved"),
    ]


def test_bound_position_market_close_keeps_reservation_after_exchange_error(tmp_path):
    class FailingClient(_FakeDeepcoinClient):
        def place_order(self, order_payload):
            self.order_payloads.append(order_payload)
            raise RuntimeError("connection lost after request may have reached exchange")

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(session_factory, pos_id="pos-1", status="active")
    client = FailingClient()

    with pytest.raises(RuntimeError, match="connection lost"):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
        )

    with pytest.raises(DeepcoinExecutionActionError, match="close_already_reserved"):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
        )

    assert len(client.order_payloads) == 1
    events = list_execution_events(session_factory, pos_id="pos-1")
    assert [(event.action, event.status) for event in events] == [
        ("close_bound_position_reservation", "unknown_exchange_outcome"),
        ("close_bound_position_reservation", "reserved"),
    ]


def test_process_trade_signal_live_cancels_bound_trigger_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert result["cancel_type"] == "trigger"
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status == "cancelled"


def test_process_trade_signal_live_cancels_trigger_entry_identified_by_algo_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "algoId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert result["cancel_type"] == "trigger"
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status == "cancelled"


def test_process_trade_signal_live_cancels_all_bound_trigger_entry_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        order_id="trigger-1,trigger-2",
        client_order_id="client-1,client-2",
        pos_id=None,
        status="open",
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-1",
            client_order_id="client-1",
            status="open",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-2",
            client_order_id="client-2",
            status="open",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-1",
            "clOrdId": "client-1",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        },
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-2",
            "clOrdId": "client-2",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == [
        "trigger-1",
        "trigger-2",
    ]
    assert result["order_id"] == "trigger-1,trigger-2"
    assert len(result["cancelled_orders"]) == 2
    events = list_execution_events(session_factory, execution_binding_id=binding_id)
    assert [event.order_id for event in events] == ["trigger-2", "trigger-1"]
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "cancelled"),
        (2, "trigger-2", "cancelled"),
    ]


def test_process_trade_signal_live_recreates_trigger_entry_to_adjust_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-old",
            client_order_id="client-old",
            status="open",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="adjust_trigger_entry_tpsl",
        payload={
            "binding_id": binding_id,
            "client_order_id": "client-new",
            "take_profit": 1615.12,
            "stop_loss": 1577.04,
        },
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
            "price": "1000",
            "triggerPrice": "1000",
            "sz": "0.1",
            "tpTriggerPx": "1605.6",
            "slTriggerPx": "1567.52",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert client.trigger_payloads[0]["price"] == "1000"
    assert client.trigger_payloads[0]["tpTriggerPx"] == 1615.12
    assert client.trigger_payloads[0]["slTriggerPx"] == 1577.04
    assert result["old_order_id"] == "trigger-old"
    assert result["new_order_id"] == "trigger-new"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.order_id == "trigger-new"
        assert binding.last_exchange_status == "trigger_entry_recreated"
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.status) for leg in legs] == [
        (1, "trigger-new", "client-new", "open")
    ]
