from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PendingTpslSnapshotObservation
from telegram_kol_research.protection_snapshot import (
    build_position_protection_audit,
    observe_pending_tpsl,
    record_pending_tpsl_observation,
)
from telegram_kol_research.protection_ledger import (
    build_account_protection_ownership,
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


def test_position_audit_prefers_verified_composite_backup_and_ignores_cancelled_tp():
    audit = build_position_protection_audit(
        position={
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "9",
        },
        protection_ledger=[
            {
                "id": 10,
                "pos_id": "pos-1",
                "purpose": "stop_loss",
                "order_id": "old-primary",
                "trigger_price": "62800",
                "size_text": "18",
                "status": "cancelled",
            },
            {
                "id": 11,
                "pos_id": "pos-1",
                "purpose": "take_profit",
                "order_id": "old-tp",
                "trigger_price": "65700",
                "size_text": "18",
                "status": "cancelled",
            },
            {
                "id": 12,
                "pos_id": "pos-1",
                "purpose": "stop_loss",
                "order_id": "new-primary",
                "trigger_price": "63900",
                "size_text": "9",
                "status": "verified",
                "evidence_source": "position_mutation_intent_readback",
            },
            {
                "id": 13,
                "pos_id": "pos-1",
                "purpose": "backup_stop",
                "order_id": "new-backup",
                "trigger_price": "63772.2",
                "size_text": "9",
                "status": "verified",
                "evidence_source": "position_mutation_intent_readback",
            },
            {
                "id": 14,
                "pos_id": "pos-1",
                "purpose": "backup_stop",
                "order_id": "failed-backup",
                "trigger_price": "63600",
                "size_text": "9",
                "status": "stop_trigger_failed",
            },
            {
                "id": 15,
                "pos_id": "pos-1",
                "purpose": "backup_stop",
                "order_id": "missing-backup",
                "trigger_price": "63500",
                "size_text": "9",
                "status": "protection_missing",
            },
        ],
        backup_stops=[
            {
                "id": 99,
                "pos_id": "pos-1",
                "order_id": "stale-backup",
                "trigger_price": "62674.4",
                "status": "missing",
                "request_json": '{"slTriggerPx":"62674.4"}',
            }
        ],
        take_profit_orders=[
            {
                "id": 100,
                "pos_id": "pos-1",
                "order_id": "old-tp",
                "trigger_price": "65700",
                "size_text": "18",
                "status": "active",
            }
        ],
        pending_trigger_orders=[
            {
                "ordId": "new-primary",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "9",
                "slTriggerPx": "63900",
            },
            {
                "ordId": "new-backup",
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "9",
                "slTriggerPx": "63772.2",
            },
        ],
    )

    assert audit["primary_stop"]["order_id"] == "new-primary"
    assert audit["backup_stop"] == {
        "protocol": "native",
        "verification_status": "verified",
        "matching_strategy": "order_id",
        "order_id": "new-backup",
    }
    assert audit["take_profits"] == []
    assert audit["has_verified_backup_stop"] is True
    assert audit["readback_complete"] is True
    assert audit["automation_safe"] is True
    assert audit["protected"] is True


def test_position_audit_flags_terminal_local_order_that_is_still_pending():
    audit = build_position_protection_audit(
        position={
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "9",
        },
        protection_ledger=[
            {
                "id": 1,
                "pos_id": "pos-1",
                "purpose": "stop_loss",
                "order_id": "primary",
                "trigger_price": "63900",
                "size_text": "9",
                "status": "verified",
            },
            {
                "id": 2,
                "pos_id": "pos-1",
                "purpose": "backup_stop",
                "order_id": "backup",
                "trigger_price": "63772.2",
                "size_text": "9",
                "status": "verified",
            },
            {
                "id": 3,
                "pos_id": "pos-1",
                "purpose": "take_profit",
                "order_id": "cancelled-tp-still-pending",
                "trigger_price": "65700",
                "size_text": "9",
                "status": "cancelled",
            },
        ],
        backup_stops=[],
        take_profit_orders=[],
        pending_trigger_orders=[
            {
                "ordId": order_id,
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "sz": "9",
                trigger_field: trigger_price,
            }
            for order_id, trigger_field, trigger_price in (
                ("primary", "slTriggerPx", "63900"),
                ("backup", "slTriggerPx", "63772.2"),
                ("cancelled-tp-still-pending", "tpTriggerPx", "65700"),
            )
        ],
    )

    assert audit["take_profits"] == []
    assert audit["manual_order_ids"] == ["cancelled-tp-still-pending"]
    assert audit["has_unowned_orders"] is True
    assert audit["automation_safe"] is False
    assert audit["protected"] is False


def test_position_audit_does_not_borrow_other_positions_ledger_orders():
    positions = [
        {"posId": "pos-a", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"},
        {"posId": "pos-b", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "5"},
    ]
    pending = [
        {
            "ordId": "sl-a",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPx": "61000",
        },
        {
            "ordId": "sl-b",
            "triggerOrderType": "TPSL",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "slTriggerPx": "60000",
        },
    ]
    ledger = [
        {
            "venue": "deepcoin",
            "order_id": "sl-a",
            "pos_id": "pos-a",
            "status": "verified",
            "purpose": "stop_loss",
            "trigger_price": "61000",
            "size_text": "0",
        },
        {
            "venue": "deepcoin",
            "order_id": "sl-b",
            "pos_id": "pos-b",
            "status": "verified",
            "purpose": "stop_loss",
            "trigger_price": "60000",
            "size_text": "0",
        },
    ]
    ownership = build_account_protection_ownership(
        ledger,
        live_pos_ids={"pos-a", "pos-b"},
    )

    audit_a = build_position_protection_audit(
        position=positions[0],
        protection_ledger=ledger,
        backup_stops=[],
        take_profit_orders=[],
        pending_trigger_orders=pending,
        open_positions=positions,
        account_ownership=ownership,
    )
    audit_b = build_position_protection_audit(
        position=positions[1],
        protection_ledger=ledger,
        backup_stops=[],
        take_profit_orders=[],
        pending_trigger_orders=pending,
        open_positions=positions,
        account_ownership=ownership,
    )

    assert audit_a["manual_order_ids"] == []
    assert audit_b["manual_order_ids"] == []
    assert audit_a["has_verified_stop"] is True
    assert audit_b["has_verified_stop"] is True
