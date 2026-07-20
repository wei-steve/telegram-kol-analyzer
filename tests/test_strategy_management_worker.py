from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import StrategyManagementBatch
from telegram_kol_research.strategy_management_executor import (
    ManagementBatchExecutionError,
)
from telegram_kol_research.strategy_management_batches import load_management_batch
from telegram_kol_research.strategy_management_worker import (
    StrategyManagementWorkerCursor,
    run_strategy_management_worker_tick,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _batch(*, batch_id: int, strategy: str, status: str, action="partial_close", legs=()):
    return SimpleNamespace(
        id=batch_id,
        strategy_instance_id=strategy,
        status=status,
        effective_action=action,
        reason_code=None,
        legs=tuple(legs),
    )


def test_worker_claims_ready_batch_once_across_racing_ticks(tmp_path):
    session_factory = create_session_factory(tmp_path / "worker.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="a" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="b" * 64,
        target_snapshot_json="{}",
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        # This test exercises the database claim only; FK enforcement is disabled
        # in the SQLite test database just as it is for existing batch unit tests.
        session.add(row)
        session.commit()
        batch_id = row.id
        strategy_instance_id = row.strategy_instance_id

    executed = []
    lister = lambda *_args, **_kwargs: [
        _batch(batch_id=batch_id, strategy=strategy_instance_id, status="ready")
    ]

    def execute(*_args, **kwargs):
        executed.append(kwargs["batch_id"])

    def tick():
        return run_strategy_management_worker_tick(
            session_factory,
            deepcoin_client_factory=lambda: object(),
                batch_lister=lister,
                executor=execute,
                restart_validator=lambda *_args, **_kwargs: None,
            max_batches=1,
            processed_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [
            future.result() for future in (pool.submit(tick), pool.submit(tick))
        ]

    assert executed == [batch_id]
    assert sorted((first.executed, second.executed)) == [0, 1]


def test_restart_queries_exchange_before_reconciling_and_never_resubmits_unknown():
    events = []
    unknown = _batch(
        batch_id=7,
        strategy="deepcoin:100:10:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submit_unknown"),),
    )

    def snapshot_loader(*_args, **_kwargs):
        events.append("exchange-read")
        return object()

    def reconciler(*_args, **kwargs):
        events.append(("reconcile", tuple(kwargs["batch_ids"])))

    def executor(*_args, **_kwargs):
        raise AssertionError("submit_unknown must never be submitted again")

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [unknown],
        snapshot_loader=snapshot_loader,
        reconciler=reconciler,
        executor=executor,
        processed_at=NOW,
    )

    assert events == ["exchange-read", ("reconcile", (7,))]
    assert result.recovered == 1
    assert result.executed == 0


@pytest.mark.parametrize(
    "status", ["reserved", "submitted", "submit_unknown", "reconciling"]
)
def test_restart_batch_states_are_reconciled_only(status):
    events = []
    batch = _batch(
        batch_id=8,
        strategy="deepcoin:100:10:BTC:short",
        status=status,
        legs=(SimpleNamespace(status=status),),
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [batch],
        snapshot_loader=lambda *_args, **_kwargs: events.append("read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("reconcile"),
        executor=lambda *_args, **_kwargs: events.append("execute"),
        processed_at=NOW,
    )

    assert events == ["read", "reconcile"]
    assert result.recovered == 1


def test_recovery_required_is_paused_and_other_strategies_are_independent():
    executed = []
    batches = [
        _batch(batch_id=1, strategy="deepcoin:100:10:BTC:short", status="recovery_required"),
        _batch(batch_id=2, strategy="deepcoin:200:10:BTC:short", status="ready"),
    ]

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: batches,
        claimer=lambda *_args, **kwargs: kwargs["batch_id"] == 2,
        executor=lambda *_args, **kwargs: executed.append(kwargs["batch_id"]),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert executed == [2]
    assert result.executed == 1
    assert result.paused == 1


def test_disabled_or_shadow_mode_never_claims_or_executes_ready_batches():
    events = []
    ready = _batch(
        batch_id=2,
        strategy="deepcoin:200:10:BTC:short",
        status="ready",
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: events.append("client") or object(),
        batch_lister=lambda *_args, **_kwargs: [ready],
        claimer=lambda *_args, **_kwargs: events.append("claim") or True,
        executor=lambda *_args, **_kwargs: events.append("execute"),
        allow_execution=False,
        processed_at=NOW,
    )

    assert events == []
    assert result.skipped == 1


def test_batch_failure_does_not_stop_later_bounded_work():
    batches = [
        _batch(batch_id=1, strategy="deepcoin:100:10:BTC:short", status="ready"),
        _batch(batch_id=2, strategy="deepcoin:200:10:BTC:short", status="ready"),
        _batch(batch_id=3, strategy="deepcoin:300:10:BTC:short", status="ready"),
    ]
    executed = []

    def execute(*_args, **kwargs):
        executed.append(kwargs["batch_id"])
        if kwargs["batch_id"] == 1:
            raise RuntimeError("boom")

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: batches,
        claimer=lambda *_args, **_kwargs: True,
        executor=execute,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=2,
        processed_at=NOW,
    )

    assert executed == [1, 2]
    assert result.failed == 1
    assert result.executed == 1
    assert result.discovered == 2


