"""Helpers for normalizing Telegram messages before database persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MediaAsset, RawMessage, SyncCheckpoint


@dataclass(slots=True)
class NormalizedMessageRecord:
    chat_id: int
    message_id: int
    sender_id: int | None
    sender_name: str | None
    text: str | None
    reply_to_message_id: int | None
    media_kind: str | None
    media_path: str | None
    media_payload: dict[str, Any] | None
    archived_target_group: bool
    posted_at: datetime | None
    edit_date: datetime | None
    raw_payload: str


def normalize_message_payload(
    payload: dict[str, Any],
    *,
    archived_target_group: bool = False,
) -> NormalizedMessageRecord:
    """Normalize a Telegram message payload into a storage-friendly record."""

    media = payload.get("media") or {}
    return NormalizedMessageRecord(
        chat_id=payload["chat_id"],
        message_id=payload["message_id"],
        sender_id=payload.get("sender_id"),
        sender_name=payload.get("sender_name"),
        text=payload.get("text"),
        reply_to_message_id=payload.get("reply_to_msg_id"),
        media_kind=media.get("kind"),
        media_path=media.get("path"),
        media_payload=media or None,
        archived_target_group=archived_target_group,
        posted_at=_parse_optional_datetime(payload.get("posted_at")),
        edit_date=_parse_optional_datetime(payload.get("edit_date")),
        raw_payload=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        ),
    )


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def persist_normalized_messages(
    session_factory: sessionmaker,
    records: list[NormalizedMessageRecord],
    *,
    sync_kind: str = "history",
    broker=None,
) -> dict[str, int]:
    """Persist normalized raw messages, media metadata, and sync checkpoints."""

    inserted_messages = 0
    inserted_media_assets = 0

    with session_factory() as session:
        latest_records_by_chat: dict[tuple[int, str], NormalizedMessageRecord] = {}
        for record in records:
            inserted_current_message = False
            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == record.chat_id,
                    RawMessage.message_id == record.message_id,
                )
                .one_or_none()
            )
            if raw_message is None:
                raw_message = RawMessage(
                    chat_id=record.chat_id,
                    message_id=record.message_id,
                    sender_id=record.sender_id,
                    sender_name=record.sender_name,
                    text=record.text,
                    raw_payload=record.raw_payload,
                    reply_to_message_id=record.reply_to_message_id,
                    archived_target_group=record.archived_target_group,
                    posted_at=record.posted_at,
                    edit_date=record.edit_date,
                )
                session.add(raw_message)
                session.flush()
                inserted_messages += 1
                inserted_current_message = True
            else:
                raw_message.sender_id = record.sender_id
                raw_message.sender_name = record.sender_name
                raw_message.text = record.text
                raw_message.raw_payload = record.raw_payload
                raw_message.reply_to_message_id = record.reply_to_message_id
                raw_message.archived_target_group = record.archived_target_group
                raw_message.posted_at = record.posted_at
                raw_message.edit_date = record.edit_date

            if record.media_kind:
                media_asset = (
                    session.query(MediaAsset)
                    .filter(
                        MediaAsset.raw_message_id == raw_message.id,
                        MediaAsset.kind == record.media_kind,
                        MediaAsset.local_path == record.media_path,
                    )
                    .one_or_none()
                )
                if media_asset is None:
                    session.add(
                        MediaAsset(
                            raw_message_id=raw_message.id,
                            kind=record.media_kind,
                            local_path=record.media_path,
                        )
                    )
                    inserted_media_assets += 1

            checkpoint_key = (record.chat_id, sync_kind)
            existing_latest = latest_records_by_chat.get(checkpoint_key)
            if existing_latest is None or record.message_id > existing_latest.message_id:
                latest_records_by_chat[checkpoint_key] = record

        for (chat_id, checkpoint_sync_kind), record in latest_records_by_chat.items():
            checkpoint = (
                session.query(SyncCheckpoint)
                .filter(
                    SyncCheckpoint.chat_id == chat_id,
                    SyncCheckpoint.sync_kind == checkpoint_sync_kind,
                )
                .one_or_none()
            )
            if checkpoint is None:
                checkpoint = SyncCheckpoint(chat_id=chat_id, sync_kind=checkpoint_sync_kind)
                session.add(checkpoint)

            checkpoint.last_message_id = record.message_id
            checkpoint.last_message_at = record.posted_at

        session.commit()

    if broker is not None:
        for record in records:
            broker.publish_message(chat_id=record.chat_id, message_id=record.message_id)

    return {
        "inserted_messages": inserted_messages,
        "inserted_media_assets": inserted_media_assets,
        "processed_records": len(records),
    }


def repair_history_checkpoints(session_factory: sessionmaker) -> dict[str, int]:
    """Repair stale history checkpoints using the latest persisted raw message per chat."""

    repaired_checkpoints = 0

    with session_factory() as session:
        chat_ids = [
            chat_id
            for (chat_id,) in session.query(RawMessage.chat_id)
            .distinct()
            .all()
        ]

        for chat_id in chat_ids:
            latest_message = (
                session.query(RawMessage)
                .filter(RawMessage.chat_id == chat_id)
                .order_by(RawMessage.message_id.desc())
                .first()
            )
            if latest_message is None:
                continue

            checkpoint = (
                session.query(SyncCheckpoint)
                .filter(
                    SyncCheckpoint.chat_id == chat_id,
                    SyncCheckpoint.sync_kind == "history",
                )
                .one_or_none()
            )
            if checkpoint is None:
                checkpoint = SyncCheckpoint(chat_id=chat_id, sync_kind="history")
                session.add(checkpoint)

            if checkpoint.last_message_id != latest_message.message_id:
                checkpoint.last_message_id = latest_message.message_id
                checkpoint.last_message_at = latest_message.posted_at
                repaired_checkpoints += 1

        session.commit()

    return {"repaired_checkpoints": repaired_checkpoints}
