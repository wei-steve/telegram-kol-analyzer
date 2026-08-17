"""Bounded deployment work evidence and policy classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


WORK_CLASSIFICATIONS = (
    "in_flight_write",
    "unknown_outcome",
    "restart_safe_wait",
    "historical_residue",
    "terminal",
    "malformed",
)
DEPLOYMENT_CHANGE_CLASSES = frozenset(
    {"code", "schema_compatible", "execution_writer", "live_promotion"}
)
WRITER_SENSITIVE_CHANGE_CLASSES = frozenset(
    {"execution_writer", "live_promotion"}
)
_MAX_BOUNDED_COUNT = 1_000_000


class DeploymentWorkEvidenceError(ValueError):
    """Work evidence is incomplete, unbounded, or otherwise malformed."""


@dataclass(frozen=True, slots=True)
class DeploymentWorkDecision:
    blocking_reason_codes: tuple[str, ...]
    warning_reason_codes: tuple[str, ...]


def classify_deployment_work(
    *,
    counts: Mapping[str, Mapping[str, int]],
    change_class: str,
) -> DeploymentWorkDecision:
    """Map bounded durable work facts to deterministic deployment reasons."""

    normalized_class = str(change_class).strip().lower()
    if normalized_class not in DEPLOYMENT_CHANGE_CLASSES:
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    _validate_counts(counts)

    blocking: set[str] = set()
    warnings: set[str] = set()
    if _has_work(counts, "in_flight_write"):
        blocking.add("deployment_in_flight_write")
    if _has_work(counts, "unknown_outcome"):
        blocking.add("deployment_unknown_outcome")
    if _has_work(counts, "malformed"):
        blocking.add("deployment_evidence_malformed")
    for classification, reason in (
        ("restart_safe_wait", "deployment_restart_safe_wait"),
        ("historical_residue", "deployment_historical_residue"),
    ):
        if not _has_work(counts, classification):
            continue
        target = (
            blocking
            if normalized_class in WRITER_SENSITIVE_CHANGE_CLASSES
            else warnings
        )
        target.add(reason)
    return DeploymentWorkDecision(
        blocking_reason_codes=tuple(sorted(blocking)),
        warning_reason_codes=tuple(sorted(warnings)),
    )


def _has_work(counts: Mapping[str, Mapping[str, int]], classification: str) -> bool:
    return any(value > 0 for value in counts.get(classification, {}).values())


def _validate_counts(counts: Mapping[str, Mapping[str, int]]) -> None:
    if not isinstance(counts, Mapping):
        raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
    allowed = set(WORK_CLASSIFICATIONS)
    for classification, sources in counts.items():
        if classification not in allowed or not isinstance(sources, Mapping):
            raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
        for source, value in sources.items():
            if not isinstance(source, str) or not source:
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_BOUNDED_COUNT
            ):
                raise DeploymentWorkEvidenceError("deployment_evidence_malformed")
