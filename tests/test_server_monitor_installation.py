from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.service"
TIMER_PATH = PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-monitor.timer"
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_server_monitor.sh"


def _normalized_unit(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_monitor_service_has_exact_read_only_live_safety_command_and_paths():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    normalized = _normalized_unit(SERVICE_PATH)

    assert "Type=oneshot" in service
    assert "WorkingDirectory=/opt/telegram-kol-analyzer" in service
    assert "EnvironmentFile=/etc/telegram-kol-monitor.env" in service
    assert (
        "ExecStart=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
        "monitor-production-safety "
        "--expected-head ${TELEGRAM_KOL_MONITOR_EXPECTED_HEAD} "
        "--expected-auto-trade-enabled "
        "--expected-management-mode live "
        "--expected-max-concurrent-positions 4 "
        "--database-path /opt/telegram-kol-analyzer/data/research.db "
        "--state-path /var/lib/telegram-kol-monitor/state.json "
        "--lookback-minutes 35 "
        "--notify"
    ) in normalized


def test_monitor_service_is_root_only_and_hardened_to_one_state_directory():
    service = SERVICE_PATH.read_text(encoding="utf-8")

    expected_directives = {
        "User=root",
        "Group=root",
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateDevices=true",
        "PrivateTmp=true",
        "ProtectClock=true",
        "ProtectControlGroups=true",
        "ProtectHome=true",
        "ProtectHostname=true",
        "ProtectKernelLogs=true",
        "ProtectKernelModules=true",
        "ProtectKernelTunables=true",
        "ProtectSystem=strict",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "RestrictRealtime=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "ReadWritePaths=/var/lib/telegram-kol-monitor",
        "ReadOnlyPaths=/opt/telegram-kol-analyzer",
    }
    assert expected_directives <= set(service.splitlines())
    assert "ReadWritePaths=/opt/telegram-kol-analyzer" not in service


def test_monitor_timer_has_exact_persistent_thirty_minute_schedule():
    timer = TIMER_PATH.read_text(encoding="utf-8")

    assert "OnBootSec=5min" in timer
    assert "OnUnitActiveSec=30min" in timer
    assert "RandomizedDelaySec=2min" in timer
    assert "Persistent=true" in timer
    assert "Unit=telegram-kol-monitor.service" in timer
    assert "WantedBy=timers.target" in timer


def test_installer_requires_root_accepts_only_enable_and_freezes_current_head():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    assert 'if [[ "$(id -u)" -ne 0 ]]; then' in installer
    assert 'case "$#:$*" in' in installer
    assert '"0:")' in installer
    assert '"1:--enable")' in installer
    assert 'expected_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"' in installer
    assert 'TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=%s\\n' in installer
    assert 'ENV_FILE="/etc/telegram-kol-monitor.env"' in installer


def test_installer_uses_exact_modes_and_orders_enable_after_install_and_reload():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")

    state_index = installer.index(
        'install -d -o root -g root -m 0700 "$STATE_DIRECTORY"'
    )
    env_index = installer.index(
        'install -o root -g root -m 0600 "$env_source" "$ENV_FILE"'
    )
    service_index = installer.index(
        'install -o root -g root -m 0644 "$SERVICE_SOURCE" "$SERVICE_DEST"'
    )
    timer_index = installer.index(
        'install -o root -g root -m 0644 "$TIMER_SOURCE" "$TIMER_DEST"'
    )
    reload_index = installer.index("systemctl daemon-reload")
    enable_index = installer.index(
        "systemctl enable --now telegram-kol-monitor.timer"
    )

    assert state_index < env_index < service_index < timer_index < reload_index
    assert reload_index < enable_index
    assert 'if [[ "$enable_timer" == true ]]; then' in installer


def test_installer_default_does_not_start_or_enable_and_enable_targets_timer_only():
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    systemctl_lines = [
        line.strip()
        for line in installer.splitlines()
        if line.strip().startswith("systemctl ")
    ]

    assert systemctl_lines == [
        "systemctl daemon-reload",
        "systemctl enable --now telegram-kol-monitor.timer",
    ]
    assert "systemctl start" not in installer
    assert "systemctl restart" not in installer
    assert "systemctl stop" not in installer


def test_units_and_installer_never_mutate_trading_service_database_or_exchange():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (SERVICE_PATH, TIMER_PATH, INSTALLER_PATH)
    ).lower()

    forbidden = (
        "systemctl restart telegram-kol.service",
        "systemctl stop telegram-kol.service",
        "systemctl start telegram-kol.service",
        "sqlite3",
        " insert ",
        " update ",
        " delete ",
        "--apply",
        "place_order",
        "cancel_order",
        "close-bound-position",
        "api.deepcoin.com",
    )
    for value in forbidden:
        assert value not in combined


def test_operations_docs_cover_staged_enable_status_and_monitor_only_rollback():
    documents = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            PROJECT_ROOT / "docs" / "runbook.md",
            PROJECT_ROOT / "docs" / "server-deployment.md",
            PROJECT_ROOT / "docs" / "migration-handoff.md",
        )
    }
    combined = "\n".join(documents.values())

    assert "./scripts/install_server_monitor.sh" in combined
    assert "--force-full-audit" in combined
    assert "--test-notification" in combined
    assert "./scripts/install_server_monitor.sh --enable" in combined
    assert "systemctl list-timers telegram-kol-monitor.timer" in combined
    assert "systemctl disable --now telegram-kol-monitor.timer" in combined
    assert "normal trade notifications" in combined.lower()
    assert all("telegram-kol-monitor" in text for text in documents.values())
