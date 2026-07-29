"""Bounded one-shot proof for a read-only production management audit."""

from __future__ import annotations

import re
import json
import sys
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from telegram_kol_research.production_safety_monitor import (
    _run_bounded_command,
)


_MAX_EPHEMERAL_CAPTURES = 32
_MAX_COUNT = 1_000_000
_MAX_COMMAND_OUTPUT_BYTES = 1_048_576
_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_COUNT_FIELDS = (
    "batches_total",
    "blocked",
    "submit_unknown",
    "partial_failed",
    "recovery_required",
)


class RuntimeAgentProductionAuditError(ValueError):
    """The read-only production audit proof is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class _AuditProof:
    snapshot_status: str
    snapshot_validation: str
    schema_status: str
    output_complete: bool
    batches_truncated: bool
    malformed_row_count: int
    malformed_field_count: int
    legacy_complete: bool
    counts: dict[str, int]

    @property
    def complete(self) -> bool:
        return (
            self.snapshot_status == "stable"
            and self.snapshot_validation == "ok"
            and self.schema_status == "available"
            and self.output_complete is True
            and self.batches_truncated is False
            and self.malformed_row_count == 0
            and self.malformed_field_count == 0
            and self.legacy_complete is True
        )


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeAgentProductionAuditError(
            "production audit status is invalid"
        )
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise RuntimeAgentProductionAuditError(
            "production audit status is invalid"
        )
    return normalized


def _bounded_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_COUNT
    ):
        raise RuntimeAgentProductionAuditError(
            "production audit count is invalid"
        )
    return value


def _project_audit(value: Any) -> _AuditProof:
    if not isinstance(value, Mapping):
        raise RuntimeAgentProductionAuditError(
            "production audit result must be an object"
        )
    counts = value.get("counts")
    if not isinstance(counts, Mapping):
        raise RuntimeAgentProductionAuditError(
            "production audit counts must be an object"
        )
    output_complete = value.get("output_complete")
    if not isinstance(output_complete, bool):
        raise RuntimeAgentProductionAuditError(
            "production audit completeness is invalid"
        )
    batches_truncated = value.get("batches_truncated")
    if not isinstance(batches_truncated, bool):
        raise RuntimeAgentProductionAuditError(
            "production audit truncation state is invalid"
        )
    legacy = value.get("legacy_pending_management")
    if not isinstance(legacy, Mapping):
        raise RuntimeAgentProductionAuditError(
            "production audit legacy state is invalid"
        )
    legacy_flags = tuple(
        legacy.get(name)
        for name in ("complete", "truncated", "scan_truncated")
    )
    if any(not isinstance(flag, bool) for flag in legacy_flags):
        raise RuntimeAgentProductionAuditError(
            "production audit legacy state is invalid"
        )
    return _AuditProof(
        snapshot_status=_bounded_text(value.get("snapshot_status")),
        snapshot_validation=_bounded_text(
            value.get("snapshot_validation")
        ),
        schema_status=_bounded_text(value.get("schema_status")),
        output_complete=output_complete,
        batches_truncated=batches_truncated,
        malformed_row_count=_bounded_count(
            value.get("malformed_row_count")
        ),
        malformed_field_count=_bounded_count(
            value.get("malformed_field_count")
        ),
        legacy_complete=(
            legacy_flags[0] is True
            and legacy_flags[1] is False
            and legacy_flags[2] is False
        ),
        counts={
            name: _bounded_count(counts.get(name))
            for name in _COUNT_FIELDS
        },
    )


def _proof_payload(proof: _AuditProof) -> dict[str, Any]:
    return {
        "snapshot_status": proof.snapshot_status,
        "snapshot_validation": proof.snapshot_validation,
        "schema_status": proof.schema_status,
        "output_complete": proof.output_complete,
        "batches_truncated": proof.batches_truncated,
        "malformed_row_count": proof.malformed_row_count,
        "malformed_field_count": proof.malformed_field_count,
        "legacy_pending_management": {
            "complete": proof.legacy_complete,
            "truncated": False if proof.legacy_complete else True,
            "scan_truncated": False,
        },
        "counts": dict(proof.counts),
    }


def project_bounded_production_audit(value: Any) -> dict[str, Any]:
    """Reduce an audit result to the fixed proof accepted by the sidecar."""

    return _proof_payload(_project_audit(value))


def run_bounded_production_audit_command(
    database_path: str | Path,
    *,
    timeout_seconds: float = 20.0,
    audit_command: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run the exact read-only audit in a killable, output-bounded process."""

    if not 0.05 <= float(timeout_seconds) <= 20.0:
        raise ValueError("production audit timeout is invalid")
    command = audit_command or (
        sys.executable,
        "-m",
        "telegram_kol_research.cli",
        "audit-management-batches",
        "--database-path",
        str(Path(database_path)),
        "--limit",
        "100",
        "--output-format",
        "json",
    )
    try:
        completed = _run_bounded_command(
            command,
            timeout_seconds=float(timeout_seconds),
            max_output_bytes=_MAX_COMMAND_OUTPUT_BYTES,
        )
        if completed.returncode != 0:
            raise RuntimeError("audit command failed")
        value = json.loads(completed.output)
        return project_bounded_production_audit(value)
    except Exception as exc:
        raise RuntimeAgentProductionAuditError(
            "production audit unavailable"
        ) from exc


class RuntimeAgentProductionAuditRefresh:
    """Run one read-only audit and expose its bounded proof exactly once."""

    def __init__(self, *, runner: Callable[[], Mapping[str, Any]]) -> None:
        self._runner = runner
        self._captures: OrderedDict[int, _AuditProof] = OrderedDict()
        self._lock = Lock()

    def has_capture(self, incident_id: int) -> bool:
        with self._lock:
            return int(incident_id) in self._captures

    def rerun(
        self,
        *,
        incident_id: int,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> bool:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id < 1
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 255
            or not isinstance(expected_fingerprint, str)
            or not _FINGERPRINT_PATTERN.fullmatch(expected_fingerprint)
        ):
            raise RuntimeAgentProductionAuditError(
                "production audit identity is invalid"
            )
        try:
            proof = _project_audit(self._runner())
        except RuntimeAgentProductionAuditError:
            raise
        except Exception as exc:
            raise RuntimeAgentProductionAuditError(
                "production audit unavailable"
            ) from exc
        with self._lock:
            self._captures[incident_id] = proof
            self._captures.move_to_end(incident_id)
            while len(self._captures) > _MAX_EPHEMERAL_CAPTURES:
                self._captures.popitem(last=False)
        return True

    def consume_verification(
        self,
        *,
        incident_id: int,
    ) -> dict[str, Any] | None:
        with self._lock:
            proof = self._captures.pop(int(incident_id), None)
        if proof is None:
            return None
        return {
            "available": True,
            "audit_run_completed": True,
            "complete": proof.complete,
            "monitor_error": None if proof.complete else "audit_incomplete",
            "snapshot_status": proof.snapshot_status,
            "snapshot_validation": proof.snapshot_validation,
            "schema_status": proof.schema_status,
            "output_complete": proof.output_complete,
            "batches_truncated": proof.batches_truncated,
            "malformed_row_count": proof.malformed_row_count,
            "malformed_field_count": proof.malformed_field_count,
            "legacy_complete": proof.legacy_complete,
            "counts": dict(proof.counts),
        }
