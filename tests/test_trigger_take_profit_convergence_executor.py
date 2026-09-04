from __future__ import annotations

from datetime import UTC, datetime

import pytest


NOW = datetime(2026, 7, 22, 10, 15, tzinfo=UTC)


class _ContractSpec:
    def __init__(self, *, quantity_step="1", min_quantity="1"):
        self.quantity_step = quantity_step
        self.min_quantity = min_quantity


class _ContractSpecProvider:
    def __init__(self, *, quantity_step="1", min_quantity="1"):
        self.spec = _ContractSpec(quantity_step=quantity_step, min_quantity=min_quantity)

    def get_contract_spec(self, instrument_id):
        return self.spec if instrument_id == "BTC-USDT-SWAP" else None


class _Client:
    def __init__(self):
        self.cancel_calls = []
        self.submit_calls = []
        self.contract_spec_provider = _ContractSpecProvider()
        self.pending = [
            {
                "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short", "ordId": "sl-1",
                "triggerOrderType": "TPSL", "slTriggerPx": "67200", "slOrdPx": "-1", "sz": "10",
            },
            {
                "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short", "ordId": "backup-1",
                "triggerOrderType": "TPSL", "slTriggerPx": "67334.4", "slOrdPx": "-1", "sz": "0",
            },
        ]

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
            "pos": "10", "avgPx": "65000", "mrgPosition": "split",
            "mgnMode": "cross", "cTime": "1000",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def read_trigger_orders_pending(self, *, inst_id):
        return {
            "code": "0",
            "data": self.list_trigger_orders_pending(inst_id=inst_id),
        }

    def cancel_trigger_order(self, payload):
        self.cancel_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def set_position_sltp(self, payload):
        self.submit_calls.append(dict(payload))
        order_id = f"tp-new-{len(self.submit_calls)}"
        self.pending.append({
            "ordId": order_id, "instId": payload["instId"], "posId": payload["posId"],
            "posSide": payload["posSide"], "triggerOrderType": "TPSL",
            "tpTriggerPx": payload["tpTriggerPx"], "tpOrdPx": payload["tpOrdPx"], "tpPrice": "0",
            "sz": payload["sz"],
        })
        return {"code": "0", "data": {"ordId": order_id}}


class _ExactBackupOnlyClient(_Client):
    def __init__(self):
        super().__init__()
        self.contract_spec_provider = _ContractSpecProvider(
            quantity_step="0.1", min_quantity="0.1"
        )
        self.pending = [row for row in self.pending if row["ordId"] == "backup-1"]

    def list_positions(self, *, inst_id=None):
        rows = super().list_positions(inst_id=inst_id)
        rows[0]["pos"] = "3.4"
        return rows


def _ready_convergence(
    session_factory,
    *,
    existing_take_profit=True,
    desired_take_profits=None,
    with_backup=True,
    order_kind="trigger_limit",
):
    from telegram_kol_research.db import create_session_factory  # keep import boundaries explicit
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.models import ExecutionOrderLeg, PositionBackupStopOrder
    from telegram_kol_research.position_take_profit_orders import record_take_profit_order
    from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
    from telegram_kol_research.trigger_take_profit_convergence import (
        create_or_get_trigger_take_profit_convergence,
        mark_trigger_take_profit_convergence_ready,
    )

    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="short",
            venue="deepcoin", margin_mode="cross", position_mode="split",
            pos_id="pos-10", status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            strategy_instance_id="deepcoin:1:1:BTC:short",
            order_kind=order_kind, venue="deepcoin", pos_id="pos-10", status="active",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_status = "verified"
        leg.attribution_evidence_json = '{"policy_version":2}'
        convergence = create_or_get_trigger_take_profit_convergence(
            session, venue="deepcoin", execution_order_leg_id=leg_id,
            desired_take_profits=desired_take_profits or [
                {"price": "64500", "allocation_pct": "50"},
                {"price": "63800", "allocation_pct": "30"},
                {"price": "63100", "allocation_pct": "20"},
            ],
            created_at=NOW,
        )
        mark_trigger_take_profit_convergence_ready(session, convergence, ready_at=NOW)
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id="pos-10",
            instrument_id="BTC-USDT-SWAP", side="short", order_id="sl-1",
            purpose="stop_loss", trigger_price="67200", size_text=None, status="verified",
            evidence_source="test", evidence={}, seen_at=NOW,
        )
        if with_backup:
            session.add(PositionBackupStopOrder(
                venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg_id, pos_id="pos-10",
                instrument_id="BTC-USDT-SWAP", side="short", trigger_price="67334.4",
                order_id="backup-1", client_order_id="backup-client-1", status="active",
                request_json=(
                    '{"instType":"SWAP","instId":"BTC-USDT-SWAP",'
                    '"mrgPosition":"split","posId":"pos-10","posSide":"short",'
                    '"slOrdPx":"-1","slTriggerPx":"67334.4","tdMode":"cross"}'
                ),
            ))
        if existing_take_profit:
            record_take_profit_order(
                session, venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg_id, pos_id="pos-10", order_id="tp-old-1",
                trigger_price="64500", size_text="10", created_at=NOW,
                trigger_take_profit_convergence_id=convergence.id,
                evidence={
                    "source": "native_tpsl_pending_readback",
                    "native_tpsl": {
                        "triggerOrderType": "TPSL", "ordId": "tp-old-1",
                        "tpTriggerPx": "64500", "tpOrdPx": "-1", "sz": "10",
                    },
                },
            )
        session.commit()
        return convergence.id


def _remove_native_primary_ownership(session_factory):
    from telegram_kol_research.models import PositionProtectionLedger

    with session_factory() as session:
        session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id == "sl-1"
        ).delete()
        session.commit()


