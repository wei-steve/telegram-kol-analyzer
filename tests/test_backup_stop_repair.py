from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import StaticDeepcoinContractSpecProvider
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionOrderLeg, PositionBackupStopOrder
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


class _Client:
    def __init__(self, *, verify_after_submit=False):
        self.trigger_payloads = []
        self.verify_after_submit = verify_after_submit
        self.pending_rows = []

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending_rows if row["instId"] == inst_id]

    def trigger_order(self, payload):
        self.trigger_payloads.append(dict(payload))
        if self.verify_after_submit:
            self.pending_rows.append({
                "ordId": "backup-1", "instId": payload["instId"],
                "closePosId": payload["closePosId"], "posSide": payload["posSide"],
                "orderType": payload["orderType"], "triggerPrice": payload["triggerPrice"],
            })
        return {"code": "0", "data": "backup-1"}


def _provider():
    return StaticDeepcoinContractSpecProvider({
        "ETH-USDT-SWAP": DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP", contract_value=0.1, quantity_step=0.1,
            min_quantity=0.1, price_tick=0.1,
        )
    })


def _seed(session_factory):
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="ETH", side="short",
            venue="deepcoin", margin_mode="cross", position_mode="split", status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry", order_kind="market",
            venue="deepcoin", pos_id="pos-1", status="active", attribution_status="verified",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP", side="short", order_id="primary-1",
            purpose="stop_loss", trigger_price="1900", size_text="4.4", status="verified",
            evidence_source="test", evidence={}, seen_at=NOW,
        )
        session.commit()


def test_repair_plan_is_read_only_fingerprinted_and_uses_twenty_bps(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    assert client.trigger_payloads == []
    assert plan.conflicts == ()
    assert plan.fingerprint
    assert [(action.pos_id, action.primary_order_id, action.backup_stop) for action in plan.actions] == [
        ("pos-1", "primary-1", "1903.8")
    ]


def test_repair_plan_uses_persisted_backup_stop_buffer_setting(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    save_trading_settings(session_factory, {"trigger_backup_stop_buffer_bps": 25})
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=_Client(), contract_spec_provider=_provider(), now=NOW
    )
    assert plan.actions[0].backup_stop == "1904.8"


def test_repair_plan_flags_local_active_backup_missing_from_exchange(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id, pos_id="pos-1", instrument_id="ETH-USDT-SWAP",
            side="short", order_id="backup-local", trigger_price="1903.8",
            client_order_id="backup-client", status="active", request_json="{}",
        ))
        session.commit()

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=_Client(), contract_spec_provider=_provider(), now=NOW
    )
    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "backup_stop_missing_on_exchange"},)


def test_targeted_backup_repair_gate_ignores_unrelated_conflicts():
    from telegram_kol_research.backup_stop_repair import BackupStopRepairPlan
    from telegram_kol_research.cli import _backup_stop_conflicts_for_target

    plan = BackupStopRepairPlan(
        created_at=NOW,
        actions=(),
        conflicts=(
            {"pos_id": "blocked-pos", "reason": "primary_stop_not_verified"},
            {"pos_id": "target-pos", "reason": "backup_exchange_outcome_unknown"},
        ),
        database_fingerprint="database",
        exchange_fingerprint="exchange",
        fingerprint="plan",
    )

    assert _backup_stop_conflicts_for_target(plan, pos_id="safe-pos") == ()
    assert _backup_stop_conflicts_for_target(plan, pos_id="target-pos") == (
        {"pos_id": "target-pos", "reason": "backup_exchange_outcome_unknown"},
    )


def test_repair_apply_requires_one_position_and_exact_fingerprint(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    with pytest.raises(ValueError, match="pos_id"):
        apply_backup_stop_repair_plan(
            session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
            pos_id="", expected_fingerprint=plan.fingerprint, now=NOW,
        )
    with pytest.raises(ValueError, match="fingerprint"):
        apply_backup_stop_repair_plan(
            session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
            pos_id="pos-1", expected_fingerprint="wrong", now=NOW,
        )
    assert client.trigger_payloads == []


def test_repair_apply_marks_unverified_exchange_result_unknown_without_retry(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", expected_fingerprint=plan.fingerprint, now=NOW,
    )

    assert result.status == "unknown_exchange_outcome"
    assert len(client.trigger_payloads) == 1
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert row.status == "unknown_exchange_outcome"
