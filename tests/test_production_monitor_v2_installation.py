import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
INSTALLER = ROOT / "scripts" / "install_production_monitor_v2.sh"

SNAPSHOT_SERVICE = SYSTEMD / "telegram-kol-monitor-snapshot.service"
SNAPSHOT_TIMER = SYSTEMD / "telegram-kol-monitor-snapshot.timer"
SENTINEL_SERVICE = SYSTEMD / "telegram-kol-sentinel.service"
SENTINEL_TIMER = SYSTEMD / "telegram-kol-sentinel.timer"
AUDIT_SERVICE = SYSTEMD / "telegram-kol-monitor-audit.service"
AUDIT_TIMER = SYSTEMD / "telegram-kol-monitor-audit.timer"
DB_STAGE_SERVICE = SYSTEMD / "telegram-kol-monitor-db-stage@.service"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _directives(path: Path) -> set[str]:
    return set(_text(path).splitlines())


def test_v2_has_three_dormant_oneshot_timer_pairs_with_exact_cadences():
    snapshot_service = _text(SNAPSHOT_SERVICE)
    sentinel_service = _text(SENTINEL_SERVICE)
    audit_service = _text(AUDIT_SERVICE)
    snapshot_timer = _text(SNAPSHOT_TIMER)
    sentinel_timer = _text(SENTINEL_TIMER)
    audit_timer = _text(AUDIT_TIMER)

    for service in (snapshot_service, sentinel_service, audit_service):
        assert "Type=oneshot" in service
        assert "WantedBy=telegram-kol.service" not in service
        assert "PartOf=telegram-kol.service" not in service
    assert "OnUnitActiveSec=2min" in snapshot_timer
    assert "OnUnitActiveSec=5min" in sentinel_timer
    assert "OnCalendar=daily" in audit_timer
    assert "Unit=telegram-kol-monitor-snapshot.service" in snapshot_timer
    assert "Unit=telegram-kol-sentinel.service" in sentinel_timer
    assert "Unit=telegram-kol-monitor-audit.service" in audit_timer


def test_snapshot_and_sentinel_use_distinct_unprivileged_identities():
    snapshot = _directives(SNAPSHOT_SERVICE)
    sentinel = _directives(SENTINEL_SERVICE)
    audit = _directives(AUDIT_SERVICE)

    assert "User=telegram-kol-monitor-snapshot" in snapshot
    assert "Group=telegram-kol-monitor-snapshot" in snapshot
    assert "User=telegram-kol-monitor-sentinel" in sentinel
    assert "Group=telegram-kol-monitor-sentinel" in sentinel
    assert snapshot.isdisjoint({"User=root", "Group=root"})
    assert sentinel.isdisjoint({"User=root", "Group=root"})
    # The audit updates the sentinel-owned audit slot under the same state lease.
    assert "User=telegram-kol-monitor-sentinel" in audit
    assert "Group=telegram-kol-monitor-sentinel" in audit


def test_units_expose_only_the_required_read_and_write_paths():
    snapshot = _directives(SNAPSHOT_SERVICE)
    sentinel = _directives(SENTINEL_SERVICE)
    audit = _directives(AUDIT_SERVICE)
    database_mount = "BindReadOnlyPaths=/opt/telegram-kol-analyzer/data"

    assert database_mount not in snapshot
    stage = _directives(DB_STAGE_SERVICE)
    assert database_mount not in sentinel
    assert database_mount not in audit
    assert database_mount in stage
    assert "ReadWritePaths=/var/lib/telegram-kol-monitor-v2/snapshot" in snapshot
    assert "ReadWritePaths=/var/lib/telegram-kol-monitor-v2/sentinel" in sentinel
    assert "ReadWritePaths=/var/lib/telegram-kol-monitor-v2/sentinel" in audit
    assert "ReadWritePaths=/var/cache/telegram-kol-monitor-v2/audit" in audit
    for service in (snapshot, sentinel, audit):
        assert "ReadWritePaths=/opt/telegram-kol-analyzer" not in service
    assert (
        "LoadCredential=monitor-snapshot:/var/lib/telegram-kol-monitor-v2/"
        "snapshot/manifest.json"
    ) in sentinel
    assert (
        "ExecStartPre=/usr/bin/install -m 0600 "
        "${CREDENTIALS_DIRECTORY}/monitor-snapshot "
        "/var/cache/telegram-kol-monitor-v2/sentinel/snapshot.json"
    ) in sentinel
    assert "BindReadOnlyPaths=/var/lib/telegram-kol-monitor-v2/snapshot" not in sentinel
    assert "Requires=telegram-kol-monitor-db-stage@sentinel.service" in sentinel
    assert "Requires=telegram-kol-monitor-db-stage@audit.service" in audit
    assert "/var/cache/telegram-kol-monitor-v2/sentinel/research-snapshot.db" in " ".join(sentinel)
    assert "/var/cache/telegram-kol-monitor-v2/audit/research-snapshot.db" in " ".join(audit)
    for service in (sentinel, audit):
        assert not any("/opt/telegram-kol-analyzer/data" in line for line in service)
        assert "TemporaryFileSystem=/opt/telegram-kol-analyzer:ro" in service
    assert "ExecStart=/usr/bin/python3 /usr/local/libexec/telegram-kol-monitor-db-stage --consumer %i" in stage
    assert "PrivateNetwork=true" in stage
    assert "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE" in stage


