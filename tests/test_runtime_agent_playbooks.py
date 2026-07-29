from telegram_kol_research.runtime_agent_playbooks import (
    RUNTIME_AGENT_PLAYBOOK_CATALOG_VERSION,
    RUNTIME_AGENT_PLAYBOOKS,
    get_runtime_agent_playbook,
)


EXPECTED_PLAYBOOKS = {
    "refresh_read_only_exchange_snapshot",
    "rerun_production_audit",
    "recover_stale_side_effect_free_claim",
    "reschedule_non_writing_ai_job",
    "fetch_missing_telegram_evidence",
    "build_read_only_reconciliation_plan",
}


def test_phase6_catalog_has_six_versioned_low_risk_playbooks():
    assert RUNTIME_AGENT_PLAYBOOK_CATALOG_VERSION == "runtime-playbooks-v1"
    assert set(RUNTIME_AGENT_PLAYBOOKS) == EXPECTED_PLAYBOOKS

    for name, playbook in RUNTIME_AGENT_PLAYBOOKS.items():
        assert playbook.name == name
        assert playbook.version == 1
        assert playbook.shadow_only is False
        assert playbook.shadow_supported is True
        assert playbook.executable_in_phase_6 is True
        assert playbook.permitted_incident_types
        assert playbook.prerequisites
        assert playbook.side_effect_class in {
            "read_only",
            "operational_reversible",
        }
        assert "{incident_id}" in playbook.idempotency_key_template
        assert playbook.maximum_attempts == 1
        assert playbook.verification_query
        assert playbook.terminal_success_condition
        assert playbook.escalation_condition
        assert playbook.refusal_reasons


def test_catalog_lookup_is_closed_and_never_accepts_arbitrary_names():
    assert (
        get_runtime_agent_playbook("refresh_read_only_exchange_snapshot")
        is RUNTIME_AGENT_PLAYBOOKS["refresh_read_only_exchange_snapshot"]
    )
    assert get_runtime_agent_playbook("retry_business_instruction") is None
    assert get_runtime_agent_playbook(None) is None
