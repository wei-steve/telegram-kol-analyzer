"""Web-only human labels for recognition quality observation.

Automated recognition, context resolution, candidate, execution, and notification
paths must never import or consume this module or its table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_kol_research.models import (
    MessageEvidenceVersion,
    MessageRecognitionLabel,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
    utc_now,
)
from telegram_kol_research.web_queries import (
    _select_current_mimo_run,
    _serialize_raw_messages,
)

LABEL_VERDICTS = frozenset({"correct", "incorrect", "uncertain"})
LABEL_ERROR_KINDS = frozenset(
    {
        "should_be_strategy",
        "should_not_be_strategy",
        "wrong_event_type",
        "wrong_target",
        "wrong_image_reading",
        "wrong_parameters",
        "context_should_have_been_used",
        "other",
    }
)
LABEL_CLIENT_FIELDS = frozenset({"verdict", "error_kind", "note"})
LABEL_NOTE_MAX_LENGTH = 2000


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _validate_payload(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("recognition label payload must be an object")
    unexpected = set(payload) - LABEL_CLIENT_FIELDS
    if unexpected:
        raise ValueError("recognition label payload contains unsupported fields")
    verdict = payload.get("verdict")
    if verdict not in LABEL_VERDICTS:
        raise ValueError("recognition label verdict is invalid")
    error_kind = _optional_text(payload.get("error_kind"), field="error_kind")
    if error_kind is not None and error_kind not in LABEL_ERROR_KINDS:
        raise ValueError("recognition label error_kind is invalid")
    if verdict != "incorrect" and error_kind is not None:
        raise ValueError("recognition label error_kind requires incorrect verdict")
    note = _optional_text(payload.get("note"), field="note")
    if note is not None and len(note) > LABEL_NOTE_MAX_LENGTH:
        raise ValueError("recognition label note exceeds 2000 characters")
    return str(verdict), error_kind, note


def _serialize_label(row: MessageRecognitionLabel) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "raw_message_id": int(row.raw_message_id),
        "verdict": row.verdict,
        "error_kind": row.error_kind,
        "note": row.note,
        "labeled_recognition_result": row.labeled_recognition_result,
        "labeled_event_type": row.labeled_event_type,
        "labeled_confidence": row.labeled_confidence,
        "labeled_model": row.labeled_model,
        "labeled_prompt_versions_json": row.labeled_prompt_versions_json,
        "labeled_prompt_versions_source": row.labeled_prompt_versions_source,
        "labeled_signal_candidate_count": row.labeled_signal_candidate_count,
        "labeled_accepted_candidate_count": row.labeled_accepted_candidate_count,
        "labeled_context_attempt_status": row.labeled_context_attempt_status,
        "created_at": _as_utc(row.created_at),
        "updated_at": _as_utc(row.updated_at),
    }


def _build_snapshot(session, raw_message: RawMessage) -> dict[str, Any]:
    projected = _serialize_raw_messages(session, [raw_message])[0]
    decision = session.scalar(
        select(RecognitionDecision).where(
            RecognitionDecision.raw_message_id == int(raw_message.id)
        )
    )
    evidence = session.scalar(
        select(MessageEvidenceVersion)
        .where(
            MessageEvidenceVersion.raw_message_id == int(raw_message.id),
            MessageEvidenceVersion.superseded_at.is_(None),
        )
        .order_by(MessageEvidenceVersion.id.desc())
    )
    runs = list(
        session.scalars(
            select(MimoRecognitionRun)
            .where(MimoRecognitionRun.raw_message_id == int(raw_message.id))
            .order_by(MimoRecognitionRun.id.asc())
        )
    )
    current_run = _select_current_mimo_run(evidence=evidence, runs=runs)
    prompt_versions_json = None
    prompt_versions_source = None
    if current_run is not None and str(current_run.prompt_versions_json or "").strip():
        prompt_versions_json = current_run.prompt_versions_json
        prompt_versions_source = "mimo_run"
    elif decision is not None and str(decision.prompt_versions_json or "").strip():
        prompt_versions_json = decision.prompt_versions_json
        prompt_versions_source = "recognition_decision"

    mimo_analysis = projected.get("mimo_analysis")
    mimo_analysis = mimo_analysis if isinstance(mimo_analysis, dict) else None
    runtime = mimo_analysis.get("runtime") if mimo_analysis is not None else None
    runtime = runtime if isinstance(runtime, dict) else None
    legacy_result = (
        mimo_analysis.get("legacy_result") if mimo_analysis is not None else None
    )
    legacy_result = legacy_result if isinstance(legacy_result, dict) else None
    system_acceptance = projected.get("system_acceptance")
    system_acceptance = (
        system_acceptance if isinstance(system_acceptance, dict) else None
    )
    context_resolution = projected.get("context_resolution")
    context_resolution = (
        context_resolution if isinstance(context_resolution, dict) else None
    )
    confidence = mimo_analysis.get("confidence") if mimo_analysis is not None else None
    model = runtime.get("model") if runtime is not None else None
    if model is None and legacy_result is not None:
        model = legacy_result.get("model")
    if model is None and decision is not None:
        model = decision.authoritative_model

    return {
        "labeled_recognition_result": projected.get("recognition_result"),
        "labeled_event_type": projected.get("lifecycle_event_type"),
        "labeled_confidence": confidence,
        "labeled_model": model,
        "labeled_prompt_versions_json": prompt_versions_json,
        "labeled_prompt_versions_source": prompt_versions_source,
        "labeled_signal_candidate_count": projected.get("signal_candidate_count"),
        "labeled_accepted_candidate_count": (
            system_acceptance.get("accepted_candidate_count")
            if system_acceptance is not None
            else None
        ),
        "labeled_context_attempt_status": (
            context_resolution.get("attempt_status")
            if context_resolution is not None
            else None
        ),
    }


def save_message_recognition_label(
    session_factory,
    *,
    raw_message_id: int,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and upsert one Web-only label with a server-owned snapshot."""

    verdict, error_kind, note = _validate_payload(payload)
    captured_at = _as_utc(now or utc_now())
    with session_factory() as session:
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            # Pin one writable snapshot before reading recognition facts. This
            # avoids a WAL read-to-write upgrade racing a worker commit.
            session.execute(text("BEGIN IMMEDIATE"))
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError(f"Raw message {raw_message_id} not found")
        values = {
            "raw_message_id": int(raw_message_id),
            "verdict": verdict,
            "error_kind": error_kind,
            "note": note,
            **_build_snapshot(session, raw_message),
            "created_at": captured_at,
            "updated_at": captured_at,
        }
        update_values = {
            key: value
            for key, value in values.items()
            if key not in {"raw_message_id", "created_at"}
        }
        session.execute(
            sqlite_insert(MessageRecognitionLabel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[MessageRecognitionLabel.raw_message_id],
                set_=update_values,
            )
        )
        session.commit()
        row = session.scalar(
            select(MessageRecognitionLabel).where(
                MessageRecognitionLabel.raw_message_id == int(raw_message_id)
            )
        )
        if row is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("recognition label upsert did not persist a row")
        return _serialize_label(row)


def load_message_recognition_label(
    session_factory,
    *,
    raw_message_id: int,
) -> dict[str, Any] | None:
    """Read one label for Web display without affecting automated decisions."""

    with session_factory() as session:
        if session.get(RawMessage, int(raw_message_id)) is None:
            raise LookupError(f"Raw message {raw_message_id} not found")
        row = session.scalar(
            select(MessageRecognitionLabel).where(
                MessageRecognitionLabel.raw_message_id == int(raw_message_id)
            )
        )
        return _serialize_label(row) if row is not None else None
