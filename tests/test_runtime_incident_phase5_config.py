import pytest

from telegram_kol_research.config import load_runtime_incident_config


def test_phase5_shadow_playbooks_are_dormant_and_exact_allowlisted():
    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_AGENT_SHADOW_PLAYBOOKS": (
                " refresh_read_only_exchange_snapshot,"
                "retry_business_instruction,REFRESH_READ_ONLY_EXCHANGE_SNAPSHOT "
            )
        },
        env_file_paths=[],
    )

    assert dormant.agent_shadow_playbooks == frozenset()
    assert enabled.agent_shadow_playbooks == frozenset(
        {
            "refresh_read_only_exchange_snapshot",
            "retry_business_instruction",
        }
    )
    assert dormant.feature_policy_version == "runtime-incident-phase-6-v1"


def test_phase6_action_authority_is_dormant_bounded_and_exact_allowlisted():
    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS": (
                " refresh_read_only_exchange_snapshot,"
                "RETRY_BUSINESS_INSTRUCTION "
            ),
            "TELEGRAM_KOL_RUNTIME_AGENT_ACTION_CIRCUIT_THRESHOLD": "99",
        },
        env_file_paths=[],
    )

    assert dormant.agent_actions_enabled is False
    assert dormant.agent_action_playbooks == frozenset()
    assert enabled.agent_actions_enabled is True
    assert enabled.agent_action_playbooks == frozenset(
        {
            "refresh_read_only_exchange_snapshot",
            "retry_business_instruction",
        }
    )
    assert enabled.agent_action_circuit_threshold == 5


def test_runtime_config_uses_systemd_environment_when_secret_file_is_unreadable(
    tmp_path,
    monkeypatch,
):
    secret_file = tmp_path / "runtime_incident_agent.env"
    secret_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(secret_file):
            raise PermissionError("root-owned secret")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    config = load_runtime_incident_config(
        environ={"TELEGRAM_KOL_RUNTIME_AGENT_ENABLED": "true"},
        env_file_paths=[secret_file],
    )

    assert config.agent_enabled is True


def test_runtime_config_does_not_hide_unreadable_non_secret_config(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "telegram.env"
    config_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(config_file):
            raise PermissionError("unexpected permissions")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    with pytest.raises(PermissionError):
        load_runtime_incident_config(
            environ={},
            env_file_paths=[config_file],
        )


def test_runtime_config_prefers_complete_systemd_environment_over_unreadable_defaults(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "telegram.env"
    config_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(config_file):
            raise PermissionError("root-owned unrelated config")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", "false")

    config = load_runtime_incident_config(
        env_file_paths=[config_file],
    )

    assert config.agent_enabled is False
