import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionReconciliationObservation,
)
from telegram_kol_research.position_reconciliation_observations import (
    build_position_observation_payload,
    record_position_reconciliation_observation,
)


def _owned_leg(session):
    binding = ExecutionBinding(
        strategy_instance_id="deepcoin:1:2:BTC:short",
        kol_id="group:1",
        chat_id=1,
        message_id=2,
        symbol="BTC",
        side="short",
        venue="deepcoin",
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
        order_id="pos-1",
        pos_id="pos-1",
        venue="deepcoin",
        attribution_status="verified",
        status="active",
    )
    session.add(leg)
    session.flush()
    return binding, leg


def test_build_position_observation_payload_is_stable_and_bounded():
    position = {
        "posId": "pos-1",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "pos": "5.000",
        "avgPx": "63076.700",
        "secret": "must-not-survive",
    }
    first = build_position_observation_payload(
        position=position,
        pending_tpsl=[
            {"ordId": "tp-2", "posId": "pos-1", "sz": "2", "triggerPx": "61700"},
            {"ordId": "tp-1", "posId": "pos-1", "sz": "3", "triggerPx": "62400"},
        ],
        complete=True,
    )
    second = build_position_observation_payload(
        position=position,
        pending_tpsl=list(reversed([
            {"ordId": "tp-2", "posId": "pos-1", "sz": "2", "triggerPx": "61700"},
            {"ordId": "tp-1", "posId": "pos-1", "sz": "3", "triggerPx": "62400"},
        ])),
        complete=True,
    )

    assert first == second
    assert first["size_text"] == "5"
    assert first["avg_entry_price"] == "63076.7"
    assert [row["order_id"] for row in first["pending_tpsl"]] == ["tp-1", "tp-2"]
    assert "secret" not in json.dumps(first)


def test_record_position_observation_adopts_duplicate_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    observed_at = datetime(2026, 8, 2, 7, 0, tzinfo=UTC)
    with session_factory() as session:
        binding, leg = _owned_leg(session)
        session.commit()
        first = record_position_reconciliation_observation(
            session,
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            position={
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "5",
                "avgPx": "63076.7",
            },
            pending_tpsl=[
                {"ordId": "tp-2", "posId": "pos-1", "sz": "2", "triggerPx": "61700"}
            ],
            snapshot_complete=True,
            observed_at=observed_at,
        )
        second = record_position_reconciliation_observation(
            session,
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            position={
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "5.0",
                "avgPx": "63076.70",
            },
            pending_tpsl=[
                {"triggerPx": "61700.0", "sz": "2.0", "posId": "pos-1", "ordId": "tp-2"}
            ],
            snapshot_complete=True,
            observed_at=observed_at + timedelta(seconds=10),
        )
        session.commit()

        assert first.id == second.id
        assert session.query(PositionReconciliationObservation).count() == 1
        assert first.snapshot_complete is True


def test_incomplete_position_observation_is_persisted_but_not_complete(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg = _owned_leg(session)
        row = record_position_reconciliation_observation(
            session,
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            position={
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "5",
                "avgPx": "63076.7",
            },
            pending_tpsl=[],
            snapshot_complete=False,
            observed_at=datetime(2026, 8, 2, 7, 0, tzinfo=UTC),
        )
        session.commit()

        assert row.snapshot_complete is False
        assert json.loads(row.pending_tpsl_json) == []