def test_exact_backup_allows_tp_without_native_primary_ownership(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "exact-backup-only.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[
            {"price": "1890", "allocation_pct": "50"},
            {"price": "1860", "allocation_pct": "30"},
            {"price": "1825", "allocation_pct": "20"},
        ],
    )
    _remove_native_primary_ownership(session_factory)

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=_ExactBackupOnlyClient(),
        planned_at=NOW,
    )

    assert plan.status == "ready"
    assert [payload["tpTriggerPx"] for payload in plan.payloads] == [
        "1890", "1860", "1825"
    ]
    assert [payload["sz"] for payload in plan.payloads] == ["1.7", "1", "0.7"]


def test_liveness_v2_shadow_plans_take_profit_without_exchange_write(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_ready_trigger_take_profit_convergences,
    )

    session_factory = create_session_factory(tmp_path / "liveness-shadow-tp.db")
    _ready_convergence(session_factory, existing_take_profit=False)
    save_trading_settings(
        session_factory,
        {"position_management_liveness_v2_mode": "shadow"},
    )
    client = _Client()

    completed = execute_ready_trigger_take_profit_convergences(
        session_factory,
        deepcoin_client=client,
        processed_at=NOW,
    )

    assert completed == 0
    assert client.submit_calls == []
    with session_factory() as session:
        assert session.query(PositionProtectionLeg).filter_by(
            role="take_profit"
        ).count() == 3


@pytest.mark.parametrize(
    "settings",
    [
        {"position_management_liveness_v2_mode": "disabled"},
        {
            "position_management_liveness_v2_mode": "live",
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
        },
        {
            "position_management_liveness_v2_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "shadow",
        },
    ],
)
def test_liveness_v2_disabled_or_global_off_never_executes_ready_tp(
    tmp_path, settings
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_ready_trigger_take_profit_convergences,
    )

    session_factory = create_session_factory(tmp_path / "liveness-disabled-tp.db")
    _ready_convergence(session_factory, existing_take_profit=False)
    save_trading_settings(session_factory, settings)
    client = _Client()

    completed = execute_ready_trigger_take_profit_convergences(
        session_factory, deepcoin_client=client, processed_at=NOW,
    )

    assert completed == 0
    assert client.submit_calls == []
    with session_factory() as session:
        assert session.query(PositionProtectionLeg).filter_by(
            role="take_profit"
        ).count() == 0


def test_unknown_targeting_tp_still_blocks_additive_tp(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "exact-backup-unknown-tp.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    _remove_native_primary_ownership(session_factory)
    client = _ExactBackupOnlyClient()
    client.pending.append({
        "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
        "ordId": "unknown-tp", "triggerOrderType": "TPSL",
        "tpTriggerPx": "64000", "tpOrdPx": "-1", "sz": "1",
    })

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code, plan.payloads) == (
        "conflicted", "convergence_unowned_take_profit_present", ()
    )
    assert client.submit_calls == []


@pytest.mark.parametrize(
    "pending_patch",
    (
        {"posId": None},
        {"slOrdPx": "67300"},
    ),
)
def test_exact_backup_requires_exact_market_pending_readback(tmp_path, pending_patch):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "strict-backup-readback.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    _remove_native_primary_ownership(session_factory)
    client = _ExactBackupOnlyClient()
    client.pending[0].update(pending_patch)

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted", "convergence_verified_stop_missing"
    )


def test_exact_backup_requires_matching_local_instrument_and_side(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionBackupStopOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "strict-backup-ledger.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    _remove_native_primary_ownership(session_factory)
    with session_factory() as session:
        backup = session.query(PositionBackupStopOrder).one()
        backup.instrument_id = "ETH-USDT-SWAP"
        backup.side = "long"
        session.commit()

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=_ExactBackupOnlyClient(),
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted", "convergence_verified_stop_missing"
    )


@pytest.mark.parametrize("evidence_problem", ("request_price", "duplicate_order"))
def test_exact_backup_rejects_inconsistent_or_ambiguous_evidence(
    tmp_path, evidence_problem
):
    import json

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionBackupStopOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "strict-backup-evidence.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    _remove_native_primary_ownership(session_factory)
    client = _ExactBackupOnlyClient()
    if evidence_problem == "request_price":
        with session_factory() as session:
            backup = session.query(PositionBackupStopOrder).one()
            request = json.loads(backup.request_json)
            request["slTriggerPx"] = "67400"
            backup.request_json = json.dumps(request)
            session.commit()
    else:
        client.pending.append(dict(client.pending[0]))

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted", "convergence_verified_stop_missing"
    )


def test_plan_accepts_verified_native_primary_without_backup(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False, with_backup=False
    )

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=_Client(), planned_at=NOW
    )

    assert plan.status == "ready"
    assert plan.reason_code is None
    assert len(plan.payloads) == 3
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert (convergence.status, convergence.reason_code) == ("ready", None)


def test_take_profit_convergence_never_races_reserved_close(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        BoundPositionCloseReservation,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "tp-close-wins.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        session.add(
            BoundPositionCloseReservation(
                pos_id="pos-10",
                execution_binding_id=convergence.execution_binding_id,
                status="reserved",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    client = _Client()

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert plan.status == "conflicted"
    assert plan.reason_code == "convergence_close_in_progress"
    assert client.submit_calls == []


def test_plan_refuses_backup_order_without_persisted_exact_close_position(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        PositionBackupStopOrder,
        PositionProtectionLedger,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False, with_backup=False
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id == "sl-1"
        ).delete()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=convergence.execution_binding_id,
            execution_order_leg_id=convergence.execution_order_leg_id, pos_id="pos-10",
            instrument_id="BTC-USDT-SWAP", side="short", trigger_price="67334.4",
            order_id="backup-1", client_order_id="backup-client-1", status="active",
            request_json='{"instId":"BTC-USDT-SWAP","orderType":"market"}',
        ))
        session.commit()

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=_Client(), planned_at=NOW
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted", "convergence_verified_stop_missing"
    )


