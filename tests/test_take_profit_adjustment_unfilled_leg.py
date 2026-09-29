"""A strategy with one filled and one unfilled entry leg (binding 388's shape).

Production, read-only, 2026-09-29: binding 388 (BTC short) has leg 662 filled
(its take-profit convergence ``submitted``, take-profit legs ``verified``) and
leg 663 unfilled (convergence ``waiting_backup_stop``, empty ``pos_id``,
take-profit protection legs ``planned`` with ``planned_size='50.0'`` -- a
percent of the future position, not contracts).

A take-profit adjustment must change both: the filled leg's orders now, and the
unfilled leg's plan, so that when 663 fills the convergence stages the *new*
take profits. And the convergence machinery that runs afterwards must accept
what the adjustment left behind without freezing anything.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from telegram_kol_research import strategy_management_planner as planner
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionTakeProfitOrder,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.position_take_profit_orders import (
    reconcile_trigger_take_profit_order_history,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.take_profit_adjustment import (
    MODE_FULL_RESET,
    MODE_PRICES_ONLY,
    MODE_RATIOS_ONLY,
    MODE_SINGLE_TIER,
    PLAN_READY,
    TakeProfitInstruction,
    plan_unfilled_take_profit_allocations,
)
from telegram_kol_research.take_profit_adjustment_executor import (
    execute_take_profit_adjustment_batch,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trigger_take_profit_convergence import (
    mark_trigger_take_profit_convergence_ready,
)
from telegram_kol_research.trigger_take_profit_convergence_executor import (
    execute_ready_trigger_take_profit_convergences,
)
from tests.test_take_profit_adjustment_planner import (  # noqa: F401 - fixtures
    INST,
    NOW,
    FakeExchange,
    _Specs,
    _batch,
    _fixed_stop_gate_clock,
    _no_reconciliation,
    _position,
    _sl_row,
    _tp_row,
)

STRATEGY = "deepcoin:100:20:BTC:short"
TEXT = "BTC空单继续持有，设置好止盈！\n止盈位：83000！！！"


@pytest.fixture
def session_factory(tmp_path):
    from telegram_kol_research.db import create_session_factory

    factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        factory,
        {
            "take_profit_adjust_mode": "live",
            # Production values, read 2026-09-29.
            "management_execution_mode": "live",
            "position_management_liveness_v2_mode": "live",
            # Without it the effective liveness mode is ``disabled`` and the
            # convergence lane would pass this test by never running.
            "auto_trade_enabled": True,
        },
    )
    return factory


def _seed_binding_388(session_factory, *, text=TEXT):
    """Leg A filled on pos-a (15 contracts, TPs 84000x10 / 82000x5), leg B unfilled."""

    with session_factory() as session:
        entry = RawMessage(chat_id=100, message_id=20, posted_at=NOW, text="BTC 空")
        message = RawMessage(chat_id=100, message_id=30, posted_at=NOW, text=text)
        session.add_all([entry, message])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=1),
            stop_loss=87000.0,
            take_profit="84000-82000",
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id=STRATEGY,
            kol_id="kol",
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="pos-a",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        filled = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=STRATEGY,
            leg_index=0,
            purpose="entry",
            order_kind="limit",
            order_id="pos-a",
            pos_id="pos-a",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json=json.dumps(
                {"policy_version": 2, "source": "direct_order_identity"}
            ),
            status="active",
        )
        unfilled = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=STRATEGY,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            order_id="entry-order-b",
            venue="deepcoin",
            attribution_status="unassigned",
            status="open",
        )
        session.add_all([filled, unfilled])
        session.flush()
        entry_plan = json.dumps(
            [
                {"allocation_pct": "50", "price": "84000"},
                {"allocation_pct": "50", "price": "82000"},
            ],
            separators=(",", ":"),
        )
        convergence_a = TriggerTakeProfitConvergence(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=filled.id,
            desired_take_profits_json=entry_plan,
            status="submitted",
            pos_id="pos-a",
        )
        convergence_b = TriggerTakeProfitConvergence(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=unfilled.id,
            desired_take_profits_json=entry_plan,
            status="waiting_backup_stop",
            reason_code="convergence_waiting_backup_stop",
        )
        session.add_all([convergence_a, convergence_b])
        session.flush()
        # Leg A's stop and take profits, exactly as the convergence left them.
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=filled.id,
            strategy_instance_id=STRATEGY,
            pos_id="pos-a",
            instrument_id=INST,
            side="short",
            order_id="a-sl",
            purpose="stop_loss",
            trigger_price="87000",
            size_text="0",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        for index, (order_id, price, size) in enumerate(
            (("a-tp1", "84000", "10"), ("a-tp2", "82000", "5")), start=1
        ):
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=filled.id,
                strategy_instance_id=STRATEGY,
                pos_id="pos-a",
                instrument_id=INST,
                side="short",
                order_id=order_id,
                purpose="take_profit",
                trigger_price=price,
                size_text=size,
                status="verified",
                evidence_source="trigger_take_profit_pending_readback",
                evidence={},
                seen_at=NOW,
            )
            session.add(
                PositionTakeProfitOrder(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=filled.id,
                    trigger_take_profit_convergence_id=convergence_a.id,
                    pos_id="pos-a",
                    order_id=order_id,
                    trigger_price=price,
                    size_text=size,
                    status="active",
                )
            )
            # Planned at submit as percent ('50.0'), then bound and verified:
            # the percent spelling survives on filled legs too.
            session.add(
                PositionProtectionLeg(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=filled.id,
                    role="take_profit",
                    leg_index=index,
                    planned_trigger_price=price,
                    planned_size="50.0",
                    pos_id="pos-a",
                    exchange_order_id=order_id,
                    status="verified",
                )
            )
        # Leg B: only a plan. Primary/backup stop legs and two percent-sized
        # take-profit legs, none of them bound to anything yet.
        session.add_all(
            [
                PositionProtectionLeg(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=unfilled.id,
                    role="primary_stop",
                    leg_index=1,
                    planned_trigger_price="87000",
                    planned_size="10",
                    status="planned",
                ),
                PositionProtectionLeg(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=unfilled.id,
                    role="take_profit",
                    leg_index=1,
                    planned_trigger_price="84000",
                    planned_size="50.0",
                    status="planned",
                ),
                PositionProtectionLeg(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=unfilled.id,
                    role="take_profit",
                    leg_index=2,
                    planned_trigger_price="82000",
                    planned_size="50.0",
                    status="planned",
                ),
            ]
        )
        session.add(
            RecognitionDecision(
                raw_message_id=message.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(
                    {"lifecycle_event": {"event_type": "position_update", "management_action": "risk_update"}},
                ),
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=message.id,
                symbol="BTC",
                side="short",
                event_type="position_update",
                target_lifecycle_id=lifecycle.id,
                management_action="adjust_take_profit",
                recognition_generation="generation-1",
                stop_loss_text="87000",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        ids = {
            "raw_message_id": message.id,
            "binding_id": binding.id,
            "filled_leg_id": filled.id,
            "unfilled_leg_id": unfilled.id,
            "convergence_a": convergence_a.id,
            "convergence_b": convergence_b.id,
        }
    exchange = FakeExchange(
        positions=[_position("pos-a", "15")],
        pending=[
            _sl_row("a-sl", "87000"),
            _tp_row("a-tp1", "84000", "10"),
            _tp_row("a-tp2", "82000", "5"),
        ],
    )
    return ids, exchange


def _run_adjustment(session_factory, ids, exchange):
    planned = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=ids["raw_message_id"],
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        planned_at=NOW,
        execution_mode="live",
    )
    assert planned.status == "ready", planned.reason_code
    result = execute_take_profit_adjustment_batch(
        session_factory, batch_id=planned.batch_id, deepcoin_client=exchange, executed_at=NOW
    )
    return planned.batch_id, result


def _convergence(session_factory, convergence_id):
    with session_factory() as session:
        row = session.get(TriggerTakeProfitConvergence, convergence_id)
        return json.loads(row.desired_take_profits_json), row.status, row.reason_code


def _take_profit_legs(session_factory, leg_id):
    with session_factory() as session:
        return [
            (row.leg_index, row.planned_trigger_price, row.planned_size, row.status, row.pos_id)
            for row in session.query(PositionProtectionLeg)
            .filter_by(execution_order_leg_id=leg_id, role="take_profit")
            .order_by(PositionProtectionLeg.leg_index)
        ]


def test_live_adjustment_rewrites_the_unfilled_legs_plan_in_percent(session_factory):
    ids, exchange = _seed_binding_388(session_factory)

    batch_id, result = _run_adjustment(session_factory, ids, exchange)

    assert result["status"] == "succeeded", result
    assert exchange.take_profits() == [("83000", "15")]
    new_plan = [{"allocation_pct": "100", "price": "83000"}]
    assert _convergence(session_factory, ids["convergence_a"])[0] == new_plan
    plan_b, status_b, _ = _convergence(session_factory, ids["convergence_b"])
    assert plan_b == new_plan and status_b == "waiting_backup_stop"
    assert _take_profit_legs(session_factory, ids["unfilled_leg_id"]) == [
        (1, "84000", "50.0", "superseded", None),
        (2, "82000", "50.0", "superseded", None),
        (3, "83000", "100.0", "planned", None),  # still percent, same spelling
    ]
    snapshot = json.loads(_batch(session_factory, batch_id).target_snapshot_json)
    execution = snapshot["take_profit_adjustment"]["execution"]
    assert execution["unfilled_entry_legs_rewritten"] is True
    assert execution["unfilled_entry_legs"][0]["plan"]["targets"] == [["83000", "100"]]


def test_shadow_only_records_what_the_unfilled_leg_would_become(session_factory):
    save_trading_settings(session_factory, {"take_profit_adjust_mode": "shadow"})
    ids, exchange = _seed_binding_388(session_factory)

    batch_id, result = _run_adjustment(session_factory, ids, exchange)

    assert exchange.writes == []
    batch = _batch(session_factory, batch_id)
    assert (batch.status, batch.reason_code) == ("resolved", "take_profit_adjust_shadow_planned")
    plan_b, status_b, _ = _convergence(session_factory, ids["convergence_b"])
    assert [row["price"] for row in plan_b] == ["84000", "82000"]
    assert [row[3] for row in _take_profit_legs(session_factory, ids["unfilled_leg_id"])] == [
        "planned",
        "planned",
    ]
    execution = json.loads(batch.target_snapshot_json)["take_profit_adjustment"]["execution"]
    assert execution["unfilled_entry_legs"][0]["plan"]["targets"] == [["83000", "100"]]
    assert execution["unfilled_entry_legs_rewritten"] is False


def test_a_failed_batch_leaves_the_unfilled_leg_untouched(session_factory):
    ids, exchange = _seed_binding_388(session_factory)
    exchange.fail_cancel_ids = {"a-tp1"}

    batch_id, result = _run_adjustment(session_factory, ids, exchange)

    assert _batch(session_factory, batch_id).reason_code == "take_profit_replace_incomplete"
    plan_b, _, _ = _convergence(session_factory, ids["convergence_b"])
    assert [row["price"] for row in plan_b] == ["84000", "82000"]
    assert [row[3] for row in _take_profit_legs(session_factory, ids["unfilled_leg_id"])] == [
        "planned",
        "planned",
    ]
    plan_a, _, _ = _convergence(session_factory, ids["convergence_a"])
    assert [row["price"] for row in plan_a] == ["84000", "82000"]


def test_place_failure_after_cancel_still_moves_the_unfilled_leg_to_the_new_plan(
    session_factory,
):
    """The cancelled position now chases the new plan; so does the unfilled leg."""

    ids, exchange = _seed_binding_388(session_factory)
    exchange.fail_set_prices = {"83000"}

    batch_id, result = _run_adjustment(session_factory, ids, exchange)

    assert _batch(session_factory, batch_id).reason_code == "take_profit_replace_incomplete"
    new_plan = [{"allocation_pct": "100", "price": "83000"}]
    assert _convergence(session_factory, ids["convergence_a"])[:2] == (new_plan, "ready")
    assert _convergence(session_factory, ids["convergence_b"])[0] == new_plan


def test_convergence_machinery_accepts_the_adjustment_then_stages_the_new_plan_on_fill(
    session_factory,
):
    ids, exchange = _seed_binding_388(session_factory)
    _run_adjustment(session_factory, ids, exchange)
    writes_after_adjustment = len(exchange.writes)

    # 1. The take-profit history round that judges submitted/conflicted
    #    convergences. Before the fix it summed the cancelled 10+5 with the
    #    new 15 and froze convergence A as an unexplained partial reduction.
    with session_factory() as session:
        reconcile_trigger_take_profit_order_history(
            session,
            positions=exchange.list_positions(),
            pending_orders=exchange.list_trigger_orders_pending(inst_id=INST),
            trigger_history=[],
            observed_at=NOW + timedelta(seconds=30),
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={INST: True},
        )
        session.commit()
    # 2. The ready-convergence lane.
    execute_ready_trigger_take_profit_convergences(
        session_factory,
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        processed_at=NOW + timedelta(seconds=30),
        group_trading_mode_provider=lambda chat_id: "auto_trade",
    )

    with session_factory() as session:
        statuses = {
            row.id: (row.status, row.reason_code)
            for row in session.query(TriggerTakeProfitConvergence)
        }
    assert all(status != "conflicted" for status, _ in statuses.values()), statuses
    assert len(exchange.writes) == writes_after_adjustment
    new_plan = [{"allocation_pct": "100", "price": "83000"}]
    assert _convergence(session_factory, ids["convergence_a"])[0] == new_plan

    # 3. Leg B fills: 10 contracts on pos-b, with its own verified stop.
    with session_factory() as session:
        leg_b = session.get(ExecutionOrderLeg, ids["unfilled_leg_id"])
        leg_b.status = "active"
        leg_b.pos_id = "pos-b"
        leg_b.order_id = "pos-b"
        leg_b.attribution_status = "verified"
        leg_b.attribution_evidence_json = json.dumps(
            {"policy_version": 2, "source": "direct_order_identity"}
        )
        session.get(ExecutionBinding, ids["binding_id"]).pos_id = "pos-a,pos-b"
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=ids["binding_id"],
            execution_order_leg_id=leg_b.id,
            strategy_instance_id=STRATEGY,
            pos_id="pos-b",
            instrument_id=INST,
            side="short",
            order_id="b-sl",
            purpose="stop_loss",
            trigger_price="87000",
            size_text="0",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        session.commit()
    exchange.positions.append(_position("pos-b", "10"))
    exchange.pending.append(_sl_row("b-sl", "87000"))
    with session_factory() as session:
        convergence_b = session.get(TriggerTakeProfitConvergence, ids["convergence_b"])
        mark_trigger_take_profit_convergence_ready(
            session, convergence_b, ready_at=NOW + timedelta(minutes=1)
        )
        session.commit()

    execute_ready_trigger_take_profit_convergences(
        session_factory,
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        processed_at=NOW + timedelta(minutes=1),
        group_trading_mode_provider=lambda chat_id: "auto_trade",
    )

    placed = [
        (payload["posId"], payload["tpTriggerPx"], payload["sz"])
        for kind, payload in exchange.writes[writes_after_adjustment:]
        if kind == "set_position_sltp"
    ]
    assert placed == [("pos-b", "83000", "10")], _convergence(session_factory, ids["convergence_b"])
    assert _convergence(session_factory, ids["convergence_b"])[1] == "submitted"
    with session_factory() as session:
        conflicted = session.query(TriggerTakeProfitConvergence).filter_by(
            status="conflicted"
        ).count()
    assert conflicted == 0


# ---------------------------------------------------------------- pure plan


def _instruction(mode, prices=(), allocations=(), tier_index=None):
    return TakeProfitInstruction(
        mode=mode,
        prices=tuple(prices),
        allocations=tuple(allocations),
        tier_index=tier_index,
        stop_loss=None,
    )


@pytest.mark.parametrize(
    ("instruction", "current", "expected"),
    [
        (
            _instruction(MODE_RATIOS_ONLY, allocations=("50", "50")),
            [("84000", "40"), ("82000", "60")],
            [("84000", "50"), ("82000", "50")],
        ),
        (
            _instruction(MODE_PRICES_ONLY, ("83500", "81500")),
            [("84000", "30"), ("82000", "70")],
            [("83500", "30"), ("81500", "70")],  # same count: keeps its split
        ),
        (
            _instruction(MODE_PRICES_ONLY, ("83500", "82500", "81500")),
            [("84000", "50"), ("82000", "50")],
            [("83500", "40"), ("82500", "30"), ("81500", "30")],  # default table
        ),
        (
            _instruction(MODE_FULL_RESET, ("84000", "82000"), ("30", "70")),
            [("84000", "50"), ("82000", "50")],
            [("84000", "30"), ("82000", "70")],
        ),
        (
            _instruction(MODE_SINGLE_TIER, ("82500",), ("30",)),
            [("83000", "40"), ("81000", "30"), ("80000", "30")],
            [("83000", "28"), ("82500", "30"), ("81000", "21"), ("80000", "21")],
        ),
        (
            _instruction(MODE_SINGLE_TIER, ("83500",), ("60",), tier_index=1),
            [("84000", "50"), ("82000", "50")],
            [("83500", "60"), ("82000", "40")],
        ),
    ],
)
def test_unfilled_plan_shapes(instruction, current, expected):
    plan = plan_unfilled_take_profit_allocations(
        instruction=instruction,
        side="short",
        last_price="85000",
        current_plan=current,
        strategy_take_profit_prices=["84000", "82000"],
    )

    assert plan.status == PLAN_READY
    assert list(plan.targets) == expected
    assert sum(float(share) for _, share in plan.targets) == pytest.approx(100)


def test_unfilled_plan_drops_crossed_tiers_and_rescales():
    plan = plan_unfilled_take_profit_allocations(
        instruction=_instruction(MODE_FULL_RESET, ("85500", "84000"), ("50", "50")),
        side="short",
        last_price="85000",
        current_plan=[("84000", "100")],
    )

    assert plan.targets == (("84000", "100"),)
    assert plan.dropped_crossed == ("85500",)
