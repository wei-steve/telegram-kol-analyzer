"""A-4: the ledger repair may only ever touch the rows on the frozen manifest.

The manifest is a compare-and-set list: each row names the values it must
still hold. These tests cover the three ways that can go wrong -- a row that
drifted, a row that looks the same but is not on the list, and a second run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionProtectionLedger,
    PositionProtectionLeg,
    PositionTakeProfitOrder,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementNotification,
    TelegramSourceMessageEvent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.one_off.management_ledger_repair_2026_09_08 import (
    DELETION_EXIT_RELEASE_REASON,
    SUPERSEDED_NOTIFICATION_ID,
    EVIDENCE_PATH,
    POSITIONS_CLOSED_AT,
    REPAIR_TAG,
    TP1_FILLED_AT,
    apply_management_ledger_repair,
    plan_management_ledger_repair,
)


NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)


def _binding(session, *, binding_id: int, chat_id: int, message_id: int, symbol: str,
             side: str, status: str, last_exchange_status: str | None) -> None:
    session.add(
        ExecutionBinding(
            id=binding_id,
            kol_id=f"group:{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side=side,
            status=status,
            last_exchange_status=last_exchange_status,
        )
    )


def _leg(session, *, leg_id: int, binding_id: int, leg_index: int, pos_id: str,
         status: str) -> None:
    session.add(
        ExecutionOrderLeg(
            id=leg_id,
            execution_binding_id=binding_id,
            leg_index=leg_index,
            purpose="entry",
            order_kind="trigger_limit",
            pos_id=pos_id,
            status=status,
            attribution_status="verified",
        )
    )


def _fixture(tmp_path):
    """The manifest rows this test exercises, plus one look-alike off the list."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _binding(
            session,
            binding_id=337,
            chat_id=-1003048800035,
            message_id=4495,
            symbol="BTC",
            side="long",
            status="active",
            last_exchange_status="position_attribution_evidence_unavailable",
        )
        # Same shape, not on the manifest: it must come out untouched.
        _binding(
            session,
            binding_id=999,
            chat_id=-1003048800035,
            message_id=4499,
            symbol="BTC",
            side="short",
            status="active",
            last_exchange_status="position_attribution_evidence_unavailable",
        )
        _binding(
            session,
            binding_id=342,
            chat_id=-1002409877375,
            message_id=9210,
            symbol="ETH",
            side="long",
            status="active",
            last_exchange_status="position_ownership_verified",
        )
        session.flush()
        _leg(session, leg_id=579, binding_id=337, leg_index=1,
             pos_id="1001125123045253", status="active")
        _leg(session, leg_id=580, binding_id=337, leg_index=2,
             pos_id="1001125126414222", status="active")
        _leg(session, leg_id=588, binding_id=342, leg_index=1,
             pos_id="1001125164628529", status="active")
        _leg(session, leg_id=998, binding_id=999, leg_index=1,
             pos_id="1001125199999999", status="active")
        session.add(
            StrategyLifecycle(
                id=1074,
                chat_id=-1003048800035,
                message_id=4495,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 9, 4, 3, 49, 26),
                filled_tp_index=0,
                execution_binding_id=337,
            )
        )
        session.flush()
        for order_id, tp_id, price, size in (
            ("1001125123049529", 195, "81100", "5"),
            ("1001125123049649", 196, "81800", "3"),
            ("1001125123049805", 197, "82500", "2"),
        ):
            session.add(
                PositionTakeProfitOrder(
                    id=tp_id,
                    execution_binding_id=337,
                    execution_order_leg_id=579,
                    pos_id="1001125123045253",
                    order_id=order_id,
                    trigger_price=price,
                    size_text=size,
                    status="active",
                )
            )
        session.add(
            TriggerTakeProfitConvergence(
                id=231,
                execution_binding_id=342,
                execution_order_leg_id=588,
                desired_take_profits_json=json.dumps(
                    [{"allocation_pct": "100", "price": "2595"}]
                ),
                status="conflicted",
                reason_code="convergence_exact_leg_not_verified",
                pos_id="1001125164628529",
            )
        )
        for ledger_id, leg_id, pos_id, order_id, purpose, status in (
            (652, 579, "1001125123045253", "1001125123045252", "stop_loss", "verified"),
            (653, 579, "1001125123045253", "1001125123048630", "stop_loss", "verified"),
            # TP1's ledger row is already terminal and is deliberately not on
            # the manifest; it must survive untouched.
            (654, 579, "1001125123045253", "1001125123049529", "take_profit",
             "protection_missing"),
            (655, 579, "1001125123045253", "1001125123049649", "take_profit", "verified"),
            (656, 579, "1001125123045253", "1001125123049805", "take_profit", "verified"),
            (657, 580, "1001125126414222", "1001125126414221", "stop_loss", "verified"),
            (658, 580, "1001125126414222", "1001125126416266", "stop_loss", "verified"),
        ):
            session.add(
                PositionProtectionLedger(
                    id=ledger_id,
                    execution_binding_id=337,
                    execution_order_leg_id=leg_id,
                    pos_id=pos_id,
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    order_id=order_id,
                    purpose=purpose,
                    status=status,
                    evidence_source="position_mutation_intent_readback",
                )
            )
        for protection_leg_id, leg_index in ((852, 1), (853, 2), (854, 3)):
            session.add(
                PositionProtectionLeg(
                    id=protection_leg_id,
                    execution_binding_id=337,
                    execution_order_leg_id=580,
                    role="take_profit",
                    leg_index=leg_index,
                    status="protection_recovery_pending",
                    pos_id="1001125126414222",
                )
            )
        # Already moved by ``recover-management-history``: this module only
        # writes the audit row for it.
        session.add(
            StrategyManagementBatch(
                id=158,
                idempotency_fingerprint="f" * 64,
                raw_message_id=14797,
                recognition_decision_id=14794,
                recognition_generation="d" * 32,
                strategy_instance_id="deepcoin:-1003048800035:4495:BTC:long",
                requested_fraction=0.5,
                effective_fraction=0.5,
                target_fingerprint="c" * 64,
                target_lifecycle_id=1074,
                execution_binding_id=337,
                intent="partial_take_profit",
                effective_action="partial_close",
                execution_mode="live",
                status="resolved",
                reason_code="history_no_submission_confirmed",
            )
        )
        session.flush()
        session.add(
            StrategyManagementNotification(
                id=SUPERSEDED_NOTIFICATION_ID,
                management_batch_id=158,
                state="recovery_required",
                status="pending",
                payload_fingerprint="e" * 64,
                payload_json=json.dumps({"batch_id": 158, "reason": "x"}),
            )
        )
        # Already moved by ``archive-unbound-holdings``.
        session.add(
            StrategyLifecycle(
                id=1081,
                chat_id=-1002409877375,
                message_id=9181,
                symbol="ETH",
                side="long",
                lifecycle_status="invalidated",
                exit_reason="context_invalidated",
                signal_at=datetime(2026, 9, 4, 12, 48, 55),
                filled_tp_index=0,
            )
        )
        session.add(
            TelegramSourceMessageEvent(
                id=201,
                chat_id=-1002337721508,
                message_id=10254,
                event_type="message_deleted",
                event_fingerprint="a" * 64,
                occurred_at=datetime(2026, 8, 29, 4, 1, 47),
            )
        )
        session.flush()
        session.add(
            SourceMessageDeletionExit(
                id=201,
                source_event_id=201,
                raw_message_id=13776,
                target_lifecycle_id=1031,
                state="recovery_required",
                attempt_count=2,
                last_reason="frozen_ledger_identity_unverified",
                last_error="frozen_ledger_identity_unverified",
            )
        )
        session.commit()
    return session_factory


