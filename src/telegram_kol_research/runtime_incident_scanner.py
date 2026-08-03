"""Closed contracts for the dormant proactive invariant scanner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
import re
from types import MappingProxyType
from typing import Literal, Mapping
from sqlalchemy import Integer, cast, select

from telegram_kol_research.config import RUNTIME_SCANNER_RULE_IDS
from telegram_kol_research.models import PositionMutationIntent, RuntimeIncidentObservation

_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{0,63}:[A-Za-z0-9_.:-]{1,255}")
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE = ("token", "secret", "password", "api_key", "credential")


@dataclass(frozen=True, slots=True)
class InvariantObservation:
    rule_id: str
    rule_version: str
    object_kind: str
    object_id: str
    severity: str
    outcome: Literal["normal", "abnormal", "evidence_insufficient"]
    evidence_references: tuple[str, ...]
    evidence_fingerprint: str
    summary: Mapping[str, str | int | bool | float | None]

    def __post_init__(self) -> None:
        if self.rule_id not in RUNTIME_SCANNER_RULE_IDS:
            raise ValueError("unknown scanner rule")
        if self.rule_version != "1":
            raise ValueError("unsupported scanner rule version")
        if not 1 <= len(self.object_kind) <= 64 or not 1 <= len(self.object_id) <= 255:
            raise ValueError("invalid scanner object")
        if self.severity not in {"critical", "high", "medium", "low"}:
            raise ValueError("unsupported scanner severity")
        if self.outcome not in {"normal", "abnormal", "evidence_insufficient"}:
            raise ValueError("unsupported scanner outcome")
        if not 1 <= len(self.evidence_references) <= 16 or any(
            not _REFERENCE.fullmatch(value) for value in self.evidence_references
        ):
            raise ValueError("invalid evidence reference")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_fingerprint):
            raise ValueError("invalid evidence fingerprint")
        if len(self.summary) > 16:
            raise ValueError("unbounded scanner summary")
        clean = dict(self.summary)
        for key, value in clean.items():
            if not _SAFE_KEY.fullmatch(key) or any(part in key for part in _SENSITIVE):
                raise ValueError("sensitive scanner summary")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("unbounded scanner summary")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError("non-finite scanner summary")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("unsupported scanner summary")
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        object.__setattr__(self, "summary", MappingProxyType(clean))


def build_scanner_facts(session_factory, *, rules: frozenset[str], observed_at):
    """Build only bounded read-only projections required by enabled rules."""
    result: dict[str, tuple[Mapping, ...]] = {}
    if "cancel_outcome_stale_unknown_v1" in rules:
        with session_factory() as session:
            cutoff = observed_at.replace(tzinfo=None) - timedelta(minutes=10)
            unknown_statuses = {
                "reserved", "submitting", "submitted", "recovery_required"
            }
            recovery_pairs = (
                session.query(RuntimeIncidentObservation, PositionMutationIntent)
                .join(
                    PositionMutationIntent,
                    cast(RuntimeIncidentObservation.object_id, Integer)
                    == PositionMutationIntent.id,
                )
                .filter(
                    RuntimeIncidentObservation.rule_id
                    == "cancel_outcome_stale_unknown_v1",
                    RuntimeIncidentObservation.state == "shadow_confirmed",
                    PositionMutationIntent.operation.in_((
                        "cancel_trigger_order", "cancel_position_sltp"
                    )),
                    PositionMutationIntent.status.not_in(tuple(unknown_statuses)),
                )
                .order_by(RuntimeIncidentObservation.last_observed_at.asc())
                .limit(100)
                .all()
            )
            priority_rows = [
                source for _, source in recovery_pairs
            ]

            remaining = max(0, 100 - len(priority_rows))
            observing_pairs = (
                session.query(RuntimeIncidentObservation, PositionMutationIntent)
                .join(
                    PositionMutationIntent,
                    cast(RuntimeIncidentObservation.object_id, Integer)
                    == PositionMutationIntent.id,
                )
                .filter(
                    RuntimeIncidentObservation.rule_id
                    == "cancel_outcome_stale_unknown_v1",
                    RuntimeIncidentObservation.state == "observing",
                    PositionMutationIntent.operation.in_((
                        "cancel_trigger_order", "cancel_position_sltp"
                    )),
                )
                .order_by(RuntimeIncidentObservation.last_observed_at.asc())
                .limit(remaining)
                .all()
                if remaining
                else []
            )
            priority_rows.extend(source for _, source in observing_pairs)
            remaining = max(0, 100 - len(priority_rows))
            observed_subquery = select(
                cast(RuntimeIncidentObservation.object_id, Integer)
            ).where(
                RuntimeIncidentObservation.rule_id
                == "cancel_outcome_stale_unknown_v1"
            )
            candidates = (
                session.query(PositionMutationIntent)
                .filter(PositionMutationIntent.operation.in_(("cancel_trigger_order", "cancel_position_sltp")))
                .filter(PositionMutationIntent.status.in_(tuple(unknown_statuses)))
                .filter(PositionMutationIntent.updated_at <= cutoff)
                .filter(PositionMutationIntent.id.not_in(observed_subquery))
                .order_by(PositionMutationIntent.id.asc())
                .limit(remaining)
                .all()
                if remaining
                else []
            )
            rows = [*priority_rows, *candidates]
            result["cancel_outcome_stale_unknown_v1"] = tuple(
                {
                    "complete": True,
                    "object_id": str(row.id),
                    "cancel_unknown": row.status in unknown_statuses,
                    "transition_window_expired": row.updated_at <= cutoff,
                    "evidence_references": [f"mutation-intent:{row.id}"],
                }
                for row in rows
            )
    return result


def run_scanner_cycle(*, session_factory, config, facts_by_rule: Mapping[str, tuple[Mapping, ...]], observed_at):
    """Evaluate the exact allowlist and persist shadow observations only."""
    from telegram_kol_research.runtime_incident_observations import record_observations
    from telegram_kol_research.runtime_incident_rules import evaluate_rule

    counts = {"rules": 0, "observations": 0, "abnormal": 0, "insufficient": 0}
    observations = []
    for rule_id in sorted(config.rules):
        counts["rules"] += 1
        for facts in tuple(facts_by_rule.get(rule_id, ())):
            result = evaluate_rule(rule_id, facts)
            observations.append(result)
            counts["observations"] += 1
            if result.outcome == "abnormal":
                counts["abnormal"] += 1
            elif result.outcome == "evidence_insufficient":
                counts["insufficient"] += 1
    if observations:
        record_observations(
            session_factory,
            observations=tuple(observations),
            observed_at=observed_at,
        )
    return counts
