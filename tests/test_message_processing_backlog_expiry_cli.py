import json
from datetime import datetime

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MessageProcessingJob, RawMessage


def _seed_exact_backlog(database_path) -> None:
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        for raw_id in range(13877, 14031):
            session.add(
                RawMessage(
                    id=raw_id,
                    chat_id=-1000000000001,
                    message_id=raw_id,
                    text="fixture",
                    posted_at=datetime(2026, 8, 30, 1, 0),
                )
            )
            session.add(
                MessageProcessingJob(
                    raw_message_id=raw_id,
                    chat_id=-1000000000001,
                    status="pending",
                    attempt_count=0,
                    shadow=False,
                )
            )
        session.commit()


def _args(database_path) -> list[str]:
    return [
        "expire-message-processing-backlog",
        "--database-path",
        str(database_path),
        "--minimum-raw-message-id",
        "13877",
        "--watermark-raw-message-id",
        "14030",
        "--expected-pending-count",
        "154",
    ]


def test_backlog_expiry_cli_defaults_to_read_only_dry_run(tmp_path):
    database_path = tmp_path / "dry.db"
    _seed_exact_backlog(database_path)

    result = CliRunner().invoke(app, _args(database_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert payload["target_count"] == 154
    assert payload["minimum_raw_message_id"] == 13877
    assert payload["watermark_raw_message_id"] == 14030
    assert len(payload["target_manifest_sha256"]) == 64
    assert payload["changed_count"] == 0
    assert payload["exchange_write_count"] == 0
    with create_session_factory(database_path)() as session:
        assert session.query(MessageProcessingJob).filter_by(status="pending").count() == 154


def test_backlog_expiry_cli_apply_is_explicit_and_exact(tmp_path):
    database_path = tmp_path / "apply.db"
    _seed_exact_backlog(database_path)

    result = CliRunner().invoke(app, _args(database_path) + ["--apply"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "apply"
    assert payload["changed_count"] == 154
    assert payload["exchange_write_count"] == 0
    with create_session_factory(database_path)() as session:
        assert session.query(MessageProcessingJob).filter_by(status="expired").count() == 154


def test_backlog_expiry_cli_refuses_missing_database_and_drift(tmp_path):
    missing = CliRunner().invoke(app, _args(tmp_path / "missing.db"))
    assert missing.exit_code == 2
    assert "database does not exist" in missing.output

    database_path = tmp_path / "drift.db"
    _seed_exact_backlog(database_path)
    with create_session_factory(database_path)() as session:
        row = session.query(MessageProcessingJob).filter_by(raw_message_id=13877).one()
        row.attempt_count = 1
        session.commit()
    drift = CliRunner().invoke(app, _args(database_path) + ["--apply"])
    assert drift.exit_code == 2
    assert "attempt_count_not_zero" in drift.output


def test_backlog_expiry_cli_refuses_a_different_contiguous_interval(tmp_path):
    database_path = tmp_path / "other-interval.db"
    _seed_exact_backlog(database_path)

    result = CliRunner().invoke(
        app,
        [
            "expire-message-processing-backlog",
            "--database-path",
            str(database_path),
            "--minimum-raw-message-id",
            "13878",
            "--watermark-raw-message-id",
            "14030",
            "--expected-pending-count",
            "153",
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "phase1_contract_mismatch" in result.output
    with create_session_factory(database_path)() as session:
        assert session.query(MessageProcessingJob).filter_by(status="pending").count() == 154
