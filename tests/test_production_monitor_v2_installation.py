import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
INSTALLER = ROOT / "scripts" / "install_production_monitor_v2.sh"
UV_CACHE_VALIDATOR = ROOT / "scripts" / "validate_production_monitor_uv_cache.py"
RUNBOOK = ROOT / "docs" / "production-monitor-v2-runbook.md"

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
    sealed_runtime = "/opt/telegram-kol-monitor-v2/current"
    for service in (snapshot, sentinel, audit):
        assert f"{sealed_runtime}/.venv/bin/telegram-kol-research" in " ".join(service)
        assert "/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research" not in " ".join(service)
        assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/.venv" not in service
        assert "BindReadOnlyPaths=/opt/telegram-kol-analyzer/src" not in service
    assert f"--checkout-path {sealed_runtime}" in " ".join(sentinel)


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

    assert 'PATH="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in preflight
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


def test_installer_fail_closed_checks_complete_checkout_and_import_closure():
    installer = _text(INSTALLER)
    preflight = installer[: installer.index("# Preflight complete; mutations may begin.")]

    assert "scripts/install_production_monitor_v2.sh" in preflight
    assert "The running installer must be root-owned and non-writable" in preflight
    assert preflight.index("installer_real_path=") < preflight.index("approved_head=")
    assert 'git -C "$PRODUCTION_ROOT" diff --quiet --ignore-submodules=none' in preflight
    assert 'git -C "$PRODUCTION_ROOT" diff --cached --quiet --ignore-submodules=none' in preflight
    assert 'git -C "$PRODUCTION_ROOT" ls-files --others --exclude-standard -- .' in preflight
    assert 'git -C "$PRODUCTION_ROOT" ls-files -t -v' in preflight
    assert 'find "$PRODUCTION_ROOT/src" "$PRODUCTION_ROOT/scripts"' in preflight
    assert '! -type d -print0' in preflight
    for suffix in ("*.py", "*.pyc", "*.pyo", "*.so", "*.dylib", "*.pth"):
        assert suffix in preflight
    assert 'git -C "$PRODUCTION_ROOT" --literal-pathspecs' in preflight
    assert 'ls-files --error-unmatch -- "$relative_path"' in preflight
    assert "The complete production checkout differs from the approved SHA" in preflight
    assert "untracked path" in preflight
    assert "untracked or ignored import shadow" in preflight


