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
