from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_shell_server_update_helper_has_safe_defaults_and_preflight_checks():
    script = (PROJECT_ROOT / "scripts" / "server_git_update.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert 'SERVER="${SERVER:-root@43.167.220.225}"' in script
    assert 'KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"' in script
    assert 'BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"' in script
    assert "command -v ssh" in script
    assert '[ ! -r "$KEY_PATH" ]' in script
    assert "ssh -i \"$KEY_PATH\" \"$SERVER\"" in script
    assert "/usr/local/bin/telegram-kol-update" in script
    assert "BRANCH=" in script


def test_server_update_docs_cover_shell_and_powershell_helpers():
    deployment = (PROJECT_ROOT / "docs" / "server-deployment.md").read_text(
        encoding="utf-8"
    )
    handoff = (PROJECT_ROOT / "docs" / "migration-handoff.md").read_text(
        encoding="utf-8"
    )

    assert "./scripts/server_git_update.sh" in deployment
    assert "server_git_update.ps1" in deployment
    assert "./scripts/server_git_update.sh" in handoff
