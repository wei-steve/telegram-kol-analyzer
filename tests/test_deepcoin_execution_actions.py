from datetime import UTC, datetime
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.deepcoin_execution_actions import DeepcoinExecutionActionError
from telegram_kol_research.deepcoin_execution_actions import adjust_position_tpsl
from telegram_kol_research.deepcoin_execution_actions import close_bound_position_market
from telegram_kol_research.deepcoin_execution_actions import cancel_revision_entry_leg
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.deepcoin_execution_actions import partial_close_and_move_stop_to_entry
from telegram_kol_research.deepcoin_execution_actions import _management_action_matches_batch
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord
from telegram_kol_research.execution_bindings import list_execution_order_legs
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_bindings import upsert_execution_order_leg
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.strategy_threads import create_strategy_thread_for_lifecycle
from telegram_kol_research.source_message_deletion import record_source_message_deleted


def test_cancel_revision_entry_leg_requires_exact_readback_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-cancel.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:201:10:BTC:long",
            kol_id="group:201",
            chat_id=201,
            message_id=10,
            symbol="BTC",
            side="long",
            status="open",
        )
        lifecycle = StrategyLifecycle(
            chat_id=201,
            message_id=10,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        )
        session.add_all([binding, lifecycle])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="limit",
            order_id="revision-order",
            status="submitted",
        )
        session.add(leg)
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id
        leg_id = leg.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )

    class Client:
        def __init__(self):
            self.open_orders = [
                {
                    "ordId": "revision-order",
                    "instId": "BTC-USDT-SWAP",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_open_orders(self, *, inst_id):
            return list(self.open_orders)

        def cancel_order(self, payload):
            self.open_orders = []
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return [{"ordId": "revision-order", "state": "canceled"}]

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return []

        def list_positions(self, *, inst_id=None):
            return []

    result = cancel_revision_entry_leg(
        session_factory,
        strategy_thread_id=thread.id,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        deepcoin_client=Client(),
        executed_at=datetime(2026, 7, 27, 2, tzinfo=UTC),
    )

    assert result["status"] == "confirmed_cancelled"
    assert result["order_id"] == "revision-order"


def test_cancel_revision_entry_leg_never_treats_race_fill_as_cancelled(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-race-fill.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:201:11:BTC:long",
            kol_id="group:201",
            chat_id=201,
            message_id=11,
            symbol="BTC",
            side="long",
            status="open",
        )
        lifecycle = StrategyLifecycle(
            chat_id=201,
            message_id=11,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        )
        session.add_all([binding, lifecycle])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="limit",
            order_id="race-order",
            status="submitted",
        )
        session.add(leg)
        session.commit()
        lifecycle_id, binding_id, leg_id = lifecycle.id, binding.id, leg.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )

    class Client:
        def __init__(self):
            self.visible = True

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_open_orders(self, *, inst_id):
            return [{"ordId": "race-order"}] if self.visible else []

        def cancel_order(self, payload):
            self.visible = False
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return [{"ordId": "race-order", "state": "filled", "posId": "pos-race"}]

        def list_trade_fills(self, *, inst_id=None):
            return [{"ordId": "race-order", "posId": "pos-race"}]

        def list_trigger_order_history(self, *, inst_id):
            return []

        def list_positions(self, *, inst_id=None):
            return [{"posId": "pos-race", "pos": "1"}]

    result = cancel_revision_entry_leg(
        session_factory,
        strategy_thread_id=thread.id,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        deepcoin_client=Client(),
    )

    assert result["status"] == "submit_unknown"
    assert result["reason"] == "revision_order_filled_during_cancel"
