import asyncio
from datetime import timedelta
from types import SimpleNamespace

from typer.testing import CliRunner
from pathlib import Path

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.cli import app
from telegram_kol_research import cli as cli_module
from telegram_kol_research import deepcoin_contract_specs as contract_specs_module


def test_web_command_is_available_in_help():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "web" in result.stdout
    assert "alerts" in result.stdout


def test_web_server_closes_live_update_streams_before_uvicorn_shutdown(monkeypatch):
    shutdown_order = []
    broker = cli_module.LiveUpdateBroker()
    stream = broker.stream()

    class FakeConfig:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeServer:
        def __init__(self, config):
            self.config = config

        async def shutdown(self, sockets=None):
            try:
                await anext(stream)
            except StopAsyncIteration:
                shutdown_order.append("live_updates_closed")
            else:
                raise AssertionError("live update stream remained open")
            shutdown_order.append("uvicorn_shutdown")

    monkeypatch.setattr("uvicorn.Config", FakeConfig)
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    app_instance = SimpleNamespace(
        state=SimpleNamespace(live_update_broker=broker)
    )

    async def run_shutdown():
        assert await anext(stream) == ": keep-alive\n\n"
        server = cli_module._build_web_server(
            app_instance,
            host="127.0.0.1",
            port=8123,
        )
        await server.shutdown()
        return server

    server = asyncio.run(run_shutdown())

    assert shutdown_order == ["live_updates_closed", "uvicorn_shutdown"]
    assert server.config.kwargs["timeout_graceful_shutdown"] == 10


def test_web_command_starts_app_for_semantic_review_without_telegram_credentials(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        """
groups:
  - chat_title: Demo Group
    enabled: true
  - chat_title: Ignored Group
    enabled: false
""".strip(),
        encoding="utf-8",
    )

    captured = {}

    def fake_create_web_app(*, database_path, live_target_titles, runtime_role="all", media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300, group_config=None, group_config_path=None, deepcoin_contract_spec_provider=None):
        captured["database_path"] = Path(database_path)
        captured["live_target_titles"] = set(live_target_titles)
        captured["telegram_client"] = telegram_client
        captured["live_listener_status_reason"] = live_listener_status_reason
        captured["group_labels_by_title"] = dict(group_labels_by_title or {})
        captured["group_config"] = group_config
        captured["group_config_path"] = group_config_path
        captured["deepcoin_contract_spec_provider"] = deepcoin_contract_spec_provider
        return object()

    def fake_build_web_server(app_instance, *, host, port):
        captured["app_instance"] = app_instance
        captured["host"] = host
        captured["port"] = port
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_web_server",
        fake_build_web_server,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: (_ for _ in ()).throw(ValueError("TELEGRAM_API_ID is required")),
    )

    result = CliRunner().invoke(
        app,
        [
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
            "--deepcoin-contract-specs-path",
            str(tmp_path / "missing_deepcoin_contract_specs.yaml"),
        ],
    )

    assert result.exit_code == 0
    assert captured["live_target_titles"] == {"Demo Group"}
    assert captured["group_labels_by_title"] == {"Demo Group": "Demo Group"}
    assert captured["group_config"].groups[0].chat_title == "Demo Group"
    assert captured["deepcoin_contract_spec_provider"].get_contract_spec("BTC-USDT-SWAP") is None
    assert captured["port"] == 8123
    assert captured["live_listener_status_reason"] == "缺少 Telegram API 凭据或 Telethon 运行依赖"


def test_web_command_loads_deepcoin_contract_specs_when_config_exists(tmp_path, monkeypatch):
    config_path = tmp_path / "groups.yaml"
    spec_path = tmp_path / "deepcoin_contract_specs.yaml"
    config_path.write_text("groups: []", encoding="utf-8")
    spec_path.write_text(
        """
contracts:
  - instrument_id: BTC-USDT-SWAP
    contract_value: 0.001
    quantity_step: 1
    min_quantity: 1
    price_tick: 0.1
""".strip(),
        encoding="utf-8",
    )
    captured = {}

    def fake_create_web_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_web_server",
        lambda app_instance, host, port: SimpleNamespace(run=lambda: None),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: (_ for _ in ()).throw(ValueError("TELEGRAM_API_ID is required")),
    )

    result = CliRunner().invoke(
        app,
        [
            "web",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
            "--deepcoin-contract-specs-path",
            str(spec_path),
        ],
    )

    provider = captured["deepcoin_contract_spec_provider"]
    assert result.exit_code == 0
    assert provider.get_contract_spec("BTC-USDT-SWAP") == DeepcoinContractSpec(
        instrument_id="BTC-USDT-SWAP",
        contract_value=0.001,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.1,
    )


