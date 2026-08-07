"""Closed prompt and safe request builder for contextual thread resolution."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any


CONTEXT_RESOLUTION_PROMPT_VERSION = "context-resolution-v1"
CONTEXT_RESOLUTION_SYSTEM_PROMPT = """
你是交易消息“策略线程归属”解析器。MiMo 已完成当前消息的图文读取；你只根据提供的结构化证据、消息时间线、Telegram 引用关系、候选策略和脱敏交易所状态做二次判断。

禁止事项：
- 不得声称读取图片像素；请求中没有图片字节。
- 不得发明 thread_id 或 message_id。
- 不得把相邻消息自动当作同一策略。
- 不能唯一确定目标时必须输出 unresolved。
- revise、止损调整、风险调整以及任何可能增加风险的动作只能选择一个目标。
- 只有明确的降风险 cancel/exit，或当前消息明确点名每个独立目标且全部仅执行 partial_take_profit 时才可多目标；risk_reducing_fanout_allowed 必须为 true。
- 出现加仓、补仓、反手或其他增加风险的混合语义时，不得对 partial_take_profit 做多目标展开。

只输出一个 JSON 对象：
{
  "decision": "new_thread | revise_thread | manage_thread | cancel_thread | exit_thread | hold | unresolved",
  "target_thread_ids": [],
  "management_action": null,
  "confidence": 0.0,
  "supporting_message_ids": [],
  "opposing_message_ids": [],
  "conflict_types": [],
  "risk_reducing_fanout_allowed": false,
  "reanalysis_triggers": [],
  "reason": "简短、可审计的判断依据"
}

management_action 只能是：
null | cancel_pending_entry | exit_full | exit_partial | partial_take_profit |
move_stop_to_protect | hold_update | risk_update | replace_entry

conflict_types 只能是：
text_image_conflict | reply_target_conflict | multiple_candidates |
target_ambiguous | entry_or_revision | exchange_state_conflict

reanalysis_triggers 只能是：
message_edited | reply_target_available | exchange_state_changed |
strategy_state_changed | evidence_version_changed
""".strip()


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "token",
    "secret",
    "local_path",
    "image_url",
    "image_bytes",
    "payload_json",
    "request_json",
    "response_json",
    "attribution_evidence_json",
    "readback_evidence_json",
    "evidence_json",
}


def _safe_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, bytes):
        return "[binary omitted]"
    if isinstance(value, str) and value.startswith("data:image/"):
        return "[image omitted]"
    return value


def build_context_resolution_request(
    *,
    current_message: dict[str, Any],
    evidence: Any,
    context_window: Any,
    candidates: Any,
    exchange_state: Any,
    first_pass_payload: Any,
) -> dict[str, Any]:
    """Build the only data DeepSeek may receive for second resolution."""

    return _safe_value(
        {
            "current_message": current_message,
            "saved_evidence": evidence,
            "message_context": context_window,
            "candidate_strategy_threads": candidates,
            "redacted_exchange_state": exchange_state,
            "mimo_first_pass": first_pass_payload,
        }
    )


def render_context_resolution_user_prompt(request_payload: dict[str, Any]) -> str:
    return (
        "Resolve the current message against the supplied strategy threads:\n"
        + json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
