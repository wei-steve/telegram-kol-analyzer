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
from urllib.parse import quote

from sqlalchemy import select

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.db import create_existing_session_factory
from telegram_kol_research.mimo_v2_execution_adapter import (
    _execution_projection,
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import RawMessage
from telegram_kol_research.recognition_experiments import (
    infer_mimo_authoritative_v2,
    run_mimo_authoritative_for_message,
)


MAX_REPLAY_MESSAGES = 200


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    raw_message_id: int
    classification: str
    v1_status: str
    v2_status: str
    v1_duration_ms: float
    v2_duration_ms: float
    adapter_duration_ms: float
    v1_projection_fingerprint: str | None
    v2_projection_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ReplayPerformance:
    v1_p95_ms: float
    v2_p95_ms: float
    adapter_p95_ms: float
    passed: bool
    failure_reasons: tuple[str, ...]


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
    )
    unsafe_mismatches = sum(
        row.classification == "unsafe_mismatch" for row in comparisons
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
) -> ReplayPerformance:
    """Apply the approved adapter and end-to-end P95 gates."""

    v1_p95 = _percentile_95(v1_duration_ms)
    v2_p95 = _percentile_95(v2_duration_ms)
    adapter_p95 = _percentile_95(adapter_duration_ms)
    reasons: list[str] = []
    if adapter_p95 >= 50.0:
        reasons.append("adapter_p95_at_or_above_50ms")
    if v1_p95 <= 0.0 or v2_p95 > v1_p95 * 1.15:
        reasons.append("v2_p95_above_115_percent_of_v1")
    return ReplayPerformance(
        v1_p95_ms=round(v1_p95, 3),
        v2_p95_ms=round(v2_p95, 3),
        adapter_p95_ms=round(adapter_p95, 3),
        passed=not reasons,
        failure_reasons=tuple(reasons),
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
    if isinstance(max_messages, bool) or not 1 <= int(max_messages) <= MAX_REPLAY_MESSAGES:
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
    uri = f"file:{quote(str(source), safe='/:')}?mode=ro"
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
    elif _semantic_projection(v1_projection) == _semantic_projection(v2_projection):
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
        v1_projection_fingerprint=_projection_fingerprint(v1_projection),
        v2_projection_fingerprint=_projection_fingerprint(v2_projection),
    )


def _safe_v1_projection(result: Any) -> dict[str, Any] | None:
    if getattr(result, "error_message", None):
        return None
    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        return None
    try:
        return _execution_projection(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _safe_v2_projection(result: Any) -> dict[str, Any] | None:
    if getattr(result, "error_code", None):
        return None
    adapted = getattr(result, "adapted_result", None)
    payload = getattr(adapted, "payload", None)
    if not isinstance(payload, dict):
        return None
    try:
        return _execution_projection(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _semantic_projection(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in projection.items()
        if key != "input_reading"
    }


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
    if projection is None:
        return None
    canonical = json.dumps(
        _semantic_projection(projection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
