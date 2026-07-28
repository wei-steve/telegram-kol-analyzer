from telegram_kol_research.runtime_agent_prompt import (
    RUNTIME_AGENT_PROMPT_VERSION,
    RUNTIME_AGENT_SYSTEM_PROMPT,
)


def test_runtime_agent_v2_prompt_exposes_the_closed_final_contract():
    assert RUNTIME_AGENT_PROMPT_VERSION == "runtime-agent-prompt-v2"
    assert "final JSON object" in RUNTIME_AGENT_SYSTEM_PROMPT
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
    ):
        assert field in RUNTIME_AGENT_SYSTEM_PROMPT
