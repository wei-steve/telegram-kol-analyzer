import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.service"
TIMER_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.timer"
TEST_NOTIFICATION_PATH = (
    PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor-test-notification.service"
)
DIAGNOSTIC_PATH = (
    PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor-diagnostic.service"
)
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_server_monitor.sh"
PRODUCTION_ROOT = "/opt/telegram-kol-analyzer"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _state_repair_program() -> str:
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    return installer.split("# BEGIN_STATE_METADATA_REPAIR\n", 1)[1].split(
        "# END_STATE_METADATA_REPAIR", 1
    )[0]


def test_monitor_service_uses_dedicated_identity_and_exact_command():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    normalized = _normalized(SERVICE_PATH)

    assert "Type=oneshot" in service
    assert "User=telegram-kol-monitor" in service
    assert "Group=telegram-kol-monitor" in service
    assert "User=root" not in service
    assert "WorkingDirectory=/var/lib/telegram-kol-monitor" in service
    assert "EnvironmentFile=/etc/telegram-kol-monitor.env" in service
    assert (
        "ExecStart=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
        "monitor-production-safety "
        "--expected-head ${TELEGRAM_KOL_MONITOR_EXPECTED_HEAD} "
        "--expected-auto-trade-enabled "
        "--expected-management-mode live "
        "--expected-max-concurrent-positions 4 "
        "--checkout-path /opt/telegram-kol-analyzer "
        "--settings-url http://127.0.0.1:8000/api/trading-settings "
        "--database-path /opt/telegram-kol-analyzer/data/research.db "
        "--state-path /var/lib/telegram-kol-monitor/state.json "
        "--lookback-minutes 35 "
        "--notify"
    ) in normalized


def test_monitor_service_drops_all_capabilities_and_denies_system_bus():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    directives = set(service.splitlines())

    assert "CapabilityBoundingSet=" in directives
    assert "AmbientCapabilities=" in directives
    assert "NoNewPrivileges=true" in directives
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in directives
    assert "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private" in directives
    assert "SystemCallFilter=@system-service" in directives
    assert "SystemCallFilter=~@mount @privileged" in directives
    assert "RestrictNamespaces=true" in directives


def test_test_notification_unit_uses_same_identity_environment_and_sandbox_once():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    test_service = TEST_NOTIFICATION_PATH.read_text(encoding="utf-8")
    service_directives = set(service.splitlines())
    test_directives = set(test_service.splitlines())

    assert "Type=oneshot" in test_directives
    assert "User=telegram-kol-monitor" in test_directives
    assert "Group=telegram-kol-monitor" in test_directives
    assert "EnvironmentFile=/etc/telegram-kol-monitor.env" in test_directives
    assert "[Install]" not in test_directives
    assert "--notify --test-notification" in " ".join(test_service.split())
    assert "TELEGRAM_KOL_SYSTEM_BOT_TOKEN" not in test_service
    ignored_prefixes = ("Description=", "ExecStart=")
    expected_sandbox = {
        line
        for line in service_directives
        if line and not line.startswith(ignored_prefixes)
    }
    actual_sandbox = {
        line
        for line in test_directives
        if line and not line.startswith(ignored_prefixes)
    }
    assert actual_sandbox == expected_sandbox


def test_diagnostic_unit_forces_full_audit_without_notification_in_same_sandbox():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    diagnostic = DIAGNOSTIC_PATH.read_text(encoding="utf-8")
    service_directives = set(service.splitlines())
    diagnostic_directives = set(diagnostic.splitlines())

    assert "Type=oneshot" in diagnostic_directives
    assert "User=telegram-kol-monitor" in diagnostic_directives
    assert "EnvironmentFile=/etc/telegram-kol-monitor.env" in diagnostic_directives
    assert "[Install]" not in diagnostic_directives
    normalized = " ".join(diagnostic.split())
    assert "--force-full-audit" in normalized
    assert "--notify" not in normalized
    ignored_prefixes = ("Description=", "ExecStart=")
    assert {
        line
        for line in diagnostic_directives
        if line and not line.startswith(ignored_prefixes)
    } == {
        line
        for line in service_directives
        if line and not line.startswith(ignored_prefixes)
    }


