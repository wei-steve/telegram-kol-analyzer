from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    reconcile_deepcoin_execution_bindings,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    create_management_batch,
    load_management_batch,
    transition_batch,
    transition_leg,
)
from telegram_kol_research.strategy_management_reconciliation import (
    reconcile_strategy_management_batches,
)


NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def _persist_batch(
    session_factory,
    *,
    action="partial_close",
    sizes=("1",),
    preflight=("2",),
    initial_status="submitted",
    symbol="BTC",
):
    strategy_instance_id = f"deepcoin:100:10:{symbol}:short"
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=20, text="tp", posted_at=NOW)
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=10,
            symbol=symbol,
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        session.add_all([decision, lifecycle])
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id=strategy_instance_id,
            kol_id="alice",
            chat_id=100,
            message_id=10,
            symbol=symbol,
            side="short",
            venue="deepcoin",
            pos_id=",".join(f"pos-{i + 1}" for i in range(len(sizes))),
            status="active",
            last_exchange_status="positions_verified",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entries = []
        for index in range(len(sizes)):
            entry = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="market",
                order_id=f"entry-{index + 1}",
                pos_id=f"pos-{index + 1}",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json='{"policy_version":2}',
                status="active",
            )
            session.add(entry)
            entries.append(entry)
        session.commit()
        ids = raw.id, decision.id, lifecycle.id, binding.id
        entry_ids = [entry.id for entry in entries]

    batch = create_management_batch(
        session_factory,
        idempotency_fingerprint=(action[0] * 64),
        raw_message_id=ids[0],
        recognition_decision_id=ids[1],
        recognition_generation="generation-1",
        target_lifecycle_id=ids[2],
        strategy_instance_id=strategy_instance_id,
        execution_binding_id=ids[3],
        intent="partial_take_profit" if action == "partial_close" else "full_take_profit",
        effective_action=action,
        requested_fraction=0.5 if action == "partial_close" else 1.0,
        effective_fraction=0.5 if action == "partial_close" else 1.0,
        partial_round_before=0,
        target_fingerprint="b" * 64,
        target_snapshot={"identity": {"execution_binding_id": ids[3]}},
        legs=[
            ManagementLegCreate(
                execution_order_leg_id=entry_ids[index],
                pos_id=f"pos-{index + 1}",
                leg_index=index,
                preflight_size=preflight[index],
                planned_close_size=size,
                client_order_id=f"TMCLIENT{index + 1}",
                exchange_order_id=(
                    f"close-{index + 1}" if initial_status == "submitted" else None
                ),
                status=initial_status,
            )
            for index, size in enumerate(sizes)
        ],
        planned_at=NOW,
        status="reconciling",
    )
    return batch


class _Client:
    def __init__(self, *, positions, orders=None):
        self.positions = list(positions)
        self.orders = list(
            orders
            if orders is not None
            else [
                {"ordId": "close-1", "clOrdId": "TMCLIENT1"},
                {"ordId": "close-2", "clOrdId": "TMCLIENT2"},
            ]
        )
        self.calls = {"positions": 0, "open": 0, "history": 0, "fills": 0}

    def list_positions(self):
        self.calls["positions"] += 1
        return list(self.positions)

    def list_open_orders(self):
        self.calls["open"] += 1
        return list(self.orders)

    def list_order_history(self, *, inst_id):
        self.calls["history"] += 1
        return list(self.orders)

    def list_trade_fills(self, *, inst_id):
        self.calls["fills"] += 1
        return []


def _position(pos_id, size):
    return {"posId": pos_id, "instId": "BTC-USDT-SWAP", "pos": str(size), "posSide": "short"}


def _reconcile(session_factory, client):
    return reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=NOW
    )


def _reconcile_management(session_factory, *, positions, orders=None):
    return reconcile_strategy_management_batches(
        session_factory,
        snapshot=SimpleNamespace(
            positions=list(positions),
            open_orders=list(
                orders
                if orders is not None
                else [{"ordId": "close-1", "clOrdId": "TMCLIENT1"}]
            ),
            order_history=[],
            trade_fills=[],
            errors={},
        ),
        reconciled_at=NOW,
    )


