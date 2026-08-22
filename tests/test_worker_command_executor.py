import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import WorkerCommandJob
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _claim(session_factory, *, command_type, request):
    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
    )

    enqueue_worker_command(
        session_factory,
        command_type=command_type,
        request=request,
        created_at=NOW,
    )
    return claim_worker_commands(
        session_factory,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )[0]


def _config(chat_id):
    return SystemOperatorBotConfig(bot_token="token", chat_id=chat_id)


def test_sync_adapter_preserves_domain_arguments_result_and_notification_order(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "sync-adapter.db")
    claim = _claim(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
    )
    client = SimpleNamespace(list_open_orders=lambda: [])
    calls = []
    worker_threads = []

    def fake_reconcile(session_factory, *, client, recovered_at, contract_spec_provider):
        worker_threads.append(threading.get_ident())
        calls.append(
            (
                "reconcile",
                session_factory,
                client,
                recovered_at,
                contract_spec_provider,
            )
        )
        return SimpleNamespace(active=3, open=4, stale=5)

    def fake_sync(session_factory, *, client, synced_at):
        worker_threads.append(threading.get_ident())
        calls.append(("sync", session_factory, client, synced_at))
        return SimpleNamespace(
            checked=7, manually_closed=2, skipped_without_pos_id=1
        )

    async def attribution(session_factory, *, config, delivered_at, **_kwargs):
        calls.append(("attribution", session_factory, config.chat_id, delivered_at))

    async def protection(session_factory, *, config, delivered_at, **_kwargs):
        calls.append(("protection", session_factory, config.chat_id, delivered_at))

    async def cleanup(session_factory, *, config, delivered_at, **_kwargs):
        calls.append(("cleanup", session_factory, config.chat_id, delivered_at))

    monkeypatch.setattr(executor, "reconcile_deepcoin_execution_bindings", fake_reconcile)
    monkeypatch.setattr(executor, "sync_manual_closed_deepcoin_positions", fake_sync)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: client,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
        notification_bot_config=_config("notification-chat"),
        system_operator_bot_config=_config("operator-chat"),
        attribution_incident_deliverer=attribution,
        protection_incident_deliverer=protection,
        cleanup_notification_deliverer=cleanup,
    )

    result = asyncio.run(
        executor.execute_worker_command_adapter(claim, dependencies=dependencies)
    )

    assert result.http_status == 200
    assert result.body == {
        "checked": 7,
        "manually_closed": 2,
        "skipped_without_pos_id": 1,
        "reconciled_active": 3,
        "reconciled_open": 4,
        "reconciled_stale": 5,
    }
    assert [call[0] for call in calls] == [
        "reconcile",
        "sync",
        "attribution",
        "protection",
        "cleanup",
    ]
    assert calls[0][1:] == (
        session_factory,
        client,
        NOW,
        "contract-provider",
    )
    assert calls[1][1:] == (session_factory, client, NOW)
    assert worker_threads and all(
        thread_id != threading.get_ident() for thread_id in worker_threads
    )


def test_sync_adapter_rejects_unknown_effects_policy_before_client_construction(
    tmp_path,
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "invalid-sync-policy.db")
    claim = _claim(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={"effects_policy": "unknown"},
    )
    factory_calls = []
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: factory_calls.append(True) or object(),
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    with pytest.raises(
        executor.WorkerCommandMappedError,
        match="invalid sync effects policy",
    ):
        asyncio.run(
            executor.execute_worker_command_adapter(
                claim,
                dependencies=dependencies,
            )
        )

    assert factory_calls == []


