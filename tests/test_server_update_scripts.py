from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_update_scripts_are_syntax_valid():
    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/server_git_update.sh")],
        check=True,
    )
    subprocess.run(
        ["bash", "-n", str(ROOT / "deploy/telegram-kol-update")],
        check=True,
    )
    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/bootstrap_server_updater.sh")],
        check=True,
    )


def test_workstation_helpers_require_commit_and_change_class():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in shell
    assert 'CHANGE_CLASS="${CHANGE_CLASS:?' in shell
    assert "bootstrap_server_updater.sh" in shell
    assert "[Parameter(Mandatory = $true)]" in powershell
    assert "$ExpectedCommit" in powershell
    assert "$ChangeClass" in powershell
    assert "$LASTEXITCODE -ne 0" in powershell
    assert "Get-FileHash -Algorithm SHA256" in powershell
    assert "git -C \"$app_dir\" show" in powershell


def test_shell_workstation_helper_preserves_safe_transport_checks():
    script = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'SERVER="${SERVER:-root@43.167.220.225}"' in script
    assert 'KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"' in script
    assert 'BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"' in script
    assert "command -v ssh" in script
    assert '[ ! -r "$KEY_PATH" ]' in script
    assert "bootstrap_server_updater.sh" in script
    assert ",," not in script


def test_deployment_docs_keep_both_workstation_helpers_visible():
    deployment = (ROOT / "docs/server-deployment.md").read_text(encoding="utf-8")
    handoff = (ROOT / "docs/migration-handoff.md").read_text(encoding="utf-8")

    assert "./scripts/server_git_update.sh" in deployment
    assert "server_git_update.ps1" in deployment
    assert "./scripts/server_git_update.sh" in handoff


def test_server_updater_runs_preflight_before_checkout_install_or_restart():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    preflight = script.index("deployment-preflight")
    verify = script.index("verify-deployment-preflight")
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    install = script.index("python -m pip install -e .")
    stop = script.rindex("systemctl stop telegram-kol.service")
    start = script.rindex("systemctl start telegram-kol.service")
    assert preflight < stop < verify < checkout < install < start
    assert script.count("run_preflight") >= 3
    assert "PYTHONPATH=$stage_dir/src" in script
    assert "worktree add --detach" in script
    assert 'update-ref "refs/heads/$BRANCH" "$previous_commit" "$EXPECTED_COMMIT"' in script
    assert script.index("checkout_mutated=1") < script.index("python -m pip install -e .")
    assert "ROLLBACK FAILED; telegram-kol.service remains stopped." in script
    assert 'if [ "$rollback_ok" -eq 1 ]; then' in script
    assert "git pull" not in script
    assert "schema_compatible" in script
    assert "sqlite3" in script
    assert "os.O_EXCL" in script
    assert "BLOCK" in script


def test_bootstrap_installs_only_sha_verified_helper_from_expected_commit():
    script = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(encoding="utf-8")

    assert "git -C \"$app_dir\" show" in script
    assert "sha256sum" in script
    assert "UPDATER_SHA256" in script
    assert "__EMPTY__" in script
    assert script.index("sha256sum") < script.index("/usr/local/bin/telegram-kol-update")


def test_server_updater_refuses_unpinned_or_mismatched_remote_commit():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in script
    assert 'CHANGE_CLASS="${CHANGE_CLASS:?' in script
    assert 'remote_head="$(git rev-parse FETCH_HEAD)"' in script
    assert 'if [ "$remote_head" != "$EXPECTED_COMMIT" ]' in script


def test_schema_dry_run_uses_persistent_disk_and_is_always_removed():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert '$APP_DIR/data/backups/schema-dry-run-' in script
    assert "cleanup_schema_dry_run" in script
    assert script.count("cleanup_schema_dry_run") >= 3
    assert '$PREFLIGHT_DIR/schema-dry-run-' not in script
