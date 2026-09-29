"""Take-profit adjustment: shadow/live execution, supersession, deadline.

Design: docs/plans/2026-09-29-take-profit-adjustment-design.md. Every exchange
call goes to an in-memory fake; nothing here reaches a network. Fixtures live
in ``test_take_profit_adjustment_planner``.
"""

from __future__ import annotations

import json
from datetime import timedelta

from telegram_kol_research import strategy_management_planner as planner
from telegram_kol_research.management_recovery_timeout import (
    expire_take_profit_adjustment_deadlines,
)
from telegram_kol_research.models import (
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.strategy_management_executor import execute_management_batch
from telegram_kol_research.strategy_management_worker import (
    run_strategy_management_worker_tick,
)
from telegram_kol_research.system_operator_bot import (
    format_strategy_management_notification,
)
from telegram_kol_research.take_profit_adjustment_executor import (
    execute_take_profit_adjustment_batch,
)
from telegram_kol_research.trading_settings import save_trading_settings
from tests.test_take_profit_adjustment_planner import (  # noqa: F401 - fixtures
    INST,
    NOW,
    TEXT_13848,
    TEXT_18199,
    TEXT_18294,
    TEXT_19670,
    _Specs,
    _batch,
    _convergence_plan,
    _fixed_stop_gate_clock,
    _no_reconciliation,
    _notifications,
    _plan,
    _seed,
    session_factory,
)


# ---------------------------------------------------------------- execution


def _plan_and_execute(session_factory, ids, exchange, *, via="executor"):
    planned = _plan(session_factory, ids, exchange)
    assert planned.status == "ready", planned.reason_code
    if via == "management_executor":
        result = execute_management_batch(
            session_factory,
            batch_id=planned.batch_id,
            deepcoin_client=exchange,
            executed_at=NOW,
        )
    else:
        result = execute_take_profit_adjustment_batch(
            session_factory,
            batch_id=planned.batch_id,
            deepcoin_client=exchange,
            executed_at=NOW,
        )
    return planned.batch_id, result


def test_18199_replay_is_already_satisfied_with_zero_writes(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_18199)

    batch_id, result = _plan_and_execute(
        session_factory, ids, exchange, via="management_executor"
    )

    assert exchange.writes == []
    assert result["status"] == "succeeded"
    assert result["submitted"] is False
    batch = _batch(session_factory, batch_id)
    assert (batch.status, batch.reason_code) == (
        "succeeded",
        "take_profit_adjust_already_satisfied",
    )
    notes = _notifications(session_factory, batch_id)
    assert len(notes) == 1
    rendered = format_strategy_management_notification(notes[0])
    assert "【调整止盈】" in rendered and "已与新结构一致" in rendered


def test_18199_variant_is_rebuilt_to_seven_and_eight_and_r4_follows(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded" and result["submitted"] is True
    assert exchange.take_profits() == [("82000", "8"), ("84000", "7")]
    assert exchange.stops() == ["87000"]
    kinds = [kind for kind, _ in exchange.writes]
    assert kinds == [
        "cancel_position_sltp",
        "cancel_position_sltp",
        "set_position_sltp",
        "set_position_sltp",
    ]
    plan, status = _convergence_plan(session_factory)
    assert [row["price"] for row in plan] == ["84000", "82000"]
    assert status == "submitted"
    with session_factory() as session:
        legs = {
            (row.planned_trigger_price, row.planned_size): row.status
            for row in session.query(PositionProtectionLeg).filter_by(role="take_profit")
        }
        active_orders = sorted(
            (row.trigger_price, row.size_text)
            for row in session.query(PositionTakeProfitOrder).filter_by(status="active")
        )
        ledger = sorted(
            (row.trigger_price, row.size_text)
            for row in session.query(PositionProtectionLedger).filter_by(
                purpose="take_profit", status="verified"
            )
        )
    assert legs[("84000", "10")] == "superseded" and legs[("82000", "5")] == "superseded"
    assert legs[("84000", "7")] == "verified" and legs[("82000", "8")] == "verified"
    assert active_orders == [("82000", "8"), ("84000", "7")]
    assert ledger == [("82000", "8"), ("84000", "7")]
    assert _batch(session_factory, batch_id).reason_code == "take_profit_adjust_applied"


def test_the_convergence_worker_accepts_the_rebuilt_plan(session_factory):
    """R4: after the rewrite the convergence sees its own plan, not a mismatch."""

    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    _plan_and_execute(session_factory, ids, exchange)
    # One reconciliation round, as every worker tick runs: the cancels are
    # confirmed from the pending list, which no longer carries them.
    from telegram_kol_research.position_mutation_gateway import (
        reconcile_submitted_position_mutation_intents,
    )

    reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=exchange.list_trigger_orders_pending(inst_id=INST),
        reconciled_at=NOW,
    )
    with session_factory() as session:
        convergence = session.query(TriggerTakeProfitConvergence).one()
        convergence.status = "ready"
        session.commit()
        convergence_id = convergence.id

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        planned_at=NOW,
        allow_logical_adoption=True,
    )

    assert (plan.status, plan.reason_code) == ("already_converged", "convergence_take_profit_already_converged")


