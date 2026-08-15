import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import StrategyManagementComponent
from telegram_kol_research.strategy_management_components import (
    claim_management_component,
    create_management_component,
    transition_component_for_exact_position_absent_recovery,
    transition_management_component,
)
import telegram_kol_research.strategy_management_worker as worker_module
from telegram_kol_research.strategy_management_worker import (
    StrategyManagementWorkerResult,
    run_strategy_management_worker_tick,
    run_strategy_management_worker_loop,
)


def _create(
    session, *, idempotency_key="batch:1:tp", management_batch_id=1
):
    return create_management_component(
        session,
        management_batch_id=management_batch_id,
        strategy_management_leg_id=None,
        component_kind="consume_take_profit_stage",
        sequence=10,
        idempotency_key=idempotency_key,
        desired={"policy": "consume_first_stage"},
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_management_worker_heartbeat_uses_successful_tick_completion_time(monkeypatch):
    tick_entered = asyncio.Event()
    callbacks = []
    times = iter(
        [
            datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 12, 0, 3, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(
        worker_module,
        "load_trading_settings",
        lambda session_factory: SimpleNamespace(live_management_execution_enabled=False),
    )

    def tick(*args, **kwargs):
        tick_entered.set()
        return StrategyManagementWorkerResult()

    monkeypatch.setattr(worker_module, "run_strategy_management_worker_tick", tick)

    async def exercise():
        task = asyncio.create_task(
            run_strategy_management_worker_loop(
                session_factory=object(),
                deepcoin_client_factory=lambda: object(),
                interval_seconds=3600,
                now_provider=lambda: next(times),
                success_callback=callbacks.append,
            )
        )
        await asyncio.wait_for(tick_entered.wait(), timeout=2)
        while not callbacks:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert callbacks == [datetime(2026, 8, 15, 12, 0, 3, tzinfo=UTC)]


def test_management_worker_failure_and_cancellation_do_not_heartbeat(monkeypatch):
    tick_entered = asyncio.Event()
    callbacks = []
    monkeypatch.setattr(
        worker_module,
        "load_trading_settings",
        lambda session_factory: SimpleNamespace(live_management_execution_enabled=False),
    )

    def tick(*args, **kwargs):
        tick_entered.set()
        raise RuntimeError("failed")

    monkeypatch.setattr(worker_module, "run_strategy_management_worker_tick", tick)

    async def exercise():
        task = asyncio.create_task(
            run_strategy_management_worker_loop(
                session_factory=object(),
                deepcoin_client_factory=lambda: object(),
                interval_seconds=3600,
                success_callback=callbacks.append,
            )
        )
        await asyncio.wait_for(tick_entered.wait(), timeout=2)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert callbacks == []


def test_management_worker_callback_failure_is_not_a_tick_failure(monkeypatch, caplog):
    tick_entered = asyncio.Event()
    callback_entered = asyncio.Event()
    monkeypatch.setattr(
        worker_module,
        "load_trading_settings",
        lambda session_factory: SimpleNamespace(live_management_execution_enabled=False),
    )

    def tick(*args, **kwargs):
        tick_entered.set()
        return StrategyManagementWorkerResult()

    def callback(_completed_at):
        callback_entered.set()
        raise RuntimeError("heartbeat sink failed")

    monkeypatch.setattr(worker_module, "run_strategy_management_worker_tick", tick)

    async def exercise():
        task = asyncio.create_task(
            run_strategy_management_worker_loop(
                session_factory=object(),
                deepcoin_client_factory=lambda: object(),
                interval_seconds=3600,
                success_callback=callback,
            )
        )
        await asyncio.wait_for(tick_entered.wait(), timeout=2)
        await asyncio.wait_for(callback_entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert "strategy management worker tick failed" not in caplog.text


def test_management_worker_failed_result_does_not_heartbeat(monkeypatch):
    tick_entered = asyncio.Event()
    callbacks = []
    monkeypatch.setattr(
        worker_module,
        "load_trading_settings",
        lambda session_factory: SimpleNamespace(live_management_execution_enabled=False),
    )

    def tick(*args, **kwargs):
        tick_entered.set()
        return StrategyManagementWorkerResult(failed=1, cycle_success=False)

    monkeypatch.setattr(worker_module, "run_strategy_management_worker_tick", tick)

    async def exercise():
        task = asyncio.create_task(
            run_strategy_management_worker_loop(
                session_factory=object(),
                deepcoin_client_factory=lambda: object(),
                interval_seconds=3600,
                success_callback=callbacks.append,
            )
        )
        await asyncio.wait_for(tick_entered.wait(), timeout=2)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert callbacks == []


@pytest.mark.parametrize("core_lane", ["contract", "composite"])
def test_management_worker_swallowed_core_error_marks_cycle_unsuccessful(
    monkeypatch, core_lane
):
    import telegram_kol_research.instruction_execution_management_adapter as adapter

    def fail(*args, **kwargs):
        raise RuntimeError("core lane failed")

    if core_lane == "contract":
        monkeypatch.setattr(
            adapter,
            "converge_unknown_management_instruction_contracts",
            fail,
        )
        composite = worker_module.reconcile_composite_management_components
    else:
        monkeypatch.setattr(
            adapter,
            "converge_unknown_management_instruction_contracts",
            lambda *args, **kwargs: None,
        )
        composite = fail

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        allow_execution=False,
        batch_lister=lambda *args, **kwargs: [],
        composite_reconciler=composite,
    )

    assert result.failed == 0
    assert result.cycle_success is False


@pytest.mark.parametrize("initial_status", ["pending", "recovery_required"])
def test_exact_position_absent_recovery_can_safely_skip_only_batch_119(
    tmp_path, initial_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 12, tzinfo=UTC)
    fingerprint = "a" * 64
    with session_factory() as session:
        component = _create(
            session,
            idempotency_key=f"batch:119:{initial_status}",
            management_batch_id=119,
        )
        component.status = initial_status
        session.flush()

        changed = transition_component_for_exact_position_absent_recovery(
            session,
            component_id=component.id,
            expected_status=initial_status,
            recovery_evidence_fingerprint=fingerprint,
            now=now,
        )

        assert changed is True
        assert component.status == "safely_skipped"
        assert component.reason_code == (
            "composite_recovery_exact_position_absent"
        )
        assert component.completed_at == now.replace(tzinfo=None)
        assert json.loads(component.evidence_json) == [
            {
                "kind": "composite_recovery_exact_position_absent",
                "recovery_evidence_fingerprint": fingerprint,
            }
        ]


def test_normal_component_transition_cannot_safely_skip_pending(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        component = _create(session)
        session.flush()
        with pytest.raises(ValueError, match="invalid component transition"):
            transition_management_component(
                session,
                component_id=component.id,
                expected_status="pending",
                new_status="safely_skipped",
                now=datetime(2026, 8, 12, tzinfo=UTC),
            )


def test_component_identity_is_immutable_and_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        component = _create(session)
        session.commit()
        assert _create(session).id == component.id
        with pytest.raises(ValueError, match="immutable"):
            create_management_component(
                session,
                management_batch_id=1,
                strategy_management_leg_id=None,
                component_kind="consume_take_profit_stage",
                sequence=10,
                idempotency_key="different-key",
                desired={"policy": "consume_first_stage"},
                now=datetime(2026, 8, 4, tzinfo=UTC),
            )


def test_database_rejects_duplicate_batch_wide_component_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 4, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                StrategyManagementComponent(
                    management_batch_id=1,
                    strategy_management_leg_id=None,
                    component_kind="consume_take_profit_stage",
                    sequence=10,
                    status="pending",
                    idempotency_key=f"component-{index}",
                    desired_json="{}",
                    evidence_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                for index in range(2)
            ]
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_component_transition_is_compare_and_set(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        component = _create(session)
        session.flush()

        changed = transition_management_component(
            session,
            component_id=component.id,
            expected_status="pending",
            new_status="preflighting",
            now=datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
        )
        stale = transition_management_component(
            session,
            component_id=component.id,
            expected_status="pending",
            new_status="preflighting",
            now=datetime(2026, 8, 4, 0, 2, tzinfo=UTC),
        )

        assert changed is True
        assert stale is False


def test_restart_claims_only_safe_or_stale_local_states(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 4, 1, tzinfo=UTC)
    with session_factory() as session:
        component = _create(session)
        session.flush()
        assert claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )
        session.execute(
            StrategyManagementComponent.__table__.update()
            .where(StrategyManagementComponent.id == component.id)
            .values(
                status="preflighting",
                updated_at=now - timedelta(minutes=10),
            )
        )
        assert claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )


@pytest.mark.parametrize(
    "protected_status",
    ["submitting", "awaiting_exchange", "confirmed", "operator_required"],
)
def test_restart_never_reclaims_exchange_or_terminal_state_as_new_write(
    tmp_path, protected_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 4, 1, tzinfo=UTC)
    with session_factory() as session:
        component = _create(session)
        session.flush()
        session.execute(
            StrategyManagementComponent.__table__.update()
            .where(StrategyManagementComponent.id == component.id)
            .values(
                status=protected_status,
                updated_at=now - timedelta(hours=1),
            )
        )
        assert not claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )
