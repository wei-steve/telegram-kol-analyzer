#!/usr/bin/env python3
"""Send a Codex task-completion message through Telegram."""

from __future__ import annotations

import json
import subprocess
import sys
from urllib import parse as urllib_parse
from urllib import request as urllib_request

KEYCHAIN_ACCOUNT = "bot-token"
KEYCHAIN_SERVICE = "telegram-kol-codex-notifier"
TELEGRAM_CHAT_ID = "8129644952"
REQUEST_TIMEOUT_SECONDS = 10.0


class TokenUnavailable(RuntimeError):
    """Raised when the Telegram Bot Token cannot be read."""


class TelegramRejected(RuntimeError):
    """Raised when Telegram rejects a notification."""


def _load_bot_token() -> str:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise TokenUnavailable from exc

    token = result.stdout.strip()
    if not token:
        raise TokenUnavailable
    return token


def send_notification(summary: str) -> None:
    """Send one non-sensitive task-completion summary."""
    token = _load_bot_token()
    body = urllib_parse.urlencode(
        {"chat_id": TELEGRAM_CHAT_ID, "text": summary}
    ).encode()
    request = urllib_request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib_request.urlopen(
        request, timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        payload = json.loads(response.read().decode())
    if payload.get("ok") is not True:
        raise TelegramRejected


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    summary = " ".join(arguments).strip()
    if not summary:
        print(
            "Telegram notification failed: A completion summary is required.",
            file=sys.stderr,
        )
        return 2

    try:
        send_notification(summary)
    except TokenUnavailable:
        print(
            "Telegram notification failed: "
            "Bot Token is unavailable in macOS Keychain.",
            file=sys.stderr,
        )
        return 1
    except TelegramRejected:
        print(
            "Telegram notification failed: Telegram rejected the message.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Telegram notification failed: Unable to deliver the message.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
