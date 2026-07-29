import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution import resolve_contextual_strategy
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_actions import (
    DeepcoinExecutionActionError,
    cancel_pending_entry_legs,
    cancel_revision_entry_leg,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.position_management_remediation import (
    build_position_management_remediation_plan,
)
from telegram_kol_research.strategy_revision_planner import (
    advance_strategy_revision,
    plan_strategy_revision,
)
from telegram_kol_research.strategy_threads import create_strategy_thread_for_lifecycle
from telegram_kol_research.trade_signals import TradeSignalRecord
from telegram_kol_research.trading_settings import trading_settings_from_payload


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "context_resolution"
    / "dpl_1460_1465.json"
)


def test_redacted_revision_cancel_replay_keeps_one_reply_thread(tmp_path):
    replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_ids = {}
    with session_factory() as session:
        for item in replay["messages"]:
            row = RawMessage(
                chat_id=replay["chat_id"],
                message_id=item["message_id"],
                text=item["text"],
                reply_to_message_id=item.get("reply_to_message_id"),
                archived_target_group=True,
            )
            session.add(row)
            session.flush()
            raw_ids[item["message_id"]] = row.id
        session.commit()

    responses = iter(
        [
            {
                "decision": "revise_thread",
                "target_thread_ids": [10],
                "management_action": "replace_entry",
                "supporting_message_ids": [1460, 1462],
                "opposing_message_ids": [],
                "conflict_types": [],
                "confidence": 0.96,
                "reason": "reply chain explicitly updates the original plan",
                "reanalysis_triggers": [],
                "risk_reducing_fanout_allowed": False,
            },
            {
                "decision": "cancel_thread",
                "target_thread_ids": [10],
                "management_action": "cancel_pending_entry",
                "supporting_message_ids": [1460, 1462, 1465],
                "opposing_message_ids": [],
                "conflict_types": [],
                "confidence": 0.98,
                "reason": "quoted continuation cancels the same plan",
                "reanalysis_triggers": [],
                "risk_reducing_fanout_allowed": False,
            },
        ]
    )

    def decide(message_id):
        return resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_ids[message_id],
            ai_recognition_config=AiRecognitionConfig(
                text_provider=AiProviderConfig(
                    base_url="https://api.deepseek.com",
                    api_key="redacted",
                    model="deepseek",
                )
            ),
            evidence={"text": replay["messages"][1 if message_id == 1462 else 2]["text"]},
            context_window={"messages": replay["messages"]},
            candidates=[{"thread_id": 10, "root_message_id": 1460}],
            first_pass_payload={"strategy_kind": "management"},
            exchange_state={},
            model_caller=lambda **_kwargs: next(responses),
        )

    revision = decide(1462)
    cancellation = decide(1465)
    repeated_revision = decide(1462)

    assert revision.decision == "revise_thread"
    assert cancellation.decision == "cancel_thread"
    assert revision.target_thread_ids == (10,)
    assert cancellation.target_thread_ids == (10,)
    assert repeated_revision == revision
    assert replay["expected"]["must_not_create_independent_thread_for"] == [1462, 1465]
    assert {
        item["after_cancel"]
        for item in replay["expected"]["entry_transitions"]
    } == {"cancelled"}
    assert replay["expected"]["late_fill"]["required_path"] == "exact_position_remediation"


def test_context_resolution_disabled_mode_is_a_no_call_gate():
    calls = []
    settings = trading_settings_from_payload(
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "context_resolution_enabled": False,
            "context_resolution_live_chat_ids": [-1009000000001],
        }
    )

    if settings.context_resolution_enabled_for_chat(-1009000000001):
        calls.append("resolver")

    assert calls == []


