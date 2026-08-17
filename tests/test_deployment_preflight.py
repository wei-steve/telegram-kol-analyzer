from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path

import pytest

from telegram_kol_research.deployment_preflight import (
    ARTIFACT_VERSION,
    DeploymentPreflightInputError,
    build_final_deployment_preflight_artifact,
    build_preliminary_deployment_preflight_artifact,
    read_deployment_preflight_artifact,
    verify_deployment_preflight_artifact,
    write_deployment_preflight_artifact,
)
from telegram_kol_research.deployment_work_evidence import (
    DeploymentEvidenceCounts,
    DeploymentEvidenceSnapshot,
)
from telegram_kol_research.deployment_writer_surface import CandidateSurface


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
PRODUCTION = "a" * 40
CANDIDATE = "b" * 40


def _surface(*, writer_changed: bool = False, schema_changed: bool = False):
    production_fingerprint = "c" * 64
    candidate_fingerprint = "d" * 64 if writer_changed else production_fingerprint
    return CandidateSurface(
        manifest_version=1,
        production_writer_fingerprint=production_fingerprint,
        candidate_writer_fingerprint=candidate_fingerprint,
        writer_changed=writer_changed,
        schema_changed=schema_changed,
        changed_path_count=1,
    )


def _evidence(**counts: int) -> DeploymentEvidenceSnapshot:
    return DeploymentEvidenceSnapshot(
        counts=DeploymentEvidenceCounts(**counts),
        evidence_fingerprint="e" * 64,
        registered_adapter_count=8,
    )


def _snapshot(*, complete: bool = True, protected: int = 0) -> dict[str, object]:
    return {"complete": complete, "protected_live_positions": protected}


def _schema(*, verified: bool = False) -> dict[str, bool]:
    return {
        "backup_verified": verified,
        "migration_dry_run_verified": verified,
        "watermark_verified": verified,
    }


def _watermark(value: int = 10) -> dict[str, int]:
    return {"raw_messages": value, "execution_events": value}


def _preliminary(
    *,
    surface: CandidateSurface | None = None,
    evidence: DeploymentEvidenceSnapshot | None = None,
    snapshot: dict[str, object] | None = None,
    schema: dict[str, bool] | None = None,
    watermark: dict[str, int] | None = None,
    now: datetime = NOW,
) -> dict[str, object]:
    return build_preliminary_deployment_preflight_artifact(
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        surface=surface or _surface(),
        evidence=evidence or _evidence(),
        snapshot_status=snapshot or _snapshot(),
        schema_verification=schema or _schema(),
        database_watermark=watermark or _watermark(),
        now=now,
    )


def _verify(
    artifact: dict[str, object],
    *,
    phase: str,
    surface: CandidateSurface | None = None,
    evidence: DeploymentEvidenceSnapshot | None = None,
    snapshot: dict[str, object] | None = None,
    schema: dict[str, bool] | None = None,
    watermark: dict[str, int] | None = None,
    preliminary: dict[str, object] | None = None,
    now: datetime = NOW,
) -> str:
    return verify_deployment_preflight_artifact(
        artifact,
        expected_phase=phase,
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        surface=surface or _surface(),
        evidence=evidence or _evidence(),
        snapshot_status=snapshot or _snapshot(),
        schema_verification=schema or _schema(),
        database_watermark=watermark or _watermark(),
        preliminary_artifact=preliminary,
        now=now,
    )


def _rehash(artifact: dict[str, object]) -> None:
    payload = dict(artifact)
    payload.pop("fingerprint", None)
    artifact["fingerprint"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_preliminary_recomputes_block_from_evidence() -> None:
    evidence = _evidence(active_write=1)
    artifact = _preliminary(evidence=evidence)

    assert artifact["phase"] == "preliminary"
    assert artifact["decision"] == "BLOCK"
    assert artifact["reason_codes"] == ["active_exchange_write"]
    assert _verify(artifact, phase="preliminary", evidence=evidence) == "BLOCK"


def test_final_binds_one_direct_preliminary_parent() -> None:
    preliminary = _preliminary()
    final = build_final_deployment_preflight_artifact(
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        surface=_surface(),
        evidence=_evidence(),
        snapshot_status=_snapshot(),
        schema_verification=_schema(),
        database_watermark=_watermark(11),
        preliminary_artifact=preliminary,
        now=NOW + timedelta(seconds=30),
    )

    assert final["phase"] == "final"
    assert final["parent_fingerprint"] == preliminary["fingerprint"]
    assert _verify(
        final,
        phase="final",
        watermark=_watermark(11),
        preliminary=preliminary,
        now=NOW + timedelta(seconds=30),
    ) == "PASS"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda artifact: artifact.__setitem__("production_commit", "f" * 40),
        lambda artifact: artifact.__setitem__("candidate_commit", "f" * 40),
        lambda artifact: artifact.__setitem__("writer_changed", True),
        lambda artifact: artifact.__setitem__("schema_changed", True),
        lambda artifact: artifact.__setitem__("phase", "unknown"),
    ],
)
def test_verifier_rejects_expected_input_drift(mutation) -> None:
    artifact = _preliminary()
    mutation(artifact)
    _rehash(artifact)

    with pytest.raises(DeploymentPreflightInputError):
        _verify(artifact, phase="preliminary")


def test_final_rejects_wrong_parent_fingerprint() -> None:
    preliminary = _preliminary()
    final = build_final_deployment_preflight_artifact(
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        surface=_surface(),
        evidence=_evidence(),
        snapshot_status=_snapshot(),
        schema_verification=_schema(),
        database_watermark=_watermark(),
        preliminary_artifact=preliminary,
        now=NOW + timedelta(seconds=10),
    )
    final["parent_fingerprint"] = "f" * 64
    _rehash(final)

    with pytest.raises(DeploymentPreflightInputError, match="parent"):
        _verify(
            final,
            phase="final",
            preliminary=preliminary,
            now=NOW + timedelta(seconds=10),
        )


