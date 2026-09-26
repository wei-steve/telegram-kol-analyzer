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
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time as dtime, timedelta
from typing import Any, Callable

from sqlalchemy import exists, func, update
from sqlalchemy.orm import aliased, sessionmaker

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.models import (
    MessageInstructionItem,
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
    state: str  # "proposed" | "refused"
    refusal_reason: str | None
    text: str | None
    keyboard: tuple[tuple[str, str], ...] | None
    should_send: bool
    breaker_tripped: bool = False


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
    "plan_changed": "计划已变化，补救已取消",
    "expired": "提案已过期",
    "state_changed": "提案状态已变化",
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

        snapshot = _build_action_snapshot(action)
        token1: str | None = None
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
            row.state = "proposed"
            row.proposed_at = now
            row.expires_at = now + timedelta(minutes=config.proposal_expiry_minutes)
            row.updated_at = now
            if config.effective_mode == "approve":
                token1 = _new_token()
                row.step1_token_hash = _hash_token(token1)
            session.add(row)
            _append_event(
                session,
                proposal_id,
                actor="worker",
                event="gate_a",
                gate="A",
                outcome="proposed",
                detail={"action_kind": action.action_kind, "action_id": action.action_id},
                at=now,
            )
            session.commit()
            expires_at = row.expires_at

        text = _format_proposal_text(
            proposal_id=proposal_id,
            case_no=case_no,
            raw_message_id=raw_message_id,
            expires_at=expires_at,
            action=action,
            shadow=config.effective_mode != "approve",
            group_label=(
                group_label(gate.raw_message.chat_id)
                if group_label and gate.raw_message is not None
                else "群组"
            ),
            window_minutes=gate.window_minutes,
            posted_at=gate.posted_at or now,
        )
        keyboard = None
        if config.effective_mode == "approve" and token1 is not None:
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

    if config.effective_mode != "approve":
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

    if parts and parts[0] == "/fix":
        if len(parts) != 2 or not re.fullmatch(r"P\d+", parts[1]):
            return CommandOutcome(accepted=False, text="命令格式错误，应为 /fix P<提案号>")
        proposal_id = int(parts[1][1:])
        if config.effective_mode != "approve":
            return CommandOutcome(accepted=False, text="当前为只提示模式", proposal_id=proposal_id)
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

    def _fail(state: str, reason: str, *, check: str | None) -> ExecutionOutcome:
        with session_factory() as session:
            row = session.get(OncallRemediationProposal, proposal_id)
            row.state = state
            row.finished_at = now
            row.updated_at = now
            row.result_json = _bounded_json({"reason": reason}, limit=4096)
            session.add(row)
            _append_event(session, proposal_id, actor="worker", event="gate_c", gate="C", check=check, outcome=state, detail={"reason": reason}, at=now)
            breaker_message = _apply_outcome_to_breaker(session, state)
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

        scope = RemediationScope.from_json(scope_json) if scope_json else gate.scope

        with session_factory() as session:
            _append_event(session, proposal_id, actor="worker", event="gate_c", gate="C", check="C4", outcome="applying", at=now)
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


def finalize_executing_proposals(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    now: datetime,
    follow_timeout: timedelta = timedelta(minutes=15),
) -> list[FinalizeOutcome]:
    """Read (never rerun) the exchange batch each executing proposal produced.

    Status semantics per strategy_management_batches.py/strategy_management_executor.py:
    ``succeeded``/``resolved`` are the only terminal-success statuses (see
    models.py:2179-2181's ``ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE`` and the
    executor's own docstring "exchange truth closes positions later" --
    ``execute_management_batch`` itself never blocks on that closure, so this
    function is the piece that eventually reads it back).
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
            proposal.state = state
            proposal.finished_at = now
            proposal.updated_at = now
            detail = {
                "batch_status": str(batch.status) if batch is not None else None,
                "reason_code": str(batch.reason_code) if batch is not None and batch.reason_code else None,
            }
            proposal.result_json = _bounded_json(detail, limit=4096)
            session.add(proposal)
            _append_event(session, proposal_id_val, actor="worker", event="finalize", outcome=state, detail=detail, at=now)
            breaker_message = _apply_outcome_to_breaker(session, state)
            session.commit()
            detail_text = (
                "仓位/止损已按计划变化"
                if state == "succeeded"
                else str(detail.get("reason_code") or detail.get("batch_status") or "未知")
            )
            outcomes.append(
                FinalizeOutcome(
                    proposal_id=proposal_id_val,
                    state=state,
                    text=_format_result_text(
                        proposal_id=proposal_id_val, state=state, detail=detail_text, breaker_message=breaker_message
                    ),
                    breaker_tripped=breaker_message is not None,
                )
            )
    return outcomes


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
