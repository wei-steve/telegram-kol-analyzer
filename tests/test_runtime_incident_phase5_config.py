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
