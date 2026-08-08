import os
from pathlib import Path
import runpy

import pytest


def test_runtime_agent_sidecar_unit_is_separate_and_dormant_until_enabled():
    path = (
        Path(__file__).parents[1]
        / "deploy"
        / "systemd"
        / "telegram-kol-runtime-agent.service"
    )
    text = path.read_text(encoding="utf-8")

    assert "runtime-incident-agent-worker" in text
    assert "telegram-kol.service" not in text.split("ExecStart=", 1)[1]
    assert "EnvironmentFile=-/opt/telegram-kol-analyzer/config/runtime_incident_agent.env" in text
    assert "Restart=on-failure" in text
    assert "ExecStartPre=+/usr/local/libexec/telegram-kol-runtime-agent-prepare-db-acl" in text
    assert "User=telegram-kol-agent" in text
    assert "Group=telegram-kol-agent" in text
    assert "DynamicUser=yes" not in text
    assert "CapabilityBoundingSet=" in text
    assert "PrivateDevices=true" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/opt/telegram-kol-analyzer/data" in text
    assert "ReadOnlyPaths=/opt/telegram-kol-analyzer/data\n" not in text
    assert "InaccessiblePaths=-/opt/telegram-kol-analyzer/data/telegram.session" in text
    assert "StateDirectory=telegram-kol-runtime-agent" in text
    assert "InaccessiblePaths=/opt/telegram-kol-analyzer/config/runtime_incident_agent.env" in text
    assert "UMask=0077" in text
    assert "WantedBy=multi-user.target" in text

    installer = (
        Path(__file__).parents[1]
        / "scripts"
        / "install_runtime_agent_sidecar.sh"
    ).read_text(encoding="utf-8")
    assert "telegram-kol-runtime-agent.service" in installer
    assert "systemctl daemon-reload" in installer
    assert "systemctl enable --now" not in installer
    assert 'PREPARE_HELPER_SOURCE="$PROJECT_ROOT/deploy/systemd/telegram-kol-runtime-agent-prepare-db-acl"' in installer
    assert 'install -o root -g root -m 0755 "$PREPARE_HELPER_SOURCE" "$PREPARE_HELPER_TARGET"' in installer
    assert "useradd --system" in installer
    assert "setfacl -m" in installer
    assert "runuser -u \"$AGENT_USER\" -- test -w" in installer
    assert "PRIVATE_WORKSPACE=\"/var/lib/telegram-kol-runtime-agent\"" in installer
    assert "test -w \"$PRODUCTION_ROOT/src\"" in installer
    assert "Agent identity can write reviewed source" in installer
    assert 'setfacl -m "u:$AGENT_USER:-wx" "$DATA_DIRECTORY"' in installer
    assert 'd:u:$AGENT_USER:rwx' not in installer
    assert 'setfacl -m "d:g::---,d:o::---" "$DATA_DIRECTORY"' in installer
    assert 'chmod +t "$DATA_DIRECTORY"' in installer
    assert 'find "$DATA_DIRECTORY" -maxdepth 1 -type f -print0' in installer
    assert 'setfacl -m "u:$AGENT_USER:---" "$data_file"' in installer
    assert "Agent identity can access non-allowlisted production data" in installer
    assert "Agent identity cannot create SQLite sidecar files" in installer

    prepare_helper = (
        Path(__file__).parents[1]
        / "deploy"
        / "systemd"
        / "telegram-kol-runtime-agent-prepare-db-acl"
    ).read_text(encoding="utf-8")
    assert 'f"{DATABASE_PATH}-wal"' in prepare_helper
    assert 'f"{DATABASE_PATH}-shm"' in prepare_helper
    assert "os.O_NOFOLLOW" in prepare_helper
    assert "os.O_NONBLOCK" in prepare_helper
    assert "stat.S_ISREG" in prepare_helper
    assert "st_nlink != 1" in prepare_helper
    assert '"/usr/bin/setfacl"' in prepare_helper
    assert "pass_fds=(fd,)" in prepare_helper


def test_runtime_agent_acl_helper_never_opens_sqlite_symlinks(tmp_path):
    helper_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "systemd"
        / "telegram-kol-runtime-agent-prepare-db-acl"
    )
    helper = runpy.run_path(str(helper_path))
    target = tmp_path / "protected-target"
    target.write_text("unchanged", encoding="utf-8")
    original_mode = target.stat().st_mode

    for suffix in ("", "-wal", "-shm", "-journal"):
        link = tmp_path / f"research.db{suffix}"
        link.symlink_to(target)
        with pytest.raises(OSError):
            helper["_open_regular_nofollow"](str(link), optional=False)
        assert target.read_text(encoding="utf-8") == "unchanged"
        assert target.stat().st_mode == original_mode
        os.unlink(link)


def test_runtime_agent_acl_helper_rejects_special_files_without_blocking(tmp_path):
    helper_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "systemd"
        / "telegram-kol-runtime-agent-prepare-db-acl"
    )
    helper = runpy.run_path(str(helper_path))
    for suffix in ("-wal", "-shm", "-journal"):
        fifo = tmp_path / f"research.db{suffix}"
        os.mkfifo(fifo)
        with pytest.raises(RuntimeError, match="single-link regular file"):
            helper["_open_regular_nofollow"](str(fifo), optional=False)
        os.unlink(fifo)

        directory = tmp_path / f"research.db{suffix}"
        directory.mkdir()
        with pytest.raises(RuntimeError, match="single-link regular file"):
            helper["_open_regular_nofollow"](str(directory), optional=False)
        directory.rmdir()
