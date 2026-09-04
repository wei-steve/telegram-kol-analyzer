import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionHealthObservation,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
)
from telegram_kol_research.protection_health import (
    classify_current_position_protection_health,
    current_protection_incident_health_status,
    record_position_protection_health_observation,
)
from telegram_kol_research.protection_ledger import (
    load_account_protection_ownership,
)


NOW = datetime(2026, 8, 7, 4, 0, tzinfo=UTC)


def _healthy_scope(session):
    binding = ExecutionBinding(
        strategy_instance_id="flyang-regression",
        kol_id="flyang",
        chat_id=1,
        message_id=1,
        symbol="BTC",
        side="long",
        status="active",
    )
    session.add(binding)
    session.flush()
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=0,
        purpose="entry",
        order_kind="market",
        pos_id="pos-flyang-regression",
        status="active",
    )
    session.add(leg)
    session.flush()
    incident = PositionProtectionIncident(
        execution_binding_id=binding.id,
        execution_order_leg_id=leg.id,
        pos_id=leg.pos_id,
        incident_type="protection_missing",
        fingerprint="a" * 64,
        evidence_json='{"secret":"must-not-copy"}',
        delivery_status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(incident)
    rows = (
        ("primary-order", "stop_loss", "64100", "6"),
        ("backup-order", "stop_loss", "63971.8", None),
        ("take-profit-order", "take_profit", "67000", "6"),
    )
    for order_id, purpose, trigger_price, size_text in rows:
        session.add(
            PositionProtectionLedger(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=leg.pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=order_id,
                purpose=purpose,
                trigger_price=trigger_price,
                size_text=size_text,
                status="verified",
                evidence_source="readback",
                evidence_json="{}",
            )
        )
    session.add(
        PositionBackupStopOrder(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=leg.pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            trigger_price="63971.8",
            order_id="backup-order",
            client_order_id="backup-client",
            status="active",
            request_json='{"slTriggerPx":"63971.8"}',
        )
    )
    session.add(
        PositionTakeProfitOrder(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=leg.pos_id,
            order_id="take-profit-order",
            trigger_price="67000",
            size_text="6",
            status="active",
        )
    )
    session.flush()
    return binding, leg, incident


def _position():
    return {
        "posId": "pos-flyang-regression",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": "6",
    }


def _pending():
    return [
        {
            "ordId": "primary-order",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "6",
            "slTriggerPx": "64100",
        },
        {
            "ordId": "backup-order",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPx": "63971.8",
        },
        {
            "ordId": "take-profit-order",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "6",
            "tpTriggerPx": "67000",
        },
    ]


def _classify(session, *, pending=None, observations=None, errors=None):
    position = _position()
    ownership = load_account_protection_ownership(
        session, live_pos_ids=[position["posId"]]
    )
    return classify_current_position_protection_health(
        session,
        venue="deepcoin",
        execution_binding_id=1,
        execution_order_leg_id=1,
        pos_id=position["posId"],
        position=position,
        open_positions=[position],
        pending_trigger_orders=_pending() if pending is None else pending,
        pending_tpsl_observations=(
            [{"instrument_id": "BTC-USDT-SWAP", "complete": True}]
            if observations is None
            else observations
        ),
        snapshot_errors={} if errors is None else errors,
        account_ownership=ownership,
        exchange_snapshot_fingerprint="b" * 64,
        source_incident_ids=(1,),
    )


def test_complete_exact_current_evidence_is_healthy_and_append_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _healthy_scope(session)
        session.commit()

    with session_factory() as session:
        result = _classify(session)
        observation = record_position_protection_health_observation(
            session, result=result, observed_at=NOW
        )
        session.commit()
        first_id = observation.id

    assert result.classification == "healthy_current_evidence", result
    assert result.primary_order_id == "primary-order"
    assert result.backup_order_id == "backup-order"
    assert result.take_profit_order_ids == ("take-profit-order",)
    with session_factory() as session:
        stored = session.get(PositionProtectionHealthObservation, first_id)
        assert stored.classification == "healthy_current_evidence"
        rendered = stored.source_incident_ids_json + stored.summary_json
        assert json.loads(stored.source_incident_ids_json) == [1]
        assert "must-not-copy" not in rendered
        assert "primary-order" not in rendered
        assert session.query(PositionProtectionIncident).count() == 1


def test_incomplete_snapshot_is_evidence_insufficient(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _healthy_scope(session)
        session.commit()
    with session_factory() as session:
        result = _classify(
            session,
            observations=[
                {"instrument_id": "BTC-USDT-SWAP", "complete": False}
            ],
        )

    assert result.classification == "evidence_insufficient"
    assert result.reason_codes == ("target_protection_snapshot_incomplete",)


def test_verified_ownership_recovery_resolves_prior_unowned_native_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, leg, incident = _healthy_scope(session)
        incident.incident_type = "native_stop_visible_ownership_unverified"
        session.add(
            PositionProtectionIncident(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id=leg.pos_id,
                incident_type="ownership_recovered",
                fingerprint="r" * 64,
                evidence_json='{"order_id":"primary-order"}',
                delivery_status="not_required",
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()

        status = current_protection_incident_health_status(
            session, incident=incident
        )

    assert status == "resolved_by_verified_attribution"


def test_complete_snapshot_with_missing_take_profit_requires_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _healthy_scope(session)
        session.commit()
    pending = [
        row for row in _pending() if row["ordId"] != "take-profit-order"
    ]
    with session_factory() as session:
        result = _classify(session, pending=pending)

    assert result.classification == "recovery_required"
    assert "verified_take_profit_missing" in result.reason_codes
