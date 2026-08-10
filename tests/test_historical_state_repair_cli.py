from __future__ import annotations

import json

from typer.testing import CliRunner

from telegram_kol_research import cli
from telegram_kol_research.historical_state_repair import (
    HistoricalStateRepairAction,
    HistoricalStateRepairPlan,
    HistoricalStateRepairResult,
)


def _plan(*, conflicts=()):
    return HistoricalStateRepairPlan(
        schema_version=1,
        mode="dry_run",
        database_fingerprint="a" * 64,
        exchange_fingerprint="b" * 64,
        fingerprint="c" * 64,
        confirmation_token="d" * 16,
        actions=(
            HistoricalStateRepairAction(
                kind="source_deletion_exit",
                target_id=1,
                reason_code="non_strategy_or_unlinked",
            ),
        ),
        exclusions=(),
        conflicts=tuple(conflicts),
    )


def test_repair_historical_state_convergence_defaults_to_json_dry_run(
    tmp_path, monkeypatch
):
    plan = _plan()
    snapshot = object()
    monkeypatch.setattr(cli, "create_existing_session_factory", lambda _path: object())
    monkeypatch.setattr(cli, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli,
        "load_deepcoin_execution_reconciliation_snapshot_read_only",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        cli,
        "build_historical_state_repair_plan",
        lambda *_args, **_kwargs: plan,
        raising=False,
    )
    applied = []
    monkeypatch.setattr(
        cli,
        "apply_historical_state_repair_plan",
        lambda *_args, **_kwargs: applied.append(kwargs),
        raising=False,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "repair-historical-state-convergence",
            "--database-path",
            str(tmp_path / "research.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "DRY RUN"
    payload = json.loads("\n".join(result.output.splitlines()[1:]))
    assert payload["action_count"] == 1
    assert payload["fingerprint"] == plan.fingerprint
    assert payload["confirmation_token"] == plan.confirmation_token
    assert applied == []


def test_repair_historical_state_convergence_apply_requires_all_three_gates(
    tmp_path, monkeypatch
):
    plan = _plan()
    monkeypatch.setattr(cli, "create_existing_session_factory", lambda _path: object())
    monkeypatch.setattr(cli, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli,
        "load_deepcoin_execution_reconciliation_snapshot_read_only",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli,
        "build_historical_state_repair_plan",
        lambda *_args, **_kwargs: plan,
        raising=False,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "repair-historical-state-convergence",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "expected-fingerprint" in result.output
    assert "expected-action-count" in result.output
    assert "confirmation-token" in result.output


def test_repair_historical_state_convergence_applies_exact_plan(tmp_path, monkeypatch):
    plan = _plan()
    snapshot = object()
    monkeypatch.setattr(cli, "create_existing_session_factory", lambda _path: object())
    monkeypatch.setattr(cli, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli,
        "load_deepcoin_execution_reconciliation_snapshot_read_only",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        cli,
        "build_historical_state_repair_plan",
        lambda *_args, **_kwargs: plan,
        raising=False,
    )
    calls = []

    def apply(*_args, **kwargs):
        calls.append(kwargs)
        return HistoricalStateRepairResult(
            fingerprint=plan.fingerprint,
            applied_actions=1,
            audit_event_id=9,
        )

    monkeypatch.setattr(
        cli,
        "apply_historical_state_repair_plan",
        apply,
        raising=False,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "repair-historical-state-convergence",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
            "--expected-fingerprint",
            plan.fingerprint,
            "--expected-action-count",
            "1",
            "--confirmation-token",
            plan.confirmation_token,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Applied 1 historical repair action(s)." in result.output
    assert calls == [
        {
            "snapshot_loader": calls[0]["snapshot_loader"],
            "expected_fingerprint": plan.fingerprint,
            "expected_action_count": 1,
            "confirmation_token": plan.confirmation_token,
            "applied_at": calls[0]["applied_at"],
        }
    ]
    assert calls[0]["snapshot_loader"]() is snapshot
