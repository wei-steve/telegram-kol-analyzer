"""Pure deterministic invariant rules over closed read-only projections."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Mapping, Any

from telegram_kol_research.runtime_incident_scanner import InvariantObservation

_RULES = {
    "terminal_lifecycle_exchange_exposure_v1": ("critical", "lifecycle"),
    "active_position_missing_protection_v1": ("critical", "position"),
    "cancel_outcome_stale_unknown_v1": ("high", "cancel-operation"),
    "tp1_break_even_nonterminal_v1": ("high", "break-even"),
    "monitor_incident_ledger_silence_v1": ("low", "monitor"),
    "terminal_high_risk_management_without_instruction_v1": (
        "high",
        "management-recognition",
    ),
    "verified_replacement_role_gap_v1": ("high", "protection-revision"),
    "management_safety_gate_divergence_v1": (
        "medium",
        "management-safety-gate",
    ),
    "admitted_target_item_nonterminal_after_deadline_v1": (
        "high",
        "management-target",
    ),
    "management_target_batch_state_inconsistent_v1": (
        "high",
        "management-target",
    ),
}
_REQUIRED_BOOLEAN_FACTS = {
    "terminal_high_risk_management_without_instruction_v1": (
        "terminal_high_risk_management",
        "executable_instruction_present",
    ),
    "verified_replacement_role_gap_v1": (
        "replacement_verified",
        "primary_role_verified",
        "backup_role_verified",
    ),
    "management_safety_gate_divergence_v1": (
        "historical_only_refusal",
        "current_protection_healthy",
        "exact_scope_match",
        "fingerprint_generation_match",
        "hard_reason_present",
    ),
    "admitted_target_item_nonterminal_after_deadline_v1": (
        "target_admitted",
        "instruction_item_terminal",
        "execution_deadline_expired",
    ),
    "management_target_batch_state_inconsistent_v1": (
        "target_state_consistent_with_batch",
    ),
}


def _abnormal(rule_id: str, facts: Mapping[str, Any]) -> bool:
    if rule_id == "terminal_lifecycle_exchange_exposure_v1":
        return bool(facts.get("lifecycle_terminal")) and bool(
            facts.get("exchange_position_present") or facts.get("live_entry_order_present")
        )
    if rule_id == "active_position_missing_protection_v1":
        return bool(facts.get("position_present")) and not bool(facts.get("primary_protection_verified"))
    if rule_id == "cancel_outcome_stale_unknown_v1":
        return bool(facts.get("cancel_unknown")) and bool(facts.get("transition_window_expired"))
    if rule_id == "tp1_break_even_nonterminal_v1":
        return bool(facts.get("tp1_confirmed")) and not bool(facts.get("break_even_terminal")) and bool(facts.get("transition_window_expired"))
    if rule_id == "terminal_high_risk_management_without_instruction_v1":
        return bool(facts.get("terminal_high_risk_management")) and not bool(
            facts.get("executable_instruction_present")
        )
    if rule_id == "verified_replacement_role_gap_v1":
        return bool(facts.get("replacement_verified")) and not (
            bool(facts.get("primary_role_verified"))
            and bool(facts.get("backup_role_verified"))
        )
    if rule_id == "management_safety_gate_divergence_v1":
        return (
            bool(facts.get("historical_only_refusal"))
            and bool(facts.get("current_protection_healthy"))
            and bool(facts.get("exact_scope_match"))
            and bool(facts.get("fingerprint_generation_match"))
            and not bool(facts.get("hard_reason_present"))
        )
    if rule_id == "admitted_target_item_nonterminal_after_deadline_v1":
        return (
            bool(facts.get("target_admitted"))
            and not bool(facts.get("instruction_item_terminal"))
            and bool(facts.get("execution_deadline_expired"))
        )
    if rule_id == "management_target_batch_state_inconsistent_v1":
        return not bool(facts.get("target_state_consistent_with_batch"))
    return bool(facts.get("monitor_abnormal")) and not bool(facts.get("incident_present"))


def evaluate_rule(rule_id: str, facts: Mapping[str, Any]) -> InvariantObservation:
    if rule_id not in _RULES:
        raise ValueError("unknown scanner rule")
    severity, object_kind = _RULES[rule_id]
    object_id = str(facts.get("object_id") or "health")
    references = tuple(str(value) for value in facts.get("evidence_references", ()))
    if not references:
        references = (f"scanner-snapshot:{object_id}",)
    material = {
        key: value for key, value in facts.items()
        if key not in {"observed_at", "evidence_references"}
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    evidence_fingerprint = sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    required_boolean_facts = _REQUIRED_BOOLEAN_FACTS.get(rule_id, ())
    projection_valid = isinstance(facts.get("complete"), bool) and all(
        field in facts and isinstance(facts[field], bool)
        for field in required_boolean_facts
    )
    if facts.get("complete") is not True or not projection_valid:
        outcome = "evidence_insufficient"
    else:
        outcome = "abnormal" if _abnormal(rule_id, facts) else "normal"
    summary = {
        "complete": bool(facts.get("complete")),
        "abnormal": outcome == "abnormal",
        "transition_window_expired": bool(facts.get("transition_window_expired", False)),
    }
    if rule_id == "active_position_missing_protection_v1":
        for key in (
            "chat_id",
            "strategy_instance_id",
            "execution_binding_id",
            "execution_order_leg_id",
            "planned_stop",
            "exposure_started_at",
            "rescue_state",
        ):
            value = facts.get(key)
            if value is None or isinstance(value, (str, int, float, bool)):
                summary[key] = value
    elif rule_id == "management_safety_gate_divergence_v1":
        for key in (
            "management_batch_id",
            "execution_binding_id",
            "health_observation_id",
            "execution_order_leg_id",
            "pos_id",
            "refusal_reason_code",
            "target_fingerprint",
            "health_evidence_fingerprint",
            "health_scope_fingerprint",
            "exchange_snapshot_fingerprint",
            "refused_at",
            "health_observed_at",
        ):
            value = facts.get(key)
            if value is None or isinstance(value, (str, int, float, bool)):
                summary[key] = value
    return InvariantObservation(
        rule_id=rule_id,
        rule_version="1",
        object_kind=object_kind,
        object_id=object_id,
        severity=severity,
        outcome=outcome,
        evidence_references=references,
        evidence_fingerprint=evidence_fingerprint,
        summary=summary,
    )
