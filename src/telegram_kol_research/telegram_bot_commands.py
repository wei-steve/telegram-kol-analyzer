"""Telegram Bot command handling for operator shortcuts."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.strategy_alerts import StrategyAlertConfig
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    build_pending_entry_expiry_review_reply_markup,
)
from telegram_kol_research.web_queries import list_holding_strategies, list_pending_strategies
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyLifecycle,
    utc_now,
)
from telegram_kol_research.execution_bindings import (
    binding_has_unresolved_entry_leg,
    reconcile_deepcoin_execution_bindings_read_only,
)
from telegram_kol_research.deepcoin_execution_actions import (
    cancel_pending_entry_legs,
    execute_deepcoin_management_signal,
)
from telegram_kol_research.trade_signals import enqueue_trade_signal


POSITIONS_COMMAND = "positions"
PENDING_COMMAND = "pending"
MAX_TELEGRAM_MESSAGE_CHARS = 3900
EXPIRY_CONTINUE_COMMAND = "expiry_continue"
EXPIRY_EXPIRE_CANCEL_COMMAND = "expiry_expire_cancel"
EXPIRY_EXPIRE_KEEP_COMMAND = "expiry_expire_keep"
EXPIRY_REFRESH_COMMAND = "expiry_refresh"
EXPIRY_REVIEW_CONTINUE_HOURS = 3
EXPIRY_REFRESH_ELIGIBLE_LIFECYCLE_STATUSES = frozenset({"pending_entry", "entered"})
EXPIRY_LEG_STATUS_LABELS = {
    "active": "已入场",
    "partially_filled": "部分成交",
    "partial_filled": "部分成交",
    "partial": "部分成交",
    "pending": "挂单中",
    "open": "挂单中",
    "submitted": "已提交",
    "cancelled": "已取消",
    "manually_cancelled": "已取消",
    "exchange_cancelled": "已取消",
    "closed": "已结束",
    "expired": "已失效",
    "invalidated": "已失效",
    "unknown": "状态待确认",
}
EXPIRY_LIFECYCLE_STATUS_LABELS = {
    "pending_entry": "待入场",
    "entered": "已入场",
    "cancelled": "已取消",
    "expired": "已过期",
    "exited": "已离场",
    "invalidated": "已失效",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpiryReviewRefreshResult:
    answer_text: str
    status_text: str
    keep_actions: bool


def _log_system_operator_callback_processed(*, update_id: int, callback_data: str) -> None:
    """Log callback handling without retaining untrusted callback payload data."""
    del callback_data
    logger.info("System operator bot processing callback update_id=%s", update_id)


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
    async with httpx.AsyncClient(timeout=_bot_http_timeout(config.timeout_seconds)) as client:
        await _delete_webhook(client, base_url)
        await _set_bot_commands(client, base_url)
        offset = await _latest_update_offset(client, base_url)
        while True:
            try:
                updates = await _get_updates(client, base_url, offset=offset)
            except httpx.TimeoutException:
                logger.warning("Telegram bot getUpdates timed out; continuing")
                await asyncio.sleep(poll_interval_seconds)
                continue
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
    async with httpx.AsyncClient(timeout=_bot_http_timeout(config.timeout_seconds)) as client:
        await _delete_webhook(client, base_url)
        offset = await _latest_update_offset(client, base_url)
        logger.info("System operator bot command loop started chat_id=%s offset=%s", chat_id, offset)
        while True:
            try:
                updates = await _get_updates(client, base_url, offset=offset)
            except httpx.TimeoutException:
                logger.warning("System operator bot getUpdates timed out; continuing")
                await asyncio.sleep(poll_interval_seconds)
                continue
            if updates:
                logger.info("System operator bot received %d update(s)", len(updates))
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
                        _log_system_operator_callback_processed(
                            update_id=update_id,
                            callback_data=callback_data,
                        )
                        deepcoin_client = (
                            deepcoin_client_factory()
                            if deepcoin_client_factory
                            and _expiry_callback_needs_deepcoin_client(callback_data)
                            else None
                        )
                        callback_response = process_system_operator_callback_data(
                            session_factory,
                            callback_data,
                            deepcoin_client=deepcoin_client,
                        )
                        if isinstance(
                            callback_response, ExpiryReviewRefreshResult
                        ):
                            _, _, identifier = callback_data.partition(":")
                            await _finish_expiry_refresh_callback_response(
                                client,
                                base_url,
                                callback_query_id=str(callback.get("id") or ""),
                                chat_id=chat_id,
                                message_id=int(message.get("message_id") or 0),
                                lifecycle_id=int(identifier),
                                result=callback_response,
                                original_message_text=str(message.get("text") or ""),
                            )
                        elif callback_response:
                            await _finish_system_operator_callback_response(
                                client,
                                base_url,
                                callback_query_id=str(callback.get("id") or ""),
                                chat_id=chat_id,
                                message_id=int(message.get("message_id") or 0),
                                callback_data=callback_data,
                                response_text=callback_response,
                                operator_name=_callback_operator_name(callback),
                                original_message_text=str(message.get("text") or ""),
                            )
                        else:
                            await _answer_callback_query(
                                client,
                                base_url,
                                callback_query_id=str(callback.get("id") or ""),
                                text="未识别的操作",
                            )
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
) -> str | ExpiryReviewRefreshResult | None:
    action, sep, identifier = callback_data.partition(":")
    if sep != ":":
        return None
    if action == EXPIRY_REFRESH_COMMAND:
        return refresh_expiry_review_status(
            session_factory,
            identifier,
            now=now,
            deepcoin_client=deepcoin_client,
        )
    return _process_expiry_action(
        session_factory,
        action,
        identifier,
        now=now,
        deepcoin_client=deepcoin_client,
    )


def _expiry_callback_needs_deepcoin_client(callback_data: str) -> bool:
    action, separator, _ = callback_data.partition(":")
    return separator == ":" and action in {
        EXPIRY_EXPIRE_CANCEL_COMMAND,
        EXPIRY_REFRESH_COMMAND,
    }


def refresh_expiry_review_status(
    session_factory: sessionmaker,
    identifier: str,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> ExpiryReviewRefreshResult:
    lifecycle_id = _parse_operator_lifecycle_identifier(session_factory, identifier)
    if lifecycle_id is None:
        return ExpiryReviewRefreshResult(
            answer_text="未找到对应策略。",
            status_text="更新失败，未找到对应策略；未改变策略或挂单状态。",
            keep_actions=True,
        )

    event_at = now or utc_now()
    if deepcoin_client is None:
        return ExpiryReviewRefreshResult(
            answer_text=f"策略 #{lifecycle_id} 状态更新失败。",
            status_text="更新失败，Deepcoin 客户端不可用；未改变策略或挂单状态。",
            keep_actions=True,
        )

    try:
        reconcile_deepcoin_execution_bindings_read_only(
            session_factory,
            client=deepcoin_client,
            recovered_at=event_at,
        )
    except Exception as exc:
        logger.warning(
            "Expiry review status refresh failed lifecycle_id=%s error_type=%s",
            lifecycle_id,
            type(exc).__name__,
        )
        return ExpiryReviewRefreshResult(
            answer_text=f"策略 #{lifecycle_id} 状态更新失败。",
            status_text="更新失败，未改变策略或挂单状态。请稍后重试。",
            keep_actions=True,
        )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            return ExpiryReviewRefreshResult(
                answer_text=f"未找到策略 #{lifecycle_id}。",
                status_text="更新失败，策略已不存在；未改变策略或挂单状态。",
                keep_actions=True,
            )
        binding = _load_expiry_refresh_binding(session, lifecycle)
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
            if binding is not None
            else []
        )
        keep_actions = bool(
            lifecycle.lifecycle_status in EXPIRY_REFRESH_ELIGIBLE_LIFECYCLE_STATUSES
            and (
                binding is None
                or binding.last_exchange_status
                == "position_attribution_evidence_unavailable"
                or binding_has_unresolved_entry_leg(session, binding)
            )
        )
        status_text = _format_expiry_refresh_status(
            lifecycle=lifecycle,
            legs=legs,
            refreshed_at=event_at,
        )
    return ExpiryReviewRefreshResult(
        answer_text=f"策略 #{lifecycle_id} 状态已更新。",
        status_text=status_text,
        keep_actions=keep_actions,
    )


def _load_expiry_refresh_binding(session, lifecycle: StrategyLifecycle) -> ExecutionBinding | None:
    if lifecycle.execution_binding_id is not None:
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if binding is not None:
            return binding
    return (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
        .filter(ExecutionBinding.message_id == lifecycle.message_id)
        .filter(ExecutionBinding.symbol == lifecycle.symbol)
        .filter(ExecutionBinding.side == lifecycle.side)
        .order_by(ExecutionBinding.id.desc())
        .first()
    )


def _format_expiry_refresh_status(
    *,
    lifecycle: StrategyLifecycle,
    legs: list[ExecutionOrderLeg],
    refreshed_at: datetime,
) -> str:
    lifecycle_status = str(lifecycle.lifecycle_status or "unknown").lower()
    lifecycle_label = EXPIRY_LIFECYCLE_STATUS_LABELS.get(
        lifecycle_status, lifecycle_status or "未知"
    )
    entered = sum(1 for leg in legs if _expiry_leg_is_entered(leg))
    pending = sum(1 for leg in legs if _expiry_leg_is_pending(leg))
    cancelled = sum(1 for leg in legs if _expiry_leg_label(leg) == "已取消")
    progress_parts = [f"{entered}/{len(legs)} 条腿已入场"]
    if pending:
        progress_parts.append(f"{pending}/{len(legs)} 条腿挂单中")
    if cancelled:
        progress_parts.append(f"{cancelled}/{len(legs)} 条腿已取消")
    if not legs:
        progress_parts = ["状态待确认"]
    lines = [
        f"策略状态：{lifecycle_label}",
        f"入场进度：{'，'.join(progress_parts)}",
    ]
    for leg in legs:
        label = _expiry_leg_label(leg)
        identifiers = []
        if leg.pos_id:
            identifiers.append(f"仓位 ID: {_bounded_identifier(leg.pos_id)}")
        if leg.order_id and not leg.pos_id:
            identifiers.append(f"订单 ID: {_bounded_identifier(leg.order_id)}")
        suffix = f"（{'；'.join(identifiers)}）" if identifiers else ""
        lines.append(f"第{leg.leg_index}腿：{label}{suffix}")
    local_time = refreshed_at
    if local_time.tzinfo is None:
        local_time = local_time.replace(tzinfo=ZoneInfo("UTC"))
    local_time = local_time.astimezone(ZoneInfo("Asia/Shanghai"))
    lines.append(
        f"更新时间：{local_time.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai"
    )
    return "\n".join(lines)


def _expiry_leg_is_entered(leg: ExecutionOrderLeg) -> bool:
    return bool(leg.pos_id and str(leg.attribution_status or "") == "verified")


def _expiry_leg_is_pending(leg: ExecutionOrderLeg) -> bool:
    return not _expiry_leg_is_entered(leg) and str(leg.status or "").lower() in {
        "open",
        "pending",
        "submitted",
        "partially_filled",
        "partial_filled",
        "partial",
    }


def _expiry_leg_label(leg: ExecutionOrderLeg) -> str:
    if _expiry_leg_is_entered(leg):
        return "已入场"
    status = str(leg.status or "unknown").lower()
    return EXPIRY_LEG_STATUS_LABELS.get(status, f"{status or '未知'}")


def _bounded_identifier(value: Any, *, limit: int = 80) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


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
    if command not in {
        EXPIRY_CONTINUE_COMMAND,
        EXPIRY_EXPIRE_CANCEL_COMMAND,
        EXPIRY_EXPIRE_KEEP_COMMAND,
    }:
        return None
    lifecycle_id = _parse_operator_lifecycle_identifier(session_factory, identifier)
    if lifecycle_id is None:
        return "未找到对应策略，请使用内部ID或策略代码，例如 /expiry_continue #3251。"

    event_at = now or utc_now()
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            return f"未找到策略 #{lifecycle_id}。"

        if lifecycle.lifecycle_status not in {"pending_entry", "entered"}:
            return (
                f"\u7b56\u7565 #{lifecycle_id} \u5df2\u4e0d\u662f\u5f85\u5165\u573a"
                f"\uff08\u5f53\u524d: {lifecycle.lifecycle_status}\uff09\uff0c\u672c\u6b21\u64cd\u4f5c\u5df2\u5ffd\u7565\u3002"
            )
        lifecycle_was_entered = lifecycle.lifecycle_status == "entered"

        if command == EXPIRY_CONTINUE_COMMAND:
            if not lifecycle_was_entered:
                lifecycle.lifecycle_status = "pending_entry"
                lifecycle.exit_reason = None
                lifecycle.exited_at = None
            lifecycle.management_action = "expiry_review_continued"
            lifecycle.management_note = (
                (
                    "人工确认已入场策略的未触发入场腿继续等待，"
                    "持仓策略保持已入场。"
                )
                if lifecycle_was_entered
                else "人工确认继续等待，暂不标记过期。"
            )
            lifecycle.last_checked_at = event_at
            lifecycle.expiry_review_next_at = event_at + timedelta(
                hours=EXPIRY_REVIEW_CONTINUE_HOURS
            )
            lifecycle.updated_at = event_at
            session.commit()
            return f"策略 #{lifecycle_id} 已继续等待。"

        lifecycle.expiry_review_next_at = None
        if lifecycle_was_entered:
            if command == EXPIRY_EXPIRE_CANCEL_COMMAND and deepcoin_client is not None:
                binding = (
                    session.get(ExecutionBinding, lifecycle.execution_binding_id)
                    if lifecycle.execution_binding_id is not None
                    else None
                )
                if binding is None:
                    lifecycle.management_action = "expiry_pending_leg_cancel_failed"
                    lifecycle.management_note = (
                        "人工请求撤销未触发入场挂单，但未找到执行绑定；"
                        "持仓策略保持已入场，请人工确认 Deepcoin 当前委托。"
                    )
                    lifecycle.last_checked_at = event_at
                    lifecycle.updated_at = event_at
                    session.commit()
                    return (
                        f"策略 #{lifecycle_id} 未找到执行绑定，"
                        "未触发入场挂单未自动撤销；请人工确认 Deepcoin 当前委托。"
                    )
                trade_signal = enqueue_trade_signal(
                    session_factory,
                    venue="deepcoin",
                    source_type="system_operator",
                    kol_id=binding.kol_id,
                    chat_id=lifecycle.chat_id,
                    message_id=lifecycle.message_id,
                    symbol=lifecycle.symbol,
                    side=lifecycle.side,
                    action="cancel_entry",
                    payload={
                        "binding_id": lifecycle.execution_binding_id,
                        "lifecycle_id": lifecycle.id,
                    },
                    strategy_instance_id=binding.strategy_instance_id,
                    enqueued_at=event_at,
                )
                result = cancel_pending_entry_legs(
                    session_factory,
                    trade_signal=trade_signal,
                    deepcoin_client=deepcoin_client,
                    executed_at=event_at,
                )
                lifecycle.management_action = "expiry_pending_leg_cancelled"
                lifecycle.management_note = (
                    "人工确认撤销未触发入场挂单；"
                    "持仓策略保持已入场，已成交仓位不受影响。"
                )
                lifecycle.last_checked_at = event_at
                lifecycle.updated_at = event_at
                session.commit()
                return (
                    f"策略 #{lifecycle_id} 已撤销未触发入场挂单 "
                    f"{result.get('order_id') or ''}，持仓策略保持已入场。"
                )
            lifecycle.management_action = (
                "expiry_pending_leg_keep_order"
                if command == EXPIRY_EXPIRE_KEEP_COMMAND
                else "expiry_pending_leg_cancel_requested"
            )
            lifecycle.management_note = (
                "人工确认保留未触发入场挂单，持仓策略保持已入场。"
                if command == EXPIRY_EXPIRE_KEEP_COMMAND
                else (
                    "人工请求撤销未触发入场挂单；"
                    "持仓策略保持已入场，未自动标记过期。"
                )
            )
            lifecycle.last_checked_at = event_at
            lifecycle.updated_at = event_at
            session.commit()
            if command == EXPIRY_EXPIRE_KEEP_COMMAND:
                return (
                    f"策略 #{lifecycle_id} 已保留未触发入场挂单，"
                    "持仓策略保持已入场。"
                )
            return (
                f"策略 #{lifecycle_id} 已记录撤销未触发入场挂单请求，"
                "持仓策略保持已入场；请人工确认 Deepcoin 当前委托。"
            )

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
        lifecycle.management_action = "expiry_expired_no_live_order"
        lifecycle.management_note = "人工确认过期并撤单；未找到本地 live 挂单，策略已停止跟踪。"
        lifecycle.updated_at = event_at
        session.commit()
        return f"策略 #{lifecycle_id} 未找到本地 live 挂单，已标记过期并停止跟踪。"


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


def _bot_http_timeout(config_timeout_seconds: float) -> httpx.Timeout:
    read_timeout = max(float(config_timeout_seconds), 35.0)
    return httpx.Timeout(
        timeout=float(config_timeout_seconds),
        connect=float(config_timeout_seconds),
        read=read_timeout,
        write=float(config_timeout_seconds),
        pool=float(config_timeout_seconds),
    )


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


async def _finish_system_operator_callback_response(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    callback_query_id: str,
    chat_id: str,
    message_id: int,
    callback_data: str,
    response_text: str,
    operator_name: str,
    original_message_text: str = "",
) -> None:
    try:
        await _answer_callback_query(
            client,
            base_url,
            callback_query_id=callback_query_id,
            text=response_text,
        )
    except httpx.HTTPStatusError:
        logger.warning(
            "System operator bot callback answer failed; editing message anyway"
        )
    await _edit_message_text(
        client,
        base_url,
        chat_id=chat_id,
        message_id=message_id,
        text=_format_callback_resolution_text(
            callback_data=callback_data,
            response_text=response_text,
            operator_name=operator_name,
            original_message_text=original_message_text,
        ),
    )


async def _finish_expiry_refresh_callback_response(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    callback_query_id: str,
    chat_id: str,
    message_id: int,
    lifecycle_id: int,
    result: ExpiryReviewRefreshResult,
    original_message_text: str,
) -> None:
    try:
        await _answer_callback_query(
            client,
            base_url,
            callback_query_id=callback_query_id,
            text=result.answer_text,
        )
    except httpx.HTTPStatusError:
        logger.warning(
            "System operator bot refresh callback answer failed; editing message anyway"
        )
    reply_markup = (
        build_pending_entry_expiry_review_reply_markup(
            {"lifecycle_id": lifecycle_id}
        )
        if result.keep_actions
        else {"inline_keyboard": []}
    )
    await _edit_message_text(
        client,
        base_url,
        chat_id=chat_id,
        message_id=message_id,
        text=_replace_expiry_refresh_status(
            original_message_text,
            result.status_text,
        ),
        reply_markup=reply_markup,
    )


async def _edit_message_text(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    if not message_id:
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    response = await client.post(
        f"{base_url}/editMessageText",
        json=payload,
    )
    response.raise_for_status()


def _replace_expiry_refresh_status(original_text: str, status_text: str) -> str:
    heading = "【最新策略状态】"
    base = str(original_text or "").split(heading, 1)[0].rstrip()
    suffix = f"{heading}\n{status_text.strip()}"
    max_base_chars = MAX_TELEGRAM_MESSAGE_CHARS - len(suffix) - 2
    if len(base) > max_base_chars:
        base = base[: max(0, max_base_chars - 4)].rstrip() + "\n..."
    return f"{base}\n\n{suffix}".strip()


def _format_callback_resolution_text(
    *,
    callback_data: str,
    response_text: str,
    operator_name: str,
    original_message_text: str = "",
) -> str:
    action, _, identifier = callback_data.partition(":")
    action_label = {
        EXPIRY_CONTINUE_COMMAND: "\u7ee7\u7eed\u7b49\u5f85",
        EXPIRY_EXPIRE_CANCEL_COMMAND: "\u8fc7\u671f\u5e76\u64a4\u5355",
        EXPIRY_EXPIRE_KEEP_COMMAND: "\u8fc7\u671f\u4f46\u4fdd\u7559\u6302\u5355",
    }.get(action, "\u5df2\u5904\u7406")
    lines = [
        f"\u2705 \u5df2\u5904\u7406\uff1a{action_label}",
        f"\u64cd\u4f5c\u4eba: {operator_name or '-'}",
    ]
    if identifier and not _text_has_line_prefix(original_message_text, "\u5185\u90e8ID:"):
        lines.append(f"\u5185\u90e8ID: {identifier}")
    lines.append("")
    if original_message_text.strip():
        lines.append(_trim_callback_context(original_message_text.strip(), response_text=response_text))
        lines.append("")
    lines.append(response_text)
    return "\n".join(lines)


def _text_has_line_prefix(text: str, prefix: str) -> bool:
    return any(line.strip().startswith(prefix) for line in text.splitlines())


def _trim_callback_context(text: str, *, response_text: str) -> str:
    max_context_chars = MAX_TELEGRAM_MESSAGE_CHARS - len(response_text) - 120
    if len(text) <= max_context_chars:
        return text
    return text[: max(0, max_context_chars - 20)].rstrip() + "\n..."


def _callback_operator_name(callback: dict[str, Any]) -> str:
    user = callback.get("from") or {}
    username = str(user.get("username") or "").strip()
    if username:
        return username
    return " ".join(
        part
        for part in [
            str(user.get("first_name") or "").strip(),
            str(user.get("last_name") or "").strip(),
        ]
        if part
    )


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    command = text.split()[0].split("@")[0].lstrip("/").lower()
    return command
