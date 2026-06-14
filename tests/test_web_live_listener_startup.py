from fastapi.testclient import TestClient

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
    ):
        calls.append((client, set(target_titles), str(media_root), strategy_alert_config))

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

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert calls == [(fake_client, {"Demo Group"}, str((tmp_path / "media").resolve()), app.state.strategy_alert_config)]
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
