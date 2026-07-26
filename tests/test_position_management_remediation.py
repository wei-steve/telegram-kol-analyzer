from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
)
from telegram_kol_research.position_management_remediation import (
    _candidate_strategy_instance_id,
    _project_canonical_remediation_candidate,
    _require_exchange_snapshot_fingerprint,
    _require_batch_matches_confirmed_action,
    _select_executable_action,
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


def test_confirmed_break_even_action_requires_market_managed_batch():
    action = SimpleNamespace(
        lifecycle_id=1,
        strategy_instance_id="strategy-1",
        action_kind="move_stop_to_break_even",
        expected_effect={"fraction": None},
        pos_ids=("pos-1",),
        evidence={
            "execution_binding_id": 2,
            "execution_order_leg_ids": [3],
        },
    )
    batch = SimpleNamespace(
        target_lifecycle_id=1,
        strategy_instance_id="strategy-1",
        execution_binding_id=2,
        intent="move_stop_to_break_even",
        effective_action="move_stop_to_break_even",
        requested_fraction=None,
        legs=(
            SimpleNamespace(
                pos_id="pos-1",
                execution_order_leg_id=3,
            ),
        ),
        target_snapshot={"positions": []},
    )

    with pytest.raises(
        ValueError, match="planned batch does not match confirmed remediation action"
    ):
        _require_batch_matches_confirmed_action(action=action, batch=batch)


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


def _persist_failed_management_step(
    session_factory,
    *,
    lifecycle_id,
    posted_at,
    message_id,
    text,
    event_type,
    management_action,
    sequence,
):
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        raw = RawMessage(
            chat_id=lifecycle.chat_id,
            message_id=message_id,
            posted_at=posted_at,
            text=text,
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol=lifecycle.symbol,
            side=lifecycle.side,
            event_type=event_type,
            target_lifecycle_id=lifecycle.id,
            management_action=management_action,
            recognition_generation=f"repair-generation-{message_id}",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=sequence,
            instruction_kind="management",
            strategy_instance_id=binding.strategy_instance_id,
            idempotency_key=f"{raw.id}:{candidate.id}:{sequence}".ljust(64, "x"),
            status="failed",
            error_json='{"reason":"target_strategy_binding_not_visible_yet"}',
        )
        session.add(item)
        session.commit()
        return raw.id


def test_plan_groups_steps_by_strategy_and_orders_by_source_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    second_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=301,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert len(plan.chains) == 1
    chain = plan.chains[0]
    assert [step.raw_message_id for step in chain.steps] == [
        first_raw_id,
        second_raw_id,
    ]
    assert [step.state for step in chain.steps] == [
        "ready_for_approval",
        "waiting_for_predecessor",
    ]
    assert [action.raw_message_id for action in plan.actions] == [first_raw_id]


def _convert_only_instruction_to_cancel_entry(session_factory):
    with session_factory() as session:
        raw = session.query(RawMessage).one()
        candidate = session.query(SignalCandidate).one()
        raw.text = "BTC策略先取消"
        candidate.event_type = "close_signal"
        candidate.management_action = None
        candidate.management_fraction = None
        session.commit()


def test_cancel_entry_with_exact_live_fill_becomes_full_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    action = plan.actions[0]
    assert action.action_kind == "full_exit"
    assert action.evidence["original_action_kind"] == "cancel_entry"
    assert action.evidence["late_fill_conversion"] is True
    assert action.pos_ids == ("pos-1",)


def test_cancel_entry_without_exact_live_fill_never_becomes_full_exit(tmp_path):
    class MissingPositionClient(_ReadOnlyClient):
        def list_positions(self):
            return []

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=MissingPositionClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert any(
        conflict["reason"] == "late_fill_identity_not_exact"
        for conflict in plan.conflicts
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instId", "ETH-USDT-SWAP"),
        ("posSide", "short"),
    ],
)
def test_cancel_entry_late_fill_requires_exact_instrument_and_side(
    field,
    value,
    tmp_path,
):
    class DriftedIdentityClient(_ReadOnlyClient):
        def list_positions(self):
            rows = super().list_positions()
            rows[0][field] = value
            return rows

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=DriftedIdentityClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert any(
        conflict["reason"] == "late_fill_identity_not_exact"
        for conflict in plan.conflicts
    )