from telegram_kol_research.protection_ledger import (
    list_verified_ledger_rows_for_positions,
)
from telegram_kol_research.position_attribution_repair import (
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
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
                "avgPx": "1580",
                "mgnMode": "cross",
                "mrgPosition": "split",
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
        self.actual_trigger_order_payloads = []
        self.cancel_position_payloads = []
        self.cancel_order_payloads = []
        self.protection_payloads = []
        self.protection_outcomes = []
        self.order_payloads = []
        self.trigger_payloads = []
        self.order_history = []
        self.trigger_history = []
        self.trade_fills = []

    def list_positions(self, *, inst_id=None):
        return self.positions

    def list_trigger_orders_pending(self, *, inst_id):
        return self.trigger_pending

    def list_open_orders(self, *, inst_id=None):
        return self.open_orders

    def list_position_history(self, *, inst_id, pos_id):
        return []

    def list_order_history(self, *, inst_id=None):
        return list(self.order_history)

    def list_trigger_order_history(self, *, inst_id):
        return list(self.trigger_history)

    def list_trade_fills(self, *, inst_id=None):
        return list(self.trade_fills)

    def cancel_trigger_order(self, cancel_payload):
        self.actual_trigger_order_payloads.append(cancel_payload)
        self.cancel_trigger_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def cancel_position_sltp(self, cancel_payload):
        self.cancel_position_payloads.append(cancel_payload)
        self.cancel_trigger_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def cancel_order(self, cancel_payload):
        self.cancel_order_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        if self.protection_outcomes:
            outcome = self.protection_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            response = outcome
        else:
            response = {"code": "0", "data": {"ordId": "tpsl-new"}}
        data = response.get("data") if isinstance(response, dict) else None
        order_id = (
            data.get("ordId") if isinstance(data, dict)
            else data if isinstance(data, str)
            else None
        )
        if order_id:
            self.trigger_pending.append(
                {
                    "ordId": order_id,
                    "instId": protection_payload["instId"],
                    "posId": protection_payload["posId"],
                    "posSide": protection_payload["posSide"],
                    **(
                        {"slTriggerPx": protection_payload["slTriggerPx"]}
                        if protection_payload.get("slTriggerPx") not in (None, "")
                        else {"tpTriggerPx": protection_payload["tpTriggerPx"]}
                    ),
                    "sz": protection_payload.get("sz", "0"),
                }
            )
        return response

    def place_order(self, order_payload):
        self.order_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "close-1"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "trigger-new"}}


@pytest.mark.parametrize(
    ("signal_action", "intent", "effective_action", "expected"),
    [
        ("close_position", "full_exit", "full_exit", True),
        ("close_position", "partial_take_profit", "partial_close", False),
        ("adjust_stop_loss", "adjust_stop_loss", "adjust_stop_loss", True),
        ("adjust_position_tpsl", "move_stop_to_break_even", "move_stop_to_break_even", True),
        (
            "adjust_position_tpsl",
            "move_stop_to_break_even",
            "break_even_by_market",
            True,
        ),
        (
            "adjust_position_tpsl",
            "adjust_stop_loss",
            "break_even_by_market",
            False,
        ),
        ("adjust_stop_loss", "full_exit", "full_exit", False),
        (
            "partial_close_and_move_stop_to_entry",
            "partial_then_break_even",
            "partial_then_break_even",
            True,
        ),
        (
            "partial_close_and_move_stop_to_entry",
            "partial_take_profit",
            "partial_close",
            False,
        ),
    ],
)
def test_management_compatibility_action_mapping_is_bidirectional(
    signal_action, intent, effective_action, expected
):
    assert (
        _management_action_matches_batch(
            signal_action=signal_action,
            batch_intent=intent,
            effective_action=effective_action,
        )
        is expected
    )


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
        leg_id = upsert_execution_order_leg(
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
        if pos_id == "pos-1":
            with session_factory() as session:
                for order_id, purpose, trigger in (
                    ("tp-old", "take_profit", "1605.6"),
                    ("sl-old", "stop_loss", "1567.52"),
                ):
                    upsert_protection_ledger_row(
                        session,
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg_id,
                        strategy_instance_id=values.get("strategy_instance_id"),
                        pos_id=pos_id,
                        instrument_id="ETH-USDT-SWAP",
                        side="long",
                        order_id=order_id,
                        purpose=purpose,
                        trigger_price=trigger,
                        size_text="0.1",
                        status="verified",
                        evidence_source="test_exact_owner",
                        evidence={},
                        seen_at=datetime.now(UTC),
                    )
                session.commit()
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


def _complete_equivalent_live_positions():
    return [
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


def _reviewed_equivalent_binding(session_factory):
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    _persist_reviewed_equivalent_assignments(session_factory)
    return binding_id


def _signal(
    session_factory,
    *,
    action,
    payload=None,
    message_id=88,
    kol_id="alice",
    symbol="ETH",
    side="long",
    strategy_instance_id="deepcoin:100:55:ETH:long",
    source_type="manual_operator",
):
    return enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type=source_type,
        kol_id=kol_id,
        chat_id=100,
        message_id=message_id,
        symbol=symbol,
        side=side,
        action=action,
        payload=payload or {},
        strategy_instance_id=strategy_instance_id,
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

    expected_error = (
        "legacy_management_signal_requires_batch"
        if action in {"close_position", "partial_close_and_move_stop_to_entry"}
        else f"position_ownership_not_verified:{attribution_status}"
    )
    with pytest.raises(DeepcoinExecutionActionError, match=expected_error):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_legacy_close_signal_without_management_batch_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="legacy_management_signal_requires_batch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=_signal(
                session_factory,
                action="close_position",
                payload={"binding_id": binding_id, "fraction": 0.5},
            ),
            deepcoin_client=client,
        )

    assert client.order_payloads == []


