"""Worker-side deterministic remediation gates for on-call phase 3.

See docs/plans/2026-09-26-codex-oncall-phase3-spec.md. This module contains
no AI, no network calls, and no Telegram transport of its own -- it is a
synchronous, dependency-injectable library the worker process calls (via
``asyncio.to_thread``, wired up in a later batch) from a round-trip-only HTTP
endpoint and from its own system-bot ``getUpdates`` loop.

Everything here is fail-closed: any unexpected state, stale read, or
exception turns into ``refused``/``failed``/``uncertain``, never a silent
"skip the check and continue".  Every gate decision is recorded as one
``oncall_remediation_events`` row (INSERT only -- see ``_append_event``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time as dtime, timedelta
from typing import Any, Callable

from sqlalchemy import exists, func, or_, update
from sqlalchemy.orm import aliased, sessionmaker

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.execution_bindings import _load_reconcile_snapshot
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    OncallRemediationAudit,
    OncallRemediationControl,
    OncallRemediationEvent,
    OncallRemediationProposal,
    RawMessage,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
    SignalCandidate,
    Source,
    StrategyManagementBatch,
)
from telegram_kol_research.oncall_remediation_auto import (
    AutoHealthInputs,
    GateDOutcome,
    redact_structure,
    run_gate_d,
)
from telegram_kol_research.position_management_remediation import (
    PositionRemediationAction,
    PositionRemediationPlan,
    RemediationScope,
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
    resolve_remediation_scope,
)
from telegram_kol_research.trading_settings import load_trading_settings

try:  # pragma: no cover - exercised indirectly through gate A3 tests
    from telegram_kol_research.auto_trade_execution import (
        group_and_kol_auto_trade_currently_enabled,
    )
except ImportError:  # pragma: no cover - defensive only
    group_and_kol_auto_trade_currently_enabled = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WHITELISTED_ACTION_KINDS = frozenset(
    {
        "full_exit",
        "partial_take_profit",
        "move_stop_to_break_even",
        "adjust_stop_loss",
    }
)

NON_TERMINAL_PROPOSAL_STATES = frozenset(
    {"requested", "proposed", "confirming", "executing"}
)
SETTLED_PROPOSAL_STATES = frozenset({"succeeded", "executing", "uncertain"})

# spec 4.4 A7: nine irreversible-refusal reason families. Matched as a plain
# substring against error_json/result_json/reason_code/runtime_incident text
# -- these are already-produced, already-redacted machine strings from the
# main execution path, not free text, so substring matching is exact enough
# and avoids having to enumerate every literal spelling here.
A7_IRREVERSIBLE_REASON_PATTERNS = (
    "ownership_not_verified",
    "exact_position_write_gate",
    "protection_authority_frozen",
    "protection_order_unattributable",
    "explicit_stop_adjustment_not_risk_tightening",
    "management_price_implausible",
    "management_stop_direction_invalid",
    "operator_dismissed",
    "kol_or_group_auto_trade_disabled",
)

_BEIJING_OFFSET = timedelta(hours=8)

# A11 cooldown / daily execution cap count real executions only: a proposal
# still executing, one that settled after reaching the exchange-write path
# (succeeded / uncertain), or one that carries a live management batch. A
# pre-apply refusal (e.g. plan_changed) never executed anything.
_REAL_EXECUTION_PREDICATE = or_(
    OncallRemediationProposal.state.in_(("executing", "succeeded", "uncertain")),
    OncallRemediationProposal.management_batch_id.is_not(None),
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisterResult:
    proposal_id: int
    state: str
    created: bool


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    proposal_id: int
    state: str  # "proposed" | "refused" | "executing"
    refusal_reason: str | None
    text: str | None
    keyboard: tuple[tuple[str, str], ...] | None
    should_send: bool
    breaker_tripped: bool = False
    # Phase 4: non-None iff G-D passed and this proposal was just promoted
    # straight to "executing" (single-flight CAS won) -- the caller must
    # invoke ``execute_proposal`` for it, exactly like ``CallbackOutcome``'s
    # ``execute_proposal_id`` in the human-approved path.
    auto_execute_proposal_id: int | None = None


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    proposal_id: int | None
    accepted: bool
    text: str | None
    keyboard: tuple[tuple[str, str], ...] | None
    remove_keyboard: bool = False
    execute_proposal_id: int | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    accepted: bool
    text: str
    proposal_id: int | None = None
    keyboard: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    proposal_id: int
    state: str  # "executing" (still in flight, follow later) | succeeded | failed | uncertain
    management_batch_id: int | None
    text: str | None
    breaker_tripped: bool = False


@dataclass(frozen=True, slots=True)
class FinalizeOutcome:
    proposal_id: int
    state: str
    text: str
    breaker_tripped: bool = False


@dataclass(frozen=True, slots=True)
class ReadbackResult:
    """Post-execution exchange readback (spec section 4 tail / section 8).

    ``outcome`` is ``"confirmed"`` (expected effect observed on a fresh
    snapshot), ``"mismatch"`` (snapshot read fine but does not match), or
    ``"unknown"`` (snapshot incomplete, or nothing to compare against --
    treated the same as ``"mismatch"`` by the caller: this repository's
    fail-closed convention never treats an incomplete read as healthy).
    ``"skipped"`` means no ``deepcoin_client_factory`` was supplied to
    ``finalize_executing_proposals`` -- readback is a no-op then, not a
    gate (keeps every caller that predates this change unaffected).
    ``branch`` is only meaningful for ``move_stop_to_break_even``
    (``"stop_placed"`` | ``"market_closed"``), which is otherwise
    ``predict_break_even_branch``'s ``"unknown_until_execution"`` right up
    to this point -- see oncall_remediation_auto.py's D5 docstring and
    docs/codex-oncall-status.md 9.5 known gap #1.
    """

    outcome: str
    detail: dict[str, Any]
    branch: str | None = None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _naive_utc(value: datetime) -> datetime:
    """Convert to the naive-UTC representation every DateTime column stores.

    models.py has no TypeDecorator for DateTime, so SQLite round-trips a
    tz-aware datetime as a naive one; the rest of the codebase's convention
    (e.g. execution_bindings.py:5022, entry_revision_executor.py:828) is
    ``value.astimezone(UTC).replace(tzinfo=None)``. Mirrored here so this
    module's "now" is always directly comparable to what comes back from the
    ORM.
    """

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _beijing_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of "now"'s Beijing (UTC+8) calendar day, in naive UTC."""

    beijing_now = now + _BEIJING_OFFSET
    start_beijing = datetime.combine(beijing_now.date(), dtime.min)
    end_beijing = start_beijing + timedelta(days=1)
    return start_beijing - _BEIJING_OFFSET, end_beijing - _BEIJING_OFFSET


def _beijing_hhmm(value: datetime) -> str:
    local = _naive_utc(value) + _BEIJING_OFFSET
    return local.strftime("%H:%M")


