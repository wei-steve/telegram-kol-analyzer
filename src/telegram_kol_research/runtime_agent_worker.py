"""Bounded, dormant-by-default worker for read-only incident diagnosis."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentContractError,
    RuntimeAgentDiagnosis,
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
)
from telegram_kol_research.runtime_incidents import (
    claim_runtime_incident,
    defer_runtime_incident_action_claim,
    find_reusable_runtime_incident_diagnosis,
    list_claimable_runtime_incidents,
    transition_runtime_incident,
)


@dataclass(frozen=True, slots=True)
class RuntimeAgentWorkerConfig:
    enabled: bool = False
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


def _parse_model_turn(raw: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise RuntimeAgentContractError("model turn must be an object")
    if set(raw) == {"tool_call"} and isinstance(raw["tool_call"], Mapping):
        return "tool_call", raw["tool_call"]
    if set(raw) == {"final"} and isinstance(raw["final"], Mapping):
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


def _diagnosis_from_reusable(current_id: int, reusable) -> RuntimeAgentDiagnosis:
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
) -> bool:
    ledger_diagnosis = diagnosis.to_ledger_mapping()
    ledger_diagnosis["shadow_playbook_policy"] = (
        shadow_decision.to_ledger_mapping()
    )
    if recovery_policy is not None:
        ledger_diagnosis["recovery_playbook_policy"] = dict(recovery_policy)
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
) -> str:
    max_attempts = max(1, min(int(config.max_agent_attempts), 10))
    if int(incident.agent_attempt_count) >= max_attempts:
        transition_runtime_incident(
            session_factory,
            incident_id=incident.id,
            from_status="claimed",
            to_status="escalated",
            claim_token=claim_token,
            now=now,
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
        session_factory, now=operation_now, limit=1
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
    )
    if claimed is None:
        return RuntimeAgentWorkerResult(status="claim_lost", incident_id=candidate.id)
    max_agent_attempts = max(1, min(int(config.max_agent_attempts), 10))
    if int(claimed.agent_attempt_count) > max_agent_attempts:
        transition_runtime_incident(
            session_factory,
            incident_id=claimed.id,
            from_status="claimed",
            to_status="escalated",
            claim_token=claim_token,
            now=operation_now,
        )
        return RuntimeAgentWorkerResult(
            status="escalated",
            incident_id=claimed.id,
        )

    started = monotonic()
    attempted_queries: list[str] = []
    gathered_references = {f"incident:{claimed.id}"}
    refused = 0
    try:
        reusable = find_reusable_runtime_incident_diagnosis(
            session_factory,
            fingerprint=claimed.fingerprint,
            exclude_incident_id=claimed.id,
        )
        if reusable is not None:
            diagnosis = _diagnosis_from_reusable(claimed.id, reusable)
            shadow_decision = evaluate_shadow_playbook_nomination(
                incident=_incident_mapping(claimed),
                nominated_playbook=diagnosis.recommended_playbook_name,
                enabled_playbooks=config.shadow_playbooks,
                evidence_references=diagnosis.evidence_references,
            )
            completion_now = (
                operation_now if now is not None else datetime.now(UTC)
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
                incident=_handoff_incident(claimed),
                diagnosis=diagnosis,
                attempted_queries=diagnosis.attempted_queries,
                shadow_policy=shadow_decision.to_ledger_mapping(),
                recovery_policy=recovery_policy,
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
            _incident_mapping(claimed),
            max_prompt_bytes=config.max_prompt_bytes,
        )
        seen_calls: set[str] = set()
        # Reserve a model turn for the final closed diagnosis even when the
        # configured hard tool budget is four. Once this evidence budget is
        # exhausted, tools are no longer advertised to the provider.
        evidence_tool_limit = max(1, min(config.max_tool_steps, 3))
        final_instruction_added = False
        max_turns = max(2, min(config.max_tool_steps, 4) + 4)
        for _ in range(max_turns):
            if monotonic() - started > config.max_wall_seconds:
                raise TimeoutError("runtime agent wall-clock budget exhausted")
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
            raw_turn = model_turn(
                messages=messages,
                tool_schemas=(
                    tools.tool_schemas()
                    if len(attempted_queries) < evidence_tool_limit
                    else []
                ),
                timeout_seconds=min(
                    config.model_timeout_seconds,
                    max(1.0, config.max_wall_seconds - (monotonic() - started)),
                ),
            )
            _bounded_model_turn(raw_turn, maximum=config.max_model_output_bytes)
            turn_kind, payload = _parse_model_turn(raw_turn)
            if turn_kind == "final":
                diagnosis = RuntimeAgentDiagnosis.from_mapping(
                    payload, expected_incident_id=claimed.id
                ).with_attempted_queries(tuple(attempted_queries))
                if not set(diagnosis.evidence_references) <= gathered_references:
                    raise RuntimeAgentContractError(
                        "diagnosis cites evidence that was not returned"
                    )
                shadow_decision = evaluate_shadow_playbook_nomination(
                    incident=_incident_mapping(claimed),
                    nominated_playbook=diagnosis.recommended_playbook_name,
                    enabled_playbooks=config.shadow_playbooks,
                    evidence_references=diagnosis.evidence_references,
                )
                completion_now = (
                    operation_now if now is not None else datetime.now(UTC)
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
                    incident=_handoff_incident(claimed),
                    diagnosis=diagnosis,
                    attempted_queries=attempted_queries,
                    shadow_policy=shadow_decision.to_ledger_mapping(),
                    recovery_policy=recovery_policy,
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

            call = RuntimeAgentToolCall.from_mapping(
                payload,
                allowed_tools=tools.allowed_tools,
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
                transition_runtime_incident(
                    session_factory,
                    incident_id=claimed.id,
                    from_status="claimed",
                    to_status="escalated",
                    claim_token=claim_token,
                    now=operation_now,
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
        transition_runtime_incident(
            session_factory,
            incident_id=claimed.id,
            from_status="claimed",
            to_status="escalated",
            claim_token=claim_token,
            now=operation_now,
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
    except Exception:
        failure_status = _transition_retry(
            session_factory,
            incident=claimed,
            claim_token=claim_token,
            now=operation_now,
            config=config,
        )
        return RuntimeAgentWorkerResult(
            status=failure_status,
            incident_id=claimed.id,
            tool_steps=len(attempted_queries),
            refused_tool_calls=refused,
        )
