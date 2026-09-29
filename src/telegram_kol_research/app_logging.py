"""Application logging and log-file pagination helpers."""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram_kol_research.telegram_bot_api import (
    TELEGRAM_BOT_TOKEN_PATTERN,
    redact_telegram_bot_tokens,
)


LOG_FILENAME = "telegram-kol.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_APPLICATION_HANDLER_MARKER = "_telegram_kol_application_handler"
ENTRY_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) (?P<logger>[^ ]+) (?P<message>.*)$"
)
TELEGRAM_BOT_URL_PATTERN = re.compile(
    r"(https?://api\.telegram\.org/bot)[^/\s'\"?]+", re.IGNORECASE
)
_REDACTING_RECORD_FACTORY_MARKER = "_telegram_kol_redacting_record_factory"
# httpx logs every request URL at INFO ("HTTP Request: POST https://api.
# telegram.org/bot<token>/..."). Nothing enables it today -- root stays at
# WARNING -- but pin it so a future root-level INFO change cannot turn it on.
_QUIET_URL_LOGGERS = ("httpx", "httpcore")


def _redact_record(record: logging.LogRecord) -> None:
    """Mask Telegram bot tokens in a record's message, traceback and stack.

    Only a record that actually contains a token is rewritten, so every other
    record keeps its ``msg``/``args`` untouched for handlers and tests.
    """

    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - leave malformed records to the handler
        message = None
    if message is not None and TELEGRAM_BOT_TOKEN_PATTERN.search(message):
        record.msg = redact_telegram_bot_tokens(message)
        record.args = ()
    if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
        exc_text = logging.Formatter().formatException(record.exc_info)
        if TELEGRAM_BOT_TOKEN_PATTERN.search(exc_text):
            # ``Formatter.format`` reuses a pre-filled ``exc_text`` instead of
            # formatting ``exc_info`` again, so this is what every handler sees.
            record.exc_text = redact_telegram_bot_tokens(exc_text)
    if record.stack_info and TELEGRAM_BOT_TOKEN_PATTERN.search(record.stack_info):
        record.stack_info = redact_telegram_bot_tokens(record.stack_info)


def install_secret_log_redaction() -> None:
    """Redact bot tokens from every log record the process creates.

    A record factory runs once per record before any handler, so it covers the
    package logger's file and stderr handlers, uvicorn's handlers, and
    ``logging.lastResort`` alike -- a handler-level filter would miss handlers
    that uvicorn's ``dictConfig`` installs after this call. Idempotent.
    """

    current = logging.getLogRecordFactory()
    if not getattr(current, _REDACTING_RECORD_FACTORY_MARKER, False):

        def _factory(*args, **kwargs):
            record = current(*args, **kwargs)
            _redact_record(record)
            return record

        setattr(_factory, _REDACTING_RECORD_FACTORY_MARKER, True)
        logging.setLogRecordFactory(_factory)
    for name in _QUIET_URL_LOGGERS:
        url_logger = logging.getLogger(name)
        if url_logger.level < logging.WARNING:
            url_logger.setLevel(logging.WARNING)


def configure_application_logging(log_directory: Path) -> Path:
    """Configure the package logger to write a rotating UTF-8 application log."""
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / LOG_FILENAME
    install_secret_log_redaction()
    logger = logging.getLogger("telegram_kol_research")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, _APPLICATION_HANDLER_MARKER, False)
    ]
    configured_path = next(
        (
            Path(handler.baseFilename)
            for handler in handlers
            if isinstance(handler, RotatingFileHandler)
        ),
        None,
    )
    if configured_path == log_path.resolve() and len(handlers) == 2:
        return log_path

    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        setattr(handler, _APPLICATION_HANDLER_MARKER, True)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return log_path


def read_log_page(
    log_directory: Path, *, offset: int, limit: int, level: str | None
) -> dict[str, object]:
    """Return one newest-first page of parsed application log entries."""
    entries: list[dict[str, str]] = []
    log_paths = [
        log_directory / f"{LOG_FILENAME}.{number}"
        for number in range(10, 0, -1)
    ] + [log_directory / LOG_FILENAME]
    for log_path in log_paths:
        if not log_path.is_file():
            continue
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = ENTRY_PATTERN.match(line)
            if match:
                entries.append(match.groupdict())
            elif entries:
                entries[-1]["message"] += f"\n{line}"

    for entry in entries:
        entry["message"] = TELEGRAM_BOT_URL_PATTERN.sub(r"\1[REDACTED]", entry["message"])
    entries.reverse()
    if level is not None:
        entries = [entry for entry in entries if entry["level"] == level]
    items = entries[offset : offset + limit]
    return {
        "items": items,
        "next_offset": offset + len(items),
        "has_more": offset + len(items) < len(entries),
    }
