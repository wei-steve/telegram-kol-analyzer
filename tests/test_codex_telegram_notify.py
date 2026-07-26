import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "codex_telegram_notify.py"
)


def _load_module():
    if not MODULE_PATH.exists():
        pytest.fail("Telegram notification helper does not exist")
    spec = importlib.util.spec_from_file_location(
        "codex_telegram_notify", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_send_notification_reads_keychain_and_posts_summary(monkeypatch):
    codex_telegram_notify = _load_module()
    keychain_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="secret-token\n",
        stderr="",
    )
    run = Mock(return_value=keychain_result)
    urlopen = Mock(return_value=_Response({"ok": True}))
    monkeypatch.setattr(codex_telegram_notify.subprocess, "run", run)
    monkeypatch.setattr(codex_telegram_notify.urllib_request, "urlopen", urlopen)

    codex_telegram_notify.send_notification("测试任务已完成")

    run.assert_called_once_with(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            "bot-token",
            "-s",
            "telegram-kol-codex-notifier",
            "-w",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/botsecret-token/sendMessage")
    assert b"chat_id=8129644952" in request.data
    assert "%E6%B5%8B%E8%AF%95%E4%BB%BB%E5%8A%A1%E5%B7%B2%E5%AE%8C%E6%88%90" in request.data.decode()
    assert urlopen.call_args.kwargs == {"timeout": 10.0}


def test_main_fails_cleanly_when_keychain_item_is_missing(monkeypatch, capsys):
    codex_telegram_notify = _load_module()
    error = subprocess.CalledProcessError(
        returncode=44,
        cmd=["security"],
        stderr="security: item not found",
    )
    monkeypatch.setattr(
        codex_telegram_notify.subprocess,
        "run",
        Mock(side_effect=error),
    )

    exit_code = codex_telegram_notify.main(["任务已完成"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Telegram notification failed: Bot Token is unavailable in macOS Keychain." in captured.err
    assert "security: item not found" not in captured.err


def test_main_fails_when_telegram_rejects_request(monkeypatch, capsys):
    codex_telegram_notify = _load_module()
    monkeypatch.setattr(
        codex_telegram_notify.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="secret-token\n",
                stderr="",
            )
        ),
    )
    monkeypatch.setattr(
        codex_telegram_notify.urllib_request,
        "urlopen",
        Mock(return_value=_Response({"ok": False, "description": "Bad Request"})),
    )

    exit_code = codex_telegram_notify.main(["任务已完成"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Telegram notification failed: Telegram rejected the message." in captured.err
    assert "secret-token" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("secret-token network detail"),
        json.JSONDecodeError("secret-token invalid JSON", "", 0),
    ],
)
def test_main_does_not_leak_credentials_in_transport_errors(
    monkeypatch, capsys, error
):
    codex_telegram_notify = _load_module()
    monkeypatch.setattr(
        codex_telegram_notify.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="secret-token\n",
                stderr="",
            )
        ),
    )
    monkeypatch.setattr(
        codex_telegram_notify.urllib_request,
        "urlopen",
        Mock(side_effect=error),
    )

    exit_code = codex_telegram_notify.main(["任务已完成"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Telegram notification failed: Unable to deliver the message." in captured.err
    assert "secret-token" not in captured.err