def test_sync_adapter_reconcile_only_blocks_mutation_and_notification(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "reconcile-only-sync.db")
    claim = _claim(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={"effects_policy": "reconcile_only"},
    )
    calls = []

    class Client:
        def list_positions(self):
            return [{"posId": "position-1", "sz": "1"}]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id, pos_id):
            return []

        def submit_order(self, _payload):
            raise AssertionError("submit must be unreachable")

        def cancel_order(self, _payload):
            raise AssertionError("cancel must be unreachable")

        def amend_order(self, _payload):
            raise AssertionError("amend must be unreachable")

        def close_position(self, _payload):
            raise AssertionError("close must be unreachable")

    original_client = Client()

    def reject_full_reconcile(*_args, **_kwargs):
        raise AssertionError("full reconciler must be unreachable")

    def fake_read_only_reconcile(
        session_factory, *, client, recovered_at
    ):
        assert client.list_positions() == [{"posId": "position-1", "sz": "1"}]
        for name in ("submit_order", "cancel_order", "amend_order", "close_position"):
            with pytest.raises(AttributeError):
                getattr(client, name)
        calls.append(("read_only_reconcile", session_factory, recovered_at))
        return SimpleNamespace(active=3, open=4, stale=5)

    def fake_sync(
        session_factory,
        *,
        client,
        synced_at,
        allow_exchange_mutations,
    ):
        assert client.list_open_orders() == []
        with pytest.raises(AttributeError):
            getattr(client, "cancel_order")
        calls.append(
            (
                "manual_sync",
                session_factory,
                synced_at,
                allow_exchange_mutations,
            )
        )
        return SimpleNamespace(
            checked=7,
            manually_closed=2,
            skipped_without_pos_id=1,
        )

    async def unexpected_delivery(*_args, **_kwargs):
        calls.append(("unexpected_delivery",))

    monkeypatch.setattr(
        executor,
        "reconcile_deepcoin_execution_bindings",
        reject_full_reconcile,
    )
    monkeypatch.setattr(
        executor,
        "reconcile_deepcoin_execution_bindings_read_only",
        fake_read_only_reconcile,
        raising=False,
    )
    monkeypatch.setattr(executor, "sync_manual_closed_deepcoin_positions", fake_sync)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: original_client,
        contract_spec_provider="must-not-be-used",
        now_provider=lambda: NOW,
        notification_bot_config=_config("notification-chat"),
        system_operator_bot_config=_config("operator-chat"),
        attribution_incident_deliverer=unexpected_delivery,
        protection_incident_deliverer=unexpected_delivery,
        cleanup_notification_deliverer=unexpected_delivery,
    )

    result = asyncio.run(
        executor.execute_worker_command_adapter(claim, dependencies=dependencies)
    )

    assert result.http_status == 200
    assert result.body == {
        "checked": 7,
        "manually_closed": 2,
        "skipped_without_pos_id": 1,
        "reconciled_active": 3,
        "reconciled_open": 4,
        "reconciled_stale": 5,
    }
    assert calls == [
        ("read_only_reconcile", session_factory, NOW),
        ("manual_sync", session_factory, NOW, False),
    ]


def test_close_adapter_preserves_domain_arguments_result_and_cleanup_order(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "close-adapter.db")
    claim = _claim(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-7"},
    )
    client = object()
    calls = []
    worker_thread = []
    expected = {"submitted": True, "pos_id": "position-7", "order_id": "close-9"}

    def fake_close(session_factory, *, pos_id, deepcoin_client, executed_at):
        worker_thread.append(threading.get_ident())
        calls.append(
            ("close", session_factory, pos_id, deepcoin_client, executed_at)
        )
        return expected

    async def cleanup(session_factory, *, config, delivered_at, **_kwargs):
        calls.append(("cleanup", session_factory, config.chat_id, delivered_at))

    monkeypatch.setattr(executor, "close_bound_position_market", fake_close)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: client,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
        system_operator_bot_config=_config("operator-chat"),
        cleanup_notification_deliverer=cleanup,
    )

    result = asyncio.run(
        executor.execute_worker_command_adapter(claim, dependencies=dependencies)
    )

    assert result.http_status == 200
    assert result.body == expected
    assert calls == [
        ("close", session_factory, "position-7", client, NOW),
        ("cleanup", session_factory, "operator-chat", NOW),
    ]
    assert worker_thread[0] != threading.get_ident()


