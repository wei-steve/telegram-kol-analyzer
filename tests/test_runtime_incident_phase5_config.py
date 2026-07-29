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
    assert dormant.feature_policy_version == "runtime-incident-phase-5-v1"
