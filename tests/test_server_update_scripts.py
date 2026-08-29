from __future__ import annotations

import base64
import io
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tarfile

import pytest


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
CACHE_REPAIR_VERSION_AWARE_DESIGN = (
    ROOT
    / "docs"
    / "plans"
    / "2026-08-27-deepcoin-contract-cache-task12-version-aware-gates-design.md"
)
CACHE_REPAIR_VERSION_AWARE_PLAN = (
    ROOT
    / "docs"
    / "plans"
    / "2026-08-27-deepcoin-contract-cache-task12-version-aware-gates.md"
)


def _write_action_manifest(path: Path, action: str) -> None:
    payload = {
        "action": action,
        "risk_level": "L1",
        "components": [],
        "requires_restart": False,
        "schema_changed": False,
        "production_data_mutation": False,
        "exchange_write_semantics_changed": False,
        "authority_changed": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_workstation_shell_requires_one_explicit_action(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/server_git_update.sh")],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "plan|push|stage|activate" in result.stderr


def test_workstation_plan_is_local_and_does_not_advance_to_ssh(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "plan.json"
    _write_action_manifest(manifest, "local")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_marker = tmp_path / "ssh-called"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!/usr/bin/env bash\ntouch {ssh_marker!s}\nexit 99\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "scripts/server_git_update.sh"), "plan"],
        cwd=ROOT,
        env={
            **os.environ,
            "ACTION_MANIFEST": str(manifest),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "local"
    assert not ssh_marker.exists()


def test_bootstrap_rejects_ssh_option_injection_before_transport(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "activate.json"
    manifest.write_text("{}", encoding="utf-8")
    key = tmp_path / "key"
    key.write_text("test-only", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "ssh-called"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 99\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap_server_updater.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOYMENT_ACTION": "activate",
            "SERVER": "-oProxyCommand=touch-injected",
            "KEY_PATH": str(key),
            "EXPECTED_COMMIT": "1" * 40,
            "ROLLBACK_COMMIT": "2" * 40,
            "ACTION_MANIFEST": str(manifest),
            "ACTIVATION_AUTHORIZATION": "/run/authorization.json",
            "ACTIVATION_AUTHORIZATION_CONSUMED": "/run/authorization.consumed",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Invalid SERVER" in result.stderr
    assert not marker.exists()


def test_stage_transport_is_one_ssh_call_with_two_line_payload(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "stage.json"
    _write_action_manifest(manifest, "stage")
    key = tmp_path / "key"
    key.write_text("test-only", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_path = tmp_path / "ssh-args"
    stdin_path = tmp_path / "ssh-stdin"
    calls_path = tmp_path / "ssh-calls"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'x\\n' >> {calls_path!s}\n"
        f"printf '%s\\0' \"$@\" > {args_path!s}\n"
        f"while IFS= read -r line; do printf '%s\\n' \"$line\"; done > {stdin_path!s}\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap_server_updater.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOYMENT_ACTION": "stage",
            "SERVER": "root@127.0.0.1",
            "KEY_PATH": str(key),
            "EXPECTED_COMMIT": expected_commit,
            "ACTION_MANIFEST": str(manifest),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls_path.read_text(encoding="utf-8").splitlines() == ["x"]
    arguments = args_path.read_bytes().rstrip(b"\0").decode().split("\0")
    assert arguments[:3] == ["-i", str(key), "root@127.0.0.1"]
    assert len(arguments) == 4
    assert "'stage'" in arguments[3]
    payload = stdin_path.read_text(encoding="utf-8").splitlines()
    assert len(payload) == 2
    assert base64.b64decode(payload[0]) == manifest.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload[1]))) as archive:
        assert "deploy/telegram-kol-stage" in archive.getnames()


def test_activate_transport_failure_stops_after_one_ssh_call(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "activate.json"
    manifest.write_text(
        json.dumps(
            {
                "action": "activate",
                "risk_level": "L1",
                "components": ["web"],
                "requires_restart": True,
                "schema_changed": False,
                "production_data_mutation": False,
                "exchange_write_semantics_changed": False,
                "authority_changed": False,
            }
        ),
        encoding="utf-8",
    )
    key = tmp_path / "key"
    key.write_text("test-only", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_path = tmp_path / "ssh-args"
    stdin_path = tmp_path / "ssh-stdin"
    calls_path = tmp_path / "ssh-calls"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'x\\n' >> {calls_path!s}\n"
        f"printf '%s\\0' \"$@\" > {args_path!s}\n"
        f"while IFS= read -r line; do printf '%s\\n' \"$line\"; done > {stdin_path!s}\n"
        "exit 17\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap_server_updater.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOYMENT_ACTION": "activate",
            "SERVER": "root@127.0.0.1",
            "KEY_PATH": str(key),
            "EXPECTED_COMMIT": "1" * 40,
            "ROLLBACK_COMMIT": "2" * 40,
            "ACTION_MANIFEST": str(manifest),
            "ACTIVATION_AUTHORIZATION": "/run/authorization.json",
            "ACTIVATION_AUTHORIZATION_CONSUMED": "/run/authorization.consumed",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 17
    assert calls_path.read_text(encoding="utf-8").splitlines() == ["x"]
    arguments = args_path.read_bytes().rstrip(b"\0").decode().split("\0")
    assert len(arguments) == 4
    assert "'activate'" in arguments[3]
    payload = stdin_path.read_text(encoding="utf-8").splitlines()
    assert len(payload) == 2
    assert payload[1] == "-"


def test_activate_transport_preserves_payload_for_executed_remote_command(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "activate.json"
    manifest.write_text(
        json.dumps(
            {
                "action": "activate",
                "risk_level": "L1",
                "components": ["web"],
                "requires_restart": True,
                "schema_changed": False,
                "production_data_mutation": False,
                "exchange_write_semantics_changed": False,
                "authority_changed": False,
            }
        ),
        encoding="utf-8",
    )
    key = tmp_path / "key"
    key.write_text("test-only", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    remote_tmp = tmp_path / "remote-tmp"
    capture = tmp_path / "captured-manifest"
    rollback_commit = "2" * 40
    release_root = tmp_path / "releases"
    dispatcher = release_root / rollback_commit / "deploy/telegram-kol-update"
    dispatcher.parent.mkdir(parents=True)
    dispatcher.write_text(
        "#!/usr/bin/env bash\n"
        "cp \"$ACTION_MANIFEST\" \"$FAKE_MANIFEST_CAPTURE\"\n"
        "printf '{\"status\":\"activated-test-double\"}\\n'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\nremote_command=\"${!#}\"\nexec /bin/bash -c \"$remote_command\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/usr/bin/env bash\nmkdir -p \"$FAKE_REMOTE_TMP\"\nprintf '%s\\n' \"$FAKE_REMOTE_TMP\"\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)
    fake_sha256sum = fake_bin / "sha256sum"
    fake_sha256sum.write_text(
        "#!/usr/bin/env bash\nshasum -a 256 \"$1\"\n",
        encoding="utf-8",
    )
    fake_sha256sum.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap_server_updater.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOYMENT_ACTION": "activate",
            "SERVER": "root@127.0.0.1",
            "KEY_PATH": str(key),
            "EXPECTED_COMMIT": "1" * 40,
            "ROLLBACK_COMMIT": rollback_commit,
            "ACTION_MANIFEST": str(manifest),
            "ACTIVATION_AUTHORIZATION": str(tmp_path / "authorization.json"),
            "ACTIVATION_AUTHORIZATION_CONSUMED": str(
                tmp_path / "authorization.consumed"
            ),
            "SOURCE_REPO": str(tmp_path / "source"),
            "RELEASE_ROOT": str(release_root),
            "SERVICE_DROPIN_ROOT": str(tmp_path / "dropins"),
            "DATABASE_PATH": str(tmp_path / "research.db"),
            "FAKE_REMOTE_TMP": str(remote_tmp),
            "FAKE_MANIFEST_CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "activated-test-double"}
    assert capture.read_bytes() == manifest.read_bytes()


def test_workstation_and_server_helpers_have_no_legacy_or_trading_action() -> None:
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    for source in (shell, powershell, bootstrap, updater):
        assert "EXPECTED_AUTO_TRADE_STATE" not in source
        assert "legacy)" not in source
    assert "plan|push|stage|activate" in shell
    assert 'ValidateSet("plan", "push", "stage", "activate")' in powershell
    assert "stage|activate" in bootstrap
    assert "git push" not in bootstrap
    assert "DEPLOYMENT_ACTION:-" not in updater
    assert "APP_DIR=" not in updater
    assert "systemctl" not in updater


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
    subprocess.run(
        ["bash", "-n", str(ROOT / "deploy/telegram-kol-activate")],
        check=True,
    )
    subprocess.run(
        [
            "python3",
            "-m",
            "py_compile",
            str(ROOT / "deploy/telegram-kol-stage"),
        ],
        check=True,
    )


def test_updater_is_an_activate_only_immutable_dispatcher():
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert 'DEPLOYMENT_ACTION="${DEPLOYMENT_ACTION:?' in updater
    assert 'if [ "$DEPLOYMENT_ACTION" != "activate" ]' in updater
    assert 'exec "$ACTIVATOR_PATH"' in updater
    for retired in ("APP_DIR=", "EXPECTED_AUTO_TRADE_STATE", "git fetch", "systemctl"):
        assert retired not in updater


def test_updater_dispatches_only_to_sibling_activator_in_named_release(
    tmp_path: Path,
) -> None:
    rollback_commit = "2" * 40
    deploy = tmp_path / rollback_commit / "deploy"
    deploy.mkdir(parents=True)
    dispatcher = deploy / "telegram-kol-update"
    shutil.copy2(ROOT / "deploy/telegram-kol-update", dispatcher)
    marker = tmp_path / "activator-called"
    activator = deploy / "telegram-kol-activate"
    activator.write_text(
        f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 23\n",
        encoding="utf-8",
    )
    activator.chmod(0o755)

    result = subprocess.run(
        [str(dispatcher)],
        env={
            **os.environ,
            "DEPLOYMENT_ACTION": "activate",
            "ROLLBACK_COMMIT": rollback_commit,
            "ACTIVATION_SOURCE_MODE": "immutable",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    assert marker.exists()


def test_stopped_legacy_updater_uses_candidate_without_rollback_release(
    tmp_path: Path,
) -> None:
    candidate_commit = "3" * 40
    deploy = tmp_path / candidate_commit / "deploy"
    deploy.mkdir(parents=True)
    dispatcher = deploy / "telegram-kol-update"
    shutil.copy2(ROOT / "deploy/telegram-kol-update", dispatcher)
    marker = tmp_path / "candidate-activator-called"
    activator = deploy / "telegram-kol-activate"
    activator.write_text(
        f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 23\n",
        encoding="utf-8",
    )
    activator.chmod(0o755)

    result = subprocess.run(
        [str(dispatcher)],
        env={
            **os.environ,
            "DEPLOYMENT_ACTION": "activate",
            "EXPECTED_COMMIT": candidate_commit,
            "ACTIVATION_SOURCE_MODE": "stopped_legacy",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    assert marker.exists()


def test_updater_refuses_dispatcher_directory_identity_mismatch(
    tmp_path: Path,
) -> None:
    actual_commit = "2" * 40
    deploy = tmp_path / actual_commit / "deploy"
    deploy.mkdir(parents=True)
    dispatcher = deploy / "telegram-kol-update"
    shutil.copy2(ROOT / "deploy/telegram-kol-update", dispatcher)
    marker = tmp_path / "activator-called"
    activator = deploy / "telegram-kol-activate"
    activator.write_text(
        f"#!/usr/bin/env bash\ntouch {marker!s}\nexit 0\n",
        encoding="utf-8",
    )
    activator.chmod(0o755)

    result = subprocess.run(
        [str(dispatcher)],
        env={
            **os.environ,
            "DEPLOYMENT_ACTION": "activate",
            "ROLLBACK_COMMIT": "3" * 40,
            "ACTIVATION_SOURCE_MODE": "immutable",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4
    assert "identity does not match" in result.stderr
    assert not marker.exists()


def test_scoped_activator_owns_the_single_deployment_control_lock():
    activator = (
        ROOT / "src/telegram_kol_research/scoped_release_activation.py"
    ).read_text(encoding="utf-8")

    assert 'Path("/run/telegram-kol-update.lock")' in activator
    assert "/run/telegram-kol-activate.lock" not in activator


def test_scoped_activator_has_no_settings_telegram_or_exchange_write_path():
    script = (ROOT / "deploy/telegram-kol-activate").read_text(encoding="utf-8")
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")
    implementation = (
        ROOT / "src/telegram_kol_research/scoped_release_activation.py"
    ).read_text(encoding="utf-8")
    combined = script + implementation

    for forbidden in (
        "save_trading_settings",
        "api.deepcoin.com",
        "DC-ACCESS-",
        "send_message",
        "replay",
        "bulk_cancel",
    ):
        assert forbidden not in combined
    assert "deployment_activation_quiescence_check" in implementation
    assert "runtime-deployment-identity-v1" in implementation
    assert "rollback_complete" in implementation
    assert 'export PYTHONPATH="$ACTIVATOR_ROOT/src"' in script
    assert 'export PYTHONDONTWRITEBYTECODE=1' in script
    assert 'ACTIVATOR_PYTHON=/opt/telegram-kol-analyzer/.venv/bin/python' in script
    assert 'exec "$ACTIVATOR_PYTHON" -B' in script
    assert 'DISPATCHER_RELEASE_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.."' in updater
    assert 'basename -- "$DISPATCHER_RELEASE_ROOT"' in updater
    assert 'ACTIVATOR_PATH="$DISPATCHER_RELEASE_ROOT/deploy/telegram-kol-activate"' in updater
    assert 'ACTIVATOR_PATH="${ACTIVATOR_PATH:-' not in updater
    assert "run_monitor_diagnostic" not in implementation


def test_stage_only_command_has_no_live_runtime_or_trading_dependencies():
    stage = (ROOT / "deploy/telegram-kol-stage").read_text(encoding="utf-8")

    for forbidden in (
        "systemctl",
        "DATABASE_PATH",
        "research.db",
        "sqlite3",
        "api.deepcoin.com",
        "DC-ACCESS-",
        "deepcoin_client",
        "auto_trade",
        "requests",
        "httpx",
    ):
        assert forbidden not in stage
    assert "DeploymentAction.STAGE" in stage
    assert "remote" in stage
    assert "get-url" in stage
    assert "renameat2" in stage
    assert "RENAME_NOREPLACE" in stage
    assert ".telegram-kol-release.json" in stage
    assert ".telegram-kol-stage-receipt.json" in stage
    sync_release = stage.index("_sync_release(artifact)")
    publish = stage.index("_publish_no_replace(artifact", sync_release)
    sync_root = stage.index("_fsync_directory(release_root)", publish)
    assert sync_release < publish < sync_root


def test_stage_only_policy_documents_inputs_outputs_and_non_authority():
    policy = (ROOT / "docs/deployment-action-gates.md").read_text(encoding="utf-8")

    assert "## Immutable stage-only command" in policy
    for required in (
        "`EXPECTED_COMMIT`",
        "`ACTION_MANIFEST`",
        "`SOURCE_REPO`",
        "`RELEASE_ROOT`",
        "`.telegram-kol-release.json`",
        "`.telegram-kol-stage-receipt.json`",
        "does not authorize activation",
        "does not inspect the production database",
        "does not control any service",
    ):
        assert required in policy


def test_scoped_activation_policy_documents_identity_rollback_and_blockers():
    policy = (ROOT / "docs/deployment-action-gates.md").read_text(encoding="utf-8")

    for required in (
        "## Scoped immutable activation command",
        "`ACTIVATION_AUTHORIZATION`",
        "systemd `MainPID`",
        "`/proc` start ticks",
        "management,",
        "protection, close, TPSL, and rescue",
        "explicit short interruption",
        "separately validated `ROLLBACK_COMMIT`",
        "`-ActivationSourceMode stopped_legacy`",
        "-ActivationSourceMode stopped_legacy",
        "checkout HEAD is not accepted as a substitute",
        "Schema or production-data mutation is refused",
        "does not change settings",
        "replay messages",
    ):
        assert required in policy


def test_workstation_helpers_require_action_manifest_and_exact_commit():
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    assert 'ACTION_MANIFEST="${ACTION_MANIFEST:?' in shell
    assert 'EXPECTED_COMMIT="${EXPECTED_COMMIT:?' in shell
    assert "bootstrap_server_updater.sh" in shell
    assert "[Parameter(Mandatory = $true)]" in powershell
    assert "$ActionManifest" in powershell
    assert "$ExpectedCommit" in powershell
    assert "$ChangeClass" not in powershell
    assert "$LASTEXITCODE -ne 0" in powershell
    assert "Get-FileHash -Algorithm SHA256" in powershell
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
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert 'SERVER="${SERVER:-root@43.167.220.225}"' in bootstrap
    assert 'KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"' in bootstrap
    assert 'BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"' in script
    assert "command -v ssh" in bootstrap
    assert '[ -r "$KEY_PATH" ]' in bootstrap
    assert "bootstrap_server_updater.sh" in script
    assert ",," not in script


def test_deployment_docs_keep_both_workstation_helpers_visible():
    deployment = (ROOT / "docs/server-deployment.md").read_text(encoding="utf-8")
    handoff = (ROOT / "docs/migration-handoff.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "./scripts/server_git_update.sh" in deployment
    assert "server_git_update.ps1" in deployment
    assert "./scripts/server_git_update.sh" in handoff
    assert "server_git_update.sh stage" in deployment
    assert "server_git_update.sh activate" in deployment
    assert "server_git_update.sh stage" in handoff
    assert "one-command" in handoff
    assert '-Action stage' in agents
    assert '-Action activate' in agents


def test_action_gate_policy_documents_split_helpers_and_legacy_removal():
    policy = (ROOT / "docs/deployment-action-gates.md").read_text(encoding="utf-8")

    for command in (
        "server_git_update.sh plan",
        "server_git_update.sh push",
        "server_git_update.sh stage",
        "server_git_update.sh activate",
    ):
        assert command in policy
    assert "No helper command invokes the next action" in policy
    assert "legacy one-command updater has been removed" in policy
    assert "default remains the compatibility path" not in policy


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
    preflight_start = runbook.index("## 1. 只读 preflight")
    freeze_start = runbook.index("## 2. 冻结写入")
    preflight = runbook[preflight_start:freeze_start]
    assert "失败只因 owner 漂移" not in task12
    assert "已知的 15 条历史" not in runbook
    assert "历史 15 条拒绝" not in implementation[task12_start:]
    for required in (
        "HTTP 404 单独不构成",
        "/api/runtime-incidents/monitor-capture-health",
        "同一 token",
        "runtime_role=worker",
        "exact previous SHA 的 route",
        "Agent deny ACL 已满足或缺失",
        "除非 active row 需要窗口外证据",
    ):
        assert required in preflight
        assert required in task12
    assert "unknown owner/type/link/group/mode/ACL" in preflight
    restore_section = runbook.index("## 4. 单独恢复")
    enabled_updater = runbook.index("EXPECTED_AUTO_TRADE_STATE=enabled", restore_section)
    restore_watermark = runbook.index("restore_raw_message_id", restore_section)
    enable_setting = runbook.index("然后重新 GET 当前完整 settings", restore_section)
    assert enabled_updater < restore_watermark < enable_setting


def test_contract_cache_status_records_version_aware_task12_handoff():
    status = CACHE_REPAIR_STATUS.read_text(encoding="utf-8")

    assert "task12_refusal_baseline_count: 16" in status
    observed_max = re.search(r"task12_observed_max_raw_message_id: (\d+)", status)
    assert observed_max is not None
    assert int(observed_max.group(1)) >= 13534
    assert "task12_gate: failed_closed" in status
    assert "task12_health_classification: legacy_capability_absent" in status
    assert "task12_time_sensitive_pending_trigger_count: 7" in status
    assert (
        "pending_entry_cancel_candidate_status: "
        "superseded_by_simple_cancel_all" in status
    )
    assert (
        "pending_entry_cancel_candidate_sha: "
        "708a479f7e20aba74869d87acb3839f3fd91e96b" in status
    )
    assert (
        "pending_entry_cancel_quiescence_base_sha: "
        "47ea0885d02532faf7a941694f6b19dcdb1af9a6" in status
    )
    assert (
        "pending_entry_cancel_pushed_base_sha: "
        "91bb257e2a1c808c25a54149a7c71c392c0952e4" in status
    )
    assert "pending_entry_cancel_production_executed: false" in status
    assert "current_phase: manual_cleanup_production_cutover" in status
    assert "pending_entry_cancel_live_order_count: 0" in status
    assert (
        "manual_cleanup_exchange_snapshot_status: "
        "zero_live_orders_historical_requires_fresh_cutover_recheck" in status
    )
    assert "manual_cleanup_target_fill_count: 0" in status
    assert "manual_cleanup_local_eligible_count: 7" in status
    assert (
        "manual_cleanup_local_repair_status: "
        "complete_production_cutover_pending" in status
    )
    assert "manual_cleanup_local_repair_base_sha: " in status
    assert "manual_cleanup_local_repair_focused: 344_passed_1_skipped" in status
    assert (
        "manual_cleanup_local_repair_final_suite: "
        "6644_passed_3_skipped_32_warnings" in status
    )
    assert "auto_trade_frozen: false" in status
    assert "freeze_raw_message_id: null" in status
    assert "restore_raw_message_id: null" in status
    assert "recognized migratable legacy drift" in status
    assert "bounded 100-row history coverage" in status
    assert "seven unprotected pending trigger entries" in status
    current = status[: status.index("### Prior rejected candidate history")]
    assert "baseline of\n  15 terminal" not in current
    assert "terminal set is now 15" not in current
    assert "still require a production-read-only\n  explanation" not in current
    assert "owner and Agent ACL failed" not in current
    outstanding = status[status.index("## Outstanding") :]
    for required in (
        "same token",
        "exact worker port",
        "runtime_role=worker",
        "exact previous SHA route absence",
        "HTTP 404 alone is insufficient",
    ):
        assert required in outstanding


def test_version_aware_implementation_plan_uses_closed_legacy_requirements():
    plan = CACHE_REPAIR_VERSION_AWARE_PLAN.read_text(encoding="utf-8")
    requirements = plan[plan.index("Add assertions requiring") : plan.index("Also assert")]

    assert "Agent deny ACL may be satisfied or absent" in requirements
    assert "same token" in requirements
    assert "exact worker port" in requirements
    assert "worker runtime role" in requirements
    assert "exact previous SHA route absence" in requirements
    assert "HTTP 404 alone is insufficient" in requirements


def test_version_aware_gate_documents_have_single_terminal_newline():
    for path in (
        CACHE_REPAIR_VERSION_AWARE_DESIGN,
        CACHE_REPAIR_VERSION_AWARE_PLAN,
    ):
        assert not path.read_text(encoding="utf-8").endswith("\n\n")


def test_push_path_is_exact_fast_forward_only_and_never_chains() -> None:
    shell = (ROOT / "scripts/server_git_update.sh").read_text(encoding="utf-8")

    push_start = shell.index('if [ "$ACTION" = "push" ]')
    push_end = shell.index("\nfi\n", push_start)
    push_path = shell[push_start:push_end]
    assert 'status --porcelain --untracked-files=normal' in push_path
    assert 'rev-parse HEAD' in push_path
    assert 'merge-base --is-ancestor' in push_path
    assert 'push origin "$EXPECTED_COMMIT:refs/heads/$BRANCH"' in push_path
    assert "bootstrap_server_updater.sh" not in push_path
    assert "ssh " not in push_path
    assert "telegram-kol-stage" not in push_path


def test_stage_transport_uses_exact_commit_bundle_and_no_active_checkout_fetch() -> None:
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )

    assert 'git -C "$ROOT" archive' in bootstrap
    assert '"$EXPECTED_COMMIT"' in bootstrap
    assert "deploy/telegram-kol-stage" in bootstrap
    assert 'PYTHONPATH="$temporary/control/src"' in bootstrap
    assert 'SOURCE_REPO="$source_repo"' in bootstrap
    assert 'RELEASE_ROOT="$release_root"' in bootstrap
    assert 'git -C "$app_dir" fetch' not in bootstrap
    assert "systemctl" not in bootstrap
    assert "EXPECTED_AUTO_TRADE_STATE" not in bootstrap


def test_powershell_stage_payload_uses_stdin_not_command_line() -> None:
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(
        encoding="utf-8"
    )

    remote_command = powershell[powershell.index('$remote = "') :]
    assert "'$manifestBase64'" not in remote_command
    assert "'$bundleBase64'" not in remote_command
    assert 'IFS= read -r manifest_base64' in powershell
    assert 'IFS= read -r bundle_base64' in powershell
    assert '$payload | ssh -i $KeyPath $Server $remote' in powershell


def test_powershell_validates_ssh_destination_before_transport() -> None:
    powershell = (ROOT / "scripts/server_git_update.ps1").read_text(
        encoding="utf-8"
    )

    validation = powershell.index("function Test-SshDestination")
    rejection = powershell.index('throw "Invalid Server."', validation)
    transport = powershell.index("$payload | ssh", rejection)
    assert validation < rejection < transport
    assert "[A-Za-z_][A-Za-z0-9._-]*@" in powershell


def test_powershell_helper_parses_when_pwsh_is_available() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is unavailable; Windows CI owns this parser gate")
    script = ROOT / "scripts/server_git_update.ps1"
    command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$args[0],[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }"
    )
    subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command, str(script)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_push_command_fast_forwards_only_the_exact_local_commit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "work"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/server_git_update.sh", scripts)
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    for key, value in (("user.name", "Push Test"), ("user.email", "push@test.invalid")):
        subprocess.run(
            ["git", "-C", str(repository), "config", key, value], check=True
        )
    subprocess.run(
        ["git", "-C", str(repository), "add", "scripts/server_git_update.sh"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "reviewed"],
        check=True,
        capture_output=True,
    )
    expected_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", str(origin)],
        check=True,
    )
    manifest = tmp_path / "push.json"
    _write_action_manifest(manifest, "push")

    result = subprocess.run(
        [str(scripts / "server_git_update.sh"), "push"],
        cwd=repository,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PLANNER_PYTHON": str(ROOT / ".venv/bin/python"),
            "ACTION_MANIFEST": str(manifest),
            "EXPECTED_COMMIT": expected_commit,
            "BRANCH": "codex/test",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action": "push",
        "commit": expected_commit,
        "status": "complete",
    }
    remote_commit = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/codex/test"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_commit == expected_commit


def test_activate_transport_selects_dispatcher_by_source_mode() -> None:
    bootstrap = (ROOT / "scripts/bootstrap_server_updater.sh").read_text(
        encoding="utf-8"
    )

    activate = bootstrap[bootstrap.index("  activate)") :]
    assert 'dispatcher_commit="$rollback_commit"' in activate
    assert 'if [ "$source_mode" = "stopped_legacy" ]' in activate
    assert 'dispatcher_commit="$expected_commit"' in activate
    assert 'updater="$release_root/$dispatcher_commit/deploy/telegram-kol-update"' in activate
    assert "DEPLOYMENT_ACTION=activate" in activate
    assert 'EXPECTED_COMMIT="$expected_commit"' in activate
    assert 'ROLLBACK_COMMIT="$rollback_commit"' in activate
    assert 'ACTIVATION_AUTHORIZATION="$authorization"' in activate
    assert 'ACTIVATION_AUTHORIZATION_CONSUMED="$authorization_consumed"' in activate
    assert 'ACTIVATION_SOURCE_MODE="$source_mode"' in activate
    assert 'source_mode="${13}"' in bootstrap
    assert "'$ACTIVATION_SOURCE_MODE'" in bootstrap
    assert "git push" not in activate
    assert "telegram-kol-stage" not in activate


def test_powershell_activate_transport_passes_explicit_source_mode() -> None:
    script = (ROOT / "scripts/server_git_update.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("immutable", "stopped_legacy")]' in script
    assert '[string]$ActivationSourceMode = "immutable"' in script
    assert 'source_mode="${13}"' in script
    assert 'ACTIVATION_SOURCE_MODE="$source_mode"' in script
    assert "'$ActivationSourceMode'" in script


def test_retired_universal_updater_implementation_is_absent() -> None:
    updater = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert len(updater.splitlines()) < 40
    for retired in (
        "git fetch",
        "git merge",
        "pip install",
        "systemctl",
        "sqlite3",
        "EXPECTED_AUTO_TRADE_STATE",
        "resolve_managed_topology",
        "install_worker_cache_artifacts",
        "sync_monitor_expectations",
    ):
        assert retired not in updater
