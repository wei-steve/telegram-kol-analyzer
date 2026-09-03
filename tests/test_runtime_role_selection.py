from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

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


@pytest.mark.parametrize(
    ("role", "owns_execution"),
    [("all", True), ("worker", True), ("ingest", False), ("web", False)],
)
def test_only_worker_and_all_own_recognition_execution_leases(
    tmp_path, role, owns_execution
):
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(
        database_path=tmp_path / f"{role}.db",
        runtime_role=role,
    )

    assert (app.state.recognition_execution_owner is not None) is owns_execution
    if owns_execution:
        assert app.state.recognition_execution_owner.runtime_role == role
    assert app.state.recognition_execution_schema_valid is False


@pytest.mark.parametrize("role", ["web", "ingest"])
def test_non_owner_roles_fail_closed_before_authoritative_processing(
    tmp_path, role
):
    from telegram_kol_research.web_app import (
        _run_authoritative_processor,
        create_web_app,
    )

    app = create_web_app(
        database_path=tmp_path / f"{role}-recognition.db",
        runtime_role=role,
    )

    with pytest.raises(RuntimeError, match="not_owned_by_runtime_role"):
        _run_authoritative_processor(app, raw_message_id=1)


def test_explicit_empty_env_file_paths_disable_checkout_secret_fallbacks(
    tmp_path, monkeypatch
):
    from telegram_kol_research.llm_chat import load_llm_proxy_config
    from telegram_kol_research.strategy_alerts import load_strategy_alert_config
    from telegram_kol_research.telegram_client import load_telegram_auth_config

    (tmp_path / ".env").write_text(
        "TELEGRAM_KOL_LLM_API_KEY=checkout-secret\n"
        "TELEGRAM_KOL_ALERT_BOT_TOKEN=checkout-alert-token\n"
        "TELEGRAM_KOL_ALERT_CHAT_ID=checkout-chat\n"
        "TELEGRAM_API_ID=123\n"
        "TELEGRAM_API_HASH=checkout-hash\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    isolated_environ = {"UNRELATED_SENTINEL": "1"}

    llm_config = load_llm_proxy_config(
        environ=isolated_environ,
        env_file_paths=[],
    )
    alert_config = load_strategy_alert_config(
        environ=isolated_environ,
        env_file_paths=[],
    )

    assert llm_config.api_key == ""
    assert alert_config.bot_token == ""
    assert alert_config.alert_chat_id == ""
    with pytest.raises(ValueError, match="TELEGRAM_API_ID is required"):
        load_telegram_auth_config(
            environ=isolated_environ,
            env_file_paths=[],
        )


def test_deepcoin_client_factory_accepts_environment_only_credentials():
    from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env

    client = build_deepcoin_client_from_env(
        environ={
            "DEEPCOIN_API_KEY": "process-key",
            "DEEPCOIN_API_SECRET": "process-secret",
            "DEEPCOIN_API_PASSPHRASE": "process-passphrase",
        },
        env_file_paths=[],
    )

    assert client._credentials.api_key == "process-key"
    assert client._credentials.api_secret == "process-secret"
    assert client._credentials.passphrase == "process-passphrase"


@pytest.mark.parametrize("role", ["ingest", "worker", "web"])
def test_split_runtime_app_loads_secrets_from_process_environment_only(
    role, tmp_path, monkeypatch
):
    from telegram_kol_research import web_app
    from telegram_kol_research.config import (
        MessageOperationSupervisorConfig,
        MultiTargetManagementConfig,
        RuntimeIncidentConfig,
    )
    from telegram_kol_research.llm_chat import LLMProxyConfig
    from telegram_kol_research.strategy_alerts import StrategyAlertConfig
    from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig

    calls = {}

    def record(name, value, **kwargs):
        calls[name] = kwargs
        return value

    monkeypatch.setattr(
        web_app,
        "load_llm_proxy_config",
        lambda **kwargs: record(
            "llm",
            LLMProxyConfig("http://127.0.0.1:8317", "", "model", 1.0),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        web_app,
        "load_strategy_alert_config",
        lambda **kwargs: record(
            "strategy_alert",
            StrategyAlertConfig(
                "http://127.0.0.1:8317", "", "model", 1.0, "", ""
            ),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        web_app,
        "load_system_operator_bot_config",
        lambda **kwargs: record(
            "system_bot", SystemOperatorBotConfig("", ""), **kwargs
        ),
    )
    monkeypatch.setattr(
        web_app,
        "load_notification_bot_config",
        lambda **kwargs: record(
            "notification_bot", SystemOperatorBotConfig("", ""), **kwargs
        ),
    )
    monkeypatch.setattr(
        web_app,
        "load_runtime_incident_config",
        lambda **kwargs: record("runtime_incident", RuntimeIncidentConfig(), **kwargs),
    )
    monkeypatch.setattr(
        web_app,
        "load_message_operation_supervisor_config",
        lambda **kwargs: record(
            "message_supervisor", MessageOperationSupervisorConfig(), **kwargs
        ),
    )
    monkeypatch.setattr(
        web_app,
        "load_multi_target_management_config",
        lambda **kwargs: record(
            "multi_target", MultiTargetManagementConfig(), **kwargs
        ),
    )
    deepcoin_client = object()
    monkeypatch.setattr(
        web_app,
        "build_deepcoin_client_from_env",
        lambda **kwargs: record("deepcoin", deepcoin_client, **kwargs),
    )

    app = web_app.create_web_app(
        database_path=tmp_path / f"{role}.db",
        runtime_role=role,
    )

    assert calls == {
        "llm": {"env_file_paths": []},
        "strategy_alert": {"env_file_paths": []},
        "system_bot": {"env_file_paths": []},
        "notification_bot": {"env_file_paths": []},
        "runtime_incident": {"environment_only": True},
        "message_supervisor": {"env_file_paths": []},
        "multi_target": {"env_file_paths": []},
    }
    assert app.state.deepcoin_client_factory() is deepcoin_client
    assert calls["deepcoin"] == {"env_file_paths": []}


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
    assert selector("web") == set()
    assert selector("worker") == {
        "authoritative_gap_recovery_loop",
        "break_even_convergence_worker",
        "contract_spec_refresh",
        "deepcoin_reconcile",
        "lifecycle_monitor",
        "message_operation_supervisor",
        "message_processing_worker",
        "position_snapshot_startup",
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


@pytest.mark.parametrize("role", ["ingest", "web"])
def test_non_worker_roles_never_start_message_processing_slots(role, tmp_path):
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.web_app import create_web_app

    runner_started = False

    async def forbidden_message_processing_runner(**_kwargs):
        nonlocal runner_started
        runner_started = True
        await asyncio.Event().wait()

    app = create_web_app(
        database_path=tmp_path / f"{role}-queue.db",
        runtime_role=role,
        message_processing_worker_runner=forbidden_message_processing_runner,
    )
    save_trading_settings(
        app.state.session_factory,
        {"message_pipeline_mode": "queue"},
    )

    with TestClient(app):
        time.sleep(0.02)
        assert runner_started is False
        assert app.state.message_processing_worker_task is None


def test_ingest_lock_registry_is_not_claimed_as_cross_process_worker_lock(
    tmp_path,
):
    from telegram_kol_research.trading_settings import save_trading_settings
    from telegram_kol_research.web_app import create_web_app

    started = asyncio.Event()
    captured_kwargs = {}

    async def capture_worker_boundary(**kwargs):
        captured_kwargs.update(kwargs)
        started.set()
        await asyncio.Event().wait()

    worker_app = create_web_app(
        database_path=tmp_path / "worker-observation.db",
        runtime_role="worker",
        message_processing_worker_runner=capture_worker_boundary,
    )
    save_trading_settings(
        worker_app.state.session_factory,
        {"message_pipeline_mode": "queue"},
    )

    with TestClient(worker_app) as client:
        assert started.is_set()
        assert captured_kwargs["activity"] is (
            worker_app.state.message_processing_activity
        )
        assert "message_lock_registry" not in captured_kwargs
        assert "message_lock_provider" not in captured_kwargs
        worker_snapshot = client.get("/api/runtime/loop-health").json()

    assert "active_shared_admissions" not in worker_snapshot
    assert "configured_max_parallel_chats" in worker_snapshot


@pytest.mark.parametrize("role", ["ingest", "web"])
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


def test_worker_keeps_the_shared_live_position_cache_fresh(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    calls = []

    class ReadOnlyClient:
        def list_positions(self):
            calls.append("positions")
            return []

    app = create_web_app(
        database_path=tmp_path / "worker.db",
        runtime_role="worker",
        deepcoin_client_factory=ReadOnlyClient,
        position_snapshot_refresh_seconds=0.01,
    )

    with TestClient(app):
        deadline = time.monotonic() + 0.25
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2
        assert app.state.position_snapshot_startup_task is not None
        assert not app.state.position_snapshot_startup_task.done()


def _runtime_unit_text(role: str) -> str:
    repository_root = Path(__file__).resolve().parents[1]
    return (
        repository_root / "deploy" / "systemd" / f"telegram-kol-{role}.service"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "unit_name",
    [
        "telegram-kol-monitor.service",
        "telegram-kol-monitor-diagnostic.service",
        "telegram-kol-monitor-test-notification.service",
    ],
)
def test_split_monitor_routes_incident_capture_to_worker(unit_name):
    repository_root = Path(__file__).resolve().parents[1]
    unit = (repository_root / "deploy" / "systemd" / unit_name).read_text(
        encoding="utf-8"
    )

    capture_path = "/api/runtime-incidents/monitor-capture"
    assert f"http://127.0.0.1:8002{capture_path}" in unit
    assert f"http://127.0.0.1:8000{capture_path}" not in unit
    assert (
        "--message-operation-coverage-url "
        "http://127.0.0.1:8002/api/runtime-incidents/message-operation-coverage"
    ) in unit
    assert (
        "--live-position-sizes-url "
        "http://127.0.0.1:8002/api/runtime-incidents/live-position-sizes"
    ) in unit
    assert (
        "--contract-spec-health-url "
        "http://127.0.0.1:8002/api/runtime-incidents/contract-spec-health"
    ) in unit


@pytest.mark.parametrize(
    ("role", "port"),
    [("ingest", 8001), ("worker", 8002), ("web", 8000)],
)
def test_split_runtime_units_select_one_role_and_one_loopback_port(role, port):
    unit = _runtime_unit_text(role)

    assert f"User=telegram-kol-{role}" in unit
    assert "Group=telegram-kol-runtime" in unit
    assert f"EnvironmentFile=/etc/telegram-kol-{role}.env" in unit
    assert f"Environment=TELEGRAM_KOL_RUNTIME_ROLE={role}" in unit
    assert f"--runtime-role {role}" in unit
    assert f"--host 127.0.0.1 --port {port}" in unit
    assert "ReadWritePaths=/opt/telegram-kol-analyzer/data" in unit


@pytest.mark.parametrize("role", ["ingest", "worker", "web"])
def test_split_runtime_units_preserve_the_proven_hardening_baseline(role):
    unit = _runtime_unit_text(role)

    for directive in (
        "AmbientCapabilities=",
        "CapabilityBoundingSet=",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "NoNewPrivileges=true",
        "PrivateDevices=true",
        "PrivateMounts=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "RestrictNamespaces=true",
        "RestrictSUIDSGID=true",
        "SystemCallFilter=@system-service",
        "SystemCallFilter=~@mount @privileged",
        "ReadOnlyPaths=/opt/telegram-kol-analyzer",
    ):
        assert directive in unit


def test_split_runtime_provisioning_grants_shared_configs_read_only_access():
    repository_root = Path(__file__).resolve().parents[1]
    deployment_guide = (repository_root / "docs" / "server-deployment.md").read_text(
        encoding="utf-8"
    )
    shared_configs = (
        "/opt/telegram-kol-analyzer/config/groups.yaml",
        "/opt/telegram-kol-analyzer/config/ai_recognition.yaml",
    )

    for shared_config in shared_configs:
        assert f"chgrp telegram-kol-runtime {shared_config}" in deployment_guide
        assert f"chmod 0640 {shared_config}" in deployment_guide
    assert (
        "chgrp -R telegram-kol-runtime /opt/telegram-kol-analyzer/config"
        not in deployment_guide
    )
    assert "chmod -R" not in deployment_guide


def test_worker_contract_cache_prepare_helper_has_a_fixed_narrow_target():
    repository_root = Path(__file__).resolve().parents[1]
    helper_path = (
        repository_root
        / "deploy"
        / "systemd"
        / "telegram-kol-worker-prepare-contract-cache"
    )

    helper = helper_path.read_text(encoding="utf-8")

    assert helper.startswith("#!/usr/bin/python3\n")
    assert (
        'CACHE_PATH = Path("/opt/telegram-kol-analyzer/data/'
        'deepcoin_contract_specs_cache.json")'
    ) in helper
    assert 'WORKER_USER = "telegram-kol-worker"' in helper
    assert 'RUNTIME_GROUP = "telegram-kol-runtime"' in helper
    assert 'AGENT_USER = "telegram-kol-agent"' in helper
    assert 'if arguments == ["--check"]:' in helper
    assert "converge_contract_cache_permissions(" in helper
    assert "inspect_contract_cache_permissions(" in helper
    assert "os.geteuid() != 0" in helper

    worker_unit = (
        repository_root / "deploy" / "systemd" / "telegram-kol-worker.service"
    ).read_text(encoding="utf-8")
    exec_start_pre = (
        "ExecStartPre=+/usr/local/libexec/"
        "telegram-kol-worker-prepare-contract-cache"
    )
    assert worker_unit.count(exec_start_pre) == 1
    assert worker_unit.index(exec_start_pre) < worker_unit.index("ExecStart=")


def test_runtime_agent_sanitizer_preserves_worker_owned_cache_classification():
    repository_root = Path(__file__).resolve().parents[1]
    helper = (
        repository_root
        / "deploy"
        / "systemd"
        / "telegram-kol-runtime-agent-prepare-db-acl"
    ).read_text(encoding="utf-8")
    installer = (
        repository_root / "scripts" / "install_runtime_agent_sidecar.sh"
    ).read_text(encoding="utf-8")

    assert "WORKER_OWNED_RUNTIME_FILES" in helper
    assert 'frozenset({"deepcoin_contract_specs_cache.json"})' in helper
    assert "ROOT_SHARED_RUNTIME_FILES" in helper
    assert "SHARED_RUNTIME_DATA_FILES" not in helper
    assert "converge_contract_cache_permissions(" in helper
    assert "worker_uid = pwd.getpwnam(WORKER_USER).pw_uid" in helper
    assert "name in WORKER_OWNED_RUNTIME_FILES" in helper
    assert "name in ROOT_SHARED_RUNTIME_FILES" in helper
    assert 'WORKER_USER="telegram-kol-worker"' in installer
    assert 'RUNTIME_GROUP="telegram-kol-runtime"' in installer
    assert "getfacl" in installer


def test_split_cutover_refreshes_generated_contract_cache_permissions():
    repository_root = Path(__file__).resolve().parents[1]
    deployment_guide = (repository_root / "docs" / "server-deployment.md").read_text(
        encoding="utf-8"
    )
    marker = "### Final split cutover permission refresh"
    cache_path = "/opt/telegram-kol-analyzer/data/deepcoin_contract_specs_cache.json"

    assert marker in deployment_guide
    cutover_section = deployment_guide.split(marker, 1)[1].split(
        "## Deepcoin contract-spec runtime configuration", 1
    )[0]
    normalized_cutover_section = " ".join(cutover_section.split())
    assert "after `telegram-kol.service` has stopped" in normalized_cutover_section
    assert "telegram-kol-worker-prepare-contract-cache" in cutover_section
    assert "--check" in cutover_section
    assert f"setfacl -b {cache_path}" not in cutover_section
    assert f"chgrp telegram-kol-runtime {cache_path}" not in cutover_section
    assert f"chmod 0660 {cache_path}" not in cutover_section
    for session_path in (
        "/opt/telegram-kol-analyzer/data/telegram.session",
        "/opt/telegram-kol-analyzer/data/telegram.session.lock",
    ):
        assert f"chgrp telegram-kol-runtime {session_path}" in cutover_section
        assert f"chmod 0660 {session_path}" in cutover_section
        assert (
            "setfacl -m u:telegram-kol-agent:---,g::rw-,m::rw- "
            f"{session_path}"
        ) in cutover_section


@pytest.mark.parametrize("role", ["worker", "web"])
def test_only_ingest_unit_can_reach_telegram_session_files(role):
    unit = _runtime_unit_text(role)

    for suffix in ("", ".lock", "-journal", "-wal", "-shm"):
        assert (
            "InaccessiblePaths=-/opt/telegram-kol-analyzer/data/telegram.session"
            f"{suffix}"
        ) in unit

    assert "telegram.session" not in _runtime_unit_text("ingest")


@pytest.mark.parametrize("role", ["ingest", "worker", "web"])
def test_split_units_cannot_fall_back_to_shared_checkout_secret_files(role):
    unit = _runtime_unit_text(role)

    for secret_path in (
        ".env",
        "config/telegram.env",
        "config/system_operator_bot.env",
        "config/llm.env",
        "config/runtime_incident_agent.env",
    ):
        assert (
            f"InaccessiblePaths=-/opt/telegram-kol-analyzer/{secret_path}" in unit
        )


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
    assert captured["live_listener_status_reason"] is None
    assert captured["live_listener_delegated"] is True


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
    monkeypatch.setattr(
        cli,
        "load_telegram_auth_config",
        lambda **kwargs: calls.append(("auth", kwargs)) or config,
    )
    deepcoin_calls = []

    class DeepcoinClient:
        def list_swap_instruments(self):
            deepcoin_calls.append("list_swap_instruments")
            return []

    monkeypatch.setattr(
        cli,
        "build_deepcoin_client_from_env",
        lambda **kwargs: deepcoin_calls.append(("build", kwargs)) or DeepcoinClient(),
    )
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
    assert calls == [
        ("auth", {"env_file_paths": []}),
        "reap",
        "lock",
        "lock_enter",
        "client",
        "lock_exit",
    ]
    authoritative_provider = (
        captured["deepcoin_contract_spec_provider"].authoritative_provider
    )
    assert authoritative_provider._instrument_loader() == []
    assert deepcoin_calls == [
        ("build", {"env_file_paths": []}),
        "list_swap_instruments",
    ]


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


def test_web_role_proxies_exchange_tab_to_worker_with_filtered_query(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    calls = []

    async def requester(url, *, params, timeout_seconds):
        calls.append((url, params, timeout_seconds))
        return httpx.Response(
            200,
            content=b'<section data-exchange-tab-loaded="true">worker-tab</section>',
            headers={"content-type": "text/html; charset=utf-8"},
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        worker_exchange_tab_base_url="http://127.0.0.1:8002",
        worker_exchange_tab_requester=requester,
    )

    response = TestClient(app).get(
        "/positions-panel/tabs/order-history",
        params={
            "browse_token": "token-1",
            "cursor": "cursor-1",
            "closed_after": "2026-08-01",
            "closed_before": "2026-08-31",
            "unknown": "must-not-cross-process",
        },
    )

    assert response.status_code == 200
    assert response.text == (
        '<section data-exchange-tab-loaded="true">worker-tab</section>'
    )
    assert calls == [
        (
            "http://127.0.0.1:8002/positions-panel/tabs/order-history",
            {
                "browse_token": "token-1",
                "cursor": "cursor-1",
                "closed_after": "2026-08-01",
                "closed_before": "2026-08-31",
            },
            20,
        )
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8002",
        "http://example.com:8002",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002/path",
        "http://127.0.0.1:8002?query=1",
        "http://user:pass@127.0.0.1:8002",
    ],
)
def test_worker_exchange_tab_base_url_rejects_non_loopback_8002(value):
    from telegram_kol_research import web_app

    resolver = getattr(web_app, "resolve_worker_exchange_tab_base_url", None)
    assert resolver is not None, "worker exchange tab URL resolver is missing"
    with pytest.raises(ValueError, match="worker exchange tab"):
        resolver(value)


def test_worker_exchange_tab_proxy_rejects_oversized_response():
    from telegram_kol_research import web_app

    proxy = getattr(web_app, "proxy_worker_exchange_tab_once", None)
    assert proxy is not None, "worker exchange tab proxy is missing"

    async def requester(url, *, params, timeout_seconds):
        return httpx.Response(
            200,
            content=b"x" * (4 * 1024 * 1024 + 1),
            headers={"content-type": "text/html"},
        )

    response = asyncio.run(
        proxy(
            requester=requester,
            base_url="http://127.0.0.1:8002",
            tab_name="open-orders",
            params={},
        )
    )

    assert response is None


def test_web_exchange_tab_failure_falls_back_without_deepcoin_client(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    attempts = 0
    factory_calls = []

    async def requester(url, *, params, timeout_seconds):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("worker unavailable")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        worker_exchange_tab_requester=requester,
        deepcoin_client_factory=lambda: factory_calls.append("called"),
    )

    response = TestClient(app).get("/positions-panel/tabs/open-orders")

    assert response.status_code == 200
    assert attempts == 1
    assert factory_calls == []
    assert 'data-exchange-tab-loaded="false"' in response.text
    assert "Deepcoin 数据暂不可用" in response.text


def test_web_monitor_status_proxies_ingest_state(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    calls = []

    async def requester(url, *, timeout_seconds):
        calls.append((url, timeout_seconds))
        return httpx.Response(
            200,
            json={
                "state": "monitoring",
                "label": "监控中",
                "detail": "Telegram 正在同步监听 2 个启用群组",
                "monitored_group_count": 2,
            },
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        live_target_titles={"one", "two"},
        ingest_monitor_status_requester=requester,
    )

    response = TestClient(app).get("/api/monitor-status")

    assert response.status_code == 200
    assert response.json() == {
        "state": "monitoring",
        "label": "监控中",
        "detail": "Telegram 正在同步监听 2 个启用群组",
        "monitored_group_count": 2,
    }
    assert calls == [("http://127.0.0.1:8001/api/monitor-status", 5)]


def test_web_monitor_status_returns_unknown_when_ingest_unavailable(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    attempts = 0

    async def requester(url, *, timeout_seconds):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("ingest unavailable")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        live_target_titles={"one", "two"},
        ingest_monitor_status_requester=requester,
    )

    response = TestClient(app).get("/api/monitor-status")

    assert response.status_code == 200
    assert attempts == 1
    assert response.json() == {
        "state": "unknown",
        "label": "状态未知",
        "detail": "无法从 ingest 获取 Telegram 监听状态",
        "monitored_group_count": 2,
    }


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8001/api/monitor-status",
        "http://example.com:8001/api/monitor-status",
        "http://127.0.0.1:8002/api/monitor-status",
        "http://127.0.0.1:8001/api/refresh",
        "http://127.0.0.1:8001/api/monitor-status?query=1",
        "http://user:pass@127.0.0.1:8001/api/monitor-status",
    ],
)
def test_ingest_monitor_status_url_rejects_non_loopback_8001_path(value):
    from telegram_kol_research import web_app

    resolver = getattr(web_app, "resolve_ingest_monitor_status_url", None)
    assert resolver is not None, "ingest monitor status URL resolver is missing"
    with pytest.raises(ValueError, match="ingest monitor status"):
        resolver(value)


def test_ingest_monitor_status_proxy_rejects_invalid_payload():
    from telegram_kol_research import web_app

    proxy = getattr(web_app, "proxy_ingest_monitor_status_once", None)
    assert proxy is not None, "ingest monitor status proxy is missing"

    async def requester(url, *, timeout_seconds):
        return httpx.Response(200, json={"state": "monitoring"})

    status = asyncio.run(
        proxy(
            requester=requester,
            url="http://127.0.0.1:8001/api/monitor-status",
        )
    )

    assert status is None


def test_web_synchronous_monitor_badge_starts_unknown(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    async def forbidden_requester(url, *, timeout_seconds):
        raise AssertionError("server-side rendering must not call ingest")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        live_target_titles={"one"},
        ingest_monitor_status_requester=forbidden_requester,
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert 'data-monitor-state="unknown"' in response.text
    assert "状态未知" in response.text
    assert "is-unknown" in response.text


def test_monitor_badge_is_updated_only_from_monitor_status_endpoint():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "telegram_kol_research"
        / "static"
        / "app.js"
    )
    script = script_path.read_text(encoding="utf-8")
    live_updates = script.split("function connectLiveUpdates()", 1)[1].split(
        "function startPollingUpdates()", 1
    )[0]
    freshness = script.split("async function refreshFromDatabaseChanges()", 1)[
        1
    ].split("function scheduleRecoveryRefresh()", 1)[0]
    monitor_refresh = script.split("async function refreshMonitorStatus()", 1)[
        1
    ].split("function getMessagePanel()", 1)[0]

    assert "setMonitorStatus(" not in live_updates
    assert "await refreshMonitorStatus();" in live_updates
    assert "setMonitorStatus(" not in freshness
    assert "state: 'unknown'" in monitor_refresh
    assert "label: '状态未知'" in monitor_refresh


def test_delegated_listener_hides_internal_reason_from_user_templates(tmp_path):
    from telegram_kol_research.web_app import create_web_app

    internal_reason = "Telegram live listener is owned by ingest"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        live_listener_status_reason=internal_reason,
        live_listener_delegated=True,
    )

    root_response = TestClient(app).get("/")
    messages_response = TestClient(app).get("/groups/1/messages")

    assert root_response.status_code == 200
    assert messages_response.status_code == 200
    assert internal_reason not in root_response.text
    assert internal_reason not in messages_response.text
    assert "session-lock-banner" not in root_response.text


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
