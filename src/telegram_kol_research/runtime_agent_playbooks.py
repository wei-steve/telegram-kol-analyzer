"""Closed metadata catalog for versioned runtime recovery playbooks."""

from __future__ import annotations

from dataclasses import dataclass


RUNTIME_AGENT_PLAYBOOK_CATALOG_VERSION = "runtime-playbooks-v1"


@dataclass(frozen=True, slots=True)
class RuntimeAgentPlaybook:
    """Deterministic metadata for shadow review and closed execution."""

    name: str
    version: int
    permitted_incident_types: frozenset[str]
    prerequisites: tuple[str, ...]
    side_effect_class: str
    idempotency_key_template: str
    maximum_attempts: int
    verification_query: str
    terminal_success_condition: str
    escalation_condition: str
    refusal_reasons: tuple[str, ...]
    shadow_only: bool = False
    shadow_supported: bool = True
    executable_in_phase_6: bool = False

    def idempotency_key(self, *, incident_id: int) -> str:
        return self.idempotency_key_template.format(
            incident_id=int(incident_id)
        )


def _playbook(
    *,
    name: str,
    incident_types: tuple[str, ...],
    prerequisites: tuple[str, ...],
    side_effect_class: str,
    verification_query: str,
    terminal_success_condition: str,
    escalation_condition: str,
    refusal_reasons: tuple[str, ...],
    executable_in_phase_6: bool = False,
) -> RuntimeAgentPlaybook:
    return RuntimeAgentPlaybook(
        name=name,
        version=1,
        permitted_incident_types=frozenset(incident_types),
        prerequisites=prerequisites,
        side_effect_class=side_effect_class,
        idempotency_key_template=(
            "runtime-incident:{incident_id}:"
            + name.replace("_", "-")
            + ":v1"
        ),
        maximum_attempts=1,
        verification_query=verification_query,
        terminal_success_condition=terminal_success_condition,
        escalation_condition=escalation_condition,
        refusal_reasons=refusal_reasons,
        executable_in_phase_6=executable_in_phase_6,
    )


_PLAYBOOK_LIST = (
    _playbook(
        name="refresh_read_only_exchange_snapshot",
        incident_types=(
            "management_partial_failed",
            "management_recovery_required",
            "management_submit_unknown",
            "severe_protection_incident",
        ),
        prerequisites=("incident_evidence_present",),
        side_effect_class="read_only",
        verification_query="compare_local_exchange",
        terminal_success_condition="fresh_bounded_snapshot_available",
        escalation_condition="snapshot_unavailable_or_incoherent",
        refusal_reasons=(
            "incident_evidence_missing",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
    _playbook(
        name="rerun_production_audit",
        incident_types=(
            "management_partial_failed",
            "management_recovery_required",
            "severe_protection_incident",
        ),
        prerequisites=("incident_evidence_present",),
        side_effect_class="read_only",
        verification_query="get_service_audit_state",
        terminal_success_condition="bounded_audit_result_recorded",
        escalation_condition="audit_unavailable_or_incomplete",
        refusal_reasons=(
            "incident_evidence_missing",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
    _playbook(
        name="recover_stale_side_effect_free_claim",
        incident_types=(
            "context_worker_exhausted",
            "provider_retry_exhausted",
            "notification_delivery_failure",
        ),
        prerequisites=(
            "incident_evidence_present",
            "stale_claim_proven",
            "side_effect_free_claim_proven",
        ),
        side_effect_class="operational_reversible",
        verification_query="get_worker_state",
        terminal_success_condition="claim_returned_to_original_safe_queue",
        escalation_condition="claim_is_live_or_side_effect_free_state_unknown",
        refusal_reasons=(
            "incident_evidence_missing",
            "stale_claim_not_proven",
            "side_effect_free_claim_not_proven",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
    _playbook(
        name="reschedule_non_writing_ai_job",
        incident_types=(
            "context_worker_exhausted",
            "provider_retry_exhausted",
        ),
        prerequisites=(
            "incident_evidence_present",
            "business_write_absence_proven",
        ),
        side_effect_class="operational_reversible",
        verification_query="get_worker_state",
        terminal_success_condition="original_ai_job_rescheduled_once",
        escalation_condition="business_write_ownership_unknown_or_job_not_safe",
        refusal_reasons=(
            "incident_evidence_missing",
            "business_write_absence_not_proven",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
    _playbook(
        name="fetch_missing_telegram_evidence",
        incident_types=(
            "context_worker_exhausted",
            "notification_delivery_failure",
            "provider_retry_exhausted",
        ),
        prerequisites=("incident_evidence_present",),
        side_effect_class="read_only",
        verification_query="get_incident_summary",
        terminal_success_condition="bounded_telegram_evidence_recorded",
        escalation_condition="evidence_unavailable_or_out_of_bounds",
        refusal_reasons=(
            "incident_evidence_missing",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
    _playbook(
        name="build_read_only_reconciliation_plan",
        incident_types=(
            "management_partial_failed",
            "management_recovery_required",
            "management_submit_unknown",
            "severe_protection_incident",
        ),
        prerequisites=("incident_evidence_present",),
        side_effect_class="read_only",
        verification_query="compare_local_exchange",
        terminal_success_condition="read_only_plan_recorded_without_execution",
        escalation_condition="ownership_or_exchange_state_is_incomplete",
        refusal_reasons=(
            "incident_evidence_missing",
            "incident_type_not_permitted",
        ),
        executable_in_phase_6=True,
    ),
)

RUNTIME_AGENT_PLAYBOOKS = {
    playbook.name: playbook for playbook in _PLAYBOOK_LIST
}


def get_runtime_agent_playbook(
    name: str | None,
) -> RuntimeAgentPlaybook | None:
    if not isinstance(name, str):
        return None
    return RUNTIME_AGENT_PLAYBOOKS.get(name)
