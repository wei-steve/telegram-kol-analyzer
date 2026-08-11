"""Append-only MiMo provider-attempt audit with guarded run completion."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    utc_now,
)


RUN_KINDS = frozenset(
    {"v1_authoritative", "v2_authoritative", "v1_fallback"}
)
ATTEMPT_STATUSES = frozenset(
    {"completed", "timeout", "http_error", "invalid_json", "contract_failure"}
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed"})
MAX_ERROR_MESSAGE_LENGTH = 512

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_NAMED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passphrase|credential|"
    r"authorization|cookie)\b\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


class MimoRecognitionRunError(RuntimeError):
    """Base error for recognition-run audit operations."""


class MimoRecognitionRunValidationError(MimoRecognitionRunError):
    """Raised when closed audit inputs are invalid."""


class MimoRecognitionRunConflict(MimoRecognitionRunError):
    """Raised when append-only or terminal-state rules would be violated."""


@dataclass(frozen=True, slots=True)
class MimoRecognitionRunView:
    id: int
    raw_message_id: int
    run_kind: str
    contract_version: str
    model: str
    input_kind: str
    input_fingerprint: str
    prompt_versions: dict[str, int]
    status: str
    attempt_count: int
    retry_of_run_id: int | None
    selected_attempt_ordinal: int | None
    final_error_code: str | None
    final_error_message: str | None
    became_authoritative: bool
    canonical_payload_fingerprint: str | None
    projection_fingerprint: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MimoRecognitionAttemptView:
    id: int
    run_id: int
    ordinal: int
    retry_of_ordinal: int | None
    status: str
    error_code: str | None
    error_message: str | None
    response_fingerprint: str | None
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    selected: bool
    created_at: datetime


def start_mimo_run(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    run_kind: str,
    contract_version: str,
    model: str,
    input_kind: str,
    input_fingerprint: str,
    prompt_versions: Mapping[str, int],
    retry_of_run_id: int | None = None,
    started_at: datetime | None = None,
) -> MimoRecognitionRunView:
    """Create one run identity without claiming any execution authority."""

    if run_kind not in RUN_KINDS:
        raise MimoRecognitionRunValidationError("invalid run kind")
    contract_version = _bounded_required_text(
        contract_version, field="contract version", max_length=64
    )
    model = _bounded_required_text(model, field="model", max_length=128)
    input_kind = _bounded_required_text(
        input_kind, field="input kind", max_length=32
    )
    input_fingerprint = _bounded_required_text(
        input_fingerprint, field="input fingerprint", max_length=128
    )
    prompt_versions_json = _canonical_prompt_versions(prompt_versions)
    now = started_at or utc_now()

    with session_factory() as session:
        if session.get(RawMessage, raw_message_id) is None:
            raise MimoRecognitionRunValidationError("raw message not found")
        if retry_of_run_id is not None:
            prior = session.get(MimoRecognitionRun, retry_of_run_id)
            if prior is None:
                raise MimoRecognitionRunValidationError("retry run not found")
            if prior.raw_message_id != raw_message_id:
                raise MimoRecognitionRunConflict(
                    "retry run must reference the same message"
                )
            if prior.status not in TERMINAL_RUN_STATUSES:
                raise MimoRecognitionRunConflict("retry run must be terminal")
        row = MimoRecognitionRun(
            raw_message_id=raw_message_id,
            run_kind=run_kind,
            contract_version=contract_version,
            model=model,
            input_kind=input_kind,
            input_fingerprint=input_fingerprint,
            prompt_versions_json=prompt_versions_json,
            status="running",
            attempt_count=0,
            retry_of_run_id=retry_of_run_id,
            became_authoritative=False,
            started_at=now,
            created_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _run_view(row)


def record_mimo_attempt(
    session_factory: sessionmaker,
    *,
    run_id: int,
    ordinal: int,
    status: str,
    retry_of_ordinal: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    response_payload: Any | None = None,
    duration_ms: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MimoRecognitionAttemptView:
    """Append exactly the next provider attempt; existing attempts never change."""

    if status not in ATTEMPT_STATUSES:
        raise MimoRecognitionRunValidationError("invalid attempt status")
    if status == "completed" and (error_code or error_message):
        raise MimoRecognitionRunValidationError(
            "completed attempt cannot contain an error"
        )
    normalized_error_code = _optional_identifier(error_code, field="error code")
    safe_error = _sanitize_error_message(error_message)
    started = started_at or utc_now()
    completed = completed_at or utc_now()
    if duration_ms is None:
        try:
            duration_ms = max(0, round((completed - started).total_seconds() * 1000))
        except TypeError as exc:
            raise MimoRecognitionRunValidationError(
                "attempt timestamps must use matching timezone semantics"
            ) from exc
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise MimoRecognitionRunValidationError("duration must be a nonnegative integer")

    with session_factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        if run is None:
            raise MimoRecognitionRunValidationError("run not found")
        if run.status != "running":
            raise MimoRecognitionRunConflict("run is already terminal")
        attempts = (
            session.query(MimoRecognitionAttempt)
            .filter(MimoRecognitionAttempt.run_id == run_id)
            .order_by(MimoRecognitionAttempt.ordinal.asc())
            .all()
        )
        expected_ordinal = len(attempts) + 1
        if ordinal != expected_ordinal:
            raise MimoRecognitionRunConflict(
                f"attempt must use next ordinal {expected_ordinal}"
            )
        if retry_of_ordinal is not None:
            if retry_of_ordinal >= ordinal or not any(
                item.ordinal == retry_of_ordinal for item in attempts
            ):
                raise MimoRecognitionRunValidationError(
                    "retry attempt must reference an earlier attempt"
                )
        row = MimoRecognitionAttempt(
            run_id=run_id,
            ordinal=ordinal,
            retry_of_ordinal=retry_of_ordinal,
            status=status,
            error_code=normalized_error_code,
            error_message=safe_error,
            response_fingerprint=(
                canonical_json_fingerprint(response_payload)
                if response_payload is not None
                else None
            ),
            started_at=started,
            completed_at=completed,
            duration_ms=duration_ms,
            created_at=completed,
        )
        session.add(row)
        run.attempt_count = ordinal
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise MimoRecognitionRunConflict(
                "attempt ordinal was already recorded"
            ) from exc
        session.refresh(row)
        return _attempt_view(row, selected_ordinal=None)


def complete_mimo_run(
    session_factory: sessionmaker,
    *,
    run_id: int,
    status: str,
    selected_ordinal: int | None,
    canonical_payload: Any | None = None,
    projection_payload: Any | None = None,
    final_error_code: str | None = None,
    final_error_message: str | None = None,
    became_authoritative: bool = False,
    completed_at: datetime | None = None,
) -> MimoRecognitionRunView:
    """Make the only guarded state transition from running to terminal."""

    if status not in TERMINAL_RUN_STATUSES:
        raise MimoRecognitionRunValidationError("invalid terminal status")
    if status == "completed":
        if selected_ordinal is None:
            raise MimoRecognitionRunValidationError(
                "completed run requires a selected attempt"
            )
        if canonical_payload is None or projection_payload is None:
            raise MimoRecognitionRunValidationError(
                "completed run requires canonical and projection payloads"
            )
        if final_error_code or final_error_message:
            raise MimoRecognitionRunValidationError(
                "completed run cannot contain a final error"
            )
    else:
        if selected_ordinal is not None:
            raise MimoRecognitionRunValidationError(
                "failed run cannot contain a selected attempt"
            )
        if canonical_payload is not None or projection_payload is not None:
            raise MimoRecognitionRunValidationError(
                "failed run cannot contain result payloads"
            )
        if became_authoritative:
            raise MimoRecognitionRunValidationError(
                "failed run cannot become authoritative"
            )
        if not final_error_code:
            raise MimoRecognitionRunValidationError(
                "failed run requires a final error code"
            )

    normalized_error_code = _optional_identifier(
        final_error_code, field="final error code"
    )
    safe_error = _sanitize_error_message(final_error_message)
    finished = completed_at or utc_now()
    with session_factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        if run is None:
            raise MimoRecognitionRunValidationError("run not found")
        if run.status != "running":
            raise MimoRecognitionRunConflict("run is already terminal")
        attempts = (
            session.query(MimoRecognitionAttempt)
            .filter(MimoRecognitionAttempt.run_id == run_id)
            .order_by(MimoRecognitionAttempt.ordinal.asc())
            .all()
        )
        if status == "completed":
            selected = next(
                (item for item in attempts if item.ordinal == selected_ordinal),
                None,
            )
            if selected is None or selected.status != "completed":
                raise MimoRecognitionRunValidationError(
                    "selected attempt must be a completed attempt"
                )
        updated = (
            session.query(MimoRecognitionRun)
            .filter(MimoRecognitionRun.id == run_id)
            .filter(MimoRecognitionRun.status == "running")
            .update(
                {
                    MimoRecognitionRun.status: status,
                    MimoRecognitionRun.attempt_count: len(attempts),
                    MimoRecognitionRun.selected_attempt_ordinal: selected_ordinal,
                    MimoRecognitionRun.final_error_code: normalized_error_code,
                    MimoRecognitionRun.final_error_message: safe_error,
                    MimoRecognitionRun.became_authoritative: bool(
                        became_authoritative
                    ),
                    MimoRecognitionRun.canonical_payload_fingerprint: (
                        canonical_json_fingerprint(canonical_payload)
                        if canonical_payload is not None
                        else None
                    ),
                    MimoRecognitionRun.projection_fingerprint: (
                        canonical_json_fingerprint(projection_payload)
                        if projection_payload is not None
                        else None
                    ),
                    MimoRecognitionRun.completed_at: finished,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            session.rollback()
            raise MimoRecognitionRunConflict("run is already terminal")
        session.commit()
        completed = session.get(MimoRecognitionRun, run_id)
        if completed is None:
            raise MimoRecognitionRunConflict("completed run disappeared")
        return _run_view(completed)


def load_mimo_attempts(
    session_factory: sessionmaker,
    *,
    run_id: int,
) -> list[MimoRecognitionAttemptView]:
    with session_factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        if run is None:
            raise MimoRecognitionRunValidationError("run not found")
        rows = (
            session.query(MimoRecognitionAttempt)
            .filter(MimoRecognitionAttempt.run_id == run_id)
            .order_by(MimoRecognitionAttempt.ordinal.asc())
            .all()
        )
        return [
            _attempt_view(row, selected_ordinal=run.selected_attempt_ordinal)
            for row in rows
        ]


load_attempts = load_mimo_attempts


def canonical_json_fingerprint(payload: Any) -> str:
    """Hash a deterministic JSON representation without persisting its contents."""

    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise MimoRecognitionRunValidationError(
            "payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_prompt_versions(prompt_versions: Mapping[str, int]) -> str:
    if not isinstance(prompt_versions, Mapping):
        raise MimoRecognitionRunValidationError("prompt versions must be an object")
    normalized: dict[str, int] = {}
    for key, value in prompt_versions.items():
        if not isinstance(key, str) or not key.strip():
            raise MimoRecognitionRunValidationError("prompt version key is invalid")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MimoRecognitionRunValidationError(
                "prompt version value must be a positive integer"
            )
        normalized[key.strip()] = value
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sanitize_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    normalized = _BEARER_SECRET.sub("Bearer [redacted]", normalized)
    normalized = _NAMED_SECRET.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        normalized,
    )
    return normalized[:MAX_ERROR_MESSAGE_LENGTH]


def _optional_identifier(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise MimoRecognitionRunValidationError(f"invalid {field}")
    return normalized


def _bounded_required_text(value: str, *, field: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise MimoRecognitionRunValidationError(f"invalid {field}")
    return normalized


def _run_view(row: MimoRecognitionRun) -> MimoRecognitionRunView:
    try:
        prompt_versions = json.loads(row.prompt_versions_json or "{}")
    except json.JSONDecodeError as exc:
        raise MimoRecognitionRunConflict("stored prompt versions are invalid") from exc
    return MimoRecognitionRunView(
        id=row.id,
        raw_message_id=row.raw_message_id,
        run_kind=row.run_kind,
        contract_version=row.contract_version,
        model=row.model,
        input_kind=row.input_kind,
        input_fingerprint=row.input_fingerprint,
        prompt_versions=prompt_versions,
        status=row.status,
        attempt_count=row.attempt_count,
        retry_of_run_id=row.retry_of_run_id,
        selected_attempt_ordinal=row.selected_attempt_ordinal,
        final_error_code=row.final_error_code,
        final_error_message=row.final_error_message,
        became_authoritative=bool(row.became_authoritative),
        canonical_payload_fingerprint=row.canonical_payload_fingerprint,
        projection_fingerprint=row.projection_fingerprint,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _attempt_view(
    row: MimoRecognitionAttempt,
    *,
    selected_ordinal: int | None,
) -> MimoRecognitionAttemptView:
    return MimoRecognitionAttemptView(
        id=row.id,
        run_id=row.run_id,
        ordinal=row.ordinal,
        retry_of_ordinal=row.retry_of_ordinal,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        response_fingerprint=row.response_fingerprint,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        selected=(selected_ordinal == row.ordinal),
        created_at=row.created_at,
    )
