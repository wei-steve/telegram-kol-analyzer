"""Durable, fail-closed reanalysis of unresolved contextual decisions."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyThread,
)


EVENT_TRIGGER_MAP = {
    "reply_target_available": "reply_target_available",
    "entry_leg_status_changed": "strategy_state_changed",
    "exchange_snapshot_changed": "exchange_state_changed",
    "message_edited": "message_edited",
    "evidence_version_changed": "evidence_version_changed",
}
UNRESOLVED_DECISIONS = frozenset({"unresolved", "hold"})
TERMINAL_INSTRUCTION_STATUSES = frozenset(
    {"submitted", "submit_unknown", "succeeded", "unknown", "reconciled"}
)
DEFAULT_STALE_AFTER = timedelta(minutes=5)
DEFAULT_RETRY_DELAY = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class ContextReanalysisClaim:
    attempt_id: int
    raw_message_id: int
    context_fingerprint: str
    token: str
    trigger_event: dict[str, Any]


def build_context_state_fingerprint(
    session_factory: sessionmaker,
    raw_message_id: int,
    *,
    candidate_thread_ids: set[int] | None = None,
) -> str:
    """Fingerprint only durable context that may change a second decision."""

    with session_factory() as session:
        raw = session.get(RawMessage, int(raw_message_id))
        if raw is None:
            raise LookupError("raw message not found")
        latest_same_chat = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id == raw.chat_id)
            .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
            .first()
        )
        evidence = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == raw.id,
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
        links = (
            session.query(
                StrategyMessageLink.strategy_thread_id,
                StrategyMessageLink.relation_kind,
                StrategyMessageLink.status,
                StrategyThread.status,
                StrategyThread.current_lifecycle_id,
            )
            .join(
                StrategyThread,
                StrategyThread.id == StrategyMessageLink.strategy_thread_id,
            )
            .filter(StrategyMessageLink.raw_message_id == raw.id)
            .order_by(StrategyMessageLink.id.asc())
            .all()
        )
        resolved_thread_ids = {
            int(row.strategy_thread_id) for row in links
        }
        if candidate_thread_ids is None:
            latest_attempt = (
                session.query(ContextResolutionAttempt.request_summary_json)
                .filter(ContextResolutionAttempt.raw_message_id == raw.id)
                .order_by(ContextResolutionAttempt.id.desc())
                .first()
            )
            candidate_thread_ids = _collect_candidate_thread_ids(
                _json_dict(latest_attempt[0]) if latest_attempt is not None else {}
            )
        resolved_thread_ids.update(int(value) for value in candidate_thread_ids)
        candidate_threads = (
            session.query(StrategyThread)
            .filter(StrategyThread.id.in_(sorted(resolved_thread_ids)))
            .order_by(StrategyThread.id.asc())
            .all()
            if resolved_thread_ids
            else []
        )
        lifecycle_ids = {
            int(row.current_lifecycle_id)
            for row in candidate_threads
            if row.current_lifecycle_id is not None
        }
        lifecycle_states = (
            session.query(
                StrategyLifecycle.id,
                StrategyLifecycle.lifecycle_status,
                StrategyLifecycle.management_action,
                StrategyLifecycle.updated_at,
            )
            .filter(StrategyLifecycle.id.in_(sorted(lifecycle_ids)))
            .order_by(StrategyLifecycle.id.asc())
            .all()
            if lifecycle_ids
            else []
        )
        payload = {
            "raw": [
                raw.id,
                raw.message_id,
                raw.reply_to_message_id,
                raw.text,
                raw.edit_date.isoformat() if raw.edit_date else None,
            ],
            "latest_same_chat": (
                [latest_same_chat.id, latest_same_chat.message_id]
                if latest_same_chat is not None
                else None
            ),
            "evidence": (
                [evidence.id, evidence.version, evidence.input_fingerprint]
                if evidence is not None
                else None
            ),
            "links": [list(row) for row in links],
            "candidate_threads": [
                [
                    row.id,
                    row.status,
                    row.current_lifecycle_id,
                    row.updated_at.isoformat() if row.updated_at else None,
                ]
                for row in candidate_threads
            ],
            "lifecycle_states": [
                [
                    row.id,
                    row.lifecycle_status,
                    row.management_action,
                    row.updated_at.isoformat() if row.updated_at else None,
                ]
                for row in lifecycle_states
            ],
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _collect_candidate_thread_ids(value: Any) -> set[int]:
    found: set[int] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"thread_id", "strategy_thread_id"}:
                try:
                    found.add(int(item))
                except (TypeError, ValueError):
                    pass
            found.update(_collect_candidate_thread_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_candidate_thread_ids(item))
    return found


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _is_unresolved(attempt: ContextResolutionAttempt) -> bool:
    return str(_json_dict(attempt.decision_json).get("decision") or "") in (
        UNRESOLVED_DECISIONS
    )


def schedule_context_reanalysis(
    session_factory: sessionmaker,
    *,
    event_type: str,
    occurred_at: datetime,
    raw_message_id: int | None = None,
    chat_id: int | None = None,
) -> int:
    """Schedule only unresolved rows whose declared trigger has occurred."""

    normalized_event = str(event_type)
    trigger = EVENT_TRIGGER_MAP.get(normalized_event)
    if normalized_event != "next_same_chat_message" and trigger is None:
        return 0
    with session_factory() as session:
        query = session.query(ContextResolutionAttempt).filter(
            ContextResolutionAttempt.status.in_(
                ("completed", "pending_reanalysis")
            )
        )
        if raw_message_id is not None:
            query = query.filter(
                ContextResolutionAttempt.raw_message_id == int(raw_message_id)
            )
        if chat_id is not None:
            query = query.join(
                RawMessage,
                RawMessage.id == ContextResolutionAttempt.raw_message_id,
            ).filter(RawMessage.chat_id == int(chat_id))
        rows = query.order_by(ContextResolutionAttempt.id.asc()).all()
        scheduled = 0
        for row in rows:
            if not _is_unresolved(row):
                continue
            declared = set(_json_list(row.reanalysis_triggers_json))
            if normalized_event != "next_same_chat_message" and trigger not in declared:
                continue
            if normalized_event == "next_same_chat_message" and not declared:
                continue
            row.status = "pending_reanalysis"
            row.next_attempt_at = occurred_at
            row.trigger_event_json = json.dumps(
                {
                    "event_type": normalized_event,
                    "trigger": trigger or "next_same_chat_message",
                    "occurred_at": occurred_at.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            row.updated_at = occurred_at
            scheduled += 1
        session.commit()
        return scheduled


def _claimable(now: datetime, stale_before: datetime):
    return or_(
        and_(
            ContextResolutionAttempt.status == "pending_reanalysis",
            or_(
                ContextResolutionAttempt.next_attempt_at.is_(None),
                ContextResolutionAttempt.next_attempt_at <= now,
            ),
        ),
        and_(
            ContextResolutionAttempt.status == "running",
            ContextResolutionAttempt.claimed_at <= stale_before,
        ),
    )


def claim_next_context_reanalysis(
    session_factory: sessionmaker,
    *,
    now: datetime,
    stale_before: datetime,
) -> ContextReanalysisClaim | None:
    """Atomically claim one generation; concurrent workers cannot share it."""

    with session_factory() as session:
        while True:
            attempt_id = session.execute(
                session.query(ContextResolutionAttempt.id)
                .filter(_claimable(now, stale_before))
                .order_by(
                    ContextResolutionAttempt.next_attempt_at,
                    ContextResolutionAttempt.id,
                )
                .limit(1)
                .statement
            ).scalar_one_or_none()
            if attempt_id is None:
                return None
            token = uuid4().hex
            result = session.execute(
                update(ContextResolutionAttempt)
                .where(
                    ContextResolutionAttempt.id == int(attempt_id),
                    _claimable(now, stale_before),
                )
                .values(
                    status="running",
                    claim_token=token,
                    claimed_at=now,
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount == 1:
                session.commit()
                row = session.get(ContextResolutionAttempt, int(attempt_id))
                return ContextReanalysisClaim(
                    attempt_id=int(row.id),
                    raw_message_id=int(row.raw_message_id),
                    context_fingerprint=str(
                        row.state_fingerprint or row.context_fingerprint
                    ),
                    token=token,
                    trigger_event=_json_dict(row.trigger_event_json),
                )
            session.rollback()


def _has_terminal_instruction(session_factory, raw_message_id: int) -> bool:
    with session_factory() as session:
        return (
            session.query(MessageInstructionItem.id)
            .filter(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.status.in_(
                    sorted(TERMINAL_INSTRUCTION_STATUSES)
                ),
            )
            .first()
            is not None
        )


def _finish_claim(
    session_factory,
    *,
    claim: ContextReanalysisClaim,
    status: str,
    now: datetime,
    last_error: str | None = None,
    next_attempt_at: datetime | None = None,
    increment_attempts: bool = False,
) -> bool:
    values: dict[str, Any] = {
        "status": status,
        "claim_token": None,
        "claimed_at": None,
        "next_attempt_at": next_attempt_at,
        "last_error": last_error,
        "updated_at": now,
    }
    if increment_attempts:
        values["attempts"] = ContextResolutionAttempt.attempts + 1
    with session_factory() as session:
        result = session.execute(
            update(ContextResolutionAttempt)
            .where(
                ContextResolutionAttempt.id == claim.attempt_id,
                ContextResolutionAttempt.status == "running",
                ContextResolutionAttempt.claim_token == claim.token,
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def run_context_resolution_once(
    session_factory: sessionmaker,
    *,
    context_fingerprint_factory: Callable[[int], str],
    reanalyze: Callable[[int, str], dict[str, Any]],
    now: datetime | None = None,
    max_attempts: int = 3,
    notify_final_failure: Callable[[dict[str, Any]], Any] | None = None,
    is_eligible: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Process at most one item without directly authorizing exchange writes."""

    current = now or datetime.now(UTC)
    claim = claim_next_context_reanalysis(
        session_factory,
        now=current,
        stale_before=current - DEFAULT_STALE_AFTER,
    )
    if claim is None:
        return {"status": "idle"}
    if is_eligible is not None and not is_eligible(claim.raw_message_id):
        _finish_claim(
            session_factory,
            claim=claim,
            status="blocked_disabled",
            now=current,
        )
        return {
            "status": "blocked_disabled",
            "raw_message_id": claim.raw_message_id,
        }
    if _has_terminal_instruction(session_factory, claim.raw_message_id):
        _finish_claim(
            session_factory,
            claim=claim,
            status="blocked_execution_terminal",
            now=current,
        )
        return {
            "status": "blocked_execution_terminal",
            "raw_message_id": claim.raw_message_id,
        }
    fingerprint = str(context_fingerprint_factory(claim.raw_message_id))
    if (
        fingerprint == claim.context_fingerprint
        and claim.trigger_event.get("event_type") == "exchange_snapshot_changed"
    ):
        fingerprint = (
            fingerprint
            + ":exchange:"
            + hashlib.sha256(
                json.dumps(
                    claim.trigger_event,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
    if fingerprint == claim.context_fingerprint:
        _finish_claim(
            session_factory,
            claim=claim,
            status="completed",
            now=current,
        )
        return {
            "status": "context_unchanged",
            "raw_message_id": claim.raw_message_id,
        }
    try:
        outcome = reanalyze(claim.raw_message_id, fingerprint)
    except Exception as exc:
        with session_factory() as session:
            row = session.get(ContextResolutionAttempt, claim.attempt_id)
            attempt_count = int(row.attempts or 0)
        exhausted = attempt_count >= int(max_attempts)
        _finish_claim(
            session_factory,
            claim=claim,
            status="exhausted" if exhausted else "pending_reanalysis",
            now=current,
            last_error=type(exc).__name__,
            next_attempt_at=(
                None if exhausted else current + DEFAULT_RETRY_DELAY
            ),
            increment_attempts=not exhausted,
        )
        if exhausted and notify_final_failure is not None:
            should_notify = False
            with session_factory() as session:
                row = session.get(ContextResolutionAttempt, claim.attempt_id)
                if row.exhausted_notified_at is None:
                    row.exhausted_notified_at = current
                    session.commit()
                    should_notify = True
            if should_notify:
                notify_final_failure(
                    {
                        "raw_message_id": claim.raw_message_id,
                        "reason": "context_reanalysis_exhausted",
                    }
                )
        return {
            "status": "exhausted" if exhausted else "retry_scheduled",
            "raw_message_id": claim.raw_message_id,
        }
    _finish_claim(
        session_factory,
        claim=claim,
        status="superseded",
        now=current,
    )
    return {
        "status": str(outcome.get("status") or "completed"),
        "raw_message_id": claim.raw_message_id,
        "context_fingerprint": fingerprint,
    }