def test_plan_replaces_only_exact_leg_take_profits(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=_Client(), planned_at=NOW
    )

    assert plan.status == "ready"
    assert plan.cancel_order_ids == ()
    assert plan.payloads == (
        {
            "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-10",
            "posSide": "short", "mrgPosition": "split", "tdMode": "cross",
            "tpTriggerPx": "64500", "tpTriggerPxType": "last", "tpOrdPx": "-1", "sz": "5",
        },
        {
            "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-10",
            "posSide": "short", "mrgPosition": "split", "tdMode": "cross",
            "tpTriggerPx": "63800", "tpTriggerPxType": "last", "tpOrdPx": "-1", "sz": "3",
        },
        {
            "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-10",
            "posSide": "short", "mrgPosition": "split", "tdMode": "cross",
            "tpTriggerPx": "63100", "tpTriggerPxType": "last", "tpOrdPx": "-1", "sz": "2",
        },
    )
    assert all("slTriggerPx" not in payload for payload in plan.payloads)
    assert all(payload["tpOrdPx"] == "-1" for payload in plan.payloads)
    assert [payload["sz"] for payload in plan.payloads] == ["5", "3", "2"]


def test_rescued_fourteen_contract_leg_never_overallocates_take_profits(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    class FourteenContractClient(_Client):
        def __init__(self):
            super().__init__()
            self.pending[0]["sz"] = "14"

        def list_positions(self, *, inst_id=None):
            rows = super().list_positions(inst_id=inst_id)
            rows[0]["pos"] = "14"
            return rows

    session_factory = create_session_factory(tmp_path / "fourteen-contracts.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
    )

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=FourteenContractClient(),
        planned_at=NOW,
    )

    assert plan.status == "ready"
    sizes = [int(payload["sz"]) for payload in plan.payloads]
    assert sizes == [7, 4, 3]
    assert sum(sizes) == 14


def test_plan_accepts_exact_full_position_primary_stop_with_zero_size(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _Client()
    client.pending[0]["sz"] = "0"

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"


def test_plan_requires_native_primary_and_backup_stop_readback(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    class _GenericStopsClient(_Client):
        def __init__(self):
            super().__init__()
            self.pending = [
                {
                    "instId": "BTC-USDT-SWAP", "posId": "pos-10", "ordId": "sl-1",
                    "slTriggerPx": "67200", "sz": "10",
                },
                {
                    "instId": "BTC-USDT-SWAP", "closePosId": "pos-10", "ordId": "backup-1",
                    "triggerPrice": "67334.4", "orderType": "market", "sz": "10",
                },
            ]

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=_GenericStopsClient(),
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code, plan.payloads) == (
        "conflicted", "convergence_verified_stop_missing", ()
    )


def test_plan_allows_unscoped_full_position_native_backup_only_with_complete_unique_snapshot(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _Client()
    client.pending[1] = {
        "instId": "BTC-USDT-SWAP", "ordId": "backup-1", "posSide": "short",
        "triggerOrderType": "TPSL", "slTriggerPx": "67334.4", "slOrdPx": "-1", "sz": "0",
        "cTime": "1000",
    }

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"


def test_plan_accepts_persisted_unscoped_full_position_backup_with_same_side_splits(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    class _AmbiguousBackupClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [
                *super().list_positions(inst_id=inst_id),
                {
                    "instId": "BTC-USDT-SWAP", "posId": "pos-11", "posSide": "short",
                    "pos": "10", "mrgPosition": "split", "mgnMode": "cross", "cTime": "1000",
                },
            ]

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _AmbiguousBackupClient()
    client.pending[1] = {
        "instId": "BTC-USDT-SWAP", "ordId": "backup-1", "posSide": "short",
        "triggerOrderType": "TPSL", "slTriggerPx": "67334.4", "slOrdPx": "-1", "sz": "0",
        "cTime": "1000",
    }

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"


def test_plan_accepts_native_primary_stop_without_position_id_when_scope_is_unique(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    client = _Client()
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP", "ordId": "sl-1", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPx": "67200", "slOrdPx": "-1", "sz": "10",
            "cTime": "1000",
        },
        {
            "instId": "BTC-USDT-SWAP", "posId": "pos-10", "ordId": "backup-1", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPx": "67334.4", "slOrdPx": "-1", "sz": "0",
        },
    ]
    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"


def test_plan_accepts_persisted_unscoped_primary_stop_with_same_side_splits(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    class _SameSideSplitClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [
                *super().list_positions(inst_id=inst_id),
                {
                    "instId": "BTC-USDT-SWAP", "posId": "pos-11", "posSide": "short",
                    "pos": "7", "mrgPosition": "split", "mgnMode": "cross", "cTime": "1000",
                },
            ]

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _SameSideSplitClient()
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP", "ordId": "sl-1", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPx": "67200", "slOrdPx": "-1", "sz": "10",
            "cTime": "1000",
        },
        {
            "instId": "BTC-USDT-SWAP", "posId": "pos-10", "ordId": "backup-1", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPx": "67334.4", "slOrdPx": "-1", "sz": "0",
        },
    ]

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"


def test_plan_allocates_a_single_take_profit_to_the_full_position(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "67300", "allocation_pct": "100"}],
    )

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=_Client(), planned_at=NOW
    )

    assert plan.status == "ready"
    assert plan.payloads[0]["tpTriggerPx"] == "67300"
    assert plan.payloads[0]["sz"] == "10"


