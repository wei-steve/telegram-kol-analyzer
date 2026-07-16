from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.service"
TIMER_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.timer"
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_server_monitor.sh"
PRODUCTION_ROOT = "/opt/telegram-kol-analyzer"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


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
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in directives
    assert "AF_UNIX" not in service
    assert "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private" in directives
    assert "SystemCallFilter=@system-service" in directives
    assert "SystemCallFilter=~@mount @privileged" in directives
    assert "RestrictNamespaces=true" in directives


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
    assert "systemctl is-enabled --quiet telegram-kol-monitor.timer" in preflight
    assert 'if [[ "$enable_timer" == false && "$timer_enabled_status" -eq 0 ]]; then' in preflight
    assert "useradd " not in preflight
    assert "groupadd " not in preflight
    assert "install -d " not in preflight
    assert "systemctl daemon-reload" not in preflight


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
    assert 'install -d -o "$MONITOR_USER" -g "$MONITOR_GROUP" -m 0700 "$STATE_DIRECTORY"' in installer
    assert 'runuser -u "$MONITOR_USER" -- test -x "$PRODUCTION_ROOT/.venv/bin/telegram-kol-research"' in installer
    assert 'runuser -u "$MONITOR_USER" -- test -r "$PRODUCTION_ROOT/data/research.db"' in installer


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
        for path in (SERVICE_PATH, TIMER_PATH, INSTALLER_PATH)
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
    assert combined.index("systemctl clean --what=state telegram-kol-monitor.timer") < combined.index(
        "rm -f /etc/systemd/system/telegram-kol-monitor.timer"
    )
    assert "/etc/telegram-kol-monitor.credentials" in combined
    assert "dedicated unprivileged" in combined.lower()
    assert "normal trade notifications" in combined.lower()
