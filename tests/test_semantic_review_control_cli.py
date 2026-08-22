import json
from datetime import datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

import telegram_kol_research.cli as cli_module
from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.semantic_review_control import SemanticReviewControlError
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 21, 12, 0)


def _seed(session_factory, *, message_id: int, status: str) -> int:
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=message_id, text="fixture")
        session.add(raw)
        session.flush()
        row = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload_json='{"recognition_result":"非策略"}',
            agreement_status="pending",
            differences_json="[]",
            automation_status="skipped",
            prompt_versions_json="{}",
            comparison_status=status,
            comparison_error="402" if status == "failed" else None,
            comparison_attempts=1 if status == "failed" else 0,
            comparison_started_at=(NOW if status == "running" else None),
            comparison_claim_token=("claim" if status == "running" else None),
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(minutes=5),
        )
        session.add(row)
        session.commit()
        return raw.id


def _terminalize_args(database_path: Path, output_path: Path) -> list[str]:
    return [
        "semantic-review-terminalize",
        "--database-path",
        str(database_path),
        "--plan-output",
        str(output_path),
    ]


def test_terminalize_dry_run_is_read_only_and_writes_json_evidence(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "plan.json"
    session_factory = create_session_factory(database_path)
    raw_id = _seed(session_factory, message_id=1, status="pending")

    result = CliRunner().invoke(app, _terminalize_args(database_path, output_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    evidence = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload == evidence
    assert payload["mode"] == "dry_run"
    assert len(payload["plan_sha"]) == 64
    assert payload["target_count"] == 1
    assert payload["changed_count"] == 0
    assert payload["quick_check"] == "ok"
    assert payload["provider_call_count"] == 0
    assert payload["notification_count"] == 0
    assert payload["exchange_write_count"] == 0
    with session_factory() as session:
        assert session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one().comparison_status == "pending"


def test_terminalize_refuses_missing_database_and_invalid_apply_sha(tmp_path):
    missing = CliRunner().invoke(
        app,
        _terminalize_args(tmp_path / "missing.db", tmp_path / "plan.json"),
    )
    assert missing.exit_code == 2
    assert "does not exist" in missing.output

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    _seed(session_factory, message_id=1, status="pending")
    invalid = CliRunner().invoke(
        app,
        _terminalize_args(database_path, tmp_path / "plan.json")
        + ["--apply", "--expected-plan-sha", "not-a-sha"],
    )
    assert invalid.exit_code == 2
    assert "64-hex" in invalid.output


def test_terminalize_apply_requires_disabled_setting_and_no_running_rows(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    _seed(session_factory, message_id=1, status="pending")
    _seed(session_factory, message_id=2, status="running")
    dry = CliRunner().invoke(
        app, _terminalize_args(database_path, tmp_path / "plan.json")
    )
    sha = json.loads(dry.output)["plan_sha"]
    running = CliRunner().invoke(
        app,
        _terminalize_args(database_path, tmp_path / "plan.json")
        + ["--apply", "--expected-plan-sha", sha],
    )
    assert running.exit_code == 2
    assert "running" in running.output

    with session_factory() as session:
        row = session.query(RecognitionDecision).filter_by(
            comparison_status="running"
        ).one()
        row.comparison_status = "completed"
        row.agreement_status = "agreed"
        row.comparison_started_at = None
        row.comparison_claim_token = None
        session.commit()
    save_trading_settings(session_factory, {"semantic_review_enabled": True})
    dry = CliRunner().invoke(
        app, _terminalize_args(database_path, tmp_path / "plan.json")
    )
    enabled = CliRunner().invoke(
        app,
        _terminalize_args(database_path, tmp_path / "plan.json")
        + ["--apply", "--expected-plan-sha", json.loads(dry.output)["plan_sha"]],
    )
    assert enabled.exit_code == 2
    assert "enabled" in enabled.output


def test_terminalize_reports_drift_and_plan_write_failure(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    _seed(session_factory, message_id=1, status="pending")
    dry = CliRunner().invoke(
        app, _terminalize_args(database_path, tmp_path / "plan.json")
    )
    sha = json.loads(dry.output)["plan_sha"]

    def refuse_drift(*args, **kwargs):
        raise SemanticReviewControlError("semantic review target drift detected")

    monkeypatch.setattr(
        cli_module, "apply_semantic_review_disable_plan", refuse_drift
    )
    drift = CliRunner().invoke(
        app,
        _terminalize_args(database_path, tmp_path / "plan.json")
        + ["--apply", "--expected-plan-sha", sha],
    )
    assert drift.exit_code == 2
    assert "drift" in drift.output

    def fail_write(self, *args, **kwargs):
        raise OSError("evidence disk unavailable")

    monkeypatch.setattr(Path, "write_text", fail_write)
    failed = CliRunner().invoke(
        app, _terminalize_args(database_path, tmp_path / "unwritable.json")
    )
    assert failed.exit_code == 2
    assert "evidence disk unavailable" in failed.output


def _apply_terminalization(database_path: Path, preimage_path: Path) -> None:
    runner = CliRunner()
    dry = runner.invoke(app, _terminalize_args(database_path, preimage_path))
    assert dry.exit_code == 0, dry.output
    sha = json.loads(dry.output)["plan_sha"]
    applied = runner.invoke(
        app,
        _terminalize_args(database_path, preimage_path)
        + ["--apply", "--expected-plan-sha", sha],
    )
    assert applied.exit_code == 0, applied.output


def _rollback_args(
    database_path: Path, preimage_path: Path, output_path: Path
) -> list[str]:
    return [
        "semantic-review-terminalize-rollback",
        "--database-path",
        str(database_path),
        "--preimage-plan",
        str(preimage_path),
        "--plan-output",
        str(output_path),
    ]


def test_terminalize_rollback_dry_run_then_exact_apply(tmp_path):
    database_path = tmp_path / "research.db"
    preimage_path = tmp_path / "preimage.json"
    rollback_path = tmp_path / "rollback.json"
    session_factory = create_session_factory(database_path)
    raw_id = _seed(session_factory, message_id=1, status="failed")
    with session_factory() as session:
        original = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        original_status = original.comparison_status
        original_error = original.comparison_error
        original_updated_at = original.updated_at
    _apply_terminalization(database_path, preimage_path)

    dry = CliRunner().invoke(
        app, _rollback_args(database_path, preimage_path, rollback_path)
    )
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.output)
    assert payload["mode"] == "dry_run"
    assert payload["target_count"] == 1
    assert payload["changed_count"] == 0
    assert payload["provider_call_count"] == 0
    assert payload["notification_count"] == 0
    assert payload["exchange_write_count"] == 0

    applied = CliRunner().invoke(
        app,
        _rollback_args(database_path, preimage_path, rollback_path)
        + ["--apply", "--expected-plan-sha", payload["plan_sha"]],
    )
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.output)
    assert applied_payload["mode"] == "apply"
    assert applied_payload["changed_count"] == 1
    with session_factory() as session:
        restored = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        assert restored.comparison_status == original_status
        assert restored.comparison_error == original_error
        assert restored.updated_at == original_updated_at


def test_terminalize_rollback_refuses_drift(tmp_path):
    database_path = tmp_path / "research.db"
    preimage_path = tmp_path / "preimage.json"
    rollback_path = tmp_path / "rollback.json"
    session_factory = create_session_factory(database_path)
    raw_id = _seed(session_factory, message_id=1, status="pending")
    _apply_terminalization(database_path, preimage_path)
    dry = CliRunner().invoke(
        app, _rollback_args(database_path, preimage_path, rollback_path)
    )
    sha = json.loads(dry.output)["plan_sha"]
    with session_factory() as session:
        row = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        row.notification_status = "changed"
        row.updated_at = NOW + timedelta(days=2)
        session.commit()

    refused = CliRunner().invoke(
        app,
        _rollback_args(database_path, preimage_path, rollback_path)
        + ["--apply", "--expected-plan-sha", sha],
    )
    assert refused.exit_code == 2
    assert "drift" in refused.output
