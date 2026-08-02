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
    PositionProtectionLedger,
    PositionReconciliationObservation,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 2, 13, 0, tzinfo=UTC)


class ReadOnlyDabiaokeClient:
    def __init__(self):
        self.calls = []
        self.positions = [{
            "posId": "dabiaoke-short-market",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "5",
            "avgPx": "63076.7",
            "mgnMode": "cross",
            "mrgPosition": "split",
        }]
        self.pending = [
            {
                "ordId": "tp-2",
                "instId": "BTC-USDT-SWAP",
                "posId": "dabiaoke-short-market",
                "posSide": "short",
                "tpTriggerPx": "61700",
                "sz": "3",
            },
            {
                "ordId": "tp-3",
                "instId": "BTC-USDT-SWAP",
                "posId": "dabiaoke-short-market",
                "posSide": "short",
                "tpTriggerPx": "61000",
                "sz": "2",
            },
        ]

    def list_positions(self, *, inst_id=None):
        self.calls.append("list_positions")
        return list(self.positions)

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append("list_trigger_orders_pending")
        return list(self.pending)

    def get_ticker_quote(self, *, inst_id):
        self.calls.append("get_ticker_quote")
        return {
            "instrument_id": inst_id,
            "price": "63461.2",
            "price_field": "last",
            "observed_at": NOW.isoformat(),
        }


def test_dabiaoke_4163_tp1_fill_shadow_decides_exact_full_exit(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    save_trading_settings(sf, {
        "auto_trade_enabled": False,
        "management_execution_mode": "shadow",
        "move_stop_to_breakeven_after_tp1": True,
    }, updated_at=NOW)
    with sf() as session:
        binding = ExecutionBinding(
            strategy_instance_id="dabiaoke:4163:BTC:short",
            kol_id="大镖客",
            chat_id=-4163,
            message_id=4163,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
            pos_id="dabiaoke-short-market",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-4163,
            message_id=4163,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        market_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="dabiaoke-short-market",
            pos_id="dabiaoke-short-market",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        deferred_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="entry-63910",
            pos_id=None,
            venue="deepcoin",
            attribution_status="unassigned",
            status="pending",
            request_json='{"sz":"17","triggerPx":"63910"}',
        )
        session.add_all([market_leg, deferred_leg])
        session.flush()
        session.add(PositionReconciliationObservation(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=market_leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="dabiaoke-short-market",
            instrument_id="BTC-USDT-SWAP",
            side="short",
            size_text="5",
            avg_entry_price="63076.7",
            pending_tpsl_json=json.dumps([
                {"order_id": "tp-2", "trigger_price": "61700", "size_text": "3"},
                {"order_id": "tp-3", "trigger_price": "61000", "size_text": "2"},
            ], sort_keys=True),
            snapshot_complete=True,
            snapshot_fingerprint="4" * 64,
            observed_at=NOW,
        ))
        for order_id, price, size in [
            ("tp-2", "61700", "3"),
            ("tp-3", "61000", "2"),
        ]:
            session.add(PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=market_leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="dabiaoke-short-market",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                order_id=order_id,
                purpose="take_profit",
                trigger_price=price,
                size_text=size,
                status="verified",
                evidence_source="regression",
                evidence_json="{}",
            ))
        session.commit()

    convergence = plan_or_adopt_break_even_convergence(
        sf,
        trigger_type="tp1_fill",
        trigger_identity="tp-1@62400",
        trigger_evidence={
            "evidence_tier": "exact_order_terminal",
            "trigger_order_id": "tp-1",
            "trigger_price": "62400",
            "filled_size": "5",
            "confirmed_at": NOW.isoformat(),
        },
        strategy_instance_id="dabiaoke:4163:BTC:short",
        planned_at=NOW,
        execution_mode="shadow",
    )
    client = ReadOnlyDabiaokeClient()

    result = execute_break_even_convergence(
        sf,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "shadow_planned"
    target = json.loads(convergence.target_snapshot_json)
    assert target["deferred_entry_leg_ids"]
    assert target["deferred_entries"][0]["order_id"] == "entry-63910"
    with sf() as session:
        leg = session.query(StrategyBreakEvenConvergenceLeg).one()
        decision = json.loads(leg.decision_json)
        assert decision["entry_price"] == "63076.7"
        assert decision["market_price"] == "63461.2"
        assert decision["action"] == "full_exit"
        assert leg.status == "shadow_planned"
    assert client.calls == [
        "list_positions",
        "list_trigger_orders_pending",
        "get_ticker_quote",
    ]
