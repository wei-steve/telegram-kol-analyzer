"""Query helpers for the Telegram web workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import func, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MediaAsset, RawMessage


def load_group_rows(
    session_factory: sessionmaker,
    *,
    group_labels_by_title: dict[str, str] | None = None,
) -> list[dict[str, int | str | datetime | None | bool]]:
    """Load aggregated group rows ordered by most recent activity."""

    label_map = group_labels_by_title or {}
    with session_factory() as session:
        rows = (
            session.query(
                RawMessage.chat_id.label("chat_id"),
                func.max(RawMessage.posted_at).label("last_posted_at"),
                func.count(RawMessage.id).label("message_count"),
            )
            .group_by(RawMessage.chat_id)
            .order_by(func.max(RawMessage.posted_at).desc(), RawMessage.chat_id.desc())
            .all()
        )

        results: list[dict[str, int | str | datetime | None | bool]] = []
        for row in rows:
            latest_message = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == row.chat_id)
                .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
                .first()
            )
            raw_title = (
                latest_message.sender_name
                if latest_message and latest_message.sender_name
                else str(row.chat_id)
            )
            results.append(
                {
                    "chat_id": row.chat_id,
                    "title": label_map.get(raw_title, raw_title),
                    "raw_title": raw_title,
                    "last_posted_at": row.last_posted_at,
                    "message_count": row.message_count,
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
        effective_latest = latest_message_at
        if latest_message_at.tzinfo is None and now.tzinfo is not None:
            effective_latest = latest_message_at.replace(tzinfo=now.tzinfo)
        stale_hours = round((now - effective_latest).total_seconds() / 3600, 1)

    return {
        "latest_message_at": latest_message_at,
        "stale_hours": stale_hours,
    }


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

        raw_messages = (
            query.order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
            .limit(limit)
            .all()
        )

        rows: list[dict[str, object | None]] = []
        for raw_message in raw_messages:
            media_assets = (
                session.query(MediaAsset)
                .filter(MediaAsset.raw_message_id == raw_message.id)
                .order_by(MediaAsset.id.asc())
                .all()
            )
            rows.append(
                {
                    "raw_message_id": raw_message.id,
                    "chat_id": raw_message.chat_id,
                    "message_id": raw_message.message_id,
                    "sender_id": raw_message.sender_id,
                    "sender_name": raw_message.sender_name,
                    "posted_at": raw_message.posted_at,
                    "edit_date": raw_message.edit_date,
                    "text": raw_message.text,
                    "reply_to_message_id": raw_message.reply_to_message_id,
                    "media_assets": [
                        {
                            "id": media_asset.id,
                            "kind": media_asset.kind,
                            "mime_type": media_asset.mime_type,
                            "local_path": media_asset.local_path,
                            "ocr_text": media_asset.ocr_text,
                        }
                        for media_asset in media_assets
                    ],
                    "reply_context": None,
                }
            )

    return rows


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
            query = query.filter(RawMessage.posted_at >= posted_after)
        if posted_before is not None:
            query = query.filter(RawMessage.posted_at <= posted_before)
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
    rows: list[dict[str, object | None]] = []
    for raw_message in raw_messages:
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
        rows.append(
            {
                "raw_message_id": raw_message.id,
                "chat_id": raw_message.chat_id,
                "message_id": raw_message.message_id,
                "sender_id": raw_message.sender_id,
                "sender_name": raw_message.sender_name,
                "posted_at": raw_message.posted_at,
                "edit_date": raw_message.edit_date,
                "text": raw_message.text,
                "reply_to_message_id": raw_message.reply_to_message_id,
                "media_assets": [
                    {
                        "id": media_asset.id,
                        "kind": media_asset.kind,
                        "mime_type": media_asset.mime_type,
                        "local_path": media_asset.local_path,
                        "ocr_text": media_asset.ocr_text,
                    }
                    for media_asset in media_assets
                ],
                "reply_context": None,
            }
        )

    return rows
