from pathlib import Path

from telegram_kol_research.models import PositionProtectionHealthObservation


ROOT = Path(__file__).resolve().parents[1]


def test_migration_docs_exist_and_prohibit_secret_transfer():
    for name in ("migration-handoff.md", "mac-mini-migration.md", "remote-access.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8").lower()
        assert "production" in text
        assert "secret" in text
    assert "do not open public ports" in (
        ROOT / "docs" / "remote-access.md"
    ).read_text(encoding="utf-8").lower()


def test_development_template_is_non_secret_and_local_copy_is_ignored():
    template = (ROOT / "config" / "development.env.example").read_text(encoding="utf-8")
    assert "DEVELOPMENT ONLY" in template
    assert "TELEGRAM_API_ID=" in template
    assert "TELEGRAM_API_HASH=" in template
    assert "DEEP" not in template.upper()
    assert "config/development.env" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    for line in template.splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("=")


def test_windows_preflight_is_read_only_and_redacts_matches():
    script = (ROOT / "scripts" / "preflight_mac_migration.ps1").read_text(encoding="utf-8")
    assert "ls-files" in script
    assert "status --short" in script
    assert "Match.Value" not in script
    assert "Suspected credential:" in script
    assert "Remove-Item" not in script
    assert "Copy-Item" not in script


def test_mac_bootstrap_checks_prerequisites_without_installing_or_network_calls():
    script = (ROOT / "scripts" / "bootstrap_mac_dev.sh").read_text(encoding="utf-8")
    assert '[[ "$(uname -s)" == "Darwin" ]]' in script
    assert "xcode-select -p" in script
    assert "command -v git" in script
    assert "command -v python3" in script
    assert "command -v uv" in script
    assert "ls-files --error-unmatch config/development.env" in script
    assert "curl " not in script
    assert "brew install" not in script
    assert 'run "uv" "sync"' not in script


def test_protection_health_observation_model_keeps_bounded_evidence_fields():
    table = PositionProtectionHealthObservation.__table__

    assert table.c.evidence_fingerprint.type.length == 64
    assert table.c.exchange_snapshot_fingerprint.type.length == 64
    assert str(table.c.source_incident_ids_json.type) == "TEXT"
    assert str(table.c.summary_json.type) == "TEXT"
    assert table.c.observed_at.nullable is False
