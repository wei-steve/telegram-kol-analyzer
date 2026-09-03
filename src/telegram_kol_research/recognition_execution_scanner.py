"""Bounded, cursor-driven detection and fail-closed lease reconciliation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from sqlalchemy import and_, update
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.authoritative_execution_attempts import (
    fail_safe_authoritative_execution_attempt,
    finalize_recorded_authoritative_execution,
    mark_authoritative_execution_uncertain,
)
from telegram_kol_research.authoritative_execution_schema import (
    require_recognition_execution_schema,
)
from telegram_kol_research.entry_assembly_wakeup_executions import (
    fail_safe_wakeup,
    finalize_recorded_wakeup,
    mark_wakeup_uncertain,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    EntryAssemblyAttempt,
    EntryAssemblyWakeupExecution,
    MessageProcessingJob,
    RecognitionDecision,
    RecognitionExecutionScanCursor,
)
from telegram_kol_research.trading_settings import load_trading_settings


SCAN_FAMILIES = (
    "succeeded_job_running_decision",
    "legacy_running_decision",
    "legacy_claimed_wakeup_parent",
    "active_authoritative_attempt",
    "active_wakeup_execution",
)


@dataclass(frozen=True)
class RecognitionExecutionFinding:
    family: str
    row_id: int
    raw_message_id: int | None
    phase: str
    fingerprint: str
    action: str


def scan_recognition_execution_cycle(
    session_factory,
    *,
    runtime_role: str,
    now: datetime | None = None,
    limit: int = 100,
    owner_liveness: Callable[[object], bool | None] | None = None,
    incident_sink: Callable[[RecognitionExecutionFinding], None] | None = None,
) -> tuple[RecognitionExecutionFinding, ...]:
    """Scan each independent family once; web/ingest never touch cursors."""

    if runtime_role not in {"worker", "all"}:
        return ()
    require_recognition_execution_schema(session_factory)
    observed_at = now or datetime.now(UTC)
    page_size = max(1, min(int(limit), 1000))
    findings: list[RecognitionExecutionFinding] = []
    for family in SCAN_FAMILIES:
        try:
            family_findings = _scan_family(
                session_factory,
                family=family,
                now=observed_at,
                limit=page_size,
                owner_liveness=owner_liveness or owner_identity_is_alive,
            )
        except Exception:
            # Isolate scan families so a later query/cursor failure cannot
            # discard incidents already delivered for an earlier family.
            family_findings = [
                _finding(
                    family,
                    0,
                    None,
                    "scan",
                    "family_scan_raised",
                )
            ]
        findings.extend(family_findings)
        if incident_sink is not None:
            for finding in family_findings:
                incident_sink(finding)
    return tuple(findings)


def _scan_family(session_factory, *, family, now, limit, owner_liveness):
    cursor = _load_or_create_cursor(session_factory, family=family, now=now)
    with session_factory() as session:
        if family == "succeeded_job_running_decision":
            rows = (
                session.query(RecognitionDecision, MessageProcessingJob)
                .join(
                    MessageProcessingJob,
                    MessageProcessingJob.raw_message_id
                    == RecognitionDecision.raw_message_id,
                )
                .filter(
                    RecognitionDecision.id > cursor.last_seen_id,
                    RecognitionDecision.comparison_status.in_(
                        ("execution_running", "execution_uncertain")
                    ),
                    MessageProcessingJob.status == "succeeded",
                )
                .order_by(RecognitionDecision.id.asc())
                .limit(limit)
                .all()
            )
        elif family == "legacy_running_decision":
            rows = (
                session.query(RecognitionDecision)
                .outerjoin(
                    AuthoritativeExecutionAttempt,
                    and_(
                        AuthoritativeExecutionAttempt.raw_message_id
                        == RecognitionDecision.raw_message_id,
                        AuthoritativeExecutionAttempt.authoritative_generation
                        == RecognitionDecision.comparison_claim_token,
                    ),
                )
                .filter(
                    RecognitionDecision.id > cursor.last_seen_id,
                    RecognitionDecision.comparison_status == "execution_running",
                    AuthoritativeExecutionAttempt.id.is_(None),
                )
                .order_by(RecognitionDecision.id.asc())
                .limit(limit)
                .all()
            )
        elif family == "legacy_claimed_wakeup_parent":
            rows = (
                session.query(EntryAssemblyAttempt)
                .outerjoin(
                    EntryAssemblyWakeupExecution,
                    EntryAssemblyWakeupExecution.entry_assembly_attempt_id
                    == EntryAssemblyAttempt.id,
                )
                .filter(
                    EntryAssemblyAttempt.id > cursor.last_seen_id,
                    EntryAssemblyAttempt.status == "claimed",
                    EntryAssemblyWakeupExecution.id.is_(None),
                )
                .order_by(EntryAssemblyAttempt.id.asc())
                .limit(limit)
                .all()
            )
        elif family == "active_authoritative_attempt":
            rows = (
                session.query(AuthoritativeExecutionAttempt)
                .filter(
                    AuthoritativeExecutionAttempt.id > cursor.last_seen_id,
                    AuthoritativeExecutionAttempt.status.in_(
                        ("claimed", "executing", "outcome_recorded", "uncertain")
                    ),
                )
                .order_by(AuthoritativeExecutionAttempt.id.asc())
                .limit(limit)
                .all()
            )
        else:
            rows = (
                session.query(EntryAssemblyWakeupExecution)
                .filter(
                    EntryAssemblyWakeupExecution.id > cursor.last_seen_id,
                    EntryAssemblyWakeupExecution.status.in_(
                        ("claimed", "executing", "outcome_recorded", "uncertain")
                    ),
                )
                .order_by(EntryAssemblyWakeupExecution.id.asc())
                .limit(limit)
                .all()
            )
        session.expunge_all()
    if not rows:
        _wrap_cursor(session_factory, cursor=cursor, now=now)
        return []
    findings = []
    for item in rows:
        row = item[0] if family == "succeeded_job_running_decision" else item
        try:
            finding = _inspect_row(
                session_factory,
                family=family,
                row=row,
                now=now,
                owner_liveness=owner_liveness,
            )
        except Exception:
            # A poison row must become an incident and advance its cursor;
            # otherwise later rows in this scan family can starve forever.
            raw_id = getattr(row, "raw_message_id", None) or getattr(
                row, "strategy_raw_message_id", None
            )
            finding = _finding(
                family,
                int(row.id),
                int(raw_id) if raw_id is not None else None,
                str(getattr(row, "status", None) or getattr(
                    row, "comparison_status", "unknown"
                )),
                "inspection_raised",
            )
        if finding is not None:
            findings.append(finding)
        cursor = _advance_cursor(
            session_factory, cursor=cursor, last_seen_id=int(row.id), now=now
        )
    return findings


def _inspect_row(session_factory, *, family, row, now, owner_liveness):
    if (
        family == "succeeded_job_running_decision"
        and row.comparison_status == "execution_running"
    ):
        with session_factory() as session:
            active_attempt = (
                session.query(AuthoritativeExecutionAttempt)
                .filter(
                    AuthoritativeExecutionAttempt.raw_message_id
                    == int(row.raw_message_id),
                    AuthoritativeExecutionAttempt.authoritative_generation
                    == str(row.comparison_claim_token),
                    AuthoritativeExecutionAttempt.status.in_(
                        ("claimed", "executing", "outcome_recorded")
                    ),
                )
                .first()
            )
        # A prior message job may already be succeeded while a later context
        # reanalysis legitimately owns the same decision projection. The
        # active-attempt family owns liveness/lease inspection for this row;
        # the succeeded-job family must not double-report it as an orphan.
        if active_attempt is not None:
            return None
    if family in {
        "succeeded_job_running_decision",
        "legacy_running_decision",
        "legacy_claimed_wakeup_parent",
    }:
        raw_id = getattr(row, "raw_message_id", None) or getattr(
            row, "strategy_raw_message_id", None
        )
        return _finding(
            family,
            int(row.id),
            int(raw_id),
            str(
                getattr(row, "comparison_status", None)
                or getattr(row, "status", "unknown")
            ),
            "observe_only",
        )
    alive = owner_liveness(row)
    expired = _as_utc(row.lease_expires_at) <= _as_utc(now)
    action: str | None = None
    if row.status == "outcome_recorded" and expired and alive is False:
        try:
            if family == "active_authoritative_attempt":
                finalized = finalize_recorded_authoritative_execution(
                    session_factory,
                    attempt_id=int(row.id),
                    claim_token=str(row.claim_token),
                    semantic_review_enabled=load_trading_settings(
                        session_factory
                    ).semantic_review_enabled,
                    finalized_at=now,
                )
            else:
                finalized = finalize_recorded_wakeup(
                    session_factory,
                    child_execution_id=int(row.id),
                    claim_token=str(row.claim_token),
                    finalized_at=now,
                )
        except Exception:
            action = "finalize_raised"
        else:
            action = "finalized_locally" if finalized else "finalize_cas_failed"
    elif row.status == "outcome_recorded" and expired and alive is True:
        action = "expired_owner_still_alive"
    elif row.status == "outcome_recorded" and expired:
        action = "expired_owner_liveness_unknown"
    elif row.status == "outcome_recorded" and alive is False:
        action = "owner_not_alive_lease_active"
    elif row.status == "outcome_recorded":
        return None
    elif row.status == "uncertain":
        action = "observe_uncertain"
    elif expired and alive is False:
        error = RuntimeError("expired owner identity is no longer alive")
        if family == "active_authoritative_attempt" and row.status == "claimed":
            changed = fail_safe_authoritative_execution_attempt(
                session_factory,
                attempt_id=int(row.id),
                claim_token=str(row.claim_token),
                failed_at=now,
                error_class="ExpiredOwner",
                error_summary=str(error),
            )
            action = "failed_safe" if changed else "terminalize_cas_failed"
        elif family == "active_authoritative_attempt":
            changed = mark_authoritative_execution_uncertain(
                session_factory,
                attempt_id=int(row.id),
                claim_token=str(row.claim_token),
                uncertain_at=now,
                error_class="ExpiredOwner",
                error_summary=str(error),
            )
            action = "marked_uncertain" if changed else "terminalize_cas_failed"
        elif row.status == "claimed":
            changed = fail_safe_wakeup(
                session_factory,
                child_execution_id=int(row.id),
                claim_token=str(row.claim_token),
                failed_at=now,
                error=error,
            )
            action = "failed_safe" if changed else "terminalize_cas_failed"
        else:
            changed = mark_wakeup_uncertain(
                session_factory,
                child_execution_id=int(row.id),
                claim_token=str(row.claim_token),
                uncertain_at=now,
                error=error,
            )
            action = "marked_uncertain" if changed else "terminalize_cas_failed"
    elif expired and alive is True:
        action = "expired_owner_still_alive"
    elif expired:
        action = "expired_owner_liveness_unknown"
    elif alive is False:
        # Liveness proof may arrive before the lease expires. Preserve the
        # lease and report only; reclamation waits for both predicates.
        action = "owner_not_alive_lease_active"
    else:
        return None
    raw_id = getattr(row, "raw_message_id", None) or getattr(
        row, "strategy_raw_message_id", None
    )
    return _finding(family, int(row.id), int(raw_id), str(row.status), action)


def _finding(family, row_id, raw_message_id, phase, action):
    stable = f"{family}\0{row_id}\0{raw_message_id}\0{phase}\0{action}"
    return RecognitionExecutionFinding(
        family=family,
        row_id=row_id,
        raw_message_id=raw_message_id,
        phase=phase,
        fingerprint=hashlib.sha256(stable.encode()).hexdigest(),
        action=action,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class _Cursor:
    id: int
    last_seen_id: int
    version: int
    pass_generation: int


def _load_or_create_cursor(session_factory, *, family, now):
    with session_factory() as session:
        row = session.query(RecognitionExecutionScanCursor).filter_by(
            scan_family=family
        ).one_or_none()
        if row is None:
            row = RecognitionExecutionScanCursor(
                scan_family=family,
                last_seen_id=0,
                pass_generation=1,
                pass_started_at=now,
                version=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.query(RecognitionExecutionScanCursor).filter_by(
                    scan_family=family
                ).one()
        return _Cursor(int(row.id), int(row.last_seen_id), int(row.version), int(row.pass_generation))


def _advance_cursor(session_factory, *, cursor, last_seen_id, now):
    with session_factory() as session:
        result = session.execute(
            update(RecognitionExecutionScanCursor)
            .where(
                RecognitionExecutionScanCursor.id == cursor.id,
                RecognitionExecutionScanCursor.version == cursor.version,
            )
            .values(
                last_seen_id=last_seen_id,
                version=cursor.version + 1,
                updated_at=now,
            )
        )
        session.commit()
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("recognition_execution_scan_cursor_cas_failed")
    return _Cursor(cursor.id, last_seen_id, cursor.version + 1, cursor.pass_generation)


def _wrap_cursor(session_factory, *, cursor, now):
    with session_factory() as session:
        result = session.execute(
            update(RecognitionExecutionScanCursor)
            .where(
                RecognitionExecutionScanCursor.id == cursor.id,
                RecognitionExecutionScanCursor.version == cursor.version,
            )
            .values(
                last_seen_id=0,
                pass_generation=cursor.pass_generation + 1,
                pass_started_at=now,
                wrapped_at=now,
                version=cursor.version + 1,
                updated_at=now,
            )
        )
        session.commit()
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("recognition_execution_scan_cursor_cas_failed")


def owner_identity_is_alive(row) -> bool | None:
    """Return False only from boot+PID+start-tick proof; missing data is unknown."""

    if str(getattr(row, "owner_boot_id", "")) in {"", "missing", "unavailable"}:
        return None
    if str(getattr(row, "owner_process_start_ticks", "")) in {
        "",
        "missing",
        "unavailable",
    }:
        return None
    try:
        if int(row.owner_pid) <= 0:
            return None
    except (TypeError, ValueError):
        return None

    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        current_boot = boot_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if current_boot != str(row.owner_boot_id):
        return False
    try:
        stat = Path(f"/proc/{int(row.owner_pid)}/stat").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    tail = stat.rsplit(")", 1)[-1].strip().split()
    if len(tail) < 20:
        return None
    return tail[19] == str(row.owner_process_start_ticks)
