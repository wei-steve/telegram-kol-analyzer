"""Build a bounded Codex repair handoff from verified incident evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from telegram_kol_research.runtime_agent_contracts import RuntimeAgentDiagnosis
from telegram_kol_research.runtime_incidents import get_runtime_incident


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|credential|cookie|session|api.?key|password|passphrase|"
    r"authorization|private.?key)",
    re.IGNORECASE,
)
_ALLOWED_INCIDENT_FIELDS = frozenset(
    {"id", "incident_type", "source_kind", "source_record_id", "redacted_summary"}
)


class RuntimeIncidentHandoffError(ValueError):
    """Raised when a handoff cannot remain bounded and redacted."""


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
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise RuntimeIncidentHandoffError("handoff has unsupported data")


def build_runtime_incident_handoff(
    *,
    incident: Mapping[str, Any],
    diagnosis: RuntimeAgentDiagnosis,
    attempted_queries: Sequence[str],
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
    prompt = (
        f"请调查运行异常 incident_id={diagnosis.incident_id}。\n"
        "先读取 AGENTS.md 和该事件的 Codex 交接包。\n"
        "独立验证 Agent 的诊断假设，不要把诊断当作事实。\n"
        "禁止扩大交易权限、替代策略识别或上下文解析、绕过现有安全网关，"
        "也不要重试结果未知的写操作。\n"
        f"证据引用: {', '.join(diagnosis.evidence_references) or '-'}\n"
        f"Agent 假设: {diagnosis.diagnosis_hypothesis}\n"
        f"剩余风险: {diagnosis.remaining_risk}"
    )[:1600]
    handoff = {
        "incident": compact_incident,
        "evidence_references": list(diagnosis.evidence_references),
        "attempted_queries": bounded_queries,
        "agent_hypothesis": {
            "text": diagnosis.diagnosis_hypothesis,
            "confidence": diagnosis.confidence,
            "missing_evidence": list(diagnosis.missing_evidence),
        },
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
    ) > 8192:
        raise RuntimeIncidentHandoffError("handoff exceeds byte budget")
    return handoff


def load_runtime_incident_handoff(
    session_factory,
    *,
    incident_id: int,
) -> dict[str, Any]:
    """Rebuild a handoff from durable diagnosis and evidence ledger fields."""

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
    )
