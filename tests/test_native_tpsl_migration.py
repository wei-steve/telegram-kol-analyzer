from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLedger,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


class _Client:
    def __init__(
        self,
        *,
        native_after_submit: bool = True,
        submit_error: Exception | None = None,
        cancel_response: object | None = None,
        mutate_legacy_after_submit: dict[str, str] | None = None,
    ):
        self.native_after_submit = native_after_submit
        self.submit_error = submit_error
        self.cancel_response = cancel_response or {"code": "0", "data": "legacy-backup-1"}
        self.mutate_legacy_after_submit = mutate_legacy_after_submit
        self.pending_rows: list[dict[str, str]] = [
            {
                "ordId": "legacy-backup-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "triggerOrderType": "TRIGGER",
                "triggerPrice": "62804.1",
                "orderType": "market",
                "closePosId": "pos-1",
                "sz": "6",
            }
        ]
        self.set_position_sltp_payloads: list[dict[str, str]] = []
        self.cancel_trigger_order_payloads: list[dict[str, str]] = []

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT-SWAP", "posId": "pos-1", "posSide": "long",
            "pos": "6", "avgPx": "62930", "mgnMode": "cross",
            "mrgPosition": "split", "cTime": "1000",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending_rows if row["instId"] == inst_id]

    def set_position_sltp(self, payload):
        self.set_position_sltp_payloads.append(dict(payload))
        if self.submit_error is not None:
            raise self.submit_error
        if self.native_after_submit:
            self.pending_rows.append({
                "ordId": "native-backup-1", "instId": payload["instId"],
                "posId": payload["posId"], "posSide": payload["posSide"],
                "triggerOrderType": "TPSL", "slTriggerPrice": payload["slTriggerPx"],
                "sz": "0", "cTime": "1001",
            })
        if self.mutate_legacy_after_submit is not None:
            self.pending_rows[0].update(self.mutate_legacy_after_submit)
        return {"code": "0", "data": "native-backup-1"}

    def cancel_trigger_order(self, payload):
        self.cancel_trigger_order_payloads.append(dict(payload))
        return self.cancel_response


def _seed(session_factory, *, primary: bool = True):
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="long",
            venue="deepcoin", margin_mode="cross", position_mode="split", status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry", order_kind="market",
            strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin", pos_id="pos-1", status="active", attribution_status="verified",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        assert leg is not None
        leg.attribution_evidence_json = '{"policy_version":2}'
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, pos_id="pos-1", instrument_id="BTC-USDT-SWAP",
            side="long", trigger_price="62804.1", order_id="legacy-backup-1",
            client_order_id="legacy-client", status="active",
            request_json=(
                '{"closePosId":"pos-1","instId":"BTC-USDT-SWAP",'
                '"orderType":"market","triggerPrice":"62804.1"}'
            ),
        ))
        if primary:
            upsert_protection_ledger_row(
                session, venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP", side="long", order_id="primary-1",
                purpose="stop_loss", trigger_price="62930", size_text="6", status="verified",
                evidence_source="test", evidence={}, seen_at=NOW,
            )
        session.commit()


def _plan(session_factory, client):
    from telegram_kol_research.native_tpsl_migration import build_native_tpsl_migration_plan

    return build_native_tpsl_migration_plan(session_factory, deepcoin_client=client, now=NOW)


def _apply(session_factory, client, plan):
    from telegram_kol_research.native_tpsl_migration import apply_native_tpsl_migration_plan

    return apply_native_tpsl_migration_plan(
        session_factory, plan, deepcoin_client=client, pos_id="pos-1",
        action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="migration-confirm-test", now=NOW,
    )


def test_migration_cancels_owned_legacy_generic_only_after_native_readback(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()

    plan = _plan(session_factory, client)
    assert [action.pos_id for action in plan.actions] == ["pos-1"]
    assert client.cancel_trigger_order_payloads == []

    result = _apply(session_factory, client, plan)

    assert result.status == "migrated"
    assert client.set_position_sltp_payloads == [{
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-1",
        "posSide": "long", "mrgPosition": "split", "tdMode": "cross",
        "slTriggerPx": "62804.1", "slTriggerPxType": "last", "slOrdPx": "-1",
    }]
    assert client.cancel_trigger_order_payloads == [{
        "instId": "BTC-USDT-SWAP", "ordId": "legacy-backup-1",
    }]
    with session_factory() as session:
        rows = session.query(PositionBackupStopOrder).order_by(PositionBackupStopOrder.id).all()
        assert [(row.order_id, row.status) for row in rows] == [
            ("legacy-backup-1", "migrated"),
            ("native-backup-1", "active"),
        ]
        ledger = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id == "native-backup-1"
        ).one()
        assert (ledger.pos_id, ledger.purpose, ledger.status) == (
            "pos-1",
            "stop_loss",
            "verified",
        )
        assert session.query(ExecutionEvent).order_by(ExecutionEvent.id.desc()).first().reason == "legacy_generic_cancelled"