def test_shadow_mode_records_the_plan_and_writes_nothing(session_factory):
    save_trading_settings(session_factory, {"take_profit_adjust_mode": "shadow"})
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert exchange.writes == []
    assert result["status"] == "shadow_planned" and result["submitted"] is False
    batch = _batch(session_factory, batch_id)
    # ``resolved``, never ``blocked``: the on-call watcher treats blocked as a
    # failure and would open a case for every shadow run.
    assert (batch.status, batch.reason_code) == ("resolved", "take_profit_adjust_shadow_planned")
    assert batch.completed_at is not None
    with session_factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        recorded = json.loads(leg.request_json)["take_profit_adjustment"]
    assert recorded["plan"]["targets"] == [["84000", "7"], ["82000", "8"]]
    snapshot = json.loads(batch.target_snapshot_json)
    execution = snapshot["take_profit_adjustment"]["execution"]
    assert execution["mode"] == "shadow"
    assert execution["positions"][0]["plan"]["targets"] == [["84000", "7"], ["82000", "8"]]
    assert execution["unfilled_entry_legs_rewritten"] is False
    from telegram_kol_research.strategy_management_planner import (
        management_target_fingerprint,
    )

    assert batch.target_fingerprint == management_target_fingerprint(snapshot)
    rendered = format_strategy_management_notification(
        _notifications(session_factory, batch_id)[0]
    )
    assert "影子模式" in rendered and "84000×7" in rendered
    assert "未调用交易 API" in rendered
    plan, _ = _convergence_plan(session_factory)
    assert [row["price"] for row in plan] == ["84000", "82000"]
    assert [row["allocation_pct"] for row in plan] == ["66", "33"]  # untouched entry plan


def test_18294_without_a_price_is_refused_and_never_reduces(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_18294)

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert exchange.writes == []
    assert result["status"] == "blocked"
    batch = _batch(session_factory, batch_id)
    assert (batch.status, batch.reason_code) == ("blocked", "take_profit_adjust_price_missing")
    rendered = format_strategy_management_notification(
        _notifications(session_factory, batch_id)[0]
    )
    assert "没有给出止盈价位" in rendered


def test_19670_places_thirty_percent_at_82500_and_never_closes(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_19670, positions=(("pos-1", "10", ""),))
    exchange.last = "84000"

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded"
    assert exchange.take_profits() == [("82500", "3")]
    assert all(kind != "place_order" for kind, _ in exchange.writes)


def test_two_positions_are_each_sized_from_their_own_remaining(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_18199,
        positions=(
            ("pos-1", "15", "84000:10,82000:5"),
            ("pos-2", "4", "84000:1,82000:3"),
        ),
    )

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded"
    by_pos: dict[str, list] = {}
    for kind, payload in exchange.writes:
        if kind == "set_position_sltp":
            by_pos.setdefault(payload["posId"], []).append(
                (payload["tpTriggerPx"], payload["sz"])
            )
    assert by_pos == {
        "pos-1": [("84000", "7"), ("82000", "8")],
        "pos-2": [("84000", "2"), ("82000", "2")],
    }


