"""Deterministic policies for shadow and closed low-risk playbooks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from telegram_kol_research.runtime_agent_playbooks import (
    get_runtime_agent_playbook,
)
from telegram_kol_research.runtime_incident_snapshot import (
    MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES,
)


RUNTIME_AGENT_SHADOW_POLICY_VERSION = "runtime-shadow-policy-v1"
RUNTIME_AGENT_EXECUTION_POLICY_VERSION = "runtime-execution-policy-v1"


@dataclass(frozen=True, slots=True)
class ShadowPlaybookDecision:
    nominated_playbook: str | None
    playbook_version: int | None
    accepted: bool
    refusal_reasons: tuple[str, ...]
    idempotency_key: str | None
    verification_query: str | None
    mode: str = "shadow"
    policy_version: str = RUNTIME_AGENT_SHADOW_POLICY_VERSION
    would_execute: bool = False
    executed: bool = False

    @property
    def recovery_status(self) -> str:
        if self.nominated_playbook is None:
            return "not_requested"
        return "shadow_accepted" if self.accepted else "shadow_refused"

    def to_ledger_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_version": self.policy_version,
            "nominated_playbook": self.nominated_playbook,
            "playbook_version": self.playbook_version,
            "accepted": self.accepted,
            "refusal_reasons": list(self.refusal_reasons),
            "verification_query": self.verification_query,
            "would_execute": self.would_execute,
            "action_executed": self.executed,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlaybookDecision:
    nominated_playbook: str | None
    playbook_version: int | None
    accepted: bool
    refusal_reasons: tuple[str, ...]
    idempotency_key: str | None
    verification_query: str | None
    mode: str = "execute"
    policy_version: str = RUNTIME_AGENT_EXECUTION_POLICY_VERSION
    would_execute: bool = False
    executed: bool = False

    @property
    def recovery_status(self) -> str:
        if self.nominated_playbook is None:
            return "not_requested"
        return "action_ready" if self.accepted else "action_refused"


def _summary(incident: Mapping[str, Any]) -> Mapping[str, Any]:
    value = incident.get("redacted_summary")
    return value if isinstance(value, Mapping) else {}


def _prerequisite_refusals(
    *,
    playbook_name: str,
    incident: Mapping[str, Any],
    evidence_references: Sequence[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    incident_id = int(incident["id"])
    if f"incident:{incident_id}" not in evidence_references:
        reasons.append("incident_evidence_missing")
    summary = _summary(incident)
    if playbook_name == "recover_stale_side_effect_free_claim":
        if summary.get("claim_status") != "stale":
            reasons.append("stale_claim_not_proven")
        if summary.get("claim_side_effect_class") != "none":
            reasons.append("side_effect_free_claim_not_proven")
    elif playbook_name == "reschedule_non_writing_ai_job":
        if summary.get("business_write_owned") is not False:
            reasons.append("business_write_absence_not_proven")
    return tuple(reasons)


def evaluate_shadow_playbook_nomination(
    *,
    incident: Mapping[str, Any],
    nominated_playbook: str | None,
    enabled_playbooks: frozenset[str],
    evidence_references: Sequence[str],
) -> ShadowPlaybookDecision:
    """Evaluate a nomination without importing or calling any executor."""

    if nominated_playbook is None:
        return ShadowPlaybookDecision(
            nominated_playbook=None,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("no_nomination",),
            idempotency_key=None,
            verification_query=None,
        )
    if str(incident.get("incident_type") or "") in (
        MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES
    ):
        return ShadowPlaybookDecision(
            nominated_playbook=nominated_playbook,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("management_target_diagnosis_only",),
            idempotency_key=None,
            verification_query=None,
        )
    playbook = get_runtime_agent_playbook(nominated_playbook)
    if playbook is None:
        return ShadowPlaybookDecision(
            nominated_playbook=nominated_playbook,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("unknown_playbook",),
            idempotency_key=None,
            verification_query=None,
        )

    reasons: list[str] = []
    if nominated_playbook not in enabled_playbooks:
        reasons.append("playbook_disabled")
    if str(incident.get("incident_type") or "") not in (
        playbook.permitted_incident_types
    ):
        reasons.append("incident_type_not_permitted")
    reasons.extend(
        _prerequisite_refusals(
            playbook_name=playbook.name,
            incident=incident,
            evidence_references=evidence_references,
        )
    )
    return ShadowPlaybookDecision(
        nominated_playbook=nominated_playbook,
        playbook_version=playbook.version,
        accepted=not reasons,
        refusal_reasons=tuple(reasons),
        idempotency_key=playbook.idempotency_key(
            incident_id=int(incident["id"])
        ),
        verification_query=playbook.verification_query,
    )


def evaluate_execution_playbook_nomination(
    *,
    incident: Mapping[str, Any],
    nominated_playbook: str | None,
    actions_enabled: bool,
    enabled_playbooks: frozenset[str],
    evidence_references: Sequence[str],
) -> ExecutionPlaybookDecision:
    """Evaluate Phase 6 authority without executing or importing an executor."""

    if nominated_playbook is None:
        return ExecutionPlaybookDecision(
            nominated_playbook=None,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("no_nomination",),
            idempotency_key=None,
            verification_query=None,
        )
    if str(incident.get("incident_type") or "") in (
        MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES
    ):
        return ExecutionPlaybookDecision(
            nominated_playbook=nominated_playbook,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("management_target_diagnosis_only",),
            idempotency_key=None,
            verification_query=None,
        )
    playbook = get_runtime_agent_playbook(nominated_playbook)
    if playbook is None:
        return ExecutionPlaybookDecision(
            nominated_playbook=nominated_playbook,
            playbook_version=None,
            accepted=False,
            refusal_reasons=("unknown_playbook",),
            idempotency_key=None,
            verification_query=None,
        )

    reasons: list[str] = []
    if not actions_enabled:
        reasons.append("action_authority_disabled")
    if nominated_playbook not in enabled_playbooks:
        reasons.append("playbook_disabled")
    if not playbook.executable_in_phase_6:
        reasons.append("phase_6_execution_not_permitted")
    if str(incident.get("incident_type") or "") not in (
        playbook.permitted_incident_types
    ):
        reasons.append("incident_type_not_permitted")
    reasons.extend(
        _prerequisite_refusals(
            playbook_name=playbook.name,
            incident=incident,
            evidence_references=evidence_references,
        )
    )
    return ExecutionPlaybookDecision(
        nominated_playbook=nominated_playbook,
        playbook_version=playbook.version,
        accepted=not reasons,
        refusal_reasons=tuple(reasons),
        idempotency_key=playbook.idempotency_key(
            incident_id=int(incident["id"])
        ),
        verification_query=playbook.verification_query,
        would_execute=not reasons,
    )
