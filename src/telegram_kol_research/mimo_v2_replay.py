"""Isolated MiMo v1/v2 replay with redacted comparison artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import select

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_instructions import (
    AuthoritativeInstruction,
    AuthoritativeInstructionError,
    normalize_authoritative_instructions,
)
from telegram_kol_research.db import create_existing_session_factory
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import RawMessage
from telegram_kol_research.recognition_experiments import (
    infer_mimo_authoritative_v2,
    run_mimo_authoritative_for_message,
)


MAX_REPLAY_MESSAGES = 200
MAX_CANONICAL_V2_RESPONSE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    raw_message_id: int
    classification: str
    v1_status: str
    v2_status: str
    v1_duration_ms: float
    v2_duration_ms: float
    adapter_duration_ms: float
    canonical_v2_response_bytes: int
    v1_projection_fingerprint: str | None
    v2_projection_fingerprint: str | None
    v1_evidence_fingerprint: str | None
    v2_evidence_fingerprint: str | None
    evidence_difference_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayPerformance:
    v1_p95_ms: float
    v2_p95_ms: float
    adapter_p95_ms: float
    passed: bool
    failure_reasons: tuple[str, ...]
    canonical_v2_response_max_bytes: int = 0


@dataclass(frozen=True, slots=True)
class MimoV2ReplayResult:
    processed: int
    comparisons: tuple[ReplayComparison, ...]
    unsafe_mismatches: int
    production_writes: int
    notifications_sent: int
    performance: ReplayPerformance
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "comparisons": [asdict(row) for row in self.comparisons],
            "unsafe_mismatches": self.unsafe_mismatches,
            "production_writes": self.production_writes,
            "notifications_sent": self.notifications_sent,
            "performance": asdict(self.performance),
            "passed": self.passed,
        }


def run_mimo_v2_replay(
    *,
    source_database: str | Path,
    artifact_dir: str | Path,
    raw_message_ids: Sequence[int],
    max_messages: int = MAX_REPLAY_MESSAGES,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    media_root: str | Path = "data/media",
    v1_runner: Callable[..., Any] = run_mimo_authoritative_for_message,
    v2_runner: Callable[..., Any] = infer_mimo_authoritative_v2,
) -> MimoV2ReplayResult:
    """Compare bounded messages in a temporary copy of the source database."""

    source_path = Path(source_database).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("source_database must be an existing SQLite file")
    selected_ids = _validated_message_ids(
        raw_message_ids,
        max_messages=max_messages,
    )
    output_dir = _prepare_artifact_dir(artifact_dir)

    comparisons: list[ReplayComparison] = []
    with tempfile.TemporaryDirectory(prefix="mimo-v2-replay-") as temp_root:
        isolated_database = Path(temp_root) / "isolated-replay.db"
        _copy_sqlite_database_read_only(source_path, isolated_database)
        session_factory = create_existing_session_factory(isolated_database)
        _require_messages(session_factory, selected_ids)

        for raw_message_id in selected_ids:
            v1_started = time.perf_counter()
            v1 = v1_runner(
                session_factory=session_factory,
                raw_message_id=raw_message_id,
                ai_recognition_config=ai_recognition_config,
                ai_recognition_config_path=ai_recognition_config_path,
                media_root=media_root,
            )
            measured_v1_ms = (time.perf_counter() - v1_started) * 1000
            v1_duration_ms = _reported_duration(v1, measured_v1_ms)

            v2_started = time.perf_counter()
            v2 = v2_runner(
                session_factory=session_factory,
                raw_message_id=raw_message_id,
                config=ai_recognition_config,
                ai_recognition_config_path=ai_recognition_config_path,
                media_root=media_root,
            )
            measured_v2_ms = (time.perf_counter() - v2_started) * 1000
            v2_duration_ms = _reported_duration(v2, measured_v2_ms)

            comparison = _compare_results(
                raw_message_id=raw_message_id,
                v1=v1,
                v2=v2,
                v1_duration_ms=v1_duration_ms,
                v2_duration_ms=v2_duration_ms,
            )
            comparisons.append(comparison)

    performance = evaluate_replay_performance(
        v1_duration_ms=[row.v1_duration_ms for row in comparisons],
        v2_duration_ms=[row.v2_duration_ms for row in comparisons],
        adapter_duration_ms=[row.adapter_duration_ms for row in comparisons],
        canonical_v2_response_bytes=[
            row.canonical_v2_response_bytes for row in comparisons
        ],
    )
    unsafe_mismatches = sum(
        row.classification in {"unsafe_mismatch", "unsafe_evidence_mismatch"}
        for row in comparisons
    )
    has_terminal_failure = any(
        row.classification in {"v1_failed", "v2_failed", "both_failed"}
        for row in comparisons
    )
    result = MimoV2ReplayResult(
        processed=len(comparisons),
        comparisons=tuple(comparisons),
        unsafe_mismatches=unsafe_mismatches,
        production_writes=0,
        notifications_sent=0,
        performance=performance,
        passed=(
            unsafe_mismatches == 0
            and not has_terminal_failure
            and performance.passed
        ),
    )
    _write_artifacts(output_dir, result)
    return result


def evaluate_replay_performance(
    *,
    v1_duration_ms: Sequence[float],
    v2_duration_ms: Sequence[float],
    adapter_duration_ms: Sequence[float],
    canonical_v2_response_bytes: Sequence[int] = (),
) -> ReplayPerformance:
    """Apply the approved adapter and end-to-end P95 gates."""

    v1_p95 = _percentile_95(v1_duration_ms)
    v2_p95 = _percentile_95(v2_duration_ms)
    adapter_p95 = _percentile_95(adapter_duration_ms)
    response_max = max(
        (int(value) for value in canonical_v2_response_bytes),
        default=0,
    )
    reasons: list[str] = []
    if adapter_p95 >= 50.0:
        reasons.append("adapter_p95_at_or_above_50ms")
    if v1_p95 <= 0.0 or v2_p95 > v1_p95 * 1.15:
        reasons.append("v2_p95_above_115_percent_of_v1")
    if response_max > MAX_CANONICAL_V2_RESPONSE_BYTES:
        reasons.append("canonical_v2_response_size_exceeded")
    return ReplayPerformance(
        v1_p95_ms=round(v1_p95, 3),
        v2_p95_ms=round(v2_p95, 3),
        adapter_p95_ms=round(adapter_p95, 3),
        passed=not reasons,
        failure_reasons=tuple(reasons),
        canonical_v2_response_max_bytes=response_max,
    )


def load_replay_message_ids(path: str | Path) -> list[int]:
    """Load one positive raw-message database ID per non-comment line."""

    rows: list[int] = []
    for line_number, raw_line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(
                f"message_id_file line {line_number} is not an integer"
            ) from exc
        if parsed <= 0:
            raise ValueError(
                f"message_id_file line {line_number} must be positive"
            )
        rows.append(parsed)
    if not rows:
        raise ValueError("message_id_file must contain at least one ID")
    return rows


def _validated_message_ids(
    raw_message_ids: Sequence[int],
    *,
    max_messages: int,
) -> list[int]:
    if (
        isinstance(max_messages, bool)
        or not isinstance(max_messages, int)
        or not 1 <= max_messages <= MAX_REPLAY_MESSAGES
    ):
        raise ValueError(f"max_messages must be between 1 and {MAX_REPLAY_MESSAGES}")
    rows: list[int] = []
    seen: set[int] = set()
    for value in raw_message_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("raw_message_ids must contain positive integers")
        if value not in seen:
            seen.add(value)
            rows.append(value)
    if not rows:
        raise ValueError("raw_message_ids must not be empty")
    if len(rows) > int(max_messages):
        raise ValueError("raw_message_ids exceed max_messages")
    return rows


def _prepare_artifact_dir(path: str | Path) -> Path:
    output = Path(path)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError("artifact_dir must be new or empty")
    else:
        output.mkdir(parents=True)
    return output


def _copy_sqlite_database_read_only(source: Path, destination: Path) -> None:
    uri = f"{source.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as source_connection:
        source_connection.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _require_messages(session_factory, raw_message_ids: Sequence[int]) -> None:
    with session_factory() as session:
        found = set(
            session.scalars(
                select(RawMessage.id).where(RawMessage.id.in_(raw_message_ids))
            ).all()
        )
    missing = [value for value in raw_message_ids if value not in found]
    if missing:
        raise LookupError(f"raw messages not found: {missing}")


def _compare_results(
    *,
    raw_message_id: int,
    v1: Any,
    v2: Any,
    v1_duration_ms: float,
    v2_duration_ms: float,
) -> ReplayComparison:
    v1_projection = _safe_v1_projection(v1)
    v2_projection = _safe_v2_projection(v2)
    v1_payload = getattr(v1, "payload", None)
    adapted = getattr(v2, "adapted_result", None)
    v2_payload = getattr(adapted, "payload", None)
    v1_evidence = _safe_evidence_projection(v1_payload)
    v2_evidence = _safe_evidence_projection(v2_payload)
    evidence_difference_codes = _evidence_difference_codes(
        v1_evidence,
        v2_evidence,
    )
    adapter_duration_ms = 0.0
    parsed = getattr(v2, "parsed_result", None)
    if parsed is not None:
        started = time.perf_counter()
        adapt_mimo_v2_to_current_payload(parsed)
        adapter_duration_ms = (time.perf_counter() - started) * 1000

    v1_failed = v1_projection is None
    v2_failed = v2_projection is None
    if v1_failed and v2_failed:
        classification = "both_failed"
    elif v1_failed:
        classification = "v1_failed"
    elif v2_failed:
        classification = "v2_failed"
    elif evidence_difference_codes:
        classification = "unsafe_evidence_mismatch"
    elif v1_projection == v2_projection:
        classification = "match"
    elif not _has_executable_semantics(v1_projection) and not _has_executable_semantics(
        v2_projection
    ):
        classification = "reviewable_nonexecuting_difference"
    else:
        classification = "unsafe_mismatch"

    return ReplayComparison(
        raw_message_id=raw_message_id,
        classification=classification,
        v1_status="failed" if v1_failed else "completed",
        v2_status=(
            str(getattr(v2, "error_code", None) or "failed")
            if v2_failed
            else "completed"
        ),
        v1_duration_ms=round(max(0.0, v1_duration_ms), 3),
        v2_duration_ms=round(max(0.0, v2_duration_ms), 3),
        adapter_duration_ms=round(max(0.0, adapter_duration_ms), 3),
        canonical_v2_response_bytes=_canonical_v2_response_size(v2),
        v1_projection_fingerprint=_projection_fingerprint(v1_projection),
        v2_projection_fingerprint=_projection_fingerprint(v2_projection),
        v1_evidence_fingerprint=_value_fingerprint(v1_evidence),
        v2_evidence_fingerprint=_value_fingerprint(v2_evidence),
        evidence_difference_codes=evidence_difference_codes,
    )


def _safe_v1_projection(result: Any) -> dict[str, Any] | None:
    if getattr(result, "error_message", None):
        return None
    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        return None
    try:
        return _normalized_execution_projection(payload)
    except (AuthoritativeInstructionError, KeyError, TypeError, ValueError):
        return None


def _safe_v2_projection(result: Any) -> dict[str, Any] | None:
    if getattr(result, "error_code", None):
        return None
    adapted = getattr(result, "adapted_result", None)
    payload = getattr(adapted, "payload", None)
    if not isinstance(payload, dict):
        return None
    try:
        return _normalized_execution_projection(payload)
    except (AuthoritativeInstructionError, KeyError, TypeError, ValueError):
        return None


def _normalized_execution_projection(payload: dict[str, Any]) -> dict[str, Any]:
    instructions = normalize_authoritative_instructions(payload)
    projection: dict[str, Any] = {
        "instructions": [_instruction_projection(row) for row in instructions],
        "recognition_result": str(payload.get("recognition_result") or ""),
        "strategy": _drop_missing(payload.get("strategy") or {}),
        "lifecycle_event": _drop_missing(payload.get("lifecycle_event") or {}),
        "confidence": float(payload.get("confidence") or 0.0),
    }
    for field in ("entry_context", "entry_fragments"):
        if field in payload:
            projection[field] = _drop_missing(payload[field])
    return projection


def _instruction_projection(row: AuthoritativeInstruction) -> dict[str, Any]:
    parameters = dict(row.parameters or {})
    parameters.pop("management_action", None)
    return {
        "kind": row.kind,
        "confidence": row.confidence,
        "strategy": _drop_missing(row.strategy),
        "target": {
            "lifecycle_id": row.target_lifecycle_id,
            "thread_id": row.target_thread_id,
        },
        "parameters": _drop_missing(parameters),
    }


def _drop_missing(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _drop_missing(item)
            for key, item in value.items()
            if key != "reason" and item not in (None, "")
        }
    if isinstance(value, (list, tuple)):
        return [_drop_missing(item) for item in value]
    return value


def _has_executable_semantics(projection: dict[str, Any]) -> bool:
    if projection.get("instructions"):
        return True
    if projection.get("recognition_result") == "是策略":
        return True
    lifecycle = projection.get("lifecycle_event")
    return isinstance(lifecycle, dict) and lifecycle.get("event_type") not in {
        None,
        "none",
    }


def _projection_fingerprint(projection: dict[str, Any] | None) -> str | None:
    return _value_fingerprint(projection)


def _value_fingerprint(value: Any | None) -> str | None:
    if value is None:
        return None
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_evidence_projection(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    evidence = payload.get("evidence")
    if evidence is None:
        return {"text_fields": {}, "images": [], "conflicts": []}
    if not isinstance(evidence, dict):
        return None
    text = evidence.get("text") if isinstance(evidence.get("text"), dict) else {}
    images = evidence.get("images")
    conflicts = evidence.get("conflicts")
    if not isinstance(images, list) or not isinstance(conflicts, list):
        return None
    projected_images = []
    for row in images:
        if not isinstance(row, dict):
            return None
        projected_images.append(
            {
                "asset_id": row.get("asset_id"),
                "image_type": row.get("image_type"),
                "quality": row.get("quality"),
                "fields": _drop_missing(row.get("fields") or {}),
                "confidence": row.get("confidence"),
            }
        )
    return {
        "text_fields": _drop_missing(text.get("fields") or {}),
        "images": projected_images,
        "conflicts": list(conflicts),
    }


def _evidence_difference_codes(
    v1: dict[str, Any] | None,
    v2: dict[str, Any] | None,
) -> tuple[str, ...]:
    if v1 == v2:
        return ()
    if v1 is None or v2 is None:
        return ("evidence_unavailable",)
    codes: list[str] = []
    if _evidence_field_sources(v1["text_fields"]) != _evidence_field_sources(
        v2["text_fields"]
    ):
        codes.append("text_field_attribution_changed")
    v1_images = v1["images"]
    v2_images = v2["images"]
    v1_asset_ids = [row.get("asset_id") for row in v1_images]
    v2_asset_ids = [row.get("asset_id") for row in v2_images]
    if v1_asset_ids != v2_asset_ids:
        codes.append("image_asset_ids_changed")
    else:
        v1_metadata = [
            {
                "asset_id": row.get("asset_id"),
                "image_type": row.get("image_type"),
            }
            for row in v1_images
        ]
        v2_metadata = [
            {
                "asset_id": row.get("asset_id"),
                "image_type": row.get("image_type"),
            }
            for row in v2_images
        ]
        if v1_metadata != v2_metadata:
            codes.append("image_metadata_changed")
        v1_fields = [
            {
                "asset_id": row.get("asset_id"),
                "fields": _evidence_field_sources(row.get("fields")),
            }
            for row in v1_images
        ]
        v2_fields = [
            {
                "asset_id": row.get("asset_id"),
                "fields": _evidence_field_sources(row.get("fields")),
            }
            for row in v2_images
        ]
        if v1_fields != v2_fields:
            codes.append("image_field_attribution_changed")
    if v1["conflicts"] != v2["conflicts"]:
        codes.append("evidence_conflicts_changed")
    return tuple(codes)


def _evidence_field_sources(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            projected[str(key)] = {
                field: _drop_missing(item.get(field))
                for field in ("value", "source")
                if item.get(field) not in (None, "")
            }
        else:
            projected[str(key)] = _drop_missing(item)
    return projected


def _canonical_v2_response_size(result: Any) -> int:
    explicit = getattr(result, "response_size_bytes", None)
    if (
        isinstance(explicit, int)
        and not isinstance(explicit, bool)
        and explicit >= 0
    ):
        return explicit
    adapted = getattr(result, "adapted_result", None)
    canonical = getattr(adapted, "canonical_v2_json", None)
    if not isinstance(canonical, str):
        return 0
    return len(canonical.encode("utf-8"))


def _reported_duration(result: Any, measured_ms: float) -> float:
    value = getattr(result, "replay_duration_ms", measured_ms)
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return max(0.0, measured_ms)
    return normalized if math.isfinite(normalized) and normalized >= 0 else max(
        0.0, measured_ms
    )


def _percentile_95(values: Iterable[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _write_artifacts(output_dir: Path, result: MimoV2ReplayResult) -> None:
    (output_dir / "summary.json").write_text(
        json.dumps(
            result.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    field_names = [field.name for field in ReplayComparison.__dataclass_fields__.values()]
    with (output_dir / "comparisons.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=field_names)
        writer.writeheader()
        for row in result.comparisons:
            writer.writerow(asdict(row))
