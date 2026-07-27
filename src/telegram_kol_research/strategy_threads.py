"""Repository helpers for durable strategy-thread identity."""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.models import (
    RawMessage,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyThread,
    utc_now,
)


ACTIVE_THREAD_STATUSES = frozenset({"active", "holding"})


def _detach(session: Session, row):
    session.expunge(row)
    return row


def create_strategy_thread_for_lifecycle(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int,
) -> StrategyThread:
    """Create one thread for an exact lifecycle without merging other rows."""

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, int(lifecycle_id))
        if lifecycle is None:
            raise LookupError("strategy lifecycle not found")
        if lifecycle.strategy_thread_id is not None:
            existing = session.get(StrategyThread, int(lifecycle.strategy_thread_id))
            if existing is not None:
                return _detach(session, existing)
        thread = (
            session.query(StrategyThread)
            .filter(
                StrategyThread.chat_id == int(lifecycle.chat_id),
                StrategyThread.root_message_id == int(lifecycle.message_id),
            )
            .one_or_none()
        )
        if thread is None:
            thread = StrategyThread(
                chat_id=int(lifecycle.chat_id),
                root_message_id=int(lifecycle.message_id),
                symbol=str(lifecycle.symbol).upper(),
                side=str(lifecycle.side).lower(),
                status="active",
                current_lifecycle_id=int(lifecycle.id),
            )
            session.add(thread)
            session.flush()
        elif thread.current_lifecycle_id is None:
            thread.current_lifecycle_id = int(lifecycle.id)
        lifecycle.strategy_thread_id = int(thread.id)
        lifecycle.updated_at = utc_now()
        session.commit()
        session.refresh(thread)
        return _detach(session, thread)


def link_message_to_strategy_thread(
    session_factory: sessionmaker,
    *,
    strategy_thread_id: int,
    raw_message_id: int,
    relation_kind: str,
    resolver: str,
    confidence: float,
    decision_version: str,
    message_evidence_version_id: int | None = None,
    evidence: dict | None = None,
) -> StrategyMessageLink:
    """Idempotently link one raw message to an exact strategy thread."""

    with session_factory() as session:
        if session.get(StrategyThread, int(strategy_thread_id)) is None:
            raise LookupError("strategy thread not found")
        if session.get(RawMessage, int(raw_message_id)) is None:
            raise LookupError("raw message not found")
        row = (
            session.query(StrategyMessageLink)
            .filter(
                StrategyMessageLink.strategy_thread_id == int(strategy_thread_id),
                StrategyMessageLink.raw_message_id == int(raw_message_id),
                StrategyMessageLink.relation_kind == str(relation_kind),
            )
            .one_or_none()
        )
        if row is None:
            row = StrategyMessageLink(
                strategy_thread_id=int(strategy_thread_id),
                raw_message_id=int(raw_message_id),
                message_evidence_version_id=message_evidence_version_id,
                relation_kind=str(relation_kind),
                resolver=str(resolver),
                confidence=min(1.0, max(0.0, float(confidence))),
                evidence_json=json.dumps(
                    evidence or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                decision_version=str(decision_version),
                status="active",
            )
            session.add(row)
        session.commit()
        session.refresh(row)
        return _detach(session, row)


def list_relevant_strategy_threads(
    session: Session,
    *,
    chat_id: int,
    statuses: Iterable[str] = ACTIVE_THREAD_STATUSES,
    limit: int = 50,
) -> list[StrategyThread]:
    """List bounded strategy threads for one source group."""

    allowed = tuple(dict.fromkeys(str(value) for value in statuses))
    if not allowed:
        return []
    return (
        session.query(StrategyThread)
        .filter(
            StrategyThread.chat_id == int(chat_id),
            StrategyThread.status.in_(allowed),
        )
        .order_by(StrategyThread.updated_at.desc(), StrategyThread.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