def test_web_command_composes_rollout_provider_with_process_cache_controls(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "groups.yaml"
    spec_path = tmp_path / "deepcoin_contract_specs.yaml"
    cache_path = tmp_path / "runtime" / "deepcoin-contract-specs.json"
    config_path.write_text("groups: []", encoding="utf-8")
    spec_path.write_text(
        """
contracts:
  - instrument_id: BTC-USDT-SWAP
    contract_value: 0.001
    quantity_step: 1
    min_quantity: 1
    price_tick: 0.1
""".strip(),
        encoding="utf-8",
    )
    captured = {}

    def fake_create_web_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app)
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_web_server",
        lambda app_instance, host, port: SimpleNamespace(run=lambda: None),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: (_ for _ in ()).throw(ValueError("missing")),
    )

    result = CliRunner().invoke(
        app,
        [
            "web",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
            "--deepcoin-contract-specs-path",
            str(spec_path),
            "--deepcoin-contract-specs-cache-path",
            str(cache_path),
            "--deepcoin-contract-specs-ttl-hours",
            "12",
        ],
    )

    provider = captured["deepcoin_contract_spec_provider"]
    assert result.exit_code == 0
    assert type(provider).__name__ == "RolloutDeepcoinContractSpecProvider"
    assert provider.authoritative_provider.cache_path == cache_path
    assert provider.authoritative_provider.ttl == timedelta(hours=12)
    # The persisted default is static, so the reviewed YAML remains authoritative.
    assert provider.mode == "static"
    assert provider.get_contract_spec("BTC-USDT-SWAP").contract_value == 0.001


def test_web_command_rejects_non_positive_contract_spec_ttl(tmp_path):
    result = CliRunner().invoke(
        app,
        ["web", "--deepcoin-contract-specs-ttl-hours", "0"],
    )

    assert result.exit_code != 0
    assert "deepcoin-contract-specs-ttl-hours" in result.output


def test_contract_spec_rollout_modes_preserve_authority_and_compare_in_shadow():
    provider_class = getattr(
        contract_specs_module,
        "RolloutDeepcoinContractSpecProvider",
        None,
    )
    assert provider_class is not None

    static_spec = DeepcoinContractSpec("BTC-USDT-SWAP", 0.001, 1, 1, 0.1)
    dynamic_spec = DeepcoinContractSpec("BTC-USDT-SWAP", 0.01, 1, 1, 0.1)
    static_provider = SimpleNamespace(
        get_contract_spec=lambda instrument_id: static_spec,
    )

    class AuthoritativeProvider:
        cache_path = Path("cache.json")
        ttl = timedelta(hours=24)

        def get_contract_spec(self, instrument_id):
            return dynamic_spec

    mode = {"value": "static"}
    provider = provider_class(
        static_provider=static_provider,
        authoritative_provider=AuthoritativeProvider(),
        mode_loader=lambda: mode["value"],
    )

    assert provider.get_contract_spec("BTC-USDT-SWAP") is static_spec
    assert provider.last_comparison is None

    mode["value"] = "shadow"
    assert provider.get_contract_spec("BTC-USDT-SWAP") is static_spec
    assert provider.last_comparison.matches is False
    assert provider.last_comparison.instrument_id == "BTC-USDT-SWAP"

    mode["value"] = "live"
    assert provider.get_contract_spec("BTC-USDT-SWAP") is dynamic_spec


