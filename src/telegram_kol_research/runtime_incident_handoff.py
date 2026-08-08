"""Build a bounded Codex repair handoff from verified incident evidence."""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from telegram_kol_research.runtime_agent_contracts import RuntimeAgentDiagnosis
from telegram_kol_research.runtime_incidents import get_runtime_incident
from telegram_kol_research.models import RuntimeIncidentHandoffArtifact


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|credential|cookie|session|api.?key|password|passphrase|"
    r"authorization|private.?key)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{6,}\."),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_ALLOWED_INCIDENT_FIELDS = frozenset(
    {"id", "incident_type", "source_kind", "source_record_id", "redacted_summary"}
)


class RuntimeIncidentHandoffError(ValueError):
    """Raised when a handoff cannot remain bounded and redacted."""


_OUTCOME_KINDS = frozenset(
    {
        "diagnosed",
        "reused",
        "provider_failed",
        "tool_failed",
        "evidence_incomplete",
        "timed_out",
    }
)


def _canonical_handoff(handoff: Mapping[str, Any]) -> tuple[str, str]:
    _validate_redacted(handoff)
    encoded = json.dumps(
        dict(handoff), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > 32768:
        raise RuntimeIncidentHandoffError("handoff exceeds byte budget")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def persist_runtime_incident_handoff_in_session(
    session,
    *,
    incident_id: int,
    outcome_kind: str,
    handoff: Mapping[str, Any],
    created_at,
) -> RuntimeIncidentHandoffArtifact:
    """Append one changed handoff revision inside the caller transaction."""

    normalized_outcome = str(outcome_kind)
    if normalized_outcome not in _OUTCOME_KINDS:
        raise RuntimeIncidentHandoffError("handoff outcome is invalid")
    content_json, content_fingerprint = _canonical_handoff(handoff)
    prompt = handoff.get("codex_prompt")
    if not isinstance(prompt, str) or not prompt or len(prompt) > 1500:
        raise RuntimeIncidentHandoffError("handoff prompt is invalid")
    existing = (
        session.query(RuntimeIncidentHandoffArtifact)
        .filter(
            RuntimeIncidentHandoffArtifact.runtime_incident_id == int(incident_id),
            RuntimeIncidentHandoffArtifact.content_fingerprint == content_fingerprint,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    latest_revision = (
        session.query(RuntimeIncidentHandoffArtifact.diagnosis_revision)
        .filter(RuntimeIncidentHandoffArtifact.runtime_incident_id == int(incident_id))
        .order_by(RuntimeIncidentHandoffArtifact.diagnosis_revision.desc())
        .limit(1)
        .scalar()
    )
    artifact = RuntimeIncidentHandoffArtifact(
        runtime_incident_id=int(incident_id),
        diagnosis_revision=int(latest_revision or 0) + 1,
        outcome_kind=normalized_outcome,
        content_json=content_json,
        codex_prompt=prompt,
        evidence_document_json="{}",
        content_fingerprint=content_fingerprint,
        status="pending",
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(artifact)
    session.flush()
    document = {
        "stable_handoff_id": artifact.id,
        "runtime_incident_id": int(incident_id),
        "diagnosis_revision": artifact.diagnosis_revision,
        "content_sha256": content_fingerprint,
        "handoff": dict(handoff),
    }
    artifact.evidence_document_json = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(artifact.evidence_document_json.encode("utf-8")) > 32768:
        raise RuntimeIncidentHandoffError("handoff document exceeds byte budget")
    return artifact


def persist_runtime_incident_handoff(
    session_factory,
    *,
    incident_id: int,
    outcome_kind: str,
    handoff: Mapping[str, Any],
    created_at,
) -> RuntimeIncidentHandoffArtifact:
    with session_factory() as session:
        artifact = persist_runtime_incident_handoff_in_session(
            session,
            incident_id=incident_id,
            outcome_kind=outcome_kind,
            handoff=handoff,
            created_at=created_at,
        )
        session.commit()
        session.refresh(artifact)
        session.expunge(artifact)
        return artifact


def _validate_redacted(value, *, depth=0) -> None:
    if depth > 6:
        raise RuntimeIncidentHandoffError("handoff is not bounded")
    if isinstance(value, dict):
        if len(value) > 32:
            raise RuntimeIncidentHandoffError("handoff is not bounded")
        for key, nested in value.items():
            if not isinstance(key, str) or _SENSITIVE_KEY_PATTERN.search(key):
                raise RuntimeIncidentHandoffError(
                    "handoff contains sensitive material"
                )
            _validate_redacted(nested, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 32:
            raise RuntimeIncidentHandoffError("handoff is not bounded")
        for nested in value:
            _validate_redacted(nested, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 512:
            raise RuntimeIncidentHandoffError("handoff is not bounded")
        if any(pattern.search(value) for pattern in _SENSITIVE_VALUE_PATTERNS):
            raise RuntimeIncidentHandoffError("handoff contains sensitive material")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise RuntimeIncidentHandoffError("handoff has unsupported data")


def build_runtime_incident_handoff(
    *,
    incident: Mapping[str, Any],
    diagnosis: RuntimeAgentDiagnosis,
    attempted_queries: Sequence[str],
    shadow_policy: Mapping[str, Any] | None = None,
    recovery_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if set(incident) != _ALLOWED_INCIDENT_FIELDS:
        raise RuntimeIncidentHandoffError("incident handoff fields are invalid")
    if int(incident["id"]) != diagnosis.incident_id:
        raise RuntimeIncidentHandoffError("incident diagnosis does not match")
    compact_incident = {
        "id": diagnosis.incident_id,
        "incident_type": str(incident["incident_type"])[:64],
        "source_kind": str(incident["source_kind"])[:64],
        "source_record_id": str(incident["source_record_id"])[:255],
        "redacted_summary": incident["redacted_summary"],
    }
    _validate_redacted(compact_incident)
    bounded_queries = [str(query)[:64] for query in tuple(attempted_queries)[:16]]
    attempted_playbooks: list[dict[str, Any]] = []
    if isinstance(shadow_policy, Mapping):
        if (
            shadow_policy.get("mode") != "shadow"
            or shadow_policy.get("policy_version")
            != "runtime-shadow-policy-v1"
            or shadow_policy.get("would_execute") is not False
            or shadow_policy.get("action_executed") is not False
        ):
            raise RuntimeIncidentHandoffError(
                "shadow playbook audit violates Phase 5 boundary"
            )
        nominated = shadow_policy.get("nominated_playbook")
        refusal_reasons = shadow_policy.get("refusal_reasons")
        if isinstance(nominated, str) and nominated:
            attempted_playbooks.append(
                {
                    "name": nominated[:128],
                    "policy_version": str(
                        shadow_policy.get("policy_version") or "unknown"
                    )[:64],
                    "accepted": shadow_policy.get("accepted") is True,
                    "refusal_reasons": [
                        str(reason)[:128]
                        for reason in (
                            refusal_reasons
                            if isinstance(refusal_reasons, list)
                            else []
                        )[:8]
                    ],
                    "action_executed": (
                        shadow_policy.get("action_executed") is True
                    ),
                }
            )
    if isinstance(recovery_policy, Mapping):
        if (
            recovery_policy.get("mode") != "execute"
            or recovery_policy.get("policy_version")
            != "runtime-execution-policy-v1"
            or not isinstance(recovery_policy.get("action_executed"), bool)
            or not isinstance(recovery_policy.get("accepted"), bool)
        ):
            raise RuntimeIncidentHandoffError(
                "recovery playbook audit violates Phase 6 boundary"
            )
        nominated = recovery_policy.get("nominated_playbook")
        refusal_reasons = recovery_policy.get("refusal_reasons")
        if isinstance(nominated, str) and nominated:
            attempted_playbooks.append(
                {
                    "name": nominated[:128],
                    "mode": "execute",
                    "policy_version": str(
                        recovery_policy.get("policy_version") or "unknown"
                    )[:64],
                    "accepted": recovery_policy.get("accepted") is True,
                    "refusal_reasons": [
                        str(reason)[:128]
                        for reason in (
                            refusal_reasons
                            if isinstance(refusal_reasons, list)
                            else []
                        )[:8]
                    ],
                    "action_executed": (
                        recovery_policy.get("action_executed") is True
                    ),
                    "verification_status": str(
                        recovery_policy.get("verification_status") or "unknown"
                    )[:32],
                    "evidence_references": [
                        str(reference)[:160]
                        for reference in (
                            recovery_policy.get("evidence_references")
                            if isinstance(
                                recovery_policy.get("evidence_references"),
                                list,
                            )
                            else []
                        )[:16]
                    ],
                }
            )
    prompt = (
        f"请调查运行异常 incident_id={diagnosis.incident_id}。\n"
        "先读取 AGENTS.md 和该事件的 Codex 交接包。\n"
        "独立验证 Agent 的诊断假设，不要把诊断当作事实。\n"
        "禁止扩大交易权限、替代策略识别或上下文解析、绕过现有安全网关，"
        "也不要重试结果未知的写操作。\n"
        f"证据引用: {', '.join(diagnosis.evidence_references) or '-'}\n"
        f"Agent 假设: {diagnosis.diagnosis_hypothesis}\n"
        f"剩余风险: {diagnosis.remaining_risk}"
    )[:512]
    message_snapshot = compact_incident["redacted_summary"].get(
        "message_operation_snapshot", {}
    ) if isinstance(compact_incident["redacted_summary"], dict) else {}
    contracts = (
        message_snapshot.get("contracts", [])
        if isinstance(message_snapshot, dict)
        else []
    )
    instruction_items = [
        {
            "contract_id": contract.get("contract_id"),
            "items": contract.get("items", []),
        }
        for contract in contracts[:32]
        if isinstance(contract, dict)
    ]
    handoff = {
        "incident": compact_incident,
        "instruction_items": instruction_items,
        "original_reply_evidence": message_snapshot.get(
            "message_evidence", []
        ) if isinstance(message_snapshot, dict) else [],
        "timeline": message_snapshot.get("timeline", [])
        if isinstance(message_snapshot, dict) else [],
        "evidence_references": list(diagnosis.evidence_references),
        "attempted_queries": bounded_queries,
        "attempted_playbooks": attempted_playbooks,
        "agent_hypothesis": {
            "text": diagnosis.diagnosis_hypothesis,
            "confidence": diagnosis.confidence,
            "missing_evidence": list(diagnosis.missing_evidence),
            "classification": diagnosis.classification,
        },
        "expected_state": diagnosis.expected_state,
        "observed_state": diagnosis.observed_state,
        "affected_message_ids": list(diagnosis.affected_message_ids),
        "likely_code_paths": list(diagnosis.likely_code_paths),
        "likely_test_paths": list(diagnosis.likely_test_paths),
        "prohibited_actions": [
            "strategy_targeting",
            "context_resolution_replacement",
            "unchecked_exchange_write",
            "unknown_write_retry",
        ],
        "codex_prompt": prompt,
    }
    _validate_redacted(handoff)
    if len(
        json.dumps(handoff, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ) > 32768:
        raise RuntimeIncidentHandoffError("handoff exceeds byte budget")
    return handoff


def build_runtime_incident_failure_handoff(
    *,
    incident: Mapping[str, Any],
    outcome_kind: str,
    attempted_queries: Sequence[str],
) -> dict[str, Any]:
    """Build a deterministic terminal handoff when investigation fails closed."""

    if outcome_kind not in _OUTCOME_KINDS - {"diagnosed", "reused"}:
        raise RuntimeIncidentHandoffError("handoff outcome is invalid")
    incident_id = int(incident["id"])
    prompt = (
        f"请调查运行异常 incident_id={incident_id}。\n"
        "先读取 AGENTS.md 和稳定交接ID，独立验证证据；Agent调查未能完成。\n"
        "保留现有策略识别与上下文解析权威，禁止重试结果未知的写操作。\n"
        "添加回归测试，并遵守服务器安全窗口与部署门槛。"
    )[:1500]
    summary = incident.get("redacted_summary")
    message_snapshot = (
        summary.get("message_operation_snapshot", {})
        if isinstance(summary, dict)
        else {}
    )
    handoff = {
        "incident": {
            key: incident[key]
            for key in _ALLOWED_INCIDENT_FIELDS
        },
        "outcome_kind": outcome_kind,
        "instruction_items": [
            {
                "contract_id": contract.get("contract_id"),
                "items": contract.get("items", []),
            }
            for contract in message_snapshot.get("contracts", [])[:32]
            if isinstance(contract, dict)
        ] if isinstance(message_snapshot, dict) else [],
        "original_reply_evidence": message_snapshot.get("message_evidence", [])
        if isinstance(message_snapshot, dict) else [],
        "expected_state": "The investigation reaches a bounded terminal diagnosis.",
        "observed_state": f"The investigation ended as {outcome_kind}.",
        "timeline": message_snapshot.get("timeline", [])
        if isinstance(message_snapshot, dict) else [],
        "evidence_references": [f"incident:{incident_id}"],
        "attempted_queries": [str(value)[:64] for value in attempted_queries[:16]],
        "agent_hypothesis": {
            "text": "No trusted diagnosis was produced.",
            "confidence": "low",
            "missing_evidence": [outcome_kind],
            "classification": "insufficient_evidence",
        },
        "likely_code_paths": [
            "src/telegram_kol_research/runtime_agent_worker.py"
        ],
        "likely_test_paths": ["tests/test_runtime_agent_worker.py"],
        "prohibited_actions": [
            "strategy_targeting",
            "context_resolution_replacement",
            "unchecked_exchange_write",
            "unknown_write_retry",
        ],
        "codex_prompt": prompt,
    }
    _canonical_handoff(handoff)
    return handoff


def load_runtime_incident_handoff(
    session_factory,
    *,
    incident_id: int,
) -> dict[str, Any]:
    """Rebuild a handoff from durable diagnosis and evidence ledger fields."""

    with session_factory() as session:
        artifact = (
            session.query(RuntimeIncidentHandoffArtifact)
            .filter(
                RuntimeIncidentHandoffArtifact.runtime_incident_id
                == int(incident_id)
            )
            .order_by(RuntimeIncidentHandoffArtifact.diagnosis_revision.desc())
            .first()
        )
        if artifact is not None:
            try:
                durable = json.loads(artifact.content_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeIncidentHandoffError(
                    "durable runtime incident handoff is invalid"
                ) from exc
            if not isinstance(durable, dict):
                raise RuntimeIncidentHandoffError(
                    "durable runtime incident handoff is invalid"
                )
            _canonical_handoff(durable)
            return durable

    incident = get_runtime_incident(
        session_factory,
        incident_id=int(incident_id),
    )
    if (
        incident is None
        or incident.status != "diagnosed"
        or not incident.diagnosis_json
        or not incident.evidence_refs_json
    ):
        raise RuntimeIncidentHandoffError(
            "diagnosed runtime incident handoff is unavailable"
        )
    try:
        stored = json.loads(incident.diagnosis_json)
        references = json.loads(incident.evidence_refs_json)
        summary = json.loads(incident.redacted_summary)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeIncidentHandoffError(
            "durable runtime incident handoff is invalid"
        ) from exc
    if not isinstance(stored, dict) or not isinstance(summary, dict):
        raise RuntimeIncidentHandoffError(
            "durable runtime incident handoff is invalid"
        )
    try:
        diagnosis = RuntimeAgentDiagnosis.from_mapping(
            {
                "incident_id": incident.id,
                "diagnosis_hypothesis": stored["hypothesis"],
                "confidence": stored["confidence"],
                "evidence_references": references,
                "missing_evidence": stored.get("missing_evidence", []),
                "recommended_playbook_name": stored.get(
                    "recommended_playbook"
                ),
                "auto_handle_eligible": stored.get(
                    "auto_handle_eligible", False
                ),
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
                "affected_message_ids": stored.get(
                    "affected_message_ids", []
                ),
                "likely_code_paths": stored.get("likely_code_paths", []),
                "likely_test_paths": stored.get("likely_test_paths", []),
            },
            expected_incident_id=incident.id,
        ).with_attempted_queries(tuple(stored.get("attempted_queries", ())))
    except (KeyError, RuntimeError, ValueError) as exc:
        raise RuntimeIncidentHandoffError(
            "durable runtime incident handoff is invalid"
        ) from exc
    return build_runtime_incident_handoff(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "source_kind": incident.source_kind,
            "source_record_id": incident.source_record_id,
            "redacted_summary": summary,
        },
        diagnosis=diagnosis,
        attempted_queries=diagnosis.attempted_queries,
        shadow_policy=(
            stored.get("shadow_playbook_policy")
            if isinstance(stored.get("shadow_playbook_policy"), dict)
            else None
        ),
        recovery_policy=(
            stored.get("recovery_playbook_policy")
            if isinstance(stored.get("recovery_playbook_policy"), dict)
            else None
        ),
    )


def load_latest_runtime_incident_handoff_artifact(
    session_factory, *, incident_id: int
) -> RuntimeIncidentHandoffArtifact | None:
    with session_factory() as session:
        artifact = (
            session.query(RuntimeIncidentHandoffArtifact)
            .filter(
                RuntimeIncidentHandoffArtifact.runtime_incident_id
                == int(incident_id)
            )
            .order_by(RuntimeIncidentHandoffArtifact.diagnosis_revision.desc())
            .first()
        )
        if artifact is not None:
            session.expunge(artifact)
        return artifact
