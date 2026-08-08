"""Bounded, dormant-by-default worker for read-only incident diagnosis."""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentContractError,
    RuntimeAgentDiagnosis,
    RuntimeAgentFinalResponseError,
    RuntimeAgentToolCall,
)
from telegram_kol_research.runtime_agent_executor import (
    RuntimeAgentExecutionResult,
    RuntimeAgentExecutorConfig,
    RuntimeAgentRecoveryDeferred,
    execute_low_risk_recovery,
)
from telegram_kol_research.runtime_agent_prompt import (
    RUNTIME_AGENT_PROMPT_VERSION,
    build_runtime_agent_messages,
)
from telegram_kol_research.runtime_agent_policy import (
    ExecutionPlaybookDecision,
    ShadowPlaybookDecision,
    evaluate_execution_playbook_nomination,
    evaluate_shadow_playbook_nomination,
)
from telegram_kol_research.runtime_agent_tools import (
    RuntimeAgentToolError,
    RuntimeAgentToolRegistry,
)
from telegram_kol_research.runtime_incident_handoff import (
    build_runtime_incident_handoff,
    build_runtime_incident_failure_handoff,
)
from telegram_kol_research.runtime_incident_snapshot import (
    MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES,
    resolve_management_target_incident_snapshot,
)
from telegram_kol_research.runtime_incidents import (
    claim_runtime_incident,
    defer_runtime_incident_action_claim,
    find_reusable_runtime_incident_diagnosis,
    get_message_operation_incident_snapshot,
    list_claimable_runtime_incidents,
    transition_runtime_incident,
)


@dataclass(frozen=True, slots=True)
class RuntimeAgentWorkerConfig:
    enabled: bool = False
    incident_types: frozenset[str] | None = None
    message_operation_enabled: bool = False
    message_operation_after_contract_id: int = 2**63 - 1
    deployed_code_version: str = "unknown"
    max_tool_steps: int = 4
    max_wall_seconds: float = 45.0
    max_prompt_bytes: int = 16_384
    max_model_output_bytes: int = 8192
    claim_lease_seconds: float = 120.0
    model_timeout_seconds: float = 30.0
    max_agent_attempts: int = 3
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    shadow_playbooks: frozenset[str] = frozenset()
    actions_enabled: bool = False
    action_playbooks: frozenset[str] = frozenset()
    action_circuit_threshold: int = 3
    action_reservation_lease_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class RuntimeAgentWorkerResult:
    status: str
    incident_id: int | None = None
    tool_steps: int = 0
    refused_tool_calls: int = 0
    handoff: dict[str, Any] | None = None
    shadow_policy: dict[str, Any] | None = None
    recovery_policy: dict[str, Any] | None = None


