"""A superseded protection leg must not stop the take-profit history round.

2026-09-28 05:40Z: a composite management replacement re-recorded the two live
take-profit orders of 大镖客's BTC short (execution leg 658, orders
1001125406752123 / 1001125406752301) under new leg indexes 4/5 and marked the
originals (indexes 2/3) ``superseded``. The per-order protection-leg lookup
took ``.one_or_none()`` over both rows, raised ``MultipleResultsFound``, and
every Deepcoin execution reconcile round failed from then on.
"""

import logging
from datetime import datetime

from telegram_kol_research import position_take_profit_orders as tp_orders
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionTakeProfitOrder,
)
from telegram_kol_research.position_take_profit_orders import (
    reconcile_trigger_take_profit_order_history,
)

POS_ID = "1001125406750883"
TP2 = "1001125406752123"
TP3 = "1001125406752301"


class _Recorder(logging.Handler):
    """Records straight off the module logger: ``caplog`` misses them once
    ``configure_application_logging`` has turned propagation off in the run."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _seed(session, *, replacement_status="verified", extra_verified=False):
    binding = ExecutionBinding(
        strategy_instance_id="deepcoin:-1003048800035:4716:BTC:short",
        kol_id="group:-1003048800035",
        chat_id=-1003048800035,
        message_id=4716,
        symbol="BTC",
        side="short",
        venue="deepcoin",
        pos_id=POS_ID,
        status="active",
    )
    session.add(binding)
    session.flush()
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=1,
        purpose="entry",
        order_kind="market",
        order_id=POS_ID,
        pos_id=POS_ID,
        venue="deepcoin",
        attribution_status="verified",
        status="active",
    )
    session.add(leg)
    session.flush()
    orders = {}
    for order_id, price in ((TP2, "82100"), (TP3, "81400")):
        orders[order_id] = PositionTakeProfitOrder(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=POS_ID,
            order_id=order_id,
            trigger_price=price,
            size_text="5",
            status="active",
        )
    session.add_all(orders.values())

    def protection(index, order_id, price, status):
        return PositionProtectionLeg(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            role="take_profit",
            leg_index=index,
            planned_trigger_price=price,
            planned_size="5",
            pos_id=POS_ID,
            exchange_order_id=order_id,
            status=status,
        )

    # The production shape: originals superseded, replacements verified,
    # same exchange order ids.
    session.add_all(
        [
            protection(2, TP2, "82100", "superseded"),
            protection(3, TP3, "81400", "superseded"),
            protection(4, TP2, "82100", replacement_status),
            protection(5, TP3, "81400", "verified"),
        ]
    )
    if extra_verified:
        session.add(protection(6, TP2, "82100", "verified"))
    session.flush()
    return orders


def _reconcile(session, trigger_history):
    reconcile_trigger_take_profit_order_history(
        session,
        positions=[{"posId": POS_ID, "pos": "10"}],
        pending_orders=[],
        trigger_history=trigger_history,
        order_history=[],
        trade_fills=[],
        observed_at=datetime(2026, 9, 28, 5, 41),
    )


def _cancelled(order_id):
    return {"ordId": order_id, "posId": POS_ID, "posSide": "short", "sz": "5", "state": "canceled"}


def test_a_superseded_original_beside_its_verified_replacement_does_not_break_the_round(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        orders = _seed(session)

        _reconcile(session, [_cancelled(TP3)])

        # Before the fix this raised MultipleResultsFound on the first order.
        assert orders[TP2].status == "active"
        assert orders[TP3].status == "cancelled"


def test_the_owner_is_the_non_superseded_row():
    session_factory = create_session_factory(":memory:")
    with session_factory() as session:
        orders = _seed(session)

        owner = tp_orders._take_profit_protection_leg_for_order(session, orders[TP2])

        assert owner is not None
        assert owner.leg_index == 4
        assert owner.status == "verified"


def test_an_ambiguous_order_is_skipped_with_a_warning_and_the_round_goes_on(tmp_path):
    recorder = _Recorder()
    tp_orders.logger.addHandler(recorder)
    try:
        session_factory = create_session_factory(tmp_path / "research.db")
        with session_factory() as session:
            orders = _seed(session, extra_verified=True)

            owner = tp_orders._take_profit_protection_leg_for_order(session, orders[TP2])
            _reconcile(session, [_cancelled(TP2), _cancelled(TP3)])

            assert owner is None
            # The ambiguous order still settles from its exchange history;
            # only the TP1 fill proof is skipped for it.
            assert orders[TP2].status == "cancelled"
            assert orders[TP3].status == "cancelled"
    finally:
        tp_orders.logger.removeHandler(recorder)

    messages = [record.getMessage() for record in recorder.records]
    assert any(
        "take-profit protection leg ambiguous" in message and TP2 in message
        for message in messages
    )


def test_a_single_non_verified_live_row_is_still_the_owner():
    session_factory = create_session_factory(":memory:")
    with session_factory() as session:
        orders = _seed(session, replacement_status="waiting_fill")

        owner = tp_orders._take_profit_protection_leg_for_order(session, orders[TP2])

        assert owner is not None
        assert owner.leg_index == 4