def test_recovery_adapter_preserves_domain_arguments_and_result(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "recovery-adapter.db")
    request = {"chat_id": 100, "message_id": 55, "symbol": "BTC", "side": "long"}
    claim = _claim(
        session_factory,
        command_type="recovery_live_submit",
        request=request,
    )
    client = object()
    calls = []
    expected = {"submitted": True, "order_count": 2, "signal_id": 41}

    def fake_submit(session_factory, **kwargs):
        calls.append((threading.get_ident(), session_factory, kwargs))
        return expected

    monkeypatch.setattr(executor, "submit_recovery_order_live", fake_submit)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: client,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    result = asyncio.run(
        executor.execute_worker_command_adapter(claim, dependencies=dependencies)
    )

    assert result.body == expected
    assert calls == [
        (
            calls[0][0],
            session_factory,
            {
                **request,
                "deepcoin_client": client,
                "contract_spec_provider": "contract-provider",
                "submitted_at": NOW,
            },
        )
    ]
    assert calls[0][0] != threading.get_ident()


def test_process_next_adapter_preserves_factory_arguments_and_empty_result(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "process-next-adapter.db")
    claim = _claim(
        session_factory,
        command_type="process_next_trade_signal",
        request={},
    )
    client_factory = object()
    calls = []

    def fake_process(session_factory, **kwargs):
        calls.append((threading.get_ident(), session_factory, kwargs))
        return None

    monkeypatch.setattr(executor, "process_next_trade_signal_live", fake_process)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=client_factory,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    result = asyncio.run(
        executor.execute_worker_command_adapter(claim, dependencies=dependencies)
    )

    assert result.body == {"processed": False, "result": None}
    assert calls == [
        (
            calls[0][0],
            session_factory,
            {
                "deepcoin_client_factory": client_factory,
                "contract_spec_provider": "contract-provider",
                "processed_at": NOW,
            },
        )
    ]
    assert calls[0][0] != threading.get_ident()


