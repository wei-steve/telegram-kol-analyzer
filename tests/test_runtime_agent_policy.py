from telegram_kol_research.runtime_agent_policy import (
    RUNTIME_AGENT_SHADOW_POLICY_VERSION,
    evaluate_execution_playbook_nomination,
    evaluate_shadow_playbook_nomination,
)
from telegram_kol_research.runtime_agent_evaluation import (
    load_runtime_agent_corpus,
)


CORPUS = "tests/fixtures/runtime_incidents"


def _incident(**summary):
    return {
        "id": 17,
        "incident_type": "management_partial_failed",
        "source_kind": "strategy_management_batch",
        "generation": 2,
        "redacted_summary": summary,
    }


def test_shadow_policy_accepts_allowlisted_read_only_nomination_without_execution():
    decision = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook="refresh_read_only_exchange_snapshot",
        enabled_playbooks=frozenset(
            {"refresh_read_only_exchange_snapshot"}
        ),
        evidence_references=("incident:17", "exchange-snapshot:9"),
    )

    assert decision.accepted is True
    assert decision.refusal_reasons == ()
    assert decision.executed is False
    assert decision.would_execute is False
    assert decision.mode == "shadow"
    assert decision.policy_version == RUNTIME_AGENT_SHADOW_POLICY_VERSION
    assert decision.idempotency_key == (
        "runtime-incident:17:refresh-read-only-exchange-snapshot:v1"
    )
    assert decision.recovery_status == "shadow_accepted"


def test_shadow_policy_refuses_disabled_unknown_mismatched_and_missing_evidence():
    disabled = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook="refresh_read_only_exchange_snapshot",
        enabled_playbooks=frozenset(),
        evidence_references=("incident:17",),
    )
    unknown = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook="retry_business_instruction",
        enabled_playbooks=frozenset({"retry_business_instruction"}),
        evidence_references=("incident:17",),
    )
    mismatched = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook="fetch_missing_telegram_evidence",
        enabled_playbooks=frozenset({"fetch_missing_telegram_evidence"}),
        evidence_references=("incident:17",),
    )
    missing = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook="refresh_read_only_exchange_snapshot",
        enabled_playbooks=frozenset(
            {"refresh_read_only_exchange_snapshot"}
        ),
        evidence_references=(),
    )

    assert disabled.refusal_reasons == ("playbook_disabled",)
    assert unknown.refusal_reasons == ("unknown_playbook",)
    assert mismatched.refusal_reasons == ("incident_type_not_permitted",)
    assert missing.refusal_reasons == ("incident_evidence_missing",)
    assert all(
        decision.recovery_status == "shadow_refused"
        for decision in (disabled, unknown, mismatched, missing)
    )
    assert all(
        decision.executed is False
        for decision in (disabled, unknown, mismatched, missing)
    )


def test_operational_shadow_playbooks_require_exact_non_writing_proof():
    stale_refused = evaluate_shadow_playbook_nomination(
        incident={
            **_incident(claim_status="stale"),
            "incident_type": "context_worker_exhausted",
        },
        nominated_playbook="recover_stale_side_effect_free_claim",
        enabled_playbooks=frozenset(
            {"recover_stale_side_effect_free_claim"}
        ),
        evidence_references=("incident:17",),
    )
    reschedule_refused = evaluate_shadow_playbook_nomination(
        incident={
            **_incident(business_write_owned=None),
            "incident_type": "provider_retry_exhausted",
        },
        nominated_playbook="reschedule_non_writing_ai_job",
        enabled_playbooks=frozenset({"reschedule_non_writing_ai_job"}),
        evidence_references=("incident:17",),
    )
    reschedule_accepted = evaluate_shadow_playbook_nomination(
        incident={
            **_incident(business_write_owned=False),
            "incident_type": "provider_retry_exhausted",
        },
        nominated_playbook="reschedule_non_writing_ai_job",
        enabled_playbooks=frozenset({"reschedule_non_writing_ai_job"}),
        evidence_references=("incident:17",),
    )

    assert stale_refused.refusal_reasons == (
        "side_effect_free_claim_not_proven",
    )
    assert reschedule_refused.refusal_reasons == (
        "business_write_absence_not_proven",
    )
    assert reschedule_accepted.accepted is True
    assert reschedule_accepted.executed is False


def test_no_nomination_is_recorded_as_not_requested():
    decision = evaluate_shadow_playbook_nomination(
        incident=_incident(),
        nominated_playbook=None,
        enabled_playbooks=frozenset(
            {"refresh_read_only_exchange_snapshot"}
        ),
        evidence_references=("incident:17",),
    )

    assert decision.accepted is False
    assert decision.refusal_reasons == ("no_nomination",)
    assert decision.recovery_status == "not_requested"
    assert decision.to_ledger_mapping()["action_executed"] is False


def test_management_target_incidents_can_never_nominate_playbooks():
    incident = {
        **_incident(),
        "incident_type": "management_target_orchestration_failed",
        "source_kind": "management_message_target",
    }
    shadow = evaluate_shadow_playbook_nomination(
        incident=incident,
        nominated_playbook="refresh_read_only_exchange_snapshot",
        enabled_playbooks=frozenset({"refresh_read_only_exchange_snapshot"}),
        evidence_references=("incident:17",),
    )
    execution = evaluate_execution_playbook_nomination(
        incident=incident,
        nominated_playbook="refresh_read_only_exchange_snapshot",
        actions_enabled=True,
        enabled_playbooks=frozenset({"refresh_read_only_exchange_snapshot"}),
        evidence_references=("incident:17",),
    )

    assert shadow.accepted is False
    assert shadow.refusal_reasons == ("management_target_diagnosis_only",)
    assert execution.accepted is False
    assert execution.refusal_reasons == ("management_target_diagnosis_only",)
    assert execution.would_execute is False


def test_reviewed_phase5_corpus_has_zero_accepted_unsafe_actions():
    cases = load_runtime_agent_corpus(CORPUS)
    decisions = []
    for incident_id, case in enumerate(cases, start=1):
        nomination = case.reviewed_output["recommended_playbook_name"]
        decision = evaluate_shadow_playbook_nomination(
            incident={
                "id": incident_id,
                "incident_type": case.incident_type,
                "source_kind": case.source_kind,
                "generation": 1,
                "redacted_summary": case.redacted_summary,
            },
            nominated_playbook=nomination,
            enabled_playbooks=(
                frozenset({nomination}) if nomination else frozenset()
            ),
            evidence_references=(f"incident:{incident_id}",),
        )
        decisions.append(decision)
        assert decision.accepted is case.expectation[
            "shadow_policy_accepted"
        ]
        assert decision.executed is False

    unsafe_names = {
        "reschedule_non_writing_ai_job",
        "recover_stale_side_effect_free_claim",
    }
    assert not any(
        decision.accepted
        for decision in decisions
        if decision.nominated_playbook in unsafe_names
    )
