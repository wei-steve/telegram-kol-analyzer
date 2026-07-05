"""Telegram Bot command handling for operator shortcuts."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.strategy_alerts import StrategyAlertConfig
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.web_queries import list_holding_strategies, list_pending_strategies
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle, utc_now
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.trade_signals import enqueue_trade_signal


POSITIONS_COMMAND = "positions"
PENDING_COMMAND = "pending"
MAX_TELEGRAM_MESSAGE_CHARS = 3900
EXPIRY_CONTINUE_COMMAND = "expiry_continue"
EXPIRY_EXPIRE_CANCEL_COMMAND = "expiry_expire_cancel"
EXPIRY_EXPIRE_KEEP_COMMAND = "expiry_expire_keep"
logger = logging.getLogger(__name__)


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


async def run_system_operator_bot_command_loop(
    *,
    config: SystemOperatorBotConfig,
    session_factory: sessionmaker,
    deepcoin_client_factory=None,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Handle commands sent to the dedicated system-operator bot."""

    base_url = f"https://api.telegram.org/bot{config.bot_token}"
    chat_id = str(config.chat_id)
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        await _delete_webhook(client, base_url)
        offset = await _latest_update_offset(client, base_url)
        while True:
            updates = await _get_updates(client, base_url, offset=offset)
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                offset = max(offset, update_id + 1)
                try:
                    callback = update.get("callback_query") or {}
                    if callback:
                        message = callback.get("message") or {}
                        if not _message_is_from_alert_chat(message, chat_id):
                            continue
                        callback_data = str(callback.get("data") or "")
                        deepcoin_client = (
                            deepcoin_client_factory()
                            if deepcoin_client_factory and callback_data.startswith("expiry_expire_cancel:")
                            else None
                        )
                        response_text = process_system_operator_callback_data(
                            session_factory,
                            callback_data,
                            deepcoin_client=deepcoin_client,
                        )
                        await _answer_callback_query(
                            client,
                            base_url,
                            callback_query_id=str(callback.get("id") or ""),
                            text=response_text or "未识别的操作",
                        )
                        if response_text:
                            await _send_message(client, base_url, chat_id=chat_id, text=response_text)
                        continue

                    message = update.get("message") or {}
                    if not _message_is_from_alert_chat(message, chat_id):
                        continue
                    text = str(message.get("text") or "").strip()
                    deepcoin_client = (
                        deepcoin_client_factory()
                        if deepcoin_client_factory and _command_name(text) == EXPIRY_EXPIRE_CANCEL_COMMAND
                        else None
                    )
                    response_text = process_system_operator_command(
                        session_factory,
                        text,
                        deepcoin_client=deepcoin_client,
                    )
                    if response_text:
                        await _send_message(client, base_url, chat_id=chat_id, text=response_text)
                except Exception:
                    logger.exception("System operator bot failed to process update_id=%s", update_id)
            await asyncio.sleep(poll_interval_seconds)


def process_system_operator_callback_data(
    session_factory: sessionmaker,
    callback_data: str,
    *,
    now: datetime | None = None,
    deepcoin_client=None,
) -> str | None:
    action, sep, identifier = callback_data.partition(":")
    if sep != ":":
        return None
    return _process_expiry_action(
        session_factory,
        action,
        identifier,
        now=now,
        deepcoin_client=deepcoin_client,
    )


def process_system_operator_command(
    session_factory: sessionmaker,
    text: str,
    *,
    now: datetime | None = None,
    deepcoin_client=None,
) -> str | None:
    command = _command_name(text)
    if command not in {
        EXPIRY_CONTINUE_COMMAND,
        EXPIRY_EXPIRE_CANCEL_COMMAND,
        EXPIRY_EXPIRE_KEEP_COMMAND,
    }:
        return None
    parts = text.split()
    if len(parts) < 2:
        return "请在命令后提供 lifecycle id，例如 /expiry_continue 442"
    return _process_expiry_action(
        session_factory,
        command,
        parts[1],
        now=now,
        deepcoin_client=deepcoin_client,
    )