@pytest.mark.parametrize(
    (
        "command_type",
        "request_payload",
        "target",
        "error",
        "expected_status",
        "expected_detail",
    ),
    [
        (
            "sync_deepcoin_execution",
            {},
            "sync_manual_closed_deepcoin_positions",
            __import__(
                "telegram_kol_research.deepcoin_client", fromlist=["DeepcoinClientError"]
            ).DeepcoinClientError("sync unavailable"),
            502,
            "sync unavailable",
        ),
        (
            "sync_deepcoin_execution",
            {},
            "sync_manual_closed_deepcoin_positions",
            RuntimeError("sync internal"),
            500,
            "sync internal",
        ),
        (
            "close_bound_position",
            {"pos_id": "position-7"},
            "close_bound_position_market",
            __import__(
                "telegram_kol_research.deepcoin_execution_actions",
                fromlist=["DeepcoinExecutionActionError"],
            ).DeepcoinExecutionActionError("position conflict"),
            409,
            "position conflict",
        ),
        (
            "close_bound_position",
            {"pos_id": "position-7"},
            "close_bound_position_market",
            RuntimeError("must stay hidden"),
            500,
            "bound position close failed",
        ),
        (
            "recovery_live_submit",
            {"chat_id": 100, "message_id": 55, "symbol": "BTC", "side": "long"},
            "submit_recovery_order_live",
            __import__(
                "telegram_kol_research.recovery_live_submit",
                fromlist=["RecoveryLiveSubmitError"],
            ).RecoveryLiveSubmitError("recovery conflict"),
            409,
            "recovery conflict",
        ),
        (
            "recovery_live_submit",
            {"chat_id": 100, "message_id": 55, "symbol": "BTC", "side": "long"},
            "submit_recovery_order_live",
            ValueError("invalid recovery"),
            422,
            "invalid recovery",
        ),
        (
            "process_next_trade_signal",
            {},
            "process_next_trade_signal_live",
            __import__(
                "telegram_kol_research.recovery_live_submit",
                fromlist=["RecoveryLiveSubmitError"],
            ).RecoveryLiveSubmitError("signal conflict"),
            409,
            "signal conflict",
        ),
    ],
)
def test_adapter_maps_existing_route_errors_without_retry(
    tmp_path,
    monkeypatch,
    command_type,
    request_payload,
    target,
    error,
    expected_status,
    expected_detail,
):
    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / f"{command_type}-error.db")
    claim = _claim(
        session_factory,
        command_type=command_type,
        request=request_payload,
    )
    calls = []

    def raise_error(*_args, **_kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(executor, target, raise_error)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=object,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    with pytest.raises(executor.WorkerCommandMappedError) as exc_info:
        asyncio.run(
            executor.execute_worker_command_adapter(
                claim,
                dependencies=dependencies,
            )
        )

    assert exc_info.value.http_status == expected_status
    assert exc_info.value.body == {"detail": expected_detail}
    assert exc_info.value.error_code == type(error).__name__
    assert calls == [True]


def test_unknown_command_type_fails_closed_without_any_adapter_call(
    tmp_path,
):
    from dataclasses import replace

    import telegram_kol_research.worker_command_executor as executor

    session_factory = create_session_factory(tmp_path / "unknown-command.db")
    claim = _claim(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
    )
    claim = replace(claim, command_type="unknown")
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: pytest.fail("client must not be created"),
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    with pytest.raises(executor.WorkerCommandMappedError) as exc_info:
        asyncio.run(
            executor.execute_worker_command_adapter(
                claim,
                dependencies=dependencies,
            )
        )

    assert exc_info.value.http_status == 500
    assert exc_info.value.error_code == "unsupported_worker_command_type"


def test_worker_tick_commits_executing_before_adapter_and_settles_once(tmp_path):
    import telegram_kol_research.worker_command_executor as executor
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "worker-tick.db")
    save_trading_settings(session_factory, {"worker_command_mode": "queue"})
    command = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    adapter_calls = []

    async def adapter(claim, *, dependencies):
        with session_factory() as session:
            row = session.get(WorkerCommandJob, claim.job_id)
            adapter_calls.append(
                (row.status, row.side_effect_started_at, claim.command_id)
            )
        return executor.WorkerCommandExecutionResult(
            http_status=200,
            body={"checked": 1},
        )

    result = asyncio.run(
        executor.run_worker_command_tick(
            session_factory,
            dependencies=executor.WorkerCommandDependencies(
                session_factory=session_factory,
                deepcoin_client_factory=object,
                contract_spec_provider="contract-provider",
                now_provider=lambda: NOW,
            ),
            now=NOW,
            adapter=adapter,
        )
    )

    assert result == executor.WorkerCommandWorkerResult(claimed=1, succeeded=1)
    assert adapter_calls == [
        ("executing", NOW.replace(tzinfo=None), command.command_id)
    ]
    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
        assert row.status == "succeeded"
        assert row.attempt_count == 1
        assert row.result_json == '{"checked":1}'


def test_worker_tick_settles_mapped_error_and_never_retries_it(tmp_path):
    import telegram_kol_research.worker_command_executor as executor
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "mapped-error-tick.db")
    save_trading_settings(session_factory, {"worker_command_mode": "queue"})
    enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-7"},
        created_at=NOW,
    )
    calls = []

    async def adapter(*_args, **_kwargs):
        calls.append(True)
        raise executor.WorkerCommandMappedError(
            http_status=409,
            body={"detail": "position conflict"},
            error_code="DeepcoinExecutionActionError",
            error_summary="position conflict",
        )

    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=object,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )
    first = asyncio.run(
        executor.run_worker_command_tick(
            session_factory,
            dependencies=dependencies,
            now=NOW,
            adapter=adapter,
        )
    )
    second = asyncio.run(
        executor.run_worker_command_tick(
            session_factory,
            dependencies=dependencies,
            now=NOW + timedelta(minutes=1),
            adapter=adapter,
        )
    )

    assert first == executor.WorkerCommandWorkerResult(claimed=1, failed=1)
    assert second == executor.WorkerCommandWorkerResult()
    assert calls == [True]
    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
        assert row.status == "failed"
        assert row.http_status == 409
        assert row.result_json == '{"detail":"position conflict"}'


