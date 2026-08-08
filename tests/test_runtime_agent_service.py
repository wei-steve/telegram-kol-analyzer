from pathlib import Path


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
    assert "User=telegram-kol-agent" in text
    assert "Group=telegram-kol-agent" in text
    assert "DynamicUser=yes" not in text
    assert "CapabilityBoundingSet=" in text
    assert "PrivateDevices=true" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/opt/telegram-kol-analyzer/data" in text
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
    assert "useradd --system" in installer
    assert "setfacl -m" in installer
    assert "runuser -u \"$AGENT_USER\" -- test -w" in installer
    assert "PRIVATE_WORKSPACE=\"/var/lib/telegram-kol-runtime-agent\"" in installer
    assert "test -w \"$PRODUCTION_ROOT/src\"" in installer
    assert "Agent identity can write reviewed source" in installer
    assert 'setfacl -m "u:$AGENT_USER:-wx" "$DATA_DIRECTORY"' in installer
    assert 'd:u:$AGENT_USER:rwx' not in installer
    assert 'chmod +t "$DATA_DIRECTORY"' in installer
    assert 'find "$DATA_DIRECTORY" -maxdepth 1 -type f -print0' in installer
    assert 'setfacl -x "u:$AGENT_USER" "$data_file"' in installer
    assert "Agent identity can access non-allowlisted production data" in installer
    assert "Agent identity cannot create SQLite sidecar files" in installer