@pytest.mark.parametrize(
    "legacy_mutation",
    [
        {"closePosId": "another-position"},
        {"triggerPrice": "62800"},
        {"orderType": "limit"},
        {"side": "buy"},
    ],
)
def test_migration_freezes_when_owned_generic_payload_does_not_exactly_close_position(
    tmp_path,
    legacy_mutation,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending_rows[0].update(legacy_mutation)

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert client.cancel_trigger_order_payloads == []


def test_migration_rereads_generic_payload_before_cancel_and_freezes_if_changed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(mutate_legacy_after_submit={"closePosId": "another-position"})
    plan = _plan(session_factory, client)

    result = _apply(session_factory, client, plan)

    assert result.status == "legacy_pending_recheck_failed"
    assert client.cancel_trigger_order_payloads == []
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"


@pytest.mark.parametrize(
    ("cancel_response", "expected_status", "expected_reason"),
    [
        ({"code": "1", "data": "legacy-backup-1"}, "legacy_cancel_rejected", "legacy_cancel_rejected"),
        ({"code": "0", "data": {}}, "legacy_cancel_unconfirmed", "legacy_cancel_response_unconfirmed"),
        ({"code": "0", "data": "other-order"}, "legacy_cancel_unconfirmed", "legacy_cancel_response_unconfirmed"),
        ({"data": "legacy-backup-1"}, "legacy_cancel_unconfirmed", "legacy_cancel_response_unconfirmed"),
    ],
)
def test_migration_never_marks_legacy_cancelled_without_exact_success_response(
    tmp_path,
    cancel_response,
    expected_status,
    expected_reason,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(cancel_response=cancel_response)
    plan = _plan(session_factory, client)

    result = _apply(session_factory, client, plan)

    assert result.status == expected_status
    assert len(client.cancel_trigger_order_payloads) == 1
    with session_factory() as session:
        rows = session.query(PositionBackupStopOrder).order_by(PositionBackupStopOrder.id).all()
        assert [(row.order_id, row.status) for row in rows] == [
            ("legacy-backup-1", "migration_cancel_pending"),
            ("native-backup-1", "active"),
        ]
        assert session.query(ExecutionEvent).order_by(ExecutionEvent.id.desc()).first().reason == expected_reason


@pytest.mark.parametrize(
    ("pending_row", "reason"),
    [
        (
            {
                "ordId": "manual-63000", "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "slTriggerPrice": "63000", "sz": "0", "cTime": "1001",
            },
            "native_stop_unowned",
        ),
        (
            {
                "ordId": "native-no-pos-id", "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "slTriggerPrice": "62804.1", "sz": "0", "cTime": "1001",
            },
            "native_stop_unowned",
        ),
    ],
)
def test_migration_freezes_unowned_native_stop_and_never_cancels(tmp_path, pending_row, reason):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending_rows.append(pending_row)

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": reason},)
    assert client.cancel_trigger_order_payloads == []


def test_migration_freezes_when_multiple_no_pos_id_native_candidates_exist(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending_rows.extend([
        {
            "ordId": f"manual-{index}", "instId": "BTC-USDT-SWAP", "posSide": "long",
            "triggerOrderType": "TPSL", "slTriggerPrice": str(63000 - index), "sz": "0", "cTime": "1001",
        }
        for index in (1, 2)
    ])

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "native_stop_unowned"},)
    assert client.cancel_trigger_order_payloads == []


def test_migration_freezes_without_verified_primary_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory, primary=False)
    client = _Client()

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "primary_stop_not_verified"},)
    assert client.cancel_trigger_order_payloads == []


def test_migration_freezes_when_legacy_quantity_differs_from_exact_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending_rows[0]["sz"] = "5"

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "legacy_backup_size_mismatch"},)
    assert client.cancel_trigger_order_payloads == []


def test_migration_api_uncertainty_never_cancels_legacy_backup(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(submit_error=RuntimeError("request outcome unknown"))
    plan = _plan(session_factory, client)

    result = _apply(session_factory, client, plan)

    assert result.status == "native_submit_unknown"
    assert client.cancel_trigger_order_payloads == []
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
        assert row.status == "active"


def test_migration_pending_readback_never_cancels_legacy_backup(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(native_after_submit=False)
    plan = _plan(session_factory, client)

    result = _apply(session_factory, client, plan)

    assert result.status == "native_pending_readback"
    assert client.cancel_trigger_order_payloads == []
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
        assert row.status == "active"


def _migration_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_native_tpsl_protection.py"
    spec = importlib.util.spec_from_file_location("migration_script_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_cli_is_dry_run_by_default_and_execute_requires_exact_target():
    module = _migration_script_module()

    assert module._parse_args([]).execute is False
    with pytest.raises(SystemExit, match="2"):
        module._parse_args(["--execute"])
    with pytest.raises(SystemExit, match="2"):
        module._parse_args(["--execute", "--position-id", "pos-1"])
    with pytest.raises(SystemExit, match="2"):
        module._parse_args([
            "--execute", "--position-id", "pos-1",
            "--expected-fingerprint", "fingerprint",
        ])
    args = module._parse_args([
        "--execute", "--position-id", "pos-1",
        "--action-id", "action-1",
        "--expected-fingerprint", "fingerprint",
        "--confirmation-token", "confirm-once",
    ])
    assert args.execute is True
    assert args.position_id == "pos-1"
    assert args.action_id == "action-1"
    assert args.confirmation_token == "confirm-once"