def test_plan_submits_only_missing_targets_after_exact_owned_target_is_verified(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    client = _Client()
    with session_factory() as session:
        from telegram_kol_research.models import PositionTakeProfitOrder

        row = session.query(PositionTakeProfitOrder).filter_by(order_id="tp-old-1").one()
        row.size_text = "5"
        session.commit()
    client.pending.append({
        "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short", "ordId": "tp-old-1",
        "triggerOrderType": "TPSL", "tpTriggerPx": "64500", "tpOrdPx": "-1", "tpPrice": "0", "sz": "5",
    })
    client.pending.append({
        "instId": "BTC-USDT-SWAP", "posId": "pos-other", "posSide": "short", "ordId": "other-leg-tp",
        "triggerOrderType": "TPSL", "tpTriggerPx": "64000", "tpOrdPx": "-1", "tpPrice": "0", "sz": "2",
    })

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "ready"
    assert plan.cancel_order_ids == ()
    assert [(payload["tpTriggerPx"], payload["sz"]) for payload in plan.payloads] == [
        ("63800", "3"),
        ("63100", "2"),
    ]


def test_execution_marks_already_verified_take_profit_set_submitted_without_write(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        PositionProtectionLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import record_take_profit_order
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    client = _Client()
    pending_targets = [("tp-old-1", "64500", "5"), ("tp-old-2", "63800", "3"), ("tp-old-3", "63100", "2")]
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        first = session.query(PositionTakeProfitOrder).filter_by(order_id="tp-old-1").one()
        first.size_text = "5"
        for order_id, price, size in pending_targets[1:]:
            record_take_profit_order(
                session,
                venue="deepcoin",
                execution_binding_id=convergence.execution_binding_id,
                execution_order_leg_id=convergence.execution_order_leg_id,
                trigger_take_profit_convergence_id=convergence.id,
                pos_id="pos-10",
                order_id=order_id,
                trigger_price=price,
                size_text=size,
                created_at=NOW,
                evidence={"source": "native_tpsl_pending_readback", "native_tpsl": {"triggerOrderType": "TPSL", "ordId": order_id, "tpTriggerPx": price, "tpOrdPx": "-1", "tpPrice": "0", "sz": size}},
            )
        session.commit()
    client.pending.extend([
        {"instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short", "ordId": order_id,
         "triggerOrderType": "TPSL", "tpTriggerPx": price, "tpOrdPx": "-1", "tpPrice": "0", "sz": size}
        for order_id, price, size in pending_targets
    ])

    result = execute_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, executed_at=NOW
    )

    assert result == {"convergence_id": convergence_id, "status": "submitted", "reason": None}
    assert client.submit_calls == []
    with session_factory() as session:
        logical_legs = session.query(PositionProtectionLeg).filter_by(
            role="take_profit"
        ).all()
        assert len(logical_legs) == 3
        assert {row.status for row in logical_legs} == {"verified"}
        assert {row.exchange_order_id for row in logical_legs} == {
            "tp-old-1",
            "tp-old-2",
            "tp-old-3",
        }


def test_shadow_plan_never_adopts_existing_order_onto_failed_logical_leg(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionProtectionLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_ready_trigger_take_profit_convergences,
    )

    session_factory = create_session_factory(tmp_path / "shadow-failed-logical.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=True,
        desired_take_profits=[
            {"price": "64500", "allocation_pct": "50"},
            {"price": "63800", "allocation_pct": "50"},
        ],
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        existing_order = session.query(PositionTakeProfitOrder).one()
        existing_order.size_text = "5"
        logical_leg = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            role="take_profit",
            leg_index=1,
            planned_trigger_price="64500.0",
            planned_size="10",
        )
        bind_filled_position(session, logical_leg, pos_id="pos-10")
        logical_leg.status = "failed"
        session.commit()
    save_trading_settings(
        session_factory,
        {"position_management_liveness_v2_mode": "shadow"},
    )
    client = _Client()
    client.pending.append(
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-10",
            "posSide": "short",
            "ordId": "tp-old-1",
            "triggerOrderType": "TPSL",
            "tpTriggerPx": "64500",
            "tpOrdPx": "-1",
            "tpPrice": "0",
            "sz": "5",
        }
    )

    completed = execute_ready_trigger_take_profit_convergences(
        session_factory,
        deepcoin_client=client,
        processed_at=NOW,
    )

    assert completed == 0
    assert client.submit_calls == []
    with session_factory() as session:
        logical_leg = session.query(PositionProtectionLeg).one()
        assert logical_leg.status == "failed"
        assert logical_leg.exchange_order_id is None


def test_plan_never_adopts_take_profit_order_from_other_venue(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "other-venue-tp.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=True,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    with session_factory() as session:
        order = session.query(PositionTakeProfitOrder).one()
        order.venue = "other"
        session.commit()
    client = _Client()
    client.pending.append(
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-10",
            "posSide": "short",
            "ordId": "tp-old-1",
            "triggerOrderType": "TPSL",
            "tpTriggerPx": "64500",
            "tpOrdPx": "-1",
            "tpPrice": "0",
            "sz": "10",
        }
    )

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted",
        "convergence_unowned_take_profit_present",
    )


def test_existing_take_profit_order_alias_conflict_blocks_missing_tier_write(
    tmp_path,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "tp-order-alias-conflict.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=True,
        desired_take_profits=[
            {"price": "64500", "allocation_pct": "50"},
            {"price": "63800", "allocation_pct": "50"},
        ],
    )
    with session_factory() as session:
        existing = session.query(PositionTakeProfitOrder).one()
        existing.size_text = "5"
        session.commit()
    client = _Client()
    client.pending.append(
        {
            "ordId": "tp-old-1",
            "orderId": "other-order",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-10",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "tpTriggerPx": "64500",
            "tpOrdPx": "-1",
            "tpPrice": "0",
            "sz": "5",
        }
    )

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert client.submit_calls == []


