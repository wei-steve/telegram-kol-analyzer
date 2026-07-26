from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.position_management_remediation import (
    _require_exchange_snapshot_fingerprint,
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


class _ReadOnlyClient:
    def __init__(self):
        self.size = "10"
        self.pending = []
        self.pending_error = None
        self.write_calls = []

    def list_positions(self):
        return [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "pos": self.size,
                "avgPx": "64000",
                "cTime": "1000",
            }
        ]

    def list_open_orders(self):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        if self.pending_error is not None:
            raise self.pending_error
        return list(self.pending)

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return []

    def place_order(self, payload):
        self.write_calls.append(("place_order", dict(payload)))
        raise AssertionError("test must reject before exchange write")


def _persist_failed_partial_management(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=300,
            posted_at=NOW,
            text="BTC多单止盈一部分",
        )
        session.add(raw)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:200:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=200,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=200,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id="entry-1",
                pos_id="pos-1",
                venue="deepcoin",
                status="active",
                attribution_status="verified",
            )
        )
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="position_update",
            target_lifecycle_id=lifecycle.id,
            management_action="partial_take_profit",
            management_fraction=0.5,
            recognition_generation="repair-generation",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            strategy_instance_id=binding.strategy_instance_id,
            idempotency_key="r" * 64,
            status="failed",
            error_json='{"reason":"target_strategy_binding_not_visible_yet"}',
        )
        session.add(item)
        session.commit()
        return raw.id, lifecycle.id


def test_build_remediation_plan_is_read_only_and_fingerprinted(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )

    assert client.write_calls == []
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.raw_message_id == raw_id
    assert action.lifecycle_id == lifecycle_id
    assert action.action_kind == "partial_take_profit"
    assert action.pos_ids == ("pos-1",)
    assert action.expected_effect["fraction"] == 0.5
    assert len(action.fingerprint) == 64
    assert len(plan.snapshot_fingerprint) == 64
    assert plan.conflicts == ()


def test_snapshot_change_invalidates_action_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    first = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )

    client.size = "9"
    second = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert first.actions[0].fingerprint != second.actions[0].fingerprint
    assert first.snapshot_fingerprint != second.snapshot_fingerprint


def test_tpsl_change_invalidates_action_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    first = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "ordId": "sl-new",
            "slTriggerPx": "63000",
        }
    ]

    second = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert first.actions[0].fingerprint != second.actions[0].fingerprint


def test_incomplete_exchange_snapshot_produces_conflict_and_no_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    client.pending_error = RuntimeError("pending TPSL unavailable")

    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert plan.actions == ()
    assert plan.conflicts[0]["reason"] == "exchange_snapshot_incomplete"


def test_paginated_pending_tpsl_snapshot_produces_conflict(tmp_path):
    class PaginatedClient(_ReadOnlyClient):
        def read_trigger_orders_pending(self, *, inst_id):
            return {"data": [], "nextCursor": "next"}

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=PaginatedClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts[0]["reason"] == "exchange_snapshot_incomplete"
    assert plan.conflicts[0]["incomplete_pending_tpsl"][0]["complete"] is False


def test_final_snapshot_gate_rejects_tpsl_change(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "ordId": "sl-late",
            "slTriggerPx": "63000",
        }
    ]

    with pytest.raises(ValueError, match="snapshot changed"):
        _require_exchange_snapshot_fingerprint(
            deepcoin_client=client,
            action=plan.actions[0],
        )


def test_apply_rejects_stale_fingerprint_before_exchange_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        },
    )
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )
    client.size = "9"

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        apply_position_management_remediation_action(
            session_factory,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.actions[0].fingerprint,
            now=NOW,
        )

    assert client.write_calls == []


def test_apply_respects_global_live_management_gate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = _ReadOnlyClient()
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW
    )

    with pytest.raises(ValueError, match="execution is disabled"):
        apply_position_management_remediation_action(
            session_factory,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.actions[0].fingerprint,
            now=NOW,
        )

    assert client.write_calls == []
