from typer.testing import CliRunner
from pathlib import Path

from telegram_kol_research.cli import app


def test_web_command_is_available_in_help():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "web" in result.stdout
    assert "alerts" in result.stdout


def test_web_command_passes_enabled_target_titles_to_web_app(tmp_path, monkeypatch):
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

    def fake_create_web_app(*, database_path, live_target_titles, media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300):
        captured["database_path"] = Path(database_path)
        captured["live_target_titles"] = set(live_target_titles)
        captured["telegram_client"] = telegram_client
        captured["live_listener_status_reason"] = live_listener_status_reason
        captured["group_labels_by_title"] = dict(group_labels_by_title or {})
        return object()

    def fake_run(app_instance, host, port):
        captured["app_instance"] = app_instance
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr("uvicorn.run", fake_run)
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
        ],
    )

    assert result.exit_code == 0
    assert captured["live_target_titles"] == {"Demo Group"}
    assert captured["group_labels_by_title"] == {"Demo Group": "Demo Group"}
    assert captured["port"] == 8123
    assert captured["live_listener_status_reason"] == "缺少 Telegram API 凭据或 Telethon 运行依赖"


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

    def fake_create_web_app(*, database_path, live_target_titles, media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300):
        captured["telegram_client"] = telegram_client
        captured["live_listener_status_reason"] = live_listener_status_reason
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app_instance, host, port: None)
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

    def fake_create_web_app(*, database_path, live_target_titles, media_root=None, live_listener_runner=None, telegram_client=None, live_listener_status_reason=None, group_labels_by_title=None, now_provider=None, reconcile_runner=None, reconcile_interval_seconds=300):
        return object()

    monkeypatch.setattr("telegram_kol_research.cli.create_web_app", fake_create_web_app, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app_instance, host, port: None)
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

    async def fake_runner(*, client, session_factory, broker, target_titles, media_root, strategy_alert_config=None):
        captured["client"] = client
        captured["target_titles"] = set(target_titles)
        captured["strategy_alert_config"] = strategy_alert_config

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
    assert captured["lock_entered"] is True
    assert captured["lock_exited"] is True


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False