@pytest.mark.parametrize(
    "action",
    [
        "close_position",
        "adjust_stop_loss",
        "adjust_position_tpsl",
        "partial_close_and_move_stop_to_entry",
    ],
)
def test_automatic_legacy_management_actions_without_batch_fail_before_exchange_call(
    tmp_path, action
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    signal = _signal(
        session_factory,
        action=action,
        source_type="kol_management",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="legacy_management_signal_requires_batch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_management_compatibility_signal_delegates_only_exact_source_batch(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=88,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
            text="ETH exit",
            archived_target_group=True,
        )
        session.add(raw)
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=55,
            symbol="ETH",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
            execution_binding_id=binding_id,
        )
        session.add(lifecycle)
        session.commit()
        raw_id = raw.id
        lifecycle_id = lifecycle.id
    batch = SimpleNamespace(
        id=77,
        raw_message_id=raw_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        strategy_instance_id="deepcoin:100:55:ETH:long",
        intent="full_exit",
        effective_action="full_exit",
    )
    signal = _signal(
        session_factory,
        action="close_position",
        source_type="kol_management",
        payload={"binding_id": binding_id, "management_batch_id": 77},
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    import telegram_kol_research.deepcoin_execution_actions as actions
    import telegram_kol_research.strategy_management_executor as executor

    monkeypatch.setattr(actions, "load_management_batch", lambda *_args: batch)
    delegated = []
    monkeypatch.setattr(
        executor,
        "execute_management_batch",
        lambda _factory, *, batch_id, deepcoin_client, executed_at=None: delegated.append(
            batch_id
        )
        or {"status": "delegated", "batch_id": batch_id},
    )
    client = _FakeDeepcoinClient()

    result = execute_deepcoin_management_signal(
        session_factory,
        trade_signal=signal,
        deepcoin_client=client,
    )

    assert result == {"status": "delegated", "batch_id": 77}
    assert delegated == [77]
    assert client.order_payloads == []

    wrong_action_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        source_type="kol_management",
        payload={"binding_id": binding_id, "management_batch_id": 77},
    )
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="management_signal_batch_identity_mismatch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=wrong_action_signal,
            deepcoin_client=client,
        )
    assert delegated == [77]

    batch.target_lifecycle_id = lifecycle_id + 999
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="management_signal_batch_identity_mismatch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
        )
    batch.target_lifecycle_id = lifecycle_id
    assert delegated == [77]

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.chat_id = 300
        session.commit()
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="management_signal_batch_identity_mismatch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
        )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.chat_id = 100
        session.commit()
    assert delegated == [77]

    cross_chat_signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="alice",
        chat_id=200,
        message_id=88,
        symbol="ETH",
        side="long",
        action="close_position",
        payload={"binding_id": binding_id, "management_batch_id": 77},
        strategy_instance_id="deepcoin:100:55:ETH:long",
    )
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="management_signal_batch_identity_mismatch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=cross_chat_signal,
            deepcoin_client=client,
        )
    assert delegated == [77]
    assert client.order_payloads == []


