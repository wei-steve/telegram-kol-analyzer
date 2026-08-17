"""Deterministic, bounded deployment safety preflight.

The collector reads durable local evidence only.  It never calls an exchange
adapter and never includes raw messages, order payloads, or position identity
in its output contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Mapping

from .deployment_change_surface import ChangeSurfaceFacts
from .deployment_work_evidence import (
    DeploymentWorkEvidenceError,
    WORK_EVIDENCE_ADAPTERS,
    classify_deployment_work,
    collect_work_evidence,
)


DEPLOYMENT_CHANGE_CLASSES = frozenset(
    {"code", "schema_compatible", "execution_writer", "live_promotion"}
)
DEPLOYMENT_PREFLIGHT_DECISIONS = frozenset({"PASS", "WARN", "BLOCK"})
_WRITER_SENSITIVE_CHANGE_CLASSES = frozenset(
    {"execution_writer", "live_promotion"}
)
_EXPECTED_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHADOW_EVIDENCE_RE = re.compile(r"[0-9a-f]{64}")
_LEGACY_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "expected_commit",
        "change_class",
        "decision",
        "database_watermark",
        "checked_facts",
        "reason_codes",
        "created_at",
        "expires_at",
        "fingerprint",
    }
)
_PHASE_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "production_commit",
        "candidate_commit",
        "requested_change_class",
        "effective_change_class",
        "policy_version",
        "surface_registry_version",
        "change_surface_fingerprint",
        "restart_handler_fingerprint",
        "changed_path_count",
        "change_surface_underdeclared",
        "restart_compatibility_changed",
        "decision",
        "database_watermark",
        "checked_facts",
        "reason_codes",
        "preliminary_fingerprint",
        "created_at",
        "expires_at",
        "fingerprint",
    }
)
DEPLOYMENT_PREFLIGHT_POLICY_VERSION = 1
_MAX_ARTIFACT_BYTES = 32_768
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_DEFAULT_TTL = timedelta(minutes=5)
_DEFAULT_SNAPSHOT_MAX_AGE = timedelta(minutes=5)
_DEFAULT_SHADOW_EVIDENCE_MAX_AGE = timedelta(days=7)
_MIN_SHADOW_OBSERVATION = timedelta(minutes=30)
_BASE_REQUIRED_TABLES = frozenset(
    {"raw_messages", "message_instruction_items", "trade_signals", "execution_events"}
)
# Versioned, exact prior-schema shapes that candidate migrations are allowed to
# upgrade on a disposable backup.  New additive migrations must extend this
# list in the same reviewed commit; arbitrary missing tables remain corruption.
_KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS = frozenset(
    {
        frozenset(),
        frozenset({"trigger_take_profit_convergences"}),
        frozenset(
            {
                "strategy_break_even_convergences",
                "strategy_break_even_convergence_legs",
            }
        ),
        frozenset(
            {
                "trigger_take_profit_convergences",
                "strategy_break_even_convergences",
                "strategy_break_even_convergence_legs",
            }
        ),
    }
)


class DeploymentPreflightInputError(ValueError):
    """The preflight inputs or signed artifact are incomplete or malformed."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class DeploymentPreflightFacts:
    database_watermark: Mapping[str, int]
    work_classification_counts: Mapping[str, Mapping[str, int]]
    work_evidence_fingerprint: str
    protected_open_position_count: int
    exchange_snapshot_available: bool
    exchange_snapshot_complete: bool
    exchange_snapshot_fresh: bool
    exchange_snapshot_stable: bool
    schema_backup_valid: bool | None
    schema_migration_dry_run_valid: bool | None
    prior_schema_missing_table_count: int
    reviewed_shadow_evidence: bool
    explicit_live_authorization: bool
    unprotected_open_position_count: int = 0
    reviewed_shadow_evidence_fingerprint: str | None = None

    @classmethod
    def empty(cls) -> "DeploymentPreflightFacts":
        return cls(
            database_watermark={
                "raw_message_max_id": 0,
                "instruction_item_max_id": 0,
                "trade_signal_max_id": 0,
                "execution_event_max_id": 0,
            },
            work_classification_counts={},
            work_evidence_fingerprint=sha256(b"[]").hexdigest(),
            protected_open_position_count=0,
            exchange_snapshot_available=False,
            exchange_snapshot_complete=False,
            exchange_snapshot_fresh=False,
            exchange_snapshot_stable=False,
            schema_backup_valid=None,
            schema_migration_dry_run_valid=None,
            prior_schema_missing_table_count=0,
            reviewed_shadow_evidence=False,
            explicit_live_authorization=False,
            unprotected_open_position_count=0,
            reviewed_shadow_evidence_fingerprint=None,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "database_watermark": {
                str(key): _bounded_count(value)
                for key, value in sorted(self.database_watermark.items())
            },
            "work_classification_counts": {
                str(classification): {
                    str(source): _bounded_count(value)
                    for source, value in sorted(sources.items())
                    if _bounded_count(value) > 0
                }
                for classification, sources in sorted(
                    self.work_classification_counts.items()
                )
                if sources
            },
            "work_evidence_fingerprint": _validate_sha256_fingerprint(
                self.work_evidence_fingerprint
            ),
            "protected_open_position_count": _bounded_count(
                self.protected_open_position_count
            ),
            "unprotected_open_position_count": _bounded_count(
                self.unprotected_open_position_count
            ),
            "exchange_snapshot_available": bool(self.exchange_snapshot_available),
            "exchange_snapshot_complete": bool(self.exchange_snapshot_complete),
            "exchange_snapshot_fresh": bool(self.exchange_snapshot_fresh),
            "exchange_snapshot_stable": bool(self.exchange_snapshot_stable),
            "schema_backup_valid": self.schema_backup_valid,
            "schema_migration_dry_run_valid": self.schema_migration_dry_run_valid,
            "prior_schema_missing_table_count": _bounded_count(
                self.prior_schema_missing_table_count
            ),
            "reviewed_shadow_evidence": bool(self.reviewed_shadow_evidence),
            "reviewed_shadow_evidence_fingerprint": (
                self.reviewed_shadow_evidence_fingerprint
            ),
            "explicit_live_authorization": bool(
                self.explicit_live_authorization
            ),
        }


