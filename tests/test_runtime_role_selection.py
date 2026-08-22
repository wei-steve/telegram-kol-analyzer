from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from typer.testing import CliRunner


def _refresh_endpoint(app):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/refresh"
        and "POST" in getattr(route, "methods", set())
    )


@pytest.mark.parametrize("role", ["all", "ingest", "worker", "web"])
def test_runtime_role_selector_accepts_only_the_closed_role_set(role):
    from telegram_kol_research import web_app

    resolver = getattr(web_app, "resolve_runtime_role", None)
    assert resolver is not None, "runtime role selector is not implemented"
    assert resolver(role) == role


def test_runtime_role_selector_rejects_unknown_role():
    from telegram_kol_research import web_app

    resolver = getattr(web_app, "resolve_runtime_role", None)
    assert resolver is not None, "runtime role selector is not implemented"
    with pytest.raises(ValueError, match="runtime role"):
        resolver("combined")


@pytest.mark.parametrize(
    ("role", "owns_session"),
    [("all", True), ("ingest", True), ("worker", False), ("web", False)],
)
def test_only_all_and_ingest_roles_own_the_telegram_session(role, owns_session):
    from telegram_kol_research import web_app

    predicate = getattr(web_app, "runtime_role_owns_telegram_session", None)
    assert predicate is not None, "Telegram session role ownership is not implemented"
    assert predicate(role) is owns_session


