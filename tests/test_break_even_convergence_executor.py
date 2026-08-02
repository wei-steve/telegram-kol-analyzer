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
    PositionReconciliationObservation,
    StrategyBreakEvenConvergence,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)


class TriggerCancelClient:
    def __init__(self, *, unknown=False):
        self.unknown = unknown
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
        self.orders = []
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


def _seed_convergence(session_factory, *, mode="live"):
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


def test_shadow_convergence_never_calls_exchange_cancel(tmp_path):
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
    assert client.calls == []
    with session_factory() as session:
        assert session.get(StrategyBreakEvenConvergence, convergence.id).status == (
            "shadow_planned"
        )
