"""Dedicated Telegram bot for operator decisions required by the system."""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from telegram_kol_research.llm_chat import _load_env_file_values
from telegram_kol_research.message_instruction_items import (
    FINISH_STATUSES,
    SUMMARY_NOTIFICATION_LEASE,
    claim_message_instruction_summary,
    finish_message_instruction_summary_delivery,
)
from telegram_kol_research.time_utils import utc_naive_to_local


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SystemOperatorBotConfig:
    bot_token: str
    chat_id: str
    timeout_seconds: float = 10.0


NotificationBotConfig = SystemOperatorBotConfig


def load_system_operator_bot_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> SystemOperatorBotConfig:
    paths = (
        [
            ".env",
            "config/telegram.env",
            "config/system_operator_bot.env",
        ]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(
        _load_env_file_values(paths) if paths else {}
    )
    env.update(environ or os.environ)
    return SystemOperatorBotConfig(
        bot_token=env.get("TELEGRAM_KOL_SYSTEM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID", ""),
        timeout_seconds=float(env.get("TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS", "10")),
    )


def load_notification_bot_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> NotificationBotConfig:
    paths = (
        [
            ".env",
            "config/telegram.env",
            "config/system_operator_bot.env",
        ]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(environ or os.environ)
    return NotificationBotConfig(
        bot_token=env.get("TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID", ""),
        timeout_seconds=float(
            env.get("TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS", "10")
        ),
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
    review_reason = payload.get("review_reason") or (
        f"\u5f85\u5165\u573a\u5df2\u8d85\u8fc7 {max_age_hours} \u5c0f\u65f6"
    )
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
    pending_order_ids = [
        str(item)
        for item in (payload.get("pending_order_ids") or [])
        if str(item or "").strip()
    ]
    if pending_order_ids:
        lines.append(
            f"\u672a\u89e6\u53d1\u5165\u573a\u6302\u5355: {', '.join(pending_order_ids)}"
        )
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


def format_position_attribution_incident_message(payload: dict[str, Any]) -> str:
    state = str(payload.get("state") or "unassigned")
    state_label = {
        "attribution_conflict": "归属冲突",
        "evidence_unavailable": "归属证据暂不可用",
    }.get(state, state)
    candidate_leg_ids = payload.get("candidate_leg_ids")
    if not isinstance(candidate_leg_ids, list):
        candidate_leg_ids = []
    source_errors = payload.get("evidence_source_errors")
    if not isinstance(source_errors, dict):
        source_errors = {}
    error_text = "；".join(
        f"{key}: {value}" for key, value in sorted(source_errors.items())
    )
    return "\n".join(
        [
            "【仓位归属异常】",
            f"交易所: {payload.get('venue') or 'deepcoin'}",
            f"仓位ID: {payload.get('pos_id') or '-'}",
            f"状态: {state_label}",
            f"候选 entry legs: {', '.join(str(item) for item in candidate_leg_ids) or '-'}",
            f"证据源错误: {error_text or '-'}",
            "处理状态: 自动管理已冻结，不会自动平仓或修改止盈止损。",
        ]
    )


def format_position_protection_incident_message(payload: dict[str, Any]) -> str:
    """Format a high-priority, non-actionable stop-protection incident."""

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    exchange = evidence.get("exchange") if isinstance(evidence.get("exchange"), dict) else {}
    return "\n".join([
        "【止损保护严重异常】",
        f"交易所: {payload.get('venue') or 'deepcoin'}",
        f"仓位ID: {payload.get('pos_id') or '-'}",
        f"类型: {payload.get('incident_type') or '-'}",
        f"交易所错误: {exchange.get('errorCode') or '-'} {exchange.get('errorMsg') or ''}".strip(),
        "处理状态: 自动管理已冻结；系统不会补单、撤单或平仓，请人工决定。",
    ])
MANAGEMENT_ALERT_STATES = frozenset(
    {"blocked", "partial_failed", "submit_unknown", "recovery_required"}
)
_MANAGEMENT_TELEGRAM_MAX_CHARS = 3900
_DEFERRED_CANCEL_EVENT_ACTIONS = frozenset({
    "strategy_management_cancel_deferred_trigger_entry",
    "strategy_management_cancel_deferred_regular_entry",
    "strategy_management_deferred_entry_cancel_diagnostic",
})
_SENSITIVE_MARKERS = (
    "dc-access", "api-key", "api_key", "authorization", "passphrase",
    "secret", "signing", "raw_header", "raw_payload", "raw_response",
)


def format_strategy_management_notification(payload: dict[str, Any]) -> str:
    """Format only bounded, explicitly selected management business fields."""

    state = str(payload.get("state") or "-")
    mode = str(payload.get("mode") or "shadow")
    lines = [
        "【策略管理批次异常】",
        f"通知ID: {payload.get('notification_id') or '-'}",
        f"batch #{payload.get('batch_id') or '-'} / 状态: {state}",
        (
            f"来源: {payload.get('source_chat_title') or '-'} / "
            f"{payload.get('source_chat_id') or '-'} / "
            f"#{payload.get('source_message_id') or '-'} / "
            f"raw={payload.get('raw_message_id') or '-'}"
        ),
        (
            f"归属: lifecycle={payload.get('lifecycle_id') or '-'} / "
            f"strategy={payload.get('strategy_instance_id') or '-'} / "
            f"binding={payload.get('execution_binding_id') or '-'}"
        ),
        f"动作: {payload.get('intent') or '-'} -> {payload.get('effective_action') or '-'}",
        f"原因: {_safe_management_text(payload.get('reason'))}",
        f"模式: {mode}" + (" / 未调用交易 API" if mode != "live" else ""),
    ]
    if state == "recovery_required":
        lines.append("安全限制: 禁止自动重试")
    deferred_entry_legs = (
        payload.get("deferred_entry_legs")
        if isinstance(payload.get("deferred_entry_legs"), list) else []
    )
    if payload.get("reason") == "deferred_entry_cancel_preflight_failed":
        lines.extend([
            "未成交进场腿撤单未完成：为防止旧策略残留订单成交，系统未提交平仓单。",
            "请核对交易所挂单与以下腿的订单ID；确认撤销后再执行恢复处理。",
            "恢复处理完成前，请勿启用替代策略。",
            f"批次ID: {payload.get('batch_id') or '-'}",
            "未成交进场腿:",
        ])
        for entry in deferred_entry_legs[:20]:
            if not isinstance(entry, dict):
                continue
            identity_state = _safe_management_text(
                entry.get("identity_state") or "snapshotted", limit=40
            )
            unavailable_identifier = (
                "不可用(快照腿缺失或漂移)"
                if identity_state in {
                    "snapshot_leg_missing",
                    "snapshot_leg_reassigned",
                    "snapshot_leg_state_drift",
                }
                else "-"
            )
            lines.append(
                "- "
                f"腿: {_safe_management_text(entry.get('execution_order_leg_id'), limit=24)} "
                f"身份: {identity_state} "
                f"订单: {_safe_management_text(entry.get('order_id') or unavailable_identifier, limit=120)} "
                f"客户订单: {_safe_management_text(entry.get('client_order_id') or unavailable_identifier, limit=120)}"
            )
            diagnostic = entry.get("cancellation_diagnostic")
            if isinstance(diagnostic, dict):
                lines.append(
                    "  撤单诊断: "
                    f"live={_safe_management_text(diagnostic.get('live_match_source'), limit=40)} "
                    f"type={_safe_management_text(diagnostic.get('match_type'), limit=24)} "
                    f"status={_safe_management_text(diagnostic.get('status'), limit=32)} "
                    f"reason={_safe_management_text(diagnostic.get('reason'), limit=80)}"
                )
        if not deferred_entry_legs:
            lines.append("- 身份漂移诊断未持久化，请立即人工核对快照和待处理进场腿。")
    legs = payload.get("legs") if isinstance(payload.get("legs"), list) else []
    lines.append("仓位/腿结果:")
    for leg in legs[:20]:
        if not isinstance(leg, dict):
            continue
        lines.append(
            "- "
            f"leg={leg.get('leg_id') or '-'} entry_leg={leg.get('execution_order_leg_id') or '-'} "
            f"pos={leg.get('pos_id') or '-'} index={leg.get('leg_index')} "
            f"status={leg.get('status') or '-'} planned={leg.get('planned_close_size') or '-'} "
            f"clOrdId={leg.get('client_order_id') or '-'} ordId={leg.get('exchange_order_id') or '-'} "
            f"error={json.dumps(leg.get('error_summary') or {}, ensure_ascii=False, sort_keys=True)}"
        )
    if not legs:
        lines.append("- -")
    return "\n".join(lines)


def split_strategy_management_notification(
    payload: dict[str, Any], *, max_chars: int = _MANAGEMENT_TELEGRAM_MAX_CHARS
) -> list[str]:
    """Return deterministic line-bounded Telegram messages below 4096 chars."""

    return _split_telegram_notification_text(
        format_strategy_management_notification(payload), max_chars=max_chars
    )


def _split_telegram_notification_text(text: str, *, max_chars: int) -> list[str]:
    """Split display text deterministically on lines within Telegram's limit."""

    limit = max(1, min(int(max_chars), 4095))
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def format_message_instruction_summary(payload: dict[str, Any]) -> str:
    """Render a bounded, payload-only outcome summary for one source message."""

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    ordered_items = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda item: (_summary_sequence(item), _summary_item_id(item)),
    )
    lines = [
        "【同消息策略指令结果】",
        (
            f"来源: {payload.get('chat_title') or payload.get('group_label') or '-'} / "
            f"{payload.get('chat_id') or '-'} / #{payload.get('message_id') or '-'}"
        ),
    ]
    if payload.get("notification_id"):
        lines.append(f"通知ID: {payload['notification_id']}")
    management_failed = False
    entry_attempted = False
    for item in ordered_items[:100]:
        kind = str(item.get("instruction_kind") or "-").strip().lower()
        status = _safe_management_text(item.get("status"), limit=48)
        label = {"management": "仓位管理", "entry": "新策略开仓"}.get(kind, kind or "-")
        strategy = _safe_management_text(item.get("strategy_instance_id"), limit=255)
        reason = _safe_management_text(_summary_item_reason(item), limit=180)
        lines.append(
            f"#{_summary_sequence(item)} {label}: {status} / "
            f"strategy={strategy} / 原因: {reason}"
        )
        management_failed = management_failed or (
            kind == "management" and status in {"failed", "unknown"}
        )
        entry_attempted = entry_attempted or (
            kind == "entry" and status not in {"pending", "executing", "-"}
        )
    if not ordered_items:
        lines.append("未持久化可展示的指令结果。")
    elif management_failed and entry_attempted:
        lines.append("仓位管理异常；后续开仓已继续尝试。")
    return "\n".join(lines)


def split_message_instruction_summary(
    payload: dict[str, Any], *, max_chars: int = _MANAGEMENT_TELEGRAM_MAX_CHARS
) -> list[str]:
    """Split a message-level instruction summary using the Telegram-safe splitter."""

    return _split_telegram_notification_text(
        format_message_instruction_summary(payload), max_chars=max_chars
    )


def _summary_sequence(item: dict[str, Any]) -> int:
    sequence = item.get("sequence")
    return sequence if type(sequence) is int and sequence >= 0 else 999_999_999


def _summary_item_id(item: dict[str, Any]) -> int:
    item_id = item.get("item_id")
    return item_id if type(item_id) is int and item_id >= 0 else 999_999_999


def _summary_item_reason(item: dict[str, Any]) -> Any:
    if item.get("reason") not in (None, ""):
        return item["reason"]
    result = item.get("result")
    if isinstance(result, dict):
        for key in ("reason", "reason_code", "status"):
            if result.get(key) not in (None, ""):
                return result[key]
    return "-"


def _safe_management_text(value: Any, *, limit: int = 300) -> str:
    text = str(value or "-").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "[redacted]"
    return _truncate_text(text, limit=limit)


def _decode_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_value(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def canonical_management_error_summary(value: Any) -> dict[str, str]:
    """Select machine identifiers only; never copy exception prose or transport data."""

    decoded = _decode_value(value)
    if not isinstance(decoded, dict):
        return {}
    summary: dict[str, str] = {}
    error_type = decoded.get("type")
    if isinstance(error_type, str) and re.fullmatch(
        r"[A-Z][A-Za-z0-9]{0,70}(?:Error|Exception)", error_type
    ):
        summary["type"] = error_type
    for key in ("stage", "reason_code", "reason"):
        item = decoded.get(key)
        if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,79}", item):
            summary[key] = item
    status = decoded.get("status")
    if status in {
        "planned", "reserved", "submitted", "submit_unknown", "failed",
        "confirmed", "partial", "inconsistent", "restored", "recovery_required",
    }:
        summary["status"] = status
    return summary


def _management_payload_for_batch(session, batch, *, group_labels=None) -> dict[str, Any]:
    from telegram_kol_research.models import (
        ExecutionEvent, ExecutionOrderLeg, RawMessage, StrategyManagementLeg,
    )

    raw = session.get(RawMessage, batch.raw_message_id)
    # The raw row is the source-of-truth isolation boundary. Never infer a chat
    # from a strategy string or join another group's similarly numbered message.
    mode = str(batch.execution_mode or "disabled")
    labels = group_labels or {}
    legs = []
    for leg in (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
        .limit(20)
    ):
        legs.append(
            {
                "leg_id": leg.id,
                "execution_order_leg_id": leg.execution_order_leg_id,
                "pos_id": _safe_management_text(leg.pos_id, limit=120),
                "leg_index": leg.leg_index,
                "status": _safe_management_text(leg.status, limit=64),
                "preflight_size": _safe_management_text(leg.preflight_size, limit=64),
                "planned_close_size": _safe_management_text(leg.planned_close_size, limit=64),
                "client_order_id": _safe_management_text(leg.client_order_id, limit=120),
                "exchange_order_id": _safe_management_text(leg.exchange_order_id, limit=120),
                "error_summary": canonical_management_error_summary(leg.last_error),
            }
        )
    deferred_entry_legs = []
    cancellation_diagnostics: dict[int, dict[str, Any]] = {}
    for event in (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id == batch.execution_binding_id,
            ExecutionEvent.source_message_id == batch.raw_message_id,
            ExecutionEvent.action.in_(_DEFERRED_CANCEL_EVENT_ACTIONS),
        )
        .order_by(ExecutionEvent.id.desc())
        .limit(100)
    ):
        diagnostic = _canonical_deferred_cancel_diagnostic(event.after_json)
        leg_id = diagnostic.get("execution_order_leg_id")
        if isinstance(leg_id, int) and leg_id not in cancellation_diagnostics:
            cancellation_diagnostics[leg_id] = {
                key: value
                for key, value in diagnostic.items()
                if key != "execution_order_leg_id"
            }
    snapshot = _decode_mapping(batch.target_snapshot_json)
    identity = snapshot.get("identity")
    deferred_entry_leg_ids = (
        identity.get("deferred_entry_leg_ids") if isinstance(identity, dict) else []
    )
    if (
        isinstance(deferred_entry_leg_ids, list)
        and all(type(leg_id) is int and leg_id > 0 for leg_id in deferred_entry_leg_ids)
    ):
        diagnostic_leg_ids = list(cancellation_diagnostics)
        display_leg_ids = []
        for leg_id in [*diagnostic_leg_ids, *deferred_entry_leg_ids]:
            if leg_id not in display_leg_ids:
                display_leg_ids.append(leg_id)
            if len(display_leg_ids) == 20:
                break
        entries_by_id = {
            int(entry.id): entry
            for entry in (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.id.in_(display_leg_ids),
                    ExecutionOrderLeg.execution_binding_id
                    == batch.execution_binding_id,
                    ExecutionOrderLeg.strategy_instance_id
                    == batch.strategy_instance_id,
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.id.asc())
            )
        }
        for leg_id in display_leg_ids:
            entry = entries_by_id.get(leg_id)
            diagnostic = cancellation_diagnostics.get(leg_id, {})
            identity_state = diagnostic.get("identity_state") or "snapshotted"
            order_id = (
                entry.order_id if entry is not None else diagnostic.get("order_id")
            )
            client_order_id = (
                entry.client_order_id
                if entry is not None
                else diagnostic.get("client_order_id")
            )
            deferred_entry_legs.append(
                {
                    "execution_order_leg_id": leg_id,
                    "identity_state": _safe_management_text(
                        identity_state, limit=40
                    ),
                    "order_id": (
                        _safe_management_text(order_id, limit=120)
                        if order_id not in (None, "")
                        else None
                    ),
                    "client_order_id": (
                        _safe_management_text(client_order_id, limit=120)
                        if client_order_id not in (None, "")
                        else None
                    ),
                    "cancellation_diagnostic": diagnostic or {
                        "live_match_source": "unknown",
                        "match_type": (
                            "trigger"
                            if entry is not None
                            and "trigger" in str(entry.order_kind or "").lower()
                            else "regular"
                        ),
                        "status": (
                            "resolved"
                            if entry is not None
                            and str(entry.status or "").lower() == "cancelled"
                            else "unresolved"
                        ),
                        "reason": _safe_management_text(
                            (
                                entry.terminal_reason
                                if entry is not None
                                else None
                            )
                            or batch.reason_code,
                            limit=80,
                        ),
                    },
                }
            )
    return {
        "batch_id": batch.id,
        "state": batch.status,
        "mode": mode if mode in {"disabled", "shadow", "live"} else "disabled",
        "source_chat_id": raw.chat_id if raw is not None else None,
        "source_chat_title": (
            _safe_management_text(labels.get(raw.chat_id), limit=120)
            if raw is not None and labels.get(raw.chat_id) is not None else None
        ),
        "source_message_id": raw.message_id if raw is not None else None,
        "raw_message_id": raw.id if raw is not None else batch.raw_message_id,
        "lifecycle_id": batch.target_lifecycle_id,
        "strategy_instance_id": _safe_management_text(batch.strategy_instance_id, limit=255),
        "execution_binding_id": batch.execution_binding_id,
        "intent": _safe_management_text(batch.intent, limit=64),
        "effective_action": _safe_management_text(batch.effective_action, limit=64),
        "reason": _safe_management_text(batch.reason_code, limit=240),
        "deferred_entry_legs": deferred_entry_legs,
        "legs": legs,
    }


