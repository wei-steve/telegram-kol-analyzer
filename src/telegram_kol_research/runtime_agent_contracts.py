"""Closed, bounded contracts for the read-only runtime incident agent."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping


_EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._-]{1,128}"
)
_TOOL_CALL_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PLAYBOOK_PATTERN = re.compile(r"[a-z][a-z0-9_-]{1,127}")
_REPAIR_PATH_PATTERN = re.compile(
    r"(?:src/telegram_kol_research|tests)/[A-Za-z0-9_./-]{1,220}\.py"
)
_DIAGNOSIS_FIELDS = frozenset(
    {
        "incident_id",
        "diagnosis_hypothesis",
        "confidence",
        "evidence_references",
        "missing_evidence",
        "recommended_playbook_name",
        "auto_handle_eligible",
        "codex_handoff_required",
        "remaining_risk",
        "expected_state",
        "observed_state",
        "classification",
        "affected_message_ids",
        "likely_code_paths",
        "likely_test_paths",
    }
)
_TOOL_CALL_FIELDS = frozenset({"id", "name", "arguments"})
_CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})
_CLASSIFICATION_VALUES = frozenset(
    {
        "code_defect",
        "configuration_problem",
        "external_dependency_failure",
        "expected_safety_refusal",
        "insufficient_evidence",
    }
)


class RuntimeAgentContractError(ValueError):
    """Raised when model data falls outside the closed agent contract."""


class RuntimeAgentFinalResponseError(RuntimeAgentContractError):
    """Raised when a provider final cannot be parsed as a closed object."""


def _bounded_text(name: str, value: Any, *, maximum: int, allow_empty=False) -> str:
    if not isinstance(value, str):
        raise RuntimeAgentContractError(f"{name} must be text")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise RuntimeAgentContractError(f"{name} must be bounded")
    return normalized


def _bounded_text_list(name: str, value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise RuntimeAgentContractError(f"{name} must be a bounded list")
    return tuple(
        _bounded_text(f"{name} item", item, maximum=512) for item in value
    )


def _affected_message_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise RuntimeAgentContractError("affected_message_ids must be bounded")
    normalized = tuple(value)
    if any(type(item) is not int or item < 1 for item in normalized):
        raise RuntimeAgentContractError("affected_message_ids are invalid")
    if len(set(normalized)) != len(normalized):
        raise RuntimeAgentContractError("affected_message_ids are duplicated")
    return normalized


def _repair_paths(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise RuntimeAgentContractError(f"{name} must be bounded")
    normalized = tuple(value)
    if any(
        not isinstance(item, str)
        or not _REPAIR_PATH_PATTERN.fullmatch(item)
        or ".." in item
        for item in normalized
    ):
        raise RuntimeAgentContractError(f"{name} is invalid")
    return normalized


def validate_evidence_references(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise RuntimeAgentContractError(
            "evidence_references must be a bounded list"
        )
    references = tuple(value)
    if not all(
        isinstance(reference, str)
        and _EVIDENCE_REFERENCE_PATTERN.fullmatch(reference)
        for reference in references
    ):
        raise RuntimeAgentContractError("invalid evidence reference")
    return references


@dataclass(frozen=True, slots=True)
class RuntimeAgentDiagnosis:
    incident_id: int
    diagnosis_hypothesis: str
    confidence: str
    evidence_references: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recommended_playbook_name: str | None
    auto_handle_eligible: bool
    codex_handoff_required: bool
    remaining_risk: str
    expected_state: str
    observed_state: str
    classification: str
    affected_message_ids: tuple[int, ...]
    likely_code_paths: tuple[str, ...]
    likely_test_paths: tuple[str, ...]
    attempted_queries: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_incident_id: int,
    ) -> "RuntimeAgentDiagnosis":
        if not isinstance(payload, Mapping):
            raise RuntimeAgentContractError("diagnosis must be an object")
        unknown = set(payload) - _DIAGNOSIS_FIELDS
        missing = _DIAGNOSIS_FIELDS - set(payload)
        if unknown or missing:
            raise RuntimeAgentContractError(
                f"diagnosis fields do not match the closed contract: "
                f"unknown={sorted(unknown)!r}, missing={sorted(missing)!r}"
            )
        incident_id = payload["incident_id"]
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id != int(expected_incident_id)
        ):
            raise RuntimeAgentContractError("incident_id does not match")
        confidence = payload["confidence"]
        if confidence not in _CONFIDENCE_VALUES:
            raise RuntimeAgentContractError("confidence is invalid")
        classification = payload["classification"]
        if classification not in _CLASSIFICATION_VALUES:
            raise RuntimeAgentContractError("classification is invalid")
        playbook = payload["recommended_playbook_name"]
        if playbook is not None and (
            not isinstance(playbook, str) or not _PLAYBOOK_PATTERN.fullmatch(playbook)
        ):
            raise RuntimeAgentContractError("recommended playbook is invalid")
        auto_eligible = payload["auto_handle_eligible"]
        handoff_required = payload["codex_handoff_required"]
        if not isinstance(auto_eligible, bool) or not isinstance(
            handoff_required, bool
        ):
            raise RuntimeAgentContractError("boolean diagnosis fields are invalid")
        if handoff_required is not True:
            raise RuntimeAgentContractError("codex handoff is required")
        return cls(
            incident_id=incident_id,
            diagnosis_hypothesis=_bounded_text(
                "diagnosis_hypothesis",
                payload["diagnosis_hypothesis"],
                maximum=512,
            ),
            confidence=confidence,
            evidence_references=validate_evidence_references(
                payload["evidence_references"]
            ),
            missing_evidence=_bounded_text_list(
                "missing_evidence", payload["missing_evidence"], limit=16
            ),
            recommended_playbook_name=playbook,
            auto_handle_eligible=auto_eligible,
            codex_handoff_required=handoff_required,
            remaining_risk=_bounded_text(
                "remaining_risk", payload["remaining_risk"], maximum=512
            ),
            expected_state=_bounded_text(
                "expected_state", payload["expected_state"], maximum=512
            ),
            observed_state=_bounded_text(
                "observed_state", payload["observed_state"], maximum=512
            ),
            classification=classification,
            affected_message_ids=_affected_message_ids(
                payload["affected_message_ids"]
            ),
            likely_code_paths=_repair_paths(
                "likely_code_paths", payload["likely_code_paths"]
            ),
            likely_test_paths=_repair_paths(
                "likely_test_paths", payload["likely_test_paths"]
            ),
        )

    def with_attempted_queries(
        self, attempted_queries: tuple[str, ...]
    ) -> "RuntimeAgentDiagnosis":
        if len(attempted_queries) > 16:
            raise RuntimeAgentContractError("attempted_queries is unbounded")
        normalized = tuple(
            _bounded_text("attempted query", query, maximum=64)
            for query in attempted_queries
        )
        return replace(self, attempted_queries=normalized)

    def to_ledger_mapping(self) -> dict[str, Any]:
        return {
            "hypothesis": self.diagnosis_hypothesis,
            "confidence": self.confidence,
            "missing_evidence": list(self.missing_evidence),
            "recommended_playbook": self.recommended_playbook_name,
            "auto_handle_eligible": self.auto_handle_eligible,
            "codex_handoff_required": self.codex_handoff_required,
            "remaining_risk": self.remaining_risk,
            "attempted_queries": list(self.attempted_queries),
            "expected_state": self.expected_state,
            "observed_state": self.observed_state,
            "classification": self.classification,
            "affected_message_ids": list(self.affected_message_ids),
            "likely_code_paths": list(self.likely_code_paths),
            "likely_test_paths": list(self.likely_test_paths),
        }


@dataclass(frozen=True, slots=True)
class RuntimeAgentToolCall:
    call_id: str
    name: str
    arguments: dict[str, int]

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        allowed_tools: frozenset[str],
        expected_incident_id: int,
    ) -> "RuntimeAgentToolCall":
        if not isinstance(payload, Mapping) or set(payload) != _TOOL_CALL_FIELDS:
            raise RuntimeAgentContractError("tool call fields are invalid")
        call_id = payload["id"]
        if not isinstance(call_id, str) or not _TOOL_CALL_ID_PATTERN.fullmatch(
            call_id
        ):
            raise RuntimeAgentContractError("tool call id is invalid")
        name = payload["name"]
        if not isinstance(name, str) or name not in allowed_tools:
            raise RuntimeAgentContractError("unknown tool")
        arguments = payload["arguments"]
        if not isinstance(arguments, Mapping) or set(arguments) != {"incident_id"}:
            raise RuntimeAgentContractError("tool arguments are invalid")
        incident_id = arguments["incident_id"]
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id != int(expected_incident_id)
        ):
            raise RuntimeAgentContractError("tool incident_id does not match")
        return cls(
            call_id=call_id,
            name=name,
            arguments={"incident_id": incident_id},
        )
