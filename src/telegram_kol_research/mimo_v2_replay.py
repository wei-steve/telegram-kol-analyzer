"""Fail-closed, non-executing MiMo v1/v2 isolated replay helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_REPLAY_MESSAGES = 200
_POSITIVE_MESSAGE_ID = re.compile(r"^[1-9][0-9]*$")


class MimoV2ReplayInputError(ValueError):
    """Raised before replay when an isolation or bounded-input rule fails."""


@dataclass(frozen=True, slots=True)
class MimoV2ReplayInputs:
    source_database: Path
    message_id_file: Path
    media_root: Path
    artifact_dir: Path
    raw_message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReplayProjectionComparison:
    status: str
    reason_code: str
    v1_fingerprint: str
    v2_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReplayPerformanceGate:
    passed: bool
    failure_codes: tuple[str, ...]
    v1_p95_ms: float | None
    v2_p95_ms: float | None
    adapter_p95_ms: float | None
    v2_to_v1_ratio: float | None


def load_replay_message_ids(
    path: str | Path,
    *,
    max_messages: int,
) -> tuple[int, ...]:
    """Load a stable, explicit and bounded raw-message ID allowlist."""

    if (
        isinstance(max_messages, bool)
        or not isinstance(max_messages, int)
        or not 1 <= max_messages <= MAX_REPLAY_MESSAGES
    ):
        raise MimoV2ReplayInputError("max_messages_invalid")
    source = Path(path)
    if _path_has_symlink_component(source) or not source.is_file():
        raise MimoV2ReplayInputError("message_id_file_invalid")
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MimoV2ReplayInputError("message_id_file_invalid") from exc
    selected: list[int] = []
    seen: set[int] = set()
    for raw_line in lines:
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        if _POSITIVE_MESSAGE_ID.fullmatch(value) is None:
            raise MimoV2ReplayInputError("message_id_invalid")
        raw_message_id = int(value)
        if raw_message_id in seen:
            continue
        if len(selected) >= max_messages:
            raise MimoV2ReplayInputError("message_id_limit_exceeded")
        selected.append(raw_message_id)
        seen.add(raw_message_id)
    if not selected:
        raise MimoV2ReplayInputError("message_id_list_empty")
    return tuple(selected)


def validate_replay_inputs(
    *,
    source_database: str | Path,
    message_id_file: str | Path,
    media_root: str | Path,
    artifact_dir: str | Path,
    max_messages: int,
) -> MimoV2ReplayInputs:
    """Validate and freeze replay boundaries before any model or database call."""

    source = _validated_regular_file(
        source_database,
        reason="source_database_invalid",
    )
    id_file = _validated_regular_file(
        message_id_file,
        reason="message_id_file_invalid",
    )
    media = _validated_directory(media_root, reason="media_root_invalid")
    artifacts = _prepare_artifact_directory(artifact_dir)
    raw_message_ids = load_replay_message_ids(
        id_file,
        max_messages=max_messages,
    )
    return MimoV2ReplayInputs(
        source_database=source,
        message_id_file=id_file,
        media_root=media,
        artifact_dir=artifacts,
        raw_message_ids=raw_message_ids,
    )


def create_read_only_replay_snapshot(
    source_database: str | Path,
    destination: str | Path,
) -> Path:
    """Copy one consistent SQLite snapshot without opening source for writes."""

    source = _validated_regular_file(
        source_database,
        reason="source_database_invalid",
    )
    target = Path(destination)
    parent = target.parent
    if (
        target.exists()
        or _path_has_symlink_component(target)
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
    ):
        raise MimoV2ReplayInputError("snapshot_destination_invalid")
    try:
        has_live_sidecars = any(
            source.with_name(source.name + suffix).exists()
            for suffix in ("-wal", "-shm")
        )
        source_uri = source.as_uri() + (
            "?mode=ro" if has_live_sidecars else "?mode=ro&immutable=1"
        )
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with sqlite3.connect(target) as target_connection:
                source_connection.backup(
                    target_connection,
                    pages=1024,
                    sleep=0.01,
                )
                target_connection.commit()
        target.chmod(0o600)
        return target.resolve(strict=True)
    except (OSError, sqlite3.Error) as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise MimoV2ReplayInputError("sqlite_online_backup_failed") from exc


def compare_execution_projections(
    v1_payload: Mapping[str, Any],
    v2_payload: Mapping[str, Any],
) -> ReplayProjectionComparison:
    """Compare closed execution semantics without reading prose or evidence."""

    v1_executable = _has_executable_projection(v1_payload)
    v2_executable = _has_executable_projection(v2_payload)
    v1_projection = _canonical_execution_projection(
        v1_payload,
        executable=v1_executable,
    )
    v2_projection = _canonical_execution_projection(
        v2_payload,
        executable=v2_executable,
    )
    v1_fingerprint = _projection_fingerprint(v1_projection)
    v2_fingerprint = _projection_fingerprint(v2_projection)
    if not v1_executable and not v2_executable:
        return ReplayProjectionComparison(
            status="safe_match",
            reason_code="both_non_executable",
            v1_fingerprint=v1_fingerprint,
            v2_fingerprint=v2_fingerprint,
        )
    if v1_projection == v2_projection:
        return ReplayProjectionComparison(
            status="safe_match",
            reason_code="execution_projections_equal",
            v1_fingerprint=v1_fingerprint,
            v2_fingerprint=v2_fingerprint,
        )
    return ReplayProjectionComparison(
        status="unsafe_mismatch",
        reason_code="execution_projection_mismatch",
        v1_fingerprint=v1_fingerprint,
        v2_fingerprint=v2_fingerprint,
    )


def nearest_rank_percentile(
    samples: Iterable[float],
    percentile: float,
) -> float | None:
    """Return one deterministic nearest-rank percentile for finite timings."""

    if (
        isinstance(percentile, bool)
        or not isinstance(percentile, (int, float))
        or not math.isfinite(float(percentile))
        or not 0 < float(percentile) <= 1
    ):
        raise MimoV2ReplayInputError("percentile_invalid")
    normalized: list[float] = []
    for sample in samples:
        if isinstance(sample, bool):
            raise MimoV2ReplayInputError("latency_sample_invalid")
        try:
            value = float(sample)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MimoV2ReplayInputError("latency_sample_invalid") from exc
        if not math.isfinite(value) or value < 0:
            raise MimoV2ReplayInputError("latency_sample_invalid")
        normalized.append(value)
    if not normalized:
        return None
    normalized.sort()
    index = math.ceil(float(percentile) * len(normalized)) - 1
    return normalized[index]


def evaluate_replay_performance(
    *,
    v1_duration_ms: Iterable[float],
    v2_duration_ms: Iterable[float],
    adapter_duration_ms: Iterable[float],
) -> ReplayPerformanceGate:
    """Evaluate the approved adapter and end-to-end P95 gates."""

    v1_samples = tuple(v1_duration_ms)
    v2_samples = tuple(v2_duration_ms)
    adapter_samples = tuple(adapter_duration_ms)
    if not (len(v1_samples) == len(v2_samples) == len(adapter_samples)):
        raise MimoV2ReplayInputError("latency_samples_misaligned")
    v1_p95 = nearest_rank_percentile(v1_samples, 0.95)
    v2_p95 = nearest_rank_percentile(v2_samples, 0.95)
    adapter_p95 = nearest_rank_percentile(adapter_samples, 0.95)
    if v1_p95 is None or v2_p95 is None or adapter_p95 is None:
        return ReplayPerformanceGate(
            passed=False,
            failure_codes=("no_comparable_pairs",),
            v1_p95_ms=None,
            v2_p95_ms=None,
            adapter_p95_ms=None,
            v2_to_v1_ratio=None,
        )
    failure_codes: list[str] = []
    if adapter_p95 >= 50.0:
        failure_codes.append("adapter_latency_exceeded")
    v2_limit = v1_p95 * 1.15
    if v2_p95 > v2_limit and not math.isclose(
        v2_p95,
        v2_limit,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        failure_codes.append("v2_latency_ratio_exceeded")
    if v1_p95 == 0:
        ratio = 1.0 if v2_p95 == 0 else None
    else:
        ratio = v2_p95 / v1_p95
    return ReplayPerformanceGate(
        passed=not failure_codes,
        failure_codes=tuple(failure_codes),
        v1_p95_ms=v1_p95,
        v2_p95_ms=v2_p95,
        adapter_p95_ms=adapter_p95,
        v2_to_v1_ratio=ratio,
    )


def _has_executable_projection(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("recognition_result") or "") == "是策略":
        return True
    instructions = payload.get("instructions")
    if isinstance(instructions, list) and bool(instructions):
        return True
    lifecycle = payload.get("lifecycle_event")
    return bool(
        isinstance(lifecycle, Mapping)
        and str(lifecycle.get("event_type") or "none") != "none"
    )


def _canonical_execution_projection(
    payload: Mapping[str, Any],
    *,
    executable: bool,
) -> dict[str, Any]:
    if not executable:
        return {"executable": False}
    strategy = payload.get("strategy")
    lifecycle = payload.get("lifecycle_event")
    instructions = payload.get("instructions")
    entry_context = payload.get("entry_context")
    entry_fragments = payload.get("entry_fragments")
    projection: dict[str, Any] = {
        "executable": True,
        "recognition_result": str(payload.get("recognition_result") or ""),
        "confidence": payload.get("confidence"),
        "strategy": _plain_projection_value(
            strategy if isinstance(strategy, Mapping) else {}
        ),
        "instructions": [
            _projection_row(row)
            for row in instructions
            if isinstance(row, Mapping)
        ]
        if isinstance(instructions, list)
        else [],
        "lifecycle_event": _projection_row(lifecycle)
        if isinstance(lifecycle, Mapping)
        else {},
    }
    if isinstance(entry_context, Mapping):
        projection["entry_context"] = _projection_row(entry_context)
    if isinstance(entry_fragments, list):
        projection["entry_fragments"] = [
            _projection_row(row)
            for row in entry_fragments
            if isinstance(row, Mapping)
        ]
    return projection


def _projection_row(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _plain_projection_value(item)
        for key, item in value.items()
        if key not in {"reason", "_exact_context_risk_reduction_authorized"}
    }


def _plain_projection_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _plain_projection_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_projection_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise MimoV2ReplayInputError("execution_projection_invalid")


def _projection_fingerprint(projection: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MimoV2ReplayInputError("execution_projection_invalid") from exc
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validated_regular_file(path: str | Path, *, reason: str) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate) or not candidate.is_file():
        raise MimoV2ReplayInputError(reason)
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise MimoV2ReplayInputError(reason) from exc


def _validated_directory(path: str | Path, *, reason: str) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate) or not candidate.is_dir():
        raise MimoV2ReplayInputError(reason)
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise MimoV2ReplayInputError(reason) from exc


def _prepare_artifact_directory(path: str | Path) -> Path:
    candidate = Path(path)
    if _path_has_symlink_component(candidate):
        raise MimoV2ReplayInputError("artifact_dir_invalid")
    try:
        if candidate.exists():
            if not candidate.is_dir():
                raise MimoV2ReplayInputError("artifact_dir_invalid")
            if any(candidate.iterdir()):
                raise MimoV2ReplayInputError("artifact_dir_not_empty")
        else:
            candidate.mkdir(mode=0o700)
        candidate.chmod(0o700)
        return candidate.resolve(strict=True)
    except MimoV2ReplayInputError:
        raise
    except OSError as exc:
        raise MimoV2ReplayInputError("artifact_dir_invalid") from exc


def _path_has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False
