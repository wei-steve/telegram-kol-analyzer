"""Bounded orchestration for durable automatic break-even convergence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import or_, update

from telegram_kol_research.break_even_convergence_executor import (
    execute_break_even_convergence,
)
from telegram_kol_research.break_even_convergence_planner import (
    BreakEvenConvergencePlanningError,
    plan_or_adopt_break_even_convergence,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    PositionProtectionLeg,
    PositionProtectionIncident,
    PositionTakeProfitOrder,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
)
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)
_ALERT_STATUSES = frozenset(
    {"recovery_required", "failed_terminal"}
)
_RESUMABLE_STATUSES = frozenset({
    "claimed",
    "preflight_verified",
    "deciding_by_market",
    "executing_market_decisions",
})


@dataclass(frozen=True, slots=True)
class BreakEvenConvergenceWorkerResult:
    discovered: int = 0
    executed: int = 0
    shadowed: int = 0
    alerted: int = 0
    skipped: int = 0
    failed: int = 0


def run_break_even_convergence_worker_tick(
    session_factory,
    *,
    deepcoin_client_factory: Callable[[], Any],
    executor: Callable[..., Any] = execute_break_even_convergence,
    processed_at: datetime | None = None,
    lease_seconds: int = 120,
) -> BreakEvenConvergenceWorkerResult:
    """Claim at most one task; shadow/disabled paths never construct a client."""

    now = processed_at or datetime.now(UTC)
    _plan_proven_tp1_fills(session_factory, planned_at=now)
    alerted = _enqueue_pending_alerts(session_factory, created_at=now)
    claimed = _claim_one(
        session_factory,
        claimed_at=now,
        lease_seconds=max(1, int(lease_seconds)),
    )
    if claimed is None:
        return BreakEvenConvergenceWorkerResult(alerted=alerted)
    convergence_id, execution_mode = claimed
    if execution_mode == "disabled":
        with session_factory() as session:
            row = session.get(StrategyBreakEvenConvergence, convergence_id)
            if row is not None and row.status == "claimed":
                row.status = "blocked"
                row.reason_code = "automatic_break_even_disabled"
                row.updated_at = now
                session.commit()
        return BreakEvenConvergenceWorkerResult(
            discovered=1, alerted=alerted, skipped=1
        )

    client = deepcoin_client_factory()
    try:
        result = executor(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=client,
            executed_at=now,
        )
    except Exception as exc:
        logger.exception("automatic break-even convergence %s failed", convergence_id)
        with session_factory() as session:
            row = session.get(StrategyBreakEvenConvergence, convergence_id)
            if row is not None and row.status == "claimed":
                row.status = "recovery_required"
                row.reason_code = "break_even_worker_exception"
                row.updated_at = now
                session.commit()
        alerted += _enqueue_pending_alerts(session_factory, created_at=now)
        return BreakEvenConvergenceWorkerResult(
            discovered=1, alerted=alerted, failed=1
        )

    status = str(getattr(result, "status", "") or "")
    with session_factory() as session:
        row = session.get(StrategyBreakEvenConvergence, convergence_id)
        if row is not None and row.status == "claimed" and status:
            row.status = status
            row.reason_code = getattr(result, "reason_code", None)
            row.updated_at = now
            session.commit()
    if status in _ALERT_STATUSES:
        alerted += _enqueue_pending_alerts(session_factory, created_at=now)
    return BreakEvenConvergenceWorkerResult(
        discovered=1,
        executed=1 if status == "completed" else 0,
        shadowed=1 if status == "shadow_planned" else 0,
        alerted=alerted,
        failed=1 if status in _ALERT_STATUSES else 0,
    )


def _plan_proven_tp1_fills(session_factory, *, planned_at: datetime) -> int:
    """Bridge durable TP1 fill proof into exactly one convergence task."""

    settings = load_trading_settings(session_factory)
    if not settings.move_stop_to_breakeven_after_tp1:
        return 0
    if settings.live_management_execution_enabled:
        execution_mode = "live"
    elif settings.management_execution_mode == "shadow":
        execution_mode = "shadow"
    else:
        execution_mode = "disabled"
    with session_factory() as session:
        candidates = (
            session.query(PositionTakeProfitOrder)
            .join(
                PositionProtectionLeg,
                (PositionProtectionLeg.execution_order_leg_id
                 == PositionTakeProfitOrder.execution_order_leg_id)
                & (PositionProtectionLeg.exchange_order_id
                   == PositionTakeProfitOrder.order_id),
            )
            .filter(
                PositionTakeProfitOrder.status == "filled",
                PositionProtectionLeg.role == "take_profit",
                PositionProtectionLeg.leg_index == 1,
                PositionProtectionLeg.status == "filled",
            )
            .order_by(PositionTakeProfitOrder.id.asc())
            .all()
        )
        work = []
        for row in candidates:
            binding = session.get(ExecutionBinding, row.execution_binding_id)
            if binding is None or not binding.strategy_instance_id:
                continue
            try:
                evidence = json.loads(row.evidence_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            tp1_fill = evidence.get("tp1_fill") if isinstance(evidence, dict) else None
            if not isinstance(tp1_fill, dict) or not tp1_fill.get("evidence_tier"):
                continue
            work.append(
                (
                    str(binding.strategy_instance_id),
                    str(row.order_id),
                    {
                        "version": 1,
                        **tp1_fill,
                        "position_take_profit_order_id": int(row.id),
                        "execution_order_leg_id": int(row.execution_order_leg_id),
                        "pos_id": str(row.pos_id),
                        "confirmed_at": (
                            _utc_iso(row.completed_at)
                            if row.completed_at is not None
                            else _utc_iso(planned_at)
                        ),
                    },
                )
            )
    planned = 0
    for strategy_instance_id, trigger_identity, evidence in work:
        try:
            plan_or_adopt_break_even_convergence(
                session_factory,
                trigger_type="tp1_fill",
                trigger_identity=trigger_identity,
                trigger_evidence=evidence,
                strategy_instance_id=strategy_instance_id,
                planned_at=planned_at,
                execution_mode=execution_mode,
            )
        except BreakEvenConvergencePlanningError:
            continue
        planned += 1
    return planned


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _claim_one(
    session_factory,
    *,
    claimed_at: datetime,
    lease_seconds: int,
) -> tuple[int, str] | None:
    stale_before = claimed_at - timedelta(seconds=lease_seconds)
    with session_factory() as session:
        candidates = (
            session.query(StrategyBreakEvenConvergence)
            .filter(
                or_(
                    StrategyBreakEvenConvergence.status == "planned",
                    (
                        StrategyBreakEvenConvergence.status.in_(
                            _RESUMABLE_STATUSES
                        )
                    )
                    & (StrategyBreakEvenConvergence.updated_at <= stale_before),
                )
            )
            .order_by(
                StrategyBreakEvenConvergence.planned_at.asc(),
                StrategyBreakEvenConvergence.id.asc(),
            )
            .limit(1)
            .all()
        )
        if not candidates:
            return None
        candidate = candidates[0]
        claimable = or_(
            StrategyBreakEvenConvergence.status == "planned",
            (
                StrategyBreakEvenConvergence.status.in_(
                    _RESUMABLE_STATUSES
                )
            )
            & (StrategyBreakEvenConvergence.updated_at <= stale_before),
        )
        claimed = session.execute(
            update(StrategyBreakEvenConvergence)
            .where(
                StrategyBreakEvenConvergence.id == candidate.id,
                claimable,
            )
            .values(status="claimed", updated_at=claimed_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        session.commit()
        if claimed != 1:
            return None
        return int(candidate.id), str(candidate.execution_mode)


def _enqueue_pending_alerts(session_factory, *, created_at: datetime) -> int:
    created = 0
    with session_factory() as session:
        rows = (
            session.query(StrategyBreakEvenConvergence)
            .filter(StrategyBreakEvenConvergence.status.in_(_ALERT_STATUSES))
            .order_by(StrategyBreakEvenConvergence.id.asc())
            .all()
        )
        for row in rows:
            legs = (
                session.query(StrategyBreakEvenConvergenceLeg)
                .filter_by(convergence_id=row.id)
                .order_by(StrategyBreakEvenConvergenceLeg.id.asc())
                .all()
            )
            for leg in legs:
                fingerprint = hashlib.sha256(
                    (
                        f"automatic-break-even:{row.id}:{leg.id}:"
                        f"{row.status}:{row.reason_code or ''}"
                    ).encode("utf-8")
                ).hexdigest()
                exists = (
                    session.query(PositionProtectionIncident.id)
                    .filter_by(fingerprint=fingerprint)
                    .first()
                )
                if exists is not None:
                    continue
                evidence = {
                    "convergence_id": int(row.id),
                    "convergence_leg_id": int(leg.id),
                    "strategy_instance_id": str(row.strategy_instance_id),
                    "trigger_type": str(row.trigger_type),
                    "status": str(row.status),
                    "reason_code": row.reason_code,
                    "manual_action": (
                        "Read exchange position and TPSL state; do not retry writes "
                        "until every unknown mutation is reconciled."
                    ),
                }
                session.add(PositionProtectionIncident(
                    venue=str(row.venue),
                    execution_binding_id=int(row.execution_binding_id),
                    execution_order_leg_id=int(leg.execution_order_leg_id),
                    pos_id=str(leg.pos_id),
                    incident_type=f"automatic_break_even_{row.status}",
                    fingerprint=fingerprint,
                    evidence_json=json.dumps(
                        evidence,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    delivery_status="pending",
                    created_at=created_at,
                    updated_at=created_at,
                ))
                created += 1
        session.commit()
    return created


async def run_break_even_convergence_worker_loop(
    session_factory,
    *,
    deepcoin_client_factory: Callable[[], Any],
    interval_seconds: float = 2.0,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    """Run bounded ticks until the owning application cancels the task."""

    while True:
        try:
            run_break_even_convergence_worker_tick(
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
                processed_at=(
                    now_provider() if now_provider is not None else datetime.now(UTC)
                ),
            )
        except Exception:
            logger.exception("automatic break-even worker tick failed")
        await asyncio.sleep(max(0.01, float(interval_seconds)))
