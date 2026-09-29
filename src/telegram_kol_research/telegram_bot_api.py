"""Token-safe error boundary for Telegram Bot API calls made with httpx.

Every Bot API URL carries the bot token in its path
(``https://api.telegram.org/bot<token>/<method>``), and
``httpx.Response.raise_for_status()`` embeds the full URL in the exception
message. Any traceback of that exception -- the background-task supervisor
logs one with ``exc_info`` -- therefore wrote the token to journald and to the
application log file (2026-09-29: 13 such lines in 30 days, all in
telegram-kol-worker, from 502s on ``getUpdates``/``deleteWebhook``).

``raise_for_telegram_status`` replaces ``raise_for_status`` at every Bot API
call site. The error it raises is still an ``httpx.HTTPStatusError`` so
existing ``except httpx.HTTPStatusError`` handlers and
``exc.response.status_code`` checks keep working, but its message carries only
the method name and status code, and it is raised ``from None`` so the
original URL-bearing exception is not chained into the traceback.
"""

from __future__ import annotations

import re

import httpx


TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
TELEGRAM_BOT_TOKEN_REPLACEMENT = "bot[REDACTED]"


def redact_telegram_bot_tokens(text: str) -> str:
    """Return ``text`` with every ``bot<id>:<secret>`` token segment masked."""

    return TELEGRAM_BOT_TOKEN_PATTERN.sub(TELEGRAM_BOT_TOKEN_REPLACEMENT, text)


class TelegramBotApiStatusError(httpx.HTTPStatusError):
    """A non-2xx Bot API response, described without the request URL."""

    def __init__(
        self,
        *,
        method: str,
        status_code: int,
        request: httpx.Request | None,
        response: httpx.Response,
    ) -> None:
        self.method = method
        self.status_code = status_code
        super().__init__(
            f"Telegram Bot API {method} returned HTTP {status_code}",
            request=request,  # type: ignore[arg-type]
            response=response,
        )


def _request_of(exc: httpx.HTTPStatusError) -> httpx.Request | None:
    try:
        return exc.request
    except RuntimeError:  # httpx raises when no request was attached
        return None


def _bot_api_method(request: httpx.Request | None) -> str:
    if request is None:
        return "unknown"
    method = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    # A malformed URL could leave the token segment as the last path piece.
    if not method or TELEGRAM_BOT_TOKEN_PATTERN.search(method):
        return "unknown"
    return method


def raise_for_telegram_status(response: httpx.Response) -> None:
    """Token-safe replacement for ``response.raise_for_status()``."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        request = _request_of(exc)
        raise TelegramBotApiStatusError(
            method=_bot_api_method(request),
            status_code=int(exc.response.status_code),
            request=request,
            response=exc.response,
        ) from None
