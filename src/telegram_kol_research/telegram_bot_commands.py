"""Telegram Bot command handling for operator shortcuts."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.strategy_alerts import StrategyAlertConfig
from telegram_kol_research.web_queries import list_holding_strategies, list_pending_strategies


POSITIONS_COMMAND = "positions"
PENDING_COMMAND = "pending"
MAX_TELEGRAM_MESSAGE_CHARS = 3900


async def run_telegram_bot_command_loop(
    *,
    config: StrategyAlertConfig,
    session_factory: sessionmaker,
    group_config: GroupConfig,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Register bot commands and answer operator shortcut messages."""

    base_url = f"https://api.telegram.org/bot{config.bot_token}"
    chat_id = str(config.alert_chat_id)
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        await _delete_webhook(client, base_url)
        await _set_bot_commands(client, base_url)
        offset = await _latest_update_offset(client, base_url)
        while True:
            updates = await _get_updates(client, base_url, offset=offset)
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                offset = max(offset, update_id + 1)
                message = update.get("message") or {}
                if not _message_is_from_alert_chat(message, chat_id):
                    continue
                text = str(message.get("text") or "").strip()
                if _is_positions_command(text):
                    response_text = format_holding_positions_message(
                        session_factory=session_factory,
                        group_config=group_config,
                    )
                    for chunk in split_telegram_message(response_text):
                        await _send_message(client, base_url, chat_id=chat_id, text=chunk)
                elif _is_pending_command(text):
                    response_text = format_pending_positions_message(
                        session_factory=session_factory,
                        group_config=group_config,
                    )
                    for chunk in split_telegram_message(response_text):
                        await _send_message(client, base_url, chat_id=chat_id, text=chunk)
                elif text in {"/start", "/help"}:
                    await _send_message(
                        client,
                        base_url,
                        chat_id=chat_id,
                        text=(
                            "可用命令:\n"
                            "/positions - 查询当前 KOL 群组持仓策略\n"
                            "/pending - 查询当前 KOL 群组待入场策略"
                        ),
                    )
            await asyncio.sleep(poll_interval_seconds)


def format_holding_positions_message(
    *,
    session_factory: sessionmaker,
    group_config: GroupConfig,
    limit: int = 200,
) -> str:
    """Build the current holding strategy list for Telegram."""

    positions = list_holding_strategies(session_factory, limit=limit)
    label_by_chat_id = _group_label_by_chat_id(group_config)
    if not positions:
        return "【当前持仓策略】\n暂无持仓中的 KOL 策略。"

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        chat_id = position.get("chat_id")
        if chat_id is None:
            continue
        grouped[int(chat_id)].append(position)

    lines = [
        "【当前持仓策略】",
        f"共 {len(positions)} 条，涉及 {len(grouped)} 个群组",
    ]
    for group_index, (chat_id, group_positions) in enumerate(grouped.items(), start=1):
        title = label_by_chat_id.get(chat_id, str(chat_id))
        lines.extend(["", f"{group_index}. {title}"])
        for item_index, item in enumerate(group_positions, start=1):
            lines.extend(_format_position_lines(item_index, item))
    if len(positions) >= limit:
        lines.extend(["", f"仅显示前 {limit} 条。"])
    return "\n".join(lines)


def format_pending_positions_message(
    *,
    session_factory: sessionmaker,
    group_config: GroupConfig,
    limit: int = 200,
) -> str:
    """Build the current pending-entry strategy list for Telegram."""

    positions = list_pending_strategies(session_factory, limit=limit)
    label_by_chat_id = _group_label_by_chat_id(group_config)
    if not positions:
        return "【当前待入场策略】\n暂无待入场且未入场的 KOL 策略。"

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        chat_id = position.get("chat_id")
        if chat_id is None:
            continue
        grouped[int(chat_id)].append(position)

    lines = [
        "【当前待入场策略】",
        f"共 {len(positions)} 条，涉及 {len(grouped)} 个群组",
    ]
    for group_index, (chat_id, group_positions) in enumerate(grouped.items(), start=1):
        title = label_by_chat_id.get(chat_id, str(chat_id))
        lines.extend(["", f"{group_index}. {title}"])
        for item_index, item in enumerate(group_positions, start=1):
            lines.extend(_format_pending_position_lines(item_index, item))
    if len(positions) >= limit:
        lines.extend(["", f"仅显示前 {limit} 条。"])
    return "\n".join(lines)


