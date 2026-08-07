"""Query helpers for the Telegram web workbench."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Iterable

from sqlalchemy import func, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionOrderLeg,
    ExecutionEvent,
    EntryRevisionReplacement,
    MediaAsset,
    MessageRecognition,
    MessageEvidenceVersion,
    RawMessage,
    RecognitionDecision,
    PositionAttributionAudit,
    PositionProtectionLedger,
    RecognitionExperiment,
    SignalCandidate,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyThread,
    StrategyManagementBatch,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    utc_now,
)
from telegram_kol_research.time_utils import normalize_to_utc_naive, utc_naive_to_local
from telegram_kol_research.reporting import (
    format_entry_assembly_summary,
    format_entry_revision_summary,
)

STRATEGY_TIME_DISPLAY_FIELDS = (
    "posted_at",
    "signal_at",
    "entered_at",
    "exited_at",
    "last_checked_at",
    "original_posted_at",
    "latest_event_at",
)
MAX_MEDIA_ASSETS_PER_MESSAGE = 3


def _safe_json_dict(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_entry_preamble_binding(
    binding: ExecutionBinding,
) -> dict[str, object] | None:
    payload = _safe_json_dict(binding.payload_json)
    draft = payload.get("draft")
    evidence = (
        draft.get("entry_preamble_assembly")
        if isinstance(draft, dict)
        else None
    )
    if evidence is None:
        evidence = payload.get("entry_preamble_assembly")
    return format_entry_assembly_summary(evidence)


def _serialize_entry_revision_batch(
    batch: StrategyRevisionBatch,
    *,
    replacement_count: int,
    confirmed_change_count: int,
) -> dict[str, object] | None:
    return format_entry_revision_summary(
        {
            "status": batch.status,
            "reason_code": batch.reason_code,
            "replacement_count": replacement_count,
            "confirmed_change_count": confirmed_change_count,
            "market_snapshot": _safe_json_dict(batch.market_snapshot_json),
        }
    )


def load_home_event_rows(
    session_factory: sessionmaker,
    *,
    limit: int = 50,
    kinds: set[str] | None = None,
) -> list[dict[str, object]]:
    """Return a small, normalized feed without duplicating source state."""

    selected = kinds or {"message", "strategy", "execution"}
    rows: list[dict[str, object]] = []
    with session_factory() as session:
        if "message" in selected:
            messages = (
                session.query(RawMessage)
                .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
                .limit(limit)
                .all()
            )
            rows.extend(
                {
                    "id": f"message:{message.chat_id}:{message.message_id}",
                    "kind": "message",
                    "occurred_at": message.posted_at or message.created_at,
                    "source_label": message.sender_name or str(message.chat_id),
                    "title": "收到新消息",
                    "summary": (message.text or "图片或媒体消息").strip()[:160],
                    "symbol": None,
                    "side": None,
                    "status": "received",
                    "destination": {
                        "view": "messages",
                        "chat_id": message.chat_id,
                        "message_id": message.message_id,
                    },
                }
                for message in messages
            )
        if "strategy" in selected:
            strategies = (
                session.query(StrategyLifecycle)
                .order_by(StrategyLifecycle.updated_at.desc(), StrategyLifecycle.id.desc())
                .limit(limit)
                .all()
            )
            rows.extend(
                {
                    "id": f"strategy:{strategy.id}",
                    "kind": "strategy",
                    "occurred_at": (
                        strategy.exited_at
                        or strategy.entered_at
                        or strategy.signal_at
                    ),
                    "source_label": str(strategy.chat_id),
                    "title": "策略状态更新",
                    "summary": f"{strategy.symbol} {strategy.side} · {strategy.lifecycle_status}",
                    "symbol": strategy.symbol,
                    "side": strategy.side,
                    "status": strategy.lifecycle_status,
                    "destination": {
                        "view": "strategies",
                        "chat_id": strategy.chat_id,
                        "message_id": strategy.message_id,
                    },
                }
                for strategy in strategies
            )
        if "execution" in selected:
            executions = (
                session.query(ExecutionEvent)
                .order_by(ExecutionEvent.created_at.desc(), ExecutionEvent.id.desc())
                .limit(limit)
                .all()
            )
            rows.extend(
                {
                    "id": f"execution:{event.id}",
                    "kind": "execution",
                    "occurred_at": event.created_at,
                    "source_label": event.kol_id or event.venue,
                    "title": "交易执行更新",
                    "summary": event.reason or event.action.replace("_", " "),
                    "symbol": event.symbol,
                    "side": event.side,
                    "status": event.status,
                    "destination": {
                        "view": "positions",
                        "pos_id": event.pos_id,
                        "order_id": event.order_id,
                    },
                }
                for event in executions
            )
    rows.sort(key=lambda row: row["occurred_at"], reverse=True)
    return rows[: max(0, limit)]


def _format_strategy_time(value: object) -> str | None:
    if value is None or not isinstance(value, datetime):
        return None
    local_value = utc_naive_to_local(value)
    if local_value is None:
        return None
    offset = local_value.strftime("%z")
    timezone_label = "UTC"
    if offset:
        sign = offset[0]
        hours = int(offset[1:3])
        minutes = int(offset[3:5])
        timezone_label = f"UTC{sign}{hours}"
        if minutes:
            timezone_label = f"{timezone_label}:{minutes:02d}"
    return f"{local_value:%Y-%m-%d %H:%M:%S} {timezone_label}"


def _add_strategy_time_display_fields(row: dict[str, object]) -> dict[str, object]:
    for field_name in STRATEGY_TIME_DISPLAY_FIELDS:
        row[f"{field_name}_display"] = _format_strategy_time(row.get(field_name))
    return row


def load_group_rows(
    session_factory: sessionmaker,
    *,
    group_labels_by_title: dict[str, str] | None = None,
    configured_groups: Iterable[object] | None = None,
) -> list[dict[str, int | str | datetime | None | bool]]:
    """Load aggregated group rows ordered by most recent activity."""

    label_map = group_labels_by_title or {}
    configured_by_chat_id = {
        int(getattr(group, "chat_id")): group
        for group in configured_groups or []
        if getattr(group, "chat_id", None) is not None
    }
    with session_factory() as session:
        # Single-pass: aggregate counts + latest sender via window function
        latest_sub = (
            session.query(
                RawMessage.chat_id,
                RawMessage.sender_name,
                func.row_number()
                .over(
                    partition_by=RawMessage.chat_id,
                    order_by=[RawMessage.posted_at.desc(), RawMessage.message_id.desc()],
                )
                .label("rn"),
            )
            .subquery()
        )
        rows = (
            session.query(
                RawMessage.chat_id.label("chat_id"),
                func.max(RawMessage.posted_at).label("last_posted_at"),
                func.count(RawMessage.id).label("message_count"),
                latest_sub.c.sender_name.label("latest_sender"),
            )
            .outerjoin(
                latest_sub,
                (latest_sub.c.chat_id == RawMessage.chat_id) & (latest_sub.c.rn == 1),
            )
            .group_by(RawMessage.chat_id)
            .order_by(func.max(RawMessage.posted_at).desc(), RawMessage.chat_id.desc())
            .all()
        )

        results: list[dict[str, int | str | datetime | None | bool]] = []
        seen_chat_ids: set[int] = set()
        for row in rows:
            seen_chat_ids.add(int(row.chat_id))
            raw_title = row.latest_sender or str(row.chat_id)
            configured_group = configured_by_chat_id.get(int(row.chat_id))
            if configured_group is not None:
                raw_title = str(getattr(configured_group, "chat_title", raw_title))
            results.append(
                {
                    "chat_id": row.chat_id,
                    "title": label_map.get(raw_title, raw_title),
                    "raw_title": raw_title,
                    "last_posted_at": utc_naive_to_local(row.last_posted_at),
                    "message_count": row.message_count,
                    "has_media": False,
                }
            )
        for group in configured_groups or []:
            chat_id = getattr(group, "chat_id", None)
            if chat_id is None or int(chat_id) in seen_chat_ids:
                continue
            title = str(getattr(group, "chat_title", chat_id))
            results.append(
                {
                    "chat_id": int(chat_id),
                    "title": label_map.get(title, title),
                    "raw_title": title,
                    "last_posted_at": None,
                    "message_count": 0,
                    "has_media": False,
                }
            )
    return results


def load_database_freshness(
    session_factory: sessionmaker,
    *,
    now: datetime,
) -> dict[str, datetime | float | None]:
    """Summarize how stale the local database snapshot is."""

    with session_factory() as session:
        latest_message_at = session.query(func.max(RawMessage.posted_at)).scalar()

    stale_hours = None
    if latest_message_at is not None:
        now_utc = normalize_to_utc_naive(now)
        latest_utc = (
            latest_message_at
            if latest_message_at.tzinfo is None
            else normalize_to_utc_naive(latest_message_at)
        )
        stale_hours = round((now_utc - latest_utc).total_seconds() / 3600, 1)

    return {
        "latest_message_at": utc_naive_to_local(latest_message_at),
        "stale_hours": stale_hours,
    }


def _group_messages_query(
    session,
    *,
    chat_id: int,
    before_message_id: int | None = None,
    search_text: str | None = None,
    sender_name: str | None = None,
):
    query = session.query(RawMessage).filter(RawMessage.chat_id == chat_id)
    if before_message_id is not None:
        query = query.filter(RawMessage.message_id < before_message_id)
    if search_text:
        search_value = f"%{search_text.strip()}%"
        query = query.filter(
            or_(
                RawMessage.text.ilike(search_value),
                RawMessage.sender_name.ilike(search_value),
            )
        )
    if sender_name:
        sender_value = f"%{sender_name.strip()}%"
        query = query.filter(RawMessage.sender_name.ilike(sender_value))
    return query.order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())


def load_group_messages(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    limit: int,
    before_message_id: int | None = None,
    search_text: str | None = None,
    sender_name: str | None = None,
) -> list[dict[str, object | None]]:
    """Load message timeline rows for a single group."""

    with session_factory() as session:
        raw_messages = (
            _group_messages_query(
                session,
                chat_id=chat_id,
                before_message_id=before_message_id,
                search_text=search_text,
                sender_name=sender_name,
            )
            .limit(limit)
            .all()
        )

        return _serialize_raw_messages(session, raw_messages)


def load_group_message_page(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    page_size: int,
    before_message_id: int | None = None,
    search_text: str | None = None,
    sender_name: str | None = None,
) -> tuple[list[dict[str, object | None]], bool]:
    """Load one message page and report whether an older matching page exists."""

    with session_factory() as session:
        raw_messages = (
            _group_messages_query(
                session,
                chat_id=chat_id,
                before_message_id=before_message_id,
                search_text=search_text,
                sender_name=sender_name,
            )
            .limit(page_size + 1)
            .all()
        )
        has_more = len(raw_messages) > page_size
        return _serialize_raw_messages(session, raw_messages[:page_size]), has_more


def load_selected_messages(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    raw_message_ids: Iterable[int],
) -> list[dict[str, object | None]]:
    """Load a specific set of messages for selected-scope analysis."""

    selected_ids = [int(value) for value in raw_message_ids]
    if not selected_ids:
        return []

    with session_factory() as session:
        raw_messages = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == chat_id,
                RawMessage.id.in_(selected_ids),
            )
            .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
            .all()
        )
        return _serialize_raw_messages(session, raw_messages)


def load_messages_in_time_window(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    posted_after: datetime | None,
    posted_before: datetime | None,
    limit: int,
) -> list[dict[str, object | None]]:
    """Load messages constrained to a time window."""

    with session_factory() as session:
        query = session.query(RawMessage).filter(RawMessage.chat_id == chat_id)
        if posted_after is not None:
            query = query.filter(RawMessage.posted_at >= normalize_to_utc_naive(posted_after))
        if posted_before is not None:
            query = query.filter(RawMessage.posted_at <= normalize_to_utc_naive(posted_before))
        raw_messages = (
            query.order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
            .limit(limit)
            .all()
        )
        return _serialize_raw_messages(session, raw_messages)


def _serialize_raw_messages(
    session,
    raw_messages: list[RawMessage],
) -> list[dict[str, object | None]]:
    if not raw_messages:
        return []

    raw_message_ids = [msg.id for msg in raw_messages]

    # ── Bulk-load media assets ──
    ranked_media = (
        session.query(
            MediaAsset.id.label("id"),
            func.row_number()
            .over(
                partition_by=MediaAsset.raw_message_id,
                order_by=[
                    MediaAsset.ocr_text.is_(None),
                    MediaAsset.local_path.is_(None),
                    MediaAsset.id.asc(),
                ],
            )
            .label("rn"),
        )
        .filter(MediaAsset.raw_message_id.in_(raw_message_ids))
        .subquery()
    )
    all_media = (
        session.query(MediaAsset)
        .join(ranked_media, MediaAsset.id == ranked_media.c.id)
        .filter(ranked_media.c.rn <= MAX_MEDIA_ASSETS_PER_MESSAGE)
        .order_by(MediaAsset.raw_message_id.asc(), ranked_media.c.rn.asc())
        .all()
    )
    media_by_msg_id: dict[int, list[MediaAsset]] = {}
    for m in all_media:
        media_by_msg_id.setdefault(m.raw_message_id, []).append(m)

    # ── Bulk-load recognitions ──
    all_recs = (
        session.query(MessageRecognition)
        .filter(MessageRecognition.raw_message_id.in_(raw_message_ids))
        .all()
    )
    rec_by_msg_id: dict[int, MessageRecognition] = {r.raw_message_id: r for r in all_recs}

    all_decisions = (
        session.query(RecognitionDecision)
        .filter(RecognitionDecision.raw_message_id.in_(raw_message_ids))
        .all()
    )
    decisions_by_msg_id: dict[int, RecognitionDecision] = {
        decision.raw_message_id: decision for decision in all_decisions
    }

    all_management_batches = (
        session.query(StrategyManagementBatch)
        .filter(StrategyManagementBatch.raw_message_id.in_(raw_message_ids))
        .order_by(
            StrategyManagementBatch.raw_message_id.asc(),
            StrategyManagementBatch.id.desc(),
        )
        .all()
    )
    management_batch_by_msg_id: dict[int, StrategyManagementBatch] = {}
    for batch in all_management_batches:
        management_batch_by_msg_id.setdefault(batch.raw_message_id, batch)

    message_keys = {(int(row.chat_id), int(row.message_id)) for row in raw_messages}
    binding_rows = (
        session.query(ExecutionBinding)
        .filter(
            or_(
                *[
                    (ExecutionBinding.chat_id == chat_id)
                    & (ExecutionBinding.message_id == message_id)
                    for chat_id, message_id in message_keys
                ]
            )
        )
        .order_by(ExecutionBinding.id.asc())
        .all()
        if message_keys
        else []
    )
    bindings_by_message_key: dict[tuple[int, int], list[ExecutionBinding]] = {}
    for binding in binding_rows:
        bindings_by_message_key.setdefault(
            (int(binding.chat_id), int(binding.message_id)), []
        ).append(binding)

    binding_ids = [int(row.id) for row in binding_rows]
    revision_rows = (
        session.query(StrategyRevisionBatch)
        .filter(StrategyRevisionBatch.execution_binding_id.in_(binding_ids))
        .order_by(StrategyRevisionBatch.execution_binding_id.asc(), StrategyRevisionBatch.id.desc())
        .all()
        if binding_ids
        else []
    )
    latest_revision_by_binding_id: dict[int, StrategyRevisionBatch] = {}
    for batch in revision_rows:
        latest_revision_by_binding_id.setdefault(int(batch.execution_binding_id), batch)
    revision_ids = [int(row.id) for row in latest_revision_by_binding_id.values()]
    replacement_counts = dict(
        session.query(
            EntryRevisionReplacement.revision_batch_id,
            func.count(EntryRevisionReplacement.id),
        )
        .filter(EntryRevisionReplacement.revision_batch_id.in_(revision_ids))
        .group_by(EntryRevisionReplacement.revision_batch_id)
        .all()
    ) if revision_ids else {}
    confirmed_replacement_counts = dict(
        session.query(
            EntryRevisionReplacement.revision_batch_id,
            func.count(EntryRevisionReplacement.id),
        )
        .filter(
            EntryRevisionReplacement.revision_batch_id.in_(revision_ids),
            EntryRevisionReplacement.status == "verified",
        )
        .group_by(EntryRevisionReplacement.revision_batch_id)
        .all()
    ) if revision_ids else {}
    confirmed_cancel_counts = dict(
        session.query(
            StrategyRevisionLeg.revision_batch_id,
            func.count(StrategyRevisionLeg.id),
        )
        .filter(
            StrategyRevisionLeg.revision_batch_id.in_(revision_ids),
            StrategyRevisionLeg.action == "cancel_pending",
            StrategyRevisionLeg.status == "cancelled",
        )
        .group_by(StrategyRevisionLeg.revision_batch_id)
        .all()
    ) if revision_ids else {}

    all_experiments = (
        session.query(RecognitionExperiment)
        .filter(RecognitionExperiment.raw_message_id.in_(raw_message_ids))
        .all()
    )
    experiments_by_msg_id: dict[int, list[RecognitionExperiment]] = {}
    for experiment in all_experiments:
        experiments_by_msg_id.setdefault(experiment.raw_message_id, []).append(experiment)

    context_attempts = (
        session.query(ContextResolutionAttempt)
        .filter(ContextResolutionAttempt.raw_message_id.in_(raw_message_ids))
        .order_by(
            ContextResolutionAttempt.raw_message_id.asc(),
            ContextResolutionAttempt.id.desc(),
        )
        .all()
    )
    context_attempt_by_msg_id: dict[int, ContextResolutionAttempt] = {}
    for attempt in context_attempts:
        context_attempt_by_msg_id.setdefault(attempt.raw_message_id, attempt)
    evidence_rows = (
        session.query(MessageEvidenceVersion)
        .filter(
            MessageEvidenceVersion.raw_message_id.in_(raw_message_ids),
            MessageEvidenceVersion.superseded_at.is_(None),
        )
        .all()
    )
    evidence_by_msg_id = {row.raw_message_id: row for row in evidence_rows}
    context_links = (
        session.query(StrategyMessageLink, StrategyThread)
        .join(StrategyThread, StrategyThread.id == StrategyMessageLink.strategy_thread_id)
        .filter(StrategyMessageLink.raw_message_id.in_(raw_message_ids))
        .order_by(StrategyMessageLink.raw_message_id.asc(), StrategyMessageLink.id.asc())
        .all()
    )
    context_links_by_msg_id: dict[int, list[tuple[StrategyMessageLink, StrategyThread]]] = {}
    for link, thread in context_links:
        context_links_by_msg_id.setdefault(link.raw_message_id, []).append((link, thread))
    context_thread_ids = {thread.id for _, thread in context_links}
    thread_history_by_thread_id: dict[int, list[tuple[StrategyMessageLink, RawMessage]]] = {}
    if context_thread_ids:
        thread_history = (
            session.query(StrategyMessageLink, RawMessage)
            .join(RawMessage, RawMessage.id == StrategyMessageLink.raw_message_id)
            .filter(StrategyMessageLink.strategy_thread_id.in_(context_thread_ids))
            .order_by(
                StrategyMessageLink.strategy_thread_id.asc(),
                RawMessage.posted_at.asc(),
                RawMessage.message_id.asc(),
            )
            .all()
        )
        for link, linked_raw in thread_history:
            thread_history_by_thread_id.setdefault(
                link.strategy_thread_id, []
            ).append((link, linked_raw))

    # ── Bulk-load signal candidates ──
    all_candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id.in_(raw_message_ids))
        .order_by(SignalCandidate.raw_message_id.asc(), SignalCandidate.confidence.desc(), SignalCandidate.id.asc())
        .all()
    )
    cand_by_msg_id: dict[int, SignalCandidate] = {}
    candidates_by_msg_id: dict[int, list[SignalCandidate]] = {}
    for c in all_candidates:
        candidates_by_msg_id.setdefault(c.raw_message_id, []).append(c)
        if c.raw_message_id not in cand_by_msg_id:
            cand_by_msg_id[c.raw_message_id] = c

    # Strategy-record links must follow the exact candidate relation.  Do not
    # infer ownership from message symbol/side, and fail closed if legacy data
    # contains more than one lifecycle for the selected candidate.
    selected_candidate_ids = [candidate.id for candidate in cand_by_msg_id.values()]
    lifecycle_ids_by_candidate_id: dict[int, list[int]] = {}
    if selected_candidate_ids:
        lifecycles = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.signal_candidate_id.in_(selected_candidate_ids))
            .order_by(StrategyLifecycle.signal_candidate_id.asc(), StrategyLifecycle.id.asc())
            .all()
        )
        for lifecycle in lifecycles:
            lifecycle_ids_by_candidate_id.setdefault(
                lifecycle.signal_candidate_id, []
            ).append(lifecycle.id)

    rows: list[dict[str, object | None]] = []
    for raw_message in raw_messages:
        media_assets = media_by_msg_id.get(raw_message.id, [])
        media_asset_rows = _serialize_media_assets(media_assets)
        selected_candidate = cand_by_msg_id.get(raw_message.id)
        lifecycle_ids = (
            lifecycle_ids_by_candidate_id.get(selected_candidate.id, [])
            if selected_candidate is not None
            else []
        )
        decision = decisions_by_msg_id.get(raw_message.id)
        semantic_review = _serialize_semantic_review(decision)
        matching_bindings = bindings_by_message_key.get(
            (int(raw_message.chat_id), int(raw_message.message_id)), []
        )
        entry_preamble_assembly = (
            _serialize_entry_preamble_binding(matching_bindings[0])
            if len(matching_bindings) == 1
            else None
        )
        latest_revision = (
            latest_revision_by_binding_id.get(int(matching_bindings[0].id))
            if len(matching_bindings) == 1
            else None
        )
        entry_revision = (
            _serialize_entry_revision_batch(
                latest_revision,
                replacement_count=int(replacement_counts.get(int(latest_revision.id), 0)),
                confirmed_change_count=(
                    int(confirmed_replacement_counts.get(int(latest_revision.id), 0))
                    + int(confirmed_cancel_counts.get(int(latest_revision.id), 0))
                ),
            )
            if latest_revision is not None
            else None
        )
        rows.append(
            {
                "raw_message_id": raw_message.id,
                "chat_id": raw_message.chat_id,
                "message_id": raw_message.message_id,
                "sender_id": raw_message.sender_id,
                "sender_name": raw_message.sender_name,
                "posted_at": utc_naive_to_local(raw_message.posted_at),
                "edit_date": utc_naive_to_local(raw_message.edit_date),
                "text": raw_message.text,
                "reply_to_message_id": raw_message.reply_to_message_id,
                "media_assets": media_asset_rows,
                "strategy_lifecycle_id": (
                    lifecycle_ids[0] if len(lifecycle_ids) == 1 else None
                ),
                "low_confidence_exit_targets": _serialize_low_confidence_exit_targets(
                    candidates_by_msg_id.get(raw_message.id, [])
                ),
                "strategy_detection": _build_strategy_detection(
                    recognition=rec_by_msg_id.get(raw_message.id),
                    candidate=selected_candidate,
                    media_assets=media_assets,
                ),
                "semantic_review": semantic_review,
                "execution_outcome": _serialize_execution_outcome(
                    decision,
                    management_batch_by_msg_id.get(raw_message.id),
                ),
                "entry_preamble_assembly": entry_preamble_assembly,
                "entry_revision": entry_revision,
                "decision_card": _build_message_decision_card(
                    decision=decision,
                    semantic_review=semantic_review,
                ),
                "authoritative_model_summary": _build_authoritative_model_summary(
                    decision=decision,
                    semantic_review=semantic_review,
                ),
                "recognition_comparison": _build_recognition_comparison(
                    recognition=rec_by_msg_id.get(raw_message.id),
                    media_assets=media_assets,
                    experiments=experiments_by_msg_id.get(raw_message.id, []),
                ),
                "reply_context": None,
                "context_resolution": _serialize_context_resolution(
                    raw_message=raw_message,
                    attempt=context_attempt_by_msg_id.get(raw_message.id),
                    evidence=evidence_by_msg_id.get(raw_message.id),
                    links=context_links_by_msg_id.get(raw_message.id, []),
                    thread_history_by_thread_id=thread_history_by_thread_id,
                ),
            }
        )

    return rows


def _serialize_context_resolution(
    *,
    raw_message: RawMessage,
    attempt: ContextResolutionAttempt | None,
    evidence: MessageEvidenceVersion | None,
    links: list[tuple[StrategyMessageLink, StrategyThread]],
    thread_history_by_thread_id: dict[
        int, list[tuple[StrategyMessageLink, RawMessage]]
    ],
) -> dict[str, object] | None:
    if attempt is None and evidence is None and not links and raw_message.reply_to_message_id is None:
        return None
    decision = _safe_json_dict(attempt.decision_json) if attempt is not None else {}
    request = _safe_json_dict(attempt.request_summary_json) if attempt is not None else {}
    image_evidence = _safe_json_dict(evidence.image_evidence_json) if evidence is not None else {}
    linked = [
        {
            "thread_id": int(thread.id),
            "root_message_id": int(thread.root_message_id),
            "relation": link.relation_kind,
            "thread_status": thread.status,
            "confidence": round(float(link.confidence), 3),
        }
        for link, thread in links
    ]
    linked_messages = []
    seen_linked_messages: set[tuple[int, int, str]] = set()
    for _, thread in links:
        for history_link, linked_raw in thread_history_by_thread_id.get(thread.id, []):
            key = (thread.id, linked_raw.id, history_link.relation_kind)
            if key in seen_linked_messages:
                continue
            seen_linked_messages.add(key)
            linked_messages.append(
                {
                    "thread_id": int(thread.id),
                    "message_id": int(linked_raw.message_id),
                    "relation": history_link.relation_kind,
                    "posted_at": utc_naive_to_local(linked_raw.posted_at),
                }
            )
    triggers = []
    if attempt is not None:
        try:
            parsed = json.loads(attempt.reanalysis_triggers_json or "[]")
            triggers = [str(value) for value in parsed] if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            triggers = []
    return {
        "reply_to_message_id": raw_message.reply_to_message_id,
        "evidence_version": evidence.version if evidence is not None else None,
        "evidence_input_kind": (
            "text+image" if image_evidence else "text"
        ) if evidence is not None else None,
        "decision": str(decision.get("decision") or "") or None,
        "confidence": decision.get("confidence"),
        "supporting_message_ids": decision.get("supporting_message_ids") or [],
        "opposing_message_ids": decision.get("opposing_message_ids") or [],
        "unresolved_reason": (
            decision.get("reason")
            if str(decision.get("decision") or "") in {"unresolved", "hold"}
            else None
        ),
        "next_triggers": triggers,
        "attempt_status": attempt.status if attempt is not None else None,
        "linked_threads": linked,
        "linked_messages": linked_messages,
        "context_message_count": len(
            request.get("message_context")
            if isinstance(request.get("message_context"), list)
            else []
        ),
    }


def _serialize_low_confidence_exit_targets(
    candidates: list[SignalCandidate],
) -> list[dict[str, object]]:
    targets = [
        candidate
        for candidate in candidates
        if candidate.parse_source == "low_confidence_group_exit"
        and candidate.management_action == "partial_take_profit"
        and candidate.management_fraction == 0.5
        and candidate.target_lifecycle_id is not None
    ]
    return [
        {
            "symbol": str(candidate.symbol or "").upper(),
            "side": "空" if candidate.side == "short" else "多",
            "lifecycle_id": int(candidate.target_lifecycle_id),
        }
        for candidate in sorted(
            targets,
            key=lambda candidate: (str(candidate.symbol or ""), int(candidate.target_lifecycle_id or 0)),
        )
    ]


def _build_authoritative_model_summary(
    *,
    decision: RecognitionDecision | None,
    semantic_review: dict[str, object | None] | None,
) -> list[dict[str, str | None]]:
    """Return the two persisted decision roles for the message detail UI."""

    if decision is None:
        return []

    authoritative_payload = _parse_json_object(decision.authoritative_payload_json)
    lifecycle_event = authoritative_payload.get("lifecycle_event")
    lifecycle_event = lifecycle_event if isinstance(lifecycle_event, dict) else {}
    event_type = str(lifecycle_event.get("event_type") or "")
    authoritative_reason = authoritative_payload.get("reason")
    authoritative_reason = (
        authoritative_reason.strip()
        if isinstance(authoritative_reason, str) and authoritative_reason.strip()
        else None
    )
    auxiliary_payload = _parse_json_object(
        decision.auxiliary_payload_json or decision.comparison_payload_json
    )
    auxiliary_reason = auxiliary_payload.get("reason")
    auxiliary_reason = (
        auxiliary_reason.strip()
        if isinstance(auxiliary_reason, str) and auxiliary_reason.strip()
        else None
    )
    review_label = semantic_review.get("label") if semantic_review else None
    review_label = review_label if isinstance(review_label, str) else None

    rows = [
        {
            "label": "MiMo 主分析",
            "model": decision.authoritative_model,
            "conclusion": (
                _candidate_event_status(event_type)
                if event_type
                else decision.authoritative_status
            ),
            "reason": authoritative_reason,
        }
    ]
    auxiliary_model = decision.auxiliary_model or decision.comparison_model
    if auxiliary_model:
        rows.append(
            {
                "label": "DeepSeek 辅助复核",
                "model": auxiliary_model,
                "conclusion": decision.auxiliary_status or review_label,
                "reason": auxiliary_reason,
            }
        )
    return rows


def _parse_json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_execution_outcome(
    decision: RecognitionDecision | None,
    batch: StrategyManagementBatch | None,
) -> dict[str, str | None] | None:
    if batch is not None:
        status = str(batch.status or "")
        detail = _execution_reason_label(batch.reason_code)
        if status == "succeeded":
            if batch.effective_action in {
                "adjust_stop_loss",
                "move_stop_to_break_even",
                "partial_then_break_even",
            }:
                return {
                    "state": "accepted",
                    "label": "Deepcoin 已接受保护单更新",
                    "detail": detail,
                }
            return {
                "state": "confirmed",
                "label": "交易所已确认执行",
                "detail": detail,
            }
        if status in {"executing", "reconciling", "protection_ready"}:
            return {
                "state": "pending_confirmation",
                "label": "已提交，等待交易所确认",
                "detail": detail,
            }
        if status == "ready" and batch.execution_mode == "shadow":
            return {
                "state": "shadow",
                "label": "仅模拟计划，未实盘执行",
                "detail": detail,
            }
        if status == "ready":
            return {"state": "waiting", "label": "待执行", "detail": detail}
        if status in {"blocked", "partial_failed", "recovery_required"}:
            return {
                "state": "error",
                "label": "执行异常，需人工处理",
                "detail": detail,
            }

    if decision is None or decision.automation_status is None:
        return None
    status = str(decision.automation_status)
    detail = _execution_reason_label(decision.automation_reason)
    if status in {
        "submitted",
        "executed",
        "completed",
        "executing",
        "reconciling",
        "succeeded",
    }:
        return {
            "state": "pending_confirmation",
            "label": "已提交，等待交易所确认",
            "detail": detail,
        }
    if status in {
        "blocked",
        "failed",
        "error",
        "partial_failed",
        "recovery_required",
    }:
        return {
            "state": "error",
            "label": "未执行成功",
            "detail": detail,
        }
    if status == "shadow_planned":
        return {
            "state": "shadow",
            "label": "仅模拟计划，未实盘执行",
            "detail": detail,
        }
    return {"state": "not_executed", "label": "未执行", "detail": detail}


def _execution_reason_label(reason: str | None) -> str | None:
    return {
        "close_submitted": "平仓请求已提交",
        "management_close_exchange_confirmed": "已根据交易所仓位快照确认",
        "all_position_protection_replaced": "所有持仓的保护单均已替换",
        "management_execution_disabled": "自动持仓管理未启用",
        "management_close_pending_exchange_confirmation": "等待交易所仓位快照确认",
        "close_final_preflight_failed": "最终仓位或合约规格校验失败",
        "management_close_result_requires_recovery": "平仓结果需要人工复核",
        "mimo_authoritative_not_safely_applied": "MiMo 生命周期事件未能安全落地",
    }.get(str(reason or ""))


def _serialize_media_assets(media_assets: list[MediaAsset]) -> list[dict[str, object | None]]:
    return [
        {
            "id": media_asset.id,
            "kind": media_asset.kind,
            "mime_type": media_asset.mime_type,
            "local_path": media_asset.local_path,
            "ocr_text": media_asset.ocr_text,
        }
        for media_asset in media_assets
    ]


def _serialize_semantic_review(
    decision: RecognitionDecision | None,
) -> dict[str, object | None] | None:
    if decision is None:
        return None

    payload: dict[str, object] = {}
    if decision.comparison_payload_json:
        try:
            loaded = json.loads(decision.comparison_payload_json)
        except (json.JSONDecodeError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    else:
        reason = reason.strip()

    conflict_types = payload.get("conflict_types")
    if not isinstance(conflict_types, list):
        conflict_types = []
    conflict_types = [item for item in conflict_types if isinstance(item, str)]

    status = decision.comparison_status or "pending"
    if status == "failed" or decision.agreement_status == "authoritative_failed":
        severity = "failed"
        label = "失败"
    elif (
        status == "completed"
        and decision.disagreement_severity == "critical"
        and _is_context_only_target_review(payload=payload, conflict_types=conflict_types)
    ):
        severity = "context"
        label = "上下文待核对"
    elif status == "completed" and decision.disagreement_severity == "critical":
        severity = "critical"
        label = "严重分歧"
    elif status == "completed" and decision.disagreement_severity == "normal":
        severity = "normal"
        label = "普通差异"
    elif (
        status == "completed"
        and decision.disagreement_severity == "none"
        and decision.agreement_status == "agreed"
    ):
        severity = "agreed"
        label = "一致"
    elif status == "completed":
        severity = "unclassified"
        label = "待重新复核"
        reason = "历史记录没有语义分歧等级，需重新复核"
        conflict_types = []
    else:
        severity = "pending"
        label = "等待中"

    return {
        "status": status,
        "severity": severity,
        "label": label,
        "reason": reason,
        "conflict_types": conflict_types,
        "model": decision.comparison_model,
    }


def _build_message_decision_card(
    *,
    decision: RecognitionDecision | None,
    semantic_review: dict[str, object | None] | None,
) -> dict[str, object] | None:
    if decision is None:
        return None

    try:
        payload = json.loads(decision.authoritative_payload_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    lifecycle_event = payload.get("lifecycle_event")
    lifecycle_event = lifecycle_event if isinstance(lifecycle_event, dict) else {}
    strategy = payload.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}

    event_type = str(lifecycle_event.get("event_type") or "")
    management_action = str(lifecycle_event.get("management_action") or "")
    stop_loss = lifecycle_event.get("stop_loss")
    is_stop_management = event_type == "position_update" and (
        "stop" in management_action or management_action == "risk_update"
    )
    missing_stop_price = is_stop_management and stop_loss in (None, "")
    has_complete_strategy = all(
        strategy.get(field) not in (None, "")
        for field in ("symbol", "side", "entry", "stop_loss", "take_profit")
    )
    is_safely_linked_event = (
        event_type not in {"", "none"}
        and lifecycle_event.get("target_lifecycle_id") not in (None, "")
        and decision.agreement_status == "agreed"
        and decision.disagreement_severity == "none"
    )
    review_reason = (
        semantic_review.get("reason") if semantic_review is not None else None
    )
    review_reason = review_reason if isinstance(review_reason, str) else None
    review_conflicts = (
        semantic_review.get("conflict_types") if semantic_review is not None else []
    )
    review_conflicts = (
        {str(item) for item in review_conflicts}
        if isinstance(review_conflicts, list)
        else set()
    )
    is_execution_guarded_consensus = (
        event_type not in {"", "none"}
        and lifecycle_event.get("target_lifecycle_id") not in (None, "")
        and review_reason is not None
        and "无实质分歧" in review_reason
        and review_conflicts <= {"execution_unresolved"}
    )

    if missing_stop_price:
        state = "manual_review"
        state_label = "需人工确认"
        recommended_action = "不执行"
        blocker = "未提供新的止损价格"
    elif event_type == "position_update" and management_action == "hold_update":
        state = "record_only"
        state_label = "仅记录"
        recommended_action = "无需操作"
        blocker = None
    elif (
        decision.authoritative_status in {"识别失败", "failed", "failure", "error"}
        and not is_execution_guarded_consensus
    ):
        state = "fetch_failed"
        state_label = "获取失败"
        recommended_action = "重新识别"
        blocker = None
    elif has_complete_strategy:
        state = "strategy_identified"
        state_label = "策略已识别"
        recommended_action = "查看执行记录"
        blocker = None
    elif is_safely_linked_event or is_execution_guarded_consensus:
        state = "strategy_linked"
        state_label = "已关联策略"
        recommended_action = "查看执行记录"
        blocker = None
    elif event_type:
        state = "manual_review"
        state_label = "需人工确认"
        recommended_action = "等待安全校验"
        blocker = None
    else:
        state = "record_only"
        state_label = "仅记录"
        recommended_action = "不执行"
        blocker = None

    facts: list[dict[str, str]] = []
    symbol = lifecycle_event.get("symbol") or strategy.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        facts.append({"label": "标的", "value": symbol.strip().upper()})
    side = lifecycle_event.get("side") or strategy.get("side")
    side_label = {"long": "多", "short": "空"}.get(str(side or "").lower())
    if side_label:
        facts.append({"label": "方向", "value": side_label})

    primary_reason = payload.get("reason")
    primary_reason = primary_reason.strip() if isinstance(primary_reason, str) else None
    primary_conclusion = _candidate_event_status(event_type) if event_type else decision.authoritative_status
    review_label = (
        semantic_review.get("label") if semantic_review is not None else "待复核"
    )
    review_label = review_label if isinstance(review_label, str) else "待复核"
    review_tone = (
        semantic_review.get("severity") if semantic_review is not None else "pending"
    )
    review_tone = review_tone if isinstance(review_tone, str) else "pending"
    if review_reason and "无实质分歧" in review_reason:
        review_label = "一致"
        review_tone = "agreed"

    execution = _serialize_execution_outcome(decision, None) or {
        "state": "not_executed",
        "label": "未发送交易所请求",
        "detail": None,
    }
    if execution["state"] == "not_executed":
        execution = {
            "state": "not_executed",
            "label": (
                "自动执行未发出"
                if is_execution_guarded_consensus
                else "未发送交易所请求"
            ),
            "detail": execution["detail"] if is_execution_guarded_consensus else None,
        }

    return {
        "state": state,
        "state_label": state_label,
        "recommended_action": recommended_action,
        "blocker": blocker,
        "message_facts": facts,
        "inherited_context": [],
        "primary_analysis": {
            "label": "主分析 · MiMo",
            "conclusion": primary_conclusion,
            "reason": primary_reason,
        },
        "secondary_review": {
            "label": "辅助复核 · DeepSeek",
            "conclusion": review_label,
            "reason": review_reason,
        },
        "agreement": {
            "label": f"{review_label} · {'不自动执行' if recommended_action == '不执行' else recommended_action}",
            "tone": review_tone,
        },
        "execution": execution,
    }


def _is_context_only_target_review(
    *,
    payload: dict[str, object],
    conflict_types: list[str],
) -> bool:
    conflicts = {str(item) for item in conflict_types}
    if not conflicts or not conflicts <= {"symbol", "target_lifecycle"}:
        return False
    independent = payload.get("independent_action")
    if not isinstance(independent, dict):
        return False
    action_type = str(independent.get("action_type") or "")
    if action_type not in {"position_update", "exit_partial"}:
        return False
    return (
        independent.get("target_lifecycle_id") is None
        or independent.get("symbol") is None
    )


def _build_recognition_comparison(
    *,
    recognition: MessageRecognition | None,
    media_assets: list[MediaAsset],
    experiments: list[RecognitionExperiment],
) -> dict[str, dict[str, str | None]]:
    has_image = _has_image_like_media(media_assets)
    mimo_experiment = next(
        (
            experiment
            for experiment in experiments
            if experiment.experiment_name == "mimo_direct_v1"
        ),
        None,
    )
    return {
        "deepseek_text": _serialize_production_text_recognition(recognition),
        "glm_ocr_image": _serialize_glm_ocr_result(media_assets),
        "mimo_text": _serialize_mimo_experiment(mimo_experiment) if not has_image else _not_applicable("image message"),
        "mimo_image": _serialize_mimo_experiment(mimo_experiment) if has_image else _not_applicable("text message"),
    }


def _serialize_production_text_recognition(recognition: MessageRecognition | None) -> dict[str, str | None]:
    if recognition is None:
        return _not_run()
    return {
        "status": recognition.status,
        "summary": recognition.summary,
        "reason": recognition.reason,
        "engine": recognition.engine,
        "status_class": _recognition_status_class(recognition.status),
    }


def _serialize_glm_ocr_result(media_assets: list[MediaAsset]) -> dict[str, str | None]:
    if not _has_image_like_media(media_assets):
        return _not_applicable("text message")
    ocr_texts = [
        (media_asset.ocr_text or "").strip()
        for media_asset in media_assets
        if (media_asset.ocr_text or "").strip()
    ]
    if not ocr_texts:
        return _not_run()
    return {
        "status": "done",
        "summary": "\n\n".join(ocr_texts),
        "reason": None,
        "engine": "glm-ocr",
        "status_class": "is-pending",
    }


def _serialize_mimo_experiment(experiment: RecognitionExperiment | None) -> dict[str, str | None]:
    if experiment is None:
        return _not_run()
    summary = experiment.observed_text
    if _has_meaningful_strategy_json(experiment.strategy_json):
        summary = f"{summary or ''}\n{experiment.strategy_json}".strip()
    return {
        "status": experiment.status,
        "summary": summary,
        "reason": experiment.reason or experiment.error_message,
        "engine": experiment.model,
        "status_class": _recognition_status_class(experiment.status),
    }


def _has_meaningful_strategy_json(strategy_json: str | None) -> bool:
    if not strategy_json:
        return False
    try:
        payload = json.loads(strategy_json)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True
    return any(value not in (None, "", [], {}) for value in payload.values())


def _not_run() -> dict[str, str | None]:
    return {
        "status": "not run",
        "summary": None,
        "reason": None,
        "engine": None,
        "status_class": "is-pending",
    }


def _not_applicable(reason: str) -> dict[str, str | None]:
    return {
        "status": "n/a",
        "summary": None,
        "reason": reason,
        "engine": None,
        "status_class": "is-pending",
    }


def _build_strategy_detection(
    *,
    recognition: MessageRecognition | None,
    candidate: SignalCandidate | None,
    media_assets: list[MediaAsset],
) -> dict[str, str | None]:
    if candidate is not None and candidate.event_type != "entry_signal":
        return {
            "status": _candidate_event_status(candidate.event_type),
            "status_class": "is-strategy",
            "summary": _format_signal_candidate_summary(candidate),
            "reason": candidate.review_note or (recognition.reason if recognition is not None else None),
        }

    if recognition is not None:
        return {
            "status": recognition.status,
            "status_class": _recognition_status_class(recognition.status),
            "summary": recognition.summary,
            "reason": recognition.reason,
        }

    if candidate is not None:
        return {
            "status": "是策略",
            "status_class": "is-strategy",
            "summary": _format_signal_candidate_summary(candidate),
            "reason": None,
        }

    if _has_video_like_media(media_assets):
        return {
            "status": "非策略",
            "status_class": "is-not-strategy",
            "summary": None,
            "reason": "视频消息默认跳过",
        }

    return {
        "status": "待识别",
        "status_class": "is-pending",
        "summary": None,
        "reason": None,
    }


def _candidate_event_status(event_type: str | None) -> str:
    return {
        "entry_confirm": "入场确认",
        "cancel_entry": "取消入场",
        "exit_position": "离场信号",
        "position_update": "仓位管理",
        "strategy_correction": "策略调整",
        "duplicate_entry_signal": "重复策略",
    }.get(event_type or "", "策略事件")


def _recognition_status_class(status: str) -> str:
    if status in {
        "是策略",
        "入场确认",
        "取消入场",
        "离场信号",
        "仓位管理",
        "策略调整",
    }:
        return "is-strategy"
    if status == "非策略":
        return "is-not-strategy"
    if status == "识别失败":
        return "is-failed"
    return "is-pending"


def _format_signal_candidate_summary(candidate: SignalCandidate) -> str:
    parts: list[str] = []
    symbol_side = " ".join(
        value
        for value in [
            candidate.symbol,
            candidate.side,
        ]
        if value
    )
    if symbol_side:
        parts.append(symbol_side)
    if candidate.entry_text:
        parts.append(f"Entry {candidate.entry_text}")
    if candidate.stop_loss_text:
        parts.append(f"SL {candidate.stop_loss_text}")
    if candidate.take_profit_text:
        parts.append(f"TP {candidate.take_profit_text}")
    if candidate.leverage_text:
        parts.append(candidate.leverage_text)
    return "；".join(parts) or "已命中策略候选"


def _has_video_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "video" in media_kind or "document" in media_kind or mime_type.startswith("video/"):
            return True
    return False


def _has_image_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "photo" in media_kind or mime_type.startswith("image/"):
            return True
    return False


# ── strategy lifecycle queries ────────────────────────────────────


def _format_lifecycle_entry_text(
    entry_low: float | None,
    entry_high: float | None,
) -> str | None:
    if entry_low is None and entry_high is None:
        return None
    if entry_low is None:
        return f"{entry_high:g}"
    if entry_high is None or entry_low == entry_high:
        return f"{entry_low:g}"
    return f"{entry_low:g}-{entry_high:g}"


POSITION_SIZE_RISK_USDT = 1000.0


def _position_size_entry_price(
    *,
    entry_price_actual: float | None,
    entry_low: float | None,
    entry_high: float | None,
) -> float | None:
    if entry_price_actual is not None and entry_price_actual > 0:
        return float(entry_price_actual)
    prices = [value for value in (entry_low, entry_high) if value is not None and value > 0]
    if not prices:
        return None
    return sum(prices) / len(prices)


def _format_position_size_text(
    *,
    symbol: str | None,
    entry_price_actual: float | None,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
    risk_usdt: float = POSITION_SIZE_RISK_USDT,
) -> str | None:
    entry_price = _position_size_entry_price(
        entry_price_actual=entry_price_actual,
        entry_low=entry_low,
        entry_high=entry_high,
    )
    if entry_price is None or stop_loss is None:
        return None
    price_risk = abs(float(entry_price) - float(stop_loss))
    if price_risk <= 0:
        return None
    quantity = risk_usdt / price_risk
    base_symbol = _base_symbol(symbol)
    suffix = f" {base_symbol}" if base_symbol else ""
    return f"{quantity:.6g}{suffix}（止损{risk_usdt:g}U）"


def _backfill_closed_binding_metrics(binding: ExecutionBinding) -> dict[str, object]:
    """Recover submitted entry price and size from a legacy binding payload.

    These are submitted-order values, not exchange fill values.  The caller
    exposes their provenance so the UI never presents them as verified fills.
    """
    payload = _safe_json_dict(binding.payload_json)
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        return {}
    contract_spec = draft.get("contract_spec")
    order_legs = draft.get("order_legs")
    if not isinstance(contract_spec, dict) or not isinstance(order_legs, list):
        return {}
    try:
        contract_value = float(contract_spec.get("contract_value"))
    except (TypeError, ValueError):
        return {}
    if contract_value <= 0:
        return {}

    total_contracts = 0.0
    weighted_notional = 0.0
    for leg in order_legs:
        if not isinstance(leg, dict):
            continue
        try:
            price = float(leg.get("price"))
            quantity = float(leg.get("quantity"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or quantity <= 0:
            continue
        total_contracts += quantity
        weighted_notional += price * quantity
    if total_contracts <= 0:
        return {}

    base_quantity = total_contracts * contract_value
    base_symbol = _base_symbol(binding.symbol)
    suffix = f" {base_symbol}" if base_symbol else ""
    fallback = {
        "entry_price_actual": weighted_notional / total_contracts,
        "position_size_text": f"{base_quantity:.6g}{suffix}",
        "history_metric_source": "saved_order_payload",
    }
    history_metrics = payload.get("history_metrics")
    if not isinstance(history_metrics, dict):
        return fallback

    def number(name: str) -> float | None:
        try:
            return float(history_metrics.get(name))
        except (TypeError, ValueError):
            return None

    entry_price = number("avgPx")
    exit_price = number("closeAvgPx")
    realized_pnl = number("pnl")
    opened_contracts = number("pos")
    closed_contracts = number("closePos")
    actual_contracts = max(
        value for value in (opened_contracts, closed_contracts) if value is not None
    ) if opened_contracts is not None or closed_contracts is not None else None
    if entry_price is None or entry_price <= 0 or actual_contracts is None or actual_contracts <= 0:
        return fallback
    verified = {
        "entry_price_actual": entry_price,
        "position_size_text": f"{actual_contracts * contract_value:.6g}{suffix}",
        "history_metric_source": "deepcoin_position_history",
    }
    if exit_price is not None and exit_price > 0:
        verified["exit_price_actual"] = exit_price
    if realized_pnl is not None:
        verified["realized_pnl"] = realized_pnl
    return verified


def _message_excerpt(text: str | None, *, limit: int = 96) -> str | None:
    if not text:
        return None
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _format_price_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{float(value):g}"


def _lifecycle_status_label(status: str | None, exit_reason: str | None = None) -> str:
    if status == "pending_entry":
        return "待入场"
    if status == "entered":
        return "持仓中"
    if status == "invalidated" or exit_reason == "context_invalidated":
        return "语境失效"
    if exit_reason == "auto_trade_failed":
        return "自动交易失败"
    if status == "expired" or exit_reason == "expired":
        return "过期未入场"
    if exit_reason == "cancelled":
        return "取消挂单"
    if exit_reason == "kol_signal":
        return "KOL离场信号"
    if exit_reason == "stop_loss":
        return "止损离场"
    if exit_reason == "take_profit":
        return "止盈离场"
    if status == "exited":
        return "已离场"
    return str(status or "未知状态")


def _lifecycle_status_detail(status: str | None, exit_reason: str | None = None) -> str:
    if status == "pending_entry":
        return "原策略已记录，等待价格触发或 KOL 后续确认。"
    if status == "entered":
        return "策略已入场，继续跟踪止盈、止损和 KOL 后续离场消息。"
    if status == "invalidated" or exit_reason == "context_invalidated":
        return "KOL 后续同币种消息改变了原入场语境，旧待入场策略已移出可执行列表。"
    if exit_reason == "auto_trade_failed":
        return "交易所下单失败，策略未形成真实持仓。"
    if exit_reason == "cancelled":
        return "KOL 后续消息取消了这笔限价挂单，策略未入场。"
    if exit_reason == "kol_signal":
        return "KOL 后续消息要求离场或平仓。"
    if exit_reason == "stop_loss":
        return "行情触发止损条件。"
    if exit_reason == "take_profit":
        return "行情触发止盈条件。"
    if status == "expired" or exit_reason == "expired":
        return "超过跟踪窗口仍未触发入场。"
    return "策略生命周期已结束。"


def _management_action_label(action: str | None) -> str:
    if action == "partial_close_confirmed":
        return "部分平仓（交易所已确认）"
    if action == "full_close_confirmed":
        return "全部平仓（交易所已确认）"
    if action and "partial_take_profit" in action and "move_stop_to_protect" in action:
        return "部分止盈 + 保护止损"
    if action == "partial_take_profit":
        return "部分止盈"
    if action == "move_stop_to_protect":
        return "保护止损"
    if action == "strategy_correction":
        return "策略修正"
    if action == "risk_update":
        return "风控更新"
    if action == "hold_update":
        return "继续持有"
    if action:
        return str(action)
    return "持仓管理"


def _latest_event_label(status: str | None, exit_reason: str | None = None) -> str:
    if status == "invalidated" or exit_reason == "context_invalidated":
        return "语境失效"
    if exit_reason == "auto_trade_failed":
        return "自动交易失败"
    if exit_reason == "cancelled":
        return "取消挂单"
    if exit_reason == "kol_signal":
        return "KOL离场"
    if exit_reason == "stop_loss":
        return "止损触发"
    if exit_reason == "take_profit":
        return "止盈触发"
    if status == "expired" or exit_reason == "expired":
        return "过期未入场"
    if status == "entered":
        return "入场确认"
    if status == "pending_entry":
        return "等待入场"
    return "状态更新"


def _transition_text(status: str | None, exit_reason: str | None = None) -> str:
    if status == "invalidated" or exit_reason == "context_invalidated":
        return "pending_entry → invalidated"
    if exit_reason == "auto_trade_failed":
        return "pending_entry → auto_trade_failed"
    if exit_reason == "cancelled":
        return "pending_entry → cancelled"
    if status == "expired" or exit_reason == "expired":
        return "pending_entry → expired"
    if exit_reason in {"kol_signal", "stop_loss", "take_profit"}:
        return "entered → exited"
    if status == "entered":
        return "pending_entry → entered"
    if status == "pending_entry":
        return "entry_signal → pending_entry"
    return str(status or "")


def _apply_lifecycle_display_fields(
    row: dict[str, object],
    lifecycle,
    original_message=None,
    latest_event_message=None,
    session=None,
) -> dict[str, object]:
    latest_at = lifecycle.exited_at or lifecycle.entered_at or lifecycle.signal_at
    if lifecycle.lifecycle_status == "pending_entry":
        latest_at = lifecycle.signal_at
    management_message_id = getattr(lifecycle, "management_signal_message_id", None)
    management_action = getattr(lifecycle, "management_action", None)
    if management_message_id is not None:
        latest_at = latest_event_message.posted_at if latest_event_message is not None else latest_at

    row.update(
        {
            "current_status_label": _lifecycle_status_label(
                lifecycle.lifecycle_status,
                lifecycle.exit_reason,
            ),
            "status_detail": _lifecycle_status_detail(
                lifecycle.lifecycle_status,
                lifecycle.exit_reason,
            ),
            "management_action_label": _management_action_label(management_action),
            "management_note": getattr(lifecycle, "management_note", None),
            "transition_text": _transition_text(
                lifecycle.lifecycle_status,
                lifecycle.exit_reason,
            ),
            "original_message_id": lifecycle.message_id,
            "original_posted_at": utc_naive_to_local(
                original_message.posted_at if original_message is not None else lifecycle.signal_at
            ),
            "original_text_excerpt": _message_excerpt(
                original_message.text if original_message is not None else None
            ),
            "latest_event_label": _latest_event_label(
                lifecycle.lifecycle_status,
                lifecycle.exit_reason,
            )
            if management_message_id is None
            else _management_action_label(management_action),
            "latest_event_message_id": (
                lifecycle.exit_signal_message_id
                if lifecycle.exit_signal_message_id is not None
                else management_message_id
                if management_message_id is not None
                else lifecycle.entry_signal_message_id
                if getattr(lifecycle, "entry_signal_message_id", None) is not None
                else lifecycle.message_id
            ),
            "latest_event_at": utc_naive_to_local(
                latest_event_message.posted_at
                if latest_event_message is not None
                else latest_at
            ),
            "latest_event_text_excerpt": _message_excerpt(
                latest_event_message.text if latest_event_message is not None else None
            ),
        }
    )
    if session is not None:
        row["lifecycle_events"] = _build_lifecycle_event_timeline(
            session,
            lifecycle,
            original_message=original_message,
            latest_event_message=latest_event_message,
        )
    _add_strategy_time_display_fields(row)
    return row


def _build_lifecycle_event_timeline(
    session,
    lifecycle,
    *,
    original_message=None,
    latest_event_message=None,
) -> list[dict[str, object]]:
    from telegram_kol_research.models import RawMessage, SignalCandidate, utc_now

    events: list[dict[str, object]] = []
    seen: set[tuple[str, str | int | None]] = set()

    def add_event(
        *,
        kind: str,
        label: str,
        at,
        message_id: int | None = None,
        text: str | None = None,
        detail: str | None = None,
        transition: str | None = None,
        event_key: str | int | None = None,
    ) -> None:
        key = (kind, event_key if event_key is not None else message_id)
        if key in seen:
            return
        seen.add(key)
        events.append(
            {
                "kind": kind,
                "label": label,
                "at": utc_naive_to_local(at),
                "message_id": message_id,
                "text_excerpt": _message_excerpt(text),
                "detail": detail,
                "transition": transition,
            }
        )

    add_event(
        kind="entry_signal",
        label="原策略",
        at=original_message.posted_at if original_message is not None else lifecycle.signal_at,
        message_id=lifecycle.message_id,
        text=original_message.text if original_message is not None else None,
        transition="entry_signal → pending_entry",
    )

    if lifecycle.entered_at is not None:
        entry_message = None
        if getattr(lifecycle, "entry_signal_message_id", None) is not None:
            entry_message = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lifecycle.chat_id)
                .filter(RawMessage.message_id == lifecycle.entry_signal_message_id)
                .one_or_none()
            )
        add_event(
            kind="entry_confirm",
            label="入场确认",
            at=entry_message.posted_at if entry_message is not None else lifecycle.entered_at,
            message_id=(
                lifecycle.entry_signal_message_id
                if getattr(lifecycle, "entry_signal_message_id", None) is not None
                else None
            ),
            text=entry_message.text if entry_message is not None else None,
            detail=(
                f"入场价 {_format_price_number(lifecycle.entry_price_actual)}"
                if lifecycle.entry_price_actual is not None
                else None
            ),
            transition="pending_entry → entered",
        )

    binding_id = getattr(lifecycle, "execution_binding_id", None)
    if binding_id is not None:
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == binding_id,
                ExecutionOrderLeg.purpose == "entry",
                ExecutionOrderLeg.attribution_status == "verified",
                ExecutionOrderLeg.pos_id.is_not(None),
                ExecutionOrderLeg.pos_id != "",
            )
            .all()
        )
        entry_pos_ids = {int(row.id): str(row.pos_id) for row in entry_legs}
        if entry_pos_ids:
            adopted_rows = (
                session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.execution_order_leg_id.in_(entry_pos_ids),
                    PositionProtectionLedger.evidence_source
                    == "reconciliation_trigger_entry_adoption",
                )
                .order_by(PositionProtectionLedger.created_at, PositionProtectionLedger.id)
                .all()
            )
            for row in adopted_rows:
                if entry_pos_ids.get(int(row.execution_order_leg_id)) != str(row.pos_id):
                    continue
                add_event(
                    kind="protection_adopted",
                    label="保护单归属已验证",
                    at=row.created_at,
                    detail=f"已验证保护单 #{row.order_id}",
                    event_key=int(row.id),
                )
            refusal_rows = (
                session.query(PositionAttributionAudit)
                .filter(
                    PositionAttributionAudit.execution_order_leg_id.in_(entry_pos_ids),
                    PositionAttributionAudit.event_type == "protection_adoption_refused",
                )
                .order_by(PositionAttributionAudit.created_at, PositionAttributionAudit.id)
                .all()
            )
            for row in refusal_rows:
                if entry_pos_ids.get(int(row.execution_order_leg_id)) != str(
                    row.pos_id or ""
                ):
                    continue
                raw_evidence = _safe_json_dict(row.evidence_json)
                reason_code = str(raw_evidence.get("reason") or "unknown")
                add_event(
                    kind="protection_adoption_refused",
                    label="保护单归属未验证",
                    at=row.created_at,
                    detail=f"拒绝原因：{reason_code}",
                    event_key=int(row.id),
                )
            intents = (
                session.query(TriggerProtectionIntent)
                .filter(
                    TriggerProtectionIntent.execution_order_leg_id.in_(entry_pos_ids)
                )
                .order_by(TriggerProtectionIntent.created_at, TriggerProtectionIntent.id)
                .all()
            )
            intent_ids = {int(intent.id) for intent in intents}
            rescues_by_intent = {
                int(rescue.trigger_protection_intent_id): rescue
                for rescue in (
                    session.query(TriggerProtectionStopRescue)
                    .filter(
                        TriggerProtectionStopRescue.trigger_protection_intent_id.in_(
                            intent_ids
                        )
                    )
                    .all()
                    if intent_ids
                    else []
                )
            }
            for intent in intents:
                pos_id = entry_pos_ids.get(int(intent.execution_order_leg_id))
                if (
                    pos_id is None
                    or int(intent.execution_binding_id) != int(binding_id)
                ):
                    continue
                rescue = rescues_by_intent.get(int(intent.id))
                rescue_matches_position = (
                    rescue is not None
                    and int(rescue.execution_binding_id) == int(binding_id)
                    and int(rescue.execution_order_leg_id)
                    == int(intent.execution_order_leg_id)
                    and int(rescue.execution_binding_id)
                    == int(intent.execution_binding_id)
                    and str(rescue.pos_id) == pos_id
                )
                refusal_code = (
                    _bounded_reason_code(rescue.reason_code)
                    if rescue_matches_position and rescue is not None
                    else None
                )
                rescue_state = (
                    str(rescue.status) if rescue_matches_position and rescue else "none"
                )
                adopted_ids = [str(intent.adopted_order_id)] if intent.adopted_order_id else []
                detail = (
                    f"parent_order_id={intent.parent_trigger_order_id or '-'} · "
                    f"pos_id={pos_id} · state={intent.recovery_state} · "
                    f"attempts={int(intent.retry_attempts)} · "
                    f"adopted_tpsl_ids={','.join(adopted_ids) or '-'} · "
                    f"refusal={refusal_code or '-'} · stop_rescue={rescue_state}"
                )
                add_event(
                    kind="trigger_protection_recovery",
                    label="触发单保护恢复",
                    at=intent.updated_at or intent.created_at,
                    detail=detail,
                    event_key=int(intent.id),
                )

    start_at = lifecycle.signal_at
    end_at = lifecycle.exited_at or utc_now()
    management_rows = (
        session.query(SignalCandidate, RawMessage)
        .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
        .filter(RawMessage.chat_id == lifecycle.chat_id)
        .filter(RawMessage.posted_at >= start_at)
        .filter(RawMessage.posted_at <= end_at)
        .filter(SignalCandidate.event_type.in_(["position_update", "strategy_correction"]))
        .filter(SignalCandidate.symbol == lifecycle.symbol)
        .filter(SignalCandidate.side == lifecycle.side)
        .order_by(RawMessage.posted_at.asc(), RawMessage.message_id.asc())
        .all()
    )
    for candidate, message in management_rows:
        action = getattr(candidate, "event_type", None)
        label = (
            _management_action_label(getattr(lifecycle, "management_action", None))
            if message.message_id == getattr(lifecycle, "management_signal_message_id", None)
            else _management_action_label(action)
        )
        if candidate.event_type == "strategy_correction":
            label = "策略修正"
        add_event(
            kind=candidate.event_type,
            label=label,
            at=message.posted_at,
            message_id=message.message_id,
            text=message.text,
            detail=_format_candidate_event_detail(candidate),
        )

    if getattr(lifecycle, "management_signal_message_id", None) is not None:
        management_message = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id == lifecycle.chat_id)
            .filter(RawMessage.message_id == lifecycle.management_signal_message_id)
            .one_or_none()
        )
        if management_message is not None:
            add_event(
                kind="position_update",
                label=_management_action_label(getattr(lifecycle, "management_action", None)),
                at=management_message.posted_at,
                message_id=management_message.message_id,
                text=management_message.text,
                detail=getattr(lifecycle, "management_note", None),
            )

    if lifecycle.lifecycle_status in {"exited", "expired"} or lifecycle.exited_at is not None:
        exit_message = latest_event_message
        if lifecycle.exit_signal_message_id is not None:
            exit_message = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lifecycle.chat_id)
                .filter(RawMessage.message_id == lifecycle.exit_signal_message_id)
                .one_or_none()
            )
        add_event(
            kind="exit",
            label=_latest_event_label(lifecycle.lifecycle_status, lifecycle.exit_reason),
            at=exit_message.posted_at if exit_message is not None else lifecycle.exited_at,
            message_id=lifecycle.exit_signal_message_id,
            text=exit_message.text if exit_message is not None else None,
            detail=(
                f"离场价 {_format_price_number(lifecycle.exit_price_actual)}"
                if getattr(lifecycle, "exit_price_actual", None) is not None
                else None
            ),
            transition=_transition_text(lifecycle.lifecycle_status, lifecycle.exit_reason),
        )

    events.sort(key=lambda item: (str(item.get("at") or ""), int(item.get("message_id") or 0)))
    for event in events:
        event_at = event.get("at")
        event["at_display"] = _format_strategy_time(event_at)
    return events


def _format_candidate_event_detail(candidate) -> str | None:
    parts: list[str] = []
    if candidate.stop_loss_text:
        parts.append(f"SL {candidate.stop_loss_text}")
    if candidate.take_profit_text:
        parts.append(f"TP {candidate.take_profit_text}")
    return " / ".join(parts) or None


def _bounded_reason_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not code or len(code) > 96 or not re.fullmatch(r"[a-z0-9_:-]+", code):
        return "unknown"
    return code


def load_lifecycle_counts(
    session_factory,
    *,
    chat_id: int | None = None,
    symbol_whitelist_by_chat_id: dict[int, set[str]] | None = None,
) -> dict[str, int]:
    """Return counts for each lifecycle status (KPI cards)."""
    from telegram_kol_research.models import StrategyLifecycle

    with session_factory() as session:
        query = session.query(
            StrategyLifecycle.lifecycle_status,
            StrategyLifecycle.chat_id,
            StrategyLifecycle.symbol,
            StrategyLifecycle.entry_range_low,
            StrategyLifecycle.entry_range_high,
            ExecutionBinding.status,
        ).outerjoin(
            ExecutionBinding,
            StrategyLifecycle.execution_binding_id == ExecutionBinding.id,
        )
        if chat_id is not None:
            query = query.filter(StrategyLifecycle.chat_id == chat_id)
        rows = query.all()
    counts: dict[str, int] = {}
    whitelist_map = symbol_whitelist_by_chat_id or {}
    for status, row_chat_id, symbol, entry_low, entry_high, binding_status in rows:
        allowed_symbols = (
            whitelist_map.get(int(row_chat_id))
            if row_chat_id is not None
            else None
        )
        if status == "pending_entry":
            if _is_live_execution_binding_status(binding_status):
                continue
            if not _is_actionable_pending_entry(
                symbol=symbol,
                entry_low=entry_low,
                entry_high=entry_high,
                allowed_symbols=allowed_symbols,
            ):
                continue
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


def load_lifecycle_counts_by_chat_id(
    session_factory,
    *,
    symbol_whitelist_by_chat_id: dict[int, set[str]] | None = None,
) -> dict[int, dict[str, int]]:
    """Return actionable lifecycle status counts grouped by chat id for sidebar badges."""
    from telegram_kol_research.models import StrategyLifecycle

    with session_factory() as session:
        rows = (
            session.query(
                StrategyLifecycle.chat_id,
                StrategyLifecycle.lifecycle_status,
                StrategyLifecycle.symbol,
                StrategyLifecycle.entry_range_low,
                StrategyLifecycle.entry_range_high,
                ExecutionBinding.status,
            )
            .outerjoin(
                ExecutionBinding,
                StrategyLifecycle.execution_binding_id == ExecutionBinding.id,
            )
            .all()
        )

    counts_by_chat_id: dict[int, dict[str, int]] = {}
    whitelist_map = symbol_whitelist_by_chat_id or {}
    for chat_id, status, symbol, entry_low, entry_high, binding_status in rows:
        if status == "pending_entry":
            if _is_live_execution_binding_status(binding_status):
                continue
            if not _is_actionable_pending_entry(
                symbol=symbol,
                entry_low=entry_low,
                entry_high=entry_high,
                allowed_symbols=whitelist_map.get(int(chat_id)),
            ):
                continue
        status_counts = counts_by_chat_id.setdefault(int(chat_id), {})
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
    return counts_by_chat_id


def _is_live_execution_binding_status(status: str | None) -> bool:
    return str(status or "").lower() in {"open", "active"}


def _is_actionable_pending_entry(
    *,
    symbol: str | None,
    entry_low: float | None,
    entry_high: float | None,
    allowed_symbols: set[str] | None = None,
) -> bool:
    normalized_symbol = _base_symbol(symbol)
    if normalized_symbol in {"", "?", "QQ"}:
        return False
    if allowed_symbols is not None and normalized_symbol not in allowed_symbols:
        return False
    if entry_low is None and entry_high is None:
        return False
    return _entry_price_is_plausible(
        symbol=normalized_symbol,
        entry_low=entry_low,
        entry_high=entry_high,
    )


def _base_symbol(symbol: str | None) -> str:
    normalized = (symbol or "").upper().replace("-", "").replace("_", "")
    for suffix in ("USDT", "USD", "PERP", "SWAP"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized[: -len(suffix)]
    return normalized


def _entry_price_is_plausible(
    *,
    symbol: str,
    entry_low: float | None,
    entry_high: float | None,
) -> bool:
    prices = [value for value in (entry_low, entry_high) if value is not None]
    if not prices:
        return False
    min_price = min(prices)
    if symbol == "BTC":
        return min_price >= 1000
    if symbol == "ETH":
        return min_price >= 100
    return min_price > 0


def list_holding_strategies(
    session_factory, *, chat_id: int | None = None, limit: int = 50
) -> list[dict[str, object]]:
    """Return exchange-bound *entered* lifecycle records.

    A lifecycle can become ``entered`` from price monitoring alone.  The
    holding tab is an execution view, so only strategies with a live DeepCoin
    binding should be counted as real positions.
    """
    from telegram_kol_research.models import (
        ExecutionBinding, SignalCandidate, RawMessage, StrategyLifecycle,
    )

    with session_factory() as session:
        q = (
            session.query(StrategyLifecycle, SignalCandidate, RawMessage, ExecutionBinding)
            .join(
                ExecutionBinding,
                StrategyLifecycle.execution_binding_id == ExecutionBinding.id,
            )
            .outerjoin(
                SignalCandidate,
                StrategyLifecycle.signal_candidate_id == SignalCandidate.id,
            )
            .outerjoin(
                RawMessage,
                SignalCandidate.raw_message_id == RawMessage.id,
            )
            .filter(StrategyLifecycle.lifecycle_status == "entered")
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
        )
        if chat_id is not None:
            q = q.filter(StrategyLifecycle.chat_id == chat_id)
        q = q.order_by(
            StrategyLifecycle.entered_at.desc().nullslast(),
            StrategyLifecycle.id.desc(),
        ).limit(limit)
        rows = q.all()

    results: list[dict[str, object]] = []
    for lc, cand, raw_msg, binding in rows:
        row: dict[str, object] = {
            "lifecycle_id": lc.id,
            "chat_id": lc.chat_id,
            "message_id": lc.message_id,
            "symbol": lc.symbol,
            "side": lc.side,
            "lifecycle_status": lc.lifecycle_status,
            "signal_at": utc_naive_to_local(lc.signal_at),
            "entered_at": utc_naive_to_local(lc.entered_at),
            "entry_price_actual": lc.entry_price_actual,
            "entry_range_low": lc.entry_range_low,
            "entry_range_high": lc.entry_range_high,
            "stop_loss": lc.stop_loss,
            "take_profit": lc.take_profit,
            "entry_text": _format_lifecycle_entry_text(lc.entry_range_low, lc.entry_range_high),
            "stop_loss_text": f"{lc.stop_loss:g}" if lc.stop_loss is not None else None,
            "take_profit_text": lc.take_profit,
            "position_size_text": _format_position_size_text(
                symbol=lc.symbol,
                entry_price_actual=lc.entry_price_actual,
                entry_low=lc.entry_range_low,
                entry_high=lc.entry_range_high,
                stop_loss=lc.stop_loss,
            ),
            "position_size_risk_usdt": POSITION_SIZE_RISK_USDT,
            "filled_tp_index": lc.filled_tp_index,
            "last_checked_at": utc_naive_to_local(lc.last_checked_at),
            "is_live_bound": True,
            "execution_binding_id": binding.id,
            "execution_status": binding.status,
            "exchange_status": binding.last_exchange_status,
            "pos_id": binding.pos_id,
            "order_id": binding.order_id,
        }
        if cand is not None:
            row["entry_text"] = cand.entry_text or row["entry_text"]
            row["stop_loss_text"] = cand.stop_loss_text or row["stop_loss_text"]
            row["take_profit_text"] = cand.take_profit_text or row["take_profit_text"]
            row["confidence"] = cand.confidence
        if raw_msg is not None:
            row["sender_name"] = raw_msg.sender_name
            row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
        else:
            raw_msg = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lc.chat_id)
                .filter(RawMessage.message_id == lc.message_id)
                .one_or_none()
            )
            if raw_msg is not None:
                row["sender_name"] = raw_msg.sender_name
                row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
        latest_event_msg = None
        if getattr(lc, "management_signal_message_id", None) is not None:
            latest_event_msg = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lc.chat_id)
                .filter(RawMessage.message_id == lc.management_signal_message_id)
                .one_or_none()
            )
        elif getattr(lc, "entry_signal_message_id", None) is not None:
            latest_event_msg = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lc.chat_id)
                .filter(RawMessage.message_id == lc.entry_signal_message_id)
                .one_or_none()
            )
        _apply_lifecycle_display_fields(row, lc, raw_msg, latest_event_msg, session=session)
        results.append(row)

    return results


def list_pending_strategies(
    session_factory,
    *,
    chat_id: int | None = None,
    limit: int = 50,
    symbol_whitelist_by_chat_id: dict[int, set[str]] | None = None,
) -> list[dict[str, object]]:
    """Return actionable *pending_entry* lifecycle records."""
    from telegram_kol_research.models import (
        ExecutionBinding, SignalCandidate, RawMessage, StrategyLifecycle,
    )

    with session_factory() as session:
        q = (
            session.query(StrategyLifecycle, SignalCandidate, RawMessage, ExecutionBinding)
            .outerjoin(
                SignalCandidate,
                StrategyLifecycle.signal_candidate_id == SignalCandidate.id,
            )
            .outerjoin(
                RawMessage,
                SignalCandidate.raw_message_id == RawMessage.id,
            )
            .outerjoin(
                ExecutionBinding,
                StrategyLifecycle.execution_binding_id == ExecutionBinding.id,
            )
            .filter(StrategyLifecycle.lifecycle_status == "pending_entry")
            .filter(~StrategyLifecycle.symbol.in_(["", "?", "QQ"]))
            .filter(
                or_(
                    StrategyLifecycle.entry_range_low.isnot(None),
                    StrategyLifecycle.entry_range_high.isnot(None),
                )
            )
            .filter(
                or_(
                    ExecutionBinding.id.is_(None),
                    ~ExecutionBinding.status.in_(["open", "active"]),
                )
            )
        )
        if chat_id is not None:
            q = q.filter(StrategyLifecycle.chat_id == chat_id)
        q = q.order_by(
            StrategyLifecycle.signal_at.desc(),
            StrategyLifecycle.id.desc(),
        )
        rows = q.all()

    results: list[dict[str, object]] = []
    whitelist_map = symbol_whitelist_by_chat_id or {}
    for lc, cand, raw_msg, _binding in rows:
        if not _is_actionable_pending_entry(
            symbol=lc.symbol,
            entry_low=lc.entry_range_low,
            entry_high=lc.entry_range_high,
            allowed_symbols=whitelist_map.get(int(lc.chat_id)),
        ):
            continue
        row: dict[str, object] = {
            "lifecycle_id": lc.id,
            "chat_id": lc.chat_id,
            "message_id": lc.message_id,
            "symbol": lc.symbol,
            "side": lc.side,
            "lifecycle_status": lc.lifecycle_status,
            "execution_status": "pending_entry",
            "signal_at": utc_naive_to_local(lc.signal_at),
            "entry_range_low": lc.entry_range_low,
            "entry_range_high": lc.entry_range_high,
            "stop_loss": lc.stop_loss,
            "take_profit": lc.take_profit,
            "entry_range_text": _format_lifecycle_entry_text(
                lc.entry_range_low,
                lc.entry_range_high,
            ),
            "entry_text": _format_lifecycle_entry_text(
                lc.entry_range_low,
                lc.entry_range_high,
            ),
            "stop_loss_text": f"{lc.stop_loss:g}" if lc.stop_loss is not None else None,
            "take_profit_text": lc.take_profit,
            "position_size_text": _format_position_size_text(
                symbol=lc.symbol,
                entry_price_actual=lc.entry_price_actual,
                entry_low=lc.entry_range_low,
                entry_high=lc.entry_range_high,
                stop_loss=lc.stop_loss,
            ),
            "position_size_risk_usdt": POSITION_SIZE_RISK_USDT,
            "last_checked_at": utc_naive_to_local(lc.last_checked_at),
        }
        if cand is not None:
            row["entry_range_text"] = cand.entry_text or row["entry_range_text"]
            row["entry_text"] = cand.entry_text or row["entry_text"]
            row["stop_loss_text"] = cand.stop_loss_text or row["stop_loss_text"]
            row["take_profit_text"] = cand.take_profit_text or row["take_profit_text"]
            row["confidence"] = cand.confidence
        if raw_msg is not None:
            row["sender_name"] = raw_msg.sender_name
            row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
        else:
            raw_msg = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lc.chat_id)
                .filter(RawMessage.message_id == lc.message_id)
                .one_or_none()
            )
            if raw_msg is not None:
                row["sender_name"] = raw_msg.sender_name
                row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
        _apply_lifecycle_display_fields(row, lc, raw_msg, session=session)
        results.append(row)
        if len(results) >= limit:
            break

    return results


def list_exited_strategies(
    session_factory, *, chat_id: int | None = None, limit: int = 50
) -> list[dict[str, object]]:
    """Return exited / expired strategies from all available sources.

    Merges three data sources (deduplicated by chat_id + message_id + symbol + side):
    1. StrategyLifecycle (exited / expired) — the new lifecycle tracker
    2. ExecutionBinding (closed / cancelled) — exchange-tracked
    3. TradeIdea (closed) + close TradeUpdates — old strategy tracker
    """
    from telegram_kol_research.models import (
        SignalCandidate, RawMessage, StrategyLifecycle,
        ExecutionBinding, TradeIdea, TradeUpdate,
    )

    results: list[dict[str, object]] = []
    seen_signal_keys: set[tuple[int, int, str, str]] = set()

    def signal_key(
        chat_id_value: int | None,
        message_id_value: int | None,
        symbol_value: str | None,
        side_value: str | None,
    ) -> tuple[int, int, str, str] | None:
        if chat_id_value is None or message_id_value is None:
            return None
        return (
            int(chat_id_value),
            int(message_id_value),
            (symbol_value or "?").upper(),
            (side_value or "?").lower(),
        )

    with session_factory() as session:
        # ── 1. StrategyLifecycle (exited / expired) ──
        lc_q = (
            session.query(StrategyLifecycle, SignalCandidate, RawMessage)
            .outerjoin(
                SignalCandidate,
                StrategyLifecycle.signal_candidate_id == SignalCandidate.id,
            )
            .outerjoin(
                RawMessage,
                SignalCandidate.raw_message_id == RawMessage.id,
            )
            .filter(
                StrategyLifecycle.lifecycle_status.in_(["exited", "expired", "invalidated"])
            )
        )
        if chat_id is not None:
            lc_q = lc_q.filter(StrategyLifecycle.chat_id == chat_id)
        lc_q = lc_q.order_by(
            StrategyLifecycle.exited_at.desc().nullslast(),
            StrategyLifecycle.id.desc(),
        ).limit(limit)

        for lc, cand, raw_msg in lc_q.all():
            key = signal_key(lc.chat_id, lc.message_id, lc.symbol, lc.side)
            if key is not None and key in seen_signal_keys:
                continue
            if key is not None:
                seen_signal_keys.add(key)
            row: dict[str, object] = {
                "history_sort_id": f"lifecycle:{lc.id}",
                "history_sort_key": (lc.id, 2),
                "lifecycle_id": lc.id,
                "chat_id": lc.chat_id,
                "message_id": lc.message_id,
                "symbol": lc.symbol,
                "side": lc.side,
                "source": "lifecycle",
                "lifecycle_status": lc.lifecycle_status,
                "exit_reason": lc.exit_reason,
                "signal_at": utc_naive_to_local(lc.signal_at),
                "entered_at": utc_naive_to_local(lc.entered_at),
                "exited_at": utc_naive_to_local(lc.exited_at),
                "entry_price_actual": lc.entry_price_actual,
                "exit_price_actual": lc.exit_price_actual,
                "stop_loss": lc.stop_loss,
                "take_profit": lc.take_profit,
                "entry_text": _format_lifecycle_entry_text(
                    lc.entry_range_low,
                    lc.entry_range_high,
                ),
                "stop_loss_text": f"{lc.stop_loss:g}" if lc.stop_loss is not None else None,
                "take_profit_text": lc.take_profit,
                "position_size_text": _format_position_size_text(
                    symbol=lc.symbol,
                    entry_price_actual=lc.entry_price_actual,
                    entry_low=lc.entry_range_low,
                    entry_high=lc.entry_range_high,
                    stop_loss=lc.stop_loss,
                ),
                "position_size_risk_usdt": POSITION_SIZE_RISK_USDT,
            }
            if cand is not None:
                row["entry_text"] = cand.entry_text or row["entry_text"]
                row["stop_loss_text"] = cand.stop_loss_text or row["stop_loss_text"]
                row["take_profit_text"] = cand.take_profit_text or row["take_profit_text"]
                row["confidence"] = cand.confidence
            if raw_msg is not None:
                row["sender_name"] = raw_msg.sender_name
                row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
            else:
                raw_msg = (
                    session.query(RawMessage)
                    .filter(RawMessage.chat_id == lc.chat_id)
                    .filter(RawMessage.message_id == lc.message_id)
                    .one_or_none()
                )
                if raw_msg is not None:
                    row["sender_name"] = raw_msg.sender_name
                    row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
            latest_event_msg = None
            if lc.exit_signal_message_id is not None:
                latest_event_msg = (
                    session.query(RawMessage)
                    .filter(RawMessage.chat_id == lc.chat_id)
                    .filter(RawMessage.message_id == lc.exit_signal_message_id)
                    .one_or_none()
                )
            _apply_lifecycle_display_fields(row, lc, raw_msg, latest_event_msg, session=session)
            results.append(row)

        # ── 2. ExecutionBinding (closed / cancelled) ──
        eb_q = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.status.in_(["closed", "cancelled", "filled"]))
        )
        if chat_id is not None:
            eb_q = eb_q.filter(ExecutionBinding.chat_id == chat_id)
        eb_q = eb_q.order_by(ExecutionBinding.updated_at.desc()).limit(limit)

        for eb in eb_q.all():
            key = signal_key(eb.chat_id, eb.message_id, eb.symbol, eb.side)
            if key is not None and key in seen_signal_keys:
                continue
            if key is not None:
                seen_signal_keys.add(key)
            row = {
                "history_sort_id": f"binding:{eb.id}",
                "history_sort_key": (eb.id, 3),
                "chat_id": eb.chat_id,
                "message_id": eb.message_id,
                "symbol": eb.symbol,
                "side": eb.side,
                "source": "execution_binding",
                "lifecycle_status": "exited",
                "exit_reason": eb.status,
                "entered_at": utc_naive_to_local(eb.created_at),
                "exited_at": utc_naive_to_local(eb.updated_at),
                "sender_name": eb.kol_id,
            }
            row.update(_backfill_closed_binding_metrics(eb))
            results.append(_add_strategy_time_display_fields(row))

        # ── 3. TradeIdea (closed) ──
        closed_trade_ids = set()
        if session.query(TradeIdea).count() > 0:
            close_rows = (
                session.query(TradeUpdate.trade_idea_id)
                .filter(TradeUpdate.update_type.in_([
                    "close", "close_signal", "stop_loss_hit",
                    "take_profit_hit", "manual_close", "closed",
                ]))
                .all()
            )
            closed_trade_ids.update(r[0] for r in close_rows)

        ti_q = (
            session.query(TradeIdea, SignalCandidate, RawMessage)
            .outerjoin(SignalCandidate, TradeIdea.primary_signal_candidate_id == SignalCandidate.id)
            .outerjoin(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .filter(TradeIdea.status == "closed")
        )
        if chat_id is not None:
            ti_q = ti_q.filter(TradeIdea.chat_id == chat_id)
        ti_q = ti_q.order_by(TradeIdea.closed_at.desc().nullslast()).limit(limit)

        for ti, cand, raw_msg in ti_q.all():
            key = signal_key(
                ti.chat_id,
                raw_msg.message_id if raw_msg is not None else None,
                ti.symbol,
                ti.side,
            )
            if key is not None and key in seen_signal_keys:
                continue
            if key is not None:
                seen_signal_keys.add(key)
            row = {
                "history_sort_id": f"trade-idea:{ti.id}",
                "history_sort_key": (ti.id, 1),
                "chat_id": ti.chat_id,
                "symbol": ti.symbol or "?",
                "side": ti.side or "?",
                "source": "trade_idea",
                "lifecycle_status": "exited",
                "exit_reason": "closed",
                "entered_at": utc_naive_to_local(ti.opened_at),
                "exited_at": utc_naive_to_local(ti.closed_at),
                "confidence": ti.confidence,
            }
            if cand is not None:
                row["entry_text"] = cand.entry_text
                row["stop_loss_text"] = cand.stop_loss_text
                row["take_profit_text"] = cand.take_profit_text
            if raw_msg is not None:
                row["sender_name"] = raw_msg.sender_name
                row["message_id"] = raw_msg.message_id
                row["posted_at"] = utc_naive_to_local(raw_msg.posted_at)
            results.append(_add_strategy_time_display_fields(row))

    results.sort(
        key=lambda r: (
            r.get("exited_at") is not None,
            r.get("exited_at") or r.get("entered_at") or "",
            tuple(r.get("history_sort_key") or (0, 0)),
        ),
        reverse=True,
    )
    return results[:limit]


def _deepcoin_history_close_time(history_metrics: dict[str, object]) -> datetime | None:
    """Return the exchange close/update timestamp stored with a position row."""
    for field in ("uTime", "closeTime", "cTime"):
        value = history_metrics.get(field)
        try:
            milliseconds = int(float(value))
        except (TypeError, ValueError):
            continue
        if milliseconds > 0:
            return utc_naive_to_local(datetime.fromtimestamp(milliseconds / 1000, tz=UTC))
    return None


def _has_complete_deepcoin_history_metrics(
    binding: ExecutionBinding,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Return verified DeepCoin metrics only when they prove a closed position."""
    payload = _safe_json_dict(binding.payload_json)
    history_metrics = payload.get("history_metrics")
    if not isinstance(history_metrics, dict):
        return None
    try:
        values = {
            field: float(history_metrics[field])
            for field in ("avgPx", "closeAvgPx", "pnl", "pos", "closePos")
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (
        values["avgPx"] <= 0
        or values["closeAvgPx"] <= 0
        or values["pos"] <= 0
        or values["closePos"] <= 0
    ):
        return None
    metrics = _backfill_closed_binding_metrics(binding)
    if metrics.get("history_metric_source") != "deepcoin_position_history":
        return None
    return history_metrics, metrics


def list_verified_deepcoin_history_positions(
    session_factory, *, chat_id: int | None = None, limit: int = 50
) -> list[dict[str, object]]:
    """Return only complete, exchange-verified DeepCoin closed positions.

    Strategy status is deliberately not a criterion: a closed strategy can be
    an expired or cancelled conditional order.  A record must have both the
    cached official position metrics and an exact entry-leg position id.
    """
    with session_factory() as session:
        query = session.query(ExecutionBinding).filter(ExecutionBinding.venue == "deepcoin")
        if chat_id is not None:
            query = query.filter(ExecutionBinding.chat_id == chat_id)
        bindings = query.all()
        entry_pos_ids = {
            int(binding_id): sorted(
                {
                    str(pos_id)
                    for (pos_id,) in session.query(ExecutionOrderLeg.pos_id)
                    .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
                    .filter(ExecutionOrderLeg.purpose == "entry")
                    .filter(ExecutionOrderLeg.pos_id.is_not(None))
                    .filter(ExecutionOrderLeg.pos_id != "")
                    .all()
                }
            )
            for binding_id in [binding.id for binding in bindings]
        }

    results: list[dict[str, object]] = []
    for binding in bindings:
        verified = _has_complete_deepcoin_history_metrics(binding)
        pos_ids = entry_pos_ids.get(int(binding.id), [])
        if verified is None or not pos_ids:
            continue
        history_metrics, metrics = verified
        closed_at = _deepcoin_history_close_time(history_metrics) or utc_naive_to_local(
            binding.updated_at
        )
        row: dict[str, object] = {
            "history_sort_id": f"binding:{binding.id}",
            "history_sort_key": (str(pos_ids[0]), int(binding.id)),
            "execution_binding_id": binding.id,
            "chat_id": binding.chat_id,
            "message_id": binding.message_id,
            "symbol": binding.symbol,
            "side": binding.side,
            "source": "execution_binding",
            "lifecycle_status": "exited",
            "exit_reason": "closed",
            "entered_at": utc_naive_to_local(binding.created_at),
            "exited_at": closed_at,
            "sender_name": binding.kol_id,
            "pos_ids": pos_ids,
        }
        row.update(metrics)
        results.append(_add_strategy_time_display_fields(row))

    results.sort(
        key=lambda row: (
            row.get("exited_at") or "",
            tuple(row.get("history_sort_key") or ("", 0)),
        ),
        reverse=True,
    )
    return results[:limit]


def list_execution_strategy_overview(
    session_factory,
    *,
    status: str = "holding",
    limit: int = 200,
    group_label_by_chat_id: dict[int, str] | None = None,
    symbol_whitelist_by_chat_id: dict[int, set[str]] | None = None,
) -> dict[str, object]:
    """Return a global execution-centric strategy dashboard."""

    selected_status = status if status in {"holding", "pending", "exited"} else "holding"
    labels = group_label_by_chat_id or {}
    whitelist_map = symbol_whitelist_by_chat_id or {}
    with session_factory() as session:
        items = _load_execution_strategy_items(
            session,
            selected_status=selected_status,
            limit=limit,
            labels=labels,
            whitelist_map=whitelist_map,
        )
        counts = {
            "holding": _count_execution_lifecycles(
                session,
                "entered",
                whitelist_map=whitelist_map,
            ),
            "pending": _count_execution_lifecycles(
                session,
                "pending_entry",
                whitelist_map=whitelist_map,
            ),
            "exited": _count_execution_lifecycles(
                session,
                "exited",
                whitelist_map=whitelist_map,
            ),
        }
    return {"status": selected_status, "items": items, "counts": counts}


def mark_strategy_lifecycle_manual_close(
    session_factory,
    *,
    lifecycle_id: int,
    closed_at=None,
    exit_price: float | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Mark one active lifecycle as manually closed outside the system."""

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        StrategyLifecycle,
        TradeIdea,
    )

    now = closed_at or utc_now()
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            raise LookupError("strategy lifecycle not found")
        manual_close_error = (
            "manual close requires unique execution binding and either entered "
            "lifecycle or legacy pending_entry with entered_at"
        )

        matching_bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
            .filter(ExecutionBinding.message_id == lifecycle.message_id)
            .filter(ExecutionBinding.symbol == lifecycle.symbol)
            .filter(ExecutionBinding.side == lifecycle.side)
            .order_by(ExecutionBinding.id.asc())
            .limit(2)
            .all()
        )
        binding = None
        if lifecycle.execution_binding_id is not None:
            candidate_binding = session.get(
                ExecutionBinding, lifecycle.execution_binding_id
            )
            candidate_matches_lifecycle = (
                candidate_binding is not None
                and str(candidate_binding.venue or "").lower() == "deepcoin"
                and candidate_binding.chat_id == lifecycle.chat_id
                and candidate_binding.message_id == lifecycle.message_id
                and str(candidate_binding.symbol) == str(lifecycle.symbol)
                and str(candidate_binding.side) == str(lifecycle.side)
            )
            explicit_binding_is_unique = (
                candidate_matches_lifecycle
                and len(matching_bindings) == 1
                and int(matching_bindings[0].id) == int(candidate_binding.id)
            )
            if not explicit_binding_is_unique:
                raise ValueError(manual_close_error)
            binding = candidate_binding
        else:
            binding = matching_bindings[0] if len(matching_bindings) == 1 else None
        is_legacy_demoted_entry = bool(
            lifecycle.lifecycle_status == "pending_entry"
            and lifecycle.entered_at is not None
            and binding is not None
        )
        if binding is None or (
            lifecycle.lifecycle_status != "entered" and not is_legacy_demoted_entry
        ):
            raise ValueError(manual_close_error)

        lifecycle.lifecycle_status = "exited"
        lifecycle.exit_reason = "manual"
        lifecycle.exited_at = now
        lifecycle.exit_price_actual = exit_price
        lifecycle.updated_at = now

        if lifecycle.trade_idea_id is not None:
            trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
            if trade_idea is not None and trade_idea.status == "open":
                trade_idea.status = "closed"
                trade_idea.closed_at = now

        if binding is not None:
            binding.status = "closed"
            binding.last_exchange_status = (
                "manual_closed_by_user"
                if not note
                else f"manual_closed_by_user: {note}"[:64]
            )
            binding.updated_at = now
            entry_legs = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
                .filter(ExecutionOrderLeg.purpose == "entry")
                .all()
            )
            for leg in entry_legs:
                leg.status = "manually_closed"
                leg.terminal_reason = "manual_closed_by_user"
                leg.updated_at = now

        result = {
            "lifecycle_id": lifecycle.id,
            "chat_id": lifecycle.chat_id,
            "message_id": lifecycle.message_id,
            "symbol": lifecycle.symbol,
            "side": lifecycle.side,
            "lifecycle_status": lifecycle.lifecycle_status,
            "exit_reason": lifecycle.exit_reason,
        }
        session.commit()
        return result


def _load_execution_strategy_items(
    session,
    *,
    selected_status: str,
    limit: int,
    labels: dict[int, str],
    whitelist_map: dict[int, set[str]],
) -> list[dict[str, object]]:
    from telegram_kol_research.models import (
        ExecutionBinding,
        RawMessage,
        SignalCandidate,
        StrategyLifecycle,
    )
    from telegram_kol_research.runtime_incident_scanner import (
        _critical_unprotected_positions_in_session,
    )

    critical_by_binding: dict[int, list[dict[str, object]]] = {}
    if selected_status == "holding":
        for risk in _critical_unprotected_positions_in_session(session, limit=100):
            critical_by_binding.setdefault(
                int(risk["execution_binding_id"]), []
            ).append({
                "execution_order_leg_id": int(risk["execution_order_leg_id"]),
                "pos_id": str(risk["pos_id"]),
                "planned_stop": risk["planned_stop"],
                "exposure_started_at": risk["exposure_started_at"],
                "rescue_state": risk["rescue_state"],
            })

    lifecycle_status = {
        "holding": "entered",
        "pending": "pending_entry",
        "exited": "exited",
    }[selected_status]
    q = (
        session.query(StrategyLifecycle, SignalCandidate, RawMessage, ExecutionBinding)
        .outerjoin(SignalCandidate, StrategyLifecycle.signal_candidate_id == SignalCandidate.id)
        .outerjoin(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
        .outerjoin(ExecutionBinding, StrategyLifecycle.execution_binding_id == ExecutionBinding.id)
    )
    if selected_status == "exited":
        q = q.filter(StrategyLifecycle.lifecycle_status.in_(["exited", "expired", "invalidated"]))
    else:
        q = q.filter(StrategyLifecycle.lifecycle_status == lifecycle_status)
    if selected_status == "pending":
        q = q.filter(~StrategyLifecycle.symbol.in_(["", "?", "QQ"]))
        q = q.filter(
            or_(
                ExecutionBinding.id.is_(None),
                ~ExecutionBinding.status.in_(["open", "active"]),
            )
        )
    order_column = {
        "holding": StrategyLifecycle.entered_at.desc().nullslast(),
        "pending": StrategyLifecycle.signal_at.desc(),
        "exited": StrategyLifecycle.exited_at.desc().nullslast(),
    }[selected_status]
    rows = q.order_by(order_column, StrategyLifecycle.id.desc()).limit(limit * 3).all()

    items: list[dict[str, object]] = []
    for lc, cand, raw_msg, _binding in rows:
        if selected_status == "pending" and not _is_actionable_pending_entry(
            symbol=lc.symbol,
            entry_low=lc.entry_range_low,
            entry_high=lc.entry_range_high,
            allowed_symbols=whitelist_map.get(int(lc.chat_id)),
        ):
            continue
        if raw_msg is None:
            raw_msg = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == lc.chat_id)
                .filter(RawMessage.message_id == lc.message_id)
                .one_or_none()
            )
        binding = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.chat_id == lc.chat_id)
            .filter(ExecutionBinding.message_id == lc.message_id)
            .filter(ExecutionBinding.symbol == lc.symbol)
            .filter(ExecutionBinding.side == lc.side)
            .order_by(ExecutionBinding.id.desc())
            .first()
        )
        item = _execution_item_from_lifecycle(
            lc,
            cand,
            raw_msg,
            binding,
            group_label=labels.get(int(lc.chat_id), str(lc.chat_id)),
            selected_status=selected_status,
        )
        unprotected_positions = (
            critical_by_binding.get(int(binding.id), [])
            if binding is not None
            else []
        )
        item["critical_unprotected"] = bool(unprotected_positions)
        item["unprotected_positions"] = unprotected_positions
        _apply_lifecycle_display_fields(item, lc, raw_msg, session=session)
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _execution_item_from_lifecycle(
    lc,
    cand,
    raw_msg,
    binding,
    *,
    group_label: str,
    selected_status: str,
) -> dict[str, object]:
    entry_text = _format_lifecycle_entry_text(lc.entry_range_low, lc.entry_range_high)
    stop_loss_text = f"{lc.stop_loss:g}" if lc.stop_loss is not None else None
    take_profit_text = lc.take_profit
    if cand is not None:
        entry_text = cand.entry_text or entry_text
        stop_loss_text = cand.stop_loss_text or stop_loss_text
        take_profit_text = cand.take_profit_text or take_profit_text
    item: dict[str, object] = {
        "lifecycle_id": lc.id,
        "chat_id": lc.chat_id,
        "group_label": group_label,
        "message_id": lc.message_id,
        "symbol": lc.symbol,
        "side": lc.side,
        "status": selected_status,
        "lifecycle_status": lc.lifecycle_status,
        "exit_reason": lc.exit_reason,
        "entry_text": entry_text,
        "stop_loss_text": stop_loss_text,
        "take_profit_text": take_profit_text,
        "entry_price_actual": lc.entry_price_actual,
        "exit_price_actual": lc.exit_price_actual,
        "signal_at": utc_naive_to_local(lc.signal_at),
        "entered_at": utc_naive_to_local(lc.entered_at),
        "exited_at": utc_naive_to_local(lc.exited_at),
        "last_checked_at": utc_naive_to_local(lc.last_checked_at),
        "posted_at": utc_naive_to_local(raw_msg.posted_at) if raw_msg is not None else None,
        "sender_name": raw_msg.sender_name if raw_msg is not None else None,
        "original_text": _compact_strategy_text(raw_msg.text, limit=180) if raw_msg is not None else None,
        "execution_binding_id": binding.id if binding is not None else None,
        "execution_status": binding.status if binding is not None else "unbound",
        "exchange_status": binding.last_exchange_status if binding is not None else None,
        "pos_id": binding.pos_id if binding is not None else None,
        "order_id": binding.order_id if binding is not None else None,
        "position_size_text": _format_position_size_text(
            symbol=lc.symbol,
            entry_price_actual=lc.entry_price_actual,
            entry_low=lc.entry_range_low,
            entry_high=lc.entry_range_high,
            stop_loss=lc.stop_loss,
        ),
        "position_size_risk_usdt": POSITION_SIZE_RISK_USDT,
    }
    return _add_strategy_time_display_fields(item)


def _count_execution_lifecycles(
    session,
    lifecycle_status: str,
    *,
    whitelist_map: dict[int, set[str]],
) -> int:
    from telegram_kol_research.models import StrategyLifecycle

    rows = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.lifecycle_status == lifecycle_status)
        .all()
    )
    if lifecycle_status != "pending_entry":
        return len(rows)
    return sum(
        1
        for lc in rows
        if _is_actionable_pending_entry(
            symbol=lc.symbol,
            entry_low=lc.entry_range_low,
            entry_high=lc.entry_range_high,
            allowed_symbols=whitelist_map.get(int(lc.chat_id)),
        )
    )


def _compact_strategy_text(text: str | None, *, limit: int = 180) -> str | None:
    if not text:
        return None
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."