def test_cancelled_then_place_fails_ends_the_batch_and_hands_the_new_plan_to_convergence(
    session_factory,
):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    exchange.fail_set_prices = {"82000"}

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "failed" and result["submitted"] is True
    batch = _batch(session_factory, batch_id)
    assert (batch.status, batch.reason_code) == ("blocked", "take_profit_replace_incomplete")
    assert exchange.take_profits() == [("84000", "7")]
    assert exchange.stops() == ["87000"]  # never naked
    plan, status = _convergence_plan(session_factory)
    assert [(row["price"]) for row in plan] == ["84000", "82000"]
    assert status == "ready"  # the convergence worker retries the *new* plan


def test_cancel_failure_keeps_the_old_take_profits_and_restores_the_plan(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    exchange.fail_cancel_ids = {"pos-1-tp2"}

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    batch = _batch(session_factory, batch_id)
    assert (batch.status, batch.reason_code) == ("blocked", "take_profit_replace_incomplete")
    assert not any(kind == "set_position_sltp" for kind, _ in exchange.writes)
    assert ("82000", "5") in exchange.take_profits()
    plan, status = _convergence_plan(session_factory)
    assert [row["allocation_pct"] for row in plan] == ["66", "33"]
    assert status == "submitted"
    with session_factory() as session:
        statuses = {
            row.exchange_order_id: row.status
            for row in session.query(PositionProtectionLeg).filter_by(role="take_profit")
            if row.exchange_order_id
        }
    # tp1's cancel was accepted before tp2's failed: it stays retired in our
    # model; tp2 is still armed and keeps its leg.
    assert statuses == {"pos-1-tp1": "superseded", "pos-1-tp2": "verified"}
    assert exchange.take_profits() == [("82000", "5")]


def test_ouyang_live_moves_the_stop_first_then_the_take_profit(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_13848,
        stop="80000",
        positions=(("pos-1", "12", "74000:6,72000:6"),),
        strategy_take_profit="74000/72000",
    )
    exchange.last = "76000"

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded", result
    assert exchange.stops() == ["78700"]
    assert exchange.take_profits() == [("73070", "12")]
    kinds = [
        (kind, payload.get("slTriggerPx") or payload.get("tpTriggerPx") or payload.get("ordId"))
        for kind, payload in exchange.writes
    ]
    assert kinds[0] == ("set_position_sltp", "78700")  # new stop first
    assert kinds[1] == ("cancel_position_sltp", "pos-1-sl")


def test_ouyang_live_with_a_backup_stop_keeps_both_roles(session_factory):
    from telegram_kol_research.models import PositionBackupStopOrder

    ids, exchange = _seed(
        session_factory,
        text=TEXT_13848,
        stop="80000",
        backup_stop="80040",
        positions=(("pos-1", "12", "74000:6,72000:6"),),
        strategy_take_profit="74000/72000",
    )
    exchange.last = "76000"

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded", result
    assert exchange.stops() == ["78700", "78700"]
    assert exchange.take_profits() == [("73070", "12")]
    with session_factory() as session:
        purposes = sorted(
            (row.purpose, row.trigger_price)
            for row in session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.status == "verified",
                PositionProtectionLedger.purpose.in_(("stop_loss", "backup_stop")),
            )
        )
        backups = [
            (row.order_id, row.status)
            for row in session.query(PositionBackupStopOrder).order_by(
                PositionBackupStopOrder.id
            )
        ]
    assert purposes == [("backup_stop", "78700"), ("stop_loss", "78700")]
    assert backups[0] == ("pos-1-backup", "superseded")
    assert backups[-1][1] == "active" and backups[-1][0].startswith("new-")


def test_a_loosening_stop_refuses_the_whole_batch_with_zero_writes(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_13848,
        stop="78000",  # 78700 would loosen a short's stop
        positions=(("pos-1", "12", "74000:6,72000:6"),),
    )
    exchange.last = "76000"

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert exchange.writes == []
    assert _batch(session_factory, batch_id).reason_code == (
        "explicit_stop_adjustment_not_risk_tightening"
    )