@pytest.mark.parametrize(
    "conflicting_alias",
    [
        {"pos_id": "other-pos"},
        {"instrument_id": "ETH-USDT-SWAP"},
        {"side": "long"},
    ],
)
def test_live_position_alias_conflict_blocks_take_profit_write(
    tmp_path, conflicting_alias
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class ConflictingPositionClient(_Client):
        def list_positions(self, *, inst_id=None):
            rows = super().list_positions(inst_id=inst_id)
            rows[0].update(conflicting_alias)
            return rows

    session_factory = create_session_factory(tmp_path / "tp-position-alias-conflict.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = ConflictingPositionClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert client.submit_calls == []


def test_equivalent_buy_sell_side_aliases_do_not_block_take_profit_write(
    tmp_path,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class EquivalentSideClient(_Client):
        def list_positions(self, *, inst_id=None):
            rows = super().list_positions(inst_id=inst_id)
            rows[0]["side"] = "sell"
            return rows

        def read_trigger_orders_pending(self, *, inst_id):
            response = super().read_trigger_orders_pending(inst_id=inst_id)
            for row in response["data"]:
                row["side"] = "sell"
            return response

    session_factory = create_session_factory(tmp_path / "tp-equivalent-side.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = EquivalentSideClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "submitted"
    assert [payload["sz"] for payload in client.submit_calls] == ["5", "3", "2"]


@pytest.mark.parametrize("missing_field", ["triggerOrderType", "instId", "posSide"])
def test_incomplete_take_profit_row_blocks_preplan_write(tmp_path, missing_field):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "tp-incomplete-row.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = _Client()
    incomplete = {
        "ordId": "mystery-tp",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "tpTriggerPx": "63800",
        "tpOrdPx": "-1",
        "sz": "3",
    }
    incomplete.pop(missing_field)
    client.pending.append(incomplete)

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert client.submit_calls == []


def test_incomplete_take_profit_row_appearing_between_tiers_blocks_next_write(
    tmp_path,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class InterleavedIncompleteClient(_Client):
        def __init__(self):
            super().__init__()
            self.injected = False

        def read_trigger_orders_pending(self, *, inst_id):
            response = super().read_trigger_orders_pending(inst_id=inst_id)
            if len(self.submit_calls) == 1 and not self.injected:
                self.pending.append(
                    {
                        "ordId": "mystery-next-tier",
                        "tpTriggerPx": "63800",
                        "tpOrdPx": "-1",
                        "sz": "3",
                    }
                )
                self.injected = True
            return response

    session_factory = create_session_factory(tmp_path / "tp-interleaved-incomplete.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = InterleavedIncompleteClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert len(client.submit_calls) == 1


def test_execution_submits_initial_take_profits_without_cancelling_any_order(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder, TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _Client()

    result = execute_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "submitted"
    assert client.cancel_calls == []
    assert [payload["sz"] for payload in client.submit_calls] == ["5", "3", "2"]
    assert all("slTriggerPx" not in payload for payload in client.submit_calls)
    with session_factory() as session:
        new = session.query(PositionTakeProfitOrder).filter(
            PositionTakeProfitOrder.order_id.like("tp-new-%")
        ).order_by(PositionTakeProfitOrder.order_id).all()
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert [row.status for row in new] == ["active", "active", "active"]
    assert convergence.status == "submitted"


def test_execution_blocks_incomplete_pending_snapshot_before_tp_write(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class IncompleteClient(_Client):
        def read_trigger_orders_pending(self, *, inst_id):
            return {
                "code": "0",
                "data": self.list_trigger_orders_pending(inst_id=inst_id),
                "nextCursor": "more",
            }

    session_factory = create_session_factory(tmp_path / "incomplete-pending.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = IncompleteClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert client.submit_calls == []


def test_each_tp_write_rechecks_for_new_unowned_pending_order(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class InterleavedClient(_Client):
        def set_position_sltp(self, payload):
            response = super().set_position_sltp(payload)
            if len(self.submit_calls) == 1:
                self.pending.append(
                    {
                        "ordId": "external-next-tier",
                        "instId": payload["instId"],
                        "posId": payload["posId"],
                        "posSide": payload["posSide"],
                        "triggerOrderType": "TPSL",
                        "tpTriggerPx": "63800",
                        "tpOrdPx": "-1",
                        "tpPrice": "0",
                        "sz": "3",
                    }
                )
            return response

    session_factory = create_session_factory(tmp_path / "interleaved-tp.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False
    )
    client = InterleavedClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert len(client.submit_calls) == 1


def test_execution_binds_logical_take_profit_with_equivalent_decimal_price(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionProtectionLeg,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "decimal-price.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        logical_leg = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            role="take_profit",
            leg_index=1,
            planned_trigger_price="64500.0",
            planned_size="10",
        )
        bind_filled_position(session, logical_leg, pos_id="pos-10")
        logical_leg.status = "protection_recovery_pending"
        session.commit()

    client = _Client()
    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "submitted"
    assert len(client.submit_calls) == 1
    with session_factory() as session:
        logical_leg = session.query(PositionProtectionLeg).filter_by(
            role="take_profit"
        ).one()
        assert logical_leg.status == "verified"
        assert logical_leg.exchange_order_id == "tp-new-1"


def test_plan_blocks_ambiguous_equivalent_decimal_logical_take_profit_legs(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionOrderLeg, TriggerTakeProfitConvergence
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "ambiguous-decimal-price.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        for leg_index, price in ((1, "64500.0"), (2, "64500.00")):
            logical_leg = create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=int(leg.id),
                role="take_profit",
                leg_index=leg_index,
                planned_trigger_price=price,
                planned_size="10",
            )
            bind_filled_position(session, logical_leg, pos_id="pos-10")
        session.commit()
    client = _Client()

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted",
        "convergence_protection_leg_conflict",
    )
    assert client.submit_calls == []


@pytest.mark.parametrize("ownership_mismatch", ["position", "venue"])
def test_plan_blocks_equivalent_decimal_logical_leg_with_wrong_owner(
    tmp_path, ownership_mismatch
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionOrderLeg, TriggerTakeProfitConvergence
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(
        tmp_path / f"wrong-{ownership_mismatch}-price.db"
    )
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        logical_leg = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            role="take_profit",
            leg_index=1,
            planned_trigger_price="64500.0",
            planned_size="10",
        )
        bind_filled_position(
            session,
            logical_leg,
            pos_id="pos-other" if ownership_mismatch == "position" else "pos-10",
        )
        if ownership_mismatch == "venue":
            logical_leg.venue = "other"
        session.commit()
    client = _Client()

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted",
        "convergence_protection_leg_conflict",
    )
    assert client.submit_calls == []


def test_plan_blocks_missing_target_when_logical_leg_owns_another_order(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionOrderLeg, TriggerTakeProfitConvergence
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        bind_verified_exchange_order,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "conflicting-order-owner.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        logical_leg = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            role="take_profit",
            leg_index=1,
            planned_trigger_price="64500.0",
            planned_size="10",
        )
        bind_filled_position(session, logical_leg, pos_id="pos-10")
        bind_verified_exchange_order(
            session,
            logical_leg,
            exchange_order_id="stale-tp-order",
            readback_evidence={"source": "test"},
        )
        session.commit()
    client = _Client()

    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert (plan.status, plan.reason_code) == (
        "conflicted",
        "convergence_protection_leg_conflict",
    )
    assert client.submit_calls == []


def test_execution_freezes_when_native_take_profit_response_has_no_pending_readback(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder, TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _NoReadbackClient(_Client):
        def set_position_sltp(self, payload):
            self.submit_calls.append(dict(payload))
            return {"code": "0", "data": {"ordId": "tp-response-only"}}

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    client = _NoReadbackClient()
    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    retry = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result == {
        "convergence_id": convergence_id,
        "status": "submit_unknown",
        "reason": "convergence_take_profit_submit_unknown",
    }
    with session_factory() as session:
        assert session.query(PositionTakeProfitOrder).count() == 0
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert convergence.status == "submit_unknown"
    assert retry["status"] == "blocked"
    assert len(client.submit_calls) == 1


def test_execution_freezes_unknown_when_post_write_readback_raises(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _ReadbackFailureClient(_Client):
        def __init__(self):
            super().__init__()
            self.pending_reads = 0

        def list_trigger_orders_pending(self, *, inst_id):
            self.pending_reads += 1
            if self.pending_reads > 2:
                raise RuntimeError("simulated post-write readback failure")
            return super().list_trigger_orders_pending(inst_id=inst_id)

    session_factory = create_session_factory(tmp_path / "readback-raises.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    client = _ReadbackFailureClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result == {
        "convergence_id": convergence_id,
        "status": "submit_unknown",
        "reason": "convergence_take_profit_submit_unknown",
    }
    assert len(client.submit_calls) == 1
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert convergence.status == "submit_unknown"


def test_execution_freezes_unknown_when_logical_leg_persistence_fails(
    tmp_path, monkeypatch
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder, TriggerTakeProfitConvergence
    import telegram_kol_research.trigger_take_profit_convergence_executor as executor

    session_factory = create_session_factory(tmp_path / "logical-persist-failure.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
        desired_take_profits=[{"price": "64500", "allocation_pct": "100"}],
    )
    client = _Client()

    def fail_bind(*args, **kwargs):
        raise RuntimeError("simulated logical-leg persistence failure")

    monkeypatch.setattr(executor, "bind_verified_exchange_order", fail_bind)
    result = executor.execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    retry = executor.execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result == {
        "convergence_id": convergence_id,
        "status": "submit_unknown",
        "reason": "convergence_logical_protection_persist_unknown",
    }
    assert retry["status"] == "blocked"
    assert len(client.submit_calls) == 1
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert convergence.status == "submit_unknown"
        assert session.query(PositionTakeProfitOrder).count() == 0


def test_execution_rejects_native_take_profit_with_nonmarket_tp_price(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _LimitPriceClient(_Client):
        def set_position_sltp(self, payload):
            self.submit_calls.append(dict(payload))
            self.pending.append({
                "ordId": "tp-limit-1", "instId": payload["instId"], "posId": payload["posId"],
                "posSide": payload["posSide"], "triggerOrderType": "TPSL",
                "tpTriggerPx": payload["tpTriggerPx"], "tpOrdPx": "-1", "tpPrice": 1,
                "sz": payload["sz"],
            })
            return {"code": "0", "data": {"ordId": "tp-limit-1"}}

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=_LimitPriceClient(),
        executed_at=NOW,
    )

    assert result["status"] == "conflicted"
    assert result["reason"] == "convergence_take_profit_pending_readback"
    with session_factory() as session:
        assert session.query(PositionTakeProfitOrder).count() == 0


def test_execution_verifies_returned_unscoped_native_take_profit_with_same_side_splits(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _AmbiguousUnscopedClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [
                {
                        "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
                        "pos": "10", "avgPx": "65000", "mrgPosition": "split",
                        "mgnMode": "cross", "cTime": "1000",
                },
                {
                        "instId": "BTC-USDT-SWAP", "posId": "pos-11", "posSide": "short",
                        "pos": "10", "avgPx": "65000", "mrgPosition": "split",
                        "mgnMode": "cross", "cTime": "1000",
                },
            ]

        def set_position_sltp(self, payload):
            self.submit_calls.append(dict(payload))
            order_id = f"tp-unscoped-{len(self.submit_calls)}"
            self.pending.append({
                "ordId": order_id, "instId": payload["instId"],
                "posSide": payload["posSide"], "triggerOrderType": "TPSL",
                "tpTriggerPx": payload["tpTriggerPx"], "tpOrdPx": "-1", "sz": payload["sz"],
                "cTime": "1000",
            })
            return {"code": "0", "data": {"ordId": order_id}}

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=_AmbiguousUnscopedClient(),
        executed_at=NOW,
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        assert session.query(PositionTakeProfitOrder).count() == 3


def test_plan_allocates_decimal_position_quantity_at_verified_contract_step(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    class _FractionalPositionClient(_Client):
        def __init__(self):
            super().__init__()
            self.contract_spec_provider = _ContractSpecProvider(
                quantity_step="0.1", min_quantity="0.1"
            )
            self.pending[0]["sz"] = "10.5"

        def list_positions(self, *, inst_id=None):
            return [{
                "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
                "pos": "10.5", "mrgPosition": "split", "mgnMode": "cross",
            }]

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)

    client = _FractionalPositionClient()
    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert plan.status == "ready"
    assert [payload["sz"] for payload in plan.payloads] == ["5.2", "3.1", "2.2"]
    assert client.submit_calls == []


def test_plan_freezes_when_contract_quantity_spec_is_unavailable(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _Client()
    client.contract_spec_provider = None

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert (plan.status, plan.reason_code, plan.payloads) == (
        "conflicted", "convergence_target_contract_spec_unavailable", ()
    )
    assert client.submit_calls == []


def test_plan_freezes_when_a_take_profit_stage_is_below_contract_minimum(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _Client()
    client.contract_spec_provider = _ContractSpecProvider(quantity_step="1", min_quantity="3")

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert (plan.status, plan.reason_code, plan.payloads) == (
        "conflicted", "convergence_target_size_below_minimum", ()
    )
    assert client.submit_calls == []


def test_execution_submits_take_profits_for_market_entry_after_backup_readback(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False, order_kind="market"
    )
    client = _Client()

    result = execute_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "submitted"
    assert [payload["sz"] for payload in client.submit_calls] == ["5", "3", "2"]


def test_unknown_take_profit_submit_is_frozen_without_automatic_retry(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _UnknownClient(_Client):
        def set_position_sltp(self, payload):
            self.submit_calls.append(dict(payload))
            raise TimeoutError("response lost")

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _UnknownClient()

    first = execute_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, executed_at=NOW
    )
    second = execute_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, executed_at=NOW
    )

    assert first["status"] == "submit_unknown"
    assert second["status"] == "blocked"
    assert len(client.submit_calls) == 1


def test_definite_take_profit_submit_rejection_is_not_marked_unknown(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        execute_trigger_take_profit_convergence,
    )

    class _RejectedClient(_Client):
        def set_position_sltp(self, payload):
            self.submit_calls.append(dict(payload))
            raise DeepcoinDefiniteRejection("price below lower limit")

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    client = _RejectedClient()

    result = execute_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result == {
        "convergence_id": convergence_id,
        "status": "conflicted",
        "reason": "convergence_submit_rejected",
    }
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert convergence.status == "conflicted"
        assert convergence.reason_code == "convergence_submit_rejected"


def test_unexplained_partial_position_change_freezes_submitted_convergence(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        session.commit()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[{
                "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
                "pos": "8", "mrgPosition": "split",
            }],
            pending_orders=[{
                "instId": "BTC-USDT-SWAP", "posId": "pos-10", "ordId": "tp-old-1",
                "tpTriggerPx": "64500", "sz": "10",
            }],
            trigger_history=[],
            observed_at=NOW,
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert convergence.status == "conflicted"
    assert convergence.reason_code == "convergence_partial_position_unexplained"


def test_terminal_entry_leg_expires_absent_take_profits_and_completes_convergence(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        binding.status = "closed"
        binding.pos_id = None
        session.commit()

        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        orders = session.query(PositionTakeProfitOrder).all()
    assert convergence.status == "completed"
    assert convergence.reason_code == "convergence_position_terminal"
    assert [(row.order_id, row.status) for row in orders] == [("tp-old-1", "expired")]
    assert all(row.completed_at is not None for row in orders)


def test_terminal_entry_leg_with_active_binding_requires_binding_position_identity(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        binding.pos_id = None
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_rejected_take_profit_convergence_completes_after_position_is_terminal(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "conflicted"
        convergence.reason_code = "convergence_submit_rejected"
        convergence.error_json = '{"type":"DeepcoinDefiniteRejection"}'
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        binding.status = "closed"
        binding.pos_id = None
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert session.query(PositionTakeProfitOrder).count() == 0
        assert convergence.status == "completed"
        assert (
            convergence.reason_code
            == "convergence_submit_rejected_position_terminal"
        )


@pytest.mark.parametrize(
    "error_json",
    [None, '{"type":"TimeoutError"}', "not-json"],
)
def test_rejected_take_profit_requires_definite_exchange_rejection_evidence(
    tmp_path,
    error_json,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory,
        existing_take_profit=False,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "conflicted"
        convergence.reason_code = "convergence_submit_rejected"
        convergence.error_json = error_json
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        binding.status = "closed"
        binding.pos_id = None
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert convergence.status == "conflicted"
        assert convergence.reason_code == "convergence_submit_rejected"


@pytest.mark.parametrize(
    "live_position",
    [
        {"posId": "pos-10", "pos": "-1"},
        {"posId": "pos-10", "size": "-1"},
        {"posId": "pos-10", "sz": "2"},
        {"posId": "pos-10", "pos": "not-a-number"},
        {"posId": "pos-10"},
    ],
)
def test_rejected_take_profit_convergence_keeps_unknown_or_nonzero_position_live(
    tmp_path,
    live_position,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "conflicted"
        convergence.reason_code = "convergence_submit_rejected"
        convergence.error_json = '{"type":"DeepcoinDefiniteRejection"}'
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        binding.status = "closed"
        binding.pos_id = None
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[live_position],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert convergence.status == "conflicted"
        assert convergence.reason_code == "convergence_submit_rejected"


def test_terminal_take_profit_requires_verified_entry_leg_identity(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        leg.attribution_status = "unassigned"
        binding.status = "closed"
        binding.pos_id = None
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_terminal_take_profit_requires_matching_strategy_identity(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg.status = "manually_closed"
        leg.strategy_instance_id = "deepcoin:other:strategy:BTC:short"
        binding.status = "closed"
        binding.pos_id = None
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_terminal_take_profit_does_not_use_deepcoin_snapshot_for_other_venue(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        order = session.query(PositionTakeProfitOrder).one()
        convergence.status = "submitted"
        convergence.venue = "binance"
        binding.status = "closed"
        binding.pos_id = None
        binding.venue = "binance"
        leg.status = "manually_closed"
        leg.venue = "binance"
        order.venue = "binance"
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_deepcoin_history_does_not_terminalize_same_id_order_from_other_venue(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionTakeProfitOrder
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _ready_convergence(session_factory)
    with session_factory() as session:
        order = session.query(PositionTakeProfitOrder).one()
        order.venue = "binance"
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[{"ordId": order.order_id, "state": "cancelled"}],
            observed_at=NOW,
        )
        session.commit()

    with session_factory() as session:
        order = session.query(PositionTakeProfitOrder).one()
        assert order.status == "active"
        assert order.completed_at is None


@pytest.mark.parametrize(
    "pending_identity_key",
    ["ordId", "algoId", "clOrdId", "orderSysID"],
)
def test_terminal_entry_leg_does_not_expire_take_profit_still_pending_on_exchange(
    tmp_path,
    pending_identity_key,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        session.commit()

        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[{pending_identity_key: "tp-old-1"}],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
    assert convergence.status == "submitted"
    assert convergence.reason_code is None
    assert order.status == "active"
    assert order.completed_at is None


def test_terminal_entry_leg_normalizes_local_order_identity_before_pending_probe(
    tmp_path,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        order = session.query(PositionTakeProfitOrder).one()
        order.order_id = " tp-old-1 "
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[{"ordId": "tp-old-1"}],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_terminal_entry_leg_requires_nonblank_take_profit_order_identity(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        order = session.query(PositionTakeProfitOrder).one()
        order.order_id = " "
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "conflicted"
        assert order.status == "active"


def test_terminal_entry_leg_fails_closed_when_pending_snapshot_is_incomplete(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        session.commit()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": False},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
    assert convergence.status == "submitted"
    assert order.status == "active"


def test_terminal_entry_leg_requires_nonblank_position_identity(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        convergence.pos_id = " "
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.pos_id = " "
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        binding.status = "closed"
        binding.pos_id = None
        order = session.query(PositionTakeProfitOrder).one()
        order.pos_id = " "
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


@pytest.mark.parametrize(
    "ambiguous_kind",
    ["position", "whitespace_position", "order"],
)
def test_terminal_entry_leg_fails_closed_for_unidentifiable_exchange_row(
    tmp_path,
    ambiguous_kind,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        reconcile_trigger_take_profit_order_history(
            session,
            positions=(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "posId": (" " if ambiguous_kind == "whitespace_position" else None),
                        "pos": "1",
                    }
                ]
                if ambiguous_kind in {"position", "whitespace_position"}
                else []
            ),
            pending_orders=(
                [{"instId": "BTC-USDT-SWAP", "sz": "1"}]
                if ambiguous_kind == "order"
                else []
            ),
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_terminal_entry_leg_normalizes_position_identity_before_live_probe(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        convergence.pos_id = " pos-10 "
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.pos_id = " pos-10 "
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        binding.status = "closed"
        binding.pos_id = None
        order = session.query(PositionTakeProfitOrder).one()
        order.pos_id = " pos-10 "
        session.flush()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[{"posId": "pos-10", "pos": "1"}],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


@pytest.mark.parametrize(
    "live_position",
    [
        {"posId": "pos-10", "pos": "10"},
        {"pos_id": "pos-10", "size": "1"},
        {"PositionID": "pos-10", "sz": "1"},
        {"positionId": "pos-10", "pos": "1"},
        {"position_id": "pos-10", "pos": "1"},
        {"id": "pos-10", "pos": "1"},
        {"posId": "pos-10", "pos": "0", "size": "1"},
        {"posId": "pos-10", "pos": "0", "positionSize": "1"},
    ],
)
def test_terminal_entry_leg_fails_closed_when_position_id_is_still_live(
    tmp_path,
    live_position,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        session.commit()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[live_position],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
    assert convergence.status == "submitted"
    assert order.status == "active"


def test_terminal_entry_leg_conflicts_on_mismatched_take_profit_owner(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionTakeProfitOrder,
        TriggerTakeProfitConvergence,
    )
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.status = "submitted"
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        order = session.query(PositionTakeProfitOrder).one()
        order.pos_id = "wrong-pos"
        session.commit()
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[],
            pending_orders=[],
            trigger_history=[],
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
    assert convergence.status == "conflicted"
    assert convergence.reason_code == "convergence_take_profit_ownership_mismatch"
    assert order.status == "active"
