from typer.testing import CliRunner
from pathlib import Path

from telegram_kol_research.cli import app


def test_web_command_is_available_in_help():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "web" in result.stdout


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