def test_a_position_without_a_stop_is_not_touched(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    exchange.pending = [row for row in exchange.pending if not row.get("slTriggerPx")]

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert exchange.writes == []
    assert _batch(session_factory, batch_id).reason_code == "take_profit_adjust_stop_missing"


def test_an_incomplete_pending_read_is_refused_with_zero_writes(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    planned = _plan(session_factory, ids, exchange)
    exchange.read_trigger_orders_pending = lambda **_: {"code": "50001", "data": []}

    execute_take_profit_adjustment_batch(
        session_factory, batch_id=planned.batch_id, deepcoin_client=exchange, executed_at=NOW
    )

    assert exchange.writes == []
    assert _batch(session_factory, planned.batch_id).reason_code == (
        "take_profit_adjust_exchange_read_incomplete"
    )


# ---------------------------------------------------------------- supersession


def _plan_full_exit(session_factory, ids, exchange, *, now):
    with session_factory() as session:
        exit_message = RawMessage(chat_id=100, message_id=31, posted_at=now, text="BTC空单全部平仓")
        session.add(exit_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=exit_message.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=exit_message.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                target_lifecycle_id=ids["lifecycle_id"],
                management_action="full_exit",
                recognition_generation="generation-2",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = exit_message.id
    return planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        planned_at=now,
        execution_mode="live",
    )


def test_a_full_exit_supersedes_an_adjustment_that_has_not_started(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    adjustment = _plan(session_factory, ids, exchange)

    full_exit = _plan_full_exit(session_factory, ids, exchange, now=NOW + timedelta(seconds=5))

    assert full_exit.status == "ready", full_exit.reason_code
    superseded = _batch(session_factory, adjustment.batch_id)
    assert (superseded.status, superseded.reason_code) == (
        "resolved",
        "superseded_by_risk_reduction",
    )
    assert exchange.writes == []


def test_an_adjustment_that_started_writing_is_not_superseded(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    adjustment = _plan(session_factory, ids, exchange)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, adjustment.batch_id)
        batch.status = "executing"
        for leg in session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id
        ):
            leg.status = "reserved"
        session.commit()

    full_exit = _plan_full_exit(session_factory, ids, exchange, now=NOW + timedelta(seconds=5))

    assert (full_exit.status, full_exit.reason_code) == (
        "blocked",
        "prior_management_batch_unresolved",
    )
    assert _batch(session_factory, adjustment.batch_id).status == "executing"


def test_a_newer_adjustment_supersedes_an_older_unstarted_one(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    first = _plan(session_factory, ids, exchange)
    with session_factory() as session:
        newer = RawMessage(chat_id=100, message_id=32, posted_at=NOW, text="止盈位：83000")
        session.add(newer)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=newer.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=newer.id,
                symbol="BTC",
                side="short",
                event_type="position_update",
                target_lifecycle_id=ids["lifecycle_id"],
                management_action="adjust_take_profit",
                recognition_generation="generation-3",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        newer_id = newer.id

    second = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=newer_id,
        deepcoin_client=exchange,
        contract_spec_provider=_Specs(),
        planned_at=NOW + timedelta(seconds=3),
        execution_mode="live",
    )

    assert second.status == "ready"
    assert _batch(session_factory, first.batch_id).reason_code == (
        "superseded_by_newer_take_profit_adjustment"
    )


# ---------------------------------------------------------------- deadline


def test_an_adjustment_past_its_deadline_is_blocked_and_reported(session_factory):
    ids, exchange = _seed(session_factory, text=TEXT_18199)
    planned = _plan(session_factory, ids, exchange)

    assert expire_take_profit_adjustment_deadlines(
        session_factory, now=NOW + timedelta(seconds=179)
    ) == ()
    expired = expire_take_profit_adjustment_deadlines(
        session_factory, now=NOW + timedelta(seconds=180)
    )

    assert expired == (planned.batch_id,)
    batch = _batch(session_factory, planned.batch_id)
    assert (batch.status, batch.reason_code) == (
        "blocked",
        "take_profit_adjust_deadline_expired",
    )
    rendered = format_strategy_management_notification(
        _notifications(session_factory, planned.batch_id)[-1]
    )
    assert "180 秒内未完成" in rendered


def test_the_executor_refuses_a_batch_past_its_deadline(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    planned = _plan(session_factory, ids, exchange)

    execute_take_profit_adjustment_batch(
        session_factory,
        batch_id=planned.batch_id,
        deepcoin_client=exchange,
        executed_at=NOW + timedelta(seconds=200),
    )

    assert exchange.writes == []
    assert _batch(session_factory, planned.batch_id).reason_code == (
        "take_profit_adjust_deadline_expired"
    )


def test_the_worker_runs_a_ready_adjustment_and_expires_stale_ones(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    planned = _plan(session_factory, ids, exchange)

    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: exchange,
        processed_at=NOW + timedelta(seconds=10),
        snapshot_loader=lambda *args, **kwargs: None,
        binding_reconciler=lambda *args, **kwargs: None,
        take_profit_convergence_runner=lambda *args, **kwargs: 0,
        composite_reconciler=lambda *args, **kwargs: None,
    )

    batch = _batch(session_factory, planned.batch_id)
    assert (batch.status, batch.reason_code) == ("succeeded", "take_profit_adjust_applied")
    assert exchange.take_profits() == [("82000", "8"), ("84000", "7")]


TEXT_14306 = (
    "目前已经是东八区晚间23:40左右，多单正常持有过夜，小级别关注78500-78700附近，"
    "突破看第一止盈位79100提前设置好自动止盈50%，触发后可上移止损做成本保护，在看下一个止盈点位。"
)
TEXT_17526 = "视频内容重点提炼\n多单利润正在扩大，完成第一止盈后，剩下看向79400目标位即可！"


def test_14306_live_replaces_only_the_first_tier(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_14306,
        side="long",
        stop="77000",
        positions=(("pos-1", "10", "79000:5,80000:5"),),
        strategy_take_profit="79000/80000",
    )
    exchange.last = "78600"

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded", result
    assert exchange.take_profits() == [("79100", "5"), ("80000", "5")]
    cancelled = [p["ordId"] for kind, p in exchange.writes if kind == "cancel_position_sltp"]
    assert cancelled == ["pos-1-tp1"]  # the far tier was never touched


def test_17526_remaining_target_leaves_the_filled_tier_alone(session_factory):
    ids, exchange = _seed(
        session_factory,
        text=TEXT_17526,
        side="long",
        stop="76000",
        positions=(("pos-1", "6", "80500:6"),),
        strategy_take_profit="78000/80500",
    )
    exchange.last = "78500"
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        filled = PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=ids["binding_id"],
            execution_order_leg_id=leg.id,
            strategy_instance_id=ids["strategy_instance_id"],
            pos_id="pos-1",
            instrument_id=INST,
            side="long",
            order_id="pos-1-tp-filled",
            purpose="take_profit",
            trigger_price="78000",
            size_text="6",
            status="filled",
            evidence_source="test",
            evidence_json="{}",
        )
        session.add(filled)
        session.commit()

    batch_id, result = _plan_and_execute(session_factory, ids, exchange)

    assert result["status"] == "succeeded", result
    assert exchange.take_profits() == [("79400", "6")]
    with session_factory() as session:
        assert session.query(PositionProtectionLedger).filter_by(
            order_id="pos-1-tp-filled"
        ).one().status == "filled"


def test_switching_to_disabled_after_planning_refuses_with_zero_writes(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    planned = _plan(session_factory, ids, exchange)
    save_trading_settings(session_factory, {"take_profit_adjust_mode": "disabled"})

    execute_take_profit_adjustment_batch(
        session_factory, batch_id=planned.batch_id, deepcoin_client=exchange, executed_at=NOW
    )

    assert exchange.writes == []
    assert _batch(session_factory, planned.batch_id).reason_code == "take_profit_adjust_disabled"


def test_an_interrupted_run_is_handed_to_a_person_not_repeated(session_factory):
    ids, exchange = _seed(
        session_factory, text=TEXT_18199, positions=(("pos-1", "15", "84000:10,82000:5"),)
    )
    planned = _plan(session_factory, ids, exchange)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, planned.batch_id)
        batch.status = "executing"
        session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id
        ).update({"status": "reserved"})
        session.commit()

    result = execute_take_profit_adjustment_batch(
        session_factory, batch_id=planned.batch_id, deepcoin_client=exchange, executed_at=NOW
    )

    assert exchange.writes == []
    assert result["status"] == "failed" and result["submitted"] is True
    assert _batch(session_factory, planned.batch_id).reason_code == (
        "take_profit_adjust_interrupted"
    )