def test_units_drop_capabilities_and_cannot_reach_control_sockets():
    for path in (SNAPSHOT_SERVICE, SENTINEL_SERVICE, AUDIT_SERVICE):
        directives = _directives(path)
        assert "AmbientCapabilities=" in directives
        assert "CapabilityBoundingSet=" in directives
        assert "NoNewPrivileges=true" in directives
        assert (
            "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private"
            in directives
        )


def test_units_close_network_to_only_their_exact_api_scope():
    snapshot = _directives(SNAPSHOT_SERVICE)
    sentinel = _directives(SENTINEL_SERVICE)
    audit = _directives(AUDIT_SERVICE)

    assert "Environment=DEEPCOIN_BASE_URL=https://api.deepcoin.com" in snapshot
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in snapshot
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in sentinel
    assert "IPAddressDeny=any" in sentinel
    assert "IPAddressAllow=localhost" in sentinel
    assert "PrivateNetwork=true" in audit
    assert "RestrictAddressFamilies=AF_UNIX" in audit
    for path in (SNAPSHOT_SERVICE, SENTINEL_SERVICE, AUDIT_SERVICE):
        service = _text(path)
        assert "/opt/telegram-kol-analyzer/.env" not in service
        assert "EnvironmentFile=/opt/telegram-kol-analyzer/" not in service


def test_snapshot_uses_only_the_independent_read_only_credential_file():
    snapshot = _text(SNAPSHOT_SERVICE)
    installer = _text(INSTALLER)

    assert "EnvironmentFile=/etc/telegram-kol-monitor-snapshot.credentials" in snapshot
    assert 'SNAPSHOT_CREDENTIAL_FILE="/etc/telegram-kol-monitor-snapshot.credentials"' in installer
    assert 'MAIN_TRADING_CREDENTIAL_FILE="$PRODUCTION_ROOT/.env"' in installer
    assert '"$SNAPSHOT_CREDENTIAL_FILE" -ef "$MAIN_TRADING_CREDENTIAL_FILE"' in installer
    assert "stat -c %u" in installer
    assert "stat -c %a" in installer
    assert "stat -c %h" in installer
    assert "600" in installer
    for key in (
        "DEEPCOIN_API_KEY",
        "DEEPCOIN_API_SECRET",
        "DEEPCOIN_API_PASSPHRASE",
        "DEEPCOIN_BASE_URL",
        "DEEPCOIN_READ_ONLY_PERMISSION_PROOF",
    ):
        assert key in installer
    assert "DEEPCOIN_READ_ONLY_PERMISSION_PROOF=verified-read-only-v1" in installer
    assert "non-allowlisted" in installer


def test_installer_is_install_only_and_refuses_active_or_enabled_targets_preflight():
    installer = _text(INSTALLER)
    preflight = installer[: installer.index("# Preflight complete; mutations may begin.")]

    assert "--enable" not in installer
    assert "systemctl enable" not in installer
    assert "systemctl start" not in installer
    assert "systemctl restart" not in installer
    assert "systemctl is-active --quiet \"$target_unit\"" in preflight
    assert "systemctl is-enabled --quiet \"$target_unit\"" in preflight
    assert "systemctl is-enabled --quiet \"$target_timer\"" in preflight
    for unit in (
        "telegram-kol-monitor-snapshot.service",
        "telegram-kol-monitor-snapshot.timer",
        "telegram-kol-sentinel.service",
        "telegram-kol-sentinel.timer",
        "telegram-kol-monitor-audit.service",
        "telegram-kol-monitor-audit.timer",
    ):
        assert unit in preflight
    for mutation in ("useradd ", "groupadd ", "install -d ", "systemctl daemon-reload"):
        assert mutation not in preflight
    assert "systemctl daemon-reload" in installer


def test_installer_requires_approved_head_and_exact_head_owned_unit_bytes():
    installer = _text(INSTALLER)
    preflight = installer[: installer.index("# Preflight complete; mutations may begin.")]

    assert "--expected-head" in preflight
    assert 'approved_head="$2"' in preflight
    assert '"$expected_head" != "$approved_head"' in preflight
    assert 'git -C "$PRODUCTION_ROOT" cat-file -e' in preflight
    assert 'git -C "$PRODUCTION_ROOT" diff --quiet "$expected_head" --' in preflight
    assert "telegram-kol-monitor-db-stage@.service" in preflight
    assert "src/telegram_kol_research/production_monitor_db_stage.py" in preflight
    assert 'DB_STAGE_HELPER_DEST="/usr/local/libexec/telegram-kol-monitor-db-stage"' in preflight


