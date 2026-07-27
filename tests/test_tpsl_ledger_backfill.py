from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
)
from telegram_kol_research.tpsl_ledger_backfill import (
    apply_tpsl_ledger_backfill_plan,
    build_tpsl_ledger_backfill_plan,
)


def _seed_exact_business_orders(session_factory):
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="kol",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="deepcoin:1:2:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="entry-1",
            pos_id="pos-1",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        session.add(leg)
        session.flush()
        session.add_all(
            [
                PositionBackupStopOrder(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    pos_id="pos-1",
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    trigger_price="59000",
                    order_id="backup-1",
                    client_order_id="backup-client-1",
                    status="active",
                    request_json='{"slTriggerPx":"59000"}',
                ),
                PositionTakeProfitOrder(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    pos_id="pos-1",
                    order_id="tp-1",
                    trigger_price="65000",
                    size_text="1",
                    status="active",
                    evidence_json="{}",
                ),
            ]
        )
        session.commit()


def _positions():
    return [
        {
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "2",
        }
    ]


def _pending():
    return [
        {
            "ordId": "backup-1",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPrice": "59000",
        },
        {
            "ordId": "tp-1",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "1",
            "tpTriggerPrice": "65000",
        },
    ]


def test_backfill_plan_promotes_exact_business_records_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_business_orders(session_factory)

    plan = build_tpsl_ledger_backfill_plan(
        session_factory,
        positions=_positions(),
        pending_orders=_pending(),
        snapshot_complete=True,
    )

    assert [(row.order_id, row.pos_id, row.source_table) for row in plan.actions] == [
        ("backup-1", "pos-1", "position_backup_stop_orders"),
        ("tp-1", "pos-1", "position_take_profit_orders"),
    ]
    assert plan.refusals == ()
    assert len(plan.fingerprint) == 64


def test_backfill_plan_refuses_incomplete_or_conflicting_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_business_orders(session_factory)

    incomplete = build_tpsl_ledger_backfill_plan(
        session_factory,
        positions=_positions(),
        pending_orders=_pending(),
        snapshot_complete=False,
    )
    conflicting_orders = _pending()
    conflicting_orders[0]["posId"] = "other-pos"
    conflicting = build_tpsl_ledger_backfill_plan(
        session_factory,
        positions=_positions(),
        pending_orders=conflicting_orders,
        snapshot_complete=True,
    )

    assert incomplete.actions == ()
    assert incomplete.refusals[0].reason == "pending_snapshot_incomplete"
    assert conflicting.refusals[0].order_id == "backup-1"
    assert conflicting.refusals[0].reason == "exchange_position_conflict"


def test_backfill_apply_is_atomic_and_database_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_business_orders(session_factory)
    plan = build_tpsl_ledger_backfill_plan(
        session_factory,
        positions=_positions(),
        pending_orders=_pending(),
        snapshot_complete=True,
    )

    result = apply_tpsl_ledger_backfill_plan(
        session_factory,
        plan,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="canonical-ledger-confirm",
        fresh_plan_builder=lambda: build_tpsl_ledger_backfill_plan(
            session_factory,
            positions=_positions(),
            pending_orders=_pending(),
            snapshot_complete=True,
        ),
    )

    assert result.applied == 2
    assert result.exchange_write_count == 0
    with session_factory() as session:
        rows = session.query(PositionProtectionLedger).order_by(
            PositionProtectionLedger.order_id
        ).all()
    assert [(row.order_id, row.pos_id) for row in rows] == [
        ("backup-1", "pos-1"),
        ("tp-1", "pos-1"),
    ]


def test_backfill_apply_refuses_changed_fingerprint_without_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_business_orders(session_factory)
    plan = build_tpsl_ledger_backfill_plan(
        session_factory,
        positions=_positions(),
        pending_orders=_pending(),
        snapshot_complete=True,
    )

    try:
        apply_tpsl_ledger_backfill_plan(
            session_factory,
            plan,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="canonical-ledger-change",
            fresh_plan_builder=lambda: build_tpsl_ledger_backfill_plan(
                session_factory,
                positions=_positions(),
                pending_orders=_pending()[:1],
                snapshot_complete=True,
            ),
        )
    except ValueError as exc:
        assert str(exc) == "TPSL ledger backfill plan changed"
    else:
        raise AssertionError("changed plan was not refused")

    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0