def _canonical_deferred_cancel_diagnostic(value: Any) -> dict[str, Any]:
    decoded = _decode_value(value)
    if not isinstance(decoded, dict):
        return {}
    leg_id = decoded.get("execution_order_leg_id")
    if type(leg_id) is not int or leg_id <= 0:
        return {}
    diagnostic = {
        "execution_order_leg_id": leg_id,
        "live_match_source": _safe_management_text(
            decoded.get("live_match_source"), limit=40
        ),
        "match_type": _safe_management_text(decoded.get("match_type"), limit=24),
        "status": _safe_management_text(decoded.get("status"), limit=32),
        "reason": _safe_management_text(decoded.get("reason"), limit=80),
    }
    identity_state = decoded.get("identity_state")
    if identity_state in {
        "snapshot_leg_missing",
        "snapshot_leg_reassigned",
        "snapshot_leg_state_drift",
        "unsnapshotted_pending",
    }:
        diagnostic["identity_state"] = identity_state
    for key in ("order_id", "client_order_id"):
        if decoded.get(key) not in (None, ""):
            diagnostic[key] = _safe_management_text(decoded.get(key), limit=120)
    return diagnostic


def _management_payload_fingerprint(payload: dict[str, Any]) -> str:
    fingerprint_payload = dict(payload)
    # The display title is mutable configuration, not attribution identity.
    # Excluding it keeps transition-time outbox rows and delivery-time label
    # enrichment from producing duplicate alerts.
    fingerprint_payload.pop("source_chat_title", None)
    canonical = json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persist_strategy_management_notification_in_session(
    session, batch, *, group_labels=None
) -> bool:
    """Write the alert outbox in the same transaction as an alert transition."""

    from telegram_kol_research.models import StrategyManagementNotification

    if batch.status not in MANAGEMENT_ALERT_STATES:
        return False
    payload = _management_payload_for_batch(session, batch, group_labels=group_labels)
    fingerprint = _management_payload_fingerprint(payload)
    row = StrategyManagementNotification(
        management_batch_id=batch.id,
        state=batch.status,
        payload_fingerprint=fingerprint,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        status="pending",
    )
    # A nested savepoint lets an unchanged duplicate lose without rolling back
    # the enclosing management transition.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def enqueue_strategy_management_notifications(session_factory, *, group_labels=None) -> int:
    """Persist immutable alert identities; duplicate ticks are harmless."""

    from telegram_kol_research.models import StrategyManagementBatch
    from telegram_kol_research.models import StrategyManagementNotification

    with session_factory() as session:
        batch_ids = [
            row[0] for row in session.query(StrategyManagementBatch.id)
            .filter(StrategyManagementBatch.status.in_(MANAGEMENT_ALERT_STATES))
            .order_by(StrategyManagementBatch.id.asc()).limit(100).all()
        ]
    created = 0
    for batch_id in batch_ids:
        with session_factory() as session:
            batch = session.get(StrategyManagementBatch, batch_id)
            if batch is None or batch.status not in MANAGEMENT_ALERT_STATES:
                continue
            if persist_strategy_management_notification_in_session(
                session, batch, group_labels=group_labels
            ):
                created += 1
            session.commit()
    return created