def test_final_rejects_watermark_rollback() -> None:
    preliminary = _preliminary(watermark=_watermark(10))

    with pytest.raises(DeploymentPreflightInputError, match="watermark"):
        build_final_deployment_preflight_artifact(
            production_commit=PRODUCTION,
            candidate_commit=CANDIDATE,
            surface=_surface(),
            evidence=_evidence(),
            snapshot_status=_snapshot(),
            schema_verification=_schema(),
            database_watermark=_watermark(9),
            preliminary_artifact=preliminary,
            now=NOW + timedelta(seconds=10),
        )


def test_decision_and_reasons_cannot_be_changed_then_rehashed() -> None:
    evidence = _evidence(unknown_outcome=1)
    artifact = _preliminary(evidence=evidence)
    artifact["decision"] = "PASS"
    artifact["reason_codes"] = []
    _rehash(artifact)

    with pytest.raises(DeploymentPreflightInputError, match="semantic"):
        _verify(artifact, phase="preliminary", evidence=evidence)


def test_checked_facts_cannot_be_changed_then_rehashed() -> None:
    artifact = _preliminary()
    artifact["evidence_counts"] = {
        "active_write": 0,
        "unknown_outcome": 0,
        "queued_work": 99,
        "inactive": 0,
        "invalid_evidence": 0,
    }
    _rehash(artifact)

    with pytest.raises(DeploymentPreflightInputError, match="facts"):
        _verify(artifact, phase="preliminary")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_version", 999),
        ("reason_codes", ["x" * 10_000]),
        ("checked_at", "not-a-time"),
        ("expires_at", "not-a-time"),
        ("fingerprint", "bad"),
    ],
)
def test_malformed_artifact_fields_fail_closed(field: str, value: object) -> None:
    artifact = _preliminary()
    artifact[field] = value
    if field != "fingerprint":
        _rehash(artifact)

    with pytest.raises(DeploymentPreflightInputError):
        _verify(artifact, phase="preliminary")


def test_unknown_artifact_key_fails_closed() -> None:
    artifact = _preliminary()
    artifact["operator_override"] = True
    _rehash(artifact)

    with pytest.raises(DeploymentPreflightInputError, match="shape"):
        _verify(artifact, phase="preliminary")


def test_artifact_expires_and_cannot_come_from_future() -> None:
    artifact = _preliminary()

    with pytest.raises(DeploymentPreflightInputError, match="expired"):
        _verify(artifact, phase="preliminary", now=NOW + timedelta(minutes=6))
    with pytest.raises(DeploymentPreflightInputError, match="future"):
        _verify(artifact, phase="preliminary", now=NOW - timedelta(seconds=1))


def test_preliminary_cannot_contain_parent_and_final_cannot_chain_final() -> None:
    preliminary = _preliminary()
    preliminary["parent_fingerprint"] = "f" * 64
    _rehash(preliminary)
    with pytest.raises(DeploymentPreflightInputError, match="shape"):
        _verify(preliminary, phase="preliminary")

    valid_preliminary = _preliminary()
    final_parent = dict(valid_preliminary)
    final_parent["phase"] = "final"
    final_parent["parent_fingerprint"] = "f" * 64
    _rehash(final_parent)
    with pytest.raises(DeploymentPreflightInputError, match="parent"):
        build_final_deployment_preflight_artifact(
            production_commit=PRODUCTION,
            candidate_commit=CANDIDATE,
            surface=_surface(),
            evidence=_evidence(),
            snapshot_status=_snapshot(),
            schema_verification=_schema(),
            database_watermark=_watermark(),
            preliminary_artifact=final_parent,
            now=NOW,
        )


def test_incomplete_snapshot_warns_unchanged_writer_and_blocks_changed_writer() -> None:
    unchanged = _preliminary(snapshot=_snapshot(complete=False))
    changed_surface = _surface(writer_changed=True)
    changed = _preliminary(
        surface=changed_surface,
        snapshot=_snapshot(complete=False),
    )

    assert unchanged["decision"] == "WARN"
    assert "exchange_snapshot_incomplete" in unchanged["reason_codes"]
    assert changed["decision"] == "BLOCK"
    assert "changed_writer_snapshot_incomplete" in changed["reason_codes"]


def test_protected_live_positions_are_warning_only() -> None:
    artifact = _preliminary(snapshot=_snapshot(protected=2))

    assert artifact["decision"] == "WARN"
    assert artifact["reason_codes"] == ["protected_live_positions"]


def test_schema_change_requires_all_backup_and_dry_run_proofs() -> None:
    surface = _surface(schema_changed=True)
    blocked = _preliminary(surface=surface, schema=_schema(verified=False))
    allowed = _preliminary(surface=surface, schema=_schema(verified=True))

    assert blocked["decision"] == "BLOCK"
    assert "schema_verification_incomplete" in blocked["reason_codes"]
    assert allowed["decision"] == "PASS"


def test_artifact_build_is_deterministic_for_same_inputs() -> None:
    assert _preliminary() == _preliminary()


def test_write_is_atomic_mode_0600_and_read_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    write_deployment_preflight_artifact(path, _preliminary())

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert read_deployment_preflight_artifact(path) == _preliminary()
    assert not list(tmp_path.glob("*.tmp"))

    path.write_bytes(b"{" + b"x" * 100_000 + b"}")
    with pytest.raises(DeploymentPreflightInputError, match="too_large"):
        read_deployment_preflight_artifact(path)


def test_artifact_version_is_v2() -> None:
    assert ARTIFACT_VERSION == 2