def run_runtime_agent_loop(
    *,
    run_once: Callable[[], RuntimeAgentWorkerResult],
    on_result: Callable[[RuntimeAgentWorkerResult], None] | None = None,
    poll_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> int:
    """Run the sidecar loop, draining work before bounded idle polling."""

    interval = max(0.25, min(float(poll_seconds), 60.0))
    iterations = 0
    while max_iterations is None or iterations < max(0, int(max_iterations)):
        result = run_once()
        iterations += 1
        if on_result is not None:
            on_result(result)
        if result.status == "disabled":
            break
        if result.status in {
            "idle",
            "retry_pending",
            "escalated",
            "claim_lost",
            "action_deferred",
        }:
            sleep(interval)
    return iterations


def _incident_mapping(incident) -> dict[str, Any]:
    try:
        summary = json.loads(incident.redacted_summary)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeAgentContractError("incident summary is invalid") from exc
    if not isinstance(summary, dict):
        raise RuntimeAgentContractError("incident summary is invalid")
    return {
        "id": int(incident.id),
        "incident_type": incident.incident_type,
        "severity": incident.severity,
        "source_kind": incident.source_kind,
        "source_record_id": incident.source_record_id,
        "generation": int(incident.generation),
        "repeat_count": int(incident.repeat_count),
        "redacted_summary": summary,
    }


def _handoff_incident(incident) -> dict[str, Any]:
    incident_mapping = _incident_mapping(incident)
    return {
        key: incident_mapping[key]
        for key in (
            "id",
            "incident_type",
            "source_kind",
            "source_record_id",
            "redacted_summary",
        )
    }


def _handoff_incident_from_mapping(incident: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: incident[key]
        for key in (
            "id",
            "incident_type",
            "source_kind",
            "source_record_id",
            "redacted_summary",
        )
    }


def _diagnosis_only_for_target(
    incident_type: str,
    diagnosis: RuntimeAgentDiagnosis,
) -> RuntimeAgentDiagnosis:
    if (
        incident_type not in MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES
        and incident_type != "message_operation_failure"
    ):
        return diagnosis
    return replace(
        diagnosis,
        recommended_playbook_name=None,
        auto_handle_eligible=False,
        codex_handoff_required=True,
    )


def _agent_incident_context(session_factory, incident):
    mapping = _incident_mapping(incident)
    references = {f"incident:{incident.id}"}
    if incident.incident_type == "message_operation_failure":
        snapshot = get_message_operation_incident_snapshot(
            session_factory, incident_id=incident.id
        )
        if snapshot.get("truncated") is True:
            raise RuntimeAgentContractError(
                "affected message evidence exceeds the bounded diagnosis contract"
            )
        mapping["redacted_summary"] = {
            **mapping["redacted_summary"],
            "message_operation_snapshot": snapshot,
        }
        for raw_id, contract in zip(
            snapshot["affected_message_ids"], snapshot["contracts"], strict=True
        ):
            references.update(
                {
                    f"raw_message:{raw_id}",
                    f"message_operation_contract:{contract['contract_id']}",
                }
            )
        return mapping, references
    if incident.incident_type not in MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES:
        return mapping, references
    snapshot = resolve_management_target_incident_snapshot(
        session_factory,
        incident_id=incident.id,
    )
    mapping["redacted_summary"] = {
        **mapping["redacted_summary"],
        "management_target_snapshot": snapshot["data"],
    }
    references.update(snapshot["evidence_refs"])
    return mapping, references


def _message_operation_reuse_context(
    *, incident, agent_incident: Mapping[str, Any], deployed_code_version: str
) -> dict[str, str] | None:
    if (
        incident.incident_type != "message_operation_failure"
        or deployed_code_version == "unknown"
    ):
        return None
    summary = agent_incident.get("redacted_summary")
    snapshot = summary.get("message_operation_snapshot") if isinstance(summary, dict) else None
    contracts = snapshot.get("contracts") if isinstance(snapshot, dict) else None
    semantic_contracts = []
    for contract in contracts if isinstance(contracts, list) else []:
        if isinstance(contract, dict):
            material_refs = [
                reference
                for reference in contract.get("evidence_refs", [])
                if isinstance(reference, str)
                and not reference.startswith(
                    ("raw_message:", "message_operation_contract:")
                )
            ]
            semantic_items = []
            for item in contract.get("items", []):
                if not isinstance(item, dict):
                    continue
                semantic_items.append(
                    {
                        **{
                            key: item.get(key)
                            for key in (
                                "instruction_kind",
                                "expected_descendant_kind",
                                "expected_terminal_kind",
                                "observed_terminal_kind",
                                "status",
                            )
                        },
                        "evidence_refs": sorted(
                            reference
                            for reference in item.get("evidence_refs", [])
                            if isinstance(reference, str)
                            and not reference.startswith(
                                ("raw_message:", "message_operation_contract:")
                            )
                        ),
                    }
                )
            semantic_contracts.append(
                {
                    **{
                        key: contract.get(key)
                        for key in (
                            "intent_kind",
                            "expected_terminal_kind",
                            "observed_status",
                            "violation_code",
                        )
                    },
                    "evidence_refs": sorted(material_refs),
                    "items": semantic_items,
                }
            )
    semantic_evidence_classes = sorted(
        {
            json.dumps(
                contract,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            for contract in semantic_contracts
        }
    )
    evidence_fingerprint = hashlib.sha256(
        json.dumps(
            semantic_evidence_classes,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "deployed_code_version": deployed_code_version,
        "evidence_fingerprint": evidence_fingerprint,
        "feature_policy_version": incident.feature_policy_version,
        "severity": incident.severity,
        "violation_class": incident.source_record_id,
    }


def _assert_affected_snapshot_current(
    session_factory,
    *,
    incident,
    affected_message_ids: tuple[int, ...],
) -> None:
    if incident.incident_type != "message_operation_failure":
        return
    latest = get_message_operation_incident_snapshot(
        session_factory, incident_id=incident.id
    )
    if latest.get("truncated") is True or tuple(
        latest.get("affected_message_ids", ())
    ) != affected_message_ids:
        raise RuntimeAgentContractError(
            "affected message evidence changed during diagnosis"
        )


def _parse_model_turn(raw: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise RuntimeAgentContractError("model turn must be an object")
    if set(raw) == {"tool_call"} and isinstance(raw["tool_call"], Mapping):
        return "tool_call", raw["tool_call"]
    if set(raw) == {"final"}:
        if not isinstance(raw["final"], Mapping):
            raise RuntimeAgentFinalResponseError(
                "model final response is invalid"
            )
        return "final", raw["final"]
    raise RuntimeAgentContractError("model turn has an unknown shape")


def _bounded_model_turn(raw: Any, *, maximum: int) -> None:
    try:
        size = len(
            json.dumps(
                raw,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeAgentContractError("model turn is not JSON") from exc
    if size > maximum:
        raise RuntimeAgentContractError("model turn exceeds byte budget")


def _validate_message_budget(messages: list[dict[str, Any]], *, maximum: int) -> None:
    encoded = json.dumps(
        messages,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise RuntimeAgentContractError(
            "runtime agent transcript exceeds prompt byte budget"
        )


def _remaining_wall_budget(
    *,
    started: float,
    maximum: float,
    monotonic: Callable[[], float],
) -> float:
    remaining = float(maximum) - (monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("runtime agent wall-clock budget exhausted")
    return remaining


def _final_correction_message(
    *,
    incident_id: int,
    gathered_references: set[str],
) -> tuple[str, frozenset[str]]:
    incident_reference = f"incident:{int(incident_id)}"
    ordered = [incident_reference]
    ordered.extend(
        reference
        for reference in sorted(gathered_references)
        if reference != incident_reference
    )
    allowed = frozenset(ordered[:8])
    return (
        (
            "The previous final JSON failed the closed local contract. This "
            "is the one correction allowed for this agent attempt. Return "
            "only a corrected final JSON object; do not call a tool. Use "
            "evidence_references only from this exact allowlist: "
            + json.dumps(
                sorted(allowed),
                ensure_ascii=True,
                separators=(",", ":"),
            )
            + ". Keep diagnosis_hypothesis and remaining_risk at most 512 "
            "characters each, missing_evidence at most 16 items, and "
            "evidence_references at most 32 items."
        ),
        allowed,
    )


def _append_tool_exchange(
    messages: list[dict[str, Any]],
    *,
    call: RuntimeAgentToolCall,
    content: dict[str, Any],
) -> None:
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.call_id,
            "content": json.dumps(
                content,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )


def _diagnosis_from_reusable(
    current_id: int,
    reusable,
    *,
    affected_message_ids: tuple[int, ...] = (),
) -> RuntimeAgentDiagnosis:
    try:
        stored = json.loads(reusable.diagnosis_json)
        stored_references = json.loads(reusable.evidence_refs_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeAgentContractError("reusable diagnosis is invalid") from exc
    if not isinstance(stored_references, list):
        raise RuntimeAgentContractError("reusable diagnosis is invalid")
    current_reference = f"incident:{current_id}"
    references = [
        reference
        for reference in stored_references
        if reference != current_reference
    ][:31]
    references.append(current_reference)
    merged_message_ids = list(
        dict.fromkeys(
            [
                *stored.get("affected_message_ids", []),
                *affected_message_ids,
            ]
        )
    )[:32]
    return RuntimeAgentDiagnosis.from_mapping(
        {
            "incident_id": current_id,
            "diagnosis_hypothesis": stored["hypothesis"],
            "confidence": stored["confidence"],
            "evidence_references": references,
            "missing_evidence": stored.get("missing_evidence", []),
            "recommended_playbook_name": stored.get("recommended_playbook"),
            "auto_handle_eligible": stored.get("auto_handle_eligible", False),
            "codex_handoff_required": stored.get(
                "codex_handoff_required", True
            ),
            "remaining_risk": stored["remaining_risk"],
            "expected_state": stored.get(
                "expected_state", "A durable terminal outcome was expected."
            ),
            "observed_state": stored.get(
                "observed_state", "The historical diagnosis lacks structured state."
            ),
            "classification": stored.get(
                "classification", "insufficient_evidence"
            ),
            "affected_message_ids": merged_message_ids,
            "likely_code_paths": stored.get("likely_code_paths", []),
            "likely_test_paths": stored.get("likely_test_paths", []),
        },
        expected_incident_id=current_id,
    ).with_attempted_queries(tuple(stored.get("attempted_queries", ())))


def _commit_diagnosis(
    session_factory,
    *,
    incident,
    claim_token: str,
    diagnosis: RuntimeAgentDiagnosis,
    now: datetime,
    shadow_decision: ShadowPlaybookDecision,
    recovery_policy: Mapping[str, Any] | None = None,
    recovery_status: str | None = None,
    prompt_version: str | None = None,
    reuse_context: Mapping[str, Any] | None = None,
    handoff: Mapping[str, Any] | None = None,
    outcome_kind: str = "diagnosed",
) -> bool:
    ledger_diagnosis = diagnosis.to_ledger_mapping()
    ledger_diagnosis["shadow_playbook_policy"] = (
        shadow_decision.to_ledger_mapping()
    )
    if recovery_policy is not None:
        ledger_diagnosis["recovery_playbook_policy"] = dict(recovery_policy)
    if reuse_context is not None:
        ledger_diagnosis["reuse_context"] = dict(reuse_context)
    diagnosis_json = json.dumps(
        ledger_diagnosis,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_json = json.dumps(
        list(diagnosis.evidence_references),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="diagnosed",
        claim_token=claim_token,
        now=now,
        diagnosis_json=diagnosis_json,
        evidence_refs_json=evidence_json,
        playbook_name=diagnosis.recommended_playbook_name,
        recovery_status=recovery_status or shadow_decision.recovery_status,
        queue_notification=True,
        prompt_version=prompt_version,
        handoff=dict(handoff) if handoff is not None else None,
        handoff_outcome=outcome_kind,
    )


def _evaluate_recovery(
    session_factory,
    *,
    incident,
    claim_token: str,
    diagnosis: RuntimeAgentDiagnosis,
    config: RuntimeAgentWorkerConfig,
    tools: RuntimeAgentToolRegistry,
    action_handlers: Mapping[str, Callable[..., bool]] | None,
    now: datetime,
    clock: Callable[[], datetime] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not config.actions_enabled:
        return None, None
    decision: ExecutionPlaybookDecision = (
        evaluate_execution_playbook_nomination(
            incident=_incident_mapping(incident),
            nominated_playbook=diagnosis.recommended_playbook_name,
            actions_enabled=(
                config.actions_enabled and diagnosis.auto_handle_eligible
            ),
            enabled_playbooks=config.action_playbooks,
            evidence_references=diagnosis.evidence_references,
        )
    )
    result: RuntimeAgentExecutionResult = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=decision,
        config=RuntimeAgentExecutorConfig(
            enabled=config.actions_enabled,
            circuit_breaker_threshold=config.action_circuit_threshold,
            reservation_lease_seconds=(
                config.action_reservation_lease_seconds
            ),
        ),
        tools=tools,
        action_handlers=action_handlers,
        now=now,
        clock=clock,
    )
    if result.status in {"action_in_progress", "circuit_busy"}:
        raise RuntimeAgentRecoveryDeferred(result.status)
    mapping = result.to_ledger_mapping(decision=decision)
    if result.status in {"verified", "already_verified"}:
        recovery_status = "action_verified"
    elif result.status in {
        "verification_failed",
        "failed",
        "action_outcome_unknown",
        "circuit_open",
        "incident_action_frozen",
    }:
        recovery_status = "action_frozen"
    else:
        recovery_status = "action_refused"
    return mapping, recovery_status


def _transition_retry(
    session_factory,
    *,
    incident,
    claim_token,
    now,
    config: RuntimeAgentWorkerConfig,
    failure_outcome: str,
    attempted_queries: tuple[str, ...],
    agent_incident: Mapping[str, Any] | None,
) -> str:
    max_attempts = max(1, min(int(config.max_agent_attempts), 10))
    if int(incident.agent_attempt_count) >= max_attempts:
        failure_handoff = build_runtime_incident_failure_handoff(
            incident=(agent_incident or _handoff_incident(incident)),
            outcome_kind=failure_outcome,
            attempted_queries=attempted_queries,
        )
        transition_runtime_incident(
            session_factory,
            incident_id=incident.id,
            from_status="claimed",
            to_status="escalated",
            claim_token=claim_token,
            now=now,
            queue_notification=True,
            handoff=failure_handoff,
            handoff_outcome=failure_outcome,
        )
        return "escalated"
    delay = min(
        max(1.0, float(config.retry_max_seconds)),
        max(1.0, float(config.retry_base_seconds))
        * (2 ** max(0, int(incident.agent_attempt_count) - 1)),
    )
    transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="retry_pending",
        claim_token=claim_token,
        now=now,
        agent_next_attempt_at=now + timedelta(seconds=delay),
    )
    return "retry_pending"


def _transition_terminal_failure(
    session_factory,
    *,
    incident,
    claim_token: str,
    now: datetime,
    outcome_kind: str,
    attempted_queries: tuple[str, ...] = (),
    agent_incident: Mapping[str, Any] | None = None,
) -> bool:
    failure_handoff = build_runtime_incident_failure_handoff(
        incident=(agent_incident or _handoff_incident(incident)),
        outcome_kind=outcome_kind,
        attempted_queries=attempted_queries,
    )
    return transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="escalated",
        claim_token=claim_token,
        now=now,
        queue_notification=True,
        handoff=failure_handoff,
        handoff_outcome=outcome_kind,
    )


def run_runtime_agent_once(
    session_factory,
    *,
    config: RuntimeAgentWorkerConfig,
    tools: RuntimeAgentToolRegistry,
    model_turn: Callable[..., Mapping[str, Any]],
    action_handlers: Mapping[str, Callable[..., bool]] | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RuntimeAgentWorkerResult:
    """Claim and diagnose one incident, with dormant closed recovery authority."""

    if not config.enabled:
        return RuntimeAgentWorkerResult(status="disabled")
    operation_now = now or datetime.now(UTC)
    claimable = list_claimable_runtime_incidents(
        session_factory,
        now=operation_now,
        limit=1,
        incident_types=config.incident_types,
        message_operation_enabled=config.message_operation_enabled,
        message_operation_after_contract_id=(
            config.message_operation_after_contract_id
        ),
    )
    if not claimable:
        return RuntimeAgentWorkerResult(status="idle")
    candidate = claimable[0]
    claim_token = uuid.uuid4().hex
    claimed = claim_runtime_incident(
        session_factory,
        incident_id=candidate.id,
        claim_token=claim_token,
        claimed_at=operation_now,
        claim_expires_at=operation_now
        + timedelta(seconds=max(5.0, min(config.claim_lease_seconds, 3600.0))),
        prompt_version=RUNTIME_AGENT_PROMPT_VERSION,
        incident_types=config.incident_types,
        message_operation_enabled=config.message_operation_enabled,
        message_operation_after_contract_id=(
            config.message_operation_after_contract_id
        ),
    )
    if claimed is None:
        return RuntimeAgentWorkerResult(status="claim_lost", incident_id=candidate.id)
    max_agent_attempts = max(1, min(int(config.max_agent_attempts), 10))
    if int(claimed.agent_attempt_count) > max_agent_attempts:
        _transition_terminal_failure(
            session_factory,
            incident=claimed,
            claim_token=claim_token,
            now=operation_now,
            outcome_kind="evidence_incomplete",
        )
        return RuntimeAgentWorkerResult(
            status="escalated",
            incident_id=claimed.id,
        )

    started = monotonic()
    attempted_queries: list[str] = []
    gathered_references = {f"incident:{claimed.id}"}
    refused = 0
    agent_incident: Mapping[str, Any] | None = None
    tool_failure_seen = False
    try:
        agent_incident, gathered_references = _agent_incident_context(
            session_factory, claimed
        )
        reuse_context = _message_operation_reuse_context(
            incident=claimed,
            agent_incident=agent_incident,
            deployed_code_version=config.deployed_code_version,
        )
        snapshot = agent_incident.get("redacted_summary", {}).get(
            "message_operation_snapshot", {}
        )
        affected_message_ids = tuple(
            snapshot.get("affected_message_ids", ())
            if isinstance(snapshot, dict)
            else ()
        )
        reusable = find_reusable_runtime_incident_diagnosis(
            session_factory,
            fingerprint=claimed.fingerprint,
            exclude_incident_id=claimed.id,
            feature_policy_version=(
                reuse_context["feature_policy_version"]
                if reuse_context is not None
                else None
            ),
            deployed_code_version=(
                reuse_context["deployed_code_version"]
                if reuse_context is not None
                else None
            ),
            evidence_fingerprint=(
                reuse_context["evidence_fingerprint"]
                if reuse_context is not None
                else None
            ),
            violation_class=(
                reuse_context["violation_class"]
                if reuse_context is not None
                else None
            ),
            severity=claimed.severity if reuse_context is not None else None,
        )
        if reusable is not None:
            diagnosis = _diagnosis_only_for_target(
                claimed.incident_type,
                _diagnosis_from_reusable(
                    claimed.id,
                    reusable,
                    affected_message_ids=affected_message_ids,
                ),
            )
            shadow_decision = evaluate_shadow_playbook_nomination(
                incident=agent_incident,
                nominated_playbook=diagnosis.recommended_playbook_name,
                enabled_playbooks=config.shadow_playbooks,
                evidence_references=diagnosis.evidence_references,
            )
            completion_now = (
                operation_now if now is not None else datetime.now(UTC)
            )
            _remaining_wall_budget(
                started=started,
                maximum=config.max_wall_seconds,
                monotonic=monotonic,
            )
            recovery_policy, recovery_status = _evaluate_recovery(
                session_factory,
                incident=claimed,
                claim_token=claim_token,
                diagnosis=diagnosis,
                config=config,
                tools=tools,
                action_handlers=action_handlers,
                now=completion_now,
                clock=(
                    (lambda: operation_now)
                    if now is not None
                    else (lambda: datetime.now(UTC))
                ),
            )
            handoff = build_runtime_incident_handoff(
                incident=_handoff_incident_from_mapping(agent_incident),
                diagnosis=diagnosis,
                attempted_queries=diagnosis.attempted_queries,
                shadow_policy=shadow_decision.to_ledger_mapping(),
                recovery_policy=recovery_policy,
            )
            _assert_affected_snapshot_current(
                session_factory,
                incident=claimed,
                affected_message_ids=affected_message_ids,
            )
            if not _commit_diagnosis(
                session_factory,
                incident=claimed,
                claim_token=claim_token,
                diagnosis=diagnosis,
                now=completion_now,
                shadow_decision=shadow_decision,
                recovery_policy=recovery_policy,
                recovery_status=recovery_status,
                prompt_version=reusable.prompt_version,
                reuse_context=reuse_context,
                handoff=handoff,
                outcome_kind="reused",
            ):
                return RuntimeAgentWorkerResult(
                    status="claim_lost", incident_id=claimed.id
                )
            return RuntimeAgentWorkerResult(
                status="reused",
                incident_id=claimed.id,
                handoff=handoff,
                shadow_policy=shadow_decision.to_ledger_mapping(),
                recovery_policy=recovery_policy,
            )

        messages = build_runtime_agent_messages(
            agent_incident,
            max_prompt_bytes=config.max_prompt_bytes,
        )
        allowed_tools = (
            frozenset(
                name
                for name in tools.allowed_tools
                if name.startswith("investigate_")
            )
            if claimed.incident_type == "message_operation_failure"
            else tools.allowed_tools
        )
        seen_calls: set[str] = set()
        # Reserve a model turn for the final closed diagnosis even when the
        # configured hard tool budget is four. Once this evidence budget is
        # exhausted, tools are no longer advertised to the provider.
        evidence_tool_limit = max(1, min(config.max_tool_steps, 3))
        final_instruction_added = False
        final_correction_used = False
        correction_allowed_references: frozenset[str] | None = None
        max_turns = max(2, min(config.max_tool_steps, 4) + 4)

        def request_final_correction() -> None:
            nonlocal final_correction_used
            nonlocal correction_allowed_references
            nonlocal final_instruction_added
            if final_correction_used:
                raise RuntimeAgentContractError(
                    "corrected model final is invalid"
                )
            correction, correction_allowed_references = (
                _final_correction_message(
                    incident_id=claimed.id,
                    gathered_references=gathered_references,
                )
            )
            messages.append({"role": "user", "content": correction})
            final_correction_used = True
            final_instruction_added = True
            _validate_message_budget(
                messages,
                maximum=config.max_prompt_bytes,
            )

        for turn_index in range(max_turns + 1):
            if turn_index == max_turns and not final_correction_used:
                break
            _remaining_wall_budget(
                started=started,
                maximum=config.max_wall_seconds,
                monotonic=monotonic,
            )
            _validate_message_budget(
                messages,
                maximum=config.max_prompt_bytes,
            )
            if (
                len(attempted_queries) >= evidence_tool_limit
                and not final_instruction_added
            ):
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Evidence collection is complete. Do not request "
                            "another evidence tool. Return only the final JSON "
                            "object matching the closed diagnosis contract."
                        ),
                    }
                )
                final_instruction_added = True
                _validate_message_budget(
                    messages,
                    maximum=config.max_prompt_bytes,
                )
            remaining_wall = _remaining_wall_budget(
                started=started,
                maximum=config.max_wall_seconds,
                monotonic=monotonic,
            )
            try:
                raw_turn = model_turn(
                    messages=messages,
                    tool_schemas=(
                        tools.tool_schemas(allowed_tools=allowed_tools)
                        if (
                            len(attempted_queries) < evidence_tool_limit
                            and not final_correction_used
                        )
                        else []
                    ),
                    timeout_seconds=min(
                        config.model_timeout_seconds,
                        remaining_wall,
                    ),
                )
            except RuntimeAgentFinalResponseError:
                request_final_correction()
                continue
            _remaining_wall_budget(
                started=started,
                maximum=config.max_wall_seconds,
                monotonic=monotonic,
            )
            _bounded_model_turn(raw_turn, maximum=config.max_model_output_bytes)
            try:
                turn_kind, payload = _parse_model_turn(raw_turn)
            except RuntimeAgentFinalResponseError:
                request_final_correction()
                continue
            if turn_kind == "final":
                try:
                    diagnosis = RuntimeAgentDiagnosis.from_mapping(
                        payload, expected_incident_id=claimed.id
                    ).with_attempted_queries(tuple(attempted_queries))
                    diagnosis = _diagnosis_only_for_target(
                        claimed.incident_type, diagnosis
                    )
                    if (
                        claimed.incident_type == "message_operation_failure"
                        and tuple(sorted(diagnosis.affected_message_ids))
                        != tuple(sorted(affected_message_ids))
                    ):
                        raise RuntimeAgentContractError(
                            "affected message identities do not match evidence"
                        )
                    allowed_references = (
                        correction_allowed_references
                        if correction_allowed_references is not None
                        else gathered_references
                    )
                    if not set(diagnosis.evidence_references) <= set(
                        allowed_references
                    ):
                        raise RuntimeAgentContractError(
                            "diagnosis cites evidence that was not returned"
                        )
                except RuntimeAgentContractError:
                    request_final_correction()
                    continue
                shadow_decision = evaluate_shadow_playbook_nomination(
                    incident=agent_incident,
                    nominated_playbook=diagnosis.recommended_playbook_name,
                    enabled_playbooks=config.shadow_playbooks,
                    evidence_references=diagnosis.evidence_references,
                )
                completion_now = (
                    operation_now if now is not None else datetime.now(UTC)
                )
                _remaining_wall_budget(
                    started=started,
                    maximum=config.max_wall_seconds,
                    monotonic=monotonic,
                )
                recovery_policy, recovery_status = _evaluate_recovery(
                    session_factory,
                    incident=claimed,
                    claim_token=claim_token,
                    diagnosis=diagnosis,
                    config=config,
                    tools=tools,
                    action_handlers=action_handlers,
                    now=completion_now,
                    clock=(
                        (lambda: operation_now)
                        if now is not None
                        else (lambda: datetime.now(UTC))
                    ),
                )
                handoff = build_runtime_incident_handoff(
                    incident=_handoff_incident_from_mapping(agent_incident),
                    diagnosis=diagnosis,
                    attempted_queries=attempted_queries,
                    shadow_policy=shadow_decision.to_ledger_mapping(),
                    recovery_policy=recovery_policy,
                )
                _assert_affected_snapshot_current(
                    session_factory,
                    incident=claimed,
                    affected_message_ids=affected_message_ids,
                )
                if not _commit_diagnosis(
                    session_factory,
                    incident=claimed,
                    claim_token=claim_token,
                    diagnosis=diagnosis,
                    now=completion_now,
                    shadow_decision=shadow_decision,
                    recovery_policy=recovery_policy,
                    recovery_status=recovery_status,
                    reuse_context=reuse_context,
                    handoff=handoff,
                    outcome_kind="diagnosed",
                ):
                    return RuntimeAgentWorkerResult(
                        status="claim_lost",
                        incident_id=claimed.id,
                        tool_steps=len(attempted_queries),
                        refused_tool_calls=refused,
                    )
                return RuntimeAgentWorkerResult(
                    status="diagnosed",
                    incident_id=claimed.id,
                    tool_steps=len(attempted_queries),
                    refused_tool_calls=refused,
                    handoff=handoff,
                    shadow_policy=shadow_decision.to_ledger_mapping(),
                    recovery_policy=recovery_policy,
                )

            if final_correction_used:
                raise RuntimeAgentContractError(
                    "corrected model turn must be final"
                )
            call = RuntimeAgentToolCall.from_mapping(
                payload,
                allowed_tools=allowed_tools,
                expected_incident_id=claimed.id,
            )
            signature = json.dumps(
                [call.name, call.arguments],
                sort_keys=True,
                separators=(",", ":"),
            )
            if signature in seen_calls:
                refused += 1
                _append_tool_exchange(
                    messages,
                    call=call,
                    content={
                        "error": "repeated_tool_call_refused",
                        "name": call.name,
                    },
                )
                continue
            if len(attempted_queries) >= evidence_tool_limit:
                _transition_terminal_failure(
                    session_factory,
                    incident=claimed,
                    claim_token=claim_token,
                    now=operation_now,
                    outcome_kind="evidence_incomplete",
                    attempted_queries=tuple(attempted_queries),
                    agent_incident=agent_incident,
                )
                return RuntimeAgentWorkerResult(
                    status="escalated",
                    incident_id=claimed.id,
                    tool_steps=len(attempted_queries),
                    refused_tool_calls=refused,
                )
            seen_calls.add(signature)
            try:
                result = tools.execute(
                    call.name,
                    call.arguments,
                    expected_incident_id=claimed.id,
                )
            except RuntimeAgentToolError as exc:
                tool_failure_seen = True
                refused += 1
                _append_tool_exchange(
                    messages,
                    call=call,
                    content={
                        "error": "tool_refused",
                        "reason": str(exc)[:160],
                    },
                )
                continue
            attempted_queries.append(call.name)
            gathered_references.update(result.evidence_refs)
            _append_tool_exchange(
                messages,
                call=call,
                content=result.as_model_payload(),
            )
        _transition_terminal_failure(
            session_factory,
            incident=claimed,
            claim_token=claim_token,
            now=operation_now,
            outcome_kind=("tool_failed" if tool_failure_seen else "evidence_incomplete"),
            attempted_queries=tuple(attempted_queries),
            agent_incident=agent_incident,
        )
        return RuntimeAgentWorkerResult(
            status="escalated",
            incident_id=claimed.id,
            tool_steps=len(attempted_queries),
            refused_tool_calls=refused,
        )
    except RuntimeAgentRecoveryDeferred:
        retry_delay = max(
            5.0,
            min(float(config.action_reservation_lease_seconds), 3600.0),
        )
        deferred = defer_runtime_incident_action_claim(
            session_factory,
            incident_id=claimed.id,
            claim_token=claim_token,
            now=operation_now,
            retry_at=operation_now + timedelta(seconds=retry_delay),
        )
        return RuntimeAgentWorkerResult(
            status="action_deferred" if deferred else "claim_lost",
            incident_id=claimed.id,
            tool_steps=len(attempted_queries),
            refused_tool_calls=refused,
        )
    except Exception as exc:
        failure_outcome = (
            "timed_out"
            if isinstance(exc, TimeoutError)
            and "wall-clock budget exhausted" in str(exc)
            else "tool_failed"
            if tool_failure_seen or isinstance(exc, RuntimeAgentToolError)
            else "evidence_incomplete"
            if isinstance(exc, (RuntimeAgentContractError, RuntimeAgentFinalResponseError))
            else "provider_failed"
        )
        failure_status = _transition_retry(
            session_factory,
            incident=claimed,
            claim_token=claim_token,
            now=operation_now,
            config=config,
            failure_outcome=failure_outcome,
            attempted_queries=tuple(attempted_queries),
            agent_incident=agent_incident,
        )
        return RuntimeAgentWorkerResult(
            status=failure_status,
            incident_id=claimed.id,
            tool_steps=len(attempted_queries),
            refused_tool_calls=refused,
        )