def test_worker_tick_cancellation_leaves_executing_for_uncertain_sweeper(tmp_path):
    import telegram_kol_research.worker_command_executor as executor
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "cancel-tick.db")
    save_trading_settings(session_factory, {"worker_command_mode": "queue"})
    enqueue_worker_command(
        session_factory,
        command_type="recovery_live_submit",
        request={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
        created_at=NOW,
    )
    started = asyncio.Event()

    async def adapter(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=object,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    async def exercise():
        task = asyncio.create_task(
            executor.run_worker_command_tick(
                session_factory,
                dependencies=dependencies,
                now=NOW,
                lease_for=timedelta(seconds=30),
                adapter=adapter,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
        assert row.status == "executing"
    swept = asyncio.run(
        executor.run_worker_command_tick(
            session_factory,
            dependencies=dependencies,
            now=NOW + timedelta(seconds=31),
            lease_for=timedelta(seconds=30),
            adapter=lambda *_args, **_kwargs: pytest.fail("must not replay"),
        )
    )
    assert swept == executor.WorkerCommandWorkerResult(uncertain=1)


@pytest.mark.parametrize("mode", ["inline", "shadow"])
def test_worker_tick_is_dormant_outside_queue_mode(tmp_path, mode):
    import telegram_kol_research.worker_command_executor as executor
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / f"dormant-{mode}.db")
    save_trading_settings(session_factory, {"worker_command_mode": mode})
    enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )

    result = asyncio.run(
        executor.run_worker_command_tick(
            session_factory,
            dependencies=executor.WorkerCommandDependencies(
                session_factory=session_factory,
                deepcoin_client_factory=lambda: pytest.fail(
                    "client must stay dormant"
                ),
                contract_spec_provider="contract-provider",
                now_provider=lambda: NOW,
            ),
            now=NOW,
        )
    )

    assert result == executor.WorkerCommandWorkerResult()
    with session_factory() as session:
        assert session.query(WorkerCommandJob).one().status == "pending"


def test_worker_loop_runs_queue_ticks_and_stops_when_mode_changes(
    tmp_path, monkeypatch
):
    import telegram_kol_research.worker_command_executor as executor
    from telegram_kol_research.trading_settings import save_trading_settings

    session_factory = create_session_factory(tmp_path / "worker-loop.db")
    save_trading_settings(session_factory, {"worker_command_mode": "queue"})
    ticks = []

    async def fake_tick(*args, **kwargs):
        ticks.append((args, kwargs))
        await asyncio.to_thread(
            save_trading_settings,
            session_factory,
            {"worker_command_mode": "inline"},
        )
        return executor.WorkerCommandWorkerResult()

    monkeypatch.setattr(executor, "run_worker_command_tick", fake_tick)
    dependencies = executor.WorkerCommandDependencies(
        session_factory=session_factory,
        deepcoin_client_factory=object,
        contract_spec_provider="contract-provider",
        now_provider=lambda: NOW,
    )

    asyncio.run(
        executor.run_worker_command_loop(
            session_factory,
            dependencies=dependencies,
            interval_seconds=0.01,
        )
    )

    assert len(ticks) == 1
    assert ticks[0][0] == (session_factory,)
    assert ticks[0][1]["dependencies"] is dependencies
