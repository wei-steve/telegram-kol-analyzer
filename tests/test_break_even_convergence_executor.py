"""Break-even convergence.

**Phase 6h repaired the defect these tests used to assert.** The preflight
compared ``trigger-orders-pending`` rows against the ledger using
``row["slTriggerPx"]`` and ``row["posId"]``, while that endpoint returns
``slTriggerPrice`` and no position id at all, so every candidate was refused
with ``break_even_existing_stop_drift`` -- matching production, where
``strategy_break_even_convergences`` held two rows and no convergence had ever
succeeded. Both sides (stop and take-profit) now read through the named
readers in :mod:`deepcoin_trigger_rows` and compare as ``Decimal``.

**The TPSL fixtures below carry no ``posId``**, because the venue's do not.
They used to, and that is the whole reason the defect survived: a fixture that
agrees with the code instead of the venue makes a broken read look correct. A
mutation check proved it -- with the invented ``posId`` still in place,
restoring the old ``row["posId"] == pos_id`` requirement left every test
green.

**Fixing the read did not release the write.** Reconnecting a path that has
never once run against the venue is a separate decision from correcting a
field name, so the executor holds at
``BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS``, which names only the positions
explicitly approved -- two ETH longs since phase 6j on 2026-09-11, and nothing
before them had ever been acted on. The
tests that exercise the replacement mechanics run with the position released,
via the ``released_positions`` fixture; the gate itself is covered by its own
pair of tests at the end, in both directions. Do not make that fixture
autouse -- the empty-constant case has to be exercised explicitly, or a
removed gate would look green.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import telegram_kol_research.break_even_convergence_executor as break_even_module

from telegram_kol_research.break_even_convergence_executor import (
    execute_break_even_convergence,
)
from telegram_kol_research.break_even_convergence_planner import (
    plan_or_adopt_break_even_convergence,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionReconciliationObservation,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings


@pytest.fixture
def released_full_exit(monkeypatch):
    """Release the market-close branch, which has its own constant."""

    monkeypatch.setattr(
        break_even_module,
        "BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS",
        frozenset({"pos-1", "pos-2"}),
    )


@pytest.fixture
def released_positions(monkeypatch):
    """Release the positions these fixtures use, for the write-path tests."""

    monkeypatch.setattr(
        break_even_module,
        "BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS",
        frozenset({"pos-1", "pos-2"}),
    )


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)


class TriggerCancelClient:
    def __init__(
        self,
        *,
        unknown=False,
        market_price="62000",
        quote_observed_at=NOW,
        before_cancel_gate=None,
        after_quote=None,
    ):
        self.unknown = unknown
        self.market_price = market_price
        self.quote_observed_at = quote_observed_at
        self.before_cancel_gate = before_cancel_gate
        self.after_quote = after_quote
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
            # A pending *entry* on this endpoint is typed ``Conditional`` by the
            # exchange (ARCHITECTURE section 6). The fixture predated the field;
            # without it the row is untyped, and an untyped row is one the
            # protection chain refuses to classify rather than silently ignore.
            "triggerOrderType": "Conditional",
            "posSide": "short",
            "side": "sell",
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
        if self.before_cancel_gate is not None:
            callback, self.before_cancel_gate = self.before_cancel_gate, None
            callback()
        return []

    def list_positions(self, *, inst_id=None):
        self.calls.append(("list_positions", inst_id))
        return list(self.positions)

    def get_ticker_quote(self, *, inst_id):
        self.calls.append(("get_ticker_quote", inst_id))
        if self.after_quote is not None:
            callback, self.after_quote = self.after_quote, None
            callback()
        return {
            "instrument_id": inst_id,
            "price": self.market_price,
            "price_field": "last",
            "observed_at": self.quote_observed_at.isoformat(),
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
    save_trading_settings(session_factory, {
        "auto_trade_enabled": mode == "live",
        "management_execution_mode": mode,
        "move_stop_to_breakeven_after_tp1": True,
    }, updated_at=NOW)
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
        trigger_evidence={"evidence_tier": "exact_order_terminal", "confirmed_at": NOW.isoformat()},
        strategy_instance_id="deepcoin:1:2:BTC:short",
        planned_at=NOW,
        execution_mode=mode,
    )


def test_live_task_stops_before_any_exchange_call_when_runtime_switch_is_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    save_trading_settings(session_factory, {
        "auto_trade_enabled": False,
        "management_execution_mode": "disabled",
        "move_stop_to_breakeven_after_tp1": True,
    }, updated_at=NOW)
    client = TriggerCancelClient()

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "automatic_break_even_runtime_disabled"
    assert client.calls == []


def test_stale_quote_blocks_protection_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(quote_observed_at=NOW - timedelta(minutes=5))

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "break_even_market_preflight_unavailable"
    assert not any(call[0] in {"set_position_sltp", "place_order"} for call in client.calls)


def test_runtime_switch_is_rechecked_before_deferred_entry_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)

    def disable():
        save_trading_settings(session_factory, {
            "auto_trade_enabled": False,
            "management_execution_mode": "disabled",
            "move_stop_to_breakeven_after_tp1": True,
        }, updated_at=NOW)

    client = TriggerCancelClient(before_cancel_gate=disable)
    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "blocked"
    assert not any(call[0] == "cancel_trigger_order" for call in client.calls)


def test_runtime_switch_is_rechecked_before_position_mutation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)

    def disable():
        save_trading_settings(session_factory, {
            "auto_trade_enabled": False,
            "management_execution_mode": "disabled",
            "move_stop_to_breakeven_after_tp1": True,
        }, updated_at=NOW)

    client = TriggerCancelClient(after_quote=disable)
    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "blocked"
    assert not any(
        call[0] in {"set_position_sltp", "cancel_position_sltp", "place_order"}
        for call in client.calls
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
    assert client.calls[2] == ("list_positions", "BTC-USDT-SWAP")
    assert client.calls[3] == ("cancel_trigger_order", "entry-2")
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


def test_short_leg_below_cost_adds_exact_break_even_stop_and_completes(released_positions, tmp_path):
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


def test_short_leg_crossed_cost_is_closed_by_exact_position_id(released_full_exit, tmp_path):
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
        # Venue-shaped: a TPSL row carries no posId and spells the price
        # slTriggerPrice. It used to carry both of the position row's spellings
        # here, which is how the executor's broken read kept passing.
        "ordId": "tight-stop",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "slTriggerPrice": "62900",
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


def test_each_live_leg_uses_its_own_exchange_average_price(released_positions, tmp_path):
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


def test_break_even_stop_is_confirmed_before_weaker_stop_cancel_and_tps_stay(released_positions, tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.extend([
        {
            "ordId": "weak-stop",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "slTriggerPrice": "63500",
            "sz": "5",
        },
        {
            "ordId": "tp-remaining",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "tpTriggerPrice": "61000",
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
    # The A-5e ordering, which is the property this test exists for: the new
    # stop is placed and confirmed *before* the weaker one is cancelled, so
    # there is no instant in which the position carries neither.
    position_writes = [
        call[0] for call in client.calls
        if call[0] in {"set_position_sltp", "cancel_position_sltp"}
    ]
    assert position_writes[0] == "set_position_sltp"
    assert position_writes.index("set_position_sltp") < position_writes.index(
        "cancel_position_sltp"
    )
    assert not any(row.get("ordId") == "weak-stop" for row in client.orders)
    assert any(row.get("ordId") == "tp-remaining" for row in client.orders)


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


def test_unknown_old_stop_cancel_keeps_new_stop_and_requires_recovery(released_positions, tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.append({
        "ordId": "weak-stop",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "slTriggerPrice": "63500",
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

    # The cancel's outcome is unknown, so the new stop stays and a person is
    # required. Not "blocked": a blocked convergence claims nothing happened,
    # and something did -- the replacement stop is on the exchange.
    assert result.status == "recovery_required"
    first_write_count = len([
        call for call in client.calls if call[0] == "set_position_sltp"
    ])
    assert first_write_count == 1
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


def test_a_cancel_the_exchange_did_not_honour_is_not_a_finished_break_even(released_positions, tmp_path):
    """Phase 6a: the cancel is now proven, not assumed.

    Before, an accepted cancel response ended the leg. An exchange that answers
    "ok" and keeps the order armed left a weaker stop live while the ledger
    said it was retired -- and the leg reported success.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = TriggerCancelClient(market_price="62000")
    client.orders.append({
        "ordId": "weak-stop",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "slTriggerPrice": "63500",
        "sz": "5",
    })

    def accepted_but_not_honoured(payload):
        client.calls.append(("cancel_position_sltp", dict(payload)))
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    client.cancel_position_sltp = accepted_but_not_honoured
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

    # The venue accepted the cancel and the order is still there on read-back.
    # Not a finished break-even, and not a failure either: the new stop exists,
    # so the position is over-protected rather than bare, and a person decides.
    # The ledger must NOT retire an order still resting on the venue.
    assert result.status == "recovery_required"
    with session_factory() as session:
        retired = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id="weak-stop")
            .one()
        )
        incidents = [
            row.incident_type
            for row in session.query(PositionProtectionIncident).all()
        ]
    assert retired.status == "verified"
    assert incidents != []


