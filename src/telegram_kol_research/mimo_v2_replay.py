"""Fail-closed, non-executing MiMo v1/v2 isolated replay helpers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.recognition_experiments import (
    build_authoritative_context_for_message,
    infer_mimo_authoritative_v2,
    run_mimo_authoritative_for_message,
)


MAX_REPLAY_MESSAGES = 200
REPLAY_ARTIFACT_SCHEMA_VERSION = 1
_EXECUTION_CONFIDENCE_THRESHOLD = 0.7
_POSITIVE_MESSAGE_ID = re.compile(r"^[1-9][0-9]*$")
_COMPARISON_ARTIFACT_FIELDS = (
    "raw_message_id",
    "status",
    "reason_code",
    "v1_status",
    "v2_status",
    "v1_duration_ms",
    "v2_duration_ms",
    "adapter_duration_ms",
    "v1_projection_fingerprint",
    "v2_projection_fingerprint",
)
_REPLAY_ARTIFACT_NAMES = (
    "comparisons.json",
    "comparisons.csv",
    "summary.json",
)


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


@dataclass(frozen=True, slots=True)
class ReplayComparisonRow:
    raw_message_id: int
    status: str
    reason_code: str
    v1_status: str
    v2_status: str
    v1_duration_ms: float | None
    v2_duration_ms: float | None
    adapter_duration_ms: float | None
    v1_projection_fingerprint: str | None
    v2_projection_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class MimoV2ReplayResult:
    processed: int
    comparable: int
    unsafe_mismatches: int
    validation_failures: int
    production_writes: int
    notifications_sent: int
    execution_calls: int
    passed: bool
    comparisons: tuple[ReplayComparisonRow, ...]
    performance: ReplayPerformanceGate
    artifact_dir: Path


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


def run_mimo_v2_replay(
    *,
    inputs: MimoV2ReplayInputs,
    ai_recognition_config_path: str | Path,
    v1_runner: Callable[..., Any] = run_mimo_authoritative_for_message,
    v2_runner: Callable[..., Any] = infer_mimo_authoritative_v2,
    clock: Callable[[], float] = time.perf_counter,
) -> MimoV2ReplayResult:
    """Compare MiMo versions using only one disposable database snapshot."""

    if not isinstance(inputs, MimoV2ReplayInputs):
        raise MimoV2ReplayInputError("replay_inputs_invalid")
    comparisons: list[ReplayComparisonRow] = []
    v1_latencies: list[float] = []
    v2_latencies: list[float] = []
    adapter_latencies: list[float] = []
    engine = None
    with tempfile.TemporaryDirectory(
        prefix=".working-",
        dir=inputs.artifact_dir,
    ) as temporary:
        working_root = Path(temporary)
        working_root.chmod(0o700)
        working_database = create_read_only_replay_snapshot(
            inputs.source_database,
            working_root / "working.db",
        )
        session_factory = create_session_factory(working_database)
        engine = session_factory.kw["bind"]
        try:
            _validate_selected_messages(
                session_factory,
                raw_message_ids=inputs.raw_message_ids,
            )
            for raw_message_id in inputs.raw_message_ids:
                if not _message_images_are_available(
                    session_factory,
                    raw_message_id=raw_message_id,
                    media_root=inputs.media_root,
                ):
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="image_unavailable",
                        )
                    )
                    continue
                try:
                    context_text = str(
                        build_authoritative_context_for_message(
                            session_factory,
                            raw_message_id,
                        )
                    )
                except Exception:
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="context_build_failed",
                        )
                    )
                    continue
                v1_started = clock()
                try:
                    v1_result = v1_runner(
                        session_factory,
                        raw_message_id=raw_message_id,
                        ai_recognition_config_path=ai_recognition_config_path,
                        media_root=inputs.media_root,
                        context_text=context_text,
                    )
                except Exception:
                    v1_duration = _elapsed_ms(v1_started, clock())
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="v1_runner_error",
                            v1_status="error",
                            v1_duration_ms=v1_duration,
                        )
                    )
                    continue
                v1_duration = _elapsed_ms(v1_started, clock())
                v1_payload = getattr(v1_result, "payload", None)
                if (
                    getattr(v1_result, "error_message", None)
                    or str(getattr(v1_result, "status", "")) == "识别失败"
                    or not isinstance(v1_payload, Mapping)
                ):
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="v1_failed",
                            v1_status="failed",
                            v1_duration_ms=v1_duration,
                        )
                    )
                    continue
                v2_started = clock()
                try:
                    v2_result = v2_runner(
                        session_factory,
                        raw_message_id=raw_message_id,
                        ai_recognition_config_path=ai_recognition_config_path,
                        media_root=inputs.media_root,
                        context_text=context_text,
                    )
                except Exception:
                    v2_duration = _elapsed_ms(v2_started, clock())
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="v2_runner_error",
                            v1_status="completed",
                            v2_status="error",
                            v1_duration_ms=v1_duration,
                            v2_duration_ms=v2_duration,
                        )
                    )
                    continue
                v2_duration = _elapsed_ms(v2_started, clock())
                if not bool(getattr(v2_result, "succeeded", False)):
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code=_stable_v2_failure_code(
                                getattr(v2_result, "error_code", None)
                            ),
                            v1_status="completed",
                            v2_status="failed",
                            v1_duration_ms=v1_duration,
                            v2_duration_ms=v2_duration,
                        )
                    )
                    continue
                parsed_result = getattr(v2_result, "parsed_result", None)
                adapter_started = clock()
                try:
                    adapted = adapt_mimo_v2_to_current_payload(parsed_result)
                except Exception:
                    adapter_duration = _elapsed_ms(adapter_started, clock())
                    comparisons.append(
                        _validation_failure_row(
                            raw_message_id,
                            reason_code="adapter_failure",
                            v1_status="completed",
                            v2_status="failed",
                            v1_duration_ms=v1_duration,
                            v2_duration_ms=v2_duration,
                            adapter_duration_ms=adapter_duration,
                        )
                    )
                    continue
                adapter_duration = _elapsed_ms(adapter_started, clock())
                projection = compare_execution_projections(
                    v1_payload,
                    adapted.payload,
                )
                comparisons.append(
                    ReplayComparisonRow(
                        raw_message_id=raw_message_id,
                        status=projection.status,
                        reason_code=projection.reason_code,
                        v1_status="completed",
                        v2_status="completed",
                        v1_duration_ms=v1_duration,
                        v2_duration_ms=v2_duration,
                        adapter_duration_ms=adapter_duration,
                        v1_projection_fingerprint=projection.v1_fingerprint,
                        v2_projection_fingerprint=projection.v2_fingerprint,
                    )
                )
                v1_latencies.append(v1_duration)
                v2_latencies.append(v2_duration)
                adapter_latencies.append(adapter_duration)
        finally:
            engine.dispose()
    performance = evaluate_replay_performance(
        v1_duration_ms=v1_latencies,
        v2_duration_ms=v2_latencies,
        adapter_duration_ms=adapter_latencies,
    )
    unsafe_mismatches = sum(
        row.status == "unsafe_mismatch" for row in comparisons
    )
    validation_failures = sum(
        row.status == "validation_failed" for row in comparisons
    )
    comparable = sum(
        row.status in {"safe_match", "unsafe_mismatch"}
        for row in comparisons
    )
    production_writes = 0
    notifications_sent = 0
    execution_calls = 0
    passed = (
        len(comparisons) == len(inputs.raw_message_ids)
        and unsafe_mismatches == 0
        and validation_failures == 0
        and performance.passed
        and production_writes == 0
        and notifications_sent == 0
        and execution_calls == 0
    )
    result = MimoV2ReplayResult(
        processed=len(comparisons),
        comparable=comparable,
        unsafe_mismatches=unsafe_mismatches,
        validation_failures=validation_failures,
        production_writes=production_writes,
        notifications_sent=notifications_sent,
        execution_calls=execution_calls,
        passed=passed,
        comparisons=tuple(comparisons),
        performance=performance,
        artifact_dir=inputs.artifact_dir,
    )
    write_replay_artifacts(result)
    return result


def write_replay_artifacts(result: MimoV2ReplayResult) -> tuple[Path, ...]:
    """Atomically retain only allowlisted, non-sensitive replay fields."""

    if not isinstance(result, MimoV2ReplayResult):
        raise MimoV2ReplayInputError("artifact_result_invalid")
    artifact_dir = _validated_replay_artifact_output_directory(
        result.artifact_dir
    )
    rows = [
        {
            field: getattr(comparison, field)
            for field in _COMPARISON_ARTIFACT_FIELDS
        }
        for comparison in result.comparisons
    ]
    summary = build_replay_summary(result)
    _validate_artifact_value(rows)
    _validate_artifact_value(summary)

    comparisons_json = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    summary_json = json.dumps(
        summary,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=_COMPARISON_ARTIFACT_FIELDS,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    payloads = (
        ("comparisons.json", comparisons_json),
        ("comparisons.csv", csv_buffer.getvalue()),
        ("summary.json", summary_json),
    )
    written: list[Path] = []
    for name, content in payloads:
        written.append(
            _atomic_write_artifact_text(
                artifact_dir,
                name=name,
                content=content,
            )
        )
    return tuple(written)


def build_replay_summary(result: MimoV2ReplayResult) -> dict[str, Any]:
    """Build the compact allowlisted summary shared by artifacts and CLI."""

    if not isinstance(result, MimoV2ReplayResult):
        raise MimoV2ReplayInputError("artifact_result_invalid")
    effective_passed = (
        result.passed
        and result.unsafe_mismatches == 0
        and result.validation_failures == 0
        and result.performance.passed
        and result.production_writes == 0
        and result.notifications_sent == 0
        and result.execution_calls == 0
    )
    summary = {
        "schema_version": REPLAY_ARTIFACT_SCHEMA_VERSION,
        "processed": result.processed,
        "comparable": result.comparable,
        "unsafe_mismatches": result.unsafe_mismatches,
        "validation_failures": result.validation_failures,
        "production_writes": result.production_writes,
        "notifications_sent": result.notifications_sent,
        "execution_calls": result.execution_calls,
        "v1_p95_ms": result.performance.v1_p95_ms,
        "v2_p95_ms": result.performance.v2_p95_ms,
        "adapter_p95_ms": result.performance.adapter_p95_ms,
        "v2_to_v1_ratio": result.performance.v2_to_v1_ratio,
        "performance_passed": result.performance.passed,
        "performance_failure_codes": list(
            result.performance.failure_codes
        ),
        "passed": effective_passed,
    }
    _validate_artifact_value(summary)
    return summary


def _validated_replay_artifact_output_directory(path: Path) -> Path:
    candidate = Path(path)
    if (
        _path_has_symlink_component(candidate)
        or not candidate.is_dir()
    ):
        raise MimoV2ReplayInputError("artifact_dir_invalid")
    try:
        if stat.S_IMODE(candidate.stat().st_mode) != 0o700:
            raise MimoV2ReplayInputError("artifact_dir_invalid")
        for entry in candidate.iterdir():
            if (
                entry.name not in _REPLAY_ARTIFACT_NAMES
                or entry.is_symlink()
                or not entry.is_file()
            ):
                raise MimoV2ReplayInputError("artifact_dir_invalid")
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise MimoV2ReplayInputError("artifact_dir_invalid") from exc


def _validate_artifact_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise MimoV2ReplayInputError("artifact_value_invalid")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MimoV2ReplayInputError("artifact_value_invalid")
            _validate_artifact_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_artifact_value(item)
        return
    raise MimoV2ReplayInputError("artifact_value_invalid")


def _atomic_write_artifact_text(
    artifact_dir: Path,
    *,
    name: str,
    content: str,
) -> Path:
    if name not in _REPLAY_ARTIFACT_NAMES:
        raise MimoV2ReplayInputError("artifact_name_invalid")
    temporary = artifact_dir / f".{name}.tmp"
    destination = artifact_dir / name
    if temporary.exists() or temporary.is_symlink():
        raise MimoV2ReplayInputError("artifact_write_failed")
    try:
        def secure_opener(path, flags):
            return os.open(
                path,
                flags
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )

        with open(
            temporary,
            "w",
            encoding="utf-8",
            newline="",
            opener=secure_opener,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        return destination
    except (OSError, UnicodeError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MimoV2ReplayInputError("artifact_write_failed") from exc


def _validate_selected_messages(
    session_factory,
    *,
    raw_message_ids: tuple[int, ...],
) -> None:
    with session_factory() as session:
        found = {
            int(row[0])
            for row in session.query(RawMessage.id)
            .filter(RawMessage.id.in_(raw_message_ids))
            .all()
        }
    if any(raw_message_id not in found for raw_message_id in raw_message_ids):
        raise MimoV2ReplayInputError("raw_message_not_found")


def _message_images_are_available(
    session_factory,
    *,
    raw_message_id: int,
    media_root: Path,
) -> bool:
    with session_factory() as session:
        assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message_id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
    for asset in assets:
        kind = str(asset.kind or "").lower()
        mime_type = str(asset.mime_type or "").lower()
        if not (
            "image" in kind
            or "photo" in kind
            or mime_type.startswith("image/")
        ):
            continue
        resolved = resolve_media_path(asset.local_path, media_root=media_root)
        if (
            resolved is None
            or _path_has_symlink_component(_lexical_media_path(asset, media_root))
            or not resolved.is_file()
        ):
            return False
        try:
            if resolved.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def _lexical_media_path(asset: MediaAsset, media_root: Path) -> Path:
    local_path = str(asset.local_path or "").replace("\\", "/")
    while local_path.startswith("data/media/"):
        local_path = local_path[len("data/media/") :]
    candidate = Path(local_path)
    return candidate if candidate.is_absolute() else media_root / candidate


def _elapsed_ms(started: float, completed: float) -> float:
    if (
        isinstance(started, bool)
        or isinstance(completed, bool)
        or not math.isfinite(float(started))
        or not math.isfinite(float(completed))
        or completed < started
    ):
        raise MimoV2ReplayInputError("clock_invalid")
    return (float(completed) - float(started)) * 1000.0


def _validation_failure_row(
    raw_message_id: int,
    *,
    reason_code: str,
    v1_status: str = "not_run",
    v2_status: str = "not_run",
    v1_duration_ms: float | None = None,
    v2_duration_ms: float | None = None,
    adapter_duration_ms: float | None = None,
) -> ReplayComparisonRow:
    return ReplayComparisonRow(
        raw_message_id=raw_message_id,
        status="validation_failed",
        reason_code=reason_code,
        v1_status=v1_status,
        v2_status=v2_status,
        v1_duration_ms=v1_duration_ms,
        v2_duration_ms=v2_duration_ms,
        adapter_duration_ms=adapter_duration_ms,
        v1_projection_fingerprint=None,
        v2_projection_fingerprint=None,
    )


def _stable_v2_failure_code(value: Any) -> str:
    normalized = str(value or "")
    if normalized in {
        "provider_timeout",
        "provider_http_error",
        "invalid_json",
        "contract_validation_failed",
        "adapter_failure",
        "image_unavailable",
        "input_changed_during_analysis",
    }:
        return normalized
    return "v2_failed"


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
    entry_context = payload.get("entry_context")
    entry_fragments = payload.get("entry_fragments")
    projection: dict[str, Any] = {
        "executable": True,
        "recognition_result": str(payload.get("recognition_result") or ""),
        "execution_eligible": _execution_confidence_bucket(
            payload.get("confidence")
        ),
        "strategy": _plain_projection_value(
            strategy if isinstance(strategy, Mapping) else {}
        ),
        "instructions": _canonical_instruction_projection(payload),
        "lifecycle_event": _semantic_projection_row(lifecycle)
        if isinstance(lifecycle, Mapping)
        else {},
    }
    if isinstance(entry_context, Mapping):
        projection["entry_context"] = _semantic_projection_row(entry_context)
    if isinstance(entry_fragments, list):
        projection["entry_fragments"] = [
            _semantic_projection_row(row)
            for row in entry_fragments
            if isinstance(row, Mapping)
        ]
    return projection


def _canonical_instruction_projection(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw = payload.get("instructions")
    if isinstance(raw, list) and raw:
        return [
            _canonical_instruction_row(row)
            for row in raw
            if isinstance(row, Mapping)
        ]

    rows: list[dict[str, Any]] = []
    lifecycle = payload.get("lifecycle_event")
    if isinstance(lifecycle, Mapping):
        kind = _legacy_lifecycle_instruction_kind(lifecycle)
        if kind is not None:
            parameters = {
                str(key): _plain_projection_value(value)
                for key, value in lifecycle.items()
                if key
                not in {
                    "event_type",
                    "management_action",
                    "target_lifecycle_id",
                    "target_thread_id",
                    "confidence",
                    "reason",
                    "symbol",
                    "side",
                }
                and value is not None
            }
            rows.append(
                {
                    "kind": kind,
                    "execution_eligible": _execution_confidence_bucket(
                        lifecycle.get("confidence")
                    ),
                    "strategy": None,
                    "target": {
                        "lifecycle_id": lifecycle.get("target_lifecycle_id"),
                        "thread_id": None,
                    },
                    "parameters": parameters,
                }
            )
    strategy = payload.get("strategy")
    if (
        str(payload.get("recognition_result") or "") == "是策略"
        and isinstance(strategy, Mapping)
        and strategy
    ):
        rows.append(
            {
                "kind": "entry",
                "execution_eligible": _execution_confidence_bucket(
                    payload.get("confidence")
                ),
                "strategy": _plain_projection_value(strategy),
                "target": {"lifecycle_id": None, "thread_id": None},
                "parameters": {},
            }
        )
    return rows


def _canonical_instruction_row(row: Mapping[str, Any]) -> dict[str, Any]:
    target = row.get("target")
    target = target if isinstance(target, Mapping) else {}
    lifecycle_id = target.get("lifecycle_id", row.get("target_lifecycle_id"))
    thread_id = target.get("thread_id", row.get("target_thread_id"))
    parameters = row.get("parameters")
    return {
        "kind": _canonical_instruction_kind(row.get("kind") or row.get("action")),
        "execution_eligible": _execution_confidence_bucket(
            row.get("confidence")
        ),
        "strategy": _plain_projection_value(row.get("strategy")),
        "target": {
            "lifecycle_id": lifecycle_id,
            "thread_id": thread_id,
        },
        "parameters": _plain_projection_value(
            parameters if isinstance(parameters, Mapping) else {}
        ),
    }


def _legacy_lifecycle_instruction_kind(
    lifecycle: Mapping[str, Any],
) -> str | None:
    event_type = str(lifecycle.get("event_type") or "none")
    action = _canonical_instruction_kind(lifecycle.get("management_action"))
    if event_type == "cancel_entry":
        return "cancel_pending_entry"
    if event_type == "exit_position":
        return action if action in {"full_exit", "partial_exit"} else "full_exit"
    if event_type == "position_update":
        return action or "hold_update"
    return None


def _canonical_instruction_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return {
        "cancel_entry": "cancel_pending_entry",
        "exit_full": "full_exit",
        "exit_partial": "partial_exit",
    }.get(kind, kind)


def _semantic_projection_row(value: Mapping[str, Any]) -> dict[str, Any]:
    projection = {
        str(key): _plain_projection_value(item)
        for key, item in value.items()
        if key not in {"reason", "confidence", "_exact_context_risk_reduction_authorized"}
        and item is not None
    }
    if "confidence" in value:
        projection["execution_eligible"] = _execution_confidence_bucket(
            value.get("confidence")
        )
    return projection


def _execution_confidence_bucket(value: Any) -> bool:
    if isinstance(value, bool):
        raise MimoV2ReplayInputError("execution_projection_invalid")
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MimoV2ReplayInputError("execution_projection_invalid") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise MimoV2ReplayInputError("execution_projection_invalid")
    return confidence >= _EXECUTION_CONFIDENCE_THRESHOLD


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