def split_telegram_message(text: str, *, max_chars: int = MAX_TELEGRAM_MESSAGE_CHARS) -> list[str]:
    """Split long Telegram messages on line boundaries."""

    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_position_lines(index: int, item: dict[str, Any]) -> list[str]:
    side = {"long": "多", "short": "空"}.get(
        str(item.get("side") or "").lower(),
        str(item.get("side") or "-"),
    )
    entered_at = _format_datetime(item.get("entered_at"))
    latest_at = _format_datetime(item.get("latest_event_at") or item.get("entered_at"))
    symbol = item.get("symbol") or "-"
    entry = item.get("entry_price_actual") or item.get("entry_text") or "-"
    take_profit = item.get("take_profit_text") or item.get("take_profit") or "-"
    stop_loss = item.get("stop_loss_text") or item.get("stop_loss") or "-"
    management = item.get("management_action_label")
    lines = [
        f"  {index}) {symbol} {side}",
        f"     入场: {entry} | 止盈: {take_profit} | 止损: {stop_loss}",
    ]
    if entered_at:
        lines.append(f"     入场时间: {entered_at}")
    if latest_at and latest_at != entered_at:
        lines.append(f"     最新更新: {latest_at}")
    if management:
        lines.append(f"     状态: {management}")
    return lines


def _format_pending_position_lines(index: int, item: dict[str, Any]) -> list[str]:
    side = {"long": "多", "short": "空"}.get(
        str(item.get("side") or "").lower(),
        str(item.get("side") or "-"),
    )
    signal_at = _format_datetime(item.get("signal_at") or item.get("posted_at"))
    latest_at = _format_datetime(item.get("latest_event_at") or item.get("last_checked_at"))
    symbol = item.get("symbol") or "-"
    entry = item.get("entry_range_text") or item.get("entry_text") or "-"
    take_profit = item.get("take_profit_text") or item.get("take_profit") or "-"
    stop_loss = item.get("stop_loss_text") or item.get("stop_loss") or "-"
    lines = [
        f"  {index}) {symbol} {side}",
        f"     挂单/入场区间: {entry} | 止盈: {take_profit} | 止损: {stop_loss}",
    ]
    if signal_at:
        lines.append(f"     策略时间: {signal_at}")
    if latest_at:
        lines.append(f"     最新检查: {latest_at}")
    return lines


def _group_label_by_chat_id(group_config: GroupConfig) -> dict[int, str]:
    labels: dict[int, str] = {}
    for group in group_config.groups:
        chat_id = getattr(group, "chat_id", None)
        if chat_id is None:
            continue
        title = getattr(group, "custom_group_label", None) or getattr(group, "chat_title", None)
        labels[int(chat_id)] = str(title or chat_id)
    return labels


def _format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


async def _delete_webhook(client: httpx.AsyncClient, base_url: str) -> None:
    response = await client.post(f"{base_url}/deleteWebhook")
    response.raise_for_status()


async def _set_bot_commands(client: httpx.AsyncClient, base_url: str) -> None:
    response = await client.post(
        f"{base_url}/setMyCommands",
        json={
            "commands": [
                {
                    "command": POSITIONS_COMMAND,
                    "description": "查询当前 KOL 群组持仓策略",
                },
                {
                    "command": PENDING_COMMAND,
                    "description": "查询当前 KOL 群组待入场策略",
                }
            ]
        },
    )
    response.raise_for_status()


async def _latest_update_offset(client: httpx.AsyncClient, base_url: str) -> int:
    response = await client.get(f"{base_url}/getUpdates", params={"timeout": 0, "limit": 100})
    response.raise_for_status()
    updates = response.json().get("result") or []
    update_ids = [int(update.get("update_id") or 0) for update in updates]
    return max(update_ids, default=0) + 1 if update_ids else 0


async def _get_updates(client: httpx.AsyncClient, base_url: str, *, offset: int) -> list[dict[str, Any]]:
    response = await client.get(
        f"{base_url}/getUpdates",
        params={
            "offset": offset,
            "timeout": 25,
            "allowed_updates": '["message"]',
        },
    )
    response.raise_for_status()
    return list(response.json().get("result") or [])


async def _send_message(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    chat_id: str,
    text: str,
) -> None:
    response = await client.post(
        f"{base_url}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )
    response.raise_for_status()


def _message_is_from_alert_chat(message: dict[str, Any], chat_id: str) -> bool:
    chat = message.get("chat") or {}
    return str(chat.get("id") or "") == chat_id


def _is_positions_command(text: str) -> bool:
    return _command_name(text) == POSITIONS_COMMAND


def _is_pending_command(text: str) -> bool:
    return _command_name(text) == PENDING_COMMAND


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    command = text.split()[0].split("@")[0].lstrip("/").lower()
    return command
