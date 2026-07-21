from __future__ import annotations

from datetime import UTC, datetime


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


def test_take_profit_order_keeps_per_leg_cancel_audit_history(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.models import ExecutionOrderLeg
    from telegram_kol_research.position_take_profit_orders import (
        record_take_profit_cancel_requested,
        record_take_profit_cancelled,
        record_take_profit_order,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="short",
            venue="deepcoin", margin_mode="cross", position_mode="split",
            pos_id="pos-10", status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            order_kind="trigger_limit", venue="deepcoin", pos_id="pos-10", status="active",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_status = "verified"
        row = record_take_profit_order(
            session,
            venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, pos_id="pos-10", order_id="tp-old-1",
            trigger_price="64500", size_text="10", created_at=NOW,
            evidence={"source": "embedded_trigger"},
        )
        record_take_profit_cancel_requested(
            session, row, request={"ordId": "tp-old-1"}, requested_at=NOW
        )
        record_take_profit_cancelled(
            session, row, response={"ordId": "tp-old-1"}, cancelled_at=NOW
        )
        session.commit()

        assert row.status == "cancelled"
        assert row.venue == "deepcoin"
        assert row.execution_binding_id == binding_id
        assert row.execution_order_leg_id == leg_id
        assert row.pos_id == "pos-10"
        assert row.trigger_price == "64500"
        assert row.size_text == "10"
        assert row.cancel_requested_at.replace(tzinfo=UTC) == NOW
        assert row.cancelled_at.replace(tzinfo=UTC) == NOW
