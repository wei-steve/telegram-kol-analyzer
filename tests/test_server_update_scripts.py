from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CACHE_REPAIR_RUNBOOK = (
    ROOT / "docs" / "runbooks" / "deepcoin-contract-cache-ownership-repair.md"
)
CACHE_REPAIR_PLAN = (
    ROOT / "docs" / "plans" / "2026-08-27-deepcoin-contract-cache-ownership-repair.md"
)
CACHE_REPAIR_STATUS = (
    ROOT / "docs" / "deepcoin-contract-cache-ownership-repair-status.md"
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


def test_embedded_python_helpers_are_syntax_valid():
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")
    helpers = re.findall(r"<<'PY'\n(.*?)\nPY", updater, flags=re.DOTALL)

    assert len(helpers) == 1
    for helper in helpers:
        compile(helper, "deploy/telegram-kol-update", "exec")


def test_workstation_helpers_require_exact_commit_and_auto_trade_expectation():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in shell
    assert 'EXPECTED_AUTO_TRADE_STATE="${EXPECTED_AUTO_TRADE_STATE:?' in shell
    assert '[[ "$EXPECTED_AUTO_TRADE_STATE" =~ ^(enabled|disabled)$ ]]' in shell
    assert "bootstrap_server_updater.sh" in shell
    assert "[Parameter(Mandatory = $true)]" in powershell
    assert "$ExpectedCommit" in powershell
    assert '[ValidateSet("enabled", "disabled")]' in powershell
    assert "$ExpectedAutoTradeState" in powershell
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


def test_contract_cache_repair_docs_define_closed_freeze_and_restore_contract():
    deployment = (ROOT / "docs/server-deployment.md").read_text(encoding="utf-8")
    runbook = CACHE_REPAIR_RUNBOOK.read_text(encoding="utf-8")
    implementation = CACHE_REPAIR_PLAN.read_text(encoding="utf-8")
    combined = deployment + "\n" + runbook + "\n" + implementation

    for required in (
        "telegram-kol-worker:telegram-kol-runtime",
        "0660",
        "u:telegram-kol-agent:---",
        "regular file",
        "st_nlink == 1",
        "sticky 1777",
        "telegram-kol-worker-prepare-contract-cache",
        "EXPECTED_AUTO_TRADE_STATE=disabled",
        "EXPECTED_AUTO_TRADE_STATE=enabled",
        "永不重放",
        "代码回滚不自动恢复交易设置",
        "server evidence file",
        "freeze_raw_message_id",
        "restore_raw_message_id",
        "MAX(raw_messages.id)",
        "raw_messages.id > restore_raw_message_id",
    ):
        assert required in combined
    assert "递归 `chown`" in combined
    assert "递归 `chmod`" in combined
    for heading in (
        "只读 preflight",
        "冻结写入",
        "exact-SHA 部署与权限/刷新验证",
        "单独恢复",
    ):
        assert heading in runbook
    assert "telegram-kol.service stopped" in runbook
    assert "active_write_count=0" in runbook
    assert "记录冻结后的首个自然到达 `raw_message_id` 作为 future-only 水位" not in runbook
    for required in (
        "已识别可迁移旧版漂移",
        "root owner 与缺失的 Agent deny ACL",
        "unknown owner/type/link/group/mode/ACL",
        "`legacy_capability_absent`",
        "HTTP 404",
        "401/403",
        "有界历史覆盖",
        "100-row",
        "完整候选合同",
        "冻结时动态记录",
    ):
        assert required in combined
    assert "`attempted_exchange_write=0`" in runbook
    assert "4 条旧的 zero-write 非终态执行合同" in runbook
    task12_start = implementation.index("### Task 12: 冻结前只读门禁")
    task13_start = implementation.index("### Task 13: 冻结、部署并保持关闭")
    task12 = implementation[task12_start:task13_start]
    assert "失败只因 owner 漂移" not in task12
    assert "已知的 15 条历史" not in runbook
    assert "历史 15 条拒绝" not in implementation[task12_start:]
    restore_section = runbook.index("## 4. 单独恢复")
    enabled_updater = runbook.index("EXPECTED_AUTO_TRADE_STATE=enabled", restore_section)
    restore_watermark = runbook.index("restore_raw_message_id", restore_section)
    enable_setting = runbook.index("然后重新 GET 当前完整 settings", restore_section)
    assert enabled_updater < restore_watermark < enable_setting


def test_contract_cache_status_records_version_aware_task12_handoff():
    status = CACHE_REPAIR_STATUS.read_text(encoding="utf-8")

    assert "task12_refusal_baseline_count: 16" in status
    assert "task12_observed_max_raw_message_id: 13530" in status
    assert "task12_gate: failed_closed" in status
    assert "recognized migratable legacy drift" in status
    assert "bounded 100-row history coverage" in status
    assert "health HTTP error remains unresolved" in status


def test_server_updater_runs_two_active_checks_before_checkout_and_install():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    first_check = script.index("\nrun_active_write_check\n")
    stop = script.index("\nstop_writer_service\n", first_check)
    second_check = script.index("\nrun_active_write_check\n", first_check + 1)
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    install = script.rindex(' -m pip install -e "$APP_DIR"')
    start = script.rindex("\nif ! start_managed_services; then")
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
    assert "ROLLBACK FAILED; managed runtime units may remain stopped." in script
    assert 'if [ "$rollback_ok" -eq 1 ]; then' in script
    assert "git pull" not in script
    assert "schema_changed" in script
    assert "sqlite3" in script
    assert "os.O_EXCL" in script
    assert script.rindex("updater_installed=1") < script.index(
        'mv -f -- "$updater_candidate" "$UPDATER_PATH"'
    )


def test_server_updater_resolves_exactly_one_complete_runtime_topology():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert "resolve_managed_topology()" in script
    assert 'MONOLITH_UNITS=("telegram-kol.service")' in script
    assert "SPLIT_UNITS=(" in script
    for unit in (
        "telegram-kol-ingest.service",
        "telegram-kol-worker.service",
        "telegram-kol-web.service",
    ):
        assert f'"{unit}"' in script
    assert "Ambiguous or incomplete runtime topology" in script
    assert script.index("resolve_managed_topology") < script.index(
        "exec 9>\"$LOCK_PATH\""
    )


def test_split_topology_stop_start_and_rollback_orders_are_explicit():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert re.search(
        r'SPLIT_STOP_UNITS=\(\s*"telegram-kol-ingest\.service"\s*'
        r'"telegram-kol-web\.service"\s*"telegram-kol-worker\.service"\s*\)',
        script,
    )
    assert re.search(
        r'SPLIT_START_UNITS=\(\s*"telegram-kol-worker\.service"\s*'
        r'"telegram-kol-web\.service"\s*"telegram-kol-ingest\.service"\s*\)',
        script,
    )
    assert 'for unit in "${managed_stop_units[@]}"' in script
    assert 'for unit in "${managed_start_units[@]}"' in script
    assert script.count('for unit in "${managed_start_units[@]}"') >= 2


def test_workstation_bootstraps_require_the_dual_topology_updater_contract():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(
        encoding="utf-8"
    )

    assert 'UPDATER_TOPOLOGY_CONTRACT="dual-v1"' in shell
    assert "UPDATER_TOPOLOGY_CONTRACT" in bootstrap
    assert "resolve_managed_topology()" in bootstrap
    assert '$topologyContract = "dual-v1"' in powershell
    assert "resolve_managed_topology()" in powershell


def test_workstation_bootstraps_require_worker_cache_artifact_transaction():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(
        encoding="utf-8"
    )

    assert 'UPDATER_CACHE_ARTIFACT_CONTRACT="worker-cache-v1"' in shell
    assert "UPDATER_CACHE_ARTIFACT_CONTRACT" in bootstrap
    assert "install_worker_cache_artifacts" in bootstrap
    assert "telegram-kol-worker-prepare-contract-cache" in bootstrap
    assert '$cacheArtifactContract = "worker-cache-v1"' in powershell
    assert "install_worker_cache_artifacts" in powershell


def test_workstation_bootstraps_require_monitor_expectation_transaction():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(
        encoding="utf-8"
    )

    assert 'UPDATER_MONITOR_EXPECTATION_CONTRACT="monitor-expectation-v1"' in shell
    assert "UPDATER_MONITOR_EXPECTATION_CONTRACT" in bootstrap
    assert "sync_monitor_expectations" in bootstrap
    assert "install_monitor_service_artifact" in bootstrap
    assert '$monitorExpectationContract = "monitor-expectation-v1"' in powershell
    assert "sync_monitor_expectations" in powershell
    assert "install_monitor_service_artifact" in powershell


def test_server_updater_transactions_worker_cache_helper_and_unit():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert (
        "WORKER_CACHE_HELPER_PATH=/usr/local/libexec/"
        "telegram-kol-worker-prepare-contract-cache"
    ) in script
    assert (
        "WORKER_UNIT_PATH=/etc/systemd/system/telegram-kol-worker.service" in script
    )
    assert "install_worker_cache_artifacts()" in script
    assert "restore_worker_cache_artifacts()" in script
    assert "worker_cache_artifacts_installed=1" in script
    assert 'if [ "$managed_topology" = "split" ]; then' in script
    assert 'systemctl daemon-reload' in script
    pip_install = script.rindex(' -m pip install -e "$APP_DIR"')
    artifact_install = script.index("install_worker_cache_artifacts", pip_install)
    worker_start = script.index("\nif ! start_managed_services; then", artifact_install)
    assert pip_install < artifact_install < worker_start


def test_server_updater_transactions_monitor_expected_head_and_timer_state():
    script = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    monitor_stop = script.index("stop_monitor_for_deployment")
    application_stop = script.index("\nstop_writer_service\n", monitor_stop)
    previous_pin = script.index(
        'sync_monitor_expectations "$previous_commit"', monitor_stop
    )
    checkout = script.index('git merge --ff-only "$EXPECTED_COMMIT"')
    health = script.index("if ! verify_http_health", checkout)
    candidate_pin = script.index(
        'sync_monitor_expectations "$EXPECTED_COMMIT"', health
    )
    restore = script.index("restore_monitor_timer_state", candidate_pin)

    assert monitor_stop < previous_pin < application_stop < checkout
    assert checkout < health < candidate_pin < restore
    assert 'MONITOR_TIMER="telegram-kol-monitor.timer"' in script
    assert '"telegram-kol-monitor-test-notification.service"' in script
    assert 'MONITOR_ENV_FILE="/etc/telegram-kol-monitor.env"' in script
    assert "TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=" in script
    assert "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=" in script
    assert 'EXPECTED_AUTO_TRADE_STATE="${EXPECTED_AUTO_TRADE_STATE:?' in script
    assert '^(enabled|disabled)$' in script
    assert "install_monitor_service_artifact()" in script
    assert "restore_monitor_artifacts()" in script
    assert "MONITOR_SERVICE_PATH=/etc/systemd/system/telegram-kol-monitor.service" in script
    assert (
        "MONITOR_DIAGNOSTIC_SERVICE_PATH=/etc/systemd/system/"
        "telegram-kol-monitor-diagnostic.service"
    ) in script
    assert (
        "MONITOR_TEST_NOTIFICATION_SERVICE_PATH=/etc/systemd/system/"
        "telegram-kol-monitor-test-notification.service"
    ) in script
    assert "telegram-kol-monitor-diagnostic.service" in script
    assert "telegram-kol-monitor-test-notification.service" in script
    assert "auto_trade" not in script.lower().replace("expected_auto_trade", "")
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
