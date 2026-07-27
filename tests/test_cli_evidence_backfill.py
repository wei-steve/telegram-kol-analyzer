import json
from datetime import datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.evidence_backfill import (
    EvidenceBackfillPlan,
    EvidenceBackfillResult,
)


def _empty_plan(chat_ids=(-1001,)):
    return EvidenceBackfillPlan(
        chat_ids=tuple(chat_ids),
        start_at=None,
        end_at=None,
        limit=100,
        retry_failed=False,
        items=(),
    )


def _empty_result(mode="dry_run"):
    return EvidenceBackfillResult(
        mode=mode,
        considered=0,
        planned=0,
        succeeded=0,
        failed=0,
        skipped_completed=0,
        skipped_failed=0,
        skipped_empty=0,
        rows=(),
    )


def test_backfill_cli_rejects_empty_chat_scope_before_loading_mimo(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("MiMo config must not load without a chat scope")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "backfill-mimo-evidence",
            "--database-path",
            str(tmp_path / "research.db"),
        ],
    )

    assert result.exit_code == 2
    assert "chat" in (result.stdout + result.stderr).lower()


def test_backfill_cli_defaults_to_dry_run_and_normalizes_chat_ids(
    tmp_path,
    monkeypatch,
):
    captured = {}

    def plan(_session_factory, **kwargs):
        captured["plan"] = kwargs
        return _empty_plan(tuple(sorted(set(kwargs["chat_ids"]))))

    def run(_session_factory, **kwargs):
        captured["run"] = kwargs
        return _empty_result()

    monkeypatch.setattr(
        "telegram_kol_research.cli.plan_mimo_evidence_backfill",
        plan,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_mimo_evidence_backfill",
        run,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("dry-run must not load MiMo config")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "backfill-mimo-evidence",
            "--database-path",
            str(tmp_path / "research.db"),
            "--chat-id=-1001",
            "--chat-id=-1001",
            "--limit",
            "25",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["plan"]["chat_ids"] == [-1001]
    assert captured["plan"]["limit"] == 25
    assert captured["run"]["apply"] is False
    assert captured["run"]["ai_recognition_config"] is None
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert "payload" not in result.output.lower()


def test_backfill_cli_can_use_configured_chats_while_live_resolution_is_disabled(
    tmp_path,
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_trading_settings",
        lambda *_: SimpleNamespace(
            context_resolution_enabled=False,
            context_resolution_live_chat_ids=[-1002, -1001],
        ),
    )

    def plan(_session_factory, **kwargs):
        captured.update(kwargs)
        return _empty_plan(tuple(kwargs["chat_ids"]))

    monkeypatch.setattr(
        "telegram_kol_research.cli.plan_mimo_evidence_backfill",
        plan,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_mimo_evidence_backfill",
        lambda *_args, **_kwargs: _empty_result(),
    )

    result = CliRunner().invoke(
        app,
        [
            "backfill-mimo-evidence",
            "--database-path",
            str(tmp_path / "research.db"),
            "--use-configured-context-chats",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["chat_ids"] == [-1002, -1001]


def test_backfill_cli_apply_passes_bounds_retry_and_rate_limit(
    tmp_path,
    monkeypatch,
):
    captured = {}
    config = object()
    monkeypatch.setattr(
        "telegram_kol_research.cli.load_ai_recognition_config",
        lambda *_: config,
    )

    def plan(_session_factory, **kwargs):
        captured["plan"] = kwargs
        return EvidenceBackfillPlan(
            chat_ids=tuple(kwargs["chat_ids"]),
            start_at=kwargs["start_at"],
            end_at=kwargs["end_at"],
            limit=kwargs["limit"],
            retry_failed=kwargs["retry_failed"],
            items=(),
        )

    def run(_session_factory, **kwargs):
        captured["run"] = kwargs
        return _empty_result("apply")

    monkeypatch.setattr(
        "telegram_kol_research.cli.plan_mimo_evidence_backfill",
        plan,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_mimo_evidence_backfill",
        run,
    )

    result = CliRunner().invoke(
        app,
        [
            "backfill-mimo-evidence",
            "--database-path",
            str(tmp_path / "research.db"),
            "--ai-config-path",
            str(tmp_path / "ai.yaml"),
            "--chat-id=-1001",
            "--start-at",
            "2026-07-20T08:00:00+08:00",
            "--end-at",
            "2026-07-21T08:00:00+08:00",
            "--limit",
            "50",
            "--delay-seconds",
            "1.5",
            "--retry-failed",
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["plan"]["start_at"] == datetime.fromisoformat(
        "2026-07-20T08:00:00+08:00"
    )
    assert captured["plan"]["end_at"] == datetime.fromisoformat(
        "2026-07-21T08:00:00+08:00"
    )
    assert captured["plan"]["retry_failed"] is True
    assert captured["run"]["ai_recognition_config"] is config
    assert captured["run"]["apply"] is True
    assert captured["run"]["delay_seconds"] == 1.5
