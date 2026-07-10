from pathlib import Path
import logging

from telegram_kol_research.app_logging import (
    configure_application_logging,
    read_log_page,
)


def test_configure_application_logging_writes_rotating_utf8_log_once(tmp_path: Path):
    path = configure_application_logging(tmp_path)
    logger = logging.getLogger("telegram_kol_research.test")
    logger.info("accepted message_id=42")

    assert path == tmp_path / "telegram-kol.log"
    assert "accepted message_id=42" in path.read_text(encoding="utf-8")
    assert len(logging.getLogger("telegram_kol_research").handlers) == 2

    configure_application_logging(tmp_path)
    assert len(logging.getLogger("telegram_kol_research").handlers) == 2


def test_read_log_page_returns_latest_entries_and_keeps_traceback_attached(
    tmp_path: Path,
):
    (tmp_path / "telegram-kol.log.1").write_text(
        "2026-07-10 08:00:00,000 INFO telegram_kol_research.sync old\n",
        encoding="utf-8",
    )
    (tmp_path / "telegram-kol.log").write_text(
        "2026-07-10 09:00:00,000 ERROR telegram_kol_research.web failed\n"
        "Traceback (most recent call last):\n"
        "RuntimeError: offline\n"
        "2026-07-10 10:00:00,000 INFO telegram_kol_research.web latest\n",
        encoding="utf-8",
    )

    page = read_log_page(tmp_path, offset=0, limit=2, level=None)

    assert [entry["message"] for entry in page["items"]] == [
        "latest",
        "failed\nTraceback (most recent call last):\nRuntimeError: offline",
    ]
    assert page["next_offset"] == 2
    assert page["has_more"] is True


def test_read_log_page_filters_by_level(tmp_path: Path):
    (tmp_path / "telegram-kol.log").write_text(
        "2026-07-10 08:00:00,000 INFO telegram_kol_research.sync ok\n"
        "2026-07-10 09:00:00,000 WARNING telegram_kol_research.sync slow\n",
        encoding="utf-8",
    )

    page = read_log_page(tmp_path, offset=0, limit=100, level="WARNING")

    assert page["items"] == [
        {
            "timestamp": "2026-07-10 09:00:00,000",
            "level": "WARNING",
            "logger": "telegram_kol_research.sync",
            "message": "slow",
        }
    ]
    assert page["has_more"] is False