def test_cancel_entry_late_fill_requires_binding_pos_id_match(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        binding.pos_id = "different-pos"
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts[0]["reason"] == "late_fill_identity_not_exact"


def test_cancel_entry_late_fill_rejects_duplicate_live_pos_id(tmp_path):
    class DuplicatePositionClient(_ReadOnlyClient):
        def list_positions(self):
            row = super().list_positions()[0]
            return [row, dict(row)]

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=DuplicatePositionClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts[0]["reason"] == "late_fill_identity_not_exact"


def test_cancel_entry_late_fill_never_includes_unrelated_position(tmp_path):
    class ExtraPositionClient(_ReadOnlyClient):
        def list_positions(self):
            rows = super().list_positions()
            rows.append(
                {
                    **rows[0],
                    "posId": "unrelated-pos",
                    "pos": "3",
                }
            )
            return rows

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=ExtraPositionClient(),
        now=NOW,
    )

    assert plan.actions[0].pos_ids == ("pos-1",)
    assert [
        row["posId"] for row in plan.actions[0].evidence["positions"]
    ] == ["pos-1"]


def test_confirmed_late_fill_full_exit_resolves_original_cancel_step(tmp_path):
    class NoPositionClient(_ReadOnlyClient):
        def list_positions(self):
            return []

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    _convert_only_instruction_to_cancel_entry(session_factory)
    action = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    ).actions[0]
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="k" * 64,
                raw_message_id=raw_id,
                recognition_decision_id=1,
                recognition_generation=f"remediation:{action.fingerprint[:32]}",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                effective_fraction=1.0,
                partial_round_before=0,
                status="succeeded",
                target_fingerprint="l" * 64,
                target_snapshot_json=(
                    '{"remediation_confirmation":{"action_id":"'
                    + action.action_id
                    + '"}}'
                ),
                planned_at=NOW,
            )
        )
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=NoPositionClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.chains[0].steps[0].state == "resolved"
    assert plan.chains[0].steps[0].reason == "confirmed_full_exit"


def test_confirmed_full_exit_terminalizes_later_old_lifecycle_steps(tmp_path):
    class NoPositionClient(_ReadOnlyClient):
        def list_positions(self):
            return []

    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        first_raw = session.get(RawMessage, first_raw_id)
        first_candidate = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == first_raw_id)
            .one()
        )
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        first_raw.text = "BTC多单全部平仓"
        first_candidate.event_type = "close_signal"
        first_candidate.management_action = "full_exit"
        first_candidate.management_fraction = None
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="f" * 64,
                raw_message_id=first_raw_id,
                recognition_decision_id=1,
                recognition_generation="repair-generation",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                effective_fraction=1.0,
                partial_round_before=0,
                status="succeeded",
                target_fingerprint="t" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()
    later_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=302,
        text="BTC空单移动止损到开仓价",
        event_type="position_update",
        management_action="move_stop_to_break_even",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=NoPositionClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert len(plan.chains) == 1
    assert [step.raw_message_id for step in plan.chains[0].steps] == [
        first_raw_id,
        later_raw_id,
    ]
    assert [step.state for step in plan.chains[0].steps] == [
        "resolved",
        "terminally_skipped",
    ]


def test_waiting_step_cannot_be_applied_before_predecessor(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=303,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        },
    )
    client = _ReadOnlyClient()
    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )
    waiting = plan.chains[0].steps[1]
    assert waiting.action.fingerprint == "not-executable"

    with pytest.raises(ValueError, match="not executable chain head"):
        apply_position_management_remediation_action(
            session_factory,
            deepcoin_client=client,
            action_id=waiting.action.action_id,
            expected_fingerprint=waiting.action.fingerprint,
            now=NOW,
        )

    assert client.write_calls == []


