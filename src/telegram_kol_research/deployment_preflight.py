"""Bounded two-phase artifacts for the simplified deployment safety gate."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from .deployment_work_evidence import (
    DeploymentEvidenceCounts,
    DeploymentEvidenceSnapshot,
    decide_deployment,
)
from .deployment_writer_surface import CandidateSurface


ARTIFACT_VERSION = 2
ARTIFACT_TTL_SECONDS = 300
MAX_ARTIFACT_BYTES = 65_536
MAX_WATERMARK_KEYS = 64
MAX_WATERMARK_VALUE = 10**15
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WATERMARK_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BASE_KEYS = frozenset(
    {
        "artifact_version",
        "phase",
        "production_commit",
        "candidate_commit",
        "writer_manifest_version",
        "production_writer_fingerprint",
        "candidate_writer_fingerprint",
        "writer_changed",
        "schema_changed",
        "evidence_counts",
        "evidence_fingerprint",
        "snapshot_status",
        "schema_verification",
        "database_watermark",
        "checked_at",
        "expires_at",
        "decision",
        "reason_codes",
        "fingerprint",
    }
)


class DeploymentPreflightInputError(ValueError):
    """The preflight inputs or artifact cannot authorize a deployment."""


def build_preliminary_deployment_preflight_artifact(
    *,
    production_commit: str,
    candidate_commit: str,
    surface: CandidateSurface,
    evidence: DeploymentEvidenceSnapshot,
    snapshot_status: Mapping[str, object],
    schema_verification: Mapping[str, object],
    database_watermark: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    return _build_artifact(
        phase="preliminary",
        production_commit=production_commit,
        candidate_commit=candidate_commit,
        surface=surface,
        evidence=evidence,
        snapshot_status=snapshot_status,
        schema_verification=schema_verification,
        database_watermark=database_watermark,
        now=now,
        parent_fingerprint=None,
    )


def build_final_deployment_preflight_artifact(
    *,
    production_commit: str,
    candidate_commit: str,
    surface: CandidateSurface,
    evidence: DeploymentEvidenceSnapshot,
    snapshot_status: Mapping[str, object],
    schema_verification: Mapping[str, object],
    database_watermark: Mapping[str, object],
    preliminary_artifact: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    normalized_now = _aware_utc(now)
    production = _commit(production_commit)
    candidate = _commit(candidate_commit)
    normalized_surface = _surface_facts(surface)
    parent = _validate_parent(
        preliminary_artifact,
        production_commit=production,
        candidate_commit=candidate,
        surface=normalized_surface,
        now=normalized_now,
    )
    watermark = _watermark(database_watermark)
    _require_nondecreasing_watermark(parent["database_watermark"], watermark)
    return _build_artifact(
        phase="final",
        production_commit=production,
        candidate_commit=candidate,
        surface=surface,
        evidence=evidence,
        snapshot_status=snapshot_status,
        schema_verification=schema_verification,
        database_watermark=watermark,
        now=normalized_now,
        parent_fingerprint=str(parent["fingerprint"]),
    )


def verify_deployment_preflight_artifact(
    artifact: Mapping[str, object],
    *,
    expected_phase: str,
    production_commit: str,
    candidate_commit: str,
    surface: CandidateSurface,
    evidence: DeploymentEvidenceSnapshot,
    snapshot_status: Mapping[str, object],
    schema_verification: Mapping[str, object],
    database_watermark: Mapping[str, object],
    preliminary_artifact: Mapping[str, object] | None = None,
    now: datetime,
) -> str:
    phase = _phase(expected_phase)
    normalized = _validate_artifact_shape(artifact, phase=phase)
    _validate_fingerprint(normalized)
    _validate_times(normalized, now=_aware_utc(now))

    expected_surface = _surface_facts(surface)
    expected_facts = {
        "production_commit": _commit(production_commit),
        "candidate_commit": _commit(candidate_commit),
        **expected_surface,
        "evidence_counts": _evidence_counts(evidence),
        "evidence_fingerprint": _hash(evidence.evidence_fingerprint, "evidence"),
        "snapshot_status": _snapshot_status(snapshot_status),
        "schema_verification": _schema_verification(schema_verification),
        "database_watermark": _watermark(database_watermark),
    }
    for key, value in expected_facts.items():
        if normalized.get(key) != value:
            raise DeploymentPreflightInputError("preflight_artifact_facts_mismatch")

    decision, reasons = _decision(
        counts=DeploymentEvidenceCounts(**expected_facts["evidence_counts"]),
        writer_changed=bool(expected_facts["writer_changed"]),
        schema_changed=bool(expected_facts["schema_changed"]),
        snapshot_status=expected_facts["snapshot_status"],
        schema_verification=expected_facts["schema_verification"],
    )
    if normalized["decision"] != decision or normalized["reason_codes"] != reasons:
        raise DeploymentPreflightInputError("preflight_artifact_semantic_mismatch")

    if phase == "final":
        if preliminary_artifact is None:
            raise DeploymentPreflightInputError("preflight_artifact_parent_missing")
        parent = _validate_parent(
            preliminary_artifact,
            production_commit=str(expected_facts["production_commit"]),
            candidate_commit=str(expected_facts["candidate_commit"]),
            surface=expected_surface,
            now=_aware_utc(now),
        )
        if normalized["parent_fingerprint"] != parent["fingerprint"]:
            raise DeploymentPreflightInputError("preflight_artifact_parent_mismatch")
        _require_nondecreasing_watermark(
            parent["database_watermark"],
            normalized["database_watermark"],
        )
    elif preliminary_artifact is not None:
        raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
    return decision


def read_deployment_preflight_artifact(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            payload = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise DeploymentPreflightInputError("preflight_artifact_unreadable") from exc
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentPreflightInputError("preflight_artifact_json_invalid") from exc
    if not isinstance(value, dict):
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    return value


def write_deployment_preflight_artifact(
    path: str | Path,
    artifact: Mapping[str, object],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(dict(artifact)) + b"\n"
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _build_artifact(
    *,
    phase: str,
    production_commit: str,
    candidate_commit: str,
    surface: CandidateSurface,
    evidence: DeploymentEvidenceSnapshot,
    snapshot_status: Mapping[str, object],
    schema_verification: Mapping[str, object],
    database_watermark: Mapping[str, object],
    now: datetime,
    parent_fingerprint: str | None,
) -> dict[str, object]:
    normalized_phase = _phase(phase)
    checked_at = _aware_utc(now)
    surface_facts = _surface_facts(surface)
    counts = _evidence_counts(evidence)
    snapshot = _snapshot_status(snapshot_status)
    schema = _schema_verification(schema_verification)
    decision, reasons = _decision(
        counts=DeploymentEvidenceCounts(**counts),
        writer_changed=bool(surface_facts["writer_changed"]),
        schema_changed=bool(surface_facts["schema_changed"]),
        snapshot_status=snapshot,
        schema_verification=schema,
    )
    artifact: dict[str, object] = {
        "artifact_version": ARTIFACT_VERSION,
        "phase": normalized_phase,
        "production_commit": _commit(production_commit),
        "candidate_commit": _commit(candidate_commit),
        **surface_facts,
        "evidence_counts": counts,
        "evidence_fingerprint": _hash(evidence.evidence_fingerprint, "evidence"),
        "snapshot_status": snapshot,
        "schema_verification": schema,
        "database_watermark": _watermark(database_watermark),
        "checked_at": _iso(checked_at),
        "expires_at": _iso(checked_at + timedelta(seconds=ARTIFACT_TTL_SECONDS)),
        "decision": decision,
        "reason_codes": reasons,
    }
    if normalized_phase == "final":
        artifact["parent_fingerprint"] = _hash(parent_fingerprint, "parent")
    elif parent_fingerprint is not None:
        raise DeploymentPreflightInputError("preflight_artifact_parent_invalid")
    artifact["fingerprint"] = _fingerprint(artifact)
    if len(_canonical(artifact)) > MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    return artifact


def _validate_parent(
    artifact: Mapping[str, object],
    *,
    production_commit: str,
    candidate_commit: str,
    surface: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    try:
        parent = _validate_artifact_shape(artifact, phase="preliminary")
    except DeploymentPreflightInputError as exc:
        raise DeploymentPreflightInputError(
            "preflight_artifact_parent_invalid"
        ) from exc
    _validate_fingerprint(parent)
    _validate_times(parent, now=now)
    expected = {
        "production_commit": production_commit,
        "candidate_commit": candidate_commit,
        **surface,
    }
    if any(parent.get(key) != value for key, value in expected.items()):
        raise DeploymentPreflightInputError("preflight_artifact_parent_surface_mismatch")
    counts = DeploymentEvidenceCounts(**_counts_mapping(parent["evidence_counts"]))
    decision, reasons = _decision(
        counts=counts,
        writer_changed=bool(parent["writer_changed"]),
        schema_changed=bool(parent["schema_changed"]),
        snapshot_status=_snapshot_status(parent["snapshot_status"]),
        schema_verification=_schema_verification(parent["schema_verification"]),
    )
    if parent["decision"] != decision or parent["reason_codes"] != reasons:
        raise DeploymentPreflightInputError("preflight_artifact_parent_semantic_invalid")
    parent["database_watermark"] = _watermark(parent["database_watermark"])
    return parent


def _validate_artifact_shape(
    artifact: Mapping[str, object],
    *,
    phase: str,
) -> dict[str, object]:
    if not isinstance(artifact, Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    normalized = dict(artifact)
    expected_keys = _BASE_KEYS | ({"parent_fingerprint"} if phase == "final" else set())
    if set(normalized) != expected_keys:
        raise DeploymentPreflightInputError("preflight_artifact_shape_invalid")
    if normalized.get("artifact_version") != ARTIFACT_VERSION:
        raise DeploymentPreflightInputError("preflight_artifact_version_invalid")
    if normalized.get("phase") != phase:
        raise DeploymentPreflightInputError("preflight_artifact_phase_invalid")
    if len(_canonical(normalized)) > MAX_ARTIFACT_BYTES:
        raise DeploymentPreflightInputError("preflight_artifact_too_large")
    if normalized.get("decision") not in {"PASS", "WARN", "BLOCK"}:
        raise DeploymentPreflightInputError("preflight_artifact_decision_invalid")
    reasons = normalized.get("reason_codes")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 16
        or any(
            not isinstance(reason, str)
            or not reason
            or len(reason) > 128
            or re.fullmatch(r"[a-z0-9_]+", reason) is None
            for reason in reasons
        )
    ):
        raise DeploymentPreflightInputError("preflight_artifact_reasons_invalid")
    _counts_mapping(normalized.get("evidence_counts"))
    _snapshot_status(normalized.get("snapshot_status"))
    _schema_verification(normalized.get("schema_verification"))
    _watermark(normalized.get("database_watermark"))
    _hash(normalized.get("evidence_fingerprint"), "evidence")
    _hash(normalized.get("fingerprint"), "fingerprint")
    if phase == "final":
        _hash(normalized.get("parent_fingerprint"), "parent")
    return normalized


def _decision(
    *,
    counts: DeploymentEvidenceCounts,
    writer_changed: bool,
    schema_changed: bool,
    snapshot_status: Mapping[str, object],
    schema_verification: Mapping[str, object],
) -> tuple[str, list[str]]:
    base = decide_deployment(counts=counts, writer_changed=writer_changed)
    blockers = list(base.reason_codes) if base.decision == "BLOCK" else []
    warnings = list(base.reason_codes) if base.decision == "WARN" else []
    if schema_changed and not all(schema_verification.values()):
        blockers.append("schema_verification_incomplete")
    if not snapshot_status["complete"]:
        if writer_changed:
            blockers.append("changed_writer_snapshot_incomplete")
        else:
            warnings.append("exchange_snapshot_incomplete")
    if snapshot_status["protected_live_positions"]:
        warnings.append("protected_live_positions")
    if blockers:
        return "BLOCK", blockers
    if warnings:
        return "WARN", warnings
    return "PASS", []


def _surface_facts(surface: CandidateSurface) -> dict[str, object]:
    if not isinstance(surface, CandidateSurface):
        raise DeploymentPreflightInputError("preflight_surface_invalid")
    if (
        not isinstance(surface.manifest_version, int)
        or isinstance(surface.manifest_version, bool)
        or surface.manifest_version < 1
    ):
        raise DeploymentPreflightInputError("preflight_surface_invalid")
    production = _hash(surface.production_writer_fingerprint, "writer")
    candidate = _hash(surface.candidate_writer_fingerprint, "writer")
    if not isinstance(surface.writer_changed, bool) or surface.writer_changed != (
        production != candidate
    ):
        raise DeploymentPreflightInputError("preflight_surface_invalid")
    if not isinstance(surface.schema_changed, bool):
        raise DeploymentPreflightInputError("preflight_surface_invalid")
    return {
        "writer_manifest_version": surface.manifest_version,
        "production_writer_fingerprint": production,
        "candidate_writer_fingerprint": candidate,
        "writer_changed": surface.writer_changed,
        "schema_changed": surface.schema_changed,
    }


def _evidence_counts(evidence: DeploymentEvidenceSnapshot) -> dict[str, int]:
    if not isinstance(evidence, DeploymentEvidenceSnapshot):
        raise DeploymentPreflightInputError("preflight_evidence_invalid")
    return _counts_mapping(asdict(evidence.counts))


def _counts_mapping(value: object) -> dict[str, int]:
    expected = {
        "active_write",
        "unknown_outcome",
        "queued_work",
        "inactive",
        "invalid_evidence",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DeploymentPreflightInputError("preflight_evidence_invalid")
    normalized = dict(value)
    try:
        DeploymentEvidenceCounts(**normalized)
    except (TypeError, ValueError) as exc:
        raise DeploymentPreflightInputError("preflight_evidence_invalid") from exc
    return normalized


def _snapshot_status(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "complete",
        "protected_live_positions",
    }:
        raise DeploymentPreflightInputError("preflight_snapshot_invalid")
    complete = value.get("complete")
    protected = value.get("protected_live_positions")
    if (
        not isinstance(complete, bool)
        or not isinstance(protected, int)
        or isinstance(protected, bool)
        or protected < 0
        or protected > 1_000_000
    ):
        raise DeploymentPreflightInputError("preflight_snapshot_invalid")
    return {"complete": complete, "protected_live_positions": protected}


def _schema_verification(value: object) -> dict[str, bool]:
    keys = {
        "backup_verified",
        "migration_dry_run_verified",
        "watermark_verified",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DeploymentPreflightInputError("preflight_schema_invalid")
    if any(not isinstance(value.get(key), bool) for key in keys):
        raise DeploymentPreflightInputError("preflight_schema_invalid")
    return {key: bool(value[key]) for key in sorted(keys)}


def _watermark(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= MAX_WATERMARK_KEYS:
        raise DeploymentPreflightInputError("preflight_watermark_invalid")
    normalized: dict[str, int] = {}
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or _WATERMARK_KEY.fullmatch(key) is None
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= MAX_WATERMARK_VALUE
        ):
            raise DeploymentPreflightInputError("preflight_watermark_invalid")
        normalized[key] = count
    return dict(sorted(normalized.items()))


def _require_nondecreasing_watermark(
    parent: object,
    current: object,
) -> None:
    left = _watermark(parent)
    right = _watermark(current)
    if set(left) != set(right) or any(right[key] < left[key] for key in left):
        raise DeploymentPreflightInputError("preflight_watermark_rollback")


def _validate_fingerprint(artifact: Mapping[str, object]) -> None:
    fingerprint = _hash(artifact.get("fingerprint"), "fingerprint")
    payload = dict(artifact)
    payload.pop("fingerprint", None)
    if fingerprint != _fingerprint(payload):
        raise DeploymentPreflightInputError("preflight_artifact_fingerprint_mismatch")


def _validate_times(artifact: Mapping[str, object], *, now: datetime) -> None:
    checked_at = _time(artifact.get("checked_at"))
    expires_at = _time(artifact.get("expires_at"))
    if expires_at <= checked_at or expires_at - checked_at != timedelta(
        seconds=ARTIFACT_TTL_SECONDS
    ):
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid")
    if checked_at > now:
        raise DeploymentPreflightInputError("preflight_artifact_from_future")
    if expires_at < now:
        raise DeploymentPreflightInputError("preflight_artifact_expired")


def _phase(value: object) -> str:
    rendered = str(value)
    if rendered not in {"preliminary", "final"}:
        raise DeploymentPreflightInputError("preflight_artifact_phase_invalid")
    return rendered


def _commit(value: object) -> str:
    rendered = str(value).strip().lower()
    if _SHA40.fullmatch(rendered) is None:
        raise DeploymentPreflightInputError("preflight_commit_invalid")
    return rendered


def _hash(value: object, label: str) -> str:
    rendered = str(value or "").strip().lower()
    if _SHA256.fullmatch(rendered) is None:
        raise DeploymentPreflightInputError(f"preflight_{label}_fingerprint_invalid")
    return rendered


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DeploymentPreflightInputError("preflight_time_invalid")
    return value.astimezone(UTC)


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid") from exc
    if parsed.tzinfo is None:
        raise DeploymentPreflightInputError("preflight_artifact_time_invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _fingerprint(value: Mapping[str, object]) -> str:
    return sha256(_canonical(dict(value))).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise DeploymentPreflightInputError("preflight_artifact_json_invalid") from exc


# The legacy Typer commands remain importable until the runbook cutover, but
# cannot authorize deployment. The reviewed updater uses the standalone v2 CLI.
def collect_deployment_preflight_facts(*args, **kwargs):
    raise DeploymentPreflightInputError("legacy_preflight_interface_retired")


def build_deployment_preflight_artifact(*args, **kwargs):
    raise DeploymentPreflightInputError("legacy_preflight_interface_retired")
