"""No Telegram bot token may reach a log handler.

2026-09-29: ``journalctl -u telegram-kol-worker`` held full bot tokens, because
``raise_for_status()`` puts the Bot API URL (``/bot<token>/getUpdates``) into
the ``HTTPStatusError`` message and the background-task supervisor logs that
exception with ``exc_info``. These tests cover both boundaries: the sanitized
error raised at the Bot API call sites, and the process-wide log redaction.

``configure_application_logging`` sets ``propagate = False`` on the package
logger, so ``caplog`` is unreliable in a full run; every test attaches its own
handler to the logger it exercises.
"""

from __future__ import annotations

import asyncio
import io
import logging
import traceback
from pathlib import Path

import httpx
import pytest

from telegram_kol_research import telegram_bot_commands
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.app_logging import (
    LOG_FORMAT,
    configure_application_logging,
    install_secret_log_redaction,
)
from telegram_kol_research.telegram_bot_api import (
    TelegramBotApiStatusError,
    raise_for_telegram_status,
    redact_telegram_bot_tokens,
)
from telegram_kol_research.web_app import BackgroundTaskSupervision


FAKE_TOKEN = "123456789:AAFakeTokenForTests_only-xyz"
FAKE_SECRET = "AAFakeTokenForTests_only-xyz"
BASE_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}"


def _bad_gateway_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502))
    )


def _raw_status_error() -> httpx.HTTPStatusError:
    """The unsanitized exception ``raise_for_status`` produces for a Bot API URL."""

    request = httpx.Request("GET", f"{BASE_URL}/getUpdates?offset=5&timeout=25")
    response = httpx.Response(502, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        assert FAKE_SECRET in str(exc)  # the leak this file guards against
        return exc
    raise AssertionError("502 must raise")


class _CapturingHandler:
    def __init__(self, logger_name: str) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter(LOG_FORMAT))
        self.logger = logging.getLogger(logger_name)
        self._previous_level = self.logger.level

    def __enter__(self) -> "_CapturingHandler":
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc_info) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._previous_level)

    @property
    def text(self) -> str:
        return self.stream.getvalue()


def _supervision() -> BackgroundTaskSupervision:
    return BackgroundTaskSupervision(task_name="telegram_bot_command_task")


@pytest.mark.parametrize(
    "call",
    [
        lambda client: telegram_bot_commands._get_updates(client, BASE_URL, offset=5),
        lambda client: telegram_bot_commands._delete_webhook(client, BASE_URL),
        lambda client: telegram_bot_commands._latest_update_offset(client, BASE_URL),
        lambda client: telegram_bot_commands._send_message(
            client, BASE_URL, chat_id="-100", text="hi"
        ),
    ],
    ids=["getUpdates", "deleteWebhook", "latestOffset", "sendMessage"],
)
def test_bot_api_status_error_carries_no_token(call):
    async def run() -> BaseException:
        async with _bad_gateway_client() as client:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await call(client)
        return caught.value

    exc = asyncio.run(run())

    assert isinstance(exc, TelegramBotApiStatusError)
    assert exc.response.status_code == 502
    assert "502" in str(exc)
    assert FAKE_SECRET not in str(exc)
    assert FAKE_SECRET not in repr(exc)
    rendered = "".join(traceback.format_exception(exc))
    assert FAKE_SECRET not in rendered
    assert exc.__suppress_context__ is True


def test_status_error_names_the_bot_api_method():
    response = httpx.Response(
        502, request=httpx.Request("POST", f"{BASE_URL}/deleteWebhook")
    )
    with pytest.raises(TelegramBotApiStatusError) as caught:
        raise_for_telegram_status(response)
    assert str(caught.value) == "Telegram Bot API deleteWebhook returned HTTP 502"


def test_success_response_does_not_raise():
    response = httpx.Response(
        200, request=httpx.Request("GET", f"{BASE_URL}/getUpdates"), json={}
    )
    raise_for_telegram_status(response)


def test_supervisor_log_of_a_failed_bot_poll_contains_no_token(monkeypatch):
    """The production path: 502 on getUpdates -> supervisor warning with exc_info."""

    async def fake_sleep(delay):
        return None

    async def failing_poll():
        async with _bad_gateway_client() as client:
            await telegram_bot_commands._get_updates(client, BASE_URL, offset=1)

    monkeypatch.setattr(web_app_module.asyncio, "sleep", fake_sleep)
    with _CapturingHandler("telegram_kol_research") as capture:
        asyncio.run(
            web_app_module._supervise_restartable_background_task(
                "telegram_bot_command_task",
                failing_poll,
                supervision=_supervision(),
            )
        )

    assert "TelegramBotApiStatusError" in capture.text
    assert "getUpdates returned HTTP 502" in capture.text
    assert FAKE_SECRET not in capture.text


@pytest.mark.parametrize(
    "logger_name",
    ["telegram_kol_research.web_app", "uvicorn.error", "asyncio"],
)
def test_log_redaction_masks_a_raw_status_error_in_any_logger(logger_name):
    """Defence in depth: an unsanitized error logged anywhere is still masked."""

    install_secret_log_redaction()
    exc = _raw_status_error()
    with _CapturingHandler(logger_name) as capture:
        capture.logger.warning(
            "poll failed url=%s", exc.request.url, exc_info=exc
        )
        capture.logger.error("direct %s", str(exc))
        try:
            raise exc
        except httpx.HTTPStatusError:
            capture.logger.exception("while handling")

    assert FAKE_SECRET not in capture.text
    assert "bot[REDACTED]" in capture.text
    assert "HTTPStatusError" in capture.text


def test_log_redaction_leaves_token_free_records_untouched():
    install_secret_log_redaction()
    record = logging.getLogRecordFactory()(
        "telegram_kol_research.x", logging.INFO, __file__, 1,
        "accepted message_id=%s", (42,), None,
    )
    assert record.msg == "accepted message_id=%s"
    assert record.args == (42,)
    assert record.exc_text is None


def test_install_is_idempotent_and_quiets_httpx_request_logging():
    install_secret_log_redaction()
    factory = logging.getLogRecordFactory()
    install_secret_log_redaction()
    assert logging.getLogRecordFactory() is factory
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_application_log_file_contains_no_token(tmp_path: Path):
    log_path = configure_application_logging(tmp_path)
    exc = _raw_status_error()
    logging.getLogger("telegram_kol_research.test").warning(
        "Supervised background task %s failed", "telegram_bot_command_task",
        exc_info=exc,
    )
    for handler in logging.getLogger("telegram_kol_research").handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "Supervised background task telegram_bot_command_task failed" in content
    assert "bot[REDACTED]" in content
    assert FAKE_SECRET not in content


def test_redact_helper_masks_every_occurrence():
    text = f"a {BASE_URL}/x b bot987:Other_Secret-1 c"
    redacted = redact_telegram_bot_tokens(text)
    assert FAKE_SECRET not in redacted
    assert "Other_Secret-1" not in redacted
    assert redacted.count("bot[REDACTED]") == 2
