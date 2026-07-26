from __future__ import annotations

from datetime import UTC, datetime


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
            "pos": "10", "mrgPosition": "split", "mgnMode": "cross", "cTime": "1000",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

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
            order_kind=order_kind, venue="deepcoin", pos_id="pos-10", status="active",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_status = "verified"
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


def test_plan_waits_until_exact_backup_stop_is_verified(tmp_path):
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

    assert (plan.status, plan.reason_code, plan.payloads) == (
        "waiting_backup_stop", "convergence_waiting_backup_stop", ()
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert (convergence.status, convergence.reason_code) == (
        "waiting_backup_stop", "convergence_waiting_backup_stop"
    )


def test_plan_refuses_backup_order_without_persisted_exact_close_position(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionBackupStopOrder, TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(
        session_factory, existing_take_profit=False, with_backup=False
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
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

    assert (plan.status, plan.reason_code) == ("waiting_backup_stop", "convergence_waiting_backup_stop")


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
    from telegram_kol_research.models import PositionTakeProfitOrder, TriggerTakeProfitConvergence
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
        "status": "conflicted",
        "reason": "convergence_take_profit_pending_readback",
    }
    with session_factory() as session:
        assert session.query(PositionTakeProfitOrder).count() == 0
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
    assert convergence.status == "conflicted"
    assert retry["status"] == "blocked"
    assert len(client.submit_calls) == 1


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
                    "pos": "10", "mrgPosition": "split", "mgnMode": "cross", "cTime": "1000",
                },
                {
                    "instId": "BTC-USDT-SWAP", "posId": "pos-11", "posSide": "short",
                    "pos": "10", "mrgPosition": "split", "mgnMode": "cross", "cTime": "1000",
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
