from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.telegram_client import TelegramAuthConfig
from telegram_kol_research.telegram_client import load_telegram_auth_config


def test_sync_discover_mode_does_not_fetch_messages(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: FakeClient(),
    )

    async def fake_discover_dialogs(client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fail_fetch_dialog_messages(client, dialog, limit):
        raise AssertionError("discover mode should not fetch messages")

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr("telegram_kol_research.cli.fetch_dialog_messages", fail_fetch_dialog_messages)

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--mode",
            "discover",
        ],
    )

    assert result.exit_code == 0
    assert "Discovered 1 archived target group" in result.stdout
    assert "Discovery only mode" in result.stdout


def test_sync_reports_friendly_auth_error(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(groups=[]),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: (_ for _ in ()).throw(ValueError("TELEGRAM_API_ID is required")),
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 1
    assert "Telegram auth/config error" in result.stdout
    assert "TELEGRAM_API_ID is required" in result.stdout


def test_load_telegram_auth_config_reads_local_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_API_ID=123456",
                "TELEGRAM_API_HASH=hash-from-file",
                "TELEGRAM_SESSION_PATH=data/from-env-file.session",
            ]
        ),
        encoding="utf-8",
    )

    auth_config = load_telegram_auth_config(
        environ={},
        env_file_paths=[env_file],
    )

    assert auth_config.api_id == 123456
    assert auth_config.api_hash == "hash-from-file"
    assert str(auth_config.session_path) == "data/from-env-file.session"


def test_sync_prompts_for_first_time_login(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class FakeClient:
        def __init__(self):
            self.authorized = False
            self.sent_phone = None
            self.code = None

        def connect(self):
            return None

        def is_user_authorized(self):
            return self.authorized

        def send_code_request(self, phone_number):
            self.sent_phone = phone_number
            return None

        def sign_in(self, *, phone, code):
            self.authorized = True
            self.code = code
            return None

        def disconnect(self):
            return None

    fake_client = FakeClient()

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: fake_client,
    )

    async def fake_discover_dialogs(client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    monkeypatch.setattr("telegram_kol_research.cli.discover_dialogs", fake_discover_dialogs)
    monkeypatch.setattr(
        "telegram_kol_research.cli.typer.prompt",
        lambda text, **kwargs: "+8613812345678" if "phone" in text.lower() else "12345",
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--mode",
            "discover",
        ],
    )

    assert result.exit_code == 0
    assert "Telegram session not authorized yet" in result.stdout
    assert "Login successful" in result.stdout
    assert fake_client.sent_phone == "+8613812345678"
    assert fake_client.code == "12345"


def test_sync_discover_mode_supports_async_telegram_client(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class AsyncClient:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def iter_dialogs(self):
            yield type(
                "Dialog",
                (),
                {
                    "id": 9001,
                    "title": "VIP BTC Room",
                    "archived": True,
                    "is_group": True,
                    "is_channel": False,
                },
            )()

        async def disconnect(self):
            return None

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: AsyncClient(),
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--mode",
            "discover",
        ],
    )

    assert result.exit_code == 0
    assert "Discovered 1 archived target group" in result.stdout


def test_sync_discover_mode_keeps_single_event_loop_for_telegram_client(monkeypatch, tmp_path):
    import asyncio

    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.load_group_config",
        lambda path: GroupConfig(
            groups=[TargetGroupConfig(chat_title="VIP BTC Room", enabled=True)]
        ),
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_telegram_auth_config",
        lambda: TelegramAuthConfig(
            api_id=123456,
            api_hash="hash",
            session_path=tmp_path / "telegram.session",
        ),
    )

    class LoopBoundClient:
        def __init__(self):
            self.bound_loop = None

        async def connect(self):
            self._bind_loop()

        async def is_user_authorized(self):
            self._bind_loop()
            return True

        async def iter_dialogs(self):
            self._bind_loop()
            yield type(
                "Dialog",
                (),
                {
                    "id": 9001,
                    "title": "VIP BTC Room",
                    "archived": True,
                    "is_group": True,
                    "is_channel": False,
                },
            )()

        async def disconnect(self):
            self._bind_loop()
            return None

        def _bind_loop(self):
            loop = asyncio.get_running_loop()
            if self.bound_loop is None:
                self.bound_loop = loop
                return
            if self.bound_loop is not loop:
                raise RuntimeError("The asyncio event loop must not change after connection")

    monkeypatch.setattr(
        "telegram_kol_research.cli.create_telegram_client",
        lambda auth_config: LoopBoundClient(),
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--mode",
            "discover",
        ],
    )

    assert result.exit_code == 0
    assert "Discovered 1 archived target group" in result.stdout
