from pathlib import Path

from typer.testing import CliRunner

from telegram_kol_research.cli import app


def test_scanner_cli_is_dormant_without_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED", raising=False)
    result = CliRunner().invoke(
        app,
        ["runtime-incident-scanner", "--database-path", str(tmp_path / "db.sqlite")],
    )
    assert result.exit_code == 0
    assert '"status":"disabled"' in result.stdout
    assert not (tmp_path / "db.sqlite").exists()


def test_scanner_cli_never_bootstraps_a_missing_database(tmp_path, monkeypatch):
    database = tmp_path / "missing.db"
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY", "true")
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_SCANNER_RULES", "cancel_outcome_stale_unknown_v1"
    )
    result = CliRunner().invoke(
        app,
        ["runtime-incident-scanner", "--database-path", str(database), "--once"],
    )
    assert result.exit_code != 0
    assert not database.exists()


def test_scanner_systemd_unit_is_independent_and_has_no_secret_environment():
    root = Path(__file__).parents[1]
    unit = (root / "deploy/systemd/telegram-kol-runtime-scanner.service").read_text()
    assert "User=telegram-kol-agent" in unit
    assert "EnvironmentFile=/etc/telegram-kol-runtime-scanner.env" in unit
    assert "runtime_incident_agent.env" not in unit
    assert "InaccessiblePaths=/opt/telegram-kol-analyzer/config" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ReadWritePaths=/opt/telegram-kol-analyzer/data" in unit
    assert "Restart=on-failure" in unit


def test_scanner_installer_requires_shadow_only_and_leaves_unit_disabled():
    root = Path(__file__).parents[1]
    script = (root / "scripts/install_runtime_scanner_sidecar.sh").read_text()
    assert "TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY=true" in script
    assert "systemctl is-active --quiet" in script
    assert "systemctl is-enabled --quiet" in script
    assert "systemctl disable" in script
    assert "runtime_incident_agent.env" not in script
