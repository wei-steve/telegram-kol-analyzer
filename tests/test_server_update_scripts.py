from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_GATE_DOCS = (
    "docs/runbook.md",
    "docs/server-deployment.md",
    "docs/migration-handoff.md",
    "docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history-design.md",
    "docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history.md",
)
WORK_CLASSIFICATIONS = (
    "in_flight_write",
    "unknown_outcome",
    "restart_safe_wait",
    "historical_residue",
    "terminal",
    "malformed",
)


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


def test_deployment_documentation_records_two_phase_evidence_policy():
    for relative_path in EVIDENCE_GATE_DOCS:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(text.lower().split())
        assert "Evidence-based two-phase deployment gate" in text
        assert all(classification in text for classification in WORK_CLASSIFICATIONS)
        assert "phase a" in normalized
        assert "phase b" in normalized
        assert "unknown outcomes block regardless of age" in normalized
        assert "restart-safe/history is warn only for code/schema" in normalized
        assert "candidate updater" in normalized
        assert "not installed" in normalized
        assert "push approval and deployment approval are separate" in normalized
        assert "fresh_active_exchange_work" not in text


def test_server_updater_runs_two_bound_phases_before_mutation():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    candidate_extract = script.index("worktree add --detach")
    preliminary = script.index('run_preflight_collect "preliminary"')
    stop = script.rindex("\nstop_writer_service\n")
    final_backup = script.index("create_schema_evidence final")
    final_collect = script.index('run_preflight_collect "final"')
    final_verify = script.index('run_preflight_verify "final"')
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    install = script.index("python -m pip install -e .")
    updater_install = script.index('deploy/telegram-kol-update "$updater_candidate"')
    start = script.rindex("systemctl start telegram-kol.service")
    assert candidate_extract < preliminary < stop
    assert stop < final_backup < final_collect
    assert final_collect < final_verify < checkout < install < updater_install < start
    assert "PYTHONPATH=$stage_dir/src" in script
    assert "telegram_kol_research.deployment_preflight_cli" in script
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


def test_bootstrap_executes_sha_verified_temporary_without_installing_it():
    script = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(encoding="utf-8")

    assert "git -C \"$app_dir\" show" in script
    assert "sha256sum" in script
    assert "UPDATER_SHA256" in script
    assert "__EMPTY__" in script
    execute = script.rindex('"$temporary"')
    assert script.index("sha256sum") < execute
    assert "/usr/local/bin/telegram-kol-update" not in script[:execute]


def test_powershell_bootstrap_also_executes_temporary_without_installing_it():
    script = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    execute = script.rindex('"$temporary"')
    assert "/usr/local/bin/telegram-kol-update" not in script[:execute]


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