def test_contract_spec_rollout_live_never_falls_back_to_static():
    provider_class = getattr(
        contract_specs_module,
        "RolloutDeepcoinContractSpecProvider",
        None,
    )
    assert provider_class is not None
    static_spec = DeepcoinContractSpec("SOL-USDT-SWAP", 1, 1, 1, 0.001)
    provider = provider_class(
        static_provider=SimpleNamespace(
            get_contract_spec=lambda instrument_id: static_spec,
        ),
        authoritative_provider=SimpleNamespace(
            get_contract_spec=lambda instrument_id: None,
        ),
        mode_loader=lambda: "live",
    )

    assert provider.get_contract_spec("SOL-USDT-SWAP") is None


def test_contract_spec_shadow_comparison_failure_cannot_change_static_execution():
    provider_class = getattr(
        contract_specs_module,
        "RolloutDeepcoinContractSpecProvider",
        None,
    )
    assert provider_class is not None
    static_spec = DeepcoinContractSpec("BTC-USDT-SWAP", 0.001, 1, 1, 0.1)

    def fail_comparison(instrument_id):
        raise RuntimeError("signed request details must not escape")

    provider = provider_class(
        static_provider=SimpleNamespace(
            get_contract_spec=lambda instrument_id: static_spec,
        ),
        authoritative_provider=SimpleNamespace(
            get_contract_spec=fail_comparison,
        ),
        mode_loader=lambda: "shadow",
    )

    assert provider.get_contract_spec("BTC-USDT-SWAP") is static_spec
    assert provider.last_comparison.matches is False
    assert provider.last_comparison.error == "shadow_compare_failed:RuntimeError"


