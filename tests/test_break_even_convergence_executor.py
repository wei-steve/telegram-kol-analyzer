import json
from datetime import UTC, datetime

from telegram_kol_research.break_even_convergence_executor import (
    execute_break_even_convergence,
)
from telegram_kol_research.break_even_convergence_planner import (
    plan_or_adopt_break_even_convergence,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionReconciliationObservation,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)


class TriggerCancelClient:
    def __init__(self, *, unknown=False, market_price="62000"):
        self.unknown = unknown
        self.market_price = market_price
        self.positions = [{
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "5",
            "avgPx": "63000",
            "mgnMode": "cross",
            "mrgPosition": "split",
        }]
        self.orders = [{
            "ordId": "entry-2",
            "clOrdId": "client-2",
            "instId": "BTC-USDT-SWAP",
        }]
        self.history_state = None
        self.calls = []

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(("list_trigger_orders_pending", inst_id))
        return list(self.orders)

    def list_open_orders(self, *, inst_id):
        self.calls.append(("list_open_orders", inst_id))
        return []

    def cancel_trigger_order(self, payload):
        self.calls.append(("cancel_trigger_order", payload["ordId"]))
        if self.unknown:
            raise RuntimeError("unknown")
        self.orders = [
            row for row in self.orders
            if row.get("ordId") != payload["ordId"]
        ]
        self.history_state = "canceled"
        return {"code": "0"}

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        if self.history_state is None:
            return []
        return [{"ordId": "entry-2", "clOrdId": "client-2", "state": self.history_state}]

    def list_trade_fills(self, *, inst_id=None):
        return []

    def list_positions(self, *, inst_id=None):
        self.calls.append(("list_positions", inst_id))
        return list(self.positions)

    def get_ticker_quote(self, *, inst_id):
        self.calls.append(("get_ticker_quote", inst_id))
        return {
            "instrument_id": inst_id,
            "price": self.market_price,
            "price_field": "last",
        }

    def set_position_sltp(self, payload):
        self.calls.append(("set_position_sltp", dict(payload)))
        order_id = f"be-stop-{len([call for call in self.calls if call[0] == 'set_position_sltp'])}"
        row = {
            "ordId": order_id,
            "instId": payload["instId"],
            "posId": payload["posId"],
            "posSide": payload["posSide"],
            "slTriggerPx": payload["slTriggerPx"],
            "sz": payload.get("sz"),
        }
        self.orders.append(row)
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_position_sltp(self, payload):
        self.calls.append(("cancel_position_sltp", dict(payload)))
        self.orders = [
            row for row in self.orders
            if row.get("ordId") != payload["ordId"]
        ]
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def place_order(self, payload):
        self.calls.append(("place_order", dict(payload)))
        self.positions = [
            row for row in self.positions
            if row["posId"] != payload["closePosId"]
        ]
        return {"code": "0", "data": {"ordId": "close-1"}}


def _seed_convergence(session_factory, *, mode="live", second_live=False):
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:BTC:short",
            kol_id="group:1",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-1",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.add_all([
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id="pos-1",
                pos_id="pos-1",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            ),
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-2",
                client_order_id="client-2",
                venue="deepcoin",
                attribution_status="unassigned",
                status="pending",
            ),
        ])
        if second_live:
            session.add(ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=3,
                purpose="entry",
                order_kind="market",
                order_id="pos-2",
                pos_id="pos-2",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            ))
        session.flush()
        live_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-1").one()
        session.add(PositionReconciliationObservation(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=live_leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="pos-1",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            size_text="5",
            avg_entry_price="63000",
            pending_tpsl_json="[]",
            snapshot_complete=True,
            snapshot_fingerprint="a" * 64,
            observed_at=NOW,
        ))
        if second_live:
            second_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-2").one()
            session.add(PositionReconciliationObservation(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=second_leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-2",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                size_text="2",
                avg_entry_price="62500",
                pending_tpsl_json="[]",
                snapshot_complete=True,
                snapshot_fingerprint="b" * 64,
                observed_at=NOW,
            ))
        session.commit()
    return plan_or_adopt_break_even_convergence(
        session_factory,
        trigger_type="tp1_fill",
        trigger_identity="tp-1",
        trigger_evidence={"evidence_tier": "exact_order_terminal"},
        strategy_instance_id="deepcoin:1:2:BTC:short",
        planned_at=NOW,
        execution_mode=mode,
    )


def test_live_convergence_cancels_deferred_entry_before_market_decision(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
        stop_after_deferred_cleanup=True,
    )

    assert result.status == "deciding_by_market"
    assert client.calls[2] == ("cancel_trigger_order", "entry-2")
    with session_factory() as session:
        deferred = session.query(ExecutionOrderLeg).filter_by(order_id="entry-2").one()
        assert deferred.status == "cancelled"


def test_unknown_deferred_cancel_enters_recovery_without_further_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(unknown=True)

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
        stop_after_deferred_cleanup=True,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "deferred_entry_cancel_outcome_unknown"
    assert [call for call in client.calls if call[0] == "cancel_trigger_order"] == [
        ("cancel_trigger_order", "entry-2")
    ]


def test_shadow_convergence_reads_and_decides_but_never_writes_exchange(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory, mode="shadow")
    client = TriggerCancelClient()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
        stop_after_deferred_cleanup=True,
    )

    assert result.status == "shadow_planned"
    assert not [
        call for call in client.calls
        if call[0] in {
            "cancel_trigger_order",
            "set_position_sltp",
            "cancel_position_sltp",
            "place_order",
        }
    ]
    with session_factory() as session:
        assert session.get(StrategyBreakEvenConvergence, convergence.id).status == (
            "shadow_planned"
        )
        leg = session.query(StrategyBreakEvenConvergenceLeg).one()
        assert json.loads(leg.decision_json)["action"] == "set_break_even"