def test_reconciling_predecessor_blocks_later_chain_step(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="p" * 64,
                raw_message_id=first_raw_id,
                recognition_decision_id=1,
                recognition_generation="repair-generation",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="reconciling",
                target_fingerprint="q" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()
    _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=304,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert [step.state for step in plan.chains[0].steps] == [
        "waiting_for_reconciliation",
        "waiting_for_predecessor",
    ]


def test_unrelated_historical_conflict_does_not_block_exact_chain_head(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        raw = RawMessage(
            chat_id=999,
            message_id=999,
            posted_at=NOW - timedelta(days=1),
            text="全部平仓",
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="close_signal",
            target_lifecycle_id=999999,
            management_action="full_exit",
            recognition_generation="orphan-conflict",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                idempotency_key="orphan".ljust(64, "x"),
                status="failed",
                error_json='{"reason":"target_missing"}',
            )
        )
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert len(plan.actions) == 1
    assert plan.conflicts
    selected = _select_executable_action(
        plan,
        action_id=plan.actions[0].action_id,
    )
    assert selected == plan.actions[0]


def test_unresolved_batch_conflict_is_owned_by_its_strategy_chain(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="u" * 64,
                raw_message_id=first_raw_id,
                recognition_decision_id=1,
                recognition_generation="repair-generation",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="recovery_required",
                target_fingerprint="v" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()
    _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=305,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.chains[0].conflicts[0]["reason"] == (
        "existing_management_batch_unresolved"
    )
    assert plan.chains[0].conflicts[0]["strategy_instance_id"] == (
        plan.chains[0].strategy_instance_id
    )


def test_early_same_chain_directive_conflict_blocks_later_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        raw = session.get(RawMessage, first_raw_id)
        raw.text = "BTC多单止盈30%，保留50%"
        session.commit()
    later_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=308,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert [step.raw_message_id for step in plan.chains[0].steps] == [
        first_raw_id,
        later_raw_id,
    ]
    assert [step.state for step in plan.chains[0].steps] == [
        "blocked",
        "waiting_for_predecessor",
    ]
    assert plan.chains[0].conflicts[0]["strategy_instance_id"] == (
        plan.chains[0].strategy_instance_id
    )


def test_item_strategy_identity_blocks_chain_when_candidate_lifecycle_drifts(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        candidate.target_lifecycle_id = 999999
        session.commit()
    later_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=309,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert [step.raw_message_id for step in plan.chains[0].steps] == [
        first_raw_id,
        later_raw_id,
    ]
    assert [step.state for step in plan.chains[0].steps] == [
        "blocked",
        "waiting_for_predecessor",
    ]
    assert plan.conflicts[0]["strategy_instance_id"] == (
        plan.chains[0].strategy_instance_id
    )


def test_valid_candidate_and_item_strategy_mismatch_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, first_lifecycle_id = _persist_failed_partial_management(
        session_factory
    )
    with session_factory() as session:
        second_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:201:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=201,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-2",
            status="active",
        )
        session.add(second_binding)
        session.flush()
        second_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=201,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=second_binding.id,
        )
        session.add(second_lifecycle)
        session.flush()
        candidate = session.query(SignalCandidate).one()
        candidate.target_lifecycle_id = second_lifecycle.id
        session.commit()
        item = session.query(MessageInstructionItem).one()
        assert _candidate_strategy_instance_id(
            session=session,
            candidate=candidate,
            item=item,
        ) == "deepcoin:88:200:BTC:long"
    later_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=first_lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=310,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert [step.raw_message_id for step in plan.chains[0].steps] == [
        first_raw_id,
        later_raw_id,
    ]
    assert [step.state for step in plan.chains[0].steps] == [
        "blocked",
        "waiting_for_predecessor",
    ]
    assert plan.conflicts[0]["reason"] == "candidate_item_strategy_mismatch"


def test_predecessor_signature_includes_candidate_without_instruction_item(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    first_plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )
    original = first_plan.actions[0]
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        raw = RawMessage(
            chat_id=88,
            message_id=199,
            posted_at=NOW - timedelta(minutes=1),
            text="BTC多单移动止损到开仓价",
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="position_update",
                target_lifecycle_id=lifecycle.id,
                management_action="move_stop_to_break_even",
                recognition_generation="missing-item-generation",
                parse_source="mimo_authoritative",
                confidence=0.95,
            )
        )
        session.commit()

    second_plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )
    original_waiting = next(
        step.action
        for step in second_plan.chains[0].steps
        if step.action is not None and step.action.action_id == original.action_id
    )

    assert (
        original_waiting.evidence["predecessor_signature"]
        != original.evidence["predecessor_signature"]
    )
    assert original_waiting.fingerprint == "not-executable"


@pytest.mark.parametrize(
    ("batch_status", "expected_state"),
    [
        ("reconciling", "waiting_for_reconciliation"),
        ("recovery_required", "blocked"),
        ("succeeded", "resolved"),
    ],
)
def test_remediation_confirmation_attributes_non_terminal_batch_to_source(
    batch_status,
    expected_state,
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    action = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    ).actions[0]
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint=(batch_status[0] * 64),
                raw_message_id=raw_id,
                recognition_decision_id=1,
                recognition_generation=f"remediation:{action.fingerprint[:32]}",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status=batch_status,
                target_fingerprint=(expected_state[0] * 64),
                target_snapshot_json=(
                    '{"remediation_confirmation":{"action_id":"'
                    + action.action_id
                    + '"}}'
                ),
                planned_at=NOW,
            )
        )
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.chains[0].steps[0].state == expected_state


