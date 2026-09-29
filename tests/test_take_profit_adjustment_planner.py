"""Take-profit adjustment: the planner half, and the fixtures both halves share.

Design: docs/plans/2026-09-29-take-profit-adjustment-design.md. Every exchange
call goes to an in-memory fake; nothing here reaches a network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research import strategy_management_planner as planner
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionTakeProfitOrder,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementNotification,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.trading_settings import save_trading_settings

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
TEXT_18199 = (
    "比特币浮盈中，今晚暂时没有其他操作了，提前设置一下止盈位两个止盈位各50%，"
    "再次强调不要取整数不容易触发，上下几十点浮动都正常。\n@Tarderfengge QQ:158241758"
)
TEXT_13848 = (
    "🔥设置好止盈止损持仓过夜！🔥\n止盈位：73070！！！\n止损位：78700！！！\n"
    "@Tarderfengge QQ:158241758"
)
TEXT_18294 = (
    "比特币空单小级别关注84000-84400附近有支撑，如果是北美地区的朋友目前已经晚间了，"
    "可以提前设置一个止盈点位，至少50%自动止盈，做短线也可以提前设置好止盈位。"
)
TEXT_19670 = "空单已经入场，最高上冲84300附近，有入场可以继续轻仓持有，82500附近可以止盈30%先"


@pytest.fixture(autouse=True)
def _fixed_stop_gate_clock(monkeypatch):
    from telegram_kol_research import management_stop_price_gate as gate

    monkeypatch.setattr(gate, "_stop_check_now", lambda: NOW)


class _Specs:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class FakeExchange:
    """Positions, pending TPSL orders and a last price; writes mutate them."""

    def __init__(self, *, positions, pending, last="85000", side="short"):
        self.positions = [dict(row) for row in positions]
        self.pending = [dict(row) for row in pending]
        self.last = last
        self.side = side
        self.writes: list[tuple[str, dict]] = []
        self.fail_set_prices: set[str] = set()
        self.fail_cancel_ids: set[str] = set()
        self._next = 0

    # reads
    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self.positions if inst_id in (None, row["instId"])]

    def list_trigger_orders_pending(self, *, inst_id):
        return [dict(row) for row in self.pending if row["instId"] == inst_id]

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": self.list_trigger_orders_pending(inst_id=inst_id)}

    def list_open_orders(self, *, inst_id=None):
        return []

    def get_ticker_quote(self, *, inst_id):
        return {
            "instrument_id": inst_id,
            "price": self.last,
            "price_field": "last",
            "observed_at": NOW.isoformat(),
        }

    # writes
    def set_position_sltp(self, payload):
        self.writes.append(("set_position_sltp", dict(payload)))
        price = payload.get("tpTriggerPx") or payload.get("slTriggerPx")
        if str(price) in self.fail_set_prices:
            from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection

            raise DeepcoinDefiniteRejection("rejected by fake")
        self._next += 1
        order_id = f"new-{self._next}"
        row = {
            "ordId": order_id,
            "instId": payload["instId"],
            "posSide": payload["posSide"],
            "triggerOrderType": "TPSL",
            "sz": payload.get("sz", "0"),
        }
        if payload.get("tpTriggerPx"):
            row.update({"tpTriggerPx": payload["tpTriggerPx"], "tpOrdPx": "-1"})
        else:
            row.update({"slTriggerPx": payload["slTriggerPx"], "slOrdPx": "-1"})
        self.pending.append(row)
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_position_sltp(self, payload):
        self.writes.append(("cancel_position_sltp", dict(payload)))
        if str(payload["ordId"]) in self.fail_cancel_ids:
            raise RuntimeError("cancel failed in fake")
        self.pending = [row for row in self.pending if row["ordId"] != payload["ordId"]]
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def place_order(self, payload):  # a market close would land here
        self.writes.append(("place_order", dict(payload)))
        raise AssertionError("take-profit adjustment must never close at market")

    def take_profits(self):
        return sorted(
            (row["tpTriggerPx"], row["sz"]) for row in self.pending if row.get("tpTriggerPx")
        )

    def stops(self):
        return sorted(row["slTriggerPx"] for row in self.pending if row.get("slTriggerPx"))


def _tp_row(order_id, price, size, side="short"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": side,
        "triggerOrderType": "TPSL",
        "tpTriggerPx": price,
        "tpOrdPx": "-1",
        "sz": size,
    }


def _sl_row(order_id, price, side="short"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": side,
        "triggerOrderType": "TPSL",
        "slTriggerPx": price,
        "slOrdPx": "-1",
        "sz": "0",
    }


def _position(pos_id, size, side="short"):
    return {
        "instId": INST,
        "posId": pos_id,
        "posSide": side,
        "pos": size,
        "avgPx": "86000" if side == "short" else "78000",
        "mgnMode": "cross",
        "posMode": "split",
        "cTime": "1721000000000",
    }


def _seed(
    session_factory,
    *,
    text,
    side="short",
    positions=(("pos-1", "15", "84000:7,82000:8"),),
    stop="87000",
    strategy_take_profit="84000/82000",
    lifecycle_event=None,
):
    """One entered strategy, its binding and legs, and one management message.

    ``positions``: ``(pos_id, size, "price:size,price:size")`` -- the take
    profits each position carries, with ledger, audit, logical legs and a
    finished convergence, the way an automatic entry leaves them.
    """

    exchange_pending = []
    with session_factory() as session:
        entry = RawMessage(chat_id=100, message_id=20, posted_at=NOW, text="BTC short")
        message = RawMessage(chat_id=100, message_id=30, posted_at=NOW, text=text)
        session.add_all([entry, message])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side=side,
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=1),
            stop_loss=float(stop),
            take_profit=strategy_take_profit,
        )
        session.add(lifecycle)
        session.flush()
        strategy_instance_id = f"deepcoin:100:20:BTC:{side}"
        binding = ExecutionBinding(
            strategy_instance_id=strategy_instance_id,
            kol_id="kol",
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side=side,
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id=",".join(pos_id for pos_id, _, _ in positions),
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        for index, (pos_id, size, take_profits) in enumerate(positions):
            leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="market",
                order_id=pos_id,
                pos_id=pos_id,
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json=json.dumps(
                    {"policy_version": 2, "source": "direct_order_identity"}
                ),
                status="active",
            )
            session.add(leg)
            session.flush()
            stop_id = f"{pos_id}-sl"
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=strategy_instance_id,
                pos_id=pos_id,
                instrument_id=INST,
                side=side,
                order_id=stop_id,
                purpose="stop_loss",
                trigger_price=stop,
                size_text="0",
                status="verified",
                evidence_source="test",
                evidence={},
                seen_at=NOW,
            )
            exchange_pending.append(_sl_row(stop_id, stop, side))
            plan = []
            for tier, spec in enumerate(filter(None, take_profits.split(",")), start=1):
                price, tp_size = spec.split(":")
                order_id = f"{pos_id}-tp{tier}"
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=strategy_instance_id,
                    pos_id=pos_id,
                    instrument_id=INST,
                    side=side,
                    order_id=order_id,
                    purpose="take_profit",
                    trigger_price=price,
                    size_text=tp_size,
                    status="verified",
                    evidence_source="test",
                    evidence={},
                    seen_at=NOW,
                )
                session.add(
                    PositionTakeProfitOrder(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=leg.id,
                        pos_id=pos_id,
                        order_id=order_id,
                        trigger_price=price,
                        size_text=tp_size,
                        status="active",
                    )
                )
                session.add(
                    PositionProtectionLeg(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=leg.id,
                        role="take_profit",
                        leg_index=tier,
                        planned_trigger_price=price,
                        planned_size=tp_size,
                        pos_id=pos_id,
                        exchange_order_id=order_id,
                        status="verified",
                    )
                )
                plan.append((price, tp_size))
                exchange_pending.append(_tp_row(order_id, price, tp_size, side))
            if plan:
                total = sum(int(tp_size) for _, tp_size in plan)
                session.add(
                    TriggerTakeProfitConvergence(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=leg.id,
                        desired_take_profits_json=json.dumps(
                            [
                                {"allocation_pct": str(int(tp_size) * 100 // total), "price": price}
                                for price, tp_size in plan
                            ]
                        ),
                        status="submitted",
                        pos_id=pos_id,
                    )
                )
        session.add(
            RecognitionDecision(
                raw_message_id=message.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(
                    {
                        "lifecycle_event": lifecycle_event
                        or {
                            "event_type": "position_update",
                            "management_action": "set_take_profit_plan",
                            "target_lifecycle_id": lifecycle.id,
                        },
                        "input_reading": {"observed_text": text},
                    },
                    ensure_ascii=False,
                ),
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=message.id,
                symbol="BTC",
                side=side,
                event_type="position_update",
                target_lifecycle_id=lifecycle.id,
                management_action="adjust_take_profit",
                recognition_generation="generation-1",
                stop_loss_text=stop,
                stop_price_source=None,
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        ids = {
            "raw_message_id": message.id,
            "lifecycle_id": lifecycle.id,
            "binding_id": binding.id,
            "strategy_instance_id": strategy_instance_id,
        }
    exchange = FakeExchange(
        positions=[_position(pos_id, size, side) for pos_id, size, _ in positions],
        pending=exchange_pending,
        side=side,
    )
    return ids, exchange


@pytest.fixture
def session_factory(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(factory, {"take_profit_adjust_mode": "live"})
    return factory


@pytest.fixture(autouse=True)
def _no_reconciliation(monkeypatch):
    monkeypatch.setattr(
        planner, "reconcile_deepcoin_execution_bindings", lambda *args, **kwargs: None
    )


def _plan(session_factory, ids, exchange, *, now=NOW):
    return planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=ids["raw_message_id"],
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        planned_at=now,
        execution_mode="live",
    )


def _batch(session_factory, batch_id):
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        session.expunge(batch)
        return batch


def _notifications(session_factory, batch_id):
    with session_factory() as session:
        return [
            json.loads(row.payload_json)
            for row in session.query(StrategyManagementNotification)
            .filter(StrategyManagementNotification.management_batch_id == batch_id)
            .order_by(StrategyManagementNotification.id.asc())
        ]


def _convergence_plan(session_factory, pos_id="pos-1"):
    with session_factory() as session:
        row = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.pos_id == pos_id)
            .one()
        )
        return json.loads(row.desired_take_profits_json), row.status


# ---------------------------------------------------------------- planning


def test_18199_plans_an_adjustment_batch_with_a_180_second_deadline(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_18199)

    result = _plan(session_factory, ids, exchange)

    assert result.status == "ready"
    batch = _batch(session_factory, result.batch_id)
    assert (batch.intent, batch.effective_action) == ("adjust_take_profit", "adjust_take_profit")
    assert batch.requested_fraction is None and batch.effective_fraction is None
    deadline = batch.execution_deadline_at.replace(tzinfo=UTC)
    assert deadline == NOW + timedelta(seconds=180)
    snapshot = json.loads(batch.target_snapshot_json)
    assert snapshot["take_profit_adjustment"]["instruction"]["allocations"] == ["50", "50"]
    assert [leg.status for leg in result.batch.legs] == ["planned"]
    assert exchange.writes == []


def test_disabled_mode_creates_no_batch_and_never_reduces(session_factory):
    save_trading_settings(session_factory, {"take_profit_adjust_mode": "disabled"})
    ids, exchange = _seed(session_factory, text=TEXT_18199)

    result = _plan(session_factory, ids, exchange)

    assert (result.status, result.reason_code, result.batch) == (
        "blocked",
        "take_profit_adjust_disabled",
        None,
    )
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 0
    assert exchange.writes == []


def test_ouyang_stop_is_gated_and_carried_on_the_legs(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_13848,
        stop="80000",
        positions=(("pos-1", "12", "74000:6,72000:6"),),
        strategy_take_profit="74000/72000",
    )
    exchange.last = "76000"

    result = _plan(session_factory, ids, exchange)

    assert result.status == "ready"
    assert result.batch.legs[0].planned_tpsl == {
        "intent": "adjust_take_profit",
        "stop_loss_text": "78700",
        "stop_price_source": "current_message_text",
    }
    assert result.batch.target_snapshot["stop_price_gate"]["stop_price"] == "78700"


def test_a_stop_on_the_wrong_side_of_the_market_refuses_at_planning(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_13848, stop="80000")
    exchange.last = "79000"  # a short's stop at 78700 is below the market

    result = _plan(session_factory, ids, exchange)

    assert result.status == "blocked"
    assert result.reason_code == "management_stop_direction_invalid"


