"""A take profit that traded is not missing protection.

``trigger-orders-history`` carries no ``state`` field, so
``protection_health._successful_close`` could never see a successful close and
every filled take profit was recorded as ``protection_missing`` with a critical
incident beside it.  Production leg 579 got one eight seconds after TP1 filled,
and the incident is unerasable, which is what put the leg permanently into the
protection-recovery branch.
"""

from datetime import UTC, datetime

from deepcoin_production_rows import (
    pending_stop_row,
    position_row,
    trigger_history_row,
)

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
)
from telegram_kol_research.protection_health import (
    reconcile_position_protection_health,
)


NOW = datetime(2026, 9, 21, 7, 0, tzinfo=UTC).replace(tzinfo=None)
INSTRUMENT = "ETH-USDT-SWAP"
POS_ID = "1001125231241310"


def _session_factory(tmp_path):
    return create_session_factory(tmp_path / "health.db")


def _seed(session, *, purpose, order_id, trigger_price, size_text, status="verified"):
    session.add(
        ExecutionBinding(
            id=363,
            venue="deepcoin",
            kol_id=1,
            chat_id=-100,
            message_id=4501,
            symbol="ETH",
            side="long",
            status="open",
            strategy_instance_id="strategy-eth-long",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        ExecutionOrderLeg(
            id=579,
            execution_binding_id=363,
            strategy_instance_id="strategy-eth-long",
            venue="deepcoin",
            leg_index=0,
            purpose="entry",
            order_kind="market",
            status="filled",
            attribution_status="verified",
            pos_id=POS_ID,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=363,
            execution_order_leg_id=579,
            strategy_instance_id="strategy-eth-long",
            pos_id=POS_ID,
            instrument_id=INSTRUMENT,
            side="long",
            order_id=order_id,
            purpose=purpose,
            trigger_price=trigger_price,
            size_text=size_text,
            status=status,
            evidence_source="exchange_adopted_by_tu",
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()


def _positions():
    return [
        position_row(
            pos_id=POS_ID,
            inst_id=INSTRUMENT,
            pos_side="long",
            size="0.8",
            avg_price="2650",
        )
    ]


def _run(session, *, pending=(), history=()):
    return reconcile_position_protection_health(
        session,
        positions=_positions(),
        pending_orders=list(pending),
        trigger_history=list(history),
        snapshot_errors={},
        observed_at=NOW,
    )


def _ledger_row(session, order_id):
    return (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.order_id == order_id)
        .one()
    )


def test_a_take_profit_with_a_clean_trigger_is_recorded_as_filled(tmp_path):
    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-1",
            trigger_price="2690",
            size_text="0.7",
        )

    with factory() as session:
        created = _run(
            session,
            history=[
                trigger_history_row(
                    ord_id="tp-1",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2690",
                    size="0.7",
                )
            ],
        )
        session.commit()

    assert created == 0
    with factory() as session:
        assert _ledger_row(session, "tp-1").status == "filled"
        assert session.query(PositionProtectionIncident).count() == 0


def test_a_recorded_filled_take_profit_order_is_enough_on_its_own(tmp_path):
    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-1",
            trigger_price="2690",
            size_text="0.7",
        )
        session.add(
            PositionTakeProfitOrder(
                venue="deepcoin",
                execution_binding_id=363,
                execution_order_leg_id=579,
                pos_id=POS_ID,
                order_id="tp-1",
                trigger_price="2690",
                size_text="0.7",
                status="filled",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with factory() as session:
        created = _run(session)
        session.commit()

    assert created == 0
    with factory() as session:
        assert _ledger_row(session, "tp-1").status == "filled"
        assert session.query(PositionProtectionIncident).count() == 0


def test_a_failed_trigger_is_still_a_stop_trigger_failed_incident(tmp_path):
    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-1",
            trigger_price="2690",
            size_text="0.7",
        )

    with factory() as session:
        created = _run(
            session,
            history=[
                trigger_history_row(
                    ord_id="tp-1",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2690",
                    size="0.7",
                    error_code="51004",
                    error_message="rejected",
                )
            ],
        )
        session.commit()

    assert created == 1
    with factory() as session:
        assert _ledger_row(session, "tp-1").status == "stop_trigger_failed"


def test_a_take_profit_that_simply_vanished_is_still_protection_missing(tmp_path):
    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-1",
            trigger_price="2690",
            size_text="0.7",
        )

    with factory() as session:
        created = _run(session)
        session.commit()

    assert created == 1
    with factory() as session:
        assert _ledger_row(session, "tp-1").status == "protection_missing"
        incident = session.query(PositionProtectionIncident).one()
        assert incident.incident_type == "protection_missing"


def test_a_vanished_stop_is_never_written_as_filled(tmp_path):
    """Only take profits take this path.  A stop is the position's downside."""

    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="stop_loss",
            order_id="stop-1",
            trigger_price="2600",
            size_text="1.5",
        )

    with factory() as session:
        created = _run(
            session,
            history=[
                trigger_history_row(
                    ord_id="stop-1",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2600",
                    size="1.5",
                    purpose="stop_loss",
                )
            ],
        )
        session.commit()

    assert created == 1
    with factory() as session:
        assert _ledger_row(session, "stop-1").status == "protection_missing"


def test_a_take_profit_still_resting_stays_verified(tmp_path):
    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-2",
            trigger_price="2720",
            size_text="0.4",
        )

    with factory() as session:
        created = _run(
            session,
            pending=[
                pending_stop_row(
                    ord_id="tp-2",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2720",
                    size="0.4",
                )
            ],
        )
        session.commit()

    assert created == 0
    with factory() as session:
        assert _ledger_row(session, "tp-2").status == "verified"


def test_a_filled_ledger_row_is_not_re_examined_on_the_next_round(tmp_path):
    """``filled`` is terminal: it leaves the health query's status set."""

    factory = _session_factory(tmp_path)
    with factory() as session:
        _seed(
            session,
            purpose="take_profit",
            order_id="tp-1",
            trigger_price="2690",
            size_text="0.7",
            status="filled",
        )

    with factory() as session:
        created = _run(session)
        session.commit()

    assert created == 0
    with factory() as session:
        assert _ledger_row(session, "tp-1").status == "filled"
        assert session.query(PositionProtectionIncident).count() == 0