#: The subset of the manifest the fixture builds, so the tests can assert on an
#: exact expected action count without standing up all twenty production rows.
FIXTURE_KEYS = {
    ("execution_bindings", 337),
    ("execution_order_legs", 579),
    ("execution_order_legs", 580),
    ("strategy_lifecycles", 1074),
    ("position_take_profit_orders", 195),
    ("position_take_profit_orders", 196),
    ("position_take_profit_orders", 197),
    ("position_protection_ledger", 652),
    ("position_protection_ledger", 653),
    ("position_protection_ledger", 655),
    ("position_protection_ledger", 656),
    ("position_protection_ledger", 657),
    ("position_protection_ledger", 658),
    ("position_protection_legs", 852),
    ("position_protection_legs", 853),
    ("position_protection_legs", 854),
    ("trigger_take_profit_convergences", 231),
    ("source_message_deletion_exits", 201),
    ("strategy_management_notifications", SUPERSEDED_NOTIFICATION_ID),
    ("strategy_management_batches", 158),
    ("strategy_lifecycles", 1081),
}


def _planned_keys(plan):
    return {(action.table, action.row_id) for action in plan.actions}


def test_plan_lists_only_manifest_rows_present_in_the_database(tmp_path):
    plan = plan_management_ledger_repair(_fixture(tmp_path))

    assert _planned_keys(plan) == FIXTURE_KEYS
    # Everything else on the manifest is absent here, and absence is reported
    # rather than silently dropped.
    assert {row["reason"] for row in plan.skipped} == {"row_missing"}


