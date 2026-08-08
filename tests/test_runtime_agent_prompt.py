from telegram_kol_research.runtime_agent_prompt import (
    RUNTIME_AGENT_PROMPT_VERSION,
    RUNTIME_AGENT_SYSTEM_PROMPT,
)
from telegram_kol_research.runtime_agent_playbooks import (
    RUNTIME_AGENT_PLAYBOOKS,
)


def test_runtime_agent_v8_prompt_exposes_message_operation_diagnosis_contract():
    assert RUNTIME_AGENT_PROMPT_VERSION == "runtime-agent-prompt-v8"
    assert "final JSON object" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "at most 512 characters" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "at most 16 items" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "at most 32 items" in RUNTIME_AGENT_SYSTEM_PROMPT
    for field in (
        "incident_id",
        "diagnosis_hypothesis",
        "confidence",
        "evidence_references",
        "missing_evidence",
        "recommended_playbook_name",
        "auto_handle_eligible",
        "codex_handoff_required",
        "remaining_risk",
        "expected_state",
        "observed_state",
        "classification",
        "affected_message_ids",
        "likely_code_paths",
        "likely_test_paths",
    ):
        assert field in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "expected_safety_refusal" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "message-operation" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "cannot select a strategy" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "nominate" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "deterministic policy" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "model never executes a playbook" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "feature-flagged deterministic Phase 6 policy" in (
        RUNTIME_AGENT_SYSTEM_PROMPT
    )
    assert "unknown-write mutation is permitted" in RUNTIME_AGENT_SYSTEM_PROMPT
    for playbook_name in RUNTIME_AGENT_PLAYBOOKS:
        assert playbook_name in RUNTIME_AGENT_SYSTEM_PROMPT
