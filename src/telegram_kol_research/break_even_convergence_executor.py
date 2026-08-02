"""Execute durable strategy-wide break-even convergence in audited phases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.models import (
    ExecutionOrderLeg,
    StrategyBreakEvenConvergence,
)
from telegram_kol_research.terminal_entry_cleanup import (
    cleanup_terminal_entry_legs,
)


@dataclass(frozen=True, slots=True)
class BreakEvenConvergenceExecutionResult:
    id: int
    status: str
    reason_code: str | None


def execute_break_even_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
    stop_after_deferred_cleanup: bool = False,
) -> BreakEvenConvergenceExecutionResult:
    """Run one convergence without repeating exchange writes after uncertainty."""

    now = executed_at or datetime.now(UTC)
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, int(convergence_id))
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        if convergence.status in {
            "completed",
            "blocked",
            "failed_terminal",
            "recovery_required",
            "shadow_planned",
        }:
            return _result(convergence)
        if convergence.execution_mode == "disabled":
            convergence.status = "blocked"
            convergence.reason_code = "automatic_break_even_disabled"
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        if convergence.execution_mode == "shadow":
            convergence.status = "shadow_planned"
            convergence.reason_code = None
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        if convergence.execution_mode != "live":
            raise RuntimeError("break_even_convergence_execution_mode_invalid")
        snapshot = _load_target_snapshot(convergence.target_snapshot_json)
        deferred_ids = tuple(
            sorted(int(item) for item in snapshot.get("deferred_entry_leg_ids") or [])
        )
        current_deferred = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.id.in_(deferred_ids))
            .all()
            if deferred_ids
            else []
        )
        if {int(leg.id) for leg in current_deferred} != set(deferred_ids):
            convergence.status = "blocked"
            convergence.reason_code = "deferred_entry_target_drift"
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        for leg in current_deferred:
            if leg.pos_id or str(leg.status or "").lower() not in {
                "pending",
                "open",
                "submitted",
                "partially_filled",
                "partial",
                "cancelled",
                "exchange_cancelled",
            }:
                convergence.status = "blocked"
                convergence.reason_code = "deferred_entry_target_drift"
                convergence.updated_at = now
                session.commit()
                return _result(convergence)
        convergence.status = "preflight_verified"
        convergence.started_at = convergence.started_at or now
        convergence.updated_at = now
        session.commit()
        lifecycle_id = int(convergence.target_lifecycle_id)
        binding_id = int(convergence.execution_binding_id)

    cleanup = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=deepcoin_client,
        reason="automatic_break_even",
        cleaned_at=now,
        expected_binding_id=binding_id,
    )
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, int(convergence_id))
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        if cleanup.status in {"resolved", "already_absent"}:
            convergence.status = "deciding_by_market"
            convergence.reason_code = None
        elif cleanup.status == "unknown":
            convergence.status = "recovery_required"
            convergence.reason_code = "deferred_entry_cancel_outcome_unknown"
        else:
            filled_during_cleanup = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.id.in_(cleanup.leg_ids))
                .filter(ExecutionOrderLeg.pos_id.is_not(None))
                .first()
            )
            convergence.status = (
                "recovery_required" if filled_during_cleanup else "blocked"
            )
            convergence.reason_code = (
                "deferred_entry_filled_during_cleanup"
                if filled_during_cleanup
                else "deferred_entry_cancel_not_confirmed"
            )
        convergence.updated_at = now
        session.commit()
        result = _result(convergence)
    if stop_after_deferred_cleanup or result.status != "deciding_by_market":
        return result
    return _execute_market_decisions(
        session_factory,
        convergence_id=int(convergence_id),
        deepcoin_client=deepcoin_client,
        executed_at=now,
    )


def _execute_market_decisions(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> BreakEvenConvergenceExecutionResult:
    # Added in the next TDD step. Keeping the boundary explicit prevents the
    # deferred-entry phase from accidentally writing protection first.
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        return _result(convergence)


def _load_target_snapshot(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("break_even_target_snapshot_corrupt") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("break_even_target_snapshot_corrupt")
    return parsed


def _result(
    convergence: StrategyBreakEvenConvergence,
) -> BreakEvenConvergenceExecutionResult:
    return BreakEvenConvergenceExecutionResult(
        id=int(convergence.id),
        status=str(convergence.status),
        reason_code=convergence.reason_code,
    )
