"""Deterministically rank existing strategy threads for one message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyThread,
)
from telegram_kol_research.strategy_threads import (
    ACTIVE_THREAD_STATUSES,
    list_relevant_strategy_threads,
)


REVISION_TERMS = ("更新", "修改", "改为", "调整", "取消", "撤销", "保护", "保本")
ACTIVE_LIFECYCLE_STATUSES = frozenset(
    {"pending_entry", "entered", "holding", "expired"}
)
VERIFIED_ATTRIBUTION_STATUSES = frozenset({"verified", "bound", "confirmed"})
UNCERTAIN_ATTRIBUTION_STATUSES = frozenset(
    {"attribution_conflict", "evidence_unavailable"}
)
LIVE_ENTRY_STATUSES = frozenset({"active", "partially_filled"})
PENDING_ENTRY_STATUSES = frozenset({"pending", "submitted", "open"})
UNCERTAIN_ENTRY_STATUSES = frozenset(
    {"unknown", "recovery_required", "submitting"}
)
EXACT_ENTRY_ORDER_KINDS = frozenset(
    {"market", "limit", "regular", "trigger", "trigger_limit"}
)
REASON_WEIGHTS = {
    "direct_reply_link": 1000,
    "reply_ancestor_link": 800,
    "existing_message_link": 700,
    "same_chat": 100,
    "same_symbol": 100,
    "same_side": 100,
    "revision_language": 80,
    "overlapping_entry": 60,
    "overlapping_stop_loss": 40,
    "overlapping_take_profit": 30,
    "recent_active_thread": 20,
}
REASON_ORDER = tuple(REASON_WEIGHTS)


@dataclass(frozen=True, slots=True)
class StrategyThreadCandidate:
    thread_id: int
    lifecycle_id: int
    root_message_id: int
    symbol: str
    side: str
    status: str
    score: int
    reasons: tuple[str, ...]
    lifecycle_summary: dict[str, Any]
    binding_summary: dict[str, Any] | None
    verified_leg_summaries: tuple[dict[str, Any], ...]
    risk_state: str
    live_verified_pos_ids: tuple[str, ...]
    pending_entry_leg_ids: tuple[int, ...]
    uncertain_entry_leg_ids: tuple[int, ...]


def _overlaps(
    first_low: float | None,
    first_high: float | None,
    second_low: float | None,
    second_high: float | None,
) -> bool:
    if None in (first_low, first_high, second_low, second_high):
        return False
    return max(float(first_low), float(second_low)) <= min(
        float(first_high),
        float(second_high),
    )


def _linked_threads_for_raw_message(
    session: Session,
    raw_message_id: int,
) -> set[int]:
    return {
        int(row[0])
        for row in (
            session.query(StrategyMessageLink.strategy_thread_id)
            .filter(
                StrategyMessageLink.raw_message_id == int(raw_message_id),
                StrategyMessageLink.status == "active",
            )
            .all()
        )
    }


def _reply_link_depths(
    session: Session,
    current: RawMessage,
    *,
    max_depth: int = 5,
) -> dict[int, int]:
    depths: dict[int, int] = {}
    next_message_id = current.reply_to_message_id
    seen = {int(current.message_id)}
    for depth in range(1, max_depth + 1):
        if next_message_id is None or int(next_message_id) in seen:
            break
        seen.add(int(next_message_id))
        target = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == int(current.chat_id),
                RawMessage.message_id == int(next_message_id),
            )
            .one_or_none()
        )
        if target is None:
            break
        for thread_id in _linked_threads_for_raw_message(session, int(target.id)):
            depths.setdefault(thread_id, depth)
        next_message_id = target.reply_to_message_id
    return depths


def _binding_context(
    session: Session,
    lifecycle: StrategyLifecycle,
) -> tuple[
    dict[str, Any] | None,
    tuple[dict[str, Any], ...],
    str,
    tuple[str, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    if lifecycle.execution_binding_id is None:
        return None, (), "no_current_risk", (), (), ()
    binding = session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
    if binding is None:
        return None, (), "uncertain_risk", (), (), ()
    summary = {
        "id": int(binding.id),
        "strategy_instance_id": binding.strategy_instance_id,
        "status": binding.status,
        "order_id": binding.order_id,
        "pos_id": binding.pos_id,
        "last_exchange_status": binding.last_exchange_status,
    }
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
        .order_by(ExecutionOrderLeg.purpose.asc(), ExecutionOrderLeg.leg_index.asc())
        .all()
    )
    verified = tuple(
        {
            "id": int(leg.id),
            "purpose": leg.purpose,
            "leg_index": int(leg.leg_index),
            "status": leg.status,
            "order_id": leg.order_id,
            "pos_id": leg.pos_id,
            "attribution_status": leg.attribution_status,
            "last_verified_at": (
                leg.last_verified_at.isoformat()
                if leg.last_verified_at is not None
                else None
            ),
        }
        for leg in legs
        if leg.last_verified_at is not None
        or leg.attribution_status in {"verified", "bound", "confirmed"}
    )
    entry_legs = tuple(leg for leg in legs if str(leg.purpose) == "entry")
    live_pos_ids = tuple(
        dict.fromkeys(
            str(leg.pos_id).strip()
            for leg in entry_legs
            if str(leg.attribution_status or "") in VERIFIED_ATTRIBUTION_STATUSES
            and str(leg.status or "") in LIVE_ENTRY_STATUSES
            and str(leg.pos_id or "").strip()
        )
    )
    pending_leg_ids = tuple(
        int(leg.id)
        for leg in entry_legs
        if str(leg.status or "") in PENDING_ENTRY_STATUSES
        and str(leg.order_kind or "") in EXACT_ENTRY_ORDER_KINDS
        and bool(str(leg.order_id or leg.client_order_id or "").strip())
        and not (
            str(leg.attribution_status or "") in VERIFIED_ATTRIBUTION_STATUSES
            and str(leg.pos_id or "").strip()
        )
    )
    uncertain_leg_ids = tuple(
        int(leg.id)
        for leg in entry_legs
        if str(leg.attribution_status or "") in UNCERTAIN_ATTRIBUTION_STATUSES
        or str(leg.status or "") in UNCERTAIN_ENTRY_STATUSES
    )
    if uncertain_leg_ids:
        risk_state = "uncertain_risk"
    elif live_pos_ids or pending_leg_ids:
        risk_state = "current_risk"
    else:
        risk_state = "no_current_risk"
    return (
        summary,
        verified,
        risk_state,
        live_pos_ids,
        pending_leg_ids,
        uncertain_leg_ids,
    )


def generate_strategy_thread_candidates(
    session: Session,
    *,
    raw_message_id: int,
    symbol: str | None = None,
    side: str | None = None,
    entry_range_low: float | None = None,
    entry_range_high: float | None = None,
    stop_loss: float | None = None,
    take_profit: str | None = None,
    max_candidates: int = 20,
) -> tuple[StrategyThreadCandidate, ...]:
    """Return deterministic, auditable candidates without calling an AI."""

    current = session.get(RawMessage, int(raw_message_id))
    if current is None:
        raise LookupError("raw message not found")
    normalized_symbol = str(symbol or "").strip().upper() or None
    normalized_side = str(side or "").strip().lower() or None
    text = str(current.text or "")
    revision_language = any(term in text for term in REVISION_TERMS)
    reply_depths = _reply_link_depths(session, current)
    existing_links = _linked_threads_for_raw_message(session, int(current.id))
    threads = list_relevant_strategy_threads(
        session,
        chat_id=int(current.chat_id),
        limit=200,
    )
    ranked: list[tuple[tuple[int, int, float, int], StrategyThreadCandidate]] = []
    for thread in threads:
        if normalized_symbol is not None and thread.symbol.upper() != normalized_symbol:
            continue
        if normalized_side is not None and thread.side.lower() != normalized_side:
            continue
        lifecycle_id = thread.current_lifecycle_id
        if lifecycle_id is None:
            lifecycle = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.strategy_thread_id == int(thread.id))
                .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
                .first()
            )
        else:
            lifecycle = session.get(StrategyLifecycle, int(lifecycle_id))
        if lifecycle is None or lifecycle.lifecycle_status not in ACTIVE_LIFECYCLE_STATUSES:
            continue

        reasons: list[str] = []
        reply_depth = reply_depths.get(int(thread.id))
        if reply_depth == 1:
            reasons.append("direct_reply_link")
        elif reply_depth is not None:
            reasons.append("reply_ancestor_link")
        if int(thread.id) in existing_links:
            reasons.append("existing_message_link")
        reasons.append("same_chat")
        if normalized_symbol is not None:
            reasons.append("same_symbol")
        if normalized_side is not None:
            reasons.append("same_side")
        if revision_language:
            reasons.append("revision_language")
        if _overlaps(
            entry_range_low,
            entry_range_high,
            lifecycle.entry_range_low,
            lifecycle.entry_range_high,
        ):
            reasons.append("overlapping_entry")
        if stop_loss is not None and lifecycle.stop_loss is not None:
            if abs(float(stop_loss) - float(lifecycle.stop_loss)) <= max(
                1.0, abs(float(lifecycle.stop_loss)) * 0.005
            ):
                reasons.append("overlapping_stop_loss")
        if take_profit and lifecycle.take_profit and str(take_profit) == str(lifecycle.take_profit):
            reasons.append("overlapping_take_profit")
        if (
            current.posted_at is None
            or lifecycle.signal_at >= current.posted_at - timedelta(hours=72)
        ):
            reasons.append("recent_active_thread")
        ordered_reasons = tuple(reason for reason in REASON_ORDER if reason in reasons)
        score = sum(REASON_WEIGHTS[reason] for reason in ordered_reasons)
        (
            binding_summary,
            verified_legs,
            risk_state,
            live_verified_pos_ids,
            pending_entry_leg_ids,
            uncertain_entry_leg_ids,
        ) = _binding_context(session, lifecycle)
        candidate = StrategyThreadCandidate(
            thread_id=int(thread.id),
            lifecycle_id=int(lifecycle.id),
            root_message_id=int(thread.root_message_id),
            symbol=str(thread.symbol),
            side=str(thread.side),
            status=str(lifecycle.lifecycle_status),
            score=score,
            reasons=ordered_reasons,
            lifecycle_summary={
                "id": int(lifecycle.id),
                "signal_at": lifecycle.signal_at.isoformat(),
                "entered_at": (
                    lifecycle.entered_at.isoformat()
                    if lifecycle.entered_at is not None
                    else None
                ),
                "entry_range_low": lifecycle.entry_range_low,
                "entry_range_high": lifecycle.entry_range_high,
                "stop_loss": lifecycle.stop_loss,
                "take_profit": lifecycle.take_profit,
                "execution_binding_id": lifecycle.execution_binding_id,
            },
            binding_summary=binding_summary,
            verified_leg_summaries=verified_legs,
            risk_state=risk_state,
            live_verified_pos_ids=live_verified_pos_ids,
            pending_entry_leg_ids=pending_entry_leg_ids,
            uncertain_entry_leg_ids=uncertain_entry_leg_ids,
        )
        rank = (
            0 if reply_depth == 1 else 1 if reply_depth is not None else 2,
            -score,
            -lifecycle.signal_at.timestamp(),
            int(thread.id),
        )
        ranked.append((rank, candidate))
    ranked.sort(key=lambda item: item[0])
    bounded = max(1, min(int(max_candidates), 20))
    return tuple(candidate for _, candidate in ranked[:bounded])


def exact_single_current_risk_thread(
    session: Session,
    *,
    raw_message_id: int,
    target_thread_id: int,
) -> tuple[bool, int | None]:
    """Exhaustively prove one current-risk thread without model filters.

    The query streams every active thread in the source chat so display ranking,
    symbol/side hints, and the 20-candidate prompt bound cannot authorize an exit.
    """

    current = session.get(RawMessage, int(raw_message_id))
    if current is None:
        return False, None
    target_root_message_id: int | None = None
    target_is_current = False
    threads = (
        session.query(StrategyThread)
        .filter(StrategyThread.chat_id == int(current.chat_id))
        .filter(StrategyThread.status.in_(ACTIVE_THREAD_STATUSES))
        .order_by(StrategyThread.id.asc())
        .yield_per(100)
    )
    for thread in threads:
        lifecycle = (
            session.get(StrategyLifecycle, int(thread.current_lifecycle_id))
            if thread.current_lifecycle_id is not None
            else (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.strategy_thread_id == int(thread.id))
                .order_by(
                    StrategyLifecycle.signal_at.desc(),
                    StrategyLifecycle.id.desc(),
                )
                .first()
            )
        )
        if lifecycle is None or lifecycle.lifecycle_status not in ACTIVE_LIFECYCLE_STATUSES:
            continue
        risk_state = _binding_context(session, lifecycle)[2]
        if int(thread.id) == int(target_thread_id):
            target_root_message_id = int(thread.root_message_id)
            target_is_current = risk_state == "current_risk"
        elif risk_state != "no_current_risk":
            return False, target_root_message_id
    return target_is_current, target_root_message_id
