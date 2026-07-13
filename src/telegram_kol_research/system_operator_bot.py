"""Dedicated Telegram bot for operator decisions required by the system."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from telegram_kol_research.llm_chat import _load_env_file_values
from telegram_kol_research.time_utils import utc_naive_to_local


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
    message_id = payload.get("message_id") or "-"
    chat_title = payload.get("chat_title") or payload.get("group_label") or "-"
    chat_id = payload.get("chat_id") or "-"
    symbol = payload.get("symbol") or "-"
    side = payload.get("side") or "-"
    entry = _format_range(payload.get("entry_range_low"), payload.get("entry_range_high"))
    max_age_hours = payload.get("max_age_hours") or "-"
    review_reason = payload.get("review_reason") or f"\u5f85\u5165\u573a\u5df2\u8d85\u8fc7 {max_age_hours} \u5c0f\u65f6"
    lines = [
        "\u3010\u5f85\u5165\u573a\u7b56\u7565\u8d85\u65f6\u590d\u6838\u3011",
        f"\u7fa4\u7ec4: {chat_title}",
        f"\u7fa4ID: {chat_id}",
        f"\u7b56\u7565\u4ee3\u7801: #{message_id}",
        f"\u5185\u90e8ID: {lifecycle_id}",
        f"\u4ea4\u6613\u5bf9: {symbol} {side}",
        f"\u539f\u7b56\u7565\u65f6\u95f4: {_format_local_time(payload.get('signal_at'))}",
        f"\u8d85\u65f6\u65f6\u95f4: {_format_local_time(payload.get('expiry_at'))}",
    ]
    if payload.get("previous_review_at") is not None:
        lines.append(
            f"\u4e0a\u6b21\u4eba\u5de5\u7ee7\u7eed\u7b49\u5f85: {_format_local_time(payload.get('previous_review_at'))}"
        )
    lines.extend([
        f"\u5165\u573a\u533a\u95f4: {entry}",
        f"\u6b62\u635f: {_format_value(payload.get('stop_loss'))}",
        f"\u6b62\u76c8: {_format_value(payload.get('take_profit'))}",
        f"\u539f\u56e0: {review_reason}\uff0c\u8bf7\u786e\u8ba4\u5982\u4f55\u5904\u7406\u3002",
    ])
    return "\n".join(lines)


def format_ai_recognition_conflict_review_message(payload: dict[str, Any]) -> str:
    deepseek = (
        payload.get("deepseek")
        if isinstance(payload.get("deepseek"), dict)
        else {}
    )
    mimo = payload.get("mimo") if isinstance(payload.get("mimo"), dict) else {}
    text = str(payload.get("text") or "").strip()
    if len(text) > 700:
        text = text[:697] + "..."
    automation = (
        payload.get("automation")
        if isinstance(payload.get("automation"), dict)
        else {}
    )
    agreement_status = str(payload.get("agreement_status") or "disagreed")
    if agreement_status == "authoritative_failed":
        handling = "处理: MiMo 权威识别失败，未执行自动交易；DeepSeek 结果仅供参考。"
    else:
        handling = "处理: 已按 MiMo 结果继续，未等待人工复核。"
    lines = [
        "【AI识别分歧告警】",
        f"群组: {payload.get('chat_title') or payload.get('group_label') or '-'}",
        f"群ID: {payload.get('chat_id') or '-'}",
        f"消息: #{payload.get('message_id') or '-'}",
        f"时间: {_format_local_time(payload.get('posted_at'))}",
        f"DeepSeek: {_format_value(deepseek.get('status'))} / {_format_value(deepseek.get('kind'))}",
        f"DeepSeek原因: {_format_value(deepseek.get('reason'))}",
        f"MiMo: {_format_value(mimo.get('status'))} / {_format_value(mimo.get('kind'))}",
        f"MiMo原因: {_format_value(mimo.get('reason'))}",
        "权威结果: MiMo",
        f"自动化结果: {_format_value(automation.get('status'))} / {_format_value(automation.get('reason'))}",
        handling,
        "原文:",
        text or "-",
    ]
    return "\n".join(lines)


def format_semantic_disagreement_notification(payload: dict[str, Any]) -> str:
    """Format a final critical semantic review as a read-only audit notice."""

    deepseek = payload.get("deepseek") if isinstance(payload.get("deepseek"), dict) else {}
    mimo = payload.get("mimo") if isinstance(payload.get("mimo"), dict) else {}
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    evidence = deepseek.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    grounded_evidence = "；".join(
        str(item).strip() for item in evidence if str(item).strip()
    )
    conflict_types = payload.get("conflict_types")
    if not isinstance(conflict_types, list):
        conflict_types = deepseek.get("conflict_types")
    if not isinstance(conflict_types, list):
        conflict_types = []
    conflicts = ", ".join(
        str(item).strip() for item in conflict_types if str(item).strip()
    )
    source_label = (
        payload.get("chat_title")
        or payload.get("group_label")
        or payload.get("sender_name")
        or "-"
    )
    source = (
        f"{source_label} / {payload.get('chat_id') or '-'} / "
        f"#{payload.get('message_id') or '-'}"
    )
    lines = [
        "【AI语义严重分歧】",
        f"原始来源: {source}",
        f"时间: {_format_local_time(payload.get('posted_at'))}",
        (
            "权威结果: MiMo / "
            f"{_format_value(mimo.get('status'))} / "
            f"{_truncate_text(mimo.get('reason'), limit=400)}"
        ),
        (
            "自动化结果: "
            f"{_format_value(automation.get('status'))} / "
            f"{_truncate_text(automation.get('reason'), limit=400)}"
        ),
        (
            "复核结果: DeepSeek / "
            f"{_format_value(deepseek.get('status'))} / "
            f"{_truncate_text(deepseek.get('reason'), limit=400)}"
        ),
        f"冲突类型: {_truncate_text(conflicts, limit=400)}",
        f"依据: {_truncate_text(grounded_evidence, limit=700)}",
        "处理状态: 已按MiMo结果继续，未等待人工复核；消息已处理，不需要审批。",
        "原文:",
        _truncate_text(payload.get("text"), limit=900),
    ]
    return "\n".join(lines)


def build_pending_entry_expiry_review_reply_markup(payload: dict[str, Any]) -> dict[str, Any]:
    lifecycle_id = payload.get("lifecycle_id")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "\u7ee7\u7eed\u7b49\u5f85",
                    "callback_data": f"expiry_continue:{lifecycle_id}",
                }
            ],
            [
                {
                    "text": "\u8fc7\u671f\u5e76\u64a4\u5355",
                    "callback_data": f"expiry_expire_cancel:{lifecycle_id}",
                },
                {
                    "text": "\u8fc7\u671f\u4f46\u4fdd\u7559\u6302\u5355",
                    "callback_data": f"expiry_expire_keep:{lifecycle_id}",
                },
            ],
        ]
    }


async def send_system_operator_bot_message(
    *,
    config: SystemOperatorBotConfig,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": config.chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json=payload,
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
        reply_markup=build_pending_entry_expiry_review_reply_markup(payload),
    )


async def send_ai_recognition_conflict_review(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_ai_recognition_conflict_review_message(payload),
    )


async def send_semantic_disagreement_notification(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_semantic_disagreement_notification(payload),
        reply_markup=None,
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


def _truncate_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_local_time(value: Any) -> str:
    local = utc_naive_to_local(value)
    if local is None:
        return "-"
    return local.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
