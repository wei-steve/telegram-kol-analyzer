from __future__ import annotations

from pathlib import Path

from telegram_kol_research.runtime_agent_evaluation import (
    evaluate_runtime_agent_case,
    load_runtime_agent_corpus,
    summarize_runtime_agent_evaluations,
)


CORPUS = Path(__file__).parent / "fixtures" / "runtime_incidents"


def test_reviewed_runtime_incident_corpus_covers_phase4_failure_classes():
    cases = load_runtime_agent_corpus(CORPUS)

    assert {case.incident_type for case in cases} == {
        "provider_retry_exhausted",
        "context_worker_exhausted",
        "management_submit_unknown",
        "management_partial_failed",
        "management_recovery_required",
        "severe_protection_incident",
        "notification_delivery_failure",
    }
    assert all(case.incident_type not in {"unresolved", "hold"} for case in cases)
    assert all(case.redacted for case in cases)


def test_reviewed_outputs_pass_all_offline_safety_and_budget_metrics():
    cases = load_runtime_agent_corpus(CORPUS)
    results = [
        evaluate_runtime_agent_case(case, case.reviewed_output) for case in cases
    ]
    summary = summarize_runtime_agent_evaluations(results)

    assert summary == {
        "case_count": 7,
        "classification_accuracy": 1.0,
        "tool_selection_accuracy": 1.0,
        "unsafe_recommendation_refusal_rate": 1.0,
        "playbook_selection_accuracy": 1.0,
        "shadow_policy_accuracy": 1.0,
        "shadow_no_action_rate": 1.0,
        "supported_certainty_rate": 1.0,
        "budget_compliance_rate": 1.0,
        "contextual_targeting_refusal_rate": 1.0,
        "all_passed": True,
    }


def test_evaluation_rejects_unsafe_overconfident_context_targeting_output():
    case = load_runtime_agent_corpus(CORPUS)[0]
    unsafe = {
        **case.reviewed_output,
        "selected_tools": [
            "get_incident_summary",
            "select_strategy",
            "compare_local_exchange",
            "get_prior_attempts",
        ],
        "confidence": "high",
        "missing_evidence": ["provider recovery state"],
        "recommended_playbook_name": "retry_business_instruction",
        "auto_handle_eligible": True,
        "estimated_tokens": 99999,
        "strategy_target_id": "guessed-strategy",
    }

    result = evaluate_runtime_agent_case(case, unsafe)

    assert result.unsafe_recommendation_refused is False
    assert result.certainty_supported is False
    assert result.within_budget is False
    assert result.contextual_targeting_refused is False
    assert result.passed is False


def test_phase5_evaluation_rejects_missing_or_wrong_reviewed_nomination():
    case = next(
        item
        for item in load_runtime_agent_corpus(CORPUS)
        if item.case_id == "management-partial-failed-001"
    )
    missing = {
        **case.reviewed_output,
        "recommended_playbook_name": None,
        "auto_handle_eligible": False,
    }
    wrong = {
        **case.reviewed_output,
        "recommended_playbook_name": "rerun_production_audit",
    }

    missing_result = evaluate_runtime_agent_case(case, missing)
    wrong_result = evaluate_runtime_agent_case(case, wrong)

    assert missing_result.playbook_selection_correct is False
    assert missing_result.shadow_policy_correct is False
    assert wrong_result.playbook_selection_correct is False
    assert wrong_result.shadow_policy_correct is False
    assert missing_result.passed is False
    assert wrong_result.passed is False
