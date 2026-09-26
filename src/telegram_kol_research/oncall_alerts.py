"""Plain-Chinese on-call alerts: wording, throttling and delivery.

Phase 1 of the Codex on-call remediation program
(``docs/plans/2026-09-19-codex-oncall-phase1-spec.md``, section 5).

Three things this module is careful about.

**The reader is not an engineer.** Every line is written for somebody looking
at a phone: what the message asked for, how long ago, what the system is stuck
on, and whether the position is still open. Reason codes are translated; an
untranslated one is shown verbatim and marked as such rather than silently
dropped, because a missing translation must never turn into a missing fact.

**The KOL message is untrusted external text.** It is truncated, stripped of
newlines and quoted -- and sent with no ``parse_mode``, so nothing inside it
can be interpreted as formatting, a link, or anything else.

**The bot token never leaves memory.** It is not logged, not stored, and a
delivery failure records only the exception's *class name*: urllib puts the
full request URL -- token included -- into the text of an ``HTTPError``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from telegram_kol_research.oncall_codex import (
    CATEGORY_LABELS_ZH,
    CONFIDENCE_LABELS_ZH,
    FAILURE_AUTH,
    FAILURE_LABELS_ZH,
    SHOULD_LABELS_ZH,
    URGENCY_LABELS_ZH,
)
from telegram_kol_research.oncall_state import (
    RECOGNITION_CASE_PREFIX,
    SEALED_LANE_CASE_PREFIX,
    UNHEARD_INCIDENT_CASE_PREFIX,
    VOIDED_MESSAGE_CASE_PREFIX,
    CaseRecord,
    OncallStateStore,
    isoformat,
    parse_isoformat,
)


logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")

#: How much of an untrusted message body ever reaches the alert.
MESSAGE_EXCERPT_LIMIT = 80

ALERT_KIND_CASE_OPEN = "case_open"
ALERT_KIND_CASE_RESOLVED = "case_resolved"
ALERT_KIND_HEALTH_OPEN = "health_open"
ALERT_KIND_HEALTH_RESOLVED = "health_resolved"
ALERT_KIND_GROUP_MERGED = "group_merged"
ALERT_KIND_CAP_REACHED = "cap_reached"
ALERT_KIND_DAILY_SUMMARY = "daily_summary"
#: Phase 2.
ALERT_KIND_DIAGNOSIS = "diagnosis"
ALERT_KIND_CODEX_DOWN = "codex_down"
ALERT_KIND_CODEX_RECOVERED = "codex_recovered"

#: Alerts that count against the daily cap.
CAPPED_ALERT_KINDS = (
    ALERT_KIND_CASE_OPEN,
    ALERT_KIND_CASE_RESOLVED,
    ALERT_KIND_HEALTH_OPEN,
    ALERT_KIND_HEALTH_RESOLVED,
    ALERT_KIND_GROUP_MERGED,
    ALERT_KIND_DIAGNOSIS,
)

GROUP_MERGE_WINDOW = timedelta(minutes=10)
GROUP_MERGE_THRESHOLD = 3
HEALTH_ALERT_COOLDOWN = timedelta(minutes=15)

ACTION_LABELS = {
    "full_exit": "全部平仓 / 离场",
    "partial_take_profit": "部分止盈",
    "partial_then_break_even": "部分止盈后保本",
    "move_stop_to_break_even": "止损移到保本",
    "adjust_stop_loss": "调整止损",
    "adjust_take_profit": "调整止盈",
    "replace_entry": "改挂入场单",
    "cancel_pending_entry": "撤掉挂单",
}

SIDE_LABELS = {"long": "多", "short": "空", "buy": "多", "sell": "空"}

#: Reason code -> plain Chinese. Covers every code the phase 0 production
#: study (design 7.1) turned up, plus the translations that already existed in
#: ``web_queries._execution_reason_label``.
REASON_LABELS = {
    # --- what phase 0 actually found in production ---
    "prior_partial_batch_unresolved": "上一笔部分平仓还没结清",
    "confirmation_timeout": "等你确认目标仓位，超时了",
    "protection_missing_cancellable_order_id": "找不到可撤销的保护单编号",
    "protection_price_or_size_mismatch": "保护单的价格或数量对不上",
    "management_stop_action_conflict": "同一仓位有两个互相冲突的止损动作",
    # Management only since 2026-09-24: the sweep that writes this code now
    # filters on ``instruction_kind == "management"``. An entry that timed out
    # never had a position to find, and used to be reported with this line.
    "target_strategy_binding_visibility_retry_expired": "改仓位时一直没找到对应的持仓记录，重试超时",
    "entry_admission_deadline_expired": "入场被相邻消息推迟，等到超时都没能下单",
    "entry_admission_recheck_blocked": "入场推迟期间被重新判定为不可入场",
    "entry_admission_recheck_state_mismatch": "入场推迟记录与指令状态对不上，已作废",
    "close_final_preflight_failed": "最终仓位或合约规格校验失败",
    "protection_recovery_bypassed_for_full_exit": "全平时跳过了保护单恢复",
    "revision_replacement_incomplete": "旧单撤销 / 新单挂出没做完",
    "recovery_timeout": "异常恢复超时，没有重跑",
    "target_not_verifiable": "目标仓位无法验证，已转人工确认",
    "target_ambiguous": "有多个可能的目标仓位，分不清是哪一个",
    "no_verifiable_target": "找不到可核实的目标仓位",
    "snapshot_stale": "交易所仓位快照过期，不敢下判断",
    "lifecycle_apply_failed": "目标已验证但生命周期事件未落地",
    "contract_invalid": "指令契约校验未通过",
    "management_fraction_invalid": "减仓比例无法读取，已拒绝",
    "symbol_price_scale_conflict": "标的与价格区间矛盾，已转人工复核",
    "media_unreadable": "图片无法读取（未下载或 OCR 无内容）",
    "no_target_named": "消息没有说清楚是哪一个仓位",
    "no_actionable_intent": "消息未要求任何动作",
    # --- rule D3: the message was never read at all ---
    "authoritative_failed": "识别失败，权威模型没有产出可用结果",
    "mimo_authoritative_failed": "权威模型调用失败，这条消息没有被识别",
    "authoritative_gap_recovery_expired": "补识别窗口已过，这条消息始终没有被识别",
    "context_resolution_failed": "上下文解析失败",
    "management_recognition_unresolved": "上下文解析失败，认不出这条消息管的是哪个仓位",
    "mimo_authoritative_not_safely_applied": "识别结果未能安全落地",
    "management_close_result_requires_recovery": "平仓结果需要人工复核",
    "management_execution_disabled": "自动持仓管理未启用",
    "management_disabled_plan_only": "只做了计划，没有执行（管理开关未打开）",
    "unknown_exchange_outcome": "交易所返回结果不明",
    "unknown": "原因不明",
    # --- rules D6a/D6b/D6c: the silent stalls (2026-09-26 case note) ---
    "source_deletion_exit_sealed_lane": "删除退出卡住，这个群这个币这个方向的新消息全部被挡下",
    "waiting_source_deletion_exit": "正在等一条删除退出收口，这条消息暂时被挡下",
    "deferred_expired": "被删除退出挡下，等到超时，系统把这条消息作废了（永不执行）",
    "runtime_incident_never_notified": "这条告警一直在发生，但从来没有通知过任何人",
    "runtime_incident_notification_stale": "这条告警还在发生，但上次通知已经很久以前了",
    # --- the watcher's own codes ---
    "instruction_stuck_pending": "指令一直排队，没有开始执行",
    "instruction_stuck_executing": "指令开始执行后没有下文",
    "instruction_stuck_submitted": "已提交给交易所，但一直没有确认",
    "batch_blocked": "执行批次被拦下",
    "batch_partial_failed": "执行批次只做成了一部分",
    "batch_recovery_required": "执行批次需要人工恢复",
    "batch_submit_unknown": "已提交但结果不明",
}

#: Families whose codes carry a variable suffix.
REASON_PREFIX_LABELS = (
    ("stale_pending_voided", "系统停摆期间这条指令被作废"),
    ("protection_authority_frozen", "保护单权限被冻结"),
    ("protection_order_unattributable", "保护单归属不明"),
    ("exact_position_write_gate", "仓位写入闸门拒绝了这次操作"),
    ("ownership_not_verified", "仓位归属没有验证通过"),
)


class AlertDeliveryError(RuntimeError):
    """Delivery failed. Carries the exception *type name* and nothing else."""

    def __init__(self, error_type: str):
        self.error_type = str(error_type)[:64]
        super().__init__(self.error_type)


def reason_label(reason_code: str | None) -> str:
    """Chinese for a reason code; unknown codes are shown and flagged."""

    code = str(reason_code or "").strip()
    if not code:
        return "原因未记录"
    if code in REASON_LABELS:
        return REASON_LABELS[code]
    for prefix, label in REASON_PREFIX_LABELS:
        if code.startswith(prefix) or prefix in code:
            return label
    return f"{code}（未收录原因）"


def action_label(action: str | None) -> str:
    code = str(action or "").strip()
    return ACTION_LABELS.get(code, code or "未知动作")


def side_label(side: str | None) -> str:
    return SIDE_LABELS.get(str(side or "").strip().lower(), "")


STOP_PRICE_ACTIONS = frozenset({"adjust_stop_loss"})


def _position_label(summary: Any) -> str:
    """``"ETH short"`` as the detector records it -> ``"ETH 空"`` for the reader."""

    symbol, _, side = str(summary or "").strip().rpartition(" ")
    translated = side_label(side)
    if not symbol or not translated:
        return str(summary or "").strip()
    return f"{symbol.upper()} {translated}"


def message_excerpt(text: str | None, limit: int = MESSAGE_EXCERPT_LIMIT) -> str:
    """Untrusted text, made safe to read: one line, bounded, never executed."""

    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def beijing_time(moment: datetime | None) -> str:
    if moment is None:
        return "时间不详"
    return moment.astimezone(BEIJING).strftime("%H:%M")


def beijing_date(moment: datetime) -> str:
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d")


def format_case_alert(case: CaseRecord) -> str:
    """The opening alert for one "asked for but not done" case (spec 5.2)."""

    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    symbol = str(evidence.get("symbol") or "").upper()
    side = side_label(evidence.get("side"))
    instrument = " ".join(part for part in (symbol, side) if part)
    stop_text = str(evidence.get("stop_loss_text") or "").strip()
    minutes = evidence.get("minutes_since_message")
    posted_at = parse_isoformat(evidence.get("posted_at"))
    excerpt = message_excerpt(evidence.get("message_text"))

    request_line = f"消息要求：{action_label(evidence.get('action'))}"
    if instrument:
        request_line += f"（{instrument}）"
    # A full exit or a partial take-profit may still carry the strategy's old
    # stop on its candidate; only a stop instruction is *about* that price.
    if stop_text and str(evidence.get("action") or "") in STOP_PRICE_ACTIONS:
        request_line += f" 止损→{stop_text}"

    if minutes is None:
        status_line = "现状：交易所没有对应操作。"
    else:
        status_line = f"现状：已过 {int(minutes)} 分钟，交易所没有对应操作。"

    position_line = "仓位：仍在持仓中"
    if case.target_uncertain:
        open_positions = evidence.get("group_open_positions") or []
        listed = " / ".join(_position_label(item) for item in open_positions[:4])
        position_line += "；目标仓位未确定"
        if listed:
            position_line += f"，群内在仓：{listed}"
    if evidence.get("position_state") == "open_snapshot_stale":
        position_line += "（仓位快照未及时更新，按仍在持仓处理）"

    message_number = case.raw_message_id if case.raw_message_id is not None else "?"
    return "\n".join(
        [
            f"⚠️ 值守提醒 #{case.id}",
            f"群：{group}    消息 #{message_number}（{beijing_time(posted_at)}）",
            request_line,
            f"原文：「{excerpt}」",
            status_line,
            f"卡在：{reason_label(case.reason_code)}（{case.reason_code or '未记录'}）",
            position_line,
        ]
    )


def format_recognition_case_alert(case: CaseRecord) -> str:
    """Rule D3's opening alert (design 4.1/4.5).

    A different story from :func:`format_case_alert` and so a different shape:
    nothing was asked of the exchange, because nothing was read. The lines say
    what came back instead, quote the message, and state the one fact that
    makes it urgent -- the group is holding a position right now.
    """

    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    posted_at = parse_isoformat(evidence.get("posted_at"))
    excerpt = message_excerpt(evidence.get("message_text"))
    open_positions = evidence.get("group_open_positions") or []
    listed = " / ".join(_position_label(item) for item in open_positions[:4])

    status_line = "现状：群内有持仓"
    if listed:
        status_line += f"（{listed}）"
    status_line += "，这条消息没有被自动处理。"

    message_number = case.raw_message_id if case.raw_message_id is not None else "?"
    return "\n".join(
        [
            f"⚠️ 值守提醒 #{case.id}",
            f"群：{group}    消息 #{message_number}（{beijing_time(posted_at)}）",
            f"识别结果：识别失败（{reason_label(case.reason_code)}）",
            f"原文：「{excerpt}」",
            status_line,
        ]
    )


def format_recognition_case_resolved_alert(case: CaseRecord) -> str:
    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    return "\n".join(
        [
            f"✅ 值守提醒 #{case.id} 已自行恢复",
            f"群：{group}    消息 #{case.raw_message_id}",
            "这条消息后来被正常识别处理了，不用处理。",
        ]
    )


def format_case_resolved_alert(case: CaseRecord) -> str:
    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    return "\n".join(
        [
            f"✅ 值守提醒 #{case.id} 已自行恢复",
            f"群：{group}    消息 #{case.raw_message_id}",
            f"{action_label(evidence.get('action'))} 后来执行成功了，不用处理。",
        ]
    )


def _hours_and_minutes(minutes: Any) -> str:
    """"11 天 3 小时" reads; "16003 分钟" does not."""

    try:
        total = max(0, int(minutes))
    except (TypeError, ValueError):
        return "时长不详"
    days, rest = divmod(total, 1440)
    hours, remainder = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {remainder} 分钟"
    return f"{remainder} 分钟"


def _instrument_label(evidence: Mapping[str, Any]) -> str:
    symbol = str(evidence.get("symbol") or "").upper()
    side = side_label(evidence.get("side"))
    return " ".join(part for part in (symbol, side) if part)


def format_sealed_lane_alert(case: CaseRecord) -> str:
    """Rule D6a's opening alert (design 2026-09-26, section 1).

    The exit id is what an engineer acts on, but it says nothing to the person
    reading this, so every line translates it: which group, which instrument
    and direction are shut, how long, and -- the line that makes it urgent --
    how many of that group's later messages the seal has already thrown away.
    """

    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    instrument = _instrument_label(evidence) or "未知标的"
    voided = evidence.get("voided_messages")
    examined = evidence.get("voided_scan_examined")

    if voided is None:
        loss_line = "期间被作废的消息：数不出来（读取受限）"
    elif int(voided) == 0:
        loss_line = "期间还没有消息因此被作废。"
    else:
        loss_line = f"期间已有 {int(voided)} 条消息被作废，永不执行。"
        if examined:
            loss_line += f"（只数了封锁之后这个群的 {int(examined)} 条消息，实际可能更多）"

    return "\n".join(
        [
            f"⚠️ 值守提醒 #{case.id}（这条线被封住了）",
            f"群：{group}    被封的方向：{instrument}",
            f"封了多久：{_hours_and_minutes(evidence.get('minutes_sealed'))}",
            f"原因：{reason_label(case.reason_code)}",
            f"卡住的删除退出：#{evidence.get('exit_id', '?')}"
            f"（状态 {evidence.get('exit_state') or '未记录'}，"
            f"上次原因 {evidence.get('exit_last_reason') or '未记录'}）",
            loss_line,
            "这个方向的新策略现在一条都进不来，直到这条退出收口。",
        ]
    )


def format_sealed_lane_resolved_alert(case: CaseRecord) -> str:
    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    instrument = _instrument_label(evidence) or "未知标的"
    return "\n".join(
        [
            f"✅ 值守提醒 #{case.id} 已解封",
            f"群：{group}    方向：{instrument}",
            f"删除退出 #{evidence.get('exit_id', '?')} 已收口，这条线又能进新策略了。",
        ]
    )


def format_voided_message_alert(case: CaseRecord) -> str:
    """Rule D6b's opening alert (design 2026-09-26, section 2).

    Not the same story as D3: the message *was* read and understood, and then
    the system's own state threw it away. So the alert says what was thrown
    away, quotes it, and -- when it can -- names the exit that did it, so this
    and the D6a alert for the same lane read as one event.
    """

    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    instrument = _instrument_label(evidence)
    posted_at = parse_isoformat(evidence.get("posted_at"))
    excerpt = message_excerpt(evidence.get("message_text"))
    blocking = evidence.get("blocking_exit_id")

    subject = "这条消息" if not instrument else f"这条 {instrument} 的消息"
    lines = [
        f"⚠️ 值守提醒 #{case.id}（消息被系统作废）",
        f"群：{group}    消息 #{case.raw_message_id if case.raw_message_id is not None else '?'}"
        f"（{beijing_time(posted_at)}）",
        f"原文：「{excerpt}」",
        f"结果：{reason_label(case.reason_code)}",
    ]
    if blocking is not None:
        lines.append(
            f"挡住它的是删除退出 #{blocking}"
            f"（状态 {evidence.get('blocking_exit_state') or '未记录'}）。"
        )
    lines.append(f"{subject}不会被执行，也不会重试——要不要补，由你决定。")
    return "\n".join(lines)


def format_voided_message_resolved_alert(case: CaseRecord) -> str:  # pragma: no cover
    """Never expected: ``deferred_expired`` is terminal and nothing undoes it."""

    evidence = case.evidence or {}
    group = str(evidence.get("group_name") or case.chat_id or "未知群")
    return "\n".join(
        [
            f"✅ 值守提醒 #{case.id} 已结束",
            f"群：{group}    消息 #{case.raw_message_id}",
            "这条被作废的消息后来又有了进展，不用处理。",
        ]
    )


def format_unheard_incident_alert(case: CaseRecord) -> str:
    """Rule D6c's opening alert (design 2026-09-26, section 3).

    The one fact that makes it worth sending: this is happening *now*, and the
    last time anybody was told about it was long ago -- or never.
    """

    evidence = case.evidence or {}
    notified_at = parse_isoformat(evidence.get("notified_at"))
    repeat_count = evidence.get("repeat_count")
    summary = message_excerpt(evidence.get("summary"), limit=120)

    if notified_at is None:
        heard_line = "上次通知：从来没有通知过。"
    else:
        heard_line = (
            f"上次通知：{_hours_and_minutes(evidence.get('minutes_since_notified'))}前"
            f"（{beijing_date(notified_at)}）。"
        )
    still_line = (
        "仍在发生："
        f"{_hours_and_minutes(evidence.get('minutes_since_last_occurrence'))}前还在报"
    )
    if repeat_count is not None:
        still_line += f"，累计 {int(repeat_count)} 次"
    still_line += "。"

    return "\n".join(
        [
            f"⚠️ 值守提醒 #{case.id}（告警在喊，没人听见）",
            f"告警：{evidence.get('incident_type') or '未知类型'}"
            f"（严重度 {evidence.get('incident_severity') or '未记录'}，"
            f"记录 #{evidence.get('incident_id', '?')}）",
            f"对象：{evidence.get('source_kind') or '未记录'} "
            f"{evidence.get('source_record_id') or ''}".strip(),
            still_line,
            heard_line,
            f"系统的说法：「{summary}」" if summary else "系统没有留下摘要。",
        ]
    )


def format_unheard_incident_resolved_alert(case: CaseRecord) -> str:
    evidence = case.evidence or {}
    return "\n".join(
        [
            f"✅ 值守提醒 #{case.id} 已结束",
            f"告警 {evidence.get('incident_type') or '未知类型'}"
            f"（记录 #{evidence.get('incident_id', '?')}）"
            "已经不再发生，或者已经重新通知过了。",
        ]
    )


def format_health_alert(case: CaseRecord) -> str:
    evidence = case.evidence or {}
    rule = case.rule
    if rule.startswith("D4"):
        body = (
            f"消息处理停摆：有 {evidence.get('stalled_jobs', '?')} 条消息排队超过 "
            f"{evidence.get('minutes', '?')} 分钟还没被处理。\n"
            f"最早一条是消息 #{evidence.get('oldest_raw_message_id', '?')}。\n"
            "新消息现在很可能不会被识别，也不会下单。"
        )
    elif rule.startswith("D5a"):
        body = (
            f"值守读不到生产数据库，已经连续 {evidence.get('consecutive_failures', '?')} 轮失败。\n"
            "现在无法判断系统是否正常——这不等于系统没问题。"
        )
    elif rule.startswith("D5b"):
        body = (
            f"worker 的健康检查连续 {evidence.get('consecutive_failures', '?')} 轮没有响应，"
            "可能已经卡住。"
        )
    else:  # pragma: no cover - defensive
        body = "值守发现一个健康问题。"
    return f"⚠️ 值守提醒 #{case.id}（系统健康）\n{body}"


def format_health_resolved_alert(case: CaseRecord) -> str:
    rule = case.rule
    what = {
        "D4": "消息处理已经恢复，排队的消息都处理掉了。",
        "D5a": "值守又能读到生产数据库了。",
        "D5b": "worker 的健康检查恢复响应了。",
    }
    body = next(
        (text for prefix, text in what.items() if rule.startswith(prefix)),
        "刚才报的健康问题已经消失。",
    )
    return f"✅ 值守提醒 #{case.id} 已恢复\n{body}"


def format_diagnosis_message(case: CaseRecord, verdict: Mapping[str, Any]) -> str:
    """The six lines of spec 6.4. Plain text, no formatting, no code names.

    Everything here came back from a model that read untrusted KOL text, so it
    only reaches this function after ``oncall_codex.validate_verdict`` has
    checked every field, every length and every redaction pattern.
    """

    urgency = URGENCY_LABELS_ZH.get(str(verdict.get("urgency")), "无需处理")
    category = CATEGORY_LABELS_ZH.get(str(verdict.get("category")), "无法归类")
    should = SHOULD_LABELS_ZH.get(str(verdict.get("should_have_executed")), "说不准是否应该")
    confidence = CONFIDENCE_LABELS_ZH.get(str(verdict.get("confidence")), "低")
    return "\n".join(
        [
            f"🔎 值守诊断 #{case.id}（{urgency}）",
            f"消息本意：{verdict.get('what_message_wanted_zh', '')}",
            f"没执行的原因：{verdict.get('explanation_zh', '')}",
            f"结论：{category}；按消息本意{should}执行",
            f"建议：{verdict.get('recommended_action_zh', '')}",
            f"把握：{confidence}",
        ]
    )


def format_codex_down_alert(failure_class: str | None) -> str:
    """Going dark is itself news, and an auth failure names its own fix."""

    label = FAILURE_LABELS_ZH.get(str(failure_class or ""), "原因不明")
    lines = [
        "⚠️ Codex 当前不可用，新案件暂时没有自动诊断。",
        f"原因：{label}",
        "值守本身照常检测和提醒，只是少了「为什么没执行」这一段。",
    ]
    if str(failure_class or "") == FAILURE_AUTH:
        lines.append("请在服务器上以 root 执行：codex login")
    return "\n".join(lines)


def format_codex_recovered_alert() -> str:
    return "✅ Codex 已恢复，之前没诊断的未结案件会补上。"


def codex_unavailable_note(failure_class: str | None) -> str:
    label = FAILURE_LABELS_ZH.get(str(failure_class or ""), "原因不明")
    return f"Codex 当前不可用（{label}），本案无自动诊断。"


def codex_daily_cap_note(cap: int) -> str:
    return f"今日自动诊断已达上限（{cap} 次），本案无自动诊断。"


def format_group_merged_alert(group_name: str, case_ids: Sequence[int]) -> str:
    numbers = " ".join(f"#{case_id}" for case_id in case_ids)
    return f"⚠️ {group_name} 另有 {len(case_ids)} 条类似情况（案件号 {numbers}）。"


def format_cap_reached_alert(cap: int, suppressed: int) -> str:
    return (
        f"⚠️ 今日告警已达上限（{cap} 条），其余 {suppressed} 条只记在值守状态库里，"
        "明天 0 点后恢复发送。"
    )


def format_daily_summary(
    *,
    opened: int,
    resolved: int,
    skipped_no_position: int,
    read_failed_rounds: int,
) -> str:
    return "\n".join(
        [
            "🟢 值守正常",
            f"过去 24 小时：新建案件 {opened} 条，已恢复 {resolved} 条，"
            f"因为没有真实仓位而忽略 {skipped_no_position} 条，读库失败 {read_failed_rounds} 轮。",
            "（这条每天发一次。哪天没收到，就说明值守本身可能停了。）",
        ]
    )


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


class TelegramAlertSender:
    """Synchronous Telegram Bot API sender. No dependency, no token leak."""

    def __init__(self, *, bot_token: str, chat_id: str, timeout: float = 10.0):
        self._bot_token = str(bot_token)
        self._chat_id = str(chat_id)
        self._timeout = float(timeout)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but a token guard
        return f"TelegramAlertSender(chat_id={self._chat_id!r})"

    def send(self, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": str(text),
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if int(response.status) >= 300:
                    raise AlertDeliveryError(f"HTTP{int(response.status)}")
        except AlertDeliveryError:
            raise
        except urllib.error.HTTPError as exc:
            # ``str(exc)`` would carry the request URL, and the URL carries the
            # bot token. Only the status ever escapes.
            raise AlertDeliveryError(f"HTTP{int(exc.code)}") from None
        except Exception as exc:  # noqa: BLE001 - any failure is a retry
            raise AlertDeliveryError(type(exc).__name__) from None


def deliver_pending_alerts(
    store: OncallStateStore,
    *,
    now: datetime,
    sender: Callable[[str], None] | None,
    max_attempts: int = 5,
) -> tuple[int, int]:
    """Send what is queued. Returns ``(sent, failed)``; never raises.

    ``sender`` of ``None`` is dry-run: the alert stays in the state database
    and the log, and nothing is sent.
    """

    sent = 0
    failed = 0
    for alert in store.pending_alerts(max_attempts=max_attempts):
        if sender is None:
            store.mark_alert_dry_run(alert.id, now)
            logger.info("oncall dry-run alert id=%s kind=%s", alert.id, alert.kind)
            continue
        try:
            sender(alert.body)
        except AlertDeliveryError as exc:
            failed += 1
            store.mark_alert_failed(
                alert.id, error_type=exc.error_type, max_attempts=max_attempts
            )
            logger.warning(
                "oncall alert delivery failed id=%s error_type=%s",
                alert.id,
                exc.error_type,
            )
            continue
        except Exception as exc:  # noqa: BLE001 - delivery never breaks the loop
            failed += 1
            store.mark_alert_failed(
                alert.id,
                error_type=type(exc).__name__,
                max_attempts=max_attempts,
            )
            logger.warning(
                "oncall alert delivery failed id=%s error_type=%s",
                alert.id,
                type(exc).__name__,
            )
            continue
        sent += 1
        store.mark_alert_sent(alert.id, now)
    return sent, failed


# --------------------------------------------------------------------------
# Throttling policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    daily_cap: int = 30
    group_merge_window: timedelta = GROUP_MERGE_WINDOW
    group_merge_threshold: int = GROUP_MERGE_THRESHOLD
    health_cooldown: timedelta = HEALTH_ALERT_COOLDOWN


def _daily_counter_key(now: datetime) -> str:
    return f"daily_alerts:{beijing_date(now)}"


def _daily_suppressed_key(now: datetime) -> str:
    return f"daily_suppressed:{beijing_date(now)}"


def _cap_reached(store: OncallStateStore, now: datetime, policy: AlertPolicy) -> bool:
    return store.get_int_meta(_daily_counter_key(now), 0) >= policy.daily_cap


def _note_capped_alert(store: OncallStateStore, now: datetime) -> None:
    store.bump_counter(_daily_counter_key(now))


def _suppress_for_cap(store: OncallStateStore, now: datetime, policy: AlertPolicy) -> None:
    suppressed = store.bump_counter(_daily_suppressed_key(now))
    body = format_cap_reached_alert(policy.daily_cap, suppressed)
    dedupe_key = f"cap:{beijing_date(now)}"
    if store.enqueue_alert(
        kind=ALERT_KIND_CAP_REACHED,
        body=body,
        now=now,
        dedupe_key=dedupe_key,
    ) is None:
        # One notice a day, but its count must be right when it is sent.
        store.update_pending_alert_body(dedupe_key, body)


def compose_case_alerts(
    store: OncallStateStore,
    *,
    now: datetime,
    new_case_ids: Sequence[int],
    resolved_case_ids: Sequence[int],
    policy: AlertPolicy | None = None,
    codex_note: str | None = None,
) -> int:
    """Turn this round's case changes into queued alerts. Returns how many.

    ``codex_note`` is one extra line for the opening alert, saying that this
    case will get no automatic diagnosis and why. It is derived from state the
    watcher already holds, so composing an alert never waits on Codex.
    """

    settings = policy or AlertPolicy()
    queued = 0
    merged: dict[str, list[int]] = {}

    for case_id in new_case_ids:
        case = store.get_case(case_id)
        if case is None:
            continue
        is_health = case.case_key.startswith("health:")
        if is_health and _health_in_cooldown(store, case, now, settings):
            continue
        if _cap_reached(store, now, settings):
            _suppress_for_cap(store, now, settings)
            continue
        if not is_health and _should_merge_into_group_notice(
            store, case, now, settings
        ):
            group = str((case.evidence or {}).get("group_name") or case.chat_id or "未知群")
            merged.setdefault(group, []).append(case.id)
            store.mark_case_alerted(case.id, now)
            continue
        body = _format_open_alert(case, is_health=is_health)
        if codex_note:
            body = f"{body}\n{codex_note}"
        kind = ALERT_KIND_HEALTH_OPEN if is_health else ALERT_KIND_CASE_OPEN
        if store.enqueue_alert(
            kind=kind,
            body=body,
            now=now,
            case_id=case.id,
            dedupe_key=_episode_key(kind, case),
        ) is not None:
            queued += 1
            _note_capped_alert(store, now)
            if is_health:
                store.set_meta(f"health_alerted:{case.rule}", isoformat(now))
        store.mark_case_alerted(case.id, now)

    for group, case_ids in merged.items():
        if store.enqueue_alert(
            kind=ALERT_KIND_GROUP_MERGED,
            body=format_group_merged_alert(group, case_ids),
            now=now,
            dedupe_key=f"merged:{group}:{min(case_ids)}",
        ) is not None:
            queued += 1
            _note_capped_alert(store, now)

    for case_id in resolved_case_ids:
        case = store.get_case(case_id)
        if case is None or case.alerted_at is None:
            # Never announce the recovery of a problem nobody was told about.
            continue
        is_health = case.case_key.startswith("health:")
        if _cap_reached(store, now, settings):
            _suppress_for_cap(store, now, settings)
            continue
        kind = ALERT_KIND_HEALTH_RESOLVED if is_health else ALERT_KIND_CASE_RESOLVED
        body = _format_resolved_alert(case, is_health=is_health)
        if store.enqueue_alert(
            kind=kind,
            body=body,
            now=now,
            case_id=case.id,
            dedupe_key=_episode_key(kind, case),
        ) is not None:
            queued += 1
            _note_capped_alert(store, now)
    return queued


def compose_diagnosis_alert(
    store: OncallStateStore,
    *,
    case: CaseRecord,
    verdict: Mapping[str, Any],
    now: datetime,
    policy: AlertPolicy | None = None,
) -> bool:
    """Queue the follow-up diagnosis for one case. At most one, ever."""

    settings = policy or AlertPolicy()
    if _cap_reached(store, now, settings):
        _suppress_for_cap(store, now, settings)
        return False
    queued = store.enqueue_alert(
        kind=ALERT_KIND_DIAGNOSIS,
        body=format_diagnosis_message(case, verdict),
        now=now,
        case_id=case.id,
        dedupe_key=f"{ALERT_KIND_DIAGNOSIS}:{case.id}",
    )
    if queued is None:
        return False
    _note_capped_alert(store, now)
    return True


def compose_codex_state_alert(
    store: OncallStateStore,
    *,
    now: datetime,
    down: bool,
    failure_class: str | None = None,
    episode: str = "",
) -> bool:
    """"Codex went dark" / "Codex is back". Never subject to the daily cap.

    Silence about the diagnosis layer would look exactly like a quiet day, and
    the whole point of this phase is that a failure is never silent.
    """

    if down:
        body = format_codex_down_alert(failure_class)
        kind = ALERT_KIND_CODEX_DOWN
    else:
        body = format_codex_recovered_alert()
        kind = ALERT_KIND_CODEX_RECOVERED
    return (
        store.enqueue_alert(
            kind=kind,
            body=body,
            now=now,
            dedupe_key=f"{kind}:{episode or beijing_date(now)}",
        )
        is not None
    )


#: Case-key prefix -> (opening formatter, recovery formatter). The key is the
#: only thing that decides which story a case tells, so the mapping lives in
#: one place rather than in two ``if`` ladders that can disagree.
_CASE_FORMATTERS = (
    (
        RECOGNITION_CASE_PREFIX,
        format_recognition_case_alert,
        format_recognition_case_resolved_alert,
    ),
    (
        SEALED_LANE_CASE_PREFIX,
        format_sealed_lane_alert,
        format_sealed_lane_resolved_alert,
    ),
    (
        VOIDED_MESSAGE_CASE_PREFIX,
        format_voided_message_alert,
        format_voided_message_resolved_alert,
    ),
    (
        UNHEARD_INCIDENT_CASE_PREFIX,
        format_unheard_incident_alert,
        format_unheard_incident_resolved_alert,
    ),
)


def _format_open_alert(case: CaseRecord, *, is_health: bool) -> str:
    """The case key decides which story this case is."""

    if is_health:
        return format_health_alert(case)
    for prefix, opening, _resolved in _CASE_FORMATTERS:
        if case.case_key.startswith(prefix):
            return opening(case)
    return format_case_alert(case)


def _format_resolved_alert(case: CaseRecord, *, is_health: bool) -> str:
    if is_health:
        return format_health_resolved_alert(case)
    for prefix, _opening, resolved in _CASE_FORMATTERS:
        if case.case_key.startswith(prefix):
            return resolved(case)
    return format_case_resolved_alert(case)


def _episode_key(kind: str, case: CaseRecord) -> str:
    """One alert per case *episode*.

    A health case is re-opened when its condition returns, and each episode
    deserves its own notice; an instruction case never re-opens, so its key is
    constant and the unique index does the de-duplication across restarts.
    """

    started = isoformat(case.first_seen_at) if case.first_seen_at else "?"
    return f"{kind}:{case.id}:{started}"


def _health_in_cooldown(
    store: OncallStateStore, case: CaseRecord, now: datetime, policy: AlertPolicy
) -> bool:
    """Cooldown is per *rule*: D4 coming and going must not page repeatedly."""

    last = parse_isoformat(store.get_meta(f"health_alerted:{case.rule}"))
    if last is None:
        return False
    return (now - last) < policy.health_cooldown


def _should_merge_into_group_notice(
    store: OncallStateStore, case: CaseRecord, now: datetime, policy: AlertPolicy
) -> bool:
    if case.chat_id is None:
        return False
    already = store.count_cases_alerted_since(
        chat_id=case.chat_id, since=now - policy.group_merge_window
    )
    return already >= policy.group_merge_threshold


META_DAILY_SUMMARY_DATE = "daily_summary_date"
META_DAILY_SUMMARY_AT = "daily_summary_at"
_SUMMARY_SNAPSHOT = "daily_summary_snapshot"


def maybe_compose_daily_summary(
    store: OncallStateStore,
    *,
    now: datetime,
    counters: dict[str, int],
    hour: int = 9,
) -> bool:
    """One "the watch is fine" message a day, after 09:00 Beijing.

    Silence is the failure signal the user is asked to watch for, so this is
    the one alert that is not subject to the daily cap.
    """

    local = now.astimezone(BEIJING)
    today = beijing_date(now)
    if local.hour < hour:
        return False
    if store.get_meta(META_DAILY_SUMMARY_DATE) == today:
        return False
    previous_at = parse_isoformat(store.get_meta(META_DAILY_SUMMARY_AT)) or (
        now - timedelta(hours=24)
    )
    snapshot = _summary_snapshot(store)
    body = format_daily_summary(
        opened=store.count_cases_since(since=previous_at),
        resolved=store.count_cases_since(since=previous_at, status="resolved"),
        skipped_no_position=max(
            0, int(counters.get("skipped_no_position", 0)) - snapshot.get("skipped_no_position", 0)
        ),
        read_failed_rounds=max(
            0, int(counters.get("read_failed_rounds", 0)) - snapshot.get("read_failed_rounds", 0)
        ),
    )
    store.enqueue_alert(
        kind=ALERT_KIND_DAILY_SUMMARY,
        body=body,
        now=now,
        dedupe_key=f"daily:{today}",
    )
    store.set_meta(META_DAILY_SUMMARY_DATE, today)
    store.set_meta(META_DAILY_SUMMARY_AT, isoformat(now))
    store.set_meta(
        _SUMMARY_SNAPSHOT,
        json.dumps({key: int(value) for key, value in counters.items()}),
    )
    return True


def _summary_snapshot(store: OncallStateStore) -> dict[str, int]:
    try:
        parsed = json.loads(store.get_meta(_SUMMARY_SNAPSHOT) or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def redact_for_log(value: Any) -> str:  # pragma: no cover - used by callers
    """Never let a configuration value reach a log line by accident."""

    return "<redacted>" if value else "<empty>"