@pytest.mark.skipif(sys.platform != "linux", reason="systemd sandbox probe is Linux-only")
def test_monitor_sandbox_allows_asyncio_socketpair_but_denies_control_sockets():
    if os.geteuid() != 0:
        pytest.skip("system systemd sandbox probe requires root")
    if (
        shutil.which("systemd-run") is None
        or shutil.which("systemctl") is None
        or not Path("/run/systemd/system").is_dir()
    ):
        pytest.skip("running systemd manager is unavailable")
    manager = subprocess.run(
        ["systemctl", "is-system-running"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if manager.stdout.strip() not in {"running", "degraded", "starting", "maintenance"}:
        pytest.skip("running systemd manager is unavailable")

    probe = """
import asyncio
import socket

asyncio.run(asyncio.sleep(0))
for path in ("/run/dbus/system_bus_socket", "/run/systemd/private"):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            client.connect(path)
        except OSError:
            pass
        else:
            raise SystemExit(f"control socket connectable: {path}")
    finally:
        client.close()
"""
    completed = subprocess.run(
        [
            "systemd-run",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
            "--property=Type=oneshot",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private",
            sys.executable,
            "-c",
            probe,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_monitor_service_exposes_only_required_read_only_inputs_and_state():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    directives = set(service.splitlines())

    assert "TemporaryFileSystem=/opt/telegram-kol-analyzer:ro" in directives
    assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/.venv" in directives
    assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/src" in directives
    assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/.git" in directives
    assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/data/research.db" in directives
    assert "BindReadOnlyPaths=-/opt/telegram-kol-analyzer/data/research.db-wal" in directives
    assert "BindReadOnlyPaths=-/opt/telegram-kol-analyzer/data/research.db-shm" in directives
    assert "BindReadOnlyPaths=-/opt/telegram-kol-analyzer/data/research.db-journal" in directives
    assert "ReadWritePaths=/var/lib/telegram-kol-monitor" in directives
    assert "SupplementaryGroups=systemd-journal" in directives
    assert f"{PRODUCTION_ROOT}/.env" not in service
    assert f"{PRODUCTION_ROOT}/config" not in service
    assert "ReadWritePaths=/opt/telegram-kol-analyzer" not in service


def test_monitor_timer_is_true_persistent_thirty_minute_calendar_timer():
    timer = TIMER_PATH.read_text(encoding="utf-8")

    assert "OnCalendar=*:0/30" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=2min" in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=" not in timer
    assert "Unit=telegram-kol-monitor.service" in timer


def test_installer_validates_fixed_production_checkout_before_freezing_head():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert 'PRODUCTION_ROOT="/opt/telegram-kol-analyzer"' in installer
    assert 'if [[ "$PROJECT_ROOT" != "$PRODUCTION_ROOT" ]]; then' in installer
    assert 'git -C "$PRODUCTION_ROOT" rev-parse --show-toplevel' in installer
    assert 'expected_head="$(git -C "$PRODUCTION_ROOT" rev-parse --verify HEAD)"' in installer
    assert installer.index('if [[ "$PROJECT_ROOT" != "$PRODUCTION_ROOT" ]]; then') < installer.index(
        'expected_head="$(git -C "$PRODUCTION_ROOT" rev-parse --verify HEAD)"'
    )


def test_installer_fails_closed_on_running_or_enabled_install_only_before_changes():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    preflight_end = installer.index("# Preflight complete; mutations may begin.")
    preflight = installer[:preflight_end]

    assert "systemctl is-active --quiet telegram-kol-monitor.timer" in preflight
    for unit in (
        "telegram-kol-monitor.service",
        "telegram-kol-monitor-diagnostic.service",
        "telegram-kol-monitor-test-notification.service",
    ):
        assert unit in preflight
    assert 'systemctl is-active --quiet "$monitor_unit"' in preflight
    assert "systemctl is-enabled --quiet telegram-kol-monitor.timer" in preflight
    assert 'if [[ "$enable_timer" == false && "$timer_enabled_status" -eq 0 ]]; then' in preflight
    assert "useradd " not in preflight
    assert "groupadd " not in preflight
    assert "install -d " not in preflight
    assert "systemctl daemon-reload" not in preflight


def test_installer_accepts_systemd_not_found_as_a_fresh_disabled_install():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    enablement_case = installer.split(
        'case "$timer_enabled_status" in', 1
    )[1].split("esac", 1)[0]

    assert "0|1|3|4)" in enablement_case


def test_installer_creates_identity_and_allowlisted_monitor_environment():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert 'MONITOR_USER="telegram-kol-monitor"' in installer
    assert 'MONITOR_GROUP="telegram-kol-monitor"' in installer
    assert 'groupadd --system "$MONITOR_GROUP"' in installer
    assert "useradd --system" in installer
    assert 'if [[ "$(id -u "$MONITOR_USER")" -eq 0 || "$(id -gn "$MONITOR_USER")" != "$MONITOR_GROUP" ]]; then' in installer
    assert 'CREDENTIAL_FILE="/etc/telegram-kol-monitor.credentials"' in installer
    assert "TELEGRAM_KOL_SYSTEM_BOT_TOKEN" in installer
    assert "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID" in installer
    assert "TELEGRAM_KOL_MONITOR_EXPECTED_HEAD" in installer
    assert "DEEP_API" not in installer
    assert "DEEPCOIN" not in installer.upper()
    assert 'install -o root -g root -m 0600 "$env_source" "$ENV_FILE"' in installer
    assert 'install -d -o root -g root -m 0700 "$STATE_DIRECTORY"' in installer
    assert 'chown "$MONITOR_USER:$MONITOR_GROUP" "$STATE_DIRECTORY"' in installer
    assert 'chmod 0700 "$STATE_DIRECTORY"' in installer
    assert 'runuser -u "$MONITOR_USER" -- test -x "$PRODUCTION_ROOT/.venv/bin/telegram-kol-research"' in installer
    assert 'runuser -u "$MONITOR_USER" -- test -r "$PRODUCTION_ROOT/data/research.db"' in installer
    assert "telegram-kol-monitor-test-notification.service" in installer
    assert (
        'install -o root -g root -m 0644 "$TEST_NOTIFICATION_SOURCE" '
        '"$TEST_NOTIFICATION_DEST"'
    ) in installer
    assert "telegram-kol-monitor-diagnostic.service" in installer
    assert (
        'install -o root -g root -m 0644 "$DIAGNOSTIC_SOURCE" '
        '"$DIAGNOSTIC_DEST"'
    ) in installer


def test_installer_repairs_existing_state_metadata_without_replacing_content():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert 'STATE_FILE="$STATE_DIRECTORY/state.json"' in installer
    assert 'if [[ -e "$STATE_FILE" || -L "$STATE_FILE" ]]; then' in installer
    assert 'install -d -o root -g root -m 0700 "$STATE_DIRECTORY"' in installer
    assert 'chown "$MONITOR_USER:$MONITOR_GROUP" "$STATE_DIRECTORY"' in installer
    assert "os.O_NOFOLLOW" in installer
    assert "os.fchown" in installer
    assert "os.fchmod" in installer
    assert 'install -o "$MONITOR_USER" -g "$MONITOR_GROUP" -m 0600' not in installer
    assert 'rm -f "$STATE_FILE"' not in installer
    assert installer.index("# END_STATE_METADATA_REPAIR") < installer.index(
        "systemctl daemon-reload"
    )


def test_state_metadata_repair_preserves_bytes_and_sets_exact_metadata(tmp_path):
    state_path = tmp_path / "state.json"
    sentinel = b'{"sentinel":"preserve-exactly"}\n'
    state_path.write_bytes(sentinel)
    state_path.chmod(0o666)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _state_repair_program(),
            str(state_path),
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert state_path.read_bytes() == sentinel
    metadata = state_path.stat()
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert metadata.st_mode & 0o777 == 0o600


def test_state_metadata_repair_refuses_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    target.chmod(0o644)
    state_path = tmp_path / "state.json"
    state_path.symlink_to(target)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _state_repair_program(),
            str(state_path),
            str(os.getuid()),
            str(os.getgid()),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert target.read_text(encoding="utf-8") == "untouched"
    assert target.stat().st_mode & 0o777 == 0o644


def test_installer_orders_enable_after_install_and_reload_and_targets_timer_only():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    reload_index = installer.index("systemctl daemon-reload")
    enable_index = installer.index("systemctl enable --now telegram-kol-monitor.timer")

    assert reload_index < enable_index
    assert 'if [[ "$enable_timer" == true ]]; then' in installer
    mutation_lines = [
        line.strip()
        for line in installer.splitlines()
        if line.strip().startswith(("systemctl enable", "systemctl start", "systemctl restart", "systemctl stop"))
    ]
    assert mutation_lines == ["systemctl enable --now telegram-kol-monitor.timer"]


def test_units_and_installer_never_mutate_trading_or_exchange_state():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SERVICE_PATH,
            TIMER_PATH,
            TEST_NOTIFICATION_PATH,
            DIAGNOSTIC_PATH,
            INSTALLER_PATH,
        )
    ).lower()

    for forbidden in (
        "systemctl restart telegram-kol.service",
        "systemctl stop telegram-kol.service",
        "systemctl start telegram-kol.service",
        "sqlite3",
        "--apply",
        "place_order",
        "cancel_order",
        "api.deepcoin.com",
    ):
        assert forbidden not in combined


def test_operations_docs_require_disabled_upgrade_and_clean_persistent_timer_state():
    documents = [
        PROJECT_ROOT / "docs" / "runbook.md",
        PROJECT_ROOT / "docs" / "server-deployment.md",
        PROJECT_ROOT / "docs" / "migration-handoff.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)

    assert "systemctl disable --now telegram-kol-monitor.timer" in combined
    assert "systemctl is-enabled --quiet telegram-kol-monitor.timer" in combined
    assert "systemctl is-active --quiet telegram-kol-monitor.timer" in combined
    assert "systemctl clean --what=state telegram-kol-monitor.timer" in combined
    stop_oneshots = (
        "systemctl stop telegram-kol-monitor.service "
        "telegram-kol-monitor-diagnostic.service "
        "telegram-kol-monitor-test-notification.service"
    )
    assert combined.count(stop_oneshots) == 2
    assert combined.index(stop_oneshots) < combined.index(
        "systemctl clean --what=state telegram-kol-monitor.timer"
    )
    assert combined.index("systemctl clean --what=state telegram-kol-monitor.timer") < combined.index(
        "rm -f /etc/systemd/system/telegram-kol-monitor.timer"
    )
    assert (
        "rm -f /etc/systemd/system/telegram-kol-monitor-test-notification.service"
        in combined
    )
    assert "rm -f /etc/systemd/system/telegram-kol-monitor-diagnostic.service" in combined
    assert "/etc/telegram-kol-monitor.credentials" in combined
    assert "dedicated unprivileged" in combined.lower()
    assert "normal trade notifications" in combined.lower()


def test_operations_docs_have_exactly_one_safe_test_notification_instruction():
    documents = [
        PROJECT_ROOT / "docs" / "runbook.md",
        PROJECT_ROOT / "docs" / "server-deployment.md",
        PROJECT_ROOT / "docs" / "migration-handoff.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)
    instruction = (
        "sudo systemctl start "
        "telegram-kol-monitor-test-notification.service"
    )

    assert combined.count(instruction) == 1
    assert combined.count(
        "sudo systemctl start telegram-kol-monitor-diagnostic.service"
    ) == 1
    assert "sudo runuser -u telegram-kol-monitor" not in combined
    assert "sudo .venv/bin/telegram-kol-research monitor-production-safety" not in combined
    assert "dedicated identity" in combined.lower()
    assert "never enabled" in combined.lower()


def test_runbook_documents_monitor_identity_owned_runtime_state():
    runbook = (PROJECT_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")

    assert "monitor-identity-owned" in runbook.lower()
    assert "root-owned state file" not in runbook.lower()
    assert "pending state-integrity notification" in runbook.lower()
    assert "does not mark it delivered" in runbook.lower()
