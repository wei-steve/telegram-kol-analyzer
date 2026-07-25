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
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


class _Client:
    def __init__(self):
        self.trigger_payloads = []

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return []

    def trigger_order(self, payload):
        self.trigger_payloads.append(dict(payload))
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
