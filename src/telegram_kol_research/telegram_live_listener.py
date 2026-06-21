"""Realtime Telegram listener helpers for web live updates."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig, load_ai_recognition_config
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.message_recognition import (
    filter_records_by_inserted_message_keys,
    recognize_message_now,
    recognize_records_with_ai_config,
)
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.raw_ingest import normalize_message_payload, persist_normalized_messages
from telegram_kol_research.raw_ingest import repair_history_checkpoints
from telegram_kol_research.strategy_alerts import process_strategy_alert_for_record
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
    chat_title: str | None = None,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    strategy_alert_processor=process_strategy_alert_for_record,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
) -> dict[str, int]:
    """Normalize and persist one live Telegram event into the existing raw ingest flow.

    When AI recognition config or a config path is provided, the freshly
    persisted message is immediately submitted for AI strategy recognition
    (text -> text AI, image -> GLM-OCR -> text AI, video -> skipped). When
    a path is provided it is loaded per message so prompt edits take effect
    without restarting the listener.
    """

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
    stats = persist_normalized_messages(
        session_factory,
        [record],
        sync_kind="live",
        broker=broker,
    )

    # ── Immediately run AI recognition on every newly persisted message ──
    inserted_keys = stats.get("inserted_message_keys") or []
    recog_result = None
    live_ai_config = ai_recognition_config
    if ai_recognition_config_path is not None:
        live_ai_config = load_ai_recognition_config(ai_recognition_config_path)
    if inserted_keys and live_ai_config is not None:
        chat_id, message_id = inserted_keys[0]
        with session_factory() as session:
            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == chat_id,
                    RawMessage.message_id == message_id,
                )
                .one_or_none()
            )
        if raw_message is not None:
            recog_result = await asyncio.to_thread(
                recognize_message_now,
                session_factory,
                raw_message_id=raw_message.id,
                ai_recognition_config=live_ai_config,
            )
            # ── exit signal → lifecycle monitor ──
            if lifecycle_monitor is not None and recog_result is not None:
                ai_payload = recog_result.ai_payload or {}
                strategy_kind = ai_payload.get("strategy_kind")
                if strategy_kind == "exit":
                    strategy = ai_payload.get("strategy") or {}
                    symbol = str(strategy.get("symbol") or "")
                    side = str(strategy.get("side") or "")
                    if symbol and side:
                        await lifecycle_monitor.on_new_exit_signal(
                            chat_id=chat_id,
                            symbol=symbol,
                            side=side,
                            message_id=message_id,
                        )

    if (
        strategy_alert_config is not None
        and chat_title
        and (
            strategy_alert_enabled_for_title is None
            or strategy_alert_enabled_for_title(chat_title)
        )
    ):
        await strategy_alert_processor(
            session_factory=session_factory,
            record=record,
            chat_title=chat_title,
            config=strategy_alert_config,
            recognition_result=recog_result,
        )
    return stats


async def run_live_listener(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
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
            chat_title=title,
            strategy_alert_config=strategy_alert_config,
            strategy_alert_enabled_for_title=strategy_alert_enabled_for_title,
            ai_recognition_config=ai_recognition_config,
            ai_recognition_config_path=ai_recognition_config_path,
            lifecycle_monitor=lifecycle_monitor,
        )

    add_event_handler = getattr(client, "add_event_handler", None)
    if not callable(add_event_handler):
        raise RuntimeError("Telegram client does not support realtime event handlers")

    connect = getattr(client, "connect", None)
    if callable(connect):
        await maybe_await(connect())

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
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path | None = None,
    lifecycle_monitor: Any | None = None,
) -> asyncio.Task[None]:
    """Schedule the realtime listener in the current event loop."""

    kwargs = _filter_callable_kwargs(
        runner,
        {
            "client": client,
            "session_factory": session_factory,
            "broker": broker,
            "target_titles": target_titles,
            "media_root": media_root,
            "strategy_alert_config": strategy_alert_config,
            "strategy_alert_enabled_for_title": strategy_alert_enabled_for_title,
            "ai_recognition_config": ai_recognition_config,
            "ai_recognition_config_path": ai_recognition_config_path,
            "lifecycle_monitor": lifecycle_monitor,
        },
    )
    return asyncio.create_task(runner(**kwargs))


def _filter_callable_kwargs(callback: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(callback)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }


async def run_reconcile_once(
    *,
    client: Any,
    session_factory,
    broker,
    target_titles: set[str],
    media_root: str | Path = "data/media",
    message_limit: int = 50,
    checkpoint_overlap: int = 5,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
    strategy_alert_processor=process_strategy_alert_for_record,
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

        # ── Also re-fetch messages whose media download failed earlier ──
        dialog_id = int(dialog.get("id") or 0)
        with session_factory() as session:
            orphan_rows = (
                session.query(RawMessage.message_id)
                .join(MediaAsset, MediaAsset.raw_message_id == RawMessage.id)
                .filter(
                    RawMessage.chat_id == dialog_id,
                    MediaAsset.local_path.is_(None),
                )
                .all()
            )
        orphan_msg_ids = {row.message_id for row in orphan_rows}
        if orphan_msg_ids:
            lowest_orphan = min(orphan_msg_ids)
            if lowest_orphan > 0:
                replay_floor = min(replay_floor, max(0, lowest_orphan - 1))

        payloads = [
            payload
            for payload in payloads
            if int(payload.get("message_id") or 0) > replay_floor
        ]
        records = [
            normalize_message_payload(payload, archived_target_group=True)
            for payload in payloads
            if int(payload.get("message_id") or 0) > checkpoint_message_id
            or int(payload.get("message_id") or 0) in orphan_msg_ids
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
        candidate_stats = recognize_records_with_ai_config(
            session_factory,
            filter_records_by_inserted_message_keys(records, stats),
            fallback_recognizer=persist_text_signal_candidates,
        )
        inserted_candidates += candidate_stats["inserted_candidates"]
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]
        dialog_title = str(dialog.get("title") or "")
        if (
            strategy_alert_config is not None
            and (
                strategy_alert_enabled_for_title is None
                or strategy_alert_enabled_for_title(dialog_title)
            )
        ):
            for record in records:
                await strategy_alert_processor(
                    session_factory=session_factory,
                    record=record,
                    chat_title=dialog_title,
                    config=strategy_alert_config,
                )

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
    operation_lock: Any | None = None,
    strategy_alert_config: Any | None = None,
    strategy_alert_enabled_for_title: Callable[[str], bool] | None = None,
) -> None:
    """Periodically replay a small recent history window for missed-message recovery."""

    while True:
        if operation_lock is None:
            await run_reconcile_once(
                client=client,
                session_factory=session_factory,
                broker=broker,
                target_titles=target_titles,
                media_root=media_root,
                message_limit=message_limit,
                strategy_alert_config=strategy_alert_config,
                strategy_alert_enabled_for_title=strategy_alert_enabled_for_title,
            )
        else:
            async with operation_lock:
                await run_reconcile_once(
                    client=client,
                    session_factory=session_factory,
                    broker=broker,
                    target_titles=target_titles,
                    media_root=media_root,
                    message_limit=message_limit,
                    strategy_alert_config=strategy_alert_config,
                    strategy_alert_enabled_for_title=strategy_alert_enabled_for_title,
                )
        await asyncio.sleep(interval_seconds)
