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
    if not bool(facts.get("complete")):
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
