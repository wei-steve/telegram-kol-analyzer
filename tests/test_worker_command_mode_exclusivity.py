import asyncio
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import WorkerCommandJob
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _job(*, command_id: str, status: str) -> WorkerCommandJob:
    return WorkerCommandJob(
        command_id=command_id,
        command_type="sync_deepcoin_execution",
        request_json="{}",
        request_fingerprint=command_id.ljust(64, "0")[:64],
        status=status,
        attempt_count=1,
        result_schema_version=1,
        created_at=NOW,
        side_effect_started_at=(NOW if status == "executing" else None),
        uncertain_at=(NOW if status == "uncertain" else None),
    )


@pytest.mark.parametrize("status", ["claimed", "executing"])
def test_mode_transition_refuses_in_flight_commands(tmp_path, status):
    from telegram_kol_research.worker_command_executor import (
        WorkerCommandModeTransitionError,
        require_worker_command_mode_transition_safe,
    )

    session_factory = create_session_factory(tmp_path / f"refuse-{status}.db")
    save_trading_settings(session_factory, {"worker_command_mode": "queue"})
    with session_factory() as session:
        session.add(_job(command_id=f"job-{status}", status=status))
        session.commit()

    with pytest.raises(WorkerCommandModeTransitionError) as exc_info:
        require_worker_command_mode_transition_safe(
            session_factory, current_mode="queue", candidate_mode="inline"
        )

    assert exc_info.value.claimed + exc_info.value.executing == 1


def test_mode_transition_preserves_uncertain_and_never_adopts_shadow(tmp_path):
    from telegram_kol_research.worker_command_executor import (
        require_worker_command_mode_transition_safe,
    )

    session_factory = create_session_factory(tmp_path / "uncertain.db")
    with session_factory() as session:
        session.add(_job(command_id="job-uncertain", status="uncertain"))
        session.commit()

    require_worker_command_mode_transition_safe(
        session_factory, current_mode="queue", candidate_mode="inline"
    )

    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
    assert row.status == "uncertain"


def test_mode_supervisor_enters_queue_once_and_reacts_to_disable(tmp_path):
    from telegram_kol_research.worker_command_executor import (
        supervise_worker_command_mode,
    )

    session_factory = create_session_factory(tmp_path / "supervisor.db")
    calls = []

    async def queue_runner(_session_factory, **_kwargs):
        calls.append("queue")
        save_trading_settings(session_factory, {"worker_command_mode": "inline"})

    async def scenario():
        save_trading_settings(session_factory, {"worker_command_mode": "queue"})
        task = asyncio.create_task(
            supervise_worker_command_mode(
                session_factory,
                dependencies=object(),
                queue_runner=queue_runner,
                interval_seconds=0.001,
            )
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert calls == ["queue"]


def test_mode_supervisor_cancellation_does_not_wait_for_blocking_adapter(tmp_path):
    from telegram_kol_research.worker_command_executor import (
        supervise_worker_command_mode,
    )

    session_factory = create_session_factory(tmp_path / "cancel.db")
    started = asyncio.Event()

    async def stuck_runner(_session_factory, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        save_trading_settings(session_factory, {"worker_command_mode": "queue"})
        task = asyncio.create_task(
            supervise_worker_command_mode(
                session_factory,
                dependencies=object(),
                queue_runner=stuck_runner,
                interval_seconds=0.001,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(scenario())