def _bounded_json(value: Any, *, limit: int) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    # Fail-closed truncation: never write a value the CHECK constraint would
    # reject; a truncated-but-flagged blob is still useful for audit.
    return json.dumps({"_truncated": True}, ensure_ascii=False)[:limit]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _append_event(
    session,
    proposal_id: int | None,
    *,
    actor: str,
    event: str,
    outcome: str,
    gate: str | None = None,
    check: str | None = None,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> None:
    """INSERT one audit row. No code path in this module ever UPDATEs or
    DELETEs an ``OncallRemediationEvent`` -- see the static assertion in
    tests/test_oncall_remediation.py."""

    session.add(
        OncallRemediationEvent(
            proposal_id=proposal_id,
            at=at or datetime.now(UTC).replace(tzinfo=None),
            actor=actor[:64],
            event=event[:64],
            gate=gate,
            check=check,
            outcome=outcome[:32],
            detail_json=_bounded_json(detail or {}, limit=2048),
        )
    )


def _append_audit(session, proposal_id: int, *, phase: str, payload: dict[str, Any]) -> None:
    """INSERT one ``oncall_remediation_audit`` row (spec 6.2). INSERT-only:
    no code path in this module UPDATEs or DELETEs this table -- see the
    static assertion in tests/test_oncall_remediation_auto.py.

    Redacted (via ``oncall_remediation_auto.redact_structure``, the same
    patterns ``oncall_codex.py`` uses) and bounded to 64KB; truncation drops
    the largest ``execution_events`` response bodies first (the ones most
    likely to carry raw exchange payloads) rather than failing the INSERT.
    """

    redacted_payload, _hits = redact_structure(payload)
    encoded = json.dumps(redacted_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    truncated = False
    if len(encoded) > 65536:
        truncated = True
        trimmed = dict(redacted_payload)
        exchange_traffic = trimmed.get("exchange_traffic")
        if isinstance(exchange_traffic, list) and exchange_traffic:
            events = [row for row in exchange_traffic if row.get("kind") == "execution_event"]
            events.sort(key=lambda row: row.get("id", 0))
            while events and len(
                json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ) > 65536:
                oldest = events.pop(0)
                if oldest in exchange_traffic:
                    exchange_traffic.remove(oldest)
        encoded = json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 65536:
            encoded = json.dumps({"_truncated": True}, ensure_ascii=False)
    session.add(
        OncallRemediationAudit(
            proposal_id=proposal_id,
            phase=phase,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            payload_json=encoded,
            truncated=truncated,
        )
    )


def render_remediation_audit_report(session_factory: sessionmaker, *, proposal_id: int) -> str:
    """Public entry point for the read-only ``oncall-remediation-audit`` CLI
    command (cli.py) -- identical to what ``/audit P<n>`` sends, so the two
    "倒查" paths (Telegram, CLI) can never drift. Thin wrapper kept so the
    CLI module never has to reach into this module's private helpers."""

    return _render_audit_report(session_factory, proposal_id=proposal_id)


def _render_audit_report(session_factory: sessionmaker, *, proposal_id: int) -> str:
    """``/audit P<n>`` (spec 6.2): merge the pre_apply/result audit rows for
    one proposal into one human-readable report, <= 4000 chars."""

    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        if proposal is None:
            return f"提案 P{proposal_id} 不存在"
        rows = (
            session.query(OncallRemediationAudit)
            .filter(OncallRemediationAudit.proposal_id == proposal_id)
            .order_by(OncallRemediationAudit.id)
            .all()
        )
        lines = [
            f"倒查 P{proposal_id}（值守 #{proposal.case_no}）",
            f"动作：{proposal.action_kind or '?'}  来源：{proposal.execution_origin or 'manual'}",
            f"状态：{proposal.state}  批次：{proposal.management_batch_id}",
        ]
        if not rows:
            lines.append("尚无审计记录（可能还未进入执行）")
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError):
                payload = {}
            lines.append(f"--- {row.phase} @ {row.created_at} {'(截断)' if row.truncated else ''}")
            for key in ("trigger", "target", "gates", "params", "exchange_traffic", "result", "readback"):
                if key in payload:
                    lines.append(f"{key}: {json.dumps(payload[key], ensure_ascii=False)[:400]}")
    return "\n".join(lines)[:4000]


def _get_or_create_control(session) -> OncallRemediationControl:
    control = session.get(OncallRemediationControl, 1)
    if control is None:
        control = OncallRemediationControl(id=1, enabled=True, consecutive_failures=0)
        session.add(control)
        session.flush()
    return control


# ---------------------------------------------------------------------------
# Chinese copy (spec section 6)
# ---------------------------------------------------------------------------

_REFUSAL_REASON_ZH: dict[str, str] = {
    "remediation_disabled": "补救未启用",
    "already_remediated": "该消息此意图已补救过",
    "target_not_resolved": "目标未定，无法补救",
    "live_management_execution_disabled": "生产未开启实盘管理执行",
    "source_message_unavailable": "源消息不可用（已删除或不存在）",
    "kol_or_group_auto_trade_disabled": "该群/KOL 当前未开自动交易",
    "exchange_snapshot_incomplete": "交易所快照不完整",
    "no_ready_action": "没有可执行的补救动作",
    "ambiguous_action": "该消息对应多个待批准动作，无法唯一确定",
    "action_kind_not_supported": "该类型的补救暂不支持",
    "cancel_entry_conversion_not_supported": "晚成交转平仓暂不支持补救",
    "shadow_planned_not_remediated": "该指令当时为只计划不执行，本版不补救",
    "remediation_window_expired": "补救窗口已过期",
    "target_position_not_live": "目标仓位已不在场",
    "cooldown": "同一仓位冷却中，请稍后再试",
    "daily_execution_cap": "今日补救执行次数已达上限",
    "daily_proposal_cap": "今日提案消息已达上限",
    "plan_changed": "计划已变化（多为同币种有新成交或挂撤单），本次未执行；可发送 /fix 加本提案号重新生成提案",
    "expired": "提案已过期",
    "state_changed": "提案状态已变化",
    # Phase 4 G-D reason codes (docs/plans/2026-09-27 spec section 4).
    "d1_reason_not_transient": "原失败原因不属于可自动重试的瞬时原因",
    "d2_successor_message": "该目标之后又有新的消息/批次",
    "d3_protection_unhealthy": "仓位保护状态不健康",
    "d3_protection_ownership_gap": "保护单归属核对不上",
    "d3_position_externally_changed": "仓位疑似被外部改动",
    "d4_quote_unavailable": "无法取得可用行情",
    "d4_stop_direction_invalid": "新止损方向不对",
    "d4_stop_distance_too_close": "新止损距离市价太近",
    "d6_process_uptime_too_short": "系统刚重启，运行时间不足",
    "d6_recent_loop_stall": "近期出现事件循环卡顿",
    "d6_severe_protection_incident": "近期出现严重保护事故",
    "d7_auto_window_expired": "已超出自动补救时效窗",
    "d8_auto_daily_cap": "今日自动执行次数已达上限",
    "d8_auto_per_chat_daily_cap": "该群今日自动执行次数已达上限",
    "d8_auto_cooldown": "同一仓位自动补救冷却中",
    "d9_action_not_enabled": "该类型动作暂未放开自动执行",
    "auto_suspended": "自动补救已暂停",
    "auto_single_flight_busy": "另一笔正在自动执行，本次改为只提示",
    "d_internal_error": "自动闸门内部错误",
}


def _refusal_text_zh(reason: str | None) -> str:
    if not reason:
        return "内部检查未通过"
    if reason in _REFUSAL_REASON_ZH:
        return _REFUSAL_REASON_ZH[reason]
    if reason.startswith("irreversible_refusal:"):
        return f"存在不可推翻的拒绝原因：{reason.split(':', 1)[1]}"
    if reason.startswith("internal_error:"):
        return "系统内部错误，已按失败关闭处理"
    return f"内部检查未通过（{reason}）"


_ACTION_KIND_ZH: dict[str, str] = {
    "full_exit": "全部平仓",
    "partial_take_profit": "平掉部分仓位",
    "move_stop_to_break_even": "止损移到保本（入场价）；若价格已越过保本价将改为市价平仓",
    "adjust_stop_loss": "止损调整（仅收紧）",
}


def _action_kind_zh(action_kind: str, expected_effect: dict[str, Any]) -> str:
    if action_kind == "partial_take_profit":
        fraction = expected_effect.get("fraction")
        try:
            pct = f"{float(fraction) * 100:.0f}%"
        except (TypeError, ValueError):
            pct = "部分"
        return f"平掉 {pct}"
    if action_kind == "adjust_stop_loss":
        stop = expected_effect.get("stop_loss")
        return f"止损调整为 {stop}（仅收紧）" if stop is not None else _ACTION_KIND_ZH[action_kind]
    return _ACTION_KIND_ZH.get(action_kind, action_kind)


def _side_zh(side: str | None) -> str:
    normalized = str(side or "").strip().lower()
    if normalized in {"long", "buy"}:
        return "多"
    if normalized in {"short", "sell"}:
        return "空"
    return "?"


def _symbol_and_side_from_action(action: PositionRemediationAction) -> tuple[str, str]:
    instruments = action.evidence.get("instrument_scope") or []
    symbol = str(instruments[0]).split("-")[0] if instruments else "?"
    positions = action.evidence.get("positions") or []
    side = None
    if positions and isinstance(positions[0], dict):
        side = positions[0].get("posSide") or positions[0].get("pos_side")
    return symbol, _side_zh(side)


def _format_proposal_text(
    *,
    proposal_id: int,
    case_no: int,
    raw_message_id: int,
    expires_at: datetime | None,
    action: PositionRemediationAction,
    shadow: bool,
    group_label: str,
    window_minutes: int,
    posted_at: datetime,
    auto_downgrade_reason: str | None = None,
) -> str:
    symbol, side = _symbol_and_side_from_action(action)
    action_zh = _action_kind_zh(action.action_kind, action.expected_effect)
    window_end = posted_at + timedelta(minutes=window_minutes)
    header = "🛠 补救提案（只提示）" if shadow else f"🛠 补救提案 P{proposal_id}（对应值守 #{case_no}）"
    lines = [
        header,
        f"消息：{group_label} #{raw_message_id}（{_beijing_hhmm(posted_at)}）",
        f"将执行：{symbol} {side} {action_zh}",
        "依据：这些数字由系统从生产数据重算，不来自 AI",
        f"参考：Codex 意见见值守 #{case_no} 的诊断消息（仅供参考，不影响按钮）",
    ]
    if shadow:
        lines.append(f"消息的补救窗口到 {_beijing_hhmm(window_end)}")
        lines.append("本来会执行上述操作；当前为只提示模式")
    else:
        expires_text = _beijing_hhmm(expires_at) if expires_at is not None else "?"
        lines.append(f"时效：本提案 {expires_text} 前有效；消息的补救窗口到 {_beijing_hhmm(window_end)}")
    if auto_downgrade_reason:
        lines.append(f"未自动执行：{_refusal_text_zh(auto_downgrade_reason)}")
    return "\n".join(lines)[:4096]


def _format_refusal_text(*, case_no: int, reason: str) -> str:
    return f"ℹ️ 值守 #{case_no} 没有可执行的补救：{_refusal_text_zh(reason)}"[:4096]


def _format_confirm_text(*, action: PositionRemediationAction, confirm_expiry_minutes: int) -> str:
    symbol, side = _symbol_and_side_from_action(action)
    action_zh = _action_kind_zh(action.action_kind, action.expected_effect)
    return f"确认执行：{symbol} {side} {action_zh}？{confirm_expiry_minutes} 分钟内有效"[:4096]


def _format_result_text(
    *,
    proposal_id: int,
    state: str,
    detail: str,
    breaker_message: str | None,
) -> str:
    if state == "succeeded":
        text = f"✅ 已补救 P{proposal_id}：{detail}"
    elif state == "failed":
        text = f"❌ 补救失败 P{proposal_id}：{detail}"
    else:
        text = f"⚠️ 补救结果未知 P{proposal_id}：{detail}。请人工核对交易所。"
    if breaker_message:
        text = f"{text}\n{breaker_message}"
    return text[:4096]


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------


def _window_minutes_for(action_kind: str | None, config: OncallRemediationConfig) -> int:
    if action_kind == "full_exit":
        return config.exit_window_minutes
    if action_kind == "partial_take_profit":
        return config.partial_tp_window_minutes
    if action_kind in {"adjust_stop_loss", "move_stop_to_break_even"}:
        return config.stop_window_minutes
    return 0


def _proposal_messages_today(session, *, now: datetime) -> int:
    """Proposal-type messages produced this Beijing day (proposals + refusals)."""

    day_start, day_end = _beijing_day_bounds_utc(now)
    proposed = (
        session.query(func.count(OncallRemediationProposal.id))
        .filter(
            OncallRemediationProposal.proposed_at >= day_start,
            OncallRemediationProposal.proposed_at < day_end,
        )
        .scalar()
        or 0
    )
    refused = (
        session.query(func.count(OncallRemediationProposal.id))
        .filter(
            OncallRemediationProposal.state == "refused",
            OncallRemediationProposal.finished_at >= day_start,
            OncallRemediationProposal.finished_at < day_end,
        )
        .scalar()
        or 0
    )
    return int(proposed) + int(refused)


def _find_step_reason(plan: PositionRemediationPlan, raw_message_id: int) -> str | None:
    for chain in plan.chains:
        for step in chain.steps:
            if step.raw_message_id == raw_message_id:
                return f"{step.state}:{step.reason}" if step.reason else step.state
    for conflict in plan.conflicts:
        if int(conflict.get("raw_message_id") or 0) == raw_message_id:
            return str(conflict.get("reason"))
    return None


def _find_irreversible_reason(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    instruction_item_id: int | None,
) -> str | None:
    """A7: an index-backed, index-only search for an unrecoverable reason.

    Three sources, each reached through an indexed/primary-key lookup: the
    triggering instruction item's own error/result JSON (primary key),
    management batches for this message (``raw_message_id`` is an indexed
    column on ``strategy_management_batches``), and runtime incidents that
    named this message (``runtime_incident_affected_messages.raw_message_id``
    is indexed, joined to ``runtime_incidents`` by primary key).
    """

    with session_factory() as session:
        if instruction_item_id is not None:
            item = session.get(MessageInstructionItem, int(instruction_item_id))
            if item is not None:
                for blob in (item.error_json, item.result_json):
                    if blob:
                        hit = _match_a7_pattern(str(blob))
                        if hit:
                            return hit
        reason_codes = (
            session.query(StrategyManagementBatch.reason_code)
            .filter(StrategyManagementBatch.raw_message_id == raw_message_id)
            .all()
        )
        for (reason_code,) in reason_codes:
            if reason_code:
                hit = _match_a7_pattern(str(reason_code))
                if hit:
                    return hit
        incident_rows = (
            session.query(RuntimeIncident.incident_type, RuntimeIncident.redacted_summary)
            .join(
                RuntimeIncidentAffectedMessage,
                RuntimeIncidentAffectedMessage.runtime_incident_id == RuntimeIncident.id,
            )
            .filter(RuntimeIncidentAffectedMessage.raw_message_id == raw_message_id)
            .all()
        )
        for incident_type, summary in incident_rows:
            for blob in (incident_type, summary):
                if blob:
                    hit = _match_a7_pattern(str(blob))
                    if hit:
                        return hit
    return None


def _match_a7_pattern(text: str) -> str | None:
    for pattern in A7_IRREVERSIBLE_REASON_PATTERNS:
        if pattern in text:
            return pattern
    return None


def _build_action_snapshot(action: PositionRemediationAction) -> dict[str, Any]:
    """Bounded (<=8KB) evidence subset stored on the proposal row.

    ``evidence["positions"]`` can be large (raw exchange rows); only the
    fields a human/re-check actually needs survive: pos_id, side, size,
    average entry price. Everything else in ``evidence`` is dropped from the
    stored snapshot (it is re-derivable by rebuilding the plan from
    ``scope_json`` -- this snapshot exists for audit/display, not as the
    execution source of truth).
    """

    positions = []
    for row in action.evidence.get("positions") or []:
        if not isinstance(row, dict):
            continue
        positions.append(
            {
                "pos_id": row.get("posId") or row.get("pos_id") or row.get("id"),
                "pos_side": row.get("posSide") or row.get("pos_side"),
                "size": row.get("pos") or row.get("size") or row.get("sz"),
                "avg_entry_price": row.get("avgPx")
                or row.get("avgPrice")
                or row.get("avg_entry_price"),
            }
        )
    return {
        "action_id": action.action_id,
        "fingerprint": action.fingerprint,
        "action_kind": action.action_kind,
        "raw_message_id": action.raw_message_id,
        "lifecycle_id": action.lifecycle_id,
        "strategy_instance_id": action.strategy_instance_id,
        "pos_ids": list(action.pos_ids),
        "expected_effect": action.expected_effect,
        "instrument_scope": action.evidence.get("instrument_scope"),
        "instruction_item_id": action.evidence.get("instruction_item_id"),
        "candidate_id": action.evidence.get("candidate_id"),
        "positions": positions,
    }


@dataclass(frozen=True, slots=True)
class _GateAResult:
    ok: bool
    action: PositionRemediationAction | None = None
    plan: PositionRemediationPlan | None = None
    scope: RemediationScope | None = None
    reason: str | None = None
    check: str | None = None
    posted_at: datetime | None = None
    window_minutes: int = 0
    raw_message: RawMessage | None = None


def _run_gate_a(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    raw_message_id: int,
    deepcoin_client,
    group_config,
    now: datetime,
    resolve_scope: Callable[..., RemediationScope | None],
    build_plan: Callable[..., PositionRemediationPlan],
    exclude_proposal_id: int,
) -> _GateAResult:
    """A1-A11 (A12 is checked by the caller, which already holds ``control``).

    Shared by ``compute_requested_proposal`` (first pass) and
    ``execute_proposal`` (full rerun before promoting to exchange writes, per
    spec G-C). ``exclude_proposal_id`` keeps a proposal from tripping A10/A11
    against its own row.
    """

    if config.effective_mode == "off":
        return _GateAResult(ok=False, reason="remediation_disabled", check="A1")

    # Spec 4.2 (last bullet): a message whose management candidate has no
    # resolved target lifecycle is never proposed. ``resolve_remediation_scope``
    # deliberately includes the same-group fan-out so the *plan* stays
    # complete, which is exactly why this has to be refused here explicitly.
    with session_factory() as session:
        unresolved_target = (
            session.query(SignalCandidate.id)
            .filter(
                SignalCandidate.raw_message_id == raw_message_id,
                SignalCandidate.parse_source == "mimo_authoritative",
                SignalCandidate.review_status != "approved_remediation",
                SignalCandidate.event_type.in_(("close_signal", "position_update")),
                SignalCandidate.target_lifecycle_id.is_(None),
            )
            .first()
        )
    if unresolved_target is not None:
        return _GateAResult(ok=False, reason="target_not_resolved", check="scope")

    scope = resolve_scope(session_factory, raw_message_id=raw_message_id)
    if scope is None:
        return _GateAResult(ok=False, reason="target_not_resolved", check="scope")

    settings = load_trading_settings(session_factory)
    if not settings.live_management_execution_enabled:
        return _GateAResult(ok=False, reason="live_management_execution_disabled", check="A2")

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None or raw_message.deleted_at is not None:
            return _GateAResult(ok=False, reason="source_message_unavailable", check="A3")
        source = None
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .order_by(SignalCandidate.id)
            .all()
        )
        for candidate in candidates:
            if getattr(candidate, "source_id", None):
                source = session.get(Source, candidate.source_id)
                if source is not None:
                    break
        posted_at = _naive_utc(raw_message.posted_at) if raw_message.posted_at else None
        raw_chat_id = raw_message.chat_id
        raw_message_snapshot = raw_message

    if group_and_kol_auto_trade_currently_enabled is None:
        return _GateAResult(ok=False, reason="internal_error:auto_trade_check_unavailable", check="A3")
    if not group_and_kol_auto_trade_currently_enabled(
        group_config, raw_message=raw_message_snapshot, source=source, settings=settings
    ):
        return _GateAResult(ok=False, reason="kol_or_group_auto_trade_disabled", check="A3")

    plan = build_plan(session_factory, deepcoin_client=deepcoin_client, now=now, scope=scope)

    if any(
        conflict.get("reason") == "exchange_snapshot_incomplete" for conflict in plan.conflicts
    ):
        return _GateAResult(ok=False, reason="exchange_snapshot_incomplete", check="A4", scope=scope, plan=plan)

    matches = [a for a in plan.actions if a.raw_message_id == raw_message_id]
    if len(matches) == 0:
        step_reason = _find_step_reason(plan, raw_message_id)
        return _GateAResult(
            ok=False,
            reason="no_ready_action" + (f":{step_reason}" if step_reason else ""),
            check="A5",
            scope=scope,
            plan=plan,
        )
    if len(matches) > 1:
        return _GateAResult(ok=False, reason="ambiguous_action", check="A5", scope=scope, plan=plan)
    action = matches[0]

    if action.action_kind not in WHITELISTED_ACTION_KINDS:
        return _GateAResult(ok=False, reason="action_kind_not_supported", check="A6", scope=scope, plan=plan)
    if bool(action.evidence.get("late_fill_conversion")) or action.evidence.get(
        "original_action_kind"
    ) == "cancel_entry":
        return _GateAResult(
            ok=False,
            reason="cancel_entry_conversion_not_supported",
            check="A6",
            scope=scope,
            plan=plan,
        )

    instruction_item_id = action.evidence.get("instruction_item_id")
    if instruction_item_id is not None:
        with session_factory() as session:
            item = session.get(MessageInstructionItem, int(instruction_item_id))
            if item is not None and item.status == "succeeded" and item.result_json:
                try:
                    result_payload = json.loads(item.result_json)
                except (TypeError, ValueError):
                    result_payload = None
                if (
                    isinstance(result_payload, dict)
                    and str(result_payload.get("status") or "").lower() == "shadow_planned"
                ):
                    return _GateAResult(
                        ok=False,
                        reason="shadow_planned_not_remediated",
                        check="A6b",
                        scope=scope,
                        plan=plan,
                    )

    irreversible = _find_irreversible_reason(
        session_factory,
        raw_message_id=raw_message_id,
        instruction_item_id=instruction_item_id,
    )
    if irreversible is not None:
        return _GateAResult(
            ok=False,
            reason=f"irreversible_refusal:{irreversible}",
            check="A7",
            scope=scope,
            plan=plan,
        )

    window_minutes = _window_minutes_for(action.action_kind, config)
    if posted_at is None:
        return _GateAResult(ok=False, reason="source_message_unavailable", check="A8", scope=scope, plan=plan)
    elapsed_minutes = (now - posted_at).total_seconds() / 60.0
    if elapsed_minutes > window_minutes:
        return _GateAResult(
            ok=False,
            reason="remediation_window_expired",
            check="A8",
            scope=scope,
            plan=plan,
            posted_at=posted_at,
            window_minutes=window_minutes,
        )

    # A9: re-assert against the very snapshot the plan used (the plan already
    # guarantees it; this catches a planner regression, spec 4.4 A9).
    snapshot_pos_ids = {
        str(row.get("posId") or row.get("pos_id") or row.get("id") or "")
        for row in (action.evidence.get("positions") or [])
        if isinstance(row, dict)
    }
    snapshot_pos_ids.discard("")
    if not action.pos_ids or not set(action.pos_ids) <= snapshot_pos_ids:
        return _GateAResult(ok=False, reason="target_position_not_live", check="A9", scope=scope, plan=plan)

    with session_factory() as session:
        dup = (
            session.query(OncallRemediationProposal.id)
            .filter(
                OncallRemediationProposal.raw_message_id == action.raw_message_id,
                OncallRemediationProposal.action_kind == action.action_kind,
                OncallRemediationProposal.lifecycle_id == action.lifecycle_id,
                OncallRemediationProposal.state.in_(tuple(SETTLED_PROPOSAL_STATES)),
                OncallRemediationProposal.id != exclude_proposal_id,
            )
            .first()
        )
        if dup is not None:
            return _GateAResult(ok=False, reason="already_remediated", check="A10", scope=scope, plan=plan)

        last_executing_at = (
            session.query(func.max(OncallRemediationProposal.executing_at))
            .filter(
                OncallRemediationProposal.lifecycle_id == action.lifecycle_id,
                OncallRemediationProposal.executing_at.is_not(None),
                OncallRemediationProposal.id != exclude_proposal_id,
                _REAL_EXECUTION_PREDICATE,
            )
            .scalar()
        )
        if last_executing_at is not None:
            since = now - _naive_utc(last_executing_at)
            if since < timedelta(minutes=config.cooldown_minutes):
                return _GateAResult(ok=False, reason="cooldown", check="A11", scope=scope, plan=plan)

        day_start, day_end = _beijing_day_bounds_utc(now)
        executed_today = (
            session.query(func.count(OncallRemediationProposal.id))
            .filter(
                OncallRemediationProposal.executing_at.is_not(None),
                OncallRemediationProposal.executing_at >= day_start,
                OncallRemediationProposal.executing_at < day_end,
                OncallRemediationProposal.id != exclude_proposal_id,
                _REAL_EXECUTION_PREDICATE,
            )
            .scalar()
            or 0
        )
        if executed_today >= config.daily_execution_cap:
            return _GateAResult(ok=False, reason="daily_execution_cap", check="A11", scope=scope, plan=plan)

        proposed_today = (
            session.query(func.count(OncallRemediationProposal.id))
            .filter(
                OncallRemediationProposal.proposed_at.is_not(None),
                OncallRemediationProposal.proposed_at >= day_start,
                OncallRemediationProposal.proposed_at < day_end,
                OncallRemediationProposal.id != exclude_proposal_id,
            )
            .scalar()
            or 0
        )
        if proposed_today >= config.daily_proposal_cap:
            return _GateAResult(ok=False, reason="daily_proposal_cap", check="A11", scope=scope, plan=plan)

    return _GateAResult(
        ok=True,
        action=action,
        plan=plan,
        scope=scope,
        posted_at=posted_at,
        window_minutes=window_minutes,
        raw_message=raw_message_snapshot,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_proposal_request(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    case_key: str,
    case_no: int,
    raw_message_id: int,
    now: datetime,
) -> RegisterResult:
    """Endpoint-side registration: writes one row, computes nothing.

    Idempotent per ``raw_message_id``: an existing non-terminal proposal is
    returned as-is; an existing ``succeeded`` one causes a fresh row that is
    immediately ``refused: already_remediated`` (spec 4.3/6.3).
    """

    now = _naive_utc(now)
    with session_factory() as session:
        existing = (
            session.query(OncallRemediationProposal)
            .filter(
                OncallRemediationProposal.raw_message_id == raw_message_id,
                OncallRemediationProposal.state.in_(tuple(NON_TERMINAL_PROPOSAL_STATES)),
            )
            .order_by(OncallRemediationProposal.id.desc())
            .first()
        )
        if existing is not None:
            return RegisterResult(proposal_id=existing.id, state=existing.state, created=False)

        already_succeeded = (
            session.query(OncallRemediationProposal.id)
            .filter(
                OncallRemediationProposal.raw_message_id == raw_message_id,
                OncallRemediationProposal.state == "succeeded",
            )
            .first()
            is not None
        )
        disabled = config.effective_mode == "off" or not config.token
        if already_succeeded:
            state, reason = "refused", "already_remediated"
        elif disabled:
            state, reason = "refused", "remediation_disabled"
        else:
            state, reason = "requested", None

        proposal = OncallRemediationProposal(
            case_key=str(case_key)[:64],
            case_no=int(case_no),
            raw_message_id=int(raw_message_id),
            state=state,
            refusal_reason=reason,
            requested_at=now,
            finished_at=now if state == "refused" else None,
        )
        session.add(proposal)
        session.flush()
        _append_event(
            session,
            proposal.id,
            actor="oncall_request",
            event="register",
            outcome=state,
            detail={"case_key": case_key, "case_no": case_no, "reason": reason},
            at=now,
        )
        session.commit()
        return RegisterResult(proposal_id=proposal.id, state=state, created=True)


def compute_requested_proposal(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    proposal_id: int,
    deepcoin_client,
    group_config,
    now: datetime,
    resolve_scope: Callable[..., RemediationScope | None] = resolve_remediation_scope,
    build_plan: Callable[..., PositionRemediationPlan] = build_position_management_remediation_plan,
    group_label: Callable[[int], str] | None = None,
    auto_health: AutoHealthInputs | None = None,
) -> ProposalOutcome:
    """G-A: the full deterministic proposal gate. Called from a background task."""

    now = _naive_utc(now)
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"proposal {proposal_id} not found")
        if proposal.state != "requested":
            return ProposalOutcome(
                proposal_id=proposal_id,
                state=proposal.state,
                refusal_reason=proposal.refusal_reason,
                text=None,
                keyboard=None,
                should_send=False,
            )
        case_no = proposal.case_no
        raw_message_id = proposal.raw_message_id

    def _refuse(reason: str, *, check: str | None) -> ProposalOutcome:
        with session_factory() as session:
            result = session.execute(
                update(OncallRemediationProposal)
                .where(
                    OncallRemediationProposal.id == proposal_id,
                    OncallRemediationProposal.state == "requested",
                )
                .values(
                    state="refused",
                    refusal_reason=reason[:128],
                    finished_at=now,
                    updated_at=now,
                )
            )
            _append_event(
                session,
                proposal_id,
                actor="worker",
                event="gate_a",
                gate="A",
                check=check,
                outcome="refused",
                detail={"reason": reason},
                at=now,
            )
            session.commit()
            became_refused = result.rowcount == 1
            # Spec 6.1: refusal lines count against the same worker-side cap of
            # 30 proposal-type messages per Beijing day; beyond it they are
            # recorded only.
            under_cap = _proposal_messages_today(session, now=now) <= config.daily_proposal_cap
        return ProposalOutcome(
            proposal_id=proposal_id,
            state="refused" if became_refused else "requested",
            refusal_reason=reason,
            text=_format_refusal_text(case_no=case_no, reason=reason) if became_refused else None,
            keyboard=None,
            should_send=became_refused and under_cap,
        )

    try:
        with session_factory() as session:
            control = _get_or_create_control(session)
            control_enabled = bool(control.enabled)
            session.commit()
        if not control_enabled:
            return _refuse("remediation_disabled", check="A12")

        gate = _run_gate_a(
            session_factory,
            config=config,
            raw_message_id=raw_message_id,
            deepcoin_client=deepcoin_client,
            group_config=group_config,
            now=now,
            resolve_scope=resolve_scope,
            build_plan=build_plan,
            exclude_proposal_id=proposal_id,
        )
        if not gate.ok:
            return _refuse(gate.reason or "internal_error:gate_a", check=gate.check)

        action = gate.action
        assert action is not None and gate.scope is not None

        # Phase 4 (spec section 5): in ``auto`` mode, G-D runs right here,
        # before anything is written, using the same action/gate context G-A
        # just produced. A pass tries to CAS straight to "executing" (no
        # human step at all); a miss downgrades to an ordinary proposed
        # message with an approve-mode button attached (user ruling 2026-09-27
        # item 4), one line explaining why it was not auto-executed.
        gate_d: GateDOutcome | None = None
        if config.effective_mode == "auto":
            settings = load_trading_settings(session_factory)
            gate_d = run_gate_d(
                session_factory,
                config=config,
                action=action,
                posted_at=gate.posted_at or now,
                now=now,
                deepcoin_client=deepcoin_client,
                settings=settings,
                health=auto_health or AutoHealthInputs(process_started_at=now),
                chat_id=gate.raw_message.chat_id if gate.raw_message is not None else None,
                exclude_proposal_id=proposal_id,
            )
            with session_factory() as session:
                for check in gate_d.checks:
                    _append_event(
                        session,
                        proposal_id,
                        actor="worker",
                        event="gate_d",
                        gate="D",
                        check=check.check,
                        outcome="passed" if check.passed else "refused",
                        detail={"reason": check.reason_code} if check.reason_code else None,
                        at=now,
                    )
                session.commit()
            _note_auto_internal_error(
                session_factory,
                occurred=any(check.reason_code == "d_internal_error" for check in gate_d.checks),
                now=now,
            )

        snapshot = _build_action_snapshot(action)
        token1: str | None = None
        promote_to_executing = bool(gate_d is not None and gate_d.passed)
        with session_factory() as session:
            row = session.get(OncallRemediationProposal, proposal_id)
            if row is None or row.state != "requested":
                return ProposalOutcome(
                    proposal_id=proposal_id,
                    state=row.state if row is not None else "unknown",
                    refusal_reason="state_changed",
                    text=None,
                    keyboard=None,
                    should_send=False,
                )
            row.lifecycle_id = action.lifecycle_id
            row.action_kind = action.action_kind
            row.action_id = action.action_id
            row.action_fingerprint = action.fingerprint
            row.action_snapshot_json = _bounded_json(snapshot, limit=8192)
            row.scope_json = gate.scope.to_json()
            row.proposed_at = now
            row.expires_at = now + timedelta(minutes=config.proposal_expiry_minutes)
            row.updated_at = now
            if gate_d is not None:
                row.auto_gate_result = "passed" if gate_d.passed else (gate_d.first_failure_reason or "unknown")
            # Always land in "proposed" first; only an atomic promotion below
            # can turn it into an automatic execution.
            row.state = "proposed"
            session.add(row)
            session.flush()
            promoted = False
            if promote_to_executing:
                # Single statement: the "no other proposal executing" check and
                # the promotion are atomic under SQLite's single writer (C1,
                # same construction as phase 3's step-2 CAS). Only a promoted
                # row is marked execution_origin='auto'; a downgraded one stays
                # manual so an approver's button click is not re-refused by the
                # same G-D reason at execution time (user ruling item 4).
                other = aliased(OncallRemediationProposal)
                result = session.execute(
                    update(OncallRemediationProposal)
                    .where(
                        OncallRemediationProposal.id == proposal_id,
                        OncallRemediationProposal.state == "proposed",
                        ~exists().where(other.state == "executing", other.id != proposal_id),
                    )
                    .values(
                        state="executing",
                        execution_origin="auto",
                        approved_at=now,
                        confirmed_at=now,
                        executing_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                promoted = result.rowcount == 1
                session.refresh(row)
                if not promoted:
                    row.auto_gate_result = "auto_single_flight_busy"
            if not promoted and config.effective_mode in {"approve", "auto"}:
                token1 = _new_token()
                row.step1_token_hash = _hash_token(token1)
            session.add(row)
            _append_event(
                session,
                proposal_id,
                actor="worker",
                event="gate_a",
                gate="A",
                outcome=row.state,
                detail={"action_kind": action.action_kind, "action_id": action.action_id},
                at=now,
            )
            session.commit()
            expires_at = row.expires_at
            final_state = row.state
            auto_gate_result = row.auto_gate_result

        if final_state == "executing":
            return ProposalOutcome(
                proposal_id=proposal_id,
                state="executing",
                refusal_reason=None,
                text=None,
                keyboard=None,
                should_send=False,
                auto_execute_proposal_id=proposal_id,
            )

        text = _format_proposal_text(
            proposal_id=proposal_id,
            case_no=case_no,
            raw_message_id=raw_message_id,
            expires_at=expires_at,
            action=action,
            shadow=config.effective_mode not in {"approve", "auto"},
            group_label=(
                group_label(gate.raw_message.chat_id)
                if group_label and gate.raw_message is not None
                else "群组"
            ),
            window_minutes=gate.window_minutes,
            posted_at=gate.posted_at or now,
            auto_downgrade_reason=auto_gate_result if gate_d is not None else None,
        )
        keyboard = None
        if config.effective_mode in {"approve", "auto"} and token1 is not None:
            keyboard = (
                ("✅ 执行补救", f"orm:{proposal_id}:1:{token1}"),
                ("❌ 忽略", f"orm:{proposal_id}:d:{token1}"),
            )
        return ProposalOutcome(
            proposal_id=proposal_id,
            state="proposed",
            refusal_reason=None,
            text=text,
            keyboard=keyboard,
            should_send=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed by design
        return _refuse(f"internal_error:{type(exc).__name__}", check=None)


def record_proposal_message(
    session_factory: sessionmaker,
    *,
    proposal_id: int,
    telegram_message_id: int,
) -> None:
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        if row is None:
            return
        row.telegram_message_id = telegram_message_id
        session.add(row)
        session.commit()


_CALLBACK_RE = re.compile(r"^orm:(\d+):(1|d|2|c):([A-Za-z0-9_-]+)$")


def handle_callback(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    chat_id: int | str,
    from_user_id: int,
    data: str,
    now: datetime,
) -> CallbackOutcome:
    """G-B: button clicks. ``data`` is the raw Telegram ``callback_data``."""

    now = _naive_utc(now)
    if len(str(data).encode("utf-8")) > 64:
        return CallbackOutcome(proposal_id=None, accepted=False, text=None, keyboard=None)
    match = _CALLBACK_RE.match(str(data))
    if match is None:
        return CallbackOutcome(proposal_id=None, accepted=False, text=None, keyboard=None)
    proposal_id = int(match.group(1))
    step = match.group(2)
    token = match.group(3)

    if config.effective_mode not in {"approve", "auto"}:
        return CallbackOutcome(
            proposal_id=proposal_id,
            accepted=False,
            text="当前为只提示模式 / 补救未启用",
            keyboard=None,
        )

    authorized = (
        bool(config.approver_ids)
        and str(chat_id) == config.system_chat_id
        and int(from_user_id) in config.approver_ids
    )
    if not authorized:
        with session_factory() as session:
            _append_event(
                session,
                proposal_id,
                actor=f"telegram_user:{from_user_id}",
                event="callback",
                gate="B",
                check="B1",
                outcome="refused",
                detail={"chat_id": str(chat_id), "step": step},
                at=now,
            )
            session.commit()
        return CallbackOutcome(proposal_id=proposal_id, accepted=False, text="无权限", keyboard=None)

    actor = f"telegram_user:{from_user_id}"

    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        if proposal is None:
            return CallbackOutcome(proposal_id=proposal_id, accepted=False, text="提案不存在", keyboard=None)

        if step == "1":
            return _handle_step1(session, proposal, config=config, token=token, now=now, actor=actor)
        if step == "d":
            return _handle_dismiss(session, proposal, token=token, now=now, actor=actor)
        if step == "c":
            return _handle_cancel(session, proposal, token=token, now=now, actor=actor)
        if step == "2":
            return _handle_step2(session, proposal, token=token, now=now, actor=actor)
    return CallbackOutcome(proposal_id=proposal_id, accepted=False, text=None, keyboard=None)


def _handle_step1(session, proposal, *, config, token, now, actor) -> CallbackOutcome:
    if proposal.state != "proposed":
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    if not proposal.step1_token_hash or not hmac.compare_digest(
        _hash_token(token), proposal.step1_token_hash
    ):
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B2", outcome="refused", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "令牌无效", None)
    if proposal.expires_at is not None and now > _naive_utc(proposal.expires_at):
        proposal.state = "expired"
        proposal.finished_at = now
        proposal.updated_at = now
        session.add(proposal)
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B4", outcome="expired", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "提案已过期", None, remove_keyboard=True)

    raw_message = session.get(RawMessage, proposal.raw_message_id)
    window_minutes = _window_minutes_for(proposal.action_kind, config)
    posted_at = _naive_utc(raw_message.posted_at) if raw_message and raw_message.posted_at else None
    if posted_at is None or (now - posted_at).total_seconds() / 60.0 > window_minutes:
        proposal.state = "expired"
        proposal.finished_at = now
        proposal.updated_at = now
        session.add(proposal)
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="A8", outcome="expired", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "补救窗口已过期", None, remove_keyboard=True)

    result = session.execute(
        update(OncallRemediationProposal)
        .where(OncallRemediationProposal.id == proposal.id, OncallRemediationProposal.state == "proposed")
        .values(
            state="confirming",
            approver_user_id=int(actor.split(":")[1]),
            approved_at=now,
            step1_token_hash=None,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        session.commit()
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    token2 = _new_token()
    row = session.get(OncallRemediationProposal, proposal.id)
    row.step2_token_hash = _hash_token(token2)
    session.add(row)
    try:
        snapshot = json.loads(row.action_snapshot_json or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    action_kind = snapshot.get("action_kind") or proposal.action_kind or ""
    expected_effect = snapshot.get("expected_effect") or {}
    fake_action = PositionRemediationAction(
        action_id=str(snapshot.get("action_id") or ""),
        action_kind=action_kind,
        raw_message_id=proposal.raw_message_id,
        lifecycle_id=proposal.lifecycle_id or 0,
        strategy_instance_id=str(snapshot.get("strategy_instance_id") or ""),
        pos_ids=tuple(snapshot.get("pos_ids") or ()),
        expected_effect=expected_effect,
        evidence={
            "instrument_scope": snapshot.get("instrument_scope") or [],
            "positions": snapshot.get("positions") or [],
        },
        fingerprint=str(snapshot.get("fingerprint") or ""),
    )
    text = _format_confirm_text(action=fake_action, confirm_expiry_minutes=6)
    _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B3", outcome="confirming", at=now)
    session.commit()
    keyboard = (
        ("确认执行", f"orm:{proposal.id}:2:{token2}"),
        ("取消", f"orm:{proposal.id}:c:{token2}"),
    )
    return CallbackOutcome(proposal.id, True, text, keyboard)


def _handle_dismiss(session, proposal, *, token, now, actor) -> CallbackOutcome:
    if proposal.state != "proposed":
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    if not proposal.step1_token_hash or not hmac.compare_digest(
        _hash_token(token), proposal.step1_token_hash
    ):
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B2", outcome="refused", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "令牌无效", None)
    result = session.execute(
        update(OncallRemediationProposal)
        .where(OncallRemediationProposal.id == proposal.id, OncallRemediationProposal.state == "proposed")
        .values(state="dismissed", step1_token_hash=None, finished_at=now, updated_at=now)
    )
    if result.rowcount != 1:
        session.commit()
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B3", outcome="dismissed", at=now)
    session.commit()
    return CallbackOutcome(proposal.id, True, "已忽略", None, remove_keyboard=True)


def _handle_cancel(session, proposal, *, token, now, actor) -> CallbackOutcome:
    if proposal.state != "confirming":
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    if not proposal.step2_token_hash or not hmac.compare_digest(
        _hash_token(token), proposal.step2_token_hash
    ):
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B2", outcome="refused", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "令牌无效", None)
    result = session.execute(
        update(OncallRemediationProposal)
        .where(OncallRemediationProposal.id == proposal.id, OncallRemediationProposal.state == "confirming")
        .values(state="cancelled", step2_token_hash=None, finished_at=now, updated_at=now)
    )
    _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B3", outcome="cancelled", at=now)
    session.commit()
    if result.rowcount != 1:
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    return CallbackOutcome(proposal.id, True, "已取消", None, remove_keyboard=True)


def _handle_step2(session, proposal, *, token, now, actor) -> CallbackOutcome:
    if proposal.state != "confirming":
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    if not proposal.step2_token_hash or not hmac.compare_digest(
        _hash_token(token), proposal.step2_token_hash
    ):
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B2", outcome="refused", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "令牌无效", None)
    if proposal.approved_at is not None and now - _naive_utc(proposal.approved_at) > timedelta(minutes=2):
        result = session.execute(
            update(OncallRemediationProposal)
            .where(OncallRemediationProposal.id == proposal.id, OncallRemediationProposal.state == "confirming")
            .values(state="expired", step2_token_hash=None, finished_at=now, updated_at=now)
        )
        _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B4", outcome="expired", at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "确认已过期", None, remove_keyboard=True)

    other_executing = (
        session.query(func.count(OncallRemediationProposal.id))
        .filter(OncallRemediationProposal.state == "executing", OncallRemediationProposal.id != proposal.id)
        .scalar()
        or 0
    )
    if other_executing > 0:
        _append_event(session, proposal.id, actor=actor, event="callback", gate="C", check="C1", outcome="refused", detail={"reason": "busy"}, at=now)
        session.commit()
        return CallbackOutcome(proposal.id, False, "另一笔补救正在执行，请稍后再试", None)

    # One statement, so the "nobody else is executing" check and the promotion
    # are atomic under SQLite's single writer (C1, library half; the worker
    # adds an in-process asyncio.Lock on top).
    other = aliased(OncallRemediationProposal)
    result = session.execute(
        update(OncallRemediationProposal)
        .where(
            OncallRemediationProposal.id == proposal.id,
            OncallRemediationProposal.state == "confirming",
            ~exists().where(other.state == "executing", other.id != proposal.id),
        )
        .values(state="executing", confirmed_at=now, executing_at=now, step2_token_hash=None, updated_at=now)
    )
    if result.rowcount != 1:
        still_confirming = (
            session.query(OncallRemediationProposal.state)
            .filter(OncallRemediationProposal.id == proposal.id)
            .scalar()
            == "confirming"
        )
        _append_event(
            session, proposal.id, actor=actor, event="callback", gate="C" if still_confirming else "B",
            check="C1" if still_confirming else "B3", outcome="refused",
            detail={"reason": "busy" if still_confirming else "state_changed"}, at=now,
        )
        session.commit()
        if still_confirming:
            return CallbackOutcome(proposal.id, False, "另一笔补救正在执行，请稍后再试", None)
        return CallbackOutcome(proposal.id, False, "这条提案已处理 / 已过期", None)
    _append_event(session, proposal.id, actor=actor, event="callback", gate="B", check="B3", outcome="executing", at=now)
    session.commit()
    return CallbackOutcome(proposal.id, True, None, None, execute_proposal_id=proposal.id)


def handle_text_command(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    chat_id: int | str,
    from_user_id: int,
    text: str,
    now: datetime,
) -> CommandOutcome:
    now = _naive_utc(now)
    authorized = (
        bool(config.approver_ids)
        and str(chat_id) == config.system_chat_id
        and int(from_user_id) in config.approver_ids
    )
    if not authorized:
        return CommandOutcome(accepted=False, text="无权限")

    stripped = str(text or "").strip()
    parts = stripped.split()

    if parts and parts[0] == "/oncall_off":
        if len(parts) != 1:
            return CommandOutcome(accepted=False, text="命令格式错误")
        actor = f"telegram_user:{from_user_id}"
        with session_factory() as session:
            control = _get_or_create_control(session)
            control.enabled = False
            control.changed_at = now
            control.changed_by = actor
            control.reason = "manual_off"
            session.add(control)
            to_cancel = (
                session.query(OncallRemediationProposal.id)
                .filter(OncallRemediationProposal.state.in_(("proposed", "confirming")))
                .all()
            )
            cancel_ids = [int(row[0]) for row in to_cancel]
            if cancel_ids:
                session.execute(
                    update(OncallRemediationProposal)
                    .where(OncallRemediationProposal.id.in_(cancel_ids))
                    .values(state="cancelled", finished_at=now, updated_at=now)
                )
            for proposal_id in cancel_ids:
                _append_event(
                    session,
                    proposal_id,
                    actor=actor,
                    event="control",
                    outcome="cancelled",
                    detail={"reason": "oncall_off"},
                    at=now,
                )
            _append_event(session, None, actor=actor, event="control", outcome="disabled", detail={"cancelled": len(cancel_ids)}, at=now)
            session.commit()
        return CommandOutcome(accepted=True, text=f"补救已关闭（作废 {len(cancel_ids)} 条在途提案）")

    if parts and parts[0] == "/oncall_on":
        if len(parts) != 1:
            return CommandOutcome(accepted=False, text="命令格式错误")
        actor = f"telegram_user:{from_user_id}"
        with session_factory() as session:
            control = _get_or_create_control(session)
            control.enabled = True
            control.changed_at = now
            control.changed_by = actor
            control.reason = "manual_on"
            control.consecutive_failures = 0
            control.breaker_tripped_at = None
            session.add(control)
            _append_event(session, None, actor=actor, event="control", outcome="enabled", detail={"reason": "oncall_on"}, at=now)
            session.commit()
        return CommandOutcome(accepted=True, text="补救已开启")

    if parts and parts[0] == "/auto_off":
        if len(parts) != 1:
            return CommandOutcome(accepted=False, text="命令格式错误")
        actor = f"telegram_user:{from_user_id}"
        with session_factory() as session:
            control = _get_or_create_control(session)
            control.auto_suspended = True
            control.auto_suspended_at = now
            control.auto_suspend_reason = "manual_off"
            control.auto_consecutive_errors = 0
            session.add(control)
            _append_event(session, None, actor=actor, event="control", outcome="auto_suspended", detail={"reason": "manual_off"}, at=now)
            session.commit()
        return CommandOutcome(accepted=True, text="自动补救已暂停，改为只提示（其余照旧）")

    if parts and parts[0] == "/auto_on":
        if len(parts) != 1:
            return CommandOutcome(accepted=False, text="命令格式错误")
        actor = f"telegram_user:{from_user_id}"
        with session_factory() as session:
            control = _get_or_create_control(session)
            control.auto_suspended = False
            control.auto_suspended_at = None
            control.auto_suspend_reason = None
            control.auto_consecutive_errors = 0
            session.add(control)
            _append_event(session, None, actor=actor, event="control", outcome="auto_resumed", detail={"reason": "manual_on"}, at=now)
            session.commit()
        return CommandOutcome(accepted=True, text="自动补救已恢复")

    if parts and parts[0] == "/audit":
        if len(parts) != 2 or not re.fullmatch(r"P\d+", parts[1]):
            return CommandOutcome(accepted=False, text="命令格式错误，应为 /audit P<提案号>")
        proposal_id = int(parts[1][1:])
        return CommandOutcome(
            accepted=True,
            text=_render_audit_report(session_factory, proposal_id=proposal_id),
            proposal_id=proposal_id,
        )

    if parts and parts[0] == "/fix":
        if len(parts) != 2 or not re.fullmatch(r"P\d+", parts[1]):
            return CommandOutcome(accepted=False, text="命令格式错误，应为 /fix P<提案号>")
        proposal_id = int(parts[1][1:])
        if config.effective_mode not in {"approve", "auto"}:
            return CommandOutcome(accepted=False, text="当前为只提示模式", proposal_id=proposal_id)
        with session_factory() as session:
            proposal = session.get(OncallRemediationProposal, proposal_id)
            regenerate = (
                proposal is not None
                and proposal.state == "failed"
                and proposal.refusal_reason == "plan_changed"
                and proposal.management_batch_id is None
            )
            if regenerate:
                case_key, case_no, raw_message_id = (
                    proposal.case_key, proposal.case_no, proposal.raw_message_id,
                )
        if regenerate:
            # User ruling 2026-09-26: plan drift only refuses this proposal and
            # asks for a fresh one. A new request goes through every gate again
            # (G-A, then G-B twice, then G-C); nothing from the old proposal is
            # reused except the identifiers the watcher originally sent.
            registered = register_proposal_request(
                session_factory, config=config, case_key=case_key, case_no=case_no,
                raw_message_id=raw_message_id, now=now,
            )
            with session_factory() as session:
                _append_event(
                    session, proposal_id, actor=f"telegram_user:{from_user_id}", event="regenerate",
                    outcome=registered.state, detail={"new_proposal_id": registered.proposal_id}, at=now,
                )
                session.commit()
            if registered.state == "refused":
                return CommandOutcome(
                    accepted=False,
                    text="无法重新生成：补救未启用，或该消息已补救过",
                    proposal_id=registered.proposal_id,
                )
            return CommandOutcome(
                accepted=True,
                text=f"已重新请求提案 P{registered.proposal_id}，稍后会收到新的提案消息",
                proposal_id=registered.proposal_id,
            )
        with session_factory() as session:
            proposal = session.get(OncallRemediationProposal, proposal_id)
            if proposal is None or proposal.state != "proposed":
                return CommandOutcome(accepted=False, text="这条提案已处理 / 已过期", proposal_id=proposal_id)
            if not proposal.step1_token_hash:
                token1 = _new_token()
                proposal.step1_token_hash = _hash_token(token1)
                session.add(proposal)
                session.commit()
            else:
                return CommandOutcome(accepted=False, text="请使用提案消息上的按钮", proposal_id=proposal_id)
        keyboard = (
            ("✅ 执行补救", f"orm:{proposal_id}:1:{token1}"),
            ("❌ 忽略", f"orm:{proposal_id}:d:{token1}"),
        )
        return CommandOutcome(accepted=True, text=f"提案 P{proposal_id}", proposal_id=proposal_id, keyboard=keyboard)

    return CommandOutcome(accepted=False, text="未知命令")


def execute_proposal(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    proposal_id: int,
    deepcoin_client=None,
    group_config,
    now: datetime,
    deepcoin_client_factory: Callable[[], Any] | None = None,
    apply_fn: Callable[..., Any] = apply_position_management_remediation_action,
    build_plan: Callable[..., PositionRemediationPlan] = build_position_management_remediation_plan,
    resolve_scope: Callable[..., RemediationScope | None] = resolve_remediation_scope,
    auto_health: AutoHealthInputs | None = None,
) -> ExecutionOutcome:
    """G-C: rerun every gate, then (and only then) call ``apply_fn``."""

    now = _naive_utc(now)
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        if proposal is None or proposal.state != "executing":
            raise ValueError("proposal is not in the executing state")
        raw_message_id = proposal.raw_message_id
        stored_action_id = proposal.action_id
        stored_fingerprint = proposal.action_fingerprint
        scope_json = proposal.scope_json
        execution_origin = proposal.execution_origin
        case_no = proposal.case_no

    def _fail(state: str, reason: str, *, check: str | None) -> ExecutionOutcome:
        # User ruling 2026-09-26: the breaker counts only failures of a real
        # execution. A refusal before anything was promoted to a live batch
        # (plan_changed and every other pre-apply gate, or an apply() refusal
        # that never promoted its plan-only batch) only refuses this proposal.
        # "uncertain" always means a live write may have happened, so it counts.
        # Phase 4: an auto proposal's pre-apply refusal (including a G-D
        # rerun miss, "auto_gate_changed:*") is exactly this same category --
        # it never suspends auto-execution (spec section 8: only a *real*
        # execution failure/uncertain does that).
        counts_toward_breaker = state == "uncertain"
        with session_factory() as session:
            row = session.get(OncallRemediationProposal, proposal_id)
            row.state = state
            row.refusal_reason = reason[:128]
            row.finished_at = now
            row.updated_at = now
            row.result_json = _bounded_json({"reason": reason}, limit=4096)
            session.add(row)
            _append_event(
                session, proposal_id, actor="worker", event="gate_c", gate="C", check=check,
                outcome=state, detail={"reason": reason, "counts_toward_breaker": counts_toward_breaker}, at=now,
            )
            breaker_message = (
                _apply_outcome_to_breaker(session, state) if counts_toward_breaker else None
            )
            if counts_toward_breaker and row.execution_origin == "auto":
                auto_message = _suspend_auto(session, reason=f"real_execution_{state}")
                if auto_message:
                    breaker_message = f"{breaker_message}\n{auto_message}" if breaker_message else auto_message
            session.commit()
        return ExecutionOutcome(
            proposal_id=proposal_id,
            state=state,
            management_batch_id=None,
            text=_format_result_text(proposal_id=proposal_id, state=state, detail=_refusal_text_zh(reason), breaker_message=breaker_message),
            breaker_tripped=breaker_message is not None,
        )

    # Anything that goes wrong before apply_fn is entered never reached the
    # exchange-write path, so it is "failed"; only an exception after that
    # point is "uncertain" (spec 8.1).
    apply_started = False
    try:
        if deepcoin_client is None:
            if deepcoin_client_factory is None:
                return _fail("failed", "internal_error:no_exchange_client", check=None)
            deepcoin_client = deepcoin_client_factory()
        gate = _run_gate_a(
            session_factory,
            config=config,
            raw_message_id=raw_message_id,
            deepcoin_client=deepcoin_client,
            group_config=group_config,
            now=now,
            resolve_scope=resolve_scope,
            build_plan=build_plan,
            exclude_proposal_id=proposal_id,
        )
        if not gate.ok:
            return _fail("failed", gate.reason or "internal_error:gate_a", check=gate.check)

        action = gate.action
        assert action is not None
        if action.action_id != stored_action_id or action.fingerprint != stored_fingerprint:
            return _fail("failed", "plan_changed", check="C2")

        # Phase 4 (spec section 4/C's "重跑 G-D"): an auto-originated proposal
        # gets D1/D2/D3/D4/D6/D7 rerun here too (D8 excludes itself, D9/D0 are
        # config/control facts that cannot have changed mid-flight). Any miss
        # refuses this execution -- it never reached apply_fn, so it is
        # "failed", never "uncertain", and per the rule above never suspends
        # auto-execution.
        if execution_origin == "auto":
            settings_for_d = load_trading_settings(session_factory)
            gate_d = run_gate_d(
                session_factory,
                config=config,
                action=action,
                posted_at=gate.posted_at or now,
                now=now,
                deepcoin_client=deepcoin_client,
                settings=settings_for_d,
                health=auto_health or AutoHealthInputs(process_started_at=now - timedelta(hours=1)),
                chat_id=gate.raw_message.chat_id if gate.raw_message is not None else None,
                exclude_proposal_id=proposal_id,
            )
            _note_auto_internal_error(
                session_factory,
                occurred=any(check.reason_code == "d_internal_error" for check in gate_d.checks),
                now=now,
            )
            if not gate_d.passed:
                return _fail(
                    "failed", f"auto_gate_changed:{gate_d.first_failure_reason}", check="D"
                )

        scope = RemediationScope.from_json(scope_json) if scope_json else gate.scope

        with session_factory() as session:
            _append_event(session, proposal_id, actor="worker", event="gate_c", gate="C", check="C4", outcome="applying", at=now)
            _append_audit(
                session,
                proposal_id,
                phase="pre_apply",
                payload={
                    "trigger": {"case_no": case_no, "raw_message_id": raw_message_id},
                    "target": {
                        "lifecycle_id": action.lifecycle_id,
                        "strategy_instance_id": action.strategy_instance_id,
                        "pos_ids": list(action.pos_ids),
                    },
                    "params": {
                        "action_kind": action.action_kind,
                        "action_id": action.action_id,
                        "fingerprint": action.fingerprint,
                        "expected_effect": action.expected_effect,
                        "execution_origin": execution_origin,
                    },
                },
            )
            session.commit()

        apply_started = True
        try:
            result = apply_fn(
                session_factory,
                deepcoin_client=deepcoin_client,
                action_id=action.action_id,
                expected_fingerprint=action.fingerprint,
                now=now,
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001 - classify below
            classified = _classify_apply_exception(
                session_factory, raw_message_id=raw_message_id, executing_at=now
            )
            return _fail(classified, f"apply_error:{type(exc).__name__}", check="C4")

        batch_id = getattr(result, "batch_id", None)
        with session_factory() as session:
            row = session.get(OncallRemediationProposal, proposal_id)
            row.management_batch_id = batch_id
            row.updated_at = now
            session.add(row)
            _append_event(
                session,
                proposal_id,
                actor="worker",
                event="apply",
                gate="C",
                outcome="submitted",
                detail={"batch_id": batch_id, "status": getattr(result, "status", None)},
                at=now,
            )
            session.commit()
        # execute_management_batch's own docstring: "exchange truth closes
        # positions later" -- the proposal stays "executing" and is finalized
        # by finalize_executing_proposals once the batch itself settles.
        return ExecutionOutcome(proposal_id=proposal_id, state="executing", management_batch_id=batch_id, text=None)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return _fail(
            "uncertain" if apply_started else "failed",
            f"internal_error:{type(exc).__name__}",
            check=None,
        )


def _classify_apply_exception(
    session_factory: sessionmaker, *, raw_message_id: int, executing_at: datetime
) -> str:
    """failed if no live batch was ever produced for this message since the
    execution started; uncertain if one exists (it may have been submitted)."""

    with session_factory() as session:
        row = (
            session.query(StrategyManagementBatch.id)
            .filter(
                StrategyManagementBatch.raw_message_id == raw_message_id,
                StrategyManagementBatch.execution_mode == "live",
                StrategyManagementBatch.updated_at >= executing_at,
            )
            .first()
        )
        return "uncertain" if row is not None else "failed"


_BATCH_SUCCESS_STATUSES = frozenset({"succeeded", "resolved"})
_BATCH_FAILURE_STATUSES = frozenset({"blocked"})
_BATCH_AMBIGUOUS_STATUSES = frozenset({"partial_failed", "submit_unknown", "recovery_required"})
_BATCH_IN_FLIGHT_STATUSES = frozenset(
    {"ready", "executing", "reserved", "submitted", "reconciling", "protection_ready"}
)


def _price_close(actual: Any, expected: float, *, rel_tol: float = 1e-4, abs_tol: float = 1e-6) -> bool:
    try:
        return math.isclose(float(actual), float(expected), rel_tol=rel_tol, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return False


def _min_quantity_for_instrument(session, *, strategy_instance_id: str | None) -> float | None:
    """Best-effort contract-spec ``min_quantity`` for a partial-take-profit
    readback tolerance (spec batch-2 item A "容差取该合约最小下单数量").

    The only place a remediated binding's contract spec survives is the
    frozen order draft in ``ExecutionBinding.payload_json`` (see
    ``tests/oncall_remediation_fixtures.py``'s ``build_ready_remediation_target``
    docstring: "resolve_existing_position_contract_spec...tries this before
    ever consulting contract_spec_provider"). No live provider call is made
    here -- this is a read-only audit comparison, not an order-sizing
    decision. Returns ``None`` (falls back to the relative-tolerance rule)
    when the draft or its contract spec is absent/malformed.
    """

    if not strategy_instance_id:
        return None
    binding = (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.strategy_instance_id == str(strategy_instance_id))
        .filter(ExecutionBinding.venue == "deepcoin")
        .first()
    )
    if binding is None or not binding.payload_json:
        return None
    try:
        payload = json.loads(binding.payload_json)
        value = (payload.get("draft") or {}).get("contract_spec", {}).get("min_quantity")
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
        return None


def _perform_post_execution_readback(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    action_kind: str | None,
    expected_effect: dict[str, Any],
    pos_ids: tuple[str, ...],
    pre_execution_positions: list[dict[str, Any]],
    scope: RemediationScope | None,
    strategy_instance_id: str | None,
) -> ReadbackResult:
    """Read a fresh exchange snapshot and check it against the action's
    expected effect (spec section 4 tail / section 8's "回读不符 ->
    uncertain"). Every branch below reads only the same snapshot this call
    fetches once -- never the exchange calls the batch itself already made
    (those already live in ``_gather_exchange_traffic_for_audit``).
    """

    if scope is None or not scope.instruments or not pos_ids:
        return ReadbackResult("unknown", {"reason": "no_scope_for_readback"})
    try:
        snapshot = _load_reconcile_snapshot(deepcoin_client, instruments=set(scope.instruments))
    except Exception as exc:  # noqa: BLE001 - fail-closed: a read error is "unknown", never healthy
        return ReadbackResult("unknown", {"reason": f"snapshot_error:{type(exc).__name__}"})
    if snapshot.errors:
        return ReadbackResult("unknown", {"reason": "snapshot_incomplete", "errors": dict(snapshot.errors)})

    positions_by_pos: dict[str, dict[str, Any]] = {}
    for row in snapshot.positions:
        pid = row.get("posId") or row.get("pos_id")
        if pid is not None:
            positions_by_pos[str(pid)] = row
    pending_by_pos: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.pending_trigger_orders:
        pid = row.get("posId") or row.get("pos_id")
        if pid is not None:
            pending_by_pos.setdefault(str(pid), []).append(row)

    with session_factory() as session:
        if action_kind == "full_exit":
            still_open = [pid for pid in pos_ids if str(pid) in positions_by_pos]
            if still_open:
                return ReadbackResult("mismatch", {"expected": "all_closed", "still_open": still_open})
            return ReadbackResult("confirmed", {"expected": "all_closed", "still_open": []})

        if action_kind == "partial_take_profit":
            try:
                fraction_value = float(expected_effect.get("fraction"))
            except (TypeError, ValueError):
                return ReadbackResult("unknown", {"reason": "fraction_unavailable"})
            mismatches: list[dict[str, Any]] = []
            for pid in pos_ids:
                before_row = next(
                    (r for r in pre_execution_positions if str(r.get("pos_id")) == str(pid)),
                    None,
                )
                try:
                    before_qty = float(before_row.get("size")) if before_row else None
                except (TypeError, ValueError):
                    before_qty = None
                if before_qty is None:
                    mismatches.append({"pos_id": pid, "reason": "no_pre_execution_size"})
                    continue
                expected_after = before_qty * (1.0 - fraction_value)
                after_row = positions_by_pos.get(str(pid))
                try:
                    after_qty = float(after_row.get("pos") or after_row.get("size") or 0) if after_row else 0.0
                except (TypeError, ValueError):
                    mismatches.append({"pos_id": pid, "reason": "live_size_invalid"})
                    continue
                min_qty = _min_quantity_for_instrument(session, strategy_instance_id=strategy_instance_id)
                tolerance = min_qty if min_qty else max(abs(expected_after) * 1e-6, 1e-9)
                if abs(after_qty - expected_after) > tolerance:
                    mismatches.append(
                        {
                            "pos_id": pid,
                            "expected_after": expected_after,
                            "actual_after": after_qty,
                            "tolerance": tolerance,
                        }
                    )
            if mismatches:
                return ReadbackResult("mismatch", {"mismatches": mismatches})
            return ReadbackResult("confirmed", {"fraction": fraction_value})

        if action_kind == "move_stop_to_break_even":
            # D5 (oncall_remediation_auto.predict_break_even_branch): the
            # branch is only decided inside execute_management_batch's
            # reserve_break_even_market_actions, which this repository
            # cannot replay read-only ahead of execution -- see that
            # function's docstring and status doc 9.5 known gap #1. This is
            # the first point that can observe which branch was actually
            # taken.
            entry_price = None
            if pre_execution_positions:
                try:
                    entry_price = float(pre_execution_positions[0].get("avg_entry_price"))
                except (TypeError, ValueError):
                    entry_price = None
            still_open = [pid for pid in pos_ids if str(pid) in positions_by_pos]
            if not still_open:
                return ReadbackResult("confirmed", {"branch": "market_closed"}, branch="market_closed")
            if entry_price is None:
                return ReadbackResult("unknown", {"reason": "entry_price_unavailable"})
            for pid in still_open:
                triggers = pending_by_pos.get(str(pid), [])
                matched = any(
                    _price_close(row.get("slTriggerPx"), entry_price)
                    for row in triggers
                    if row.get("slTriggerPx") not in (None, "")
                )
                if not matched:
                    return ReadbackResult(
                        "mismatch",
                        {
                            "branch": "stop_placed",
                            "pos_id": pid,
                            "entry_price": entry_price,
                            "pending_triggers": triggers,
                        },
                        branch="stop_placed",
                    )
            return ReadbackResult(
                "confirmed", {"branch": "stop_placed", "entry_price": entry_price}, branch="stop_placed"
            )

        if action_kind == "adjust_stop_loss":
            try:
                target_price = float(expected_effect.get("stop_loss"))
            except (TypeError, ValueError):
                return ReadbackResult("unknown", {"reason": "target_stop_unavailable"})
            mismatches = []
            for pid in pos_ids:
                triggers = pending_by_pos.get(str(pid), [])
                matched = any(
                    _price_close(row.get("slTriggerPx"), target_price)
                    for row in triggers
                    if row.get("slTriggerPx") not in (None, "")
                )
                if not matched:
                    mismatches.append({"pos_id": pid, "target": target_price, "pending_triggers": triggers})
            if mismatches:
                return ReadbackResult("mismatch", {"mismatches": mismatches})
            return ReadbackResult("confirmed", {"target": target_price})

    return ReadbackResult("unknown", {"reason": f"unsupported_action_kind:{action_kind}"})


def _run_post_execution_readback_for_proposal(
    session_factory: sessionmaker,
    *,
    proposal: OncallRemediationProposal,
    deepcoin_client_factory: Callable[[], Any] | None,
) -> ReadbackResult:
    """Reconstruct the action's expected effect from the proposal's own
    frozen ``action_snapshot_json``/``scope_json`` (never re-plans -- this is
    a read-only check of what already happened, not a re-derivation of what
    should happen) and run ``_perform_post_execution_readback`` against a
    fresh exchange snapshot."""

    if deepcoin_client_factory is None:
        return ReadbackResult("skipped", {"reason": "no_client_factory"})
    try:
        snapshot_data = json.loads(proposal.action_snapshot_json or "{}")
    except (TypeError, ValueError):
        snapshot_data = {}
    action_kind = snapshot_data.get("action_kind") or proposal.action_kind
    pos_ids = tuple(str(value) for value in (snapshot_data.get("pos_ids") or []))
    expected_effect = snapshot_data.get("expected_effect") or {}
    pre_positions = snapshot_data.get("positions") or []
    strategy_instance_id = snapshot_data.get("strategy_instance_id")
    scope: RemediationScope | None = None
    if proposal.scope_json:
        try:
            scope = RemediationScope.from_json(proposal.scope_json)
        except (TypeError, ValueError, KeyError):
            scope = None
    if not pos_ids or scope is None:
        return ReadbackResult("unknown", {"reason": "no_snapshot_for_readback"})
    try:
        client = deepcoin_client_factory()
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return ReadbackResult("unknown", {"reason": f"client_factory_error:{type(exc).__name__}"})
    try:
        return _perform_post_execution_readback(
            session_factory,
            deepcoin_client=client,
            action_kind=action_kind,
            expected_effect=expected_effect,
            pos_ids=pos_ids,
            pre_execution_positions=pre_positions,
            scope=scope,
            strategy_instance_id=strategy_instance_id,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _gather_exchange_traffic_for_audit(
    session,
    *,
    batch_id: int | None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Best-effort, read-only collection of this batch's exchange requests/
    responses for the audit trail (spec 6.2 "交易所往来").

    ``position_mutation_intents``/``execution_events`` carry no
    ``management_batch_id`` column (they are keyed by
    ``execution_binding_id``/``strategy_instance_id`` instead -- see
    models.py:3090-3130/3173-3203), so this joins through
    ``strategy_management_legs``' own ``execution_binding_id`` for the given
    batch and takes everything on that binding within
    ``[window_start, window_end]`` (batch 2: tightened from the phase-4
    batch-1 binding-wide collection -- see docs/codex-oncall-status.md 9.5
    "已知偏离" item 5 -- to the proposal's own
    ``executing_at``..``finished_at + 1 minute`` window, which is the
    precise relation available: neither table carries a
    ``management_batch_id``/leg id to join on exactly, and the window is
    still narrow enough to exclude another batch's traffic on a
    multiply-managed binding). Passing no window keeps the old unbounded
    behaviour (only used by call sites that have no proposal timing yet).
    """

    from telegram_kol_research.models import (
        ExecutionEvent,
        ExecutionOrderLeg,
        PositionMutationIntent,
        StrategyManagementLeg,
    )

    if batch_id is None:
        return []
    legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch_id)
        .order_by(StrategyManagementLeg.id)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for leg in legs:
        rows.append(
            {
                "kind": "strategy_management_leg",
                "id": int(leg.id),
                "status": leg.status,
                "request": leg.request_json,
                "response": leg.response_json,
            }
        )
    binding_ids: set[int] = set()
    for leg in legs:
        if leg.execution_order_leg_id is None:
            continue
        order_leg = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if order_leg is not None and order_leg.execution_binding_id is not None:
            binding_ids.add(int(order_leg.execution_binding_id))
    for binding_id in binding_ids:
        intent_query = session.query(PositionMutationIntent).filter(
            PositionMutationIntent.execution_binding_id == binding_id
        )
        if window_start is not None:
            intent_query = intent_query.filter(PositionMutationIntent.created_at >= window_start)
        if window_end is not None:
            intent_query = intent_query.filter(PositionMutationIntent.created_at <= window_end)
        for intent in intent_query.order_by(PositionMutationIntent.id).all():
            rows.append(
                {
                    "kind": "position_mutation_intent",
                    "id": int(intent.id),
                    "operation": intent.operation,
                    "status": intent.status,
                    "request": getattr(intent, "request_json", None),
                    "response": getattr(intent, "response_json", None),
                }
            )
        event_query = session.query(ExecutionEvent).filter(
            ExecutionEvent.execution_binding_id == binding_id
        )
        if window_start is not None:
            event_query = event_query.filter(ExecutionEvent.created_at >= window_start)
        if window_end is not None:
            event_query = event_query.filter(ExecutionEvent.created_at <= window_end)
        for event in event_query.order_by(ExecutionEvent.id).all():
            rows.append(
                {
                    "kind": "execution_event",
                    "id": int(event.id),
                    "action": event.action,
                    "status": event.status,
                    "request": getattr(event, "request_json", None),
                    "response": getattr(event, "response_json", None),
                }
            )
    return rows


def finalize_executing_proposals(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    now: datetime,
    follow_timeout: timedelta = timedelta(minutes=15),
    deepcoin_client_factory: Callable[[], Any] | None = None,
) -> list[FinalizeOutcome]:
    """Read (never rerun) the exchange batch each executing proposal produced.

    Status semantics per strategy_management_batches.py/strategy_management_executor.py:
    ``succeeded``/``resolved`` are the only terminal-success statuses (see
    models.py:2179-2181's ``ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE`` and the
    executor's own docstring "exchange truth closes positions later" --
    ``execute_management_batch`` itself never blocks on that closure, so this
    function is the piece that eventually reads it back).

    Phase 4 batch 2 (spec section 4 tail / section 8, status doc 9.5 known
    gap #2): a batch that just settled ``succeeded`` gets one additional
    read-only exchange snapshot compared against the action's expected
    effect (``_run_post_execution_readback_for_proposal``). A mismatch *or*
    an incomplete/unavailable read downgrades the proposal to ``uncertain``
    -- this repository's fail-closed convention (AGENTS.md "Treat an
    incomplete external query as unknown, never as zero or healthy") is
    read as applying here too, so "unknown" is not silently accepted as
    success even though the spec's own wording ("回读不符") only names the
    mismatch case by name; this is a deliberate, documented deviation.
    Passing no ``deepcoin_client_factory`` skips readback entirely (state
    is decided by batch status alone, exactly like before this batch) --
    every real caller (the runtime background loop, the new auto
    end-to-end tests) passes one; only pre-existing tests that construct
    this call without it keep the old behaviour unchanged.
    """

    now = _naive_utc(now)
    outcomes: list[FinalizeOutcome] = []
    with session_factory() as session:
        rows = (
            session.query(OncallRemediationProposal)
            .filter(OncallRemediationProposal.state == "executing")
            .all()
        )
        for proposal in rows:
            if proposal.management_batch_id is None:
                continue
            batch = session.get(StrategyManagementBatch, proposal.management_batch_id)
            if batch is None:
                state = "uncertain"
            elif str(batch.status) in _BATCH_SUCCESS_STATUSES:
                state = "succeeded"
            elif str(batch.status) in _BATCH_FAILURE_STATUSES:
                state = "failed"
            elif str(batch.status) in _BATCH_AMBIGUOUS_STATUSES:
                state = "uncertain"
            elif str(batch.status) in _BATCH_IN_FLIGHT_STATUSES:
                executing_since = _naive_utc(proposal.executing_at) if proposal.executing_at else now
                if now - executing_since > follow_timeout:
                    state = "uncertain"
                else:
                    continue
            else:
                state = "uncertain"

            proposal_id_val = int(proposal.id)
            readback: ReadbackResult | None = None
            if state == "succeeded":
                readback = _run_post_execution_readback_for_proposal(
                    session_factory,
                    proposal=proposal,
                    deepcoin_client_factory=deepcoin_client_factory,
                )
                if readback.outcome in {"mismatch", "unknown"}:
                    state = "uncertain"

            proposal.state = state
            proposal.finished_at = now
            proposal.updated_at = now
            detail = {
                "batch_status": str(batch.status) if batch is not None else None,
                "reason_code": str(batch.reason_code) if batch is not None and batch.reason_code else None,
            }
            if readback is not None and readback.outcome in {"mismatch", "unknown"}:
                detail["reason_code"] = detail.get("reason_code") or f"readback_{readback.outcome}"
            proposal.result_json = _bounded_json(detail, limit=4096)
            session.add(proposal)
            _append_event(session, proposal_id_val, actor="worker", event="finalize", outcome=state, detail=detail, at=now)
            if readback is not None:
                _append_event(
                    session,
                    proposal_id_val,
                    actor="worker",
                    event="readback",
                    outcome=readback.outcome,
                    detail={"branch": readback.branch, **readback.detail} if readback.branch else readback.detail,
                    at=now,
                )
            breaker_message = _apply_outcome_to_breaker(session, state)
            if state in {"failed", "uncertain"} and proposal.execution_origin == "auto":
                reason = (
                    f"readback_{readback.outcome}"
                    if readback is not None and readback.outcome in {"mismatch", "unknown"}
                    else f"real_execution_{state}"
                )
                auto_message = _suspend_auto(session, reason=reason)
                if auto_message:
                    breaker_message = f"{breaker_message}\n{auto_message}" if breaker_message else auto_message
            elapsed_seconds = None
            if proposal.requested_at is not None:
                elapsed_seconds = (now - _naive_utc(proposal.requested_at)).total_seconds()
            window_start = _naive_utc(proposal.executing_at) if proposal.executing_at else None
            window_end = now + timedelta(minutes=1)
            _append_audit(
                session,
                proposal_id_val,
                phase="result",
                payload={
                    "result": {
                        "batch_status": detail.get("batch_status"),
                        "reason_code": detail.get("reason_code"),
                        "proposal_state": state,
                        "elapsed_seconds": elapsed_seconds,
                    },
                    "readback": (
                        {"outcome": readback.outcome, "branch": readback.branch, **readback.detail}
                        if readback is not None
                        else None
                    ),
                    "exchange_traffic": _gather_exchange_traffic_for_audit(
                        session,
                        batch_id=proposal.management_batch_id,
                        window_start=window_start,
                        window_end=window_end,
                    ),
                },
            )
            session.commit()
            if readback is not None and readback.branch:
                branch_zh = "市价平仓" if readback.branch == "market_closed" else "挂保本止损"
                detail_text = f"{branch_zh}，回读确认" if readback.outcome == "confirmed" else f"{branch_zh}，回读不符"
            elif readback is not None and readback.outcome in {"mismatch", "unknown"}:
                detail_text = f"回读{'不符' if readback.outcome == 'mismatch' else '结果未知'}"
            else:
                detail_text = (
                    "仓位/止损已按计划变化"
                    if state == "succeeded"
                    else str(detail.get("reason_code") or detail.get("batch_status") or "未知")
                )
            result_text = _format_result_text(
                proposal_id=proposal_id_val, state=state, detail=detail_text, breaker_message=breaker_message
            )
            if state == "succeeded" and proposal.execution_origin == "auto" and proposal.action_kind:
                ordinal = (
                    session.query(func.count(OncallRemediationProposal.id))
                    .filter(
                        OncallRemediationProposal.execution_origin == "auto",
                        OncallRemediationProposal.action_kind == proposal.action_kind,
                        OncallRemediationProposal.state == "succeeded",
                        OncallRemediationProposal.id <= proposal_id_val,
                    )
                    .scalar()
                    or 0
                )
                if 0 < ordinal <= config.auto_first_n_review:
                    action_zh = _ACTION_KIND_ZH.get(proposal.action_kind, proposal.action_kind)
                    result_text = (
                        f"{result_text}\n这是{action_zh}的第 {ordinal} 笔自动补救，请回看"
                    )
            outcomes.append(
                FinalizeOutcome(
                    proposal_id=proposal_id_val,
                    state=state,
                    text=result_text,
                    breaker_tripped=breaker_message is not None,
                )
            )
    return outcomes


def _note_auto_internal_error(session_factory: sessionmaker, *, occurred: bool, now: datetime) -> str | None:
    """Spec section 8: "G-D 或后台任务异常连续 3 次 -> 降为 shadow", independent
    of the "one real execution failure" breaker in ``_suspend_auto``'s other
    callers. ``occurred=True`` means ``run_gate_d`` itself raised inside one
    of its checks (surfaced as a synthetic ``"D?"``/``d_internal_error``
    check -- see ``run_gate_d``'s ``_run`` wrapper) or the background loop's
    own per-proposal/tick call raised; any clean tick (gate ran and produced
    an ordinary pass/refusal, or the tick completed normally) resets the
    streak to 0, mirroring ``control.consecutive_failures``'s reset on
    ``succeeded``."""

    with session_factory() as session:
        control = _get_or_create_control(session)
        if not occurred:
            if control.auto_consecutive_errors:
                control.auto_consecutive_errors = 0
                session.add(control)
                session.commit()
            return None
        control.auto_consecutive_errors = int(control.auto_consecutive_errors or 0) + 1
        session.add(control)
        message = None
        if control.auto_consecutive_errors >= 3:
            message = _suspend_auto(session, reason="auto_internal_error_streak")
        _append_event(
            session,
            None,
            actor="worker",
            event="auto_internal_error",
            outcome="error",
            detail={"consecutive": control.auto_consecutive_errors},
            at=now,
        )
        session.commit()
        return message


def note_auto_background_outcome(session_factory: sessionmaker, *, ok: bool, now: datetime) -> str | None:
    """Public entry point for ``oncall_remediation_runtime.py``'s background
    loop: an uncaught exception from a per-tick auto-relevant call
    (``compute_requested_proposal``/``execute_proposal_locked``/
    ``finalize_executing_proposals``) counts the same way a G-D internal
    error does (spec section 8's "后台任务异常连续 3 次"). Thin wrapper around
    ``_note_auto_internal_error`` so the runtime module never needs to reach
    into this module's private helpers."""

    return _note_auto_internal_error(session_factory, occurred=not ok, now=now)


def _suspend_auto(session, *, reason: str) -> str | None:
    """Phase 4's stricter breaker (spec section 8): one real execution
    failure/uncertain/readback-mismatch immediately suspends *auto*
    execution (``control.auto_suspended``), independent of the phase-3
    ``enabled``/``consecutive_failures`` breaker this sits next to. A no-op
    (returns ``None``) if auto is already suspended -- the notification only
    fires once.
    """

    control = _get_or_create_control(session)
    if control.auto_suspended:
        return None
    control.auto_suspended = True
    control.auto_suspended_at = datetime.now(UTC).replace(tzinfo=None)
    control.auto_suspend_reason = reason[:128]
    session.add(control)
    _append_event(
        session, None, actor="circuit_breaker", event="control", outcome="auto_suspended",
        detail={"reason": reason}, at=control.auto_suspended_at,
    )
    return "自动补救已暂停，改为只提示（可发 /auto_on 恢复）"


def _apply_outcome_to_breaker(session, state: str) -> str | None:
    """Update the circuit breaker for a just-settled proposal.

    Returns the "补救已自动关闭" text iff this call is the one that trips it.
    """

    control = _get_or_create_control(session)
    if state == "succeeded":
        control.consecutive_failures = 0
        session.add(control)
        return None
    if state in {"failed", "uncertain"}:
        control.consecutive_failures = int(control.consecutive_failures or 0) + 1
        session.add(control)
        if control.consecutive_failures >= 2 and control.enabled:
            control.enabled = False
            control.breaker_tripped_at = datetime.now(UTC).replace(tzinfo=None)
            control.changed_by = "circuit_breaker"
            control.reason = f"consecutive_{state}"
            session.add(control)
            session.execute(
                update(OncallRemediationProposal)
                .where(OncallRemediationProposal.state.in_(("proposed", "confirming")))
                .values(state="cancelled", updated_at=control.breaker_tripped_at, finished_at=control.breaker_tripped_at)
            )
            _append_event(
                session,
                None,
                actor="circuit_breaker",
                event="control",
                outcome="disabled",
                detail={"reason": f"consecutive_{state}", "consecutive_failures": control.consecutive_failures},
                at=control.breaker_tripped_at,
            )
            return f"补救已自动关闭：连续 {control.consecutive_failures} 次 {state}"
    return None


def recover_after_restart(session_factory: sessionmaker, *, now: datetime) -> list[str]:
    """Startup hook: an ``executing`` proposal with no batch yet is unknown.

    Never reruns anything -- see spec 6.3 ("worker 在 executing 中途重启 ->
    uncertain"). A proposal that already produced a batch is left alone;
    ``finalize_executing_proposals`` will read its outcome normally.
    """

    now = _naive_utc(now)
    texts: list[str] = []
    with session_factory() as session:
        rows = (
            session.query(OncallRemediationProposal)
            .filter(
                OncallRemediationProposal.state == "executing",
                OncallRemediationProposal.management_batch_id.is_(None),
            )
            .all()
        )
        for proposal in rows:
            proposal.state = "uncertain"
            proposal.finished_at = now
            proposal.updated_at = now
            session.add(proposal)
            _append_event(
                session,
                proposal.id,
                actor="worker",
                event="restart_recovery",
                outcome="uncertain",
                detail={"reason": "executing_without_batch_at_restart"},
                at=now,
            )
            breaker_message = _apply_outcome_to_breaker(session, "uncertain")
            session.commit()
            text = f"⚠️ 补救执行中断，结果未知 P{proposal.id}，需人工核对交易所。"
            if breaker_message:
                text = f"{text}\n{breaker_message}"
            texts.append(text)
    return texts


def expire_stale_proposals(session_factory: sessionmaker, *, now: datetime) -> list[int]:
    """CAS ``proposed``/``confirming`` rows whose clock has run out to ``expired``.

    Returns the ``telegram_message_id``s whose inline keyboard should be
    cleared.
    """

    now = _naive_utc(now)
    stale_message_ids: list[int] = []
    with session_factory() as session:
        proposed_rows = (
            session.query(OncallRemediationProposal)
            .filter(
                OncallRemediationProposal.state == "proposed",
                OncallRemediationProposal.expires_at.is_not(None),
                OncallRemediationProposal.expires_at < now,
            )
            .all()
        )
        for row in proposed_rows:
            row.state = "expired"
            row.finished_at = now
            row.updated_at = now
            session.add(row)
            _append_event(session, row.id, actor="worker", event="expire", outcome="expired", check="B4", at=now)
            if row.telegram_message_id is not None:
                stale_message_ids.append(int(row.telegram_message_id))

        confirming_rows = (
            session.query(OncallRemediationProposal)
            .filter(
                OncallRemediationProposal.state == "confirming",
                OncallRemediationProposal.approved_at.is_not(None),
            )
            .all()
        )
        for row in confirming_rows:
            if now - _naive_utc(row.approved_at) > timedelta(minutes=2):
                row.state = "expired"
                row.finished_at = now
                row.updated_at = now
                session.add(row)
                _append_event(session, row.id, actor="worker", event="expire", outcome="expired", check="B4", at=now)
                if row.telegram_message_id is not None:
                    stale_message_ids.append(int(row.telegram_message_id))
        session.commit()
    return stale_message_ids
