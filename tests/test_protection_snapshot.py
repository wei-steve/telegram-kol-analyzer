from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PendingTpslSnapshotObservation
from telegram_kol_research.protection_snapshot import (
    build_position_protection_audit,
    observe_pending_tpsl,
    record_pending_tpsl_observation,
)


def test_observation_marks_unknown_pagination_as_incomplete():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}], "nextCursor": "abc"},
    )

    assert observation["complete"] is False
    assert observation["order_ids"] == ["tp-1"]
    assert observation["reason"] == "pagination_metadata_unsupported"


def test_observation_proves_expected_exact_order_ids_visible():
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}, {"ordId": "sl-1"}]},
        expected_order_ids={"tp-1", "sl-1"},
    )

    assert observation["complete"] is True
    assert observation["expected_order_ids_visible"] is True


def test_observation_is_persisted_append_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-observation.db")
    observation = observe_pending_tpsl(
        instrument_id="BTC-USDT-SWAP",
        response={"code": "0", "data": [{"ordId": "tp-1"}]},
    )

    record_pending_tpsl_observation(session_factory, observation=observation)
    record_pending_tpsl_observation(session_factory, observation=observation)

    with session_factory() as session:
        rows = session.query(PendingTpslSnapshotObservation).all()
    assert len(rows) == 2
    assert rows[0].instrument_id == "BTC-USDT-SWAP"
    assert rows[0].response_count == 1
    assert rows[0].order_ids_json == '["tp-1"]'
    assert rows[0].complete is True


def test_position_audit_distinguishes_native_protection_manual_and_submit_response():
    audit = build_position_protection_audit(
        position={
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "0.006",
            "cTime": "1000",
        },
        protection_ledger=[
            {
                "pos_id": "pos-1",
                "purpose": "stop_loss",
                "order_id": "primary-1",
                "trigger_price": "63200",
                "size_text": "0.006",
                "status": "verified",
                "evidence_source": "entry_protection_response",
                "evidence_json": '{"match":"exchange_returned_order_id"}',
            },
            {
                "pos_id": "pos-1",
                "purpose": "take_profit",
                "order_id": "tp-submit-only",
                "trigger_price": "65000",
                "size_text": "0.003",
                "status": "verified",
                "evidence_source": "tpsl_write_response",
                "evidence_json": '{"match":"exchange_returned_order_id"}',
            },
        ],
        backup_stops=[
            {
                "pos_id": "pos-1",
                "order_id": "backup-1",
                "trigger_price": "63073.6",
                "status": "active",
                "request_json": '{"slTriggerPx":"63073.6"}',
            }
        ],
        take_profit_orders=[],
        pending_trigger_orders=[
            {
                "ordId": "primary-1",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "0.006",
                "slTriggerPx": "63200",
            },
            {
                "ordId": "backup-1",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "0.006",
                "slTriggerPx": "63073.6",
            },
            {
                "ordId": "manual-63000",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "0",
                "slTriggerPx": "63000",
            },
        ],
    )

    assert audit["primary_stop"] == {
        "source": "entry",
        "verification_status": "verified",
        "matching_strategy": "order_id",
        "order_id": "primary-1",
    }
    assert audit["backup_stop"] == {
        "protocol": "native",
        "verification_status": "verified",
        "matching_strategy": "order_id",
        "order_id": "backup-1",
    }
    assert audit["take_profits"] == [
        {
            "order_id": "tp-submit-only",
            "verification_status": "submitted_response",
            "matching_strategy": "order_id",
        }
    ]
    assert audit["manual_order_detected"] is True
    assert audit["manual_order_ids"] == ["manual-63000"]
    assert audit["freeze_reasons"] == [
        "manual_or_unowned_native_tpsl",
        "submitted_response_not_verified",
    ]
    assert audit["protected"] is False


def test_position_audit_marks_legacy_generic_backup_as_unprotected():
    audit = build_position_protection_audit(
        position={
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "0.006",
            "cTime": "1000",
        },
        protection_ledger=[],
        backup_stops=[
            {
                "pos_id": "pos-1",
                "order_id": "generic-backup-1",
                "trigger_price": "63073.6",
                "status": "active",
                "request_json": '{"triggerPrice":"63073.6","closePosId":"pos-1"}',
            }
        ],
        take_profit_orders=[],
        pending_trigger_orders=[],
    )

    assert audit["backup_stop"] == {
        "protocol": "generic",
        "verification_status": "unverified_exchange",
        "matching_strategy": "not_applicable",
        "order_id": "generic-backup-1",
    }
    assert audit["freeze_reasons"] == [
        "backup_stop_unverified_exchange",
        "primary_stop_missing",
    ]
    assert audit["protected"] is False