# ---------------------------------------------------------------------------
# Phase 6h: the release gate. Correcting the preflight reconnected a path that
# has never once run against the venue; releasing it is a separate decision,
# and in production the set is empty.
# ---------------------------------------------------------------------------


def _closes(client):
    """A market close arrives as place_order carrying closePosId."""

    return [
        call for call in client.calls
        if call[0] == "place_order" and "closePosId" in (call[1] or {})
    ]


def _position_writes(client):
    return [
        call[0] for call in client.calls
        if call[0] in {"set_position_sltp", "cancel_position_sltp"}
    ]


def _seed_break_even_ready_client(session_factory):
    """A convergence whose every check passes, so only the gate can stop it.

    The TPSL rows are venue-shaped: no ``posId``, price as ``slTriggerPrice``.
    """

    client = TriggerCancelClient(market_price="62000")
    client.orders.extend([
        {
            "ordId": "weak-stop", "instId": "BTC-USDT-SWAP", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPrice": "63500", "sz": "5",
        },
        {
            "ordId": "tp-remaining", "instId": "BTC-USDT-SWAP", "posSide": "short",
            "triggerOrderType": "TPSL", "tpTriggerPrice": "61000", "sz": "5",
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
                pos_id="pos-1", instrument_id="BTC-USDT-SWAP", side="short",
                order_id=order_id, purpose=purpose, trigger_price=price,
                size_text="5", status="verified", evidence_source="test",
                evidence_json="{}",
            ))
        session.commit()
    return client


def test_an_unreleased_position_computes_the_replacement_and_sends_none(tmp_path):
    """The production default. Everything decided, nothing written.

    Deliberately not using ``released_positions``: this is the one case that
    must run against the real constant, or a gate removed by accident would
    never be noticed.

    Asserts that *this fixture's* position is absent rather than that the
    constant is empty. The constant stopped being empty on 2026-09-11 when the
    user released the two live positions, and a test pinned to emptiness would
    have had to be loosened on that day -- turning a released position into a
    reason to weaken the gate's own test. What this case needs is an
    unreleased position, which is a property it can keep forever.
    """

    assert "pos-1" not in break_even_module.BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = _seed_break_even_ready_client(session_factory)

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert _position_writes(client) == []
    # Not "completed": nothing was replaced, so claiming convergence would be a
    # false statement about the exchange -- the weaker stop is still in force.
    assert result.status == "blocked"
    assert result.reason_code == "break_even_replacement_not_released"
    with session_factory() as session:
        held = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "break_even_would_replace")
            .all()
        )
    # The record has to be reviewable on its own: which position, the price it
    # would move to, and the exact orders it would cancel.
    assert len(held) == 1
    payload = json.loads(held[0].after_json)
    assert payload["pos_id"] == "pos-1"
    assert payload["target_stop_price"]
    assert payload["would_cancel_order_ids"] == ["weak-stop"]
    assert payload["released"] is False