@pytest.mark.parametrize("batch_id", [True, 0, -1, 1.5, "01", "1.0"])
def test_management_compatibility_signal_rejects_noncanonical_batch_id(
    tmp_path, batch_id
):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = _signal(
        session_factory,
        action="close_position",
        source_type="kol_management",
        payload={"binding_id": 1, "management_batch_id": batch_id},
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="legacy_management_signal_requires_batch",
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
        )

    assert client.order_payloads == []


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


def test_exact_bound_close_cancels_pending_entry_leg_before_market_close(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = StrategyLifecycle(
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            symbol=binding.symbol,
            side=binding.side,
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 30, tzinfo=UTC),
            entered_at=datetime(2026, 7, 30, 0, 1, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-2",
                client_order_id="client-2",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()

    class Client(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.operations = []
            self.trigger_pending.append(
                {
                    "ordId": "entry-2",
                    "clOrdId": "client-2",
                    "instId": "ETH-USDT-SWAP",
                }
            )

        def list_trigger_orders_pending(self, *, inst_id):
            self.operations.append("list_trigger")
            return list(self.trigger_pending)

        def list_open_orders(self, *, inst_id=None):
            self.operations.append("list_open")
            return list(self.open_orders)

        def cancel_trigger_order(self, cancel_payload):
            self.operations.append("cancel_entry")
            self.trigger_pending = [
                order
                for order in self.trigger_pending
                if order.get("ordId") != cancel_payload.get("ordId")
            ]
            self.trigger_history.append(
                {
                    "ordId": cancel_payload.get("ordId"),
                    "clOrdId": cancel_payload.get("clOrdId"),
                    "state": "canceled",
                }
            )
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

        def place_order(self, order_payload):
            self.operations.append("close_position")
            return super().place_order(order_payload)

    client = Client()

    result = close_bound_position_market(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
        executed_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert client.operations.index("cancel_entry") < client.operations.index(
        "close_position"
    )
    with session_factory() as session:
        pending_leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-2")
            .one()
        )
        assert pending_leg.status == "cancelled"
        assert pending_leg.terminal_reason == "terminal_entry_cleanup_confirmed"


def test_exact_bound_close_does_not_submit_when_pending_entry_cancel_is_unconfirmed(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        session.add(
            StrategyLifecycle(
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                symbol=binding.symbol,
                side=binding.side,
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 30, tzinfo=UTC),
                entered_at=datetime(2026, 7, 30, 0, 1, tzinfo=UTC),
                execution_binding_id=binding.id,
            )
        )
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-2",
                client_order_id="client-2",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()

    class Client(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.trigger_pending.append(
                {
                    "ordId": "entry-2",
                    "clOrdId": "client-2",
                    "instId": "ETH-USDT-SWAP",
                }
            )

        def cancel_trigger_order(self, cancel_payload):
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    client = Client()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="terminal_entry_cleanup_blocked",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
        )

    assert client.order_payloads == []


def test_exact_bound_close_blocks_new_position_created_during_entry_cleanup(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(
        session_factory,
        order_id="entry-1,entry-2",
        client_order_id="client-1,client-2",
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        session.add(
            StrategyLifecycle(
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                symbol=binding.symbol,
                side=binding.side,
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 30, tzinfo=UTC),
                entered_at=datetime(2026, 7, 30, 0, 1, tzinfo=UTC),
                execution_binding_id=binding.id,
            )
        )
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-2",
                client_order_id="client-2",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()

    class Client(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.position_reads = 0
            self.trigger_pending.append(
                {
                    "ordId": "entry-2",
                    "clOrdId": "client-2",
                    "instId": "ETH-USDT-SWAP",
                }
            )

        def list_positions(self, *, inst_id=None):
            self.position_reads += 1
            rows = list(self.positions)
            if self.position_reads >= 2:
                rows.append(
                    {
                        "posId": "pos-new-second-leg",
                        "instId": "ETH-USDT-SWAP",
                        "posSide": "long",
                        "pos": "0.1",
                        "avgPx": "1581",
                        "mgnMode": "cross",
                        "mrgPosition": "split",
                    }
                )
            return rows

        def cancel_trigger_order(self, cancel_payload):
            self.trigger_pending = [
                row
                for row in self.trigger_pending
                if row.get("ordId") != cancel_payload.get("ordId")
            ]
            self.trigger_history.append(
                {
                    "ordId": cancel_payload.get("ordId"),
                    "clOrdId": cancel_payload.get("clOrdId"),
                    "state": "canceled",
                }
            )
            return {"code": "0"}

    client = Client()
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="new_position_detected_during_terminal_entry_cleanup",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
        )

    assert client.order_payloads == []


def test_reviewed_equivalent_assignments_authorize_close_and_tpsl_management(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _reviewed_equivalent_binding(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = _complete_equivalent_live_positions()

    protection = adjust_position_tpsl(
        session_factory,
        trade_signal=_signal(
            session_factory,
            action="adjust_stop_loss",
            payload={
                "binding_id": binding_id,
                "pos_id": "pos-1",
                "stop_loss": 1577.04,
            },
        ),
        deepcoin_client=client,
    )
    assert protection["submitted"] is True
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )


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

    protection = adjust_position_tpsl(
        session_factory,
        trade_signal=_signal(
            session_factory,
            action="adjust_stop_loss",
            payload={
                "binding_id": binding_id,
                "pos_id": "pos-1",
                "stop_loss": 1577.04,
            },
        ),
        deepcoin_client=client,
    )
    assert protection["submitted"] is True
    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_ownership_evidence_not_authoritative",
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )


@pytest.mark.parametrize(
    ("sibling_state", "field", "value"),
    [
        ("size_drift", "pos", "0.2"),
        ("entry_drift", "avgPx", "1581"),
        ("stop_drift", "slTriggerPx", "1550"),
        ("take_profit_drift", "tpTriggerPx", "1610"),
        ("margin_drift", "mgnMode", "isolated"),
        ("position_mode_drift", "mrgPosition", "merge"),
        ("symbol_drift", "instId", "BTC-USDT-SWAP"),
        ("side_drift", "posSide", "short"),
        ("absent", None, None),
        ("duplicate", None, None),
    ],
)
def test_exact_bound_close_freezes_when_equivalent_sibling_is_not_current(
    tmp_path, sibling_state, field, value
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _reviewed_equivalent_binding(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = _complete_equivalent_live_positions()
    if field is not None:
        client.positions[1][field] = value
    elif sibling_state == "absent":
        client.positions.pop()
    else:
        client.positions.append(dict(client.positions[1]))
    calls = 0
    original_list_positions = client.list_positions

    def list_positions(*, inst_id=None):
        nonlocal calls
        calls += 1
        return original_list_positions(inst_id=inst_id)

    client.list_positions = list_positions

    with pytest.raises(
        DeepcoinExecutionActionError, match="live_position_economics_changed"
    ):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
        )

    assert calls == 1
    assert client.order_payloads == []
    with session_factory() as session:
        assert session.query(BoundPositionCloseReservation).count() == 0


@pytest.mark.parametrize("sibling_state", ["drift", "absent"])
def test_requested_subset_close_freezes_when_equivalent_sibling_is_not_current(
    tmp_path, sibling_state
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _reviewed_equivalent_binding(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = _complete_equivalent_live_positions()
    if sibling_state == "drift":
        client.positions[1]["pos"] = "0.2"
    else:
        client.positions.pop()
    calls = 0
    original_list_positions = client.list_positions

    def list_positions(*, inst_id=None):
        nonlocal calls
        calls += 1
        return original_list_positions(inst_id=inst_id)

    client.list_positions = list_positions

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=_signal(
                session_factory,
                action="close_position",
                payload={"binding_id": binding_id, "pos_id": "pos-1"},
            ),
            deepcoin_client=client,
        )

    assert calls == 0
    assert client.order_payloads == []


@pytest.mark.parametrize("sibling_state", ["drift", "absent"])
def test_tpsl_freezes_when_equivalent_sibling_is_not_current(
    tmp_path, sibling_state
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _reviewed_equivalent_binding(session_factory)
    client = _FakeDeepcoinClient()
    client.positions = _complete_equivalent_live_positions()
    if sibling_state == "drift":
        client.positions[1]["mrgPosition"] = "merge"
    else:
        client.positions.pop()
    calls = 0
    original_list_positions = client.list_positions

    def list_positions(*, inst_id=None):
        nonlocal calls
        calls += 1
        return original_list_positions(inst_id=inst_id)

    client.list_positions = list_positions

    with pytest.raises(
        DeepcoinExecutionActionError, match="live_position_economics_changed"
    ):
        adjust_position_tpsl(
            session_factory,
            trade_signal=_signal(
                session_factory,
                action="adjust_stop_loss",
                payload={
                    "binding_id": binding_id,
                    "pos_id": "pos-1",
                    "stop_loss": 1577.04,
                },
            ),
            deepcoin_client=client,
        )

    assert calls == 1
    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


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
    client.positions = _complete_equivalent_live_positions()

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
    assert client.actual_trigger_order_payloads == []
    assert all(item["instType"] == "SWAP" for item in client.cancel_position_payloads)
    assert [
        (item.get("tpTriggerPx"), item.get("slTriggerPx"), item["sz"])
        for item in client.protection_payloads
    ] == [("1605.6", None, "0.1"), (None, "1577.04", "0.1")]
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


def test_adjust_stop_loss_records_new_tpsl_orders_in_protection_ledger(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.protection_outcomes = [
        {"code": "0", "data": {"ordId": "tp-new-ledger"}},
        {"code": "0", "data": {"ordId": "sl-new-ledger"}},
    ]

    adjust_position_tpsl(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        rows = list_verified_ledger_rows_for_positions(session, ["pos-1"])

    assert [(row.order_id, row.purpose, row.trigger_price) for row in rows] == [
        ("sl-new-ledger", "stop_loss", "1577.04"),
        ("tp-new-ledger", "take_profit", "1605.6"),
    ]
    assert {row.execution_binding_id for row in rows} == {binding_id}
    assert {row.pos_id for row in rows} == {"pos-1"}
    assert {row.evidence_source for row in rows} == {"tpsl_write_response"}


def test_adjust_position_tpsl_preserves_multiple_take_profit_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending[0]["sz"] = "0.04"
    client.trigger_pending.insert(
        1,
        {
            "triggerOrderType": "TPSL",
            "ordId": "tp-old-2",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "posId": "pos-1",
            "tpTriggerPx": "1615.6",
            "tpTriggerPxType": "mark",
            "tpOrdPx": "1615",
            "sz": "0.06",
            "cTime": "1000",
        },
    )
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.pos_id == "pos-1")
            .one()
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP",
            side="long",
            order_id="tp-old-2",
            purpose="take_profit",
            trigger_price="1615.6",
            size_text="0.06",
            status="verified",
            evidence_source="test_exact_owner",
            evidence={},
            seen_at=datetime.now(UTC),
        )
        session.commit()

    adjust_position_tpsl(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
    )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == [
        "tp-old",
        "tp-old-2",
        "sl-old",
    ]
    assert [
        (item.get("tpTriggerPx"), item.get("slTriggerPx"), item["sz"])
        for item in client.protection_payloads
    ] == [
        ("1605.6", None, "0.04"),
        ("1615.6", None, "0.06"),
        (None, "1577.04", "0.1"),
    ]
    assert client.protection_payloads[1]["tpTriggerPxType"] == "mark"
    assert client.protection_payloads[1]["tpOrdPx"] == "1615"


@pytest.mark.parametrize(
    "outcome",
    [DeepcoinRequestOutcomeUnknown("response lost"), {"code": "0", "data": {}}],
    ids=["request_unknown", "success_missing_order_id"],
)
def test_manual_tpsl_unknown_replacement_never_guesses_cancel_or_restore(
    outcome, tmp_path
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.protection_outcomes = [outcome]

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="position_tpsl_replacement_outcome_unknown",
    ):
        adjust_position_tpsl(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == [
        "tp-old",
        "sl-old",
    ]
    assert len(client.protection_payloads) == 1


def test_kol_tpsl_cannot_call_exact_manual_helper_without_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
        source_type="kol_management",
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="automated_position_tpsl_requires_management_batch",
    ):
        adjust_position_tpsl(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


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
    with session_factory() as session:
        session.query(PositionProtectionLedger).delete()
        session.commit()
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

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="no_existing_position_tpsl_to_adjust",
    ):
        adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )

    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_adjust_position_tpsl_accepts_ledger_owned_unscoped_orders(tmp_path):
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


def test_process_trade_signal_live_rejects_legacy_close_without_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=client,
            processed_at=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
        )

    assert client.order_payloads == []
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "active"


def test_process_trade_signal_live_rejects_legacy_multi_position_close(tmp_path):
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

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "active"


def test_legacy_composite_breakeven_management_requires_batch(tmp_path):
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

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        execute_deepcoin_management_signal(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
        )

    assert client.order_payloads == []
    assert client.protection_payloads == []


def test_composite_helper_never_moves_stop_before_close_exchange_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    signal = _signal(
        session_factory,
        action="partial_close_and_move_stop_to_entry",
        payload={"targets": [{"binding_id": binding_id, "fraction": 0.5}]},
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="composite_management_requires_exchange_confirmed_batch_close",
    ):
        partial_close_and_move_stop_to_entry(
            session_factory,
            trade_signal=signal,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_process_trade_signal_live_rejects_legacy_subset_close(tmp_path):
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

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=client,
        )

    assert client.order_payloads == []
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "active"
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.pos_id, leg.status) for leg in legs] == [
        (1, "pos-1", "active"),
        (2, "pos-2", "active"),
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


def test_process_trade_signal_live_recreates_trigger_entry_with_embedded_stop_only(tmp_path):
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
    assert client.trigger_payloads[0]["slTriggerPx"] == "1577.04"
    assert client.trigger_payloads[0]["slTriggerPxType"] == "last"
    assert client.trigger_payloads[0]["slOrdPx"] == "-1"
    assert "posId" not in client.trigger_payloads[0]
    assert not any(key.startswith("tp") for key in client.trigger_payloads[0])
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


def test_deleted_source_blocks_trigger_entry_recreation_before_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "deleted-recreate.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=100,
                message_id=55,
                text="ETH long",
                archived_target_group=True,
            )
        )
        session.commit()
    binding_id = _binding(
        session_factory,
        order_id="trigger-old",
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
            order_id="trigger-old",
            client_order_id="client-old",
            status="open",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="adjust_trigger_entry_tpsl",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
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
            "slTriggerPx": "1567.52",
        }
    ]
    record_source_message_deleted(
        session_factory,
        chat_id=100,
        message_id=55,
    )

    with pytest.raises(DeepcoinExecutionActionError, match="source_message_deleted"):
        process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=client,
        )

    assert client.cancel_trigger_payloads == []
    assert client.trigger_payloads == []


@pytest.mark.parametrize(
    "create_response",
    [
        {"code": "0", "data": {"id": "generic-response-id"}},
        {"code": "0", "data": {}},
    ],
)
def test_recreated_pending_entry_requires_exact_exchange_response_order_id(
    tmp_path, create_response
):
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
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
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
            "slTriggerPx": "1567.52",
        }
    ]

    def trigger_order(payload):
        client.trigger_payloads.append(payload)
        return create_response

    client.trigger_order = trigger_order

    with pytest.raises(
        DeepcoinExecutionActionError,
        match="recreated_trigger_entry_missing_exchange_order_id",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=client,
        )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert binding.order_id == "trigger-old"
    assert binding.last_exchange_status is None
    assert [(leg.order_id, leg.client_order_id, leg.status) for leg in legs] == [
        ("trigger-old", "client-old", "open")
    ]