def claim_next_strategy_management_notification(
    session_factory, *, claimed_at: datetime | None = None, lease_seconds: float = 120.0
):
    """CAS one pending/failed delivery; concurrent notifiers have one winner."""

    from telegram_kol_research.models import StrategyManagementNotification

    now = claimed_at or datetime.now(UTC)
    lease_expiry = now + timedelta(seconds=max(5.0, float(lease_seconds)))
    claimable = or_(
        StrategyManagementNotification.status.in_(("pending", "failed")),
        (
            (StrategyManagementNotification.status == "delivering")
            & or_(
                StrategyManagementNotification.lease_expires_at.is_(None),
                StrategyManagementNotification.lease_expires_at <= now,
            )
        ),
    )
    with session_factory() as session:
        row_id = (
            session.query(StrategyManagementNotification.id)
            .filter(claimable)
            .order_by(StrategyManagementNotification.id.asc()).limit(1).scalar()
        )
    if row_id is None:
        return None
    token = uuid.uuid4().hex
    with session_factory() as session:
        result = session.execute(
            update(StrategyManagementNotification)
            .where(
                StrategyManagementNotification.id == row_id,
                claimable,
            )
            .values(
                status="delivering", claim_token=token, delivery_error=None,
                claimed_at=now, lease_expires_at=lease_expiry, updated_at=now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        row = session.get(StrategyManagementNotification, row_id)
        return {
            "id": row.id, "claim_token": token,
            "payload": _decode_mapping(row.payload_json),
        }


async def deliver_strategy_management_notifications(
    session_factory, *, config: SystemOperatorBotConfig, group_labels=None, limit: int = 20,
    claimed_at: datetime | None = None, lease_seconds: float = 120.0,
) -> int:
    """Deliver with a durable lease and at-least-once crash semantics.

    Telegram ``sendMessage`` has no idempotency key. A process death after
    Telegram accepts a message but before ``delivered`` commits can therefore
    cause one repeat after lease expiry. The stable notification ID embedded in
    the text is the dedup marker; committed successes are never reclaimed.
    """
    from telegram_kol_research.models import StrategyManagementNotification

    enqueue_strategy_management_notifications(session_factory, group_labels=group_labels)
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_strategy_management_notification(
            session_factory, claimed_at=claimed_at, lease_seconds=lease_seconds
        )
        if claim is None:
            break
        payload = dict(claim["payload"])
        payload["notification_id"] = claim["id"]
        if group_labels and payload.get("source_chat_id") is not None:
            payload["source_chat_title"] = _safe_management_text(
                group_labels.get(payload["source_chat_id"]), limit=120
            )
        try:
            for message in split_strategy_management_notification(payload):
                await send_system_operator_bot_message(config=config, text=message)
        except Exception as exc:
            with session_factory() as session:
                session.execute(
                    update(StrategyManagementNotification)
                    .where(
                        StrategyManagementNotification.id == claim["id"],
                        StrategyManagementNotification.status == "delivering",
                        StrategyManagementNotification.claim_token == claim["claim_token"],
                    )
                    .values(
                        status="failed", claim_token=None,
                        lease_expires_at=None,
                        delivery_error=type(exc).__name__,
                        updated_at=datetime.now(UTC),
                    )
                )
                session.commit()
            # Retry on a later worker tick. Reclaiming the just-failed row in the
            # same tick would create an unbounded hot loop during an outage.
            break
        with session_factory() as session:
            result = session.execute(
                update(StrategyManagementNotification)
                .where(
                    StrategyManagementNotification.id == claim["id"],
                    StrategyManagementNotification.status == "delivering",
                    StrategyManagementNotification.claim_token == claim["claim_token"],
                )
                .values(
                    status="delivered", claim_token=None, delivery_error=None,
                    lease_expires_at=None,
                    notified_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


async def run_strategy_management_notification_loop(
    *, session_factory, config: SystemOperatorBotConfig, group_labels=None,
    interval_seconds: float = 5.0,
) -> None:
    while True:
        try:
            await deliver_strategy_management_notifications(
                session_factory, config=config, group_labels=group_labels
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(0.1, float(interval_seconds)))
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


async def send_message_instruction_summary_notification(
    *, config: SystemOperatorBotConfig, payload: dict[str, Any]
) -> None:
    """Deliver every bounded chunk of a persisted message outcome payload."""

    for text in split_message_instruction_summary(payload):
        await send_system_operator_bot_message(config=config, text=text)


async def deliver_message_instruction_summary_notification(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    raw_message_id: int,
    chat_title: str | None = None,
    claimed_at: datetime | None = None,
) -> bool:
    """Deliver a completed summary through the leased durable outbox."""

    payload = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_message_id,
        claimed_at=claimed_at or datetime.now(UTC),
        chat_title=chat_title,
    )
    if payload is None:
        return False
    completed_at = claimed_at or datetime.now(UTC)
    try:
        await send_message_instruction_summary_notification(
            config=config,
            payload=payload,
        )
    except Exception as exc:
        finish_message_instruction_summary_delivery(
            session_factory,
            claim_token=payload["notification_claim_token"],
            item_ids=payload["notification_item_ids"],
            delivered=False,
            completed_at=completed_at,
            error=str(exc),
        )
        logger.exception(
            "message instruction summary delivery failed for raw_message_id=%s",
            raw_message_id,
        )
        return False
    finish_message_instruction_summary_delivery(
        session_factory,
        claim_token=payload["notification_claim_token"],
        item_ids=payload["notification_item_ids"],
        delivered=True,
        completed_at=completed_at,
    )
    return True


async def deliver_pending_message_instruction_summaries(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    claimed_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Retry a bounded batch of pending, failed, or expired summary deliveries."""

    from telegram_kol_research.models import MessageInstructionItem

    now = claimed_at or datetime.now(UTC)
    lease_cutoff = now - SUMMARY_NOTIFICATION_LEASE
    with session_factory() as session:
        candidate_item = aliased(MessageInstructionItem)
        blocking_item = aliased(MessageInstructionItem)
        raw_message_ids = [
            int(raw_message_id)
            for (raw_message_id,) in (
                session.query(candidate_item.raw_message_id)
                .filter(candidate_item.retired_at.is_(None))
                .filter(candidate_item.status.in_(sorted(FINISH_STATUSES)))
                .filter(
                    or_(
                        candidate_item.summary_notification_status.in_(
                            ["pending", "failed"]
                        ),
                        candidate_item.summary_notification_status.is_(None),
                        and_(
                            candidate_item.summary_notification_status == "delivering",
                            or_(
                                candidate_item.summary_notification_claimed_at.is_(None),
                                candidate_item.summary_notification_claimed_at
                                <= lease_cutoff,
                            ),
                        ),
                    )
                )
                .filter(
                    ~exists(
                        select(blocking_item.id).where(
                            blocking_item.raw_message_id
                            == candidate_item.raw_message_id,
                            blocking_item.retired_at.is_(None),
                            ~blocking_item.status.in_(sorted(FINISH_STATUSES)),
                        )
                    )
                )
                .distinct()
                .order_by(candidate_item.raw_message_id.asc())
                .limit(max(1, int(limit)))
                .all()
            )
        ]

    delivered = 0
    for raw_message_id in raw_message_ids:
        if await deliver_message_instruction_summary_notification(
            session_factory,
            config=config,
            raw_message_id=raw_message_id,
            claimed_at=now,
        ):
            delivered += 1
            if delivered >= max(1, int(limit)):
                break
    return delivered


async def deliver_pending_position_attribution_incidents(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    delivered_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Claim and deliver new attribution incidents without mutating ownership."""

    from telegram_kol_research.models import PositionAttributionAudit

    now = delivered_at or datetime.now(UTC)
    with session_factory() as session:
        candidate_ids = [
            int(row_id)
            for (row_id,) in (
                session.query(PositionAttributionAudit.id)
            .filter(PositionAttributionAudit.notification_status == "pending")
            .order_by(PositionAttributionAudit.id.asc())
            .limit(max(1, int(limit)))
            .all()
            )
        ]

    incident_ids: list[int] = []
    for incident_id in candidate_ids:
        with session_factory() as session:
            claimed = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.id == incident_id)
                .filter(PositionAttributionAudit.notification_status == "pending")
                .update(
                    {
                        PositionAttributionAudit.notification_status: "delivering",
                        PositionAttributionAudit.notification_error: None,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if claimed == 1:
                incident_ids.append(incident_id)

    delivered = 0
    for incident_id in incident_ids:
        with session_factory() as session:
            row = session.get(PositionAttributionAudit, incident_id)
            if row is None or row.notification_status != "delivering":
                continue
            try:
                evidence = json.loads(row.evidence_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            payload = {
                "venue": row.venue,
                "pos_id": row.pos_id,
                "state": row.new_state,
                "candidate_leg_ids": evidence.get("candidate_leg_ids", []),
                "evidence_source_errors": evidence.get("errors", {}),
            }
        try:
            await send_system_operator_bot_message(
                config=config,
                text=format_position_attribution_incident_message(payload),
            )
        except Exception as exc:
            with session_factory() as session:
                row = session.get(PositionAttributionAudit, incident_id)
                if row is not None:
                    row.notification_status = "failed"
                    row.notification_error = str(exc)
                    session.commit()
            continue
        with session_factory() as session:
            row = session.get(PositionAttributionAudit, incident_id)
            if row is not None:
                row.notification_status = "delivered"
                row.notification_error = None
                row.notified_at = now
                session.commit()
                delivered += 1
    return delivered


async def deliver_pending_position_protection_incidents(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    delivered_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Deliver each fingerprinted protection incident once, without trade I/O."""

    from telegram_kol_research.models import PositionProtectionIncident

    now = delivered_at or datetime.now(UTC)
    with session_factory() as session:
        ids = [int(item[0]) for item in session.query(PositionProtectionIncident.id)
               .filter(PositionProtectionIncident.delivery_status == "pending")
               .order_by(PositionProtectionIncident.id.asc()).limit(max(1, int(limit))).all()]
    delivered = 0
    for incident_id in ids:
        with session_factory() as session:
            claimed = session.query(PositionProtectionIncident).filter(
                PositionProtectionIncident.id == incident_id,
                PositionProtectionIncident.delivery_status == "pending",
            ).update({PositionProtectionIncident.delivery_status: "delivering"}, synchronize_session=False)
            session.commit()
            if claimed != 1:
                continue
            row = session.get(PositionProtectionIncident, incident_id)
            try:
                evidence = json.loads(row.evidence_json or "{}") if row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
        try:
            await send_system_operator_bot_message(
                config=config,
                text=format_position_protection_incident_message({
                    "venue": row.venue, "pos_id": row.pos_id,
                    "incident_type": row.incident_type, "evidence": evidence,
                }),
            )
        except Exception as exc:
            with session_factory() as session:
                row = session.get(PositionProtectionIncident, incident_id)
                if row is not None:
                    row.delivery_status, row.delivery_error = "failed", str(exc)
                    session.commit()
            continue
        with session_factory() as session:
            row = session.get(PositionProtectionIncident, incident_id)
            if row is not None:
                row.delivery_status, row.delivery_error, row.notified_at = "delivered", None, now
                session.commit()
                delivered += 1
    return delivered


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