def test_installer_builds_and_uses_root_owned_locked_sealed_runtime():
    installer = _text(INSTALLER)
    mutation = installer[installer.index("# Preflight complete; mutations may begin.") :]

    assert 'RELEASE_ROOT="/opt/telegram-kol-monitor-v2/releases"' in installer
    assert 'CURRENT_RELEASE_LINK="/opt/telegram-kol-monitor-v2/current"' in installer
    assert 'uv sync --locked --offline --no-dev' in mutation
    assert "--frozen" not in mutation
    assert "UV_LINK_MODE=copy" in mutation
    assert (
        'UV_CACHE_ROOT="/var/cache/telegram-kol-monitor-v2-build/uv"'
        in installer
    )
    assert 'UV_CACHE_TRUST_ANCHOR="/var/cache"' in installer
    assert (
        'UV_CACHE_PARENT="/var/cache/telegram-kol-monitor-v2-build"'
        in installer
    )
    assert 'UV_BUILD_CONSTRAINT="$release_staging/config/production-monitor-build-constraints.txt"' in mutation
    constraints = ROOT / "config" / "production-monitor-build-constraints.txt"
    assert constraints.read_text(encoding="utf-8").splitlines() == [
        "setuptools==80.9.0",
        "wheel==0.45.1",
    ]
    assert "command -v python3.12" in installer
    assert "UV_PYTHON=\"$python_path\"" in mutation
    assert 'git -c core.hooksPath=/dev/null -C "$release_staging" fetch' in mutation
    assert '"$PRODUCTION_ROOT" "$expected_head"' in mutation
    assert 'git -c core.hooksPath=/dev/null -C "$release_staging" checkout' in mutation
    assert '"--detach" "$expected_head"' in mutation
    assert 'mkdir -m 0700 "$sealed_release"' in mutation
    assert 'chown -R root:root "$release_staging"' in mutation
    assert 'chmod -R u=rwX,go=rX "$release_staging"' in mutation
    assert 'chmod -R a-w "$release_staging"' in mutation
    assert 'runuser -u "$SNAPSHOT_USER" -- env -i' in mutation
    assert mutation.count('runuser -u "$SENTINEL_USER" -- env -i') >= 2
    assert 'mv "$release_staging" "$sealed_release"' not in mutation
    assert 'ln -s "$sealed_release" "$current_link_staging"' in mutation
    assert 'mv -T "$current_link_staging" "$CURRENT_RELEASE_LINK"' in mutation
    assert '"$sealed_release/deploy/systemd/$source_unit"' in mutation
    assert '"$sealed_release/src/telegram_kol_research/production_monitor_db_stage.py"' in mutation
    assert "must be root-owned and non-writable" in installer
    assert "must execute only the sealed approved runtime" in installer
    assert 'find "$PRODUCTION_ROOT/.git"' in installer
    assert 'find "$sealed_release"' in installer
    assert 'find "$sealed_release" -xdev -type l -print0' in installer
    assert 'readlink -f "$sealed_link"' in installer
    assert 'validate_production_monitor_uv_cache.py' in installer
    assert '--trust-anchor "$UV_CACHE_TRUST_ANCHOR"' in installer
    assert '--expected-owner-uid 0' in installer
    assert r'\( ! -user root -o -type l -o -perm /022 \)' not in installer
    assert "Locked dependency cache must be root-owned and non-writable" in installer
    for command in (
        "refresh-production-monitor-snapshot",
        "run-production-monitor-sentinel",
        "run-production-monitor-audit",
    ):
        assert f'"{command}" --help' in mutation
    assert 'head -n 1 "$release_entrypoint"' in mutation
    assert "/usr/bin/timeout 15" in mutation
    assert mutation.index("systemctl daemon-reload") < mutation.index(
        'mv -T "$current_link_staging" "$CURRENT_RELEASE_LINK"'
    )
    assert mutation.index('mv -T "$current_link_staging" "$CURRENT_RELEASE_LINK"') < mutation.rindex(
        'release_staging=""'
    )


