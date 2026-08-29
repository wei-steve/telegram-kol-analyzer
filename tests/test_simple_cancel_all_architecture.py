from pathlib import Path

from typer.testing import CliRunner

from telegram_kol_research.cli import app


ROOT = Path(__file__).resolve().parents[1]


def test_one_time_maintenance_protocol_is_removed():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for retired_command in (
        "seed-entry-authority",
        "drain-one",
        "bootstrap-control",
    ):
        assert retired_command not in result.stdout

    for retired_module in (
        "immutable_control_bootstrap.py",
        "deepcoin_maintenance_actions.py",
        "deepcoin_maintenance_manifest.py",
        "entry_authority_seed.py",
        "reviewed_pending_entry_cancel.py",
        "maintenance_runtime_guard.py",
    ):
        assert not (
            ROOT / "src" / "telegram_kol_research" / retired_module
        ).exists()

    authority_source = (
        ROOT
        / "src"
        / "telegram_kol_research"
        / "entry_revision_exchange_authority.py"
    ).read_text(encoding="utf-8")
    assert "reviewed_pending_entry_cancel" not in authority_source


def test_single_manual_reconciliation_command_is_exposed():
    result = CliRunner().invoke(
        app,
        ["finalize-cancelled-pending-entries", "--help"],
    )

    assert result.exit_code == 0
    assert "--database-path" in result.stdout
    assert "--backup-path" in result.stdout
    assert "--expected-fingerprint" in result.stdout
    assert "--apply" in result.stdout
    assert "--confirmation-token" not in result.stdout
    assert "--order-id" not in result.stdout