def test_apply_moves_each_row_to_its_manifest_target(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, 337)
        assert binding.status == "closed"
        assert binding.last_exchange_status == "stop_loss_confirmed_by_position_history"
        for leg_id in (579, 580):
            leg = session.get(ExecutionOrderLeg, leg_id)
            assert leg.status == "closed"
            assert leg.terminal_reason == "historical_exchange_position_closed"
        lifecycle = session.get(StrategyLifecycle, 1074)
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "stop_loss"
        assert lifecycle.exited_at == POSITIONS_CLOSED_AT
        # No blended close price is invented for a two-leg exit.
        assert lifecycle.exit_price_actual is None
        tp1 = session.get(PositionTakeProfitOrder, 195)
        assert (tp1.status, tp1.completed_at) == ("filled", TP1_FILLED_AT)
        for tp_id in (196, 197):
            row = session.get(PositionTakeProfitOrder, tp_id)
            assert (row.status, row.completed_at) == ("expired", POSITIONS_CLOSED_AT)
        convergence = session.get(TriggerTakeProfitConvergence, 231)
        assert convergence.status == "waiting_backup_stop"
        assert convergence.reason_code == "convergence_waiting_backup_stop"
        # The live position identity is immutable and must survive the reset.
        assert convergence.pos_id == "1001125164628529"