def test_replay_executes_revision_cancel_and_late_fill_recovery_path(tmp_path):
    replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session_factory = create_session_factory(tmp_path / "execution-replay.db")
    now = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with session_factory() as session:
        root, revision, cancellation = [
            RawMessage(
                chat_id=replay["chat_id"],
                message_id=item["message_id"],
                text=item["text"],
                reply_to_message_id=item.get("reply_to_message_id"),
            )
            for item in replay["messages"]
        ]
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:replay:1460:BTC:long",
            kol_id=f"group:{replay['chat_id']}",
            chat_id=replay["chat_id"],
            message_id=1460,
            symbol="BTC",
            side="long",
            status="open",
        )
        session.add_all([root, revision, cancellation, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=replay["chat_id"],
            message_id=1460,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=now,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        for index in range(2):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"old-{index}",
                    status="submitted",
                    request_json='{"sz":"1"}',
                )
            )
        session.commit()
        revision_raw_id = revision.id
        lifecycle_id = lifecycle.id
        binding_id = binding.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=revision_raw_id,
        strategy_thread_id=thread.id,
        replacement={"entry": "65100-65300"},
        planned_at=now,
    )

    def cancel_old_leg(**kwargs):
        with session_factory() as session:
            leg = session.get(ExecutionOrderLeg, kwargs["execution_order_leg_id"])
            leg.status = "cancelled"
            leg.terminal_reason = "strategy_revision"
            session.commit()
        return {"status": "confirmed_cancelled"}

    def persist_replacement(**_kwargs):
        with session_factory() as session:
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    strategy_instance_id="deepcoin:replay:1460:BTC:long",
                    leg_index=2,
                    purpose="entry",
                    order_kind="limit",
                    order_id="replacement-1",
                    status="submitted",
                    request_json='{"sz":"2"}',
                )
            )
            session.commit()
        return {"status": "confirmed"}

    revised = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=cancel_old_leg,
        replacement_writer=persist_replacement,
        advanced_at=now,
    )
    assert revised.status == "succeeded"

    class CancelClient:
        def __init__(self):
            self.open_orders = [{"ordId": "replacement-1"}]
            self.order_history = []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_open_orders(self, *, inst_id):
            return list(self.open_orders)

        def cancel_order(self, payload):
            self.open_orders = []
            self.order_history = [
                {"ordId": "replacement-1", "state": "canceled"}
            ]
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return list(self.order_history)

        def list_trigger_order_history(self, *, inst_id):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

    signal = TradeSignalRecord(
        id=1465,
        signal_uid="replay-cancel-1465",
        strategy_instance_id="deepcoin:replay:1460:BTC:long",
        source_type="context_resolution",
        venue="deepcoin",
        kol_id=f"group:{replay['chat_id']}",
        chat_id=replay["chat_id"],
        message_id=1465,
        symbol="BTC",
        side="long",
        action="cancel_pending_entry",
        status="pending",
        payload={},
        attempts=0,
    )
    cancelled = cancel_pending_entry_legs(
        session_factory,
        trade_signal=signal,
        deepcoin_client=CancelClient(),
        executed_at=now,
    )
    assert cancelled["submitted"] is True
    with session_factory() as session:
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
    assert [leg.status for leg in entry_legs] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]
    with pytest.raises(DeepcoinExecutionActionError):
        cancel_pending_entry_legs(
            session_factory,
            trade_signal=signal,
            deepcoin_client=CancelClient(),
            executed_at=now,
        )

    with session_factory() as session:
        late_leg = ExecutionOrderLeg(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:replay:1460:BTC:long",
            leg_index=3,
            purpose="entry",
            order_kind="limit",
            order_id="late-fill-1",
            status="submitted",
            request_json='{"sz":"1"}',
        )
        session.add(late_leg)
        session.commit()
        late_leg_id = late_leg.id

    class LateFillClient:
        def __init__(self):
            self.visible = True

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_open_orders(self, *, inst_id):
            return [{"ordId": "late-fill-1"}] if self.visible else []

        def cancel_order(self, payload):
            self.visible = False
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return [{"ordId": "late-fill-1", "state": "filled"}]

        def list_trade_fills(self, *, inst_id=None):
            return [{"ordId": "late-fill-1", "posId": "redacted-pos-1"}]

        def list_trigger_order_history(self, *, inst_id):
            return []

    late_fill = cancel_revision_entry_leg(
        session_factory,
        strategy_thread_id=thread.id,
        execution_binding_id=binding_id,
        execution_order_leg_id=late_leg_id,
        deepcoin_client=LateFillClient(),
        executed_at=now,
    )
    assert late_fill["status"] == "submit_unknown"
    assert late_fill["reason"] == "revision_order_filled_during_cancel"

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        late_leg = session.get(ExecutionOrderLeg, late_leg_id)
        cancellation_raw = (
            session.query(RawMessage)
            .filter(RawMessage.message_id == 1465)
            .one()
        )
        binding.pos_id = "redacted-pos-1"
        binding.status = "active"
        late_leg.pos_id = "redacted-pos-1"
        late_leg.status = "active"
        late_leg.attribution_status = "verified"
        lifecycle.lifecycle_status = "entered"
        lifecycle.entered_at = now
        candidate = SignalCandidate(
            raw_message_id=cancellation_raw.id,
            symbol="BTC",
            side="long",
            event_type="close_signal",
            target_lifecycle_id=lifecycle_id,
            parse_source="mimo_authoritative",
            confidence=0.98,
            recognition_generation="replay-cancel-generation",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=cancellation_raw.id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id=binding.strategy_instance_id,
                idempotency_key="replay-late-fill".ljust(64, "x"),
                status="failed",
                error_json='{"reason":"target_strategy_binding_not_visible_yet"}',
            )
        )
        session.commit()

    class RemediationClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "redacted-pos-1",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "65200",
                    "cTime": "1000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_order_history(self, *, inst_id):
            return []

        def list_trade_fills(self, *, inst_id):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return []

    remediation = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=RemediationClient(),
        now=now,
    )
    assert remediation.actions, remediation
    matching_late_fill_actions = [
        action
        for action in remediation.actions
        if action.action_kind == "full_exit"
        and action.pos_ids == ("redacted-pos-1",)
    ]
    assert matching_late_fill_actions, remediation.actions
    late_fill_action = matching_late_fill_actions[0]
    assert late_fill_action.action_kind == "full_exit"
    assert late_fill_action.pos_ids == ("redacted-pos-1",)
