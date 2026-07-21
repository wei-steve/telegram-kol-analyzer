from __future__ import annotations

import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_events import ExecutionEventRecord, record_execution_event
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    TriggerProtectionIntent,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)


class _Client:
    def __init__(self):
        self.calls = []
        self.pending = []

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "2", "mgnMode": "cross", "posMode": "split",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def set_position_sltp(self, payload):
        self.calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "rescue-sl-1"}}


def _saved_deferred_intent(session_factory, *, verified=True):
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="entry", posted_at=NOW)
        session.add(raw); session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id, input_kind="text", authoritative_model="mimo",
            authoritative_status="策略", authoritative_payload_json="{}",
            agreement_status="authoritative_only", differences_json="[]",
        )
        lifecycle = StrategyLifecycle(chat_id=1, message_id=1, symbol="BTC", side="short", lifecycle_status="entered", signal_at=NOW)
        session.add_all([decision, lifecycle]); session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:1:BTC:short", kol_id="kol", chat_id=1,
            message_id=1, symbol="BTC", side="short", venue="deepcoin", margin_mode="cross",
            position_mode="split", pos_id="pos-1", status="active", last_exchange_status="positions_verified",
        )
        session.add(binding); session.flush(); lifecycle.execution_binding_id = binding.id
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="trigger_limit", order_id="entry-1",
            pos_id="pos-1", venue="deepcoin", attribution_status=("verified" if verified else "unassigned"),
            attribution_evidence_json='{"policy_version":2}', status="active",
        )
        session.add(leg); session.flush()
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=binding.id, execution_order_leg_id=leg.id,
            request_fingerprint="a" * 64, pre_submit_tpsl_baseline_json="[]",
            correlation_id="correlation-1", parent_trigger_order_id="entry-1", recovery_state="retrying",
        )
        session.add(intent); session.flush()
        record_execution_event(session_factory, ExecutionEventRecord(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            action="create_trigger_entry", order_id="entry-1", pos_id="pos-1", symbol="BTC", side="short",
            request={"slTriggerPx": "65000", "slTriggerPxType": "last", "slOrdPx": "-1"}, created_at=NOW,
        ), session=session)
        session.commit()
        return intent.id


def test_rescue_submits_stop_only_and_persists_exact_order_before_retry(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue
    from telegram_kol_research.strategy_management_executor import execute_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _Client()

    planned = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )
    assert planned.status == "ready"
    result = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "submitted"
    assert client.calls == [{
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "mrgPosition": "split", "tdMode": "cross", "slTriggerPx": "65000",
        "slTriggerPxType": "last", "slOrdPx": "-1",
    }]
    assert not any(key.startswith("tp") for key in client.calls[0])
    retry = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )
    assert retry["status"] == "submitted"
    assert len(client.calls) == 1


def test_rescue_refuses_unverified_legacy_position_without_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory, verified=False)
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_position_not_verified"
    assert client.calls == []


def test_rescue_is_noop_when_exact_position_already_has_ledger_managed_stop(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        leg = session.get(ExecutionOrderLeg, intent.execution_order_leg_id)
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=intent.execution_binding_id,
            execution_order_leg_id=leg.id, strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1", instrument_id="BTC-USDT-SWAP", side="short", order_id="managed-sl",
            purpose="stop_loss", trigger_price="65000", size_text=None, status="verified",
            evidence_source="test", evidence={}, seen_at=NOW,
        )
        session.commit()
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "noop"
    assert result.reason_code == "rescue_managed_stop_already_present"
    assert client.calls == []


def test_rescue_blocks_opaque_take_profit_without_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _Client()
    client.pending = [{"instId": "BTC-USDT-SWAP", "posId": "pos-1", "tpTriggerPx": "62000"}]

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_opaque_take_profit_present"
    assert client.calls == []
