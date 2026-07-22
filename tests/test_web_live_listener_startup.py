import asyncio

from fastapi.testclient import TestClient

from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.web_app import create_web_app


def test_web_app_starts_live_listener_when_targets_are_configured(tmp_path):
    calls: list[tuple[object, set[str], str]] = []
    reconcile_calls: list[tuple[object, set[str], str, int]] = []
    fake_client = object()

    async def fake_live_listener_runner(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        strategy_alert_config=None,
        strategy_alert_enabled_for_title=None,
        system_operator_bot_config=None,
    ):
        calls.append((client, set(target_titles), str(media_root), strategy_alert_config, system_operator_bot_config))

    async def fake_reconcile_runner(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        interval_seconds,
        operation_lock=None,
        strategy_alert_config=None,
        strategy_alert_enabled_for_title=None,
    ):
        reconcile_calls.append((client, set(target_titles), str(media_root), interval_seconds))

    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
        live_listener_runner=fake_live_listener_runner,
        reconcile_runner=fake_reconcile_runner,
        reconcile_startup_delay_seconds=0,
        telegram_client=fake_client,
    )
    app.state.strategy_alert_config = object()
    app.state.system_operator_bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    app.state.notification_bot_config = SystemOperatorBotConfig(
        bot_token="notification-token",
        chat_id="system-chat",
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert calls == [
        (
            fake_client,
            {"Demo Group"},
            str((tmp_path / "media").resolve()),
            app.state.strategy_alert_config,
            app.state.notification_bot_config,
        )
    ]
    assert reconcile_calls == [
        (fake_client, {"Demo Group"}, str((tmp_path / "media").resolve()), 300)
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