def test_uv_cache_validator_accepts_internal_links_and_rejects_escape(tmp_path):
    cache = tmp_path / "uv"
    archive = cache / "archive-v0" / "trusted"
    wheel = cache / "wheels-v6" / "pypi" / "example"
    archive.mkdir(parents=True)
    wheel.mkdir(parents=True)
    (archive / "METADATA").write_text("trusted\n", encoding="utf-8")
    internal = wheel / "1.0-py3-none-any"
    internal.symlink_to("../../../archive-v0/trusted")

    accepted = subprocess.run(
        [
            sys.executable,
            str(UV_CACHE_VALIDATOR),
            "--cache-root",
            str(cache),
            "--trust-anchor",
            str(tmp_path),
            "--expected-owner-uid",
            str(os.getuid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr

    outside = tmp_path / "outside"
    outside.write_text("untrusted\n", encoding="utf-8")
    internal.unlink()
    internal.symlink_to(outside)
    rejected = subprocess.run(
        [
            sys.executable,
            str(UV_CACHE_VALIDATOR),
            "--cache-root",
            str(cache),
            "--trust-anchor",
            str(tmp_path),
            "--expected-owner-uid",
            str(os.getuid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "symlink" in rejected.stderr.lower()


@pytest.mark.parametrize("case", ["writable_parent", "ancestor_symlink"])
def test_uv_cache_validator_rejects_mutable_or_linked_ancestor(tmp_path, case):
    trust_anchor = tmp_path / "var-cache"
    trust_anchor.mkdir()
    if case == "writable_parent":
        parent = trust_anchor / "telegram-kol-monitor-v2-build"
        parent.mkdir(mode=0o700)
        parent.chmod(0o770)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        parent = trust_anchor / "telegram-kol-monitor-v2-build"
        parent.symlink_to(outside, target_is_directory=True)
    cache = parent / "uv"
    if case == "writable_parent":
        cache.mkdir(mode=0o700)
    else:
        (outside / "uv").mkdir(mode=0o700)

    completed = subprocess.run(
        [
            sys.executable,
            str(UV_CACHE_VALIDATOR),
            "--cache-root",
            str(cache),
            "--trust-anchor",
            str(trust_anchor),
            "--expected-owner-uid",
            str(os.getuid()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "ancestor" in completed.stderr.lower()


def test_prewarmed_uv_cache_performs_real_locked_offline_build(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the sealed runtime integration probe")
    cache = subprocess.run(
        [uv, "cache", "dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = {
        "HOME": "/nonexistent",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UV_BUILD_CONSTRAINT": str(
            ROOT / "config" / "production-monitor-build-constraints.txt"
        ),
        "UV_CACHE_DIR": cache,
        "UV_LINK_MODE": "copy",
        "UV_NO_CONFIG": "1",
        "UV_NO_MANAGED_PYTHON": "1",
        "UV_OFFLINE": "1",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "sealed-venv"),
        "UV_PYTHON": sys.executable,
    }
    completed = subprocess.run(
        [
            uv,
            "sync",
            "--project",
            str(ROOT),
            "--locked",
            "--offline",
            "--no-dev",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        tmp_path / "sealed-venv" / "bin" / "telegram-kol-research"
    ).is_file()


def test_runbook_prewarms_dedicated_cache_then_proves_locked_offline_build():
    runbook = _text(RUNBOOK)
    cache = "/var/cache/telegram-kol-monitor-v2-build/uv"
    prewarm = runbook[runbook.index("## Sealed runtime dependency cache") :]

    assert cache in prewarm
    assert "BUILD_CACHE_PARENT=/var/cache/telegram-kol-monitor-v2-build" in prewarm
    assert "BUILD_CACHE_TRUST_ANCHOR=/var/cache" in prewarm
    warm_command = (
        '"$UV_PATH" sync --project "$PREWARM_ROOT/source" --locked --no-dev'
    )
    prune_command = '"$UV_PATH" cache prune --ci'
    offline_command = (
        '"$UV_PATH" sync --project "$PREWARM_ROOT/source" '
        "--locked --offline --no-dev"
    )
    assert warm_command in prewarm
    assert prune_command in prewarm
    assert "validate_production_monitor_uv_cache.py" in prewarm
    approved_validator = (
        '"$PREWARM_ROOT/source/scripts/validate_production_monitor_uv_cache.py"'
    )
    validation_command = "sudo /usr/bin/python3 \\\n  " + approved_validator
    assert approved_validator in prewarm
    assert (
        '"$PRODUCTION_ROOT/scripts/validate_production_monitor_uv_cache.py"'
        not in prewarm
    )
    assert "tar --no-same-owner -x" in prewarm
    anchor_proof = (
        'test ! -L "$BUILD_CACHE_TRUST_ANCHOR"'
    )
    parent_creation = (
        'sudo install -d -o root -g root -m 0700 '
        '"$BUILD_CACHE_PARENT" "$BUILD_CACHE"'
    )
    assert anchor_proof in prewarm
    assert parent_creation in prewarm
    assert prewarm.index(anchor_proof) < prewarm.index(parent_creation)
    assert '--trust-anchor "$BUILD_CACHE_TRUST_ANCHOR"' in prewarm
    assert prewarm.index(parent_creation) < prewarm.index(validation_command)
    assert prewarm.index(validation_command) < prewarm.index(warm_command)
    assert prewarm.count(validation_command) == 3
    assert offline_command in prewarm
    assert prewarm.index(
        "PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ) < prewarm.index('UV_PATH="$(readlink -f "$(command -v uv)")"')
    first_root_execution = prewarm.index("sudo /usr/bin/mktemp -d")
    for proof in (
        'test "$(stat -c %u "$TRUSTED_TOOL")" = 0',
        'test "$(stat -c %h "$TRUSTED_TOOL")" = 1',
        "8#$TOOL_MODE & 8#022",
        "sys.version_info[:2] == (3, 12)",
    ):
        assert proof in prewarm
        assert prewarm.index(proof) < first_root_execution
    assert prewarm.index("trap cleanup_monitor_prewarm EXIT") < first_root_execution
    assert "/var/tmp/telegram-kol-monitor-v2-prewarm.??????)" in prewarm
    assert "refusing unsafe prewarm cleanup path" in prewarm
    first_uv_build = prewarm.index("sudo env -i")
    for approved_source_proof in (
        'find "$PREWARM_ROOT/source" -xdev',
        '! -user root -o -perm /022 -o -type l',
        'sudo test -f "$PREWARM_VALIDATOR"',
        'sudo test ! -L "$PREWARM_VALIDATOR"',
        'stat -c %u "$PREWARM_VALIDATOR"',
        'stat -c %h "$PREWARM_VALIDATOR"',
    ):
        assert approved_source_proof in prewarm
        assert prewarm.index(approved_source_proof) < first_uv_build
    first_prune = prewarm.index(prune_command)
    post_warm_validation = prewarm.index(validation_command, first_prune)
    assert prewarm.index(warm_command) < first_prune
    assert first_prune < post_warm_validation
    assert post_warm_validation < prewarm.index(offline_command)


def test_installer_failure_cleanup_stays_armed_until_atomic_current_switch():
    installer = _text(INSTALLER)
    mutation = installer[installer.index("# Preflight complete; mutations may begin.") :]
    cleanup = mutation[
        mutation.index("cleanup_installer_temporaries()") : mutation.index(
            "trap cleanup_installer_temporaries EXIT"
        )
    ]
    switch = 'mv -T "$current_link_staging" "$CURRENT_RELEASE_LINK"'

    assert 'rm -rf -- "$release_staging"' in cleanup
    assert '"$release_staging" == "$RELEASE_ROOT/$expected_head"' in cleanup
    assert mutation.index('release_staging="$sealed_release"') < mutation.index(
        'install -o root -g root -m 0600 "$sentinel_env"'
    )
    assert mutation.index('release_staging="$sealed_release"') < mutation.index(
        '"$sealed_release/src/telegram_kol_research/production_monitor_db_stage.py"'
    )
    assert mutation.index('release_staging="$sealed_release"') < mutation.index(
        'install -o root -g root -m 0644'
    )
    assert mutation.index("systemctl daemon-reload") < mutation.index(switch)
    assert mutation.index(switch) < mutation.rindex('release_staging=""')


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("dirty_core", "complete production checkout differs"),
        ("staged_core", "complete production checkout differs"),
        ("dirty_installer", "complete production checkout differs"),
        ("untracked", "contains an untracked path"),
        ("ignored_import", "untracked or ignored import shadow"),
        ("skip_worktree", "hidden index override"),
    ],
)
def test_installer_checkout_gate_rejects_dirty_and_shadowed_runtime_closure(
    tmp_path, case, expected_error
):
    repository = tmp_path / "production-checkout"
    scripts = repository / "scripts"
    package = repository / "src" / "telegram_kol_research"
    scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    installer_path = scripts / INSTALLER.name
    installer = _text(INSTALLER).replace(
        'if [[ "$(id -u)" -ne 0 ]]; then',
        "if false; then",
        1,
    ).replace(
        'PRODUCTION_ROOT="/opt/telegram-kol-analyzer"',
        f'PRODUCTION_ROOT="{repository}"',
        1,
    )
    installer_path.write_text(installer, encoding="utf-8")
    core = package / "core.py"
    core.write_text("VALUE = 1\n", encoding="utf-8")
    (repository / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "monitor-test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Monitor Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=repository,
        check=True,
    )
    approved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    if case == "dirty_core":
        core.write_text("VALUE = 2\n", encoding="utf-8")
    elif case == "staged_core":
        core.write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", str(core)], cwd=repository, check=True)
    elif case == "dirty_installer":
        installer_path.write_text(installer + "\n# dirty\n", encoding="utf-8")
    elif case == "untracked":
        (repository / "surprise.txt").write_text("shadow\n", encoding="utf-8")
    elif case == "ignored_import":
        (package / "shadow.pyc").write_bytes(b"not-a-trusted-import")
    else:
        subprocess.run(
            ["git", "update-index", "--skip-worktree", str(core)],
            cwd=repository,
            check=True,
        )

    completed = subprocess.run(
        ["bash", str(installer_path), "--expected-head", approved_head],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert expected_error in completed.stderr


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
    assert '"$sealed_release/src/telegram_kol_research/production_monitor_db_stage.py"' in installer
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
