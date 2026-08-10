"""Typed, fail-closed interpretation of legacy instruction result payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


VISIBILITY_DEFER_REASONS = frozenset(
    {
        "adjacent_entry_context_pending",
        "target_strategy_binding_not_visible_yet",
        "preceding_entry_context_unresolved",
    }
)


@dataclass(frozen=True, slots=True)
class InstructionOutcome:
    state: Literal[
        "deferred",
        "submitting",
        "submit_unknown",
        "verified",
        "failed",
        "expired",
    ]
    reason_code: str
    terminal_kind: str | None = None
    attempted_exchange_write: bool = False


class InstructionOutcomeContractError(ValueError):
    """A legacy result cannot be mapped to one deterministic outcome."""


def interpret_instruction_outcome(
    result: dict[str, object],
    *,
    intent_kind: str,
) -> InstructionOutcome:
    """Interpret one known legacy result without a default-success branch."""

    if not isinstance(result, dict):
        raise InstructionOutcomeContractError("instruction result must be a mapping")
    status = str(result.get("status") or "").strip().lower()
    reason = str(result.get("reason") or "").strip()
    if not status:
        raise InstructionOutcomeContractError("instruction result status is missing")
    submitted = result.get("submitted")
    if submitted not in {None, False, True}:
        raise InstructionOutcomeContractError("submitted must be boolean when present")
    leg_statuses = {
        str(leg.get("status") or "").strip().lower()
        for leg in result.get("legs", [])
        if isinstance(leg, dict)
    }
    reason_code = reason or f"legacy_{status}"

    if reason.lower() == "unknown_exchange_outcome":
        raise InstructionOutcomeContractError(
            "unknown exchange outcome contradicts deterministic status"
        )
    if "submit_unknown" in leg_statuses:
        if status in {"deferred", "skipped", "shadow_planned", "blocked"}:
            raise InstructionOutcomeContractError(
                "non-writing result contains an unknown exchange leg"
            )
        return InstructionOutcome(
            state="submit_unknown",
            reason_code=reason_code,
            attempted_exchange_write=True,
        )
    if status == "deferred":
        if reason not in VISIBILITY_DEFER_REASONS or submitted is True:
            raise InstructionOutcomeContractError(
                "deferred result requires a registered reason and no submission"
            )
        return InstructionOutcome(state="deferred", reason_code=reason)
    if status in {"reconciling", "executing", "reserved", "in_progress"}:
        if submitted is False and status in {"reconciling", "executing"}:
            raise InstructionOutcomeContractError(
                "active exchange state contradicts submitted=false"
            )
        return InstructionOutcome(
            state="submitting",
            reason_code=reason_code,
            attempted_exchange_write=(
                submitted is True or status in {"reconciling", "executing"}
            ),
        )
    if status in {"unknown", "submit_unknown", "recovery_required"}:
        return InstructionOutcome(
            state="submit_unknown",
            reason_code=reason_code,
            attempted_exchange_write=True,
        )
    if status in {"failed", "partial_failed", "blocked", "new_thread_required"}:
        if status == "blocked" and submitted is True:
            raise InstructionOutcomeContractError(
                "blocked result contradicts submitted=true"
            )
        attempted = submitted is True or status == "partial_failed" or bool(
            leg_statuses.intersection(
                {"submitted", "succeeded", "filled", "recovery_required"}
            )
        )
        return InstructionOutcome(
            state="failed",
            reason_code=reason_code,
            attempted_exchange_write=attempted,
        )
    if status == "expired":
        if submitted is True:
            raise InstructionOutcomeContractError(
                "expired result contradicts submitted=true"
            )
        return InstructionOutcome(state="expired", reason_code=reason_code)
    if status in {"skipped", "shadow_planned"}:
        if submitted is True:
            raise InstructionOutcomeContractError(
                "non-executing refusal contradicts submitted=true"
            )
        return InstructionOutcome(
            state="verified",
            reason_code=reason_code,
            terminal_kind="verified_refusal",
        )
    if status in {"submitted", "succeeded"}:
        if status == "submitted" and submitted is False:
            raise InstructionOutcomeContractError(
                "submitted status contradicts submitted=false"
            )
        attempted = status == "submitted" or submitted is True or bool(
            leg_statuses.intersection({"submitted", "succeeded", "filled"})
        )
        return InstructionOutcome(
            state="verified",
            reason_code=reason_code,
            terminal_kind=_verified_terminal_kind(result, intent_kind=intent_kind),
            attempted_exchange_write=attempted,
        )
    raise InstructionOutcomeContractError(
        f"unrecognized instruction result status: {status}"
    )


def legacy_status_for_instruction_outcome(outcome: InstructionOutcome) -> str:
    """Convert a typed outcome into the temporary instruction-item mirror."""

    if outcome.state == "deferred":
        return "pending"
    if outcome.state == "submitting":
        return "executing"
    if outcome.state == "submit_unknown":
        return "unknown"
    if outcome.state in {"failed", "expired"}:
        return "failed"
    if outcome.state == "verified":
        return "submitted" if outcome.attempted_exchange_write else "succeeded"
    raise InstructionOutcomeContractError(
        f"unsupported typed instruction outcome: {outcome.state}"
    )


def legacy_status_for_instruction_result(
    result: dict[str, object],
    *,
    intent_kind: str,
    enforcement_mode: str,
) -> str:
    """Validate every result while preserving mirrors before live enforcement."""

    outcome = interpret_instruction_outcome(result, intent_kind=intent_kind)
    if str(enforcement_mode).strip().lower() == "live":
        return legacy_status_for_instruction_outcome(outcome)
    status = str(result.get("status") or "").strip().lower()
    if outcome.state == "deferred":
        return "pending"
    if outcome.state == "submit_unknown":
        return "unknown"
    if outcome.state in {"failed", "expired"}:
        return "failed"
    if status == "submitted" or result.get("submitted") is True:
        return "submitted"
    return "succeeded"


def _verified_terminal_kind(
    result: dict[str, object],
    *,
    intent_kind: str,
) -> str:
    if str(intent_kind).strip().lower() == "entry":
        return "verified_entry"
    action = str(result.get("management_action") or "").strip().lower()
    if action in {"cancel", "cancel_entry", "cancel_pending_entry"}:
        return "verified_cancel"
    if action in {
        "full_exit",
        "exit_full",
        "full_close",
        "close_position",
    }:
        return "verified_exit"
    return "verified_management"