def test_submitted_order_with_unchanged_position_stays_pending_on_one_snapshot(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)
    client = _Client(
        positions=[_position("pos-1", "2")],
        orders=[{"ordId": "close-1", "clOrdId": "TMCLIENT1"}],
    )

    _reconcile(sf, client)

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "reconciling"
    assert stored.legs[0].status == "submitted"
    assert stored.reconciled_at is None
    assert client.calls == {"positions": 1, "open": 1, "history": 1, "fills": 1}


def test_submission_phase_does_not_stamp_exchange_reconciliation_time(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"reconciling"},
        new_status="executing",
        transitioned_at=NOW,
    )
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"executing"},
        new_status="reconciling",
        transitioned_at=NOW,
    )

    assert load_management_batch(sf, batch.id).reconciled_at is None


def test_planned_partial_fully_reflected_confirms_and_advances_once(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile_management(sf, positions=[_position("pos-1", "1")])
    first = load_management_batch(sf, batch.id)
    _reconcile(sf, _Client(positions=[_position("pos-1", "1")]))
    repeated = load_management_batch(sf, batch.id)

    assert first.status == repeated.status == "succeeded"
    assert first.legs[0].status == repeated.legs[0].status == "confirmed"
    assert first.reconciled_at == repeated.reconciled_at == NOW
    assert first.completed_at == repeated.completed_at == NOW
    with sf() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.management_signal_message_id == 20
        assert lifecycle.management_action == "partial_close_confirmed"
        assert lifecycle.management_note == "Deepcoin exchange confirmed every planned close leg."


def test_partially_filled_leg_requires_recovery_and_freezes_round(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile(sf, _Client(positions=[_position("pos-1", "1.5")]))

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.legs[0].status == "partial"
    assert stored.reconciled_at is None


def test_unknown_submission_is_resolved_by_deterministic_client_id(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, initial_status="submit_unknown")

    _reconcile(
        sf,
        _Client(
            positions=[_position("pos-1", "2")],
            orders=[{"ordId": "recovered-1", "clOrdId": "TMCLIENT1"}],
        ),
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "reconciling"
    assert stored.legs[0].status == "submitted"
    assert stored.legs[0].exchange_order_id == "recovered-1"


def test_unknown_without_order_evidence_never_becomes_retryable(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, initial_status="submit_unknown")

    _reconcile(sf, _Client(positions=[_position("pos-1", "2")], orders=[]))

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.reason_code == "management_close_submission_unresolved"
    assert stored.legs[0].status == "submit_unknown"


def test_submitted_leg_conflicting_order_and_client_rows_freezes(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile(
        sf,
        _Client(
            positions=[_position("pos-1", "1")],
            orders=[
                {"ordId": "close-1", "clOrdId": "OTHER"},
                {"ordId": "other-order", "clOrdId": "TMCLIENT1"},
            ],
        ),
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.legs[0].status == "inconsistent"
    assert stored.legs[0].last_error == {
        "reason": "management_close_order_identity_conflict"
    }


def test_submitted_position_delta_without_exact_order_evidence_does_not_succeed(
    tmp_path,
):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile(sf, _Client(positions=[_position("pos-1", "1")], orders=[]))

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "reconciling"
    assert stored.legs[0].status == "submitted"
    assert stored.legs[0].last_error == {
        "reason": "management_close_order_not_found"
    }


def test_durable_order_and_client_ids_on_disconnected_rows_are_ambiguous(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile(
        sf,
        _Client(
            positions=[_position("pos-1", "1")],
            orders=[{"ordId": "close-1"}, {"clOrdId": "TMCLIENT1"}],
        ),
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.legs[0].status == "inconsistent"
    assert stored.legs[0].last_error == {
        "reason": "management_close_order_identity_ambiguous"
    }


def test_partially_confirmed_multi_position_partial_freezes_strategy(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, sizes=("1", "1"), preflight=("2", "2"))

    _reconcile(
        sf,
        _Client(positions=[_position("pos-1", "1"), _position("pos-2", "2")]),
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert [leg.status for leg in stored.legs] == ["confirmed", "submitted"]
    assert stored.reconciled_at is None


def test_planned_deferred_range_entry_leg_allows_partial_then_break_even_handoff(
    tmp_path,
):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(
        sf,
        action="partial_then_break_even",
        sizes=("1",),
        preflight=("2",),
    )
    with sf() as session:
        deferred = ExecutionOrderLeg(
            execution_binding_id=batch.execution_binding_id,
            strategy_instance_id=batch.strategy_instance_id,
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="entry-pending",
            pos_id=None,
            venue="deepcoin",
            attribution_status="unassigned",
            status="pending",
        )
        session.add(deferred)
        session.flush()
        row = session.get(StrategyManagementBatch, batch.id)
        row.target_snapshot_json = json.dumps(
            {
                "identity": {
                    "execution_binding_id": batch.execution_binding_id,
                    "deferred_entry_leg_ids": [deferred.id],
                }
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        session.commit()

    _reconcile_management(sf, positions=[_position("pos-1", "1")])

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "protection_ready"
    assert stored.reason_code == "management_close_confirmed_protection_ready"
    assert stored.legs[0].status == "confirmed"


def test_definite_failure_is_preserved_when_other_leg_confirms(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, sizes=("1", "1"), preflight=("2", "2"))
    assert transition_leg(
        sf, batch.legs[0].id, expected_statuses={"submitted"}, new_status="failed"
    )
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"reconciling"},
        new_status="partial_failed",
    )

    _reconcile(
        sf,
        _Client(positions=[_position("pos-1", "2"), _position("pos-2", "1")]),
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "partial_failed"
    assert [leg.status for leg in stored.legs] == ["failed", "confirmed"]


def test_full_close_terminalizes_only_after_every_exact_position_disappears(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(
        sf, action="full_close", sizes=("2", "2"), preflight=("2", "2")
    )

    _reconcile(sf, _Client(positions=[_position("pos-2", "2")]))
    interim = load_management_batch(sf, batch.id)
    with sf() as session:
        assert session.get(ExecutionBinding, batch.execution_binding_id).status != "closed"
        assert session.get(StrategyLifecycle, batch.target_lifecycle_id).lifecycle_status == "entered"

    _reconcile(sf, _Client(positions=[]))
    final = load_management_batch(sf, batch.id)
    with sf() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        entries = session.query(ExecutionOrderLeg).filter_by(purpose="entry").all()

    assert interim.status == "reconciling"
    assert final.status == "succeeded"
    assert [leg.status for leg in final.legs] == ["confirmed", "confirmed"]
    assert binding.status == "closed"
    assert binding.pos_id is None
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exited_at.replace(tzinfo=UTC) == NOW
    assert all(entry.status == "closed" for entry in entries)
    assert all(entry.terminal_reason == "management_full_close_confirmed" for entry in entries)


def test_manual_close_during_full_close_reconciliation_preserves_audit(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, action="full_close", sizes=("2",), preflight=("2",))
    with sf() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        entry = session.get(ExecutionOrderLeg, batch.legs[0].execution_order_leg_id)
        binding.status = "closed"
        binding.last_exchange_status = "manual_closed_by_user"
        lifecycle.lifecycle_status = "exited"
        lifecycle.exit_reason = "manual"
        lifecycle.exited_at = NOW
        entry.status = "manually_closed"
        entry.terminal_reason = "manual_position_missing"
        session.commit()

    _reconcile(sf, _Client(positions=[]))

    stored = load_management_batch(sf, batch.id)
    with sf() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        entry = session.get(ExecutionOrderLeg, batch.legs[0].execution_order_leg_id)
    assert stored.status == "recovery_required"
    assert stored.reason_code == "management_reconciliation_identity_mismatch"
    assert binding.status == "closed"
    assert binding.last_exchange_status == "manual_closed_by_user"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"
    assert entry.status == "manually_closed"
    assert entry.terminal_reason == "manual_position_missing"


@pytest.mark.parametrize(
    ("action", "planned", "positions", "extra_strategy"),
    [
        ("full_close", "2", [], "exact"),
        ("partial_close", "1", [_position("pos-1", "1")], "exact"),
        ("full_close", "2", [], None),
        (
            "partial_close",
            "1",
            [_position("pos-1", "1")],
            "deepcoin:other:strategy",
        ),
    ],
)
def test_entry_leg_added_after_planning_freezes_complete_identity_set(
    action, planned, positions, extra_strategy, tmp_path
):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(
        sf, action=action, sizes=(planned,), preflight=("2",)
    )
    with sf() as session:
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=batch.execution_binding_id,
                strategy_instance_id=(
                    batch.strategy_instance_id
                    if extra_strategy == "exact"
                    else extra_strategy
                ),
                leg_index=99,
                purpose="entry",
                order_kind="trigger_limit",
                venue="deepcoin",
                attribution_status="unassigned",
                status="pending",
            )
        )
        session.commit()

    _reconcile_management(sf, positions=positions)

    stored = load_management_batch(sf, batch.id)
    with sf() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        entries = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=batch.execution_binding_id, purpose="entry")
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
    assert stored.status == "recovery_required"
    assert stored.reason_code == "management_reconciliation_identity_mismatch"
    assert binding.status == "active"
    assert lifecycle.lifecycle_status == "entered"
    assert len(entries) == 2
    assert entries[0].status == "active"
    assert entries[0].terminal_reason is None
    assert entries[1].status == "pending"
    assert entries[1].attribution_status == "unassigned"
    assert entries[1].pos_id is None


@pytest.mark.parametrize(
    "symbol",
    ["BTC", "BTCUSDT", "BTC_USDT", "BTC-USDT", "BTC-USDT-SWAP"],
)
def test_reconciliation_uses_canonical_deepcoin_instrument(symbol, tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, symbol=symbol)

    _reconcile_management(sf, positions=[_position("pos-1", "1")])

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "succeeded"
    assert stored.legs[0].status == "confirmed"


@pytest.mark.parametrize("invalid_size", ["NaN", "Infinity", "-Infinity", None])
def test_invalid_current_size_freezes_only_the_leg(invalid_size, tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)
    position = _position("pos-1", invalid_size)
    if invalid_size is None:
        position.pop("pos")

    _reconcile(sf, _Client(positions=[position]))

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.legs[0].status == "inconsistent"
    assert stored.legs[0].last_error == {"reason": "management_position_size_invalid"}


def test_overclose_or_position_growth_is_inconsistent_and_never_succeeds(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)

    _reconcile(sf, _Client(positions=[]))

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.legs[0].status == "inconsistent"


def test_composite_protection_unknown_is_never_reprocessed_as_close_phase(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf, action="partial_then_break_even")
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"reconciling"},
        new_status="recovery_required",
        reason_code="protection_recovery_required",
    )
    assert transition_leg(
        sf,
        batch.legs[0].id,
        expected_statuses={"submitted"},
        new_status="recovery_required",
        last_error={"stage": "replace_protection_outcome_unknown"},
    )

    _reconcile_management(sf, positions=[_position("pos-1", "1")])

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "recovery_required"
    assert stored.reason_code == "protection_recovery_required"
    assert [leg.status for leg in stored.legs] == ["recovery_required"]
    assert stored.legs[0].last_error == {
        "stage": "replace_protection_outcome_unknown"
    }


def test_close_recovery_required_is_permanently_paused_from_auto_reconcile(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(sf)
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"reconciling"},
        new_status="recovery_required",
        reason_code="operator_review_required",
    )

    result = _reconcile_management(sf, positions=[_position("pos-1", "1")])

    stored = load_management_batch(sf, batch.id)
    assert result.checked == 0
    assert stored.status == "recovery_required"
    assert stored.reason_code == "operator_review_required"
    assert stored.legs[0].status == "submitted"


def test_composite_restored_partial_failure_is_never_reprocessed_as_close_phase(
    tmp_path,
):
    sf = create_session_factory(tmp_path / "research.db")
    batch = _persist_batch(
        sf,
        action="partial_then_break_even",
        sizes=("1", "2"),
        preflight=("2", "4"),
    )
    assert transition_batch(
        sf,
        batch.id,
        expected_statuses={"reconciling"},
        new_status="partial_failed",
        reason_code="protection_replacement_failed_and_restored",
    )
    assert transition_leg(
        sf,
        batch.legs[0].id,
        expected_statuses={"submitted"},
        new_status="succeeded",
    )
    assert transition_leg(
        sf,
        batch.legs[1].id,
        expected_statuses={"submitted"},
        new_status="restored",
        last_error={"stage": "replace_protection"},
    )

    _reconcile_management(
        sf,
        positions=[_position("pos-1", "1"), _position("pos-2", "2")],
        orders=[
            {"ordId": "close-1", "clOrdId": "TMCLIENT1"},
            {"ordId": "close-2", "clOrdId": "TMCLIENT2"},
        ],
    )

    stored = load_management_batch(sf, batch.id)
    assert stored.status == "partial_failed"
    assert stored.reason_code == "protection_replacement_failed_and_restored"
    assert [leg.status for leg in stored.legs] == ["succeeded", "restored"]
    assert stored.legs[1].last_error == {"stage": "replace_protection"}
