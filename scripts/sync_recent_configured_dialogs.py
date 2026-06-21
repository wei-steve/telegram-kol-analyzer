import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.message_recognition import (
    filter_records_by_inserted_message_keys,
    recognize_records_with_ai_config,
)
from telegram_kol_research.raw_ingest import (
    normalize_message_payload,
    persist_normalized_messages,
)
from telegram_kol_research.telegram_client import (
    _download_media_if_present,
    _format_sender_name,
    create_telegram_client,
    load_telegram_auth_config,
)
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates
from telegram_kol_research.models import RawMessage


def load_latest_message_ids(session_factory) -> dict[int, int]:
    latest_by_chat: dict[int, int] = {}
    with session_factory() as session:
        rows = (
            session.query(RawMessage.chat_id, RawMessage.message_id)
            .order_by(RawMessage.chat_id.asc(), RawMessage.message_id.desc())
            .all()
        )
    for chat_id, message_id in rows:
        if chat_id not in latest_by_chat:
            latest_by_chat[int(chat_id)] = int(message_id or 0)
    return latest_by_chat


async def sync_dialog(
    client,
    group,
    *,
    cutoff: datetime,
    max_messages: int | None,
    latest_message_id: int = 0,
):
    entity = await client.get_entity(group.chat_id)
    payloads = []
    async for message in client.iter_messages(entity, limit=max_messages):
        message_id = int(getattr(message, "id", 0) or 0)
        if latest_message_id and message_id <= latest_message_id:
            break
        posted_at = getattr(message, "date", None)
        if posted_at is not None and posted_at.astimezone(UTC) < cutoff:
            break
        sender = None
        get_sender = getattr(message, "get_sender", None)
        if callable(get_sender):
            sender = await get_sender()
        media = getattr(message, "media", None)
        media_path = await _download_media_if_present(
            client,
            dialog_id=group.chat_id,
            message=message,
            media_root=Path("data/media"),
            timeout_seconds=30,
        )
        payloads.append(
            {
                "chat_id": group.chat_id,
                "message_id": message_id,
                "sender_id": getattr(message, "sender_id", None),
                "sender_name": _format_sender_name(sender),
                "text": getattr(message, "message", None),
                "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
                "posted_at": posted_at,
                "edit_date": getattr(message, "edit_date", None),
                "media": {
                    "kind": type(media).__name__.lower(),
                    "path": media_path,
                }
                if media is not None
                else None,
            }
        )
    return payloads


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--config-path", default="config/groups.yaml")
    parser.add_argument("--database-path", default="data/research.db")
    parser.add_argument("--max-messages-per-group", type=int, default=0)
    args = parser.parse_args()

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    group_config = load_group_config(args.config_path)
    groups = [
        group
        for group in group_config.groups
        if group.enabled and group.chat_id is not None
    ]
    client = create_telegram_client(load_telegram_auth_config())
    session_factory = create_session_factory(args.database_path)
    latest_message_ids = load_latest_message_ids(session_factory)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Telegram session is not authorized")
        total_payloads = 0
        total_inserted = 0
        total_candidates = 0
        max_messages = args.max_messages_per_group or None
        for index, group in enumerate(groups, start=1):
            latest_message_id = latest_message_ids.get(int(group.chat_id or 0), 0)
            print(
                f"[{index}/{len(groups)}] {group.chat_title}: "
                f"checking newer than message_id={latest_message_id}",
                flush=True,
            )
            try:
                payloads = await sync_dialog(
                    client,
                    group,
                    cutoff=cutoff,
                    max_messages=max_messages,
                    latest_message_id=latest_message_id,
                )
            except Exception as exc:
                print(f"[{index}/{len(groups)}] {group.chat_title}: ERROR {exc}", flush=True)
                continue

            records = [
                normalize_message_payload(payload, archived_target_group=True)
                for payload in payloads
                if payload.get("message_id") is not None
            ]
            stats = persist_normalized_messages(
                session_factory,
                records,
                sync_kind="history",
            )
            candidate_stats = recognize_records_with_ai_config(
                session_factory,
                filter_records_by_inserted_message_keys(records, stats),
                fallback_recognizer=persist_text_signal_candidates,
            )
            trade_stats = persist_trade_ideas_from_candidates(session_factory)
            total_payloads += len(payloads)
            total_inserted += stats["inserted_messages"]
            total_candidates += candidate_stats["inserted_candidates"]
            print(
                f"[{index}/{len(groups)}] {group.chat_title}: "
                f"fetched={len(payloads)} inserted={stats['inserted_messages']} "
                f"candidates={candidate_stats['inserted_candidates']} "
                f"trades={trade_stats['inserted_trade_ideas']}",
                flush=True,
            )
        print(
            f"done groups={len(groups)} fetched={total_payloads} "
            f"inserted={total_inserted} candidates={total_candidates}",
            flush=True,
        )
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
