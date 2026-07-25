from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import PositionBackupStopOrder


NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class _ReadonlyClient:
    """A fake whose write methods fail if reconciliation attempts to use them."""

    def __init__(self, *, pending=(), history=(), positions=()):
        self.pending = list(pending)
        self.history = list(history)
        self.positions = list(positions)
        self.read_calls: list[tuple[str, str]] = []
        self.write_calls: list[object] = []

    def list_positions(self, *, inst_id=None):
        self.read_calls.append(("positions", str(inst_id)))
        return list(self.positions)

    def list_trigger_orders_pending(self, *, inst_id):
        self.read_calls.append(("pending", inst_id))
        return list(self.pending)

    def list_trigger_order_history(self, *, inst_id):
        self.read_calls.append(("history", inst_id))
        return list(self.history)

    def __getattr__(self, name):
        if name in {
            "set_position_sltp", "trigger_order", "cancel_position_sltp",
            "cancel_trigger_order", "cancel_order", "place_order",
        }:
            def _write(*args, **kwargs):
                self.write_calls.append((name, args, kwargs))
                raise AssertionError(f"reconciliation must not call {name}")

            return _write
        raise AttributeError(name)


def _seed(session_factory, *, pos_id="pos-1", order_id="legacy-1"):
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
            venue="deepcoin", pos_id=pos_id, status="active", attribution_status="verified",
        ),
    )
    with session_factory() as session:
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=binding_id, execution_order_leg_id=leg_id,
            pos_id=pos_id, instrument_id="BTC-USDT-SWAP", side="long",
            trigger_price="62804.1", order_id=order_id, client_order_id="legacy-client",
            status="active",
            request_json=json.dumps({
                "instId": "BTC-USDT-SWAP", "closePosId": pos_id,
                "triggerPrice": "62804.1", "orderType": "market",
            }),
        ))
        session.commit()


def _plan(session_factory, client):
    from telegram_kol_research.legacy_backup_reconciliation import (
        build_legacy_backup_reconciliation_plan,
    )

    return build_legacy_backup_reconciliation_plan(
        session_factory, deepcoin_client=client, now=NOW,
    )


def test_reconciliation_marks_generic_order_absent_from_pending_and_history_unverified(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _ReadonlyClient(positions=[{"posId": "pos-1", "instId": "BTC-USDT-SWAP"}])

    plan = _plan(session_factory, client)

    assert [(action.pos_id, action.order_id) for action in plan.actions] == [("pos-1", "legacy-1")]
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"

    from telegram_kol_research.legacy_backup_reconciliation import (
        apply_legacy_backup_reconciliation_plan,
    )

    result = apply_legacy_backup_reconciliation_plan(
        session_factory, plan, deepcoin_client=client,
        expected_fingerprint=plan.fingerprint, now=NOW,
    )

    assert result.updated_pos_ids == ("pos-1",)
    assert client.write_calls == []
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
        assert row.status == "unverified_exchange"
        assert json.loads(row.error_json)["reason"] == "legacy_order_absent_from_pending_and_history"


@pytest.mark.parametrize("pending,history", [
    ([{"ordId": "legacy-1"}], []),
    ([], [{"ordId": "legacy-1"}]),
    ([{"ordId": "legacy-1"}], [{"ordId": "legacy-1"}]),
])
def test_reconciliation_leaves_pending_or_ambiguous_legacy_order_unchanged(tmp_path, pending, history):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _ReadonlyClient(pending=pending, history=history)

    plan = _plan(session_factory, client)

    assert plan.actions == ()
    assert {conflict["reason"] for conflict in plan.conflicts} == {"legacy_order_present_or_ambiguous"}
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"


def _reconciliation_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_legacy_backup_status.py"
    spec = importlib.util.spec_from_file_location("legacy_backup_reconciliation_script_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconciliation_cli_dry_run_does_not_write_and_execute_requires_fingerprint(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    _seed(session_factory)
    client = _ReadonlyClient()
    module = _reconciliation_script_module()

    assert module.main(
        ["--database-path", str(database_path)],
        client_builder=lambda: client,
    ) == 0
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"
    assert client.write_calls == []

    with pytest.raises(SystemExit, match="2"):
        module._parse_args(["--execute"])

    plan = _plan(session_factory, client)
    assert module.main([
        "--database-path", str(database_path), "--execute",
        "--expected-fingerprint", plan.fingerprint,
    ], client_builder=lambda: client) == 0
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "unverified_exchange"
    assert client.write_calls == []
