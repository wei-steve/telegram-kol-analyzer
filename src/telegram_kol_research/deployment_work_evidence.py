"""Small, deterministic deployment evidence policy."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal


MAX_EVIDENCE_COUNT = 1_000_000_000


@dataclass(frozen=True, slots=True)
class DeploymentEvidenceCounts:
    active_write: int = 0
    unknown_outcome: int = 0
    queued_work: int = 0
    inactive: int = 0
    invalid_evidence: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > MAX_EVIDENCE_COUNT
            ):
                raise ValueError("evidence_count_invalid")


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    decision: Literal["PASS", "WARN", "BLOCK"]
    reason_codes: tuple[str, ...]


def decide_deployment(
    *,
    counts: DeploymentEvidenceCounts,
    writer_changed: bool,
) -> DeploymentDecision:
    """Evaluate the fixed deployment safety matrix without operator overrides."""

    if not isinstance(counts, DeploymentEvidenceCounts):
        raise ValueError("evidence_counts_invalid")
    if not isinstance(writer_changed, bool):
        raise ValueError("writer_changed_invalid")

    blocking_reasons: list[str] = []
    if counts.invalid_evidence:
        blocking_reasons.append("invalid_registered_evidence")
    if counts.active_write:
        blocking_reasons.append("active_exchange_write")
    if counts.unknown_outcome:
        blocking_reasons.append("unknown_exchange_outcome")
    if writer_changed and counts.queued_work:
        blocking_reasons.append("writer_changed_with_queued_work")
    if blocking_reasons:
        return DeploymentDecision("BLOCK", tuple(blocking_reasons))
    if counts.queued_work:
        return DeploymentDecision("WARN", ("queued_work_with_unchanged_writer",))
    return DeploymentDecision("PASS", ())