def test_create_web_app_defaults_to_the_existing_all_role(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(database_path=tmp_path / "research.db")

    assert app.state.runtime_role == "all"


def test_split_runtime_roles_partition_the_existing_singleton_task_set():
    from telegram_kol_research import web_app

    selector = getattr(web_app, "runtime_role_singleton_tasks", None)
    assert selector is not None, "runtime task partition is not implemented"

    all_tasks = selector("all")
    split_tasks = {
        role: selector(role)
        for role in ("ingest", "worker", "web")
    }

    assert all(split_tasks[role] < all_tasks for role in split_tasks)
    assert set.union(*split_tasks.values()) == all_tasks
    assert split_tasks["ingest"].isdisjoint(split_tasks["worker"])
    assert split_tasks["ingest"].isdisjoint(split_tasks["web"])
    assert split_tasks["worker"].isdisjoint(split_tasks["web"])


def test_runtime_role_partition_preserves_the_phase_6_responsibility_boundary():
    from telegram_kol_research import web_app

    selector = getattr(web_app, "runtime_role_singleton_tasks", None)
    assert selector is not None, "runtime task partition is not implemented"

    assert selector("ingest") == {"live_listener", "reconcile"}
    assert selector("web") == {"position_snapshot_startup"}
    assert selector("worker") == {
        "authoritative_gap_recovery_loop",
        "break_even_convergence_worker",
        "contract_spec_refresh",
        "deepcoin_reconcile",
        "lifecycle_monitor",
        "message_operation_supervisor",
        "message_processing_worker",
        "runtime_incident_notification",
        "semantic_review",
        "source_message_deletion_worker",
        "strategy_management_notification",
        "strategy_management_worker",
        "system_operator_bot_command",
        "telegram_bot_command",
        "worker_command_worker",
    }


@pytest.mark.parametrize("role", ["all", "ingest", "worker", "web"])
def test_loop_lag_monitor_is_process_local_instrumentation(role):
    from telegram_kol_research import web_app

    predicate = getattr(web_app, "runtime_role_starts_process_monitor", None)
    assert predicate is not None, "process-local monitor selection is not implemented"
    assert predicate(role, "loop_lag_monitor") is True


@pytest.mark.parametrize("role", ["ingest", "web"])
def test_non_worker_lifespans_do_not_start_worker_singletons(role, tmp_path):
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(
        database_path=tmp_path / f"{role}.db",
        runtime_role=role,
    )

    with TestClient(app):
        assert app.state.lifecycle_monitor_task is None
        assert app.state.authoritative_gap_recovery_loop_task is None
        assert app.state.deepcoin_reconcile_task is None
        assert app.state.strategy_management_worker_task is None
        assert app.state.break_even_convergence_worker_task is None
        assert app.state.source_message_deletion_worker_task is None
        assert app.state.worker_command_worker_task is None
        assert app.state.semantic_review_task is None


@pytest.mark.parametrize("role", ["ingest", "worker"])
def test_non_web_lifespans_do_not_start_web_singletons(role, tmp_path):
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(
        database_path=tmp_path / f"{role}.db",
        runtime_role=role,
    )

    with TestClient(app):
        assert app.state.position_snapshot_startup_task is None


@pytest.mark.parametrize("role", ["all", "ingest", "worker", "web"])
def test_every_runtime_lifespan_starts_its_process_local_loop_monitor(role, tmp_path):
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(
        database_path=tmp_path / f"{role}.db",
        runtime_role=role,
    )

    with TestClient(app):
        assert app.state.loop_lag_monitor_task is not None


@pytest.mark.parametrize("role", ["web", "worker"])
def test_non_ingest_cli_roles_never_load_or_lock_telegram(role, tmp_path, monkeypatch):
    from telegram_kol_research import cli

    config_path = tmp_path / "groups.yaml"
    config_path.write_text("groups: []\n", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(
        cli,
        "load_telegram_auth_config",
        lambda: (_ for _ in ()).throw(
            AssertionError(f"{role} must not load Telegram auth")
        ),
    )
    monkeypatch.setattr(
        cli,
        "acquire_telegram_session_lock",
        lambda path: (_ for _ in ()).throw(
            AssertionError(f"{role} must not acquire the Telegram session lock")
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_telegram_client",
        lambda config: (_ for _ in ()).throw(
            AssertionError(f"{role} must not create a Telegram client")
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_web_app",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(
        cli,
        "_build_web_server",
        lambda app_instance, host, port: type(
            "Server", (), {"run": lambda self: None}
        )(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "web",
            "--runtime-role",
            role,
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["runtime_role"] == role
    assert captured["telegram_client"] is None


def test_ingest_cli_role_acquires_session_before_creating_client(
    tmp_path, monkeypatch
):
    from telegram_kol_research import cli
    from telegram_kol_research.telegram_client import TelegramAuthConfig

    config_path = tmp_path / "groups.yaml"
    config_path.write_text("groups: []\n", encoding="utf-8")
    session_path = tmp_path / "telegram.session"
    calls = []
    captured = {}

    class Lock:
        def __enter__(self):
            calls.append("lock_enter")
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls.append("lock_exit")
            return False

    config = TelegramAuthConfig(api_id=1, api_hash="hash", session_path=session_path)
    client = object()
    monkeypatch.setattr(cli, "load_telegram_auth_config", lambda: calls.append("auth") or config)
    monkeypatch.setattr(
        cli,
        "reap_stopped_session_lock_owner",
        lambda path, current_command=None: calls.append("reap") or False,
    )
    monkeypatch.setattr(
        cli,
        "acquire_telegram_session_lock",
        lambda path: calls.append("lock") or Lock(),
    )
    monkeypatch.setattr(
        cli,
        "create_telegram_client",
        lambda auth: calls.append("client") or client,
    )
    monkeypatch.setattr(
        cli,
        "create_web_app",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(
        cli,
        "_build_web_server",
        lambda app_instance, host, port: type(
            "Server", (), {"run": lambda self: None}
        )(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "web",
            "--runtime-role",
            "ingest",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["runtime_role"] == "ingest"
    assert captured["telegram_client"] is client
    assert calls == ["auth", "reap", "lock", "lock_enter", "client", "lock_exit"]


def test_web_role_proxies_refresh_once_and_preserves_success_body(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    calls = []

    async def requester(url, *, timeout_seconds):
        calls.append((url, timeout_seconds))
        return httpx.Response(200, json={"checked": 7, "reconciled": 3})

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        ingest_refresh_url="http://127.0.0.1:8001/api/refresh",
        ingest_refresh_requester=requester,
    )

    response = asyncio.run(_refresh_endpoint(app)())

    assert response.status_code == 200
    assert json.loads(response.body) == {"checked": 7, "reconciled": 3}
    assert calls == [("http://127.0.0.1:8001/api/refresh", 180)]


def test_web_role_preserves_ingest_refresh_error_status_and_json(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    async def requester(url, *, timeout_seconds):
        return httpx.Response(
            409,
            json={"detail": {"code": "telegram_session_busy", "owner_pid": 42}},
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        ingest_refresh_requester=requester,
    )

    response = asyncio.run(_refresh_endpoint(app)())

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "detail": {"code": "telegram_session_busy", "owner_pid": 42}
    }


def test_web_role_does_not_retry_an_unknown_ingest_refresh_failure(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    attempts = 0

    async def requester(url, *, timeout_seconds):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("lost ingest response")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        ingest_refresh_requester=requester,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_refresh_endpoint(app)())

    assert attempts == 1
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "ingest_refresh_unavailable",
        "outcome": "unknown",
    }


def test_worker_role_rejects_refresh_without_calling_ingest(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    async def requester(url, *, timeout_seconds):
        raise AssertionError("worker must not proxy Telegram refresh")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="worker",
        ingest_refresh_requester=requester,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_refresh_endpoint(app)())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {"code": "refresh_not_owned_by_runtime_role"}


def test_ingest_refresh_rpc_url_must_be_localhost(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    with pytest.raises(ValueError, match="localhost"):
        create_web_app(
            database_path=tmp_path / "research.db",
            runtime_role="web",
            ingest_refresh_url="https://example.com/api/refresh",
        )


def test_web_cli_passes_the_configured_ingest_refresh_url(tmp_path, monkeypatch):
    from telegram_kol_research import cli

    config_path = tmp_path / "groups.yaml"
    config_path.write_text("groups: []\n", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        cli,
        "create_web_app",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(
        cli,
        "_build_web_server",
        lambda app_instance, host, port: type(
            "Server", (), {"run": lambda self: None}
        )(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "web",
            "--runtime-role",
            "web",
            "--ingest-refresh-url",
            "http://localhost:8124/api/refresh",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["ingest_refresh_url"] == "http://localhost:8124/api/refresh"


def test_ingest_role_executes_the_existing_local_refresh_path(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    calls = []

    class Client:
        async def connect(self):
            calls.append("connect")

    async def local_reconcile(**kwargs):
        calls.append("reconcile")
        return {"checked": 2, "reconciled": 1}

    async def requester(url, *, timeout_seconds):
        raise AssertionError("ingest must execute locally, not proxy")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="ingest",
        telegram_client=Client(),
        ingest_refresh_requester=requester,
    )
    app.state.reconcile_once_runner = local_reconcile

    result = asyncio.run(_refresh_endpoint(app)())

    assert result == {"checked": 2, "reconciled": 1}
    assert calls == ["connect", "reconcile"]