def test_apply_releases_the_lane_and_records_why_it_was_safe(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        exit_row = session.get(SourceMessageDeletionExit, 201)
        assert exit_row.state == "succeeded"  # this is what unpins the lane
        assert exit_row.last_reason == DELETION_EXIT_RELEASE_REASON
        assert exit_row.last_error is None
        proof = json.loads(exit_row.flat_proof_json)
        assert proof["repair"] == REPAIR_TAG
        assert proof["evidence_path"] == EVIDENCE_PATH
        assert proof["execution_bindings"] == 0
        assert proof["froze_on"] == "frozen_ledger_identity_unverified"
        assert proof["blocking_execution_event_id"] == 3796


def test_a_look_alike_row_off_the_manifest_is_never_touched(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        other = session.get(ExecutionBinding, 999)
        assert other.status == "active"
        assert other.last_exchange_status == "position_attribution_evidence_unavailable"
        assert session.get(ExecutionOrderLeg, 998).status == "active"


def test_a_row_that_drifted_is_skipped_and_reported_not_forced(tmp_path):
    session_factory = _fixture(tmp_path)
    with session_factory() as session:
        session.get(ExecutionBinding, 337).status = "unknown"
        session.commit()

    plan = plan_management_ledger_repair(session_factory)

    assert ("execution_bindings", 337) not in _planned_keys(plan)
    drifted = [
        row
        for row in plan.skipped
        if row["table"] == "execution_bindings" and row["row_id"] == 337
    ]
    assert drifted and drifted[0]["reason"] == "before_state_changed"
    assert drifted[0]["found"]["status"] == "unknown"

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )
    with session_factory() as session:
        assert session.get(ExecutionBinding, 337).status == "unknown"


def test_apply_refuses_when_the_plan_is_not_the_one_that_was_reviewed(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    with pytest.raises(ValueError, match="refusing apply"):
        apply_management_ledger_repair(
            session_factory, expected_action_count=plan.action_count + 1, now=NOW
        )

    with session_factory() as session:
        assert session.get(ExecutionBinding, 337).status == "active"


def test_a_second_run_changes_nothing(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)
    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    replan = plan_management_ledger_repair(session_factory)
    assert replan.action_count == 0

    with session_factory() as session:
        audits_before = session.query(PositionAttributionAudit).count()
    apply_management_ledger_repair(session_factory, expected_action_count=0, now=NOW)
    with session_factory() as session:
        assert session.query(PositionAttributionAudit).count() == audits_before


def test_every_change_leaves_an_audit_row_carrying_the_repair_tag(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    result = apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    assert len(result.audit_ids) == plan.action_count
    with session_factory() as session:
        audits = session.query(PositionAttributionAudit).all()
        assert len(audits) == plan.action_count
        for audit in audits:
            evidence = json.loads(audit.evidence_json)
            assert evidence["repair"] == REPAIR_TAG
            assert evidence["evidence_path"] == EVIDENCE_PATH
            assert evidence["changes"]
            # An audit nobody can trace back to a row is not an audit.
            assert (evidence["table"], evidence["row_id"]) in FIXTURE_KEYS


def test_every_protection_row_of_a_closed_position_ends_up_consistent(tmp_path):
    """The whole position, not just the stop: a half-terminalized position is
    exactly the inconsistency this repair exists to remove."""

    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        for ledger_id in (652, 653, 655, 656, 657, 658):
            assert session.get(PositionProtectionLedger, ledger_id).status == "cancelled"
        # TP1's row was already terminal before this repair and is not on the
        # manifest; leaving it alone is the point.
        assert (
            session.get(PositionProtectionLedger, 654).status == "protection_missing"
        )
        for protection_leg_id in (852, 853, 854):
            leg = session.get(PositionProtectionLeg, protection_leg_id)
            assert leg.status == "cancelled"
            # These were never created on the exchange, and nothing here
            # invents an order id for them.
            assert leg.exchange_order_id is None


def test_notification_94_is_marked_without_inventing_a_status(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        row = session.get(StrategyManagementNotification, SUPERSEDED_NOTIFICATION_ID)
        assert (row.state, row.status) == ("recovery_required", "pending")
        payload = json.loads(row.payload_json)
        assert payload["superseded_by"] == REPAIR_TAG
        assert payload["superseded_batch_status"] == "resolved"
        # The original payload survives intact.
        assert payload["batch_id"] == 158


def test_rows_moved_by_an_existing_command_get_an_audit_but_no_write(tmp_path):
    session_factory = _fixture(tmp_path)
    plan = plan_management_ledger_repair(session_factory)

    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        # Untouched by this module: the CLI command already moved them.
        batch = session.get(StrategyManagementBatch, 158)
        assert (batch.status, batch.reason_code) == (
            "resolved",
            "history_no_submission_confirmed",
        )
        ghost = session.get(StrategyLifecycle, 1081)
        assert ghost.lifecycle_status == "invalidated"
        audited = {
            (json.loads(audit.evidence_json)["table"],
             json.loads(audit.evidence_json)["row_id"])
            for audit in session.query(PositionAttributionAudit).all()
        }
        assert ("strategy_management_batches", 158) in audited
        assert ("strategy_lifecycles", 1081) in audited


def test_no_audit_is_claimed_for_a_command_that_was_not_run(tmp_path):
    session_factory = _fixture(tmp_path)
    with session_factory() as session:
        session.get(StrategyManagementBatch, 158).status = "recovery_required"
        session.commit()

    plan = plan_management_ledger_repair(session_factory)

    assert ("strategy_management_batches", 158) not in _planned_keys(plan)
    pending = [
        row
        for row in plan.skipped
        if row["table"] == "strategy_management_batches" and row["row_id"] == 158
    ]
    assert pending and pending[0]["reason"] == "external_tool_not_applied_yet"


def test_a_terminalized_convergence_carries_a_completion_time(tmp_path):
    """A convergence in a terminal state with no ``completed_at`` reads as one
    that is still running; the online path always stamps one."""

    session_factory = _fixture(tmp_path)
    with session_factory() as session:
        session.add(
            TriggerTakeProfitConvergence(
                id=230,
                execution_binding_id=337,
                execution_order_leg_id=579,
                desired_take_profits_json=json.dumps(
                    [{"allocation_pct": "50", "price": "80700"}]
                ),
                status="conflicted",
                reason_code="convergence_exact_leg_not_verified",
                pos_id="1001125163581280",
            )
        )
        session.commit()

    plan = plan_management_ledger_repair(session_factory)
    apply_management_ledger_repair(
        session_factory, expected_action_count=plan.action_count, now=NOW
    )

    with session_factory() as session:
        terminal = session.get(TriggerTakeProfitConvergence, 230)
        assert terminal.status == "completed"
        assert terminal.reason_code == "convergence_position_terminal"
        assert terminal.completed_at == NOW.replace(tzinfo=None)
        # The one that was only reset stays open: nothing has completed there.
        assert session.get(TriggerTakeProfitConvergence, 231).completed_at is None