def test_composite_protection_phase_is_not_sent_to_close_reconciliation():
    composite = _batch(
        batch_id=9,
        strategy="deepcoin:100:10:BTC:short",
        status="executing",
        action="partial_then_break_even",
        legs=(SimpleNamespace(status="reserved"),),
    )
    composite.reason_code = "protection_phase_executing"
    events = []

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [composite],
        snapshot_loader=lambda *_args, **_kwargs: events.append("exchange-read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("close-reconcile"),
        executor=lambda *_args, **_kwargs: events.append("protection-executor"),
        processed_at=NOW,
    )

    assert events == ["exchange-read", "protection-executor"]


def test_composite_protection_leg_state_survives_missing_reason_after_restart():
    composite = _batch(
        batch_id=10,
        strategy="deepcoin:100:10:BTC:short",
        status="executing",
        action="partial_then_break_even",
        legs=(SimpleNamespace(status="succeeded"),),
    )
    events = []

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [composite],
        snapshot_loader=lambda *_args, **_kwargs: events.append("read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("close-reconcile"),
        executor=lambda *_args, **_kwargs: events.append("protection-executor"),
        processed_at=NOW,
    )

    assert events == ["read", "protection-executor"]


def test_ready_claim_with_deterministic_pre_submit_failure_is_persistently_blocked(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "blocked.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="z" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="y" * 64,
        target_snapshot_json="{}",
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_binding_not_active_or_exact")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.failed == 1
    assert stored.status == "blocked"
    assert stored.reason_code == "management_pre_submit_validation_failed"


def test_full_exit_deferred_set_validation_failure_uses_cancellation_recovery_path(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "deferred-recovery.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="d" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="e" * 64,
        target_snapshot_json='{"identity":{"deferred_entry_leg_ids":[]}}',
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_entry_set_not_exact")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.failed == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "deferred_entry_cancel_preflight_failed"


def test_full_exit_restart_validator_deferred_set_failure_uses_cancellation_recovery_path(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "deferred-restart-recovery.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="f" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="executing",
        target_fingerprint="g" * 64,
        target_snapshot_json='{"identity":{"deferred_entry_leg_ids":[2]}}',
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_entry_set_not_exact")
        ),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe restart must stay frozen")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.recovered == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "deferred_entry_cancel_preflight_failed"


def test_old_reconciling_backlog_does_not_starve_later_ready_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "fair.db")
    with session_factory() as session:
        for index in range(5):
            session.add(
                StrategyManagementBatch(
                    idempotency_fingerprint=f"recovery-{index}",
                    raw_message_id=index + 1,
                    recognition_decision_id=index + 1,
                    recognition_generation="g1",
                    target_lifecycle_id=index + 1,
                    strategy_instance_id=f"deepcoin:{index}:10:BTC:short",
                    execution_binding_id=index + 1,
                    intent="full_take_profit",
                    effective_action="full_exit",
                    partial_round_before=0,
                    status="reconciling",
                    target_fingerprint=f"target-{index}",
                    target_snapshot_json="{}",
                    planned_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        ready = StrategyManagementBatch(
            idempotency_fingerprint="later-ready",
            raw_message_id=99,
            recognition_decision_id=99,
            recognition_generation="g1",
            target_lifecycle_id=99,
            strategy_instance_id="deepcoin:999:10:BTC:short",
            execution_binding_id=99,
            intent="full_take_profit",
            effective_action="full_exit",
            partial_round_before=0,
            status="ready",
            target_fingerprint="later-target",
            target_snapshot_json="{}",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(ready)
        session.commit()
        ready_id = ready.id
    executed = []

    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: None,
        executor=lambda *_args, **kwargs: executed.append(kwargs["batch_id"]),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=2,
        processed_at=NOW,
    )

    assert ready_id in executed


def test_max_one_cursor_gives_recovery_a_bounded_live_tick():
    cursor = StrategyManagementWorkerCursor()
    ready = _batch(batch_id=1, strategy="deepcoin:1:1:BTC:short", status="ready")
    recovery = _batch(
        batch_id=2,
        strategy="deepcoin:2:2:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submitted"),),
    )
    events = []

    def lister(*_args, **kwargs):
        return [recovery] if kwargs["prefer_recovery"] else [ready]

    common = dict(
        deepcoin_client_factory=lambda: object(),
        batch_lister=lister,
        claimer=lambda *_args, **_kwargs: True,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: events.append("recovery"),
        executor=lambda *_args, **_kwargs: events.append("ready"),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=1,
        cursor=cursor,
        processed_at=NOW,
    )
    run_strategy_management_worker_tick(object(), **common)
    run_strategy_management_worker_tick(object(), **common)

    assert events == ["ready", "recovery"]


def test_disabled_shadow_max_one_prioritizes_recovery_and_never_claims_ready():
    cursor = StrategyManagementWorkerCursor()
    recovery = _batch(
        batch_id=2,
        strategy="deepcoin:2:2:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submitted"),),
    )
    events = []

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **kwargs: (
            [recovery]
            if kwargs["prefer_recovery"]
            else (_ for _ in ()).throw(AssertionError("ready lane selected"))
        ),
        claimer=lambda *_args, **_kwargs: events.append("claim") or True,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: events.append("recovery"),
        executor=lambda *_args, **_kwargs: events.append("write"),
        max_batches=1,
        cursor=cursor,
        allow_execution=False,
        processed_at=NOW,
    )

    assert result.recovered == 1
    assert events == ["recovery"]