def test_a_released_position_reaches_the_replacement(released_positions, tmp_path):
    """The other direction, so the gate is not merely "never writes".

    Without this the constant could be hard-wired shut and every test above
    would still pass while phase 6h did nothing.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = _seed_break_even_ready_client(session_factory)

    execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert "set_position_sltp" in _position_writes(client)
    with session_factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "break_even_would_replace")
            .count()
            == 0
        )


def _seed_full_exit_ready_client(session_factory):
    """A convergence whose market has crossed back through the entry price.

    Same seed as the replacement case, but the quote sits on the losing side,
    so the policy answers ``full_exit`` instead of ``set_break_even``. Which
    branch a live convergence takes is decided by where the market happens to
    be at that instant -- observed flipping on production data within one
    minute on 2026-09-10 -- so both branches need their own gate and both
    gates need their own pair of tests.
    """

    client = TriggerCancelClient(market_price="64000")
    client.orders.append({
        "ordId": "weak-stop", "instId": "BTC-USDT-SWAP", "posSide": "short",
        "triggerOrderType": "TPSL", "slTriggerPrice": "63500", "sz": "5",
    })
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-1").one()
        session.add(PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1", instrument_id="BTC-USDT-SWAP", side="short",
            order_id="weak-stop", purpose="stop_loss", trigger_price="63500",
            size_text="5", status="verified", evidence_source="test",
            evidence_json="{}",
        ))
        session.commit()
    return client


def test_an_unreleased_full_exit_records_the_close_and_sends_none(tmp_path):
    """A market close is a different decision from moving a stop, gated apart.

    Runs against the real constant on purpose: this is the branch that ends
    the position, and a gate removed by accident here costs more than one
    removed on the replacement side.
    """

    assert break_even_module.BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS == frozenset()

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = _seed_full_exit_ready_client(session_factory)

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert _position_writes(client) == []
    assert _closes(client) == []
    assert result.status == "blocked"
    assert result.reason_code == "break_even_full_exit_not_released"
    with session_factory() as session:
        held = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "break_even_would_close")
            .all()
        )
    assert len(held) == 1
    payload = json.loads(held[0].after_json)
    assert payload["pos_id"] == "pos-1"
    assert payload["ord_type"] == "market"
    assert payload["endpoint"] == "close_position"
    # The fact that is not visible from the payload: this branch cancels
    # nothing first, so the stops on the position are untouched by it.
    assert payload["cancels_stops_first"] is False
    assert payload["released"] is False


def test_a_released_full_exit_reaches_the_close(released_full_exit, tmp_path):
    """The other direction, so the full-exit gate is not merely "never closes"."""

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = _seed_full_exit_ready_client(session_factory)

    execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    closes = _closes(client)
    assert len(closes) == 1
    assert closes[0][1]["closePosId"] == "pos-1"
    assert closes[0][1]["ordType"] == "market"
    with session_factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "break_even_would_close")
            .count()
            == 0
        )


def test_the_two_gates_are_independent(tmp_path, monkeypatch):
    """Releasing the replacement must not release the close.

    They came from one decision -- the market policy picks the branch -- so a
    single constant would have let approving "move the stop" quietly approve
    "close the position" as well.
    """

    monkeypatch.setattr(
        break_even_module,
        "BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS",
        frozenset({"pos-1", "pos-2"}),
    )
    assert break_even_module.BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS == frozenset()

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence = _seed_convergence(session_factory)
    client = _seed_full_exit_ready_client(session_factory)

    result = execute_break_even_convergence(
        session_factory,
        convergence_id=convergence.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert _closes(client) == []
    assert result.reason_code == "break_even_full_exit_not_released"


def test_the_two_release_constants_are_declared_independently():
    """Source-level, because aliasing them cannot be caught at runtime.

    ``BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS = BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS``
    behaves identically in every test that rebinds the names -- monkeypatch
    replaces one name and the other keeps pointing at the original object --
    so the runtime independence test above stays green. The risk it misses is
    an editing risk: with the names aliased, adding a position id to the
    replacement set silently releases the market close for it too. That is a
    property of the text, so the text is what this reads.

    Note what this can and cannot do. It guards the text, not the behaviour: an
    equivalent rewrite that keeps the two independent would fail it wrongly,
    and it cannot see an alias built at runtime. It is a weaker instrument
    than the behavioural cases above and is here only for the one failure they
    structurally cannot reach.
    """

    import inspect

    source = inspect.getsource(break_even_module)
    # Neither declaration may be written in terms of the other. Asserted this
    # way rather than as "both are empty", because the replacement set stopped
    # being empty when the user released two positions, while the property
    # that matters -- releasing one branch never releases the other -- did not
    # change that day and must not be weakened by it.
    for name, other in (
        ("BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS",
         "BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS"),
        ("BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS",
         "BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS"),
    ):
        declaration = next(
            line for line in source.splitlines()
            if line.startswith(f"{name}:") or line.startswith(f"{name} =")
        )
        assert other not in declaration, (
            f"{name} must not be declared in terms of {other}; releasing one "
            "branch must never release the other"
        )
    # And the close branch specifically is still unreleased: the user approved
    # the replacement on 2026-09-11 and did not approve this one.
    assert break_even_module.BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS == frozenset()