def test_batch_from_other_recognition_generation_does_not_resolve_candidate(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="g" * 64,
                raw_message_id=raw_id,
                recognition_decision_id=1,
                recognition_generation="different-generation",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="succeeded",
                target_fingerprint="h" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert [action.raw_message_id for action in plan.actions] == [raw_id]


def test_batch_is_not_attributed_when_candidate_sequence_is_ambiguous(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        original = session.query(SignalCandidate).one()
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        duplicate = SignalCandidate(
            raw_message_id=raw_id,
            symbol="BTC",
            side="long",
            event_type="position_update",
            target_lifecycle_id=lifecycle_id,
            management_action="partial_take_profit",
            management_fraction=0.5,
            recognition_generation=original.recognition_generation,
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(duplicate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw_id,
                signal_candidate_id=duplicate.id,
                sequence=1,
                instruction_kind="management",
                strategy_instance_id=binding.strategy_instance_id,
                idempotency_key="duplicate-sequence".ljust(64, "x"),
                status="failed",
                error_json='{"reason":"target_not_visible"}',
            )
        )
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="i" * 64,
                raw_message_id=raw_id,
                recognition_decision_id=1,
                recognition_generation=original.recognition_generation,
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="succeeded",
                target_fingerprint="j" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    assert plan.actions == ()
    assert all(step.state == "blocked" for step in plan.chains[0].steps)
    assert {
        conflict["reason"] for conflict in plan.chains[0].conflicts
    } == {"management_batch_candidate_ambiguous"}


def test_production_shape_replays_break_even_exit_then_old_followup_in_order(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    with session_factory() as session:
        first_raw = session.get(RawMessage, first_raw_id)
        first_candidate = session.query(SignalCandidate).one()
        first_raw.text = "BTC多单移动止损到开仓价"
        first_candidate.management_action = "move_stop_to_break_even"
        first_candidate.management_fraction = None
        session.commit()
    second_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=1),
        message_id=306,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        sequence=0,
    )
    third_raw_id = _persist_failed_management_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW + timedelta(minutes=2),
        message_id=307,
        text="BTC多单继续移动止损到开仓价",
        event_type="position_update",
        management_action="move_stop_to_break_even",
        sequence=0,
    )

    plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    )

    chain = plan.chains[0]
    assert [step.raw_message_id for step in chain.steps] == [
        first_raw_id,
        second_raw_id,
        third_raw_id,
    ]
    assert [step.action_kind for step in chain.steps] == [
        "move_stop_to_break_even",
        "full_exit",
        "move_stop_to_break_even",
    ]
    assert [step.state for step in chain.steps] == [
        "ready_for_approval",
        "waiting_for_predecessor",
        "waiting_for_predecessor",
    ]
    assert [action.raw_message_id for action in plan.actions] == [first_raw_id]


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


def test_market_price_noise_does_not_invalidate_action_fingerprint(tmp_path):
    class MarketNoiseClient(_ReadOnlyClient):
        def __init__(self):
            super().__init__()
            self.last_price = "64490"
            self.unrealized = "1.25"

        def list_positions(self):
            rows = super().list_positions()
            rows[0]["lastPx"] = self.last_price
            rows[0]["unrealizedProfit"] = self.unrealized
            rows[0]["useMargin"] = "5.1"
            return rows

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = MarketNoiseClient()
    first = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )
    client.last_price = "64520"
    client.unrealized = "3.75"
    second = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )

    assert first.actions[0].fingerprint == second.actions[0].fingerprint
    assert first.chains[0].fingerprint == second.chains[0].fingerprint
    assert first.snapshot_fingerprint == second.snapshot_fingerprint


def test_position_protection_change_invalidates_action_fingerprint(tmp_path):
    class ProtectionClient(_ReadOnlyClient):
        def __init__(self):
            super().__init__()
            self.stop_loss = "63000"

        def list_positions(self):
            rows = super().list_positions()
            rows[0]["slTriggerPx"] = self.stop_loss
            return rows

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_failed_partial_management(session_factory)
    client = ProtectionClient()
    first = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )
    client.stop_loss = "63500"
    second = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=client,
        now=NOW,
    )

    assert first.actions[0].fingerprint != second.actions[0].fingerprint


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


def test_canonical_source_with_shadow_projects_distinct_remediation_candidate(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _persist_failed_partial_management(session_factory)
    action = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        now=NOW,
    ).actions[0]
    with session_factory() as session:
        source = session.query(SignalCandidate).one()
        source_id = source.id
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="s" * 64,
                raw_message_id=raw_id,
                recognition_decision_id=1,
                recognition_generation=source.recognition_generation,
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="shadow",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="blocked",
                reason_code="management_shadow_plan_only",
                target_fingerprint="t" * 64,
                target_snapshot_json="{}",
                planned_at=NOW,
            )
        )
        session.commit()

    projected_id = _project_canonical_remediation_candidate(
        session_factory,
        action=action,
    )
    retried_projected_id = _project_canonical_remediation_candidate(
        session_factory,
        action=action,
    )

    with session_factory() as session:
        source = session.get(SignalCandidate, source_id)
        projected = session.get(SignalCandidate, projected_id)
        assert projected_id != source_id
        assert retried_projected_id == projected_id
        assert (
            session.query(SignalCandidate)
            .filter(SignalCandidate.review_status == "approved_remediation")
            .count()
            == 1
        )
        assert source.review_status == "pending"
        assert projected.review_status == "approved_remediation"
        assert projected.recognition_generation == (
            f"remediation:{action.fingerprint[:32]}"
        )
        assert projected.management_action == action.action_kind
        assert projected.management_fraction == action.expected_effect["fraction"]
