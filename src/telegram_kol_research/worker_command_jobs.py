"""Durable worker-command identities and lifecycle transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, text

from telegram_kol_research.models import WorkerCommandJob, utc_now


WORKER_COMMAND_TYPES = frozenset(
    {
        "sync_deepcoin_execution",
        "close_bound_position",
        "recovery_live_submit",
        "process_next_trade_signal",
    }
)
MAX_REQUEST_JSON_BYTES = 16_384
MAX_RESULT_JSON_BYTES = 65_536
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_ERROR_SUMMARY_CHARS = 512
RESULT_SCHEMA_VERSION = 1

_SECRET_FIELD_NAMES = frozenset(
    {
        "authorization",
        "header",
        "headers",
        "token",
        "api_key",
        "api_secret",
        "secret",
        "password",
        "passphrase",
        "session",
        "session_string",
        "cookie",
        "dc_access_key",
        "dc_access_sign",
        "dc_access_timestamp",
        "dc_access_passphrase",
    }
)


class WorkerCommandValidationError(ValueError):
    """A command cannot be represented safely by the durable contract."""


class WorkerCommandIdempotencyConflict(RuntimeError):
    """One caller key was reused for a different canonical request."""

    def __init__(self, *, command_id: str) -> None:
        super().__init__("idempotency_key payload conflict")
        self.command_id = command_id


@dataclass(frozen=True, slots=True)
class WorkerCommandSnapshot:
    job_id: int
    command_id: str
    command_type: str
    request: dict[str, Any]
    request_fingerprint: str
    idempotency_key: str | None
    status: str
    attempt_count: int
    claim_token: str | None
    http_status: int | None
    result: Any | None
    error_code: str | None
    error_summary: str | None
    result_schema_version: int


@dataclass(frozen=True, slots=True)
class WorkerCommandClaim:
    job_id: int
    command_id: str
    command_type: str
    request: dict[str, Any]
    request_fingerprint: str
    attempt_count: int
    claim_token: str
    claimed_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ShadowWorkerCommandAdmission:
    snapshot: WorkerCommandSnapshot
    owner_claim: WorkerCommandClaim | None


def begin_shadow_worker_command(
    session_factory,
    *,
    command_type: str,
    request: dict[str, Any],
    idempotency_key: str | None = None,
    started_at: datetime | None = None,
    lease_for: timedelta = timedelta(minutes=5),
) -> ShadowWorkerCommandAdmission:
    """Durably cross the shadow execution boundary before Web authority runs."""

    canonical_json, fingerprint = canonical_worker_command_request(
        command_type=command_type,
        request=request,
    )
    normalized_key = _validated_idempotency_key(idempotency_key)
    execution_time = _naive_utc(started_at or utc_now())
    lease_expires_at = execution_time + timedelta(
        seconds=max(1.0, float(lease_for.total_seconds()))
    )
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        if normalized_key is not None:
            existing = (
                session.query(WorkerCommandJob)
                .filter(
                    WorkerCommandJob.command_type == command_type,
                    WorkerCommandJob.idempotency_key == normalized_key,
                )
                .one_or_none()
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    command_id = str(existing.command_id)
                    session.rollback()
                    raise WorkerCommandIdempotencyConflict(command_id=command_id)
                snapshot = _snapshot(existing)
                session.commit()
                return ShadowWorkerCommandAdmission(snapshot, None)

        token = uuid4().hex
        row = WorkerCommandJob(
            command_id=uuid4().hex,
            command_type=command_type,
            request_json=canonical_json,
            request_fingerprint=fingerprint,
            idempotency_key=normalized_key,
            status="executing",
            claim_token=token,
            claimed_at=execution_time,
            lease_expires_at=lease_expires_at,
            attempt_count=1,
            side_effect_started_at=execution_time,
            result_schema_version=RESULT_SCHEMA_VERSION,
            created_at=execution_time,
        )
        session.add(row)
        session.flush()
        snapshot = _snapshot(row)
        claim = WorkerCommandClaim(
            job_id=int(row.id),
            command_id=str(row.command_id),
            command_type=command_type,
            request=json.loads(canonical_json),
            request_fingerprint=fingerprint,
            attempt_count=1,
            claim_token=token,
            claimed_at=execution_time,
            lease_expires_at=lease_expires_at,
        )
        session.commit()
        return ShadowWorkerCommandAdmission(snapshot, claim)


def enqueue_worker_command(
    session_factory,
    *,
    command_type: str,
    request: dict[str, Any],
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> WorkerCommandSnapshot:
    """Create or reattach to one canonical durable command."""

    canonical_json, fingerprint = canonical_worker_command_request(
        command_type=command_type,
        request=request,
    )
    normalized_key = _validated_idempotency_key(idempotency_key)
    command_time = _naive_utc(created_at or utc_now())

    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        if normalized_key is not None:
            existing = (
                session.query(WorkerCommandJob)
                .filter(
                    WorkerCommandJob.command_type == command_type,
                    WorkerCommandJob.idempotency_key == normalized_key,
                )
                .one_or_none()
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    command_id = str(existing.command_id)
                    session.rollback()
                    raise WorkerCommandIdempotencyConflict(
                        command_id=command_id
                    )
                snapshot = _snapshot(existing)
                session.commit()
                return snapshot

        row = WorkerCommandJob(
            command_id=uuid4().hex,
            command_type=command_type,
            request_json=canonical_json,
            request_fingerprint=fingerprint,
            idempotency_key=normalized_key,
            status="pending",
            attempt_count=0,
            result_schema_version=RESULT_SCHEMA_VERSION,
            created_at=command_time,
        )
        session.add(row)
        session.flush()
        snapshot = _snapshot(row)
        session.commit()
        return snapshot


def claim_worker_commands(
    session_factory,
    *,
    claimed_at: datetime | None = None,
    lease_for: timedelta = timedelta(seconds=30),
    limit: int = 20,
    allowed_command_types: frozenset[str] | None = None,
) -> list[WorkerCommandClaim]:
    """Atomically claim oldest pending or expired pre-execution commands."""

    claim_time = _naive_utc(claimed_at or utc_now())
    lease_seconds = max(1.0, float(lease_for.total_seconds()))
    lease_expires_at = claim_time + timedelta(seconds=lease_seconds)
    claim_limit = max(0, int(limit))
    if claim_limit == 0:
        return []
    if allowed_command_types is not None and not allowed_command_types:
        return []

    claims: list[WorkerCommandClaim] = []
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        query = session.query(WorkerCommandJob).filter(
            or_(
                WorkerCommandJob.status == "pending",
                and_(
                    WorkerCommandJob.status == "claimed",
                    WorkerCommandJob.side_effect_started_at.is_(None),
                    WorkerCommandJob.lease_expires_at.is_not(None),
                    WorkerCommandJob.lease_expires_at <= claim_time,
                ),
            )
        )
        if allowed_command_types is not None:
            query = query.filter(
                WorkerCommandJob.command_type.in_(sorted(allowed_command_types))
            )
        rows = (
            query
            .order_by(WorkerCommandJob.created_at.asc(), WorkerCommandJob.id.asc())
            .limit(claim_limit)
            .all()
        )
        for row in rows:
            previous_status = str(row.status)
            previous_token = row.claim_token
            token = uuid4().hex
            conditions = [WorkerCommandJob.id == int(row.id)]
            if previous_status == "pending":
                conditions.append(WorkerCommandJob.status == "pending")
            else:
                conditions.extend(
                    [
                        WorkerCommandJob.status == "claimed",
                        WorkerCommandJob.claim_token == previous_token,
                        WorkerCommandJob.side_effect_started_at.is_(None),
                        WorkerCommandJob.lease_expires_at <= claim_time,
                    ]
                )
            attempt_count = int(row.attempt_count or 0) + 1
            updated = (
                session.query(WorkerCommandJob)
                .filter(*conditions)
                .update(
                    {
                        WorkerCommandJob.status: "claimed",
                        WorkerCommandJob.claim_token: token,
                        WorkerCommandJob.claimed_at: claim_time,
                        WorkerCommandJob.lease_expires_at: lease_expires_at,
                        WorkerCommandJob.attempt_count: attempt_count,
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                claims.append(
                    WorkerCommandClaim(
                        job_id=int(row.id),
                        command_id=str(row.command_id),
                        command_type=str(row.command_type),
                        request=json.loads(row.request_json),
                        request_fingerprint=str(row.request_fingerprint),
                        attempt_count=attempt_count,
                        claim_token=token,
                        claimed_at=claim_time,
                        lease_expires_at=lease_expires_at,
                    )
                )
        session.commit()
    return claims


def get_worker_command(
    session_factory, *, command_id: str
) -> WorkerCommandSnapshot | None:
    with session_factory() as session:
        row = (
            session.query(WorkerCommandJob)
            .filter(WorkerCommandJob.command_id == str(command_id))
            .one_or_none()
        )
        return _snapshot(row) if row is not None else None


def mark_worker_command_executing(
    session_factory,
    *,
    claim: WorkerCommandClaim,
    started_at: datetime | None = None,
) -> bool:
    """Commit the no-automatic-replay boundary before invoking an adapter."""

    execution_time = _naive_utc(started_at or utc_now())
    with session_factory() as session:
        updated = (
            session.query(WorkerCommandJob)
            .filter(
                WorkerCommandJob.id == int(claim.job_id),
                WorkerCommandJob.status == "claimed",
                WorkerCommandJob.claim_token == claim.claim_token,
                WorkerCommandJob.side_effect_started_at.is_(None),
            )
            .update(
                {
                    WorkerCommandJob.status: "executing",
                    WorkerCommandJob.side_effect_started_at: execution_time,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return updated == 1


def settle_worker_command_succeeded(
    session_factory,
    *,
    claim: WorkerCommandClaim,
    result: Any,
    http_status: int = 200,
    completed_at: datetime | None = None,
) -> bool:
    return _settle_worker_command(
        session_factory,
        claim=claim,
        status="succeeded",
        result=result,
        http_status=http_status,
        error_code=None,
        error_summary=None,
        completed_at=completed_at or utc_now(),
    )


def settle_worker_command_failed(
    session_factory,
    *,
    claim: WorkerCommandClaim,
    result: Any,
    http_status: int,
    error_code: str,
    error_summary: str,
    completed_at: datetime | None = None,
) -> bool:
    return _settle_worker_command(
        session_factory,
        claim=claim,
        status="failed",
        result=result,
        http_status=http_status,
        error_code=error_code,
        error_summary=error_summary,
        completed_at=completed_at or utc_now(),
    )


def mark_expired_executing_commands_uncertain(
    session_factory,
    *,
    uncertain_at: datetime | None = None,
) -> int:
    """Freeze expired post-boundary work instead of ever replaying it."""

    uncertainty_time = _naive_utc(uncertain_at or utc_now())
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        updated = (
            session.query(WorkerCommandJob)
            .filter(
                WorkerCommandJob.status == "executing",
                WorkerCommandJob.side_effect_started_at.is_not(None),
                WorkerCommandJob.lease_expires_at.is_not(None),
                WorkerCommandJob.lease_expires_at <= uncertainty_time,
            )
            .update(
                {
                    WorkerCommandJob.status: "uncertain",
                    WorkerCommandJob.claim_token: None,
                    WorkerCommandJob.claimed_at: None,
                    WorkerCommandJob.lease_expires_at: None,
                    WorkerCommandJob.error_code: (
                        "worker_lost_after_side_effect_boundary"
                    ),
                    WorkerCommandJob.error_summary: (
                        "worker lease expired after durable execution boundary"
                    ),
                    WorkerCommandJob.uncertain_at: uncertainty_time,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return int(updated)


def _settle_worker_command(
    session_factory,
    *,
    claim: WorkerCommandClaim,
    status: str,
    result: Any,
    http_status: int,
    error_code: str | None,
    error_summary: str | None,
    completed_at: datetime,
) -> bool:
    result_json = _canonical_result_json(result)
    if not 100 <= int(http_status) <= 599:
        raise WorkerCommandValidationError("http_status must be 100..599")
    normalized_error_code = (
        str(error_code)[:128] if error_code is not None else None
    )
    normalized_error_summary = (
        str(error_summary)[:MAX_ERROR_SUMMARY_CHARS]
        if error_summary is not None
        else None
    )
    with session_factory() as session:
        updated = (
            session.query(WorkerCommandJob)
            .filter(
                WorkerCommandJob.id == int(claim.job_id),
                WorkerCommandJob.status == "executing",
                WorkerCommandJob.claim_token == claim.claim_token,
            )
            .update(
                {
                    WorkerCommandJob.status: status,
                    WorkerCommandJob.claim_token: None,
                    WorkerCommandJob.claimed_at: None,
                    WorkerCommandJob.lease_expires_at: None,
                    WorkerCommandJob.http_status: int(http_status),
                    WorkerCommandJob.result_json: result_json,
                    WorkerCommandJob.error_code: normalized_error_code,
                    WorkerCommandJob.error_summary: normalized_error_summary,
                    WorkerCommandJob.completed_at: _naive_utc(completed_at),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return updated == 1


def _canonical_result_json(result: Any) -> str:
    secret_path = _find_secret_path(result, path="result")
    if secret_path is not None:
        raise WorkerCommandValidationError(
            f"result contains forbidden secret field: {secret_path}"
        )
    try:
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkerCommandValidationError("result_json is not canonical JSON") from exc
    if len(result_json.encode("utf-8")) > MAX_RESULT_JSON_BYTES:
        raise WorkerCommandValidationError("result_json exceeds durable size limit")
    return result_json


def canonical_worker_command_request(
    *, command_type: str, request: dict[str, Any]
) -> tuple[str, str]:
    if command_type not in WORKER_COMMAND_TYPES:
        raise WorkerCommandValidationError("unsupported command_type")
    if not isinstance(request, dict):
        raise WorkerCommandValidationError("request must be an object")
    secret_path = _find_secret_path(request)
    if secret_path is not None:
        raise WorkerCommandValidationError(
            f"request contains forbidden secret field: {secret_path}"
        )
    try:
        canonical_json = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkerCommandValidationError("request_json is not canonical JSON") from exc
    if len(canonical_json.encode("utf-8")) > MAX_REQUEST_JSON_BYTES:
        raise WorkerCommandValidationError("request_json exceeds durable size limit")
    fingerprint = hashlib.sha256(
        f"{command_type}\n{canonical_json}".encode("utf-8")
    ).hexdigest()
    return canonical_json, fingerprint


def _validated_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkerCommandValidationError("idempotency_key must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise WorkerCommandValidationError("idempotency_key exceeds durable size limit")
    return normalized


def _find_secret_path(value: Any, *, path: str = "request") -> str | None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if (
                normalized in _SECRET_FIELD_NAMES
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
                or normalized.endswith("_password")
                or normalized.endswith("_passphrase")
            ):
                return f"{path}.{key}"
            found = _find_secret_path(child, path=f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_secret_path(child, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _snapshot(row: WorkerCommandJob) -> WorkerCommandSnapshot:
    return WorkerCommandSnapshot(
        job_id=int(row.id),
        command_id=str(row.command_id),
        command_type=str(row.command_type),
        request=json.loads(row.request_json),
        request_fingerprint=str(row.request_fingerprint),
        idempotency_key=row.idempotency_key,
        status=str(row.status),
        attempt_count=int(row.attempt_count or 0),
        claim_token=row.claim_token,
        http_status=row.http_status,
        result=(json.loads(row.result_json) if row.result_json is not None else None),
        error_code=row.error_code,
        error_summary=row.error_summary,
        result_schema_version=int(row.result_schema_version),
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