def test_short_leg_below_cost_adds_exact_break_even_stop_and_completes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "completed"
    writes = [call for call in client.calls if call[0] == "set_position_sltp"]
    assert len(writes) == 1
    assert writes[0][1]["posId"] == "pos-1"
    assert writes[0][1]["slTriggerPx"] == "63000"
    assert writes[0][1]["sz"] == "5"
    with session_factory() as session:
        leg = session.query(StrategyBreakEvenConvergenceLeg).one()
        assert json.loads(leg.decision_json)["action"] == "set_break_even"
        assert leg.status == "succeeded"
        intent = session.query(PositionMutationIntent).one()
        assert intent.operation == "set_position_sltp"
        assert intent.status == "confirmed"


def test_short_leg_crossed_cost_is_closed_by_exact_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="64000")

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "completed"
    closes = [call for call in client.calls if call[0] == "place_order"]
    assert len(closes) == 1
    assert closes[0][1]["closePosId"] == "pos-1"
    assert closes[0][1]["sz"] == "5"
    assert not [call for call in client.calls if call[0] == "set_position_sltp"]


def test_existing_tighter_short_stop_is_kept_without_position_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.append({
        "ordId": "tight-stop",
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-1",
        "posSide": "short",
        "slTriggerPx": "62900",
        "sz": "5",
    })
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-1").one()
        session.add(PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="tight-stop",
            purpose="stop_loss",
            trigger_price="62900",
            size_text="5",
            status="verified",
            evidence_source="test",
            evidence_json="{}",
        ))
        session.commit()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "completed"
    assert not [
        call for call in client.calls
        if call[0] in {"set_position_sltp", "place_order"}
    ]
    with session_factory() as session:
        leg = session.query(StrategyBreakEvenConvergenceLeg).one()
        assert json.loads(leg.decision_json)["action"] == "keep_tighter_stop"


def test_each_live_leg_uses_its_own_exchange_average_price(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory, second_live=True)
    client = TriggerCancelClient(market_price="62000")
    client.positions.append({
        "posId": "pos-2",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "pos": "2",
        "avgPx": "62500",
        "mgnMode": "cross",
        "mrgPosition": "split",
    })

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "completed"
    writes = [call[1] for call in client.calls if call[0] == "set_position_sltp"]
    assert {(row["posId"], row["slTriggerPx"], row["sz"]) for row in writes} == {
        ("pos-1", "63000", "5"),
        ("pos-2", "62500", "2"),
    }


def test_break_even_stop_is_confirmed_before_weaker_stop_cancel_and_tps_stay(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.extend([
        {
            "ordId": "weak-stop",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "short",
            "slTriggerPx": "63500",
            "sz": "5",
        },
        {
            "ordId": "tp-remaining",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "short",
            "tpTriggerPx": "61000",
            "sz": "5",
        },
    ])
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-1").one()
        for order_id, purpose, price in [
            ("weak-stop", "stop_loss", "63500"),
            ("tp-remaining", "take_profit", "61000"),
        ]:
            session.add(PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=leg.execution_binding_id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=leg.strategy_instance_id,
                pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                order_id=order_id,
                purpose=purpose,
                trigger_price=price,
                size_text="5",
                status="verified",
                evidence_source="test",
                evidence_json="{}",
            ))
        session.commit()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "completed"
    position_writes = [
        call[0] for call in client.calls
        if call[0] in {"set_position_sltp", "cancel_position_sltp"}
    ]
    assert position_writes == ["set_position_sltp", "cancel_position_sltp"]
    assert any(row.get("ordId") == "tp-remaining" for row in client.orders)
    assert not any(row.get("ordId") == "weak-stop" for row in client.orders)


def test_untrusted_quote_blocks_all_position_mutations(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")

    def untrusted_quote(*, inst_id):
        return {
            "instrument_id": inst_id,
            "price": "62000",
            "price_field": "markPx",
        }

    client.get_ticker_quote = untrusted_quote
    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "break_even_market_preflight_unavailable"
    assert not [
        call for call in client.calls
        if call[0] in {
            "set_position_sltp",
            "cancel_position_sltp",
            "place_order",
        }
    ]


def test_unknown_old_stop_cancel_keeps_new_stop_and_requires_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.append({
        "ordId": "weak-stop",
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-1",
        "posSide": "short",
        "slTriggerPx": "63500",
        "sz": "5",
    })

    def unknown_cancel(payload):
        client.calls.append(("cancel_position_sltp", dict(payload)))
        raise RuntimeError("unknown")

    client.cancel_position_sltp = unknown_cancel
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-1").one()
        session.add(PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            order_id="weak-stop",
            purpose="stop_loss",
            trigger_price="63500",
            size_text="5",
            status="verified",
            evidence_source="test",
            evidence_json="{}",
        ))
        session.commit()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert any(row.get("ordId") == "be-stop-1" for row in client.orders)
    first_write_count = len([
        call for call in client.calls if call[0] == "set_position_sltp"
    ])
    repeated = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    assert repeated.status == "recovery_required"
    assert len([
        call for call in client.calls if call[0] == "set_position_sltp"
    ]) == first_write_count
