"""Dedicated Telegram bot for operator decisions required by the system."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from telegram_kol_research.llm_chat import _load_env_file_values


@dataclass(slots=True)
class SystemOperatorBotConfig:
    bot_token: str
    chat_id: str
    timeout_seconds: float = 10.0


def load_system_operator_bot_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> SystemOperatorBotConfig:
    env = dict(
        _load_env_file_values(
            env_file_paths
            or [
                ".env",
                "config/telegram.env",
                "config/system_operator_bot.env",
            ]
        )
    )
    env.update(environ or os.environ)
    return SystemOperatorBotConfig(
        bot_token=env.get("TELEGRAM_KOL_SYSTEM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID", ""),
        timeout_seconds=float(env.get("TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS", "10")),
    )


def system_operator_bot_enabled(config: SystemOperatorBotConfig | None) -> bool:
    return bool(config and config.bot_token and config.chat_id)


def format_pending_entry_expiry_review_message(payload: dict[str, Any]) -> str:
    lifecycle_id = payload.get("lifecycle_id")
    symbol = payload.get("symbol") or "-"
    side = payload.get("side") or "-"
    entry = _format_range(payload.get("entry_range_low"), payload.get("entry_range_high"))
    max_age_hours = payload.get("max_age_hours") or "-"
    lines = [
        "【待入场策略超时复核】",
        f"策略: #{lifecycle_id}",
        f"交易对: {symbol} {side}",
        f"入场区间: {entry}",
        f"止损: {_format_value(payload.get('stop_loss'))}",
        f"止盈: {_format_value(payload.get('take_profit'))}",
        f"原因: 待入场已超过 {max_age_hours} 小时，请确认如何处理。",
        "",
        "可用操作:",
        f"/expiry_continue {lifecycle_id} - 继续等待",
        f"/expiry_expire_cancel {lifecycle_id} - 标记过期并撤销交易所挂单",
        f"/expiry_expire_keep {lifecycle_id} - 标记过期但保留交易所挂单",
    ]
    return "\n".join(lines)


async def send_system_operator_bot_message(
    *,
    config: SystemOperatorBotConfig,
    text: str,
) -> None:
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json={
                "chat_id": config.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()


async def send_pending_entry_expiry_review(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_pending_entry_expiry_review_message(payload),
    )


def _format_range(low: Any, high: Any) -> str:
    if low is None and high is None:
        return "-"
    if high is None or low == high:
        return _format_value(low)
    if low is None:
        return _format_value(high)
    return f"{_format_value(low)}-{_format_value(high)}"


def _format_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
