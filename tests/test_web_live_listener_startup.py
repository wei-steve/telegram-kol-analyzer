import asyncio

from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_web_app_starts_live_listener_when_targets_are_configured(tmp_path):
    calls: list[tuple[object, set[str], str, object]] = []
    reconcile_calls: list[tuple[object, set[str], str, int, object]] = []
    fake_client = object()

    async def fake_live_listener_runner(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        operation_lock=None,
    ):
        calls.append(
            (client, set(target_titles), str(media_root), operation_lock)
        )

    async def fake_reconcile_runner(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        interval_seconds,
        operation_lock=None,
    ):
        reconcile_calls.append(
            (
                client,
                set(target_titles),
                str(media_root),
                interval_seconds,
                operation_lock,
            )
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
        live_listener_runner=fake_live_listener_runner,
        reconcile_runner=fake_reconcile_runner,
        reconcile_startup_delay_seconds=0,
        telegram_client=fake_client,
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    # Both ingest tasks get the one process-local registry, and nothing else.
    assert calls == [
        (
            fake_client,
            {"Demo Group"},
            str((tmp_path / "media").resolve()),
            app.state.message_lock_registry,
        )
    ]
    assert reconcile_calls == [
        (
            fake_client,
            {"Demo Group"},
            str((tmp_path / "media").resolve()),
            300,
            app.state.message_lock_registry,
        )
    ]


def test_web_app_runs_first_reconcile_shortly_after_startup_by_default(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
        telegram_client=object(),
    )

    assert app.state.reconcile_startup_delay_seconds == 15


def test_web_app_starts_deepcoin_execution_reconcile_loop(tmp_path):
    calls: list[int] = []

    async def fake_deepcoin_reconcile_runner(**kwargs):
        calls.append(kwargs["interval_seconds"])

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_reconcile_runner=fake_deepcoin_reconcile_runner,
        deepcoin_reconcile_startup_delay_seconds=0,
        deepcoin_reconcile_interval_seconds=30,
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert calls == [30]


def test_monitor_status_reports_reconcile_auth_failure(tmp_path):
    async def fake_live_listener_runner(**kwargs):
        await asyncio.Event().wait()

    async def failing_reconcile_runner(**kwargs):
        raise RuntimeError(
            "The authorization key (session file) was used under two different IP addresses simultaneously"
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
        live_listener_runner=fake_live_listener_runner,
        reconcile_runner=failing_reconcile_runner,
        reconcile_startup_delay_seconds=0,
        telegram_client=object(),
    )

    with TestClient(app) as client:
        first = client.get("/api/monitor-status")
        second = client.get("/api/monitor-status")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["state"] == "disconnected"
    assert "authorization key" in second.json()["detail"].lower()
