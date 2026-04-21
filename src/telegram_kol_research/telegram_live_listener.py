"""Realtime Telegram listener helpers for web live updates."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.raw_ingest import normalize_message_payload, persist_normalized_messages
from telegram_kol_research.raw_ingest import repair_history_checkpoints
from telegram_kol_research.telegram_client import (
    _download_media_if_present,
    _format_sender_name,
    discover_dialogs,
    fetch_dialog_messages,
    filter_target_dialogs,
    maybe_await,
)
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates


async def persist_live_message_event(
    *,
    event: Any,
    session_factory,
    broker,
    media_root: str | Path = "data/media",
) -> dict[str, int]:
    """Normalize and persist one live Telegram event into the existing raw ingest flow."""

    message = getattr(event, "message", None)
    if message is None:
        return {"inserted_messages": 0, "inserted_media_assets": 0, "processed_records": 0}

    sender = None
    get_sender = getattr(message, "get_sender", None)
    if callable(get_sender):
        sender = await get_sender()

    media_path = await _download_media_if_present(
        getattr(event, "client", None),
        dialog_id=getattr(event, "chat_id"),
        message=message,
        media_root=Path(media_root),
    ) if getattr(event, "client", None) is not None else None

    payload = {
        "chat_id": getattr(event, "chat_id"),
        "message_id": getattr(message, "id", None),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _format_sender_name(sender),
        "text": getattr(message, "message", None),
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "posted_at": getattr(message, "date", None),
        "edit_date": getattr(message, "edit_date", None),
        "media": {
            "kind": type(getattr(message, "media", None)).__name__.lower(),
            "path": media_path,
        }
        if getattr(message, "media", None) is not None
        else None,
    }
    record = normalize_message_payload(payload, archived_target_group=True)
    return persist_normalized_messages(
        session_factory,
        [record],
        sync_kind="live",
        broker=broker,
    )


async def run_live_listener(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
) -> None:
    """Attach Telethon new-message handlers and keep the client alive."""

    try:
        from telethon import events
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Telethon is not installed in the current environment. Install project dependencies first."
        ) from exc

    async def handle_new_message(event: Any) -> None:
        chat = await maybe_await(event.get_chat()) if hasattr(event, "get_chat") else None
        title = getattr(chat, "title", None)
        if target_titles and title not in target_titles:
            return
        await persist_live_message_event(
            event=event,
            session_factory=session_factory,
            broker=broker,
            media_root=media_root,
        )

    add_event_handler = getattr(client, "add_event_handler", None)
    if not callable(add_event_handler):
        raise RuntimeError("Telegram client does not support realtime event handlers")

    add_event_handler(handle_new_message, events.NewMessage())
    run_until_disconnected = getattr(client, "run_until_disconnected", None)
    if callable(run_until_disconnected):
        await maybe_await(run_until_disconnected())


def launch_live_listener_task(
    *,
    runner: Callable[..., Awaitable[None]],
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path,
) -> asyncio.Task[None]:
    """Schedule the realtime listener in the current event loop."""

    return asyncio.create_task(
        runner(
            client=client,
            session_factory=session_factory,
            broker=broker,
            target_titles=target_titles,
            media_root=media_root,
        )
    )


async def run_reconcile_once(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    message_limit: int = 50,
    checkpoint_overlap: int = 5,
    discover_dialogs_fn=discover_dialogs,
    fetch_dialog_messages_fn=fetch_dialog_messages,
) -> dict[str, int]:
    """Fetch a recent overlap window and persist only messages newer than the history checkpoint."""

    repair_history_checkpoints(session_factory)
    dialogs = await discover_dialogs_fn(client)
    matched_dialogs = filter_target_dialogs(dialogs, target_titles)

    history_checkpoints: dict[int, int] = {}
    with session_factory() as session:
        from telegram_kol_research.models import SyncCheckpoint

        checkpoints = (
            session.query(SyncCheckpoint)
            .filter(SyncCheckpoint.sync_kind == "history")
            .all()
        )
        history_checkpoints = {
            checkpoint.chat_id: int(checkpoint.last_message_id or 0)
            for checkpoint in checkpoints
        }

    inserted_messages = 0
    inserted_candidates = 0
    inserted_trade_ideas = 0

    for dialog in matched_dialogs:
        payloads = await fetch_dialog_messages_fn(
            client,
            dialog,
            limit=message_limit,
            media_root=media_root,
        )
        checkpoint_message_id = history_checkpoints.get(int(dialog.get("id") or 0), 0)
        replay_floor = max(0, checkpoint_message_id - checkpoint_overlap)
        payloads = [
            payload
            for payload in payloads
            if int(payload.get("message_id") or 0) > replay_floor
        ]
        records = [
            normalize_message_payload(payload, archived_target_group=True)
            for payload in payloads
            if int(payload.get("message_id") or 0) > checkpoint_message_id
        ]
        if not records:
            continue

        stats = persist_normalized_messages(
            session_factory,
            records,
            sync_kind="history",
            broker=broker,
        )
        inserted_messages += stats["inserted_messages"]
        candidate_stats = persist_text_signal_candidates(session_factory, records)
        inserted_candidates += candidate_stats["inserted_candidates"]
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]

    return {
        "matched_dialogs": len(matched_dialogs),
        "inserted_messages": inserted_messages,
        "inserted_candidates": inserted_candidates,
        "inserted_trade_ideas": inserted_trade_ideas,
    }


async def run_periodic_reconcile(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    interval_seconds: int = 300,
    message_limit: int = 50,
) -> None:
    """Periodically replay a small recent history window for missed-message recovery."""

    while True:
        await run_reconcile_once(
            client=client,
            session_factory=session_factory,
            broker=broker,
            target_titles=target_titles,
            media_root=media_root,
            message_limit=message_limit,
        )
        await asyncio.sleep(interval_seconds)