def build_deployment_preflight_artifact(
    *,
    expected_commit: str,
    change_class: str,
    facts: DeploymentPreflightFacts,
    now: datetime,
    ttl: timedelta = _DEFAULT_TTL,
) -> dict[str, object]:
    """Classify bounded facts and return a self-authenticating JSON artifact."""

    commit = _validate_expected_commit(expected_commit)
    normalized_class = _validate_change_class(change_class)
    checked_at = _aware_utc(now)
    if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
        raise DeploymentPreflightInputError("preflight_ttl_invalid")

    fact_json = facts.to_json()
    blocking: set[str] = set()
    warnings: set[str] = set()
    try:
        work_decision = classify_deployment_work(
            counts=facts.work_classification_counts,
            change_class=normalized_class,
        )
    except DeploymentWorkEvidenceError as exc:
        raise DeploymentPreflightInputError(str(exc)) from exc
    blocking.update(work_decision.blocking_reason_codes)
    warnings.update(work_decision.warning_reason_codes)
    if int(fact_json["protected_open_position_count"]) > 0:
        warnings.add("protected_open_positions_present")
    if int(fact_json["unprotected_open_position_count"]) > 0:
        blocking.add("unprotected_open_positions_present")

    snapshot_complete = all(
        bool(fact_json[key])
        for key in (
            "exchange_snapshot_available",
            "exchange_snapshot_complete",
            "exchange_snapshot_fresh",
            "exchange_snapshot_stable",
        )
    )
    if not snapshot_complete:
        target = (
            blocking
            if normalized_class in _WRITER_SENSITIVE_CHANGE_CLASSES
            else warnings
        )
        target.add("exchange_snapshot_incomplete")

    if normalized_class == "schema_compatible":
        if facts.schema_backup_valid is not True:
            blocking.add("schema_backup_unavailable")
        if facts.schema_migration_dry_run_valid is not True:
            blocking.add("schema_migration_dry_run_failed")
    if normalized_class == "live_promotion":
        if (
            not facts.reviewed_shadow_evidence
            or not isinstance(facts.reviewed_shadow_evidence_fingerprint, str)
            or not _SHADOW_EVIDENCE_RE.fullmatch(
                facts.reviewed_shadow_evidence_fingerprint
            )
        ):
            blocking.add("reviewed_shadow_evidence_missing")
        if not facts.explicit_live_authorization:
            blocking.add("live_promotion_authorization_missing")

    decision = "BLOCK" if blocking else "WARN" if warnings else "PASS"
    reason_codes = sorted(blocking | warnings)
    body: dict[str, object] = {
        "schema_version": 1,
        "expected_commit": commit,
        "change_class": normalized_class,
        "decision": decision,
        "database_watermark": fact_json.pop("database_watermark"),
        "checked_facts": fact_json,
        "reason_codes": reason_codes,
        "created_at": checked_at.isoformat(),
        "expires_at": (checked_at + ttl).isoformat(),
    }
    body["fingerprint"] = _artifact_fingerprint(body)
    if len(_canonical_json(body).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    return body


def verify_deployment_preflight_artifact(
    artifact: Mapping[str, object],
    *,
    expected_commit: str,
    change_class: str,
    now: datetime,
) -> str:
    """Verify identity, expiry, shape, and fingerprint of one artifact."""

    if not isinstance(artifact, Mapping) or set(artifact) != _LEGACY_ARTIFACT_KEYS:
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    commit = _validate_expected_commit(expected_commit)
    normalized_class = _validate_change_class(change_class)
    if artifact.get("schema_version") != 1:
        raise DeploymentPreflightInputError("preflight_artifact_version_invalid")
    if artifact.get("expected_commit") != commit:
        raise DeploymentPreflightInputError("preflight_artifact_commit_mismatch")
    if artifact.get("change_class") != normalized_class:
        raise DeploymentPreflightInputError("preflight_artifact_class_mismatch")
    decision = artifact.get("decision")
    if decision not in DEPLOYMENT_PREFLIGHT_DECISIONS:
        raise DeploymentPreflightInputError("preflight_artifact_decision_invalid")
    fingerprint = artifact.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DeploymentPreflightInputError("preflight_artifact_fingerprint_invalid")
    unsigned = {key: value for key, value in artifact.items() if key != "fingerprint"}
    if not _constant_time_equal(fingerprint, _artifact_fingerprint(unsigned)):
        raise DeploymentPreflightInputError("preflight_artifact_fingerprint_mismatch")
    try:
        created_at = _aware_utc(datetime.fromisoformat(str(artifact["created_at"])))
        expires_at = _aware_utc(datetime.fromisoformat(str(artifact["expires_at"])))
    except (TypeError, ValueError) as exc:
        raise DeploymentPreflightInputError(
            "preflight_artifact_time_invalid"
        ) from exc
    checked_at = _aware_utc(now)
    if expires_at <= created_at or expires_at - created_at > timedelta(minutes=15):
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid")
    if checked_at < created_at - timedelta(seconds=30):
        raise DeploymentPreflightInputError("preflight_artifact_from_future")
    if checked_at >= expires_at:
        raise DeploymentPreflightInputError("preflight_artifact_expired")
    if not isinstance(artifact.get("database_watermark"), Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_watermark_invalid")
    if not isinstance(artifact.get("checked_facts"), Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_facts_invalid")
    reasons = artifact.get("reason_codes")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 32
        or any(not isinstance(value, str) or len(value) > 96 for value in reasons)
        or reasons != sorted(set(reasons))
    ):
        raise DeploymentPreflightInputError("preflight_artifact_reasons_invalid")
    return str(decision)


def build_preliminary_deployment_preflight_artifact(
    *,
    production_commit: str,
    candidate_commit: str,
    requested_change_class: str,
    change_surface: ChangeSurfaceFacts,
    facts: DeploymentPreflightFacts,
    now: datetime,
    ttl: timedelta = _DEFAULT_TTL,
) -> dict[str, object]:
    """Build phase A evidence, which can authorize only a service-stop attempt."""

    return _build_phase_bound_deployment_preflight_artifact(
        phase="preliminary",
        production_commit=production_commit,
        candidate_commit=candidate_commit,
        requested_change_class=requested_change_class,
        change_surface=change_surface,
        facts=facts,
        preliminary_fingerprint=None,
        now=now,
        ttl=ttl,
    )


def build_final_deployment_preflight_artifact(
    *,
    preliminary_artifact: Mapping[str, object],
    production_commit: str,
    candidate_commit: str,
    requested_change_class: str,
    change_surface: ChangeSurfaceFacts,
    facts: DeploymentPreflightFacts,
    now: datetime,
    ttl: timedelta = _DEFAULT_TTL,
) -> dict[str, object]:
    """Build phase B evidence only from one valid matching phase A artifact."""

    checked_at = _aware_utc(now)
    preliminary_decision = verify_phase_bound_deployment_preflight_artifact(
        preliminary_artifact,
        phase="preliminary",
        production_commit=production_commit,
        candidate_commit=candidate_commit,
        requested_change_class=requested_change_class,
        change_surface=change_surface,
        now=checked_at,
    )
    if preliminary_decision == "BLOCK":
        raise DeploymentPreflightInputError("preliminary_artifact_blocked")
    preliminary_watermark = preliminary_artifact.get("database_watermark")
    if not isinstance(preliminary_watermark, Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_watermark_invalid")
    final_watermark = facts.to_json()["database_watermark"]
    _validate_watermark_transition(preliminary_watermark, final_watermark)
    preliminary_fingerprint = preliminary_artifact.get("fingerprint")
    if not isinstance(preliminary_fingerprint, str):
        raise DeploymentPreflightInputError(
            "preflight_artifact_fingerprint_invalid"
        )
    return _build_phase_bound_deployment_preflight_artifact(
        phase="final",
        production_commit=production_commit,
        candidate_commit=candidate_commit,
        requested_change_class=requested_change_class,
        change_surface=change_surface,
        facts=facts,
        preliminary_fingerprint=preliminary_fingerprint,
        now=checked_at,
        ttl=ttl,
    )


def verify_phase_bound_deployment_preflight_artifact(
    artifact: Mapping[str, object],
    *,
    phase: str,
    production_commit: str,
    candidate_commit: str,
    requested_change_class: str,
    change_surface: ChangeSurfaceFacts,
    now: datetime,
    preliminary_artifact: Mapping[str, object] | None = None,
) -> str:
    """Verify one schema-v2 artifact and its exact Git/surface identity."""

    if not isinstance(artifact, Mapping) or set(artifact) != _PHASE_ARTIFACT_KEYS:
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    normalized_phase = _validate_phase(phase)
    production = _validate_expected_commit(production_commit)
    candidate = _validate_expected_commit(candidate_commit)
    requested = _validate_change_class(requested_change_class)
    _validate_change_surface(change_surface, requested_change_class=requested)
    if artifact.get("schema_version") != 2:
        raise DeploymentPreflightInputError("preflight_artifact_version_invalid")
    if artifact.get("phase") != normalized_phase:
        raise DeploymentPreflightInputError("preflight_artifact_phase_mismatch")
    if artifact.get("production_commit") != production:
        raise DeploymentPreflightInputError("preflight_artifact_commit_mismatch")
    if artifact.get("candidate_commit") != candidate:
        raise DeploymentPreflightInputError("preflight_artifact_commit_mismatch")
    if artifact.get("requested_change_class") != requested:
        raise DeploymentPreflightInputError("preflight_artifact_class_mismatch")
    if artifact.get("effective_change_class") != change_surface.effective_change_class:
        raise DeploymentPreflightInputError("preflight_artifact_class_mismatch")
    expected_surface_values = {
        "policy_version": DEPLOYMENT_PREFLIGHT_POLICY_VERSION,
        "surface_registry_version": change_surface.registry_version,
        "change_surface_fingerprint": change_surface.change_surface_fingerprint,
        "restart_handler_fingerprint": change_surface.restart_handler_fingerprint,
        "changed_path_count": change_surface.changed_path_count,
        "change_surface_underdeclared": change_surface.underdeclared,
        "restart_compatibility_changed": (
            change_surface.restart_compatibility_changed
        ),
    }
    if any(artifact.get(key) != value for key, value in expected_surface_values.items()):
        raise DeploymentPreflightInputError("preflight_artifact_surface_mismatch")
    decision = _verify_common_artifact_fields(artifact, now=now)
    parent_fingerprint = artifact.get("preliminary_fingerprint")
    if normalized_phase == "preliminary":
        if parent_fingerprint is not None:
            raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
    else:
        if not isinstance(parent_fingerprint, str) or not _SHADOW_EVIDENCE_RE.fullmatch(
            parent_fingerprint
        ):
            raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
        if preliminary_artifact is None:
            raise DeploymentPreflightInputError(
                "preflight_artifact_parent_required"
            )
        verify_phase_bound_deployment_preflight_artifact(
            preliminary_artifact,
            phase="preliminary",
            production_commit=production,
            candidate_commit=candidate,
            requested_change_class=requested,
            change_surface=change_surface,
            now=now,
        )
        if preliminary_artifact.get("fingerprint") != parent_fingerprint:
            raise DeploymentPreflightInputError(
                "preflight_artifact_parent_mismatch"
            )
    return decision


def _build_phase_bound_deployment_preflight_artifact(
    *,
    phase: str,
    production_commit: str,
    candidate_commit: str,
    requested_change_class: str,
    change_surface: ChangeSurfaceFacts,
    facts: DeploymentPreflightFacts,
    preliminary_fingerprint: str | None,
    now: datetime,
    ttl: timedelta,
) -> dict[str, object]:
    normalized_phase = _validate_phase(phase)
    production = _validate_expected_commit(production_commit)
    candidate = _validate_expected_commit(candidate_commit)
    requested = _validate_change_class(requested_change_class)
    _validate_change_surface(change_surface, requested_change_class=requested)
    checked_at = _aware_utc(now)
    if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
        raise DeploymentPreflightInputError("preflight_ttl_invalid")
    if normalized_phase == "preliminary" and preliminary_fingerprint is not None:
        raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
    if normalized_phase == "final" and (
        not isinstance(preliminary_fingerprint, str)
        or not _SHADOW_EVIDENCE_RE.fullmatch(preliminary_fingerprint)
    ):
        raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
    fact_json = facts.to_json()
    blocking, warnings = _classify_phase_facts(
        facts=facts,
        fact_json=fact_json,
        effective_change_class=change_surface.effective_change_class,
        require_schema_evidence=normalized_phase == "final",
    )
    blocking.update(change_surface.blocking_reason_codes)
    decision = "BLOCK" if blocking else "WARN" if warnings else "PASS"
    body: dict[str, object] = {
        "schema_version": 2,
        "phase": normalized_phase,
        "production_commit": production,
        "candidate_commit": candidate,
        "requested_change_class": requested,
        "effective_change_class": change_surface.effective_change_class,
        "policy_version": DEPLOYMENT_PREFLIGHT_POLICY_VERSION,
        "surface_registry_version": change_surface.registry_version,
        "change_surface_fingerprint": change_surface.change_surface_fingerprint,
        "restart_handler_fingerprint": change_surface.restart_handler_fingerprint,
        "changed_path_count": _bounded_count(change_surface.changed_path_count),
        "change_surface_underdeclared": bool(change_surface.underdeclared),
        "restart_compatibility_changed": bool(
            change_surface.restart_compatibility_changed
        ),
        "decision": decision,
        "database_watermark": fact_json.pop("database_watermark"),
        "checked_facts": fact_json,
        "reason_codes": sorted(blocking | warnings),
        "preliminary_fingerprint": preliminary_fingerprint,
        "created_at": checked_at.isoformat(),
        "expires_at": (checked_at + ttl).isoformat(),
    }
    body["fingerprint"] = _artifact_fingerprint(body)
    if len(_canonical_json(body).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    return body


def _classify_phase_facts(
    *,
    facts: DeploymentPreflightFacts,
    fact_json: Mapping[str, object],
    effective_change_class: str,
    require_schema_evidence: bool,
) -> tuple[set[str], set[str]]:
    blocking: set[str] = set()
    warnings: set[str] = set()
    try:
        work_decision = classify_deployment_work(
            counts=facts.work_classification_counts,
            change_class=effective_change_class,
        )
    except DeploymentWorkEvidenceError as exc:
        raise DeploymentPreflightInputError(str(exc)) from exc
    blocking.update(work_decision.blocking_reason_codes)
    warnings.update(work_decision.warning_reason_codes)
    if int(fact_json["protected_open_position_count"]) > 0:
        warnings.add("protected_open_positions_present")
    if int(fact_json["unprotected_open_position_count"]) > 0:
        blocking.add("unprotected_open_positions_present")
    snapshot_complete = all(
        bool(fact_json[key])
        for key in (
            "exchange_snapshot_available",
            "exchange_snapshot_complete",
            "exchange_snapshot_fresh",
            "exchange_snapshot_stable",
        )
    )
    if not snapshot_complete:
        target = (
            blocking
            if effective_change_class in _WRITER_SENSITIVE_CHANGE_CLASSES
            else warnings
        )
        target.add("exchange_snapshot_incomplete")
    if require_schema_evidence and effective_change_class == "schema_compatible":
        if not isinstance(facts.schema_backup_valid, bool) or not isinstance(
            facts.schema_migration_dry_run_valid, bool
        ):
            raise DeploymentPreflightInputError(
                "preflight_schema_evidence_invalid"
            )
        if facts.schema_backup_valid is not True:
            blocking.add("schema_backup_unavailable")
        if facts.schema_migration_dry_run_valid is not True:
            blocking.add("schema_migration_dry_run_failed")
    if effective_change_class == "live_promotion":
        if (
            not facts.reviewed_shadow_evidence
            or not isinstance(facts.reviewed_shadow_evidence_fingerprint, str)
            or not _SHADOW_EVIDENCE_RE.fullmatch(
                facts.reviewed_shadow_evidence_fingerprint
            )
        ):
            blocking.add("reviewed_shadow_evidence_missing")
        if not facts.explicit_live_authorization:
            blocking.add("live_promotion_authorization_missing")
    return blocking, warnings


def _validate_change_surface(
    change_surface: ChangeSurfaceFacts,
    *,
    requested_change_class: str,
) -> None:
    if not isinstance(change_surface, ChangeSurfaceFacts):
        raise DeploymentPreflightInputError("preflight_artifact_surface_invalid")
    if change_surface.effective_change_class not in DEPLOYMENT_CHANGE_CLASSES:
        raise DeploymentPreflightInputError("preflight_artifact_surface_invalid")
    if change_surface.registry_version <= 0:
        raise DeploymentPreflightInputError("preflight_artifact_surface_invalid")
    _validate_sha256_fingerprint(change_surface.change_surface_fingerprint)
    _validate_sha256_fingerprint(change_surface.restart_handler_fingerprint)
    _bounded_count(change_surface.changed_path_count)
    requested_rank = _change_class_rank(requested_change_class)
    effective_rank = _change_class_rank(change_surface.effective_change_class)
    if effective_rank < requested_rank:
        raise DeploymentPreflightInputError("preflight_artifact_class_mismatch")


def _change_class_rank(value: str) -> int:
    return {
        "code": 0,
        "schema_compatible": 1,
        "execution_writer": 2,
        "live_promotion": 3,
    }[value]


def _validate_phase(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"preliminary", "final"}:
        raise DeploymentPreflightInputError("preflight_artifact_phase_invalid")
    return normalized


def _validate_watermark_transition(
    preliminary: Mapping[str, object],
    final: Mapping[str, object],
) -> None:
    if set(preliminary) != set(final):
        raise DeploymentPreflightInputError("preflight_watermark_invalid")
    for key, initial in preliminary.items():
        later = final.get(key)
        if not _is_nonnegative_plain_int(initial) or not _is_nonnegative_plain_int(
            later
        ):
            raise DeploymentPreflightInputError("preflight_watermark_invalid")
        if int(later) < int(initial):
            raise DeploymentPreflightInputError("preflight_watermark_regression")


def _verify_common_artifact_fields(
    artifact: Mapping[str, object],
    *,
    now: datetime,
) -> str:
    decision = artifact.get("decision")
    if decision not in DEPLOYMENT_PREFLIGHT_DECISIONS:
        raise DeploymentPreflightInputError("preflight_artifact_decision_invalid")
    fingerprint = artifact.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHADOW_EVIDENCE_RE.fullmatch(
        fingerprint
    ):
        raise DeploymentPreflightInputError(
            "preflight_artifact_fingerprint_invalid"
        )
    unsigned = {key: value for key, value in artifact.items() if key != "fingerprint"}
    if not _constant_time_equal(fingerprint, _artifact_fingerprint(unsigned)):
        raise DeploymentPreflightInputError(
            "preflight_artifact_fingerprint_mismatch"
        )
    try:
        created_at = _aware_utc(datetime.fromisoformat(str(artifact["created_at"])))
        expires_at = _aware_utc(datetime.fromisoformat(str(artifact["expires_at"])))
    except (TypeError, ValueError) as exc:
        raise DeploymentPreflightInputError(
            "preflight_artifact_time_invalid"
        ) from exc
    checked_at = _aware_utc(now)
    if expires_at <= created_at or expires_at - created_at > timedelta(minutes=15):
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid")
    if checked_at < created_at - timedelta(seconds=30):
        raise DeploymentPreflightInputError("preflight_artifact_from_future")
    if checked_at >= expires_at:
        raise DeploymentPreflightInputError("preflight_artifact_expired")
    if not isinstance(artifact.get("database_watermark"), Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_watermark_invalid")
    if not isinstance(artifact.get("checked_facts"), Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_facts_invalid")
    reasons = artifact.get("reason_codes")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 32
        or any(not isinstance(value, str) or len(value) > 96 for value in reasons)
        or reasons != sorted(set(reasons))
    ):
        raise DeploymentPreflightInputError("preflight_artifact_reasons_invalid")
    return str(decision)


def read_deployment_preflight_artifact(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        if source.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise DeploymentPreflightInputError("preflight_artifact_too_large")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except DeploymentPreflightInputError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeploymentPreflightInputError("preflight_artifact_unreadable") from exc
    if not isinstance(payload, dict):
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    return payload


def write_deployment_preflight_artifact(
    path: str | Path,
    artifact: Mapping[str, object],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = _canonical_json(artifact) + "\n"
    if len(body.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def collect_deployment_preflight_facts(
    *,
    database_path: str | Path,
    change_class: str,
    now: datetime,
    live_snapshot_path: str | Path | None = None,
    previous_live_snapshot_path: str | Path | None = None,
    schema_backup_path: str | Path | None = None,
    schema_migration_dry_run_path: str | Path | None = None,
    reviewed_shadow_evidence_path: str | Path | None = None,
    expected_commit: str | None = None,
    explicit_live_authorization: bool = False,
) -> DeploymentPreflightFacts:
    """Collect bounded facts through query-only SQLite and a persisted snapshot."""

    normalized_class = _validate_change_class(change_class)
    checked_at = _aware_utc(now)
    shadow_claim = _read_shadow_evidence_claim(reviewed_shadow_evidence_path)
    database = Path(database_path).resolve()
    if not database.is_file() or database.stat().st_size <= 0:
        raise DeploymentPreflightInputError("database_schema_incomplete")

    expected_tables = set(_BASE_REQUIRED_TABLES) | {"position_protection_ledger"} | {
        *(adapter.table for adapter in WORK_EVIDENCE_ADAPTERS),
    }
    uri = database.as_uri() + "?mode=ro"
    work_summary = None
    stop_ownership: set[tuple[str, str, str, str]] = set()
    shadow_observation: Mapping[str, int] | None = None
    available: set[str] = set()
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not _BASE_REQUIRED_TABLES.issubset(available):
                raise DeploymentPreflightInputError("database_schema_incomplete")
            missing_work_tables = expected_tables - available
            if missing_work_tables and normalized_class != "schema_compatible":
                raise DeploymentPreflightInputError("database_schema_incomplete")
            if (
                normalized_class == "schema_compatible"
                and frozenset(missing_work_tables)
                not in _KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS
            ):
                raise DeploymentPreflightInputError(
                    "database_prior_schema_unrecognized"
                )
            watermark = {
                "raw_message_max_id": _max_id(connection, "raw_messages"),
                "instruction_item_max_id": _max_id(
                    connection, "message_instruction_items"
                ),
                "trade_signal_max_id": _max_id(connection, "trade_signals"),
                "execution_event_max_id": _max_id(
                    connection, "execution_events"
                ),
            }
            work_summary = collect_work_evidence(
                connection,
                available_tables=available,
                now=checked_at,
            )
            if "position_protection_ledger" in available:
                ledger_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(position_protection_ledger)"
                    ).fetchall()
                }
                required_ledger_columns = {
                    "venue", "order_id", "pos_id", "instrument_id", "side",
                    "purpose", "status",
                }
                if not required_ledger_columns.issubset(ledger_columns):
                    raise DeploymentPreflightInputError("database_schema_incomplete")
                stop_ownership = {
                    (
                        str(row[0]),
                        str(row[1]),
                        str(row[2]).upper(),
                        str(row[3]).lower(),
                    )
                    for row in connection.execute(
                        "SELECT order_id, pos_id, instrument_id, side "
                        "FROM position_protection_ledger "
                        "WHERE venue = 'deepcoin' "
                        "AND purpose IN ('stop_loss', 'combined') "
                        "AND status IN ('verified', 'active')"
                    ).fetchall()
                    if all(value not in (None, "") for value in row)
                }
            if shadow_claim is not None:
                shadow_observation = _collect_shadow_observation(
                    connection,
                    claim=shadow_claim,
                    available_tables=available,
                )
            connection.rollback()
    except DeploymentPreflightInputError:
        raise
    except DeploymentWorkEvidenceError as exc:
        raise DeploymentPreflightInputError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise DeploymentPreflightInputError("database_read_incomplete") from exc

    snapshot = _read_exchange_snapshot(
        live_snapshot_path,
        previous_path=previous_live_snapshot_path,
        stop_ownership=stop_ownership,
        now=checked_at,
        max_age=_DEFAULT_SNAPSHOT_MAX_AGE,
    )
    backup_valid = (
        _validate_schema_backup(
            schema_backup_path,
            live_database_path=database,
            required_tables=_BASE_REQUIRED_TABLES | (available & expected_tables),
            expected_watermark=watermark,
        )
        if normalized_class == "schema_compatible"
        else None
    )
    migration_valid = (
        _validate_migration_dry_run(
            schema_migration_dry_run_path,
            live_database_path=database,
            expected_tables=expected_tables,
            expected_watermark=watermark,
        )
        if normalized_class == "schema_compatible"
        else None
    )
    reviewed, reviewed_fingerprint = _validate_shadow_evidence(
        reviewed_shadow_evidence_path,
        expected_commit=expected_commit,
        database_watermark=watermark,
        observed=shadow_observation,
        now=checked_at,
    )
    return DeploymentPreflightFacts(
        database_watermark=watermark,
        work_classification_counts=work_summary.counts if work_summary else {},
        work_evidence_fingerprint=(
            work_summary.fingerprint
            if work_summary
            else sha256(b"[]").hexdigest()
        ),
        protected_open_position_count=snapshot["protected_open_position_count"],
        exchange_snapshot_available=snapshot["available"],
        exchange_snapshot_complete=snapshot["complete"],
        exchange_snapshot_fresh=snapshot["fresh"],
        exchange_snapshot_stable=snapshot["stable"],
        schema_backup_valid=backup_valid,
        schema_migration_dry_run_valid=migration_valid,
        prior_schema_missing_table_count=len(expected_tables - available),
        reviewed_shadow_evidence=reviewed,
        explicit_live_authorization=bool(explicit_live_authorization),
        unprotected_open_position_count=snapshot[
            "unprotected_open_position_count"
        ],
        reviewed_shadow_evidence_fingerprint=reviewed_fingerprint,
    )


def _read_exchange_snapshot(
    path: str | Path | None,
    *,
    previous_path: str | Path | None,
    stop_ownership: set[tuple[str, str, str, str]],
    now: datetime,
    max_age: timedelta,
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "complete": False,
        "fresh": False,
        "stable": False,
        "protected_open_position_count": 0,
        "unprotected_open_position_count": 0,
    }
    if path is None:
        return unavailable
    current = _parse_exchange_snapshot(
        path, now=now, max_age=max_age, stop_ownership=stop_ownership
    )
    if not current["available"]:
        return unavailable
    previous = _parse_exchange_snapshot(
        previous_path, now=now, max_age=max_age, stop_ownership=stop_ownership
    )
    current["stable"] = bool(
        previous["available"]
        and current["complete"]
        and previous["complete"]
        and current["fresh"]
        and previous["fresh"]
        and current["capture_identity"] != previous["capture_identity"]
        and current["canonical_facts"] == previous["canonical_facts"]
    )
    current.pop("capture_identity", None)
    current.pop("canonical_facts", None)
    return current


def _parse_exchange_snapshot(
    path: str | Path | None,
    *,
    now: datetime,
    max_age: timedelta,
    stop_ownership: set[tuple[str, str, str, str]],
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "complete": False,
        "fresh": False,
        "stable": False,
        "protected_open_position_count": 0,
        "unprotected_open_position_count": 0,
        "capture_identity": "",
        "canonical_facts": "",
    }
    if path is None:
        return unavailable
    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size > _MAX_SNAPSHOT_BYTES:
            return unavailable
        raw = source.read_bytes()
        payload = json.loads(raw)
        captured_at = _aware_utc(datetime.fromisoformat(str(payload["captured_at"])))
        version = str(payload.get("version") or "")
        body = payload["payload"]
        live = body["_live_source"]
        positions = live["positions"]
        tpsl_orders = live["tpsl_orders"]
        complete = (
            payload.get("schema_version") == 1
            and bool(version)
            and isinstance(body, dict)
            and body.get("error") in (None, "")
            and isinstance(positions, list)
            and isinstance(tpsl_orders, list)
            and live.get("tpsl_evidence_available") is True
        )
        owned_stop_identities: set[tuple[str, str, str, str]] = set()
        if isinstance(tpsl_orders, list):
            for order in tpsl_orders[:1000]:
                if not isinstance(order, dict):
                    complete = False
                    continue
                order_id = _exchange_order_id(order)
                raw_pos_ids = {
                    str(order.get(field)).strip()
                    for field in (
                        "closePosId",
                        "close_pos_id",
                        "closePositionId",
                        "posId",
                        "pos_id",
                        "positionId",
                    )
                    if order.get(field) not in (None, "")
                }
                raw_instrument = str(order.get("instId") or "").strip().upper()
                raw_side = str(order.get("posSide") or "").strip().lower()
                if order_id and any(
                    _is_positive_decimal(order.get(field))
                    for field in (
                        "slTriggerPx",
                        "slTriggerPrice",
                        "closeSLTriggerPrice",
                    )
                ):
                    for ownership in stop_ownership:
                        owned_order_id, pos_id, instrument, side = ownership
                        if owned_order_id != order_id:
                            continue
                        if raw_pos_ids and raw_pos_ids != {pos_id}:
                            continue
                        if raw_instrument and raw_instrument != instrument:
                            continue
                        if raw_side and raw_side != side:
                            continue
                        owned_stop_identities.add(ownership)
            if len(tpsl_orders) > 1000:
                complete = False
        owned_positions = {
            (pos_id, instrument, side)
            for _, pos_id, instrument, side in owned_stop_identities
        }
        protected_count = 0
        unprotected_count = 0
        if isinstance(positions, list):
            for row in positions[:500]:
                if not isinstance(row, dict):
                    complete = False
                    continue
                try:
                    is_open = Decimal(str(row.get("pos"))) > 0
                except (InvalidOperation, TypeError, ValueError):
                    complete = False
                    continue
                if not is_open:
                    continue
                identity = _position_side_identity(row)
                pos_id = str(row.get("posId") or "").strip()
                if identity is None or not pos_id:
                    complete = False
                has_owned_stop = bool(
                    identity is not None
                    and (pos_id, identity[0], identity[1]) in owned_positions
                )
                if _is_positive_decimal(row.get("slTriggerPx")) or has_owned_stop:
                    protected_count += 1
                else:
                    unprotected_count += 1
        if len(positions) > 500:
            complete = False
        fresh = now - max_age <= captured_at <= now + timedelta(seconds=30)
        canonical_facts = sha256(
            _canonical_json(
                _canonical_exchange_safety_facts(
                    positions=positions,
                    tpsl_orders=tpsl_orders,
                    evidence_available=live.get("tpsl_evidence_available") is True,
                )
            ).encode("utf-8")
        ).hexdigest()
        return {
            "available": True,
            "complete": bool(complete),
            "fresh": bool(fresh),
            "stable": False,
            "protected_open_position_count": protected_count,
            "unprotected_open_position_count": unprotected_count,
            "capture_identity": f"{version}:{captured_at.isoformat()}",
            "canonical_facts": canonical_facts,
        }
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return unavailable


def _validate_schema_backup(
    path: str | Path | None,
    *,
    live_database_path: Path,
    required_tables: set[str],
    expected_watermark: Mapping[str, int],
) -> bool:
    if path is None:
        return False
    source = Path(path).resolve()
    if (
        source == live_database_path.resolve()
        or not source.is_file()
        or source.stat().st_size <= 0
    ):
        return False
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            backup_watermark = {
                "raw_message_max_id": _max_id(connection, "raw_messages"),
                "instruction_item_max_id": _max_id(
                    connection, "message_instruction_items"
                ),
                "trade_signal_max_id": _max_id(connection, "trade_signals"),
                "execution_event_max_id": _max_id(connection, "execution_events"),
            }
    except sqlite3.Error:
        return False
    return required_tables.issubset(available) and backup_watermark == dict(
        expected_watermark
    )


def _validate_migration_dry_run(
    path: str | Path | None,
    *,
    live_database_path: Path,
    expected_tables: set[str],
    expected_watermark: Mapping[str, int],
) -> bool:
    """Prove the expected code migrated a disposable backup successfully."""

    if path is None:
        return False
    source = Path(path).resolve()
    if (
        source == live_database_path.resolve()
        or not source.is_file()
        or source.stat().st_size <= 0
    ):
        return False
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            dry_run_watermark = {
                "raw_message_max_id": _max_id(connection, "raw_messages"),
                "instruction_item_max_id": _max_id(
                    connection, "message_instruction_items"
                ),
                "trade_signal_max_id": _max_id(connection, "trade_signals"),
                "execution_event_max_id": _max_id(connection, "execution_events"),
            }
            connection.rollback()
    except sqlite3.Error:
        return False
    return (
        expected_tables.issubset(available)
        and dry_run_watermark == dict(expected_watermark)
        and _candidate_model_schema_matches(source)
    )


def _candidate_model_schema_matches(database_path: Path) -> bool:
    """Require the dry-run copy to satisfy every candidate ORM column/index."""

    from sqlalchemy import UniqueConstraint
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from telegram_kol_research.models import Base

    try:
        with sqlite3.connect(
            database_path.as_uri() + "?mode=ro", uri=True
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            for table_name, table in Base.metadata.tables.items():
                actual_columns = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({_safe_identifier(table_name)})"
                    ).fetchall()
                }
                expected_columns = {str(column.name) for column in table.columns}
                if not expected_columns.issubset(actual_columns):
                    return False

                actual_global_unique_columns: set[tuple[str, ...]] = set()
                actual_unique_by_name: dict[str, tuple[tuple[str, ...], str]] = {}
                actual_index_names: set[str] = set()
                for index_row in connection.execute(
                    f"PRAGMA index_list({_safe_identifier(table_name)})"
                ).fetchall():
                    actual_index_names.add(str(index_row[1]))
                    if not bool(index_row[2]):
                        continue
                    index_name = str(index_row[1]).replace("'", "''")
                    key_rows = [
                        row
                        for row in connection.execute(
                            f"PRAGMA index_xinfo('{index_name}')"
                        ).fetchall()
                        if bool(row[5])
                    ]
                    if not key_rows or any(row[2] is None for row in key_rows):
                        continue
                    columns = tuple(str(row[2]) for row in key_rows)
                    if columns:
                        sql_row = connection.execute(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type = 'index' AND name = ?",
                            (str(index_row[1]),),
                        ).fetchone()
                        actual_unique_by_name[str(index_row[1])] = (
                            columns,
                            str(sql_row[0] or "") if sql_row else "",
                        )
                        if not re.search(
                            r"\bWHERE\b",
                            str(sql_row[0] or "") if sql_row else "",
                            flags=re.I,
                        ):
                            actual_global_unique_columns.add(columns)
                expected_constraint_columns = {
                    tuple(str(column.name) for column in constraint.columns)
                    for constraint in table.constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                if not expected_constraint_columns.issubset(
                    actual_global_unique_columns
                ):
                    return False
                for index in table.indexes:
                    if not bool(index.unique) or not index.name:
                        continue
                    actual_index = actual_unique_by_name.get(str(index.name))
                    expected_index_columns = tuple(
                        str(column.name) for column in index.columns
                    )
                    expected_where = index.dialect_options["sqlite"].get("where")
                    compiled_where = (
                        str(
                            expected_where.compile(
                                dialect=sqlite_dialect.dialect(),
                                compile_kwargs={"literal_binds": True},
                            )
                        )
                        if expected_where is not None
                        else ""
                    )
                    if (
                        actual_index is None
                        and str(index.name) not in actual_index_names
                        and connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'index' AND name = ?",
                            (str(index.name),),
                        ).fetchone()
                        is None
                        and expected_index_columns == ("venue", "pos_id")
                        and _normalize_sql_predicate(compiled_where)
                        == _normalize_sql_predicate(
                            "pos_id IS NOT NULL AND pos_id != ''"
                        )
                        and _audited_legacy_unique_index_gap(
                            connection,
                            table_name=table_name,
                            index_name=str(index.name),
                        )
                    ):
                        continue
                    if actual_index is None or actual_index[0] != expected_index_columns:
                        return False
                    where_parts = re.split(
                        r"\bWHERE\b", actual_index[1], maxsplit=1, flags=re.I
                    )
                    actual_where = where_parts[1] if len(where_parts) == 2 else ""
                    if _normalize_sql_predicate(actual_where) != _normalize_sql_predicate(
                        compiled_where
                    ):
                        return False
            connection.rollback()
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return True


def _audited_legacy_unique_index_gap(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    index_name: str,
) -> bool:
    """Accept only the duplicate-owner gap that bootstrap intentionally preserves."""

    if (
        table_name != "execution_order_legs"
        or index_name != "uq_execution_order_legs_venue_pos"
    ):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM execution_order_legs "
            "WHERE pos_id IS NOT NULL AND pos_id != '' "
            "GROUP BY venue, pos_id HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        is not None
    )


def _normalize_sql_predicate(value: object) -> str:
    return re.sub(r"[\s\"`\[\]]+", "", str(value or "")).lower()


def _read_shadow_evidence_claim(path: str | Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size > _MAX_ARTIFACT_BYTES:
            return None
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _collect_shadow_observation(
    connection: sqlite3.Connection,
    *,
    claim: Mapping[str, object],
    available_tables: set[str],
) -> dict[str, int] | None:
    if not {
        "message_instruction_items",
        "signal_candidates",
        "instruction_execution_contracts",
        "trading_settings",
    }.issubset(available_tables):
        return None
    scope = claim.get("promotion_scope")
    activation = claim.get("activation_watermark")
    end_item_id = claim.get("observation_end_item_id")
    if (
        scope not in {"entry", "management"}
        or not _is_nonnegative_plain_int(activation)
        or not _is_nonnegative_plain_int(end_item_id)
    ):
        return None
    try:
        observation_started_at = _aware_utc(
            datetime.fromisoformat(str(claim.get("observation_started_at")))
        )
        observation_ended_at = _aware_utc(
            datetime.fromisoformat(str(claim.get("observation_ended_at")))
        )
    except (TypeError, ValueError, DeploymentPreflightInputError):
        return None
    required_columns = {
        "message_instruction_items": {
            "id", "signal_candidate_id", "instruction_kind", "retired_at",
            "result_json", "error_json",
        },
        "signal_candidates": {"id", "parse_source"},
        "instruction_execution_contracts": {
            "id", "message_instruction_item_id", "state", "state_version",
            "terminal_kind", "completion_scope", "updated_at",
        },
        "trading_settings": {"key", "value_json"},
    }
    for table, columns in required_columns.items():
        present = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_safe_identifier(table)})"
            ).fetchall()
        }
        if not columns.issubset(present):
            return None
    settings_row = connection.execute(
        "SELECT value_json FROM trading_settings WHERE key = 'global'"
    ).fetchone()
    try:
        settings = json.loads(str(settings_row[0])) if settings_row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    watermark_key = (
        "instruction_execution_entry_after_item_id"
        if scope == "entry"
        else "instruction_execution_management_after_item_id"
    )
    configured_watermark = settings.get(watermark_key, 0)
    if (
        settings.get("instruction_execution_contract_mode") != "shadow"
        or not _is_nonnegative_plain_int(configured_watermark)
        or int(configured_watermark) != int(activation)
    ):
        return None
    rows = connection.execute(
        "SELECT i.result_json, i.error_json, i.updated_at, "
        "c.id, c.state, c.state_version, c.terminal_kind, "
        "c.completion_scope, c.updated_at "
        "FROM message_instruction_items AS i "
        "JOIN signal_candidates AS s ON s.id = i.signal_candidate_id "
        "LEFT JOIN instruction_execution_contracts AS c "
        "ON c.message_instruction_item_id = i.id "
        "WHERE i.instruction_kind = ? AND i.id > ? AND i.id <= ? "
        "AND i.retired_at IS NULL AND s.parse_source = 'mimo_authoritative'",
        (str(scope), int(activation), int(end_item_id)),
    ).fetchall()
    observed_count = 0
    divergence_count = 0
    for (
        result_json,
        error_json,
        item_updated_at,
        contract_id,
        contract_state,
        state_version,
        terminal_kind,
        completion_scope,
        contract_updated_at,
    ) in rows:
        if contract_id is None or str(contract_state) not in {
            "verified", "failed", "expired"
        }:
            continue
        try:
            item_time = _sqlite_utc_datetime(item_updated_at)
            contract_time = _sqlite_utc_datetime(contract_updated_at)
        except DeploymentPreflightInputError:
            continue
        if not (
            observation_started_at <= item_time <= observation_ended_at
            and observation_started_at <= contract_time <= observation_ended_at
        ):
            continue
        raw = result_json if result_json not in (None, "") else error_json
        try:
            item_result = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        mirror = (
            item_result.get("instruction_execution_contract")
            if isinstance(item_result, dict)
            else None
        )
        if isinstance(mirror, dict) and mirror.get("divergence") is True:
            divergence_count += 1
        if (
            not isinstance(mirror, dict)
            or mirror.get("contract_id") != int(contract_id)
            or mirror.get("state") != str(contract_state)
            or mirror.get("state_version") != int(state_version)
            or mirror.get("terminal_kind") != terminal_kind
            or mirror.get("completion_scope") != completion_scope
            or mirror.get("divergence") is not False
        ):
            continue
        observed_count += 1
    return {
        "eligible_contract_count": len(rows),
        "observed_contract_count": observed_count,
        "divergence_count": divergence_count,
    }


def _validate_shadow_evidence(
    path: str | Path | None,
    *,
    expected_commit: str | None,
    database_watermark: Mapping[str, int],
    observed: Mapping[str, int] | None,
    now: datetime,
) -> tuple[bool, str | None]:
    """Validate one bounded, self-authenticating reviewed shadow report."""

    if path is None:
        return False, None
    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size > _MAX_ARTIFACT_BYTES:
            return False, None
        payload = json.loads(source.read_text(encoding="utf-8"))
        required = {
            "schema_version",
            "kind",
            "reviewed_commit",
            "promotion_scope",
            "activation_watermark",
            "observation_end_item_id",
            "observation_started_at",
            "observation_ended_at",
            "eligible_contract_count",
            "observed_contract_count",
            "unexplained_divergence_count",
            "conclusion",
            "fingerprint",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            return False, None
        fingerprint = str(payload.get("fingerprint") or "").lower()
        unsigned = {key: value for key, value in payload.items() if key != "fingerprint"}
        if (
            payload.get("schema_version") != 1
            or payload.get("kind") != "instruction_execution_shadow_review"
            or not _SHADOW_EVIDENCE_RE.fullmatch(fingerprint)
            or not _constant_time_equal(fingerprint, _artifact_fingerprint(unsigned))
            or payload.get("reviewed_commit") != _validate_expected_commit(
                str(expected_commit or "")
            )
            or payload.get("conclusion") != "approved"
            or payload.get("promotion_scope") not in {"entry", "management"}
            or not _is_nonnegative_plain_int(payload.get("activation_watermark"))
            or not _is_nonnegative_plain_int(payload.get("observation_end_item_id"))
            or payload.get("observation_end_item_id")
            != database_watermark.get("instruction_item_max_id")
            or int(payload.get("activation_watermark", 0))
            >= int(payload.get("observation_end_item_id", 0))
            or not _is_nonnegative_plain_int(payload.get("eligible_contract_count"))
            or int(payload.get("eligible_contract_count", 0)) <= 0
            or not _is_nonnegative_plain_int(payload.get("observed_contract_count"))
            or not isinstance(payload.get("unexplained_divergence_count"), int)
            or isinstance(payload.get("unexplained_divergence_count"), bool)
            or int(payload.get("unexplained_divergence_count", -1)) != 0
            or observed is None
            or int(payload.get("eligible_contract_count", -1))
            != observed.get("eligible_contract_count")
            or int(payload.get("observed_contract_count", -1))
            != observed.get("observed_contract_count")
            or observed.get("observed_contract_count")
            != observed.get("eligible_contract_count")
            or observed.get("divergence_count") != 0
        ):
            return False, None
        started = _aware_utc(datetime.fromisoformat(str(payload["observation_started_at"])))
        ended = _aware_utc(datetime.fromisoformat(str(payload["observation_ended_at"])))
        if (
            ended - started < _MIN_SHADOW_OBSERVATION
            or ended > now + timedelta(seconds=30)
            or now - ended > _DEFAULT_SHADOW_EVIDENCE_MAX_AGE
        ):
            return False, None
        return True, fingerprint
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        DeploymentPreflightInputError,
    ):
        return False, None


def _exchange_order_id(value: Mapping[str, object]) -> str | None:
    rendered = str(value.get("ordId") or value.get("orderId") or "").strip()
    return rendered or None


def _canonical_exchange_safety_facts(
    *,
    positions: list[object],
    tpsl_orders: list[object],
    evidence_available: bool,
) -> dict[str, object]:
    """Exclude mark price/PnL/timestamps while preserving deployment safety facts."""

    position_keys = (
        "posId",
        "instId",
        "posSide",
        "pos",
        "slTriggerPx",
        "slTriggerPrice",
        "closeSLTriggerPrice",
    )
    order_keys = (
        "ordId",
        "orderId",
        "posId",
        "closePosId",
        "instId",
        "posSide",
        "triggerOrderType",
        "state",
        "status",
        "sz",
        "slTriggerPx",
        "slTriggerPrice",
        "closeSLTriggerPrice",
        "tpTriggerPx",
        "tpTriggerPrice",
        "closeTPTriggerPrice",
    )

    def select(rows: list[object], keys: tuple[str, ...]) -> list[str]:
        return sorted(
            _canonical_json(
                {key: row.get(key) for key in keys if key in row}
            )
            for row in rows
            if isinstance(row, dict)
        )

    return {
        "positions": select(positions, position_keys),
        "tpsl_orders": select(tpsl_orders, order_keys),
        "tpsl_evidence_available": evidence_available,
    }


def _position_side_identity(value: Mapping[str, object]) -> tuple[str, str] | None:
    instrument = str(value.get("instId") or "").strip().upper()
    side = str(value.get("posSide") or "").strip().lower()
    if not instrument or side not in {"long", "short", "net"}:
        return None
    return instrument, side


def _is_positive_decimal(value: object) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _validate_expected_commit(value: str) -> str:
    rendered = str(value or "").lower()
    if not _EXPECTED_COMMIT_RE.fullmatch(rendered):
        raise DeploymentPreflightInputError("expected_commit_invalid")
    return rendered


def _validate_change_class(value: str) -> str:
    rendered = str(value or "").strip().lower()
    if rendered not in DEPLOYMENT_CHANGE_CLASSES:
        raise DeploymentPreflightInputError("change_class_invalid")
    return rendered


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DeploymentPreflightInputError("preflight_time_invalid")
    return value.astimezone(UTC)


def _sqlite_utc_datetime(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DeploymentPreflightInputError("preflight_time_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _artifact_fingerprint(value: Mapping[str, object]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
    return sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DeploymentPreflightInputError("preflight_json_invalid") from exc


def _constant_time_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for first, second in zip(left.encode("ascii"), right.encode("ascii")):
        result |= first ^ second
    return result == 0


def _bounded_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeploymentPreflightInputError("preflight_count_invalid")
    if value > 1_000_000_000:
        raise DeploymentPreflightInputError("preflight_count_unbounded")
    return value


def _validate_sha256_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _SHADOW_EVIDENCE_RE.fullmatch(value):
        raise DeploymentPreflightInputError("deployment_evidence_malformed")
    return value


def _is_nonnegative_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise DeploymentPreflightInputError("database_schema_incomplete")
    return value


def _max_id(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX(id), 0) FROM {_safe_identifier(table)}"
    ).fetchone()
    return _bounded_count(int(row[0]))