def test_installer_validates_hardening_before_copying_any_unit():
    installer = _text(INSTALLER)
    preflight = installer[: installer.index("# Preflight complete; mutations may begin.")]

    for directive in (
        "CapabilityBoundingSet=",
        "NoNewPrivileges=true",
        "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private",
    ):
        assert directive in preflight
    assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/data" in preflight
    assert "ReadWritePaths=/var/(lib|cache)/telegram-kol-monitor-v2" in preflight
    assert "unexpected writable path" in preflight
    assert "runuser -u \"$SENTINEL_USER\" -- test -r" in installer
    assert "must not be readable by the sentinel identity" in installer
    assert 'install -o root -g root -m 0755 "$DB_STAGE_HELPER_SOURCE"' in installer
    assert 'stat -c %u "$DB_STAGE_HELPER_DEST"' in preflight
    assert 'stat -c %a "$DB_STAGE_HELPER_DEST"' in preflight
    assert 'stat -c %h "$DB_STAGE_HELPER_DEST"' in preflight


def test_installer_rejects_existing_privileged_or_cross_group_identities():
    installer = _text(INSTALLER)
    mutation = installer[installer.index("# Preflight complete; mutations may begin.") :]

    assert '"$(id -u "$SNAPSHOT_USER")" -eq 0' in mutation
    assert '"$(id -u "$SENTINEL_USER")" -eq 0' in mutation
    assert '"$(id -gn "$SNAPSHOT_USER")" != "$SNAPSHOT_USER"' in mutation
    assert '"$(id -gn "$SENTINEL_USER")" != "$SENTINEL_USER"' in mutation


def test_installer_keeps_snapshot_private_and_uses_root_mediated_read_copy():
    installer = _text(INSTALLER)
    mutation = installer[installer.index("# Preflight complete; mutations may begin.") :]

    assert 'install -d -o root -g root -m 0711 "$STATE_ROOT" "$CACHE_ROOT"' in mutation
    assert (
        'install -d -o "$SNAPSHOT_USER" -g "$SNAPSHOT_USER" -m 0700 '
        '"$STATE_ROOT/snapshot"'
    ) in " ".join(mutation.split())
    assert (
        'install -d -o "$SENTINEL_USER" -g "$SENTINEL_USER" -m 0700'
        in mutation
    )


def test_installer_preflight_rejects_wrong_existing_state_owners():
    installer = _text(INSTALLER)
    preflight = installer[: installer.index("# Preflight complete; mutations may begin.")]

    assert 'stat -c %u "$existing_path"' in preflight
    assert 'stat -c %g "$existing_path"' in preflight
    assert 'stat -c %U "$STATE_ROOT/snapshot"' in preflight
    assert 'stat -c %G "$STATE_ROOT/snapshot"' in preflight
    assert 'stat -c %U "$existing_path"' in preflight
    assert 'stat -c %G "$existing_path"' in preflight
    for state_file in (
        "$STATE_ROOT/snapshot/manifest.json",
        "$STATE_ROOT/sentinel/sentinel-v2.json",
        "$CACHE_ROOT/sentinel/snapshot.json",
        "$CACHE_ROOT/sentinel/coverage.json",
        "$CACHE_ROOT/sentinel/journal.json",
        "$CACHE_ROOT/sentinel/research-snapshot.db",
        "$CACHE_ROOT/audit/research-snapshot.db",
    ):
        assert state_file in preflight
    assert 'stat -c %h "$existing_file"' in preflight


@pytest.mark.skipif(sys.platform != "linux", reason="mount namespaces are Linux-only")
def test_main_unit_namespace_cannot_see_production_data_tree(tmp_path):
    if os.geteuid() != 0 or shutil.which("unshare") is None or shutil.which("mount") is None:
        pytest.skip("mount namespace probe requires Linux root and util-linux")
    checkout = tmp_path / "checkout"
    data = checkout / "data"
    cache = tmp_path / "cache"
    data.mkdir(parents=True)
    cache.mkdir()
    (data / "telegram.session.bak.secret").write_text("credential", encoding="ascii")
    staged = cache / "research-snapshot.db"
    staged.write_text("coherent", encoding="ascii")
    script = f"""
set -eu
mount -t tmpfs -o ro tmpfs {checkout!s}
test ! -e {checkout!s}/data
test "$(cat {staged!s})" = coherent
"""
    completed = subprocess.run(
        ["unshare", "--mount", "--propagation", "private", "sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