def _process_expiry_action(
    session_factory: sessionmaker,
    command: str,
    identifier: str,
    *,
    now: datetime | None = None,
    deepcoin_client=None,
) -> str | None:
    lifecycle_id = _parse_operator_lifecycle_identifier(session_factory, identifier)
    if lifecycle_id is None:
        return "未找到对应策略，请使用内部ID或策略代码，例如 /expiry_continue #3251。"

    event_at = now or utc_now()
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            return f"未找到策略 #{lifecycle_id}。"

        if command == EXPIRY_CONTINUE_COMMAND:
            lifecycle.lifecycle_status = "pending_entry"
            lifecycle.exit_reason = None
            lifecycle.exited_at = None
            lifecycle.management_action = "expiry_review_continued"
            lifecycle.management_note = "人工确认继续等待，暂不标记过期。"
            lifecycle.last_checked_at = event_at
            lifecycle.updated_at = event_at
            session.commit()
            return f"策略 #{lifecycle_id} 已继续等待。"

        if command == EXPIRY_EXPIRE_KEEP_COMMAND:
            lifecycle.lifecycle_status = "expired"
            lifecycle.exit_reason = "expired"
            lifecycle.exited_at = event_at
            lifecycle.management_action = "expiry_expired_keep_order"
            lifecycle.management_note = "人工确认标记过期，但保留交易所挂单。"
            lifecycle.updated_at = event_at
            session.commit()
            return f"策略 #{lifecycle_id} 已标记过期，交易所挂单保留。"

        live_binding = _load_live_binding(session, lifecycle)
        if live_binding is not None and deepcoin_client is not None:
            trade_signal = enqueue_trade_signal(
                session_factory,
                venue="deepcoin",
                source_type="system_operator",
                kol_id=live_binding.kol_id,
                chat_id=live_binding.chat_id,
                message_id=live_binding.message_id,
                symbol=live_binding.symbol,
                side=live_binding.side,
                action="cancel_entry",
                payload={"binding_id": live_binding.id, "lifecycle_id": lifecycle.id},
                strategy_instance_id=live_binding.strategy_instance_id,
                enqueued_at=event_at,
            )
            execute_deepcoin_management_signal(
                session_factory,
                trade_signal=trade_signal,
                deepcoin_client=deepcoin_client,
                executed_at=event_at,
            )
            lifecycle.lifecycle_status = "expired"
            lifecycle.exit_reason = "expired"
            lifecycle.exited_at = event_at
            lifecycle.management_action = "expiry_cancelled_and_expired"
            lifecycle.management_note = "人工确认过期，交易所挂单已撤销。"
            lifecycle.updated_at = event_at
            session.commit()
            return f"策略 #{lifecycle_id} 已撤销交易所挂单并标记过期。"

        if live_binding is not None:
            lifecycle.management_action = "expiry_cancel_requested"
            lifecycle.management_note = "人工确认标记过期并撤销挂单；等待交易所撤单执行。"
            lifecycle.last_checked_at = event_at
            lifecycle.updated_at = event_at
            session.commit()
            return f"策略 #{lifecycle_id} 已请求撤销交易所挂单，撤单完成前不会标记过期。"

        lifecycle.lifecycle_status = "expired"
        lifecycle.exit_reason = "expired"
        lifecycle.exited_at = event_at
        lifecycle.management_action = "expiry_expired_cancel_no_live_order"
        lifecycle.management_note = "人工确认标记过期并撤单；未发现本地 live 绑定。"
        lifecycle.updated_at = event_at
        session.commit()
        return f"策略 #{lifecycle_id} 未发现本地 live 挂单，已标记过期。"


def _parse_operator_lifecycle_identifier(
    session_factory: sessionmaker,
    identifier: str,
) -> int | None:
    value = str(identifier or "").strip()
    if not value:
        return None
    if value.startswith("#"):
        message_id_text = value[1:]
        if not message_id_text.isdigit():
            return None
        message_id = int(message_id_text)
        with session_factory() as session:
            row = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.message_id == message_id)
                .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
                .first()
            )
            return int(row.id) if row is not None else None
    return int(value) if value.isdigit() else None


def _lifecycle_has_live_binding(session, lifecycle: StrategyLifecycle) -> bool:
    return _load_live_binding(session, lifecycle) is not None


def _load_live_binding(session, lifecycle: StrategyLifecycle) -> ExecutionBinding | None:
    if lifecycle.execution_binding_id is not None:
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if binding is not None and binding.status in {"open", "active"}:
            return binding
    return (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
        .filter(ExecutionBinding.message_id == lifecycle.message_id)
        .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
        .filter(ExecutionBinding.side == lifecycle.side.lower())
        .filter(ExecutionBinding.status.in_(["open", "active"]))
        .first()
    )


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
            "allowed_updates": '["message","callback_query"]',
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


async def _answer_callback_query(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    callback_query_id: str,
    text: str,
) -> None:
    if not callback_query_id:
        return
    response = await client.post(
        f"{base_url}/answerCallbackQuery",
        json={
            "callback_query_id": callback_query_id,
            "text": text[:180],
            "show_alert": False,
        },
    )
    response.raise_for_status()


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    command = text.split()[0].split("@")[0].lstrip("/").lower()
    return command
