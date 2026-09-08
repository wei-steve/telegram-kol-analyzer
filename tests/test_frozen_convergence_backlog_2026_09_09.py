"""A-5b: the frozen-convergence backlog repair decides by position liveness.

Twenty-nine convergences sat ``conflicted /
convergence_partial_position_unexplained`` because the online reconcile only
visits ``submitted`` ones. Twenty-eight of their positions had closed weeks
earlier; one was still open. The whole repair turns on telling those two apart
correctly, and on refusing to guess when the positions snapshot is unusable.
"""

from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionTakeProfitOrder,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.one_off.frozen_convergence_backlog_2026_09_09 import (
    FROZEN_REASON_CODE,
    ORDER_TERMINAL_REASON,
    TERMINAL_REASON_CODE,
    FrozenConvergenceRepairError,
    apply_plan,
    build_plan,
    live_position_ids,
)


NOW = datetime(2026, 9, 8, 23, 37, tzinfo=UTC)


def _fixture(tmp_path):
    """Two frozen convergences: one closed position, one still open."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        for binding_id, leg_id, pos_id in (
            (337, 579, "pos-closed"),
            (345, 593, "pos-live"),
        ):
            session.add(
                ExecutionBinding(
                    id=binding_id,
                    venue="deepcoin",
                    strategy_instance_id=f"strategy-{binding_id}",
                    kol_id=7,
                    chat_id=-100,
                    message_id=binding_id,
                    symbol="BTC",
                    side="long",
                    status="closed",
                    pos_id=pos_id,
                )
            )
            session.add(
                ExecutionOrderLeg(
                    id=leg_id,
                    execution_binding_id=binding_id,
                    venue="deepcoin",
                    purpose="entry",
                    leg_index=1,
                    status="closed",
                    pos_id=pos_id,
                    attribution_status="verified",
                )
            )
        for conv_id, binding_id, leg_id, pos_id in (
            (222, 337, 579, "pos-closed"),
            (237, 345, 593, "pos-live"),
        ):
            session.add(
                TriggerTakeProfitConvergence(
                    id=conv_id,
                    venue="deepcoin",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=leg_id,
                    desired_take_profits_json="[]",
                    status="conflicted",
                    reason_code=FROZEN_REASON_CODE,
                    pos_id=pos_id,
                )
            )
            for index, (order_id, size) in enumerate(
                ((f"{pos_id}-tp1", "5"), (f"{pos_id}-tp2", "3")), start=1
            ):
                session.add(
                    PositionTakeProfitOrder(
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg_id,
                        trigger_take_profit_convergence_id=conv_id,
                        pos_id=pos_id,
                        order_id=order_id,
                        trigger_price=str(80000 + index),
                        size_text=size,
                        status="active",
                    )
                )
        session.commit()
    return session_factory


def test_closed_position_is_terminalized_and_live_one_is_not(tmp_path):
    session_factory = _fixture(tmp_path)

    plan = build_plan(
        session_factory,
        live_pos_ids=frozenset({"pos-live"}),
        judged_at=NOW,
    )
    result = apply_plan(
        session_factory,
        plan,
        applied_at=NOW,
        live_position_scene={237: {"reason_code": "criterion_three_missing"}},
    )

    assert plan.live_frozen_convergence_ids == (237,)
    assert result.skipped == ()
    with session_factory() as session:
        closed = session.get(TriggerTakeProfitConvergence, 222)
        live = session.get(TriggerTakeProfitConvergence, 237)
        assert (closed.status, closed.reason_code) == (
            "completed",
            TERMINAL_REASON_CODE,
        )
        assert closed.completed_at == NOW.replace(tzinfo=None)
        # The live one keeps its freeze; only the scene is recorded.
        assert (live.status, live.reason_code) == ("conflicted", FROZEN_REASON_CODE)
        assert live.completed_at is None
        assert "criterion_three_missing" in live.error_json

        statuses = {
            row.order_id: row.status
            for row in session.query(PositionTakeProfitOrder).all()
        }
        assert statuses == {
            "pos-closed-tp1": "expired",
            "pos-closed-tp2": "expired",
            # Untouched: this repair has no authority over a live position's plan.
            "pos-live-tp1": "active",
            "pos-live-tp2": "active",
        }
        expired = (
            session.query(PositionTakeProfitOrder)
            .filter_by(order_id="pos-closed-tp1")
            .one()
        )
        assert ORDER_TERMINAL_REASON in expired.evidence_json
        audits = session.query(PositionAttributionAudit).all()
        assert len(audits) == len(plan.actions)
        assert {a.event_type for a in audits} == {"historical_cleanup"}
        assert {a.notification_status for a in audits} == {"not_needed"}


def test_second_run_writes_no_second_audit(tmp_path):
    session_factory = _fixture(tmp_path)
    live = frozenset({"pos-live"})

    first = apply_plan(
        session_factory,
        build_plan(session_factory, live_pos_ids=live, judged_at=NOW),
        applied_at=NOW,
    )
    second = apply_plan(
        session_factory,
        build_plan(session_factory, live_pos_ids=live, judged_at=NOW),
        applied_at=NOW,
    )

    # The terminalized rows no longer match their ``before`` values, so they are
    # not even planned again; the still-frozen live row is, and its audit
    # fingerprint already exists.
    # 2 take-profit rows + 2 convergence rows on the first pass.
    assert len(first.audit_ids) == 4
    assert second.audit_ids == ()
    with session_factory() as session:
        assert session.query(PositionAttributionAudit).count() == 4


def test_a_row_production_moved_is_refused_not_overwritten(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = build_plan(
        session_factory, live_pos_ids=frozenset({"pos-live"}), judged_at=NOW
    )
    with session_factory() as session:
        moved = session.get(TriggerTakeProfitConvergence, 222)
        moved.status = "completed"
        moved.reason_code = "convergence_position_terminal"
        session.commit()

    result = apply_plan(session_factory, plan, applied_at=NOW)

    refused = [item for item in result.skipped if item.get("row_id") == 222]
    assert refused and refused[0]["reason"] == "changed_since_plan"


def test_an_unreadable_positions_snapshot_refuses_to_build_a_plan():
    """"Absent from the snapshot" only means "closed" if the snapshot is whole."""

    with pytest.raises(FrozenConvergenceRepairError):
        live_position_ids([{"posId": "pos-a"}, {"instId": "BTC-USDT-SWAP"}])
    with pytest.raises(FrozenConvergenceRepairError):
        live_position_ids([{"posId": "pos-a"}, "not-a-row"])


def test_an_empty_positions_snapshot_is_usable():
    """Every position closed is a real and ordinary state."""

    assert live_position_ids([]) == frozenset()
