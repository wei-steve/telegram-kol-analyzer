from __future__ import annotations

from datetime import UTC, datetime


NOW = datetime(2026, 7, 22, 10, 15, tzinfo=UTC)


class _Client:
    def __init__(self):
        self.cancel_calls = []
        self.submit_calls = []
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

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT-SWAP", "posId": "pos-10", "posSide": "short",
            "pos": "10", "mrgPosition": "split", "mgnMode": "cross",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def cancel_trigger_order(self, payload):
        self.cancel_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def set_position_sltp(self, payload):
        self.submit_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": f"tp-new-{len(self.submit_calls)}"}}


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
                    '{"closePosId":"pos-10","instId":"BTC-USDT-SWAP",'
                    '"orderType":"market","posSide":"short","triggerPrice":"67334.4"}'
                ),
            ))
        if existing_take_profit:
            record_take_profit_order(
                session, venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg_id, pos_id="pos-10", order_id="tp-old-1",
                trigger_price="64500", size_text="10", created_at=NOW,
                trigger_take_profit_convergence_id=convergence.id,
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


def test_plan_accepts_parent_intent_stop_when_pending_row_omits_position_id(tmp_path):
    import json

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionOrderLeg, PositionProtectionLedger
    from telegram_kol_research.trigger_protection_intents import (
        create_or_get_trigger_protection_intent,
        record_trigger_protection_parent,
        transition_trigger_protection_intent,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory, existing_take_profit=False)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        stop = session.query(PositionProtectionLedger).one()
        intent = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg.id,
            request_fingerprint="a" * 64,
            pre_submit_tpsl_baseline_json="[]",
            correlation_id="test-parent-intent",
        )
        record_trigger_protection_parent(session, intent, parent_trigger_order_id="parent-1")
        transition_trigger_protection_intent(
            session, intent, recovery_state="adopted", adopted_order_id="sl-1"
        )
        stop.evidence_source = "reconciliation_trigger_protection_intent"
        stop.evidence_json = json.dumps({"parent_trigger_order_id": "parent-1"})
        session.commit()

    client = _Client()
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP", "ordId": "sl-1", "slTriggerPx": "67200", "sz": "10",
        },
        {
            "instId": "BTC-USDT-SWAP", "ordId": "backup-1", "triggerPrice": "67334.4",
            "orderType": "market", "sz": "10",
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


def test_initial_take_profit_plan_blocks_when_a_take_profit_is_already_present(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        plan_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id = _ready_convergence(session_factory)
    client = _Client()
    client.pending.append({
        "instId": "BTC-USDT-SWAP", "posId": "pos-10", "ordId": "tp-old-1",
        "tpTriggerPx": "64500", "sz": "10",
    })

    plan = plan_trigger_take_profit_convergence(
        session_factory, convergence_id=convergence_id, deepcoin_client=client, planned_at=NOW
    )

    assert plan.status == "conflicted"
    assert plan.reason_code == "convergence_take_profit_already_present"


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