def test_web_command_does_not_create_live_client_when_session_lock_is_busy(
    tmp_path, monkeypatch
):
    from telegram_kol_research.telegram_client import TelegramAuthConfig
    from telegram_kol_research.telegram_session_lock import TelegramSessionLockError

    config_path = tmp_path / "groups.yaml"
    session_path = tmp_path / "telegram.session"
    config_path.write_text(
        """
groups:
  - chat_title: Demo Group
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    captured = {}

    def fake_create_web_app(*, database_path, live_target_titles, runtime_role="all", media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300, group_config=None, group_config_path=None, deepcoin_contract_spec_provider=None):
        captured["telegram_client"] = telegram_client
        captured["live_listener_status_reason"] = live_listener_status_reason
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_web_server",
        lambda app_instance, host, port: SimpleNamespace(run=lambda: None),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=session_path,
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.acquire_telegram_session_lock",
        lambda path: (_ for _ in ()).throw(
            TelegramSessionLockError(f"{path} is already in use")
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: (_ for _ in ()).throw(
            AssertionError("web must not create a Telegram client when lock is busy")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "web",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["telegram_client"] is None
    assert "already in use" in captured["live_listener_status_reason"]


def test_web_command_reaps_stopped_session_owner_before_creating_client(
    tmp_path, monkeypatch
):
    from telegram_kol_research.telegram_client import TelegramAuthConfig

    config_path = tmp_path / "groups.yaml"
    session_path = tmp_path / "telegram.session"
    config_path.write_text(
        """
groups:
  - chat_title: Demo Group
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    calls = []

    def fake_create_web_app(*, database_path, live_target_titles, runtime_role="all", media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300, group_config=None, group_config_path=None, deepcoin_contract_spec_provider=None):
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr(
        "telegram_kol_research.cli._build_web_server",
        lambda app_instance, host, port: SimpleNamespace(run=lambda: None),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=session_path,
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.reap_stopped_session_lock_owner",
        lambda path, current_command=None: calls.append(("reap", path, current_command)) or True,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.acquire_telegram_session_lock",
        lambda path: calls.append(("lock", path)) or _NoopLock(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: calls.append(("client", auth_config.session_path)) or object(),
    )

    result = CliRunner().invoke(
        app,
        [
            "web",
            "--database-path",
            str(tmp_path / "research.db"),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert calls[:3] == [
        ("reap", session_path, "telegram-kol-research web"),
        ("lock", session_path),
        ("client", session_path),
    ]


def test_alerts_command_starts_live_listener_with_strategy_config(tmp_path, monkeypatch):
    from telegram_kol_research.telegram_client import TelegramAuthConfig
    from telegram_kol_research.strategy_alerts import StrategyAlertConfig

    config_path = tmp_path / "groups.yaml"
    session_path = tmp_path / "telegram.session"
    database_path = tmp_path / "research.db"
    config_path.write_text(
        """
groups:
  - chat_title: Demo Group
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    captured = {}

    class NoopLock:
        def __enter__(self):
            captured["lock_entered"] = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            captured["lock_exited"] = True
            return False

    async def fake_runner(
        *,
        client,
        session_factory,
        broker,
        target_titles,
        media_root,
        strategy_alert_config=None,
        strategy_alert_enabled_for_title=None,
        ai_recognition_config_path=None,
        authoritative_processor=None,
        system_operator_bot_config=None,
        notification_bot_config=None,
    ):
        captured["client"] = client
        captured["target_titles"] = set(target_titles)
        captured["strategy_alert_config"] = strategy_alert_config
        captured["ai_recognition_config_path"] = ai_recognition_config_path
        captured["authoritative_processor"] = authoritative_processor
        captured["system_operator_bot_config"] = system_operator_bot_config
        captured["notification_bot_config"] = notification_bot_config

    fake_client = object()
    fake_alert_config = StrategyAlertConfig(
        llm_base_url="http://llm.test",
        llm_api_key="key",
        llm_model="cheap",
        timeout_seconds=5,
        bot_token="bot-token",
        alert_chat_id="123",
    )

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=session_path,
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.acquire_telegram_session_lock",
        lambda path: NoopLock(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: fake_client,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_strategy_alert_config",
        lambda: fake_alert_config,
    )
    operator_config = object()
    notification_config = object()
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_system_operator_bot_config",
        lambda: operator_config,
        raising=False,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_notification_bot_config",
        lambda: notification_config,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_live_listener",
        fake_runner,
    )

    result = CliRunner().invoke(
        app,
        [
            "alerts",
            "--database-path",
            str(database_path),
            "--config-path",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["target_titles"] == {"Demo Group"}
    assert captured["strategy_alert_config"] is fake_alert_config
    assert captured["ai_recognition_config_path"].name == "ai_recognition.yaml"
    assert callable(captured["authoritative_processor"])
    assert captured["system_operator_bot_config"] is operator_config
    assert captured["notification_bot_config"] is notification_config
    assert captured["lock_entered"] is True
    assert captured["lock_exited"] is True


def test_session_status_command_shows_lock_owner(tmp_path, monkeypatch):
    from telegram_kol_research.telegram_client import TelegramAuthConfig
    from telegram_kol_research.telegram_session_lock import TelegramSessionLockOwner

    session_path = tmp_path / "telegram.session"
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=session_path,
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.describe_session_lock_owner",
        lambda path: TelegramSessionLockOwner(
            pid=12345,
            status="S",
            command="/repo/.venv/bin/telegram-kol-research web",
        ),
    )

    result = CliRunner().invoke(app, ["session-status"])

    assert result.exit_code == 0
    assert "Telegram session owner" in result.stdout
    assert "pid=12345" in result.stdout
    assert "telegram-kol-research web" in result.stdout


def test_session_release_command_requires_matching_pid(tmp_path, monkeypatch):
    from telegram_kol_research.telegram_client import TelegramAuthConfig
    from telegram_kol_research.telegram_session_lock import TelegramSessionLockOwner

    session_path = tmp_path / "telegram.session"
    calls = []
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=session_path,
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.release_session_lock_owner",
        lambda path, expected_pid, current_command=None: calls.append(
            (path, expected_pid, current_command)
        )
        or TelegramSessionLockOwner(
            pid=12345,
            status="S",
            command="/repo/.venv/bin/telegram-kol-research web",
        ),
    )

    result = CliRunner().invoke(app, ["session-release", "--pid", "12345"])

    assert result.exit_code == 0
    assert calls == [(session_path, 12345, "telegram-kol-research session-release")]
    assert "Released Telegram session owner" in result.stdout
    assert "pid=12345" in result.stdout


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False
