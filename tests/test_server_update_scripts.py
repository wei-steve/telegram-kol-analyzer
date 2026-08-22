from __future__ import annotations

from pathlib import Path
import re
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


def test_embedded_python_helpers_are_syntax_valid():
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")
    helpers = re.findall(r"<<'PY'\n(.*?)\nPY", updater, flags=re.DOTALL)

    assert len(helpers) == 1
    for helper in helpers:
        compile(helper, "deploy/telegram-kol-update", "exec")


def test_workstation_helpers_require_only_branch_and_exact_commit():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in shell
    assert "bootstrap_server_updater.sh" in shell
    assert "[Parameter(Mandatory = $true)]" in powershell
    assert "$ExpectedCommit" in powershell
    assert "$ChangeClass" not in powershell
    assert "$LASTEXITCODE -ne 0" in powershell
    assert "Get-FileHash -Algorithm SHA256" in powershell
    assert "git -C \"$app_dir\" show" in powershell
    forbidden = (
        "CHANGE_CLASS",
        "ChangeClass",
        "live_promotion",
        "AuthorizeLivePromotion",
        "operator override",
    )
    assert all(value not in shell for value in forbidden)
    assert all(value not in powershell for value in forbidden)


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


def test_server_updater_runs_two_active_checks_before_checkout_and_install():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    first_check = script.index("\nrun_active_write_check\n")
    stop = script.index("\nstop_writer_service\n", first_check)
    second_check = script.index("\nrun_active_write_check\n", first_check + 1)
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    install = script.rindex(' -m pip install -e "$APP_DIR"')
    start = script.rindex("systemctl start telegram-kol.service")
    health = script.index("if ! verify_http_health", start)
    updater_install = script.rindex(
        'install -o root -g root -m 0755 "$stage_dir/deploy/telegram-kol-update"'
    )
    assert first_check < stop < second_check < checkout < install < start
    assert start < health < updater_install
    assert script.count("\nrun_active_write_check\n") == 2
    assert "PYTHONPATH=$stage_dir/src" in script
    assert "worktree add --detach" in script
    assert 'update-ref "refs/heads/$BRANCH" "$previous_commit" "$EXPECTED_COMMIT"' in script
    assert script.index("checkout_mutated=1") < script.rindex(
        ' -m pip install -e "$APP_DIR"'
    )
    assert "ROLLBACK FAILED; telegram-kol.service may remain stopped." in script
    assert 'if [ "$rollback_ok" -eq 1 ]; then' in script
    assert "git pull" not in script
    assert "schema_changed" in script
    assert "sqlite3" in script
    assert "os.O_EXCL" in script
    assert script.rindex("updater_installed=1") < script.index(
        'mv -f -- "$updater_candidate" "$UPDATER_PATH"'
    )


def test_server_updater_transactions_monitor_expected_head_and_timer_state():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    monitor_stop = script.index("stop_monitor_for_deployment")
    application_stop = script.index("\nstop_writer_service\n", monitor_stop)
    previous_pin = script.index(
        'sync_monitor_expected_head "$previous_commit"', monitor_stop
    )
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    health = script.index("if ! verify_http_health", checkout)
    candidate_pin = script.index(
        'sync_monitor_expected_head "$EXPECTED_COMMIT"', health
    )
    restore = script.index("restore_monitor_timer_state", candidate_pin)

    assert monitor_stop < previous_pin < application_stop < checkout
    assert checkout < health < candidate_pin < restore
    assert 'MONITOR_TIMER="telegram-kol-monitor.timer"' in script
    assert '"telegram-kol-monitor-test-notification.service"' in script
    assert 'MONITOR_ENV_FILE="/etc/telegram-kol-monitor.env"' in script
    assert "TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=" in script
    assert "git reset" not in script
    assert "git push" not in script


def test_server_updater_detects_schema_changes_only_by_git_paths():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert 'git -C "$APP_DIR" diff --quiet' in script
    assert "src/telegram_kol_research/models.py" in script
    assert "src/telegram_kol_research/db.py" in script
    assert "migrations" in script
    assert "CHANGE_CLASS" not in script
    assert "fingerprint" not in script


def test_bootstrap_installs_only_sha_verified_helper_from_expected_commit():
    script = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(encoding="utf-8")

    assert "git -C \"$app_dir\" show" in script
    assert "sha256sum" in script
    assert "UPDATER_SHA256" in script
    assert "chmod 0700" in script
    assert "mktemp -d" in script
    assert "install -o root" not in script
    assert script.index("sha256sum") < script.index('bash "$temporary/updater"')


def test_server_updater_refuses_unpinned_or_mismatched_remote_commit():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in script
    assert "CHANGE_CLASS" not in script
    assert 'remote_head="$(git rev-parse FETCH_HEAD)"' in script
    assert 'if [ "$remote_head" != "$EXPECTED_COMMIT" ]' in script


def test_schema_dry_run_uses_persistent_disk_and_is_always_removed():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert 'BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/data/backups}"' in script
    assert '$BACKUP_DIR/schema-dry-run-' in script
    assert "cleanup_schema_dry_run" in script
    assert script.count("cleanup_schema_dry_run") >= 3
    assert "PREFLIGHT_DIR" not in script
