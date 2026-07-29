"""Bounded one-shot proof for a read-only production management audit."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any


_MAX_EPHEMERAL_CAPTURES = 32
_MAX_COUNT = 1_000_000
_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_COUNT_FIELDS = (
    "batches_total",
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
    output_complete: bool
    malformed_row_count: int
    counts: dict[str, int]

    @property
    def complete(self) -> bool:
        return (
            self.snapshot_status == "stable"
            and self.snapshot_validation == "ok"
            and self.output_complete is True
            and self.malformed_row_count == 0
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
    return _AuditProof(
        snapshot_status=_bounded_text(value.get("snapshot_status")),
        snapshot_validation=_bounded_text(
            value.get("snapshot_validation")
        ),
        output_complete=output_complete,
        malformed_row_count=_bounded_count(
            value.get("malformed_row_count")
        ),
        counts={
            name: _bounded_count(counts.get(name))
            for name in _COUNT_FIELDS
        },
    )


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
            "output_complete": proof.output_complete,
            "malformed_row_count": proof.malformed_row_count,
            "counts": dict(proof.counts),
        }

