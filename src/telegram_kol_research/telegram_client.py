"""Telegram user-account bootstrap and dialog discovery helpers."""

from __future__ import annotations

import os
from inspect import isawaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TelegramAuthConfig:
    api_id: int
    api_hash: str
    session_path: Path


def load_telegram_auth_config(
    environ: dict[str, str] | None = None,
    session_path: str | Path | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> TelegramAuthConfig:
    """Load Telegram user-account credentials from environment variables."""

    env = dict(_load_env_file_values(env_file_paths))
    env.update(environ or os.environ)
    api_id = env.get("TELEGRAM_API_ID")
    api_hash = env.get("TELEGRAM_API_HASH")
    resolved_session_path = Path(session_path or env.get("TELEGRAM_SESSION_PATH", "data/telegram.session"))

    if not api_id:
        raise ValueError("TELEGRAM_API_ID is required")
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH is required")

    return TelegramAuthConfig(
        api_id=int(api_id),
        api_hash=api_hash,
        session_path=resolved_session_path,
    )


def _load_env_file_values(
    env_file_paths: list[str | Path] | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    candidate_paths = env_file_paths or [
        ".env",
        "config/telegram.env",
    ]
    for raw_path in candidate_paths:
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def create_telegram_client(auth_config: TelegramAuthConfig) -> Any:
    """Create a Telethon client for the configured user account."""

    try:
        from telethon import TelegramClient
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Telethon is not installed in the current environment. Install project dependencies first."
        ) from exc

    auth_config.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(auth_config.session_path), auth_config.api_id, auth_config.api_hash)


async def maybe_await(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


async def ensure_telegram_login(
    client: Any,
    *,
    prompt_phone,
    prompt_code,
    prompt_password,
    echo,
) -> bool:
    """Connect the client and prompt for first-time login when needed."""

    connect = getattr(client, "connect", None)
    if callable(connect):
        await maybe_await(connect())

    is_user_authorized = getattr(client, "is_user_authorized", None)
    if not callable(is_user_authorized):
        return False

    authorized = bool(await maybe_await(is_user_authorized()))
    if authorized:
        return False

    echo("Telegram session not authorized yet. Keep Telegram open to receive the login code.")

    send_code_request = getattr(client, "send_code_request", None)
    sign_in = getattr(client, "sign_in", None)
    if not callable(send_code_request) or not callable(sign_in):
        raise RuntimeError("Telegram client does not support interactive login")

    phone_number = prompt_phone()
    await maybe_await(send_code_request(phone_number))
    code = prompt_code()
    try:
        await maybe_await(sign_in(phone=phone_number, code=code))
    except Exception as exc:
        if exc.__class__.__name__ != "SessionPasswordNeededError":
            raise
        password = prompt_password()
        await maybe_await(sign_in(password=password))

    authorized = bool(await maybe_await(is_user_authorized()))
    if not authorized:
        raise RuntimeError("Telegram login did not complete successfully")

    echo("Login successful. Telegram user session is now authorized.")
    return True


async def discover_dialogs(client: Any) -> list[dict[str, Any]]:
    """Enumerate dialogs and capture the fields needed for archived-group filtering."""

    dialogs: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs():
        dialogs.append(
            {
                "id": getattr(dialog, "id", None),
                "title": getattr(dialog, "title", None),
                "archived": bool(getattr(dialog, "archived", False)),
                "is_group": bool(getattr(dialog, "is_group", False)),
                "is_channel": bool(getattr(dialog, "is_channel", False)),
            }
        )
    return dialogs


async def fetch_dialog_messages(
    client: Any,
    dialog: dict[str, Any],
    limit: int = 100,
    media_root: str | Path = "data/media",
) -> list[dict[str, Any]]:
    """Fetch recent messages for a dialog and normalize the Telegram fields we need."""

    messages: list[dict[str, Any]] = []
    resolved_media_root = Path(media_root)
    async for message in client.iter_messages(dialog["id"], limit=limit):
        media = getattr(message, "media", None)
        sender = None
        get_sender = getattr(message, "get_sender", None)
        if callable(get_sender):
            sender = await get_sender()
        media_path = await _download_media_if_present(
            client,
            dialog_id=dialog["id"],
            message=message,
            media_root=resolved_media_root,
        )
        messages.append(
            {
                "chat_id": dialog["id"],
                "message_id": getattr(message, "id", None),
                "sender_id": getattr(message, "sender_id", None),
                "sender_name": _format_sender_name(sender),
                "text": getattr(message, "message", None),
                "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
                "posted_at": getattr(message, "date", None),
                "edit_date": getattr(message, "edit_date", None),
                "media": {
                    "kind": type(media).__name__.lower(),
                    "path": media_path,
                }
                if media is not None
                else None,
            }
        )
    return messages


async def _download_media_if_present(
    client: Any,
    *,
    dialog_id: int,
    message: Any,
    media_root: Path,
) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    if not _should_download_media(message):
        return None

    download_media = getattr(client, "download_media", None)
    if not callable(download_media):
        return None

    message_id = getattr(message, "id", None) or "unknown"
    target_dir = media_root / str(dialog_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = await download_media(media, file=str(target_dir / f"{message_id}"))
    if output_path in (None, ""):
        return None
    return str(Path(output_path))


def _should_download_media(message: Any) -> bool:
    if getattr(message, "photo", None) is not None:
        return True

    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", None)
    if isinstance(mime_type, str) and mime_type.startswith("image/"):
        return True

    return False


def _format_sender_name(sender: Any) -> str | None:
    if sender is None:
        return None
    first_name = getattr(sender, "first_name", None) or ""
    last_name = getattr(sender, "last_name", None) or ""
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if full_name:
        return full_name
    return getattr(sender, "title", None)


def filter_target_dialogs(dialogs: list[dict[str, Any]], titles: set[str]) -> list[dict[str, Any]]:
    """Keep only archived dialogs whose titles match the selected target groups."""

    return [
        dialog
        for dialog in dialogs
        if dialog.get("archived") and dialog.get("title") in titles
    ]
