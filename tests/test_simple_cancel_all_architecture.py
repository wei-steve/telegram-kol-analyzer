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
    ):
        assert not (
            ROOT / "src" / "telegram_kol_research" / retired_module
        ).exists()
