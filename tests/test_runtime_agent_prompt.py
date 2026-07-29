from telegram_kol_research.runtime_agent_prompt import (
    RUNTIME_AGENT_PROMPT_VERSION,
    RUNTIME_AGENT_SYSTEM_PROMPT,
)
from telegram_kol_research.runtime_agent_playbooks import (
    RUNTIME_AGENT_PLAYBOOKS,
)


def test_runtime_agent_v3_prompt_exposes_the_closed_shadow_contract():
    assert RUNTIME_AGENT_PROMPT_VERSION == "runtime-agent-prompt-v3"
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
    assert "nominate" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "deterministic policy" in RUNTIME_AGENT_SYSTEM_PROMPT
    assert "no playbook executes" in RUNTIME_AGENT_SYSTEM_PROMPT
    for playbook_name in RUNTIME_AGENT_PLAYBOOKS:
        assert playbook_name in RUNTIME_AGENT_SYSTEM_PROMPT
