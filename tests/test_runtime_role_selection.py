from __future__ import annotations

import pytest
from typer.testing import CliRunner


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
