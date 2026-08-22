import json
from types import SimpleNamespace

from typer.testing import CliRunner

import telegram_kol_research.cli as cli_module


def test_worker_command_reconcile_defaults_to_read_only_dry_run(tmp_path, monkeypatch):
    calls = []

    def fake_reconcile(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return SimpleNamespace(
            command_id="command-7",
            outcome="evidence_incomplete",
            reason="external snapshot incomplete",
            applied=False,
        )

    monkeypatch.setattr(
        cli_module,
        "reconcile_worker_command_by_id",
        fake_reconcile,
        raising=False,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "worker-command-reconcile",
            "--database-path",
            str(tmp_path / "commands.db"),
            "--command-id",
            "command-7",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert payload["exchange_write_count"] == 0
    assert calls[0][1]["apply_confirmed"] is False


def test_worker_command_reconcile_apply_flag_is_explicit(tmp_path, monkeypatch):
    calls = []

    def fake_reconcile(_session_factory, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            command_id="command-7",
            outcome="confirmed_succeeded",
            reason="exact identity chain",
            applied=True,
        )

    monkeypatch.setattr(
        cli_module,
        "reconcile_worker_command_by_id",
        fake_reconcile,
        raising=False,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "worker-command-reconcile",
            "--database-path",
            str(tmp_path / "commands.db"),
            "--command-id",
            "command-7",
            "--apply-confirmed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "command_id": "command-7",
            "deepcoin_client_factory": cli_module.build_deepcoin_client_from_env,
            "apply_confirmed": True,
        }
    ]
