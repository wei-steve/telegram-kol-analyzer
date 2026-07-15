"""Bounded, crash-safe orchestration for durable strategy-management batches."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    claim_worker_batch,
    list_worker_batches,
    load_management_batch,
    transition_batch,
)
from telegram_kol_research.strategy_management_executor import (
    ManagementBatchExecutionError,
    execute_management_batch,
    validate_management_restart_snapshot,
)
from telegram_kol_research.strategy_management_reconciliation import (
    reconcile_strategy_management_batches,
)
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)
_RECOVERY_STATUSES = frozenset(
    {"executing", "reserved", "submitted", "submit_unknown", "reconciling"}
)
_PAUSED_STATUSES = frozenset({"recovery_required"})
_PROTECTION_ACTIONS = frozenset(
    {"adjust_stop_loss", "move_stop_to_break_even"}
)
_PROTECTION_PHASE_LEG_STATUSES = frozenset(
    {"succeeded", "restored", "recovery_required"}
)


@dataclass(frozen=True, slots=True)
class StrategyManagementWorkerResult:
    discovered: int = 0
    recovered: int = 0
    executed: int = 0
    paused: int = 0
    skipped: int = 0
    failed: int = 0


def run_strategy_management_worker_tick(
    session_factory,
    *,
    deepcoin_client_factory,
    max_batches: int = 10,
    allow_execution: bool = True,
    processed_at: datetime | None = None,
    batch_lister: Callable[..., list[ManagementBatchRecord]] = list_worker_batches,
    claimer: Callable[..., bool] = claim_worker_batch,
    snapshot_loader: Callable[..., Any] = load_deepcoin_execution_reconciliation_snapshot,
    reconciler: Callable[..., Any] = reconcile_strategy_management_batches,
    executor: Callable[..., Any] = execute_management_batch,
    restart_validator: Callable[..., None] = validate_management_restart_snapshot,
) -> StrategyManagementWorkerResult:
    """Process a bounded amount of work, isolating every durable batch failure."""

    limit = max(0, int(max_batches))
    if limit == 0:
        return StrategyManagementWorkerResult()
    batches = list(batch_lister(session_factory, limit=limit))[:limit]
    counts = {
        "discovered": len(batches),
        "recovered": 0,
        "executed": 0,
        "paused": 0,
        "skipped": 0,
        "failed": 0,
    }
    now = processed_at or datetime.now(UTC)
    client = None
    snapshot = None

    def get_client():
        nonlocal client
        if client is None:
            client = deepcoin_client_factory()
        return client

    def get_snapshot():
        nonlocal snapshot
        if snapshot is None:
            snapshot = snapshot_loader(session_factory, client=get_client())
        return snapshot

    for batch in batches:
        try:
            if batch.status in _PAUSED_STATUSES:
                counts["paused"] += 1
                continue
            if batch.status in {"ready", "protection_ready"}:
                if not allow_execution:
                    counts["skipped"] += 1
                    continue
                if not claimer(
                    session_factory,
                    batch_id=batch.id,
                    expected_status=batch.status,
                    claimed_at=now,
                ):
                    counts["skipped"] += 1
                    continue
                executor(
                    session_factory,
                    batch_id=batch.id,
                    deepcoin_client=get_client(),
                    executed_at=now,
                )
                counts["executed"] += 1
                continue
            if batch.status not in _RECOVERY_STATUSES:
                counts["skipped"] += 1
                continue

            # A restart path must establish exchange truth before it decides
            # whether any durable state can advance or any request can run.
            current_snapshot = get_snapshot()
            if _is_composite_protection_phase(batch):
                if not allow_execution:
                    counts["skipped"] += 1
                    continue
                executor(
                    session_factory,
                    batch_id=batch.id,
                    deepcoin_client=get_client(),
                    executed_at=now,
                )
                counts["recovered"] += 1
                continue
            if batch.status != "executing" or _has_submission_evidence(batch):
                reconciler(
                    session_factory,
                    snapshot=current_snapshot,
                    reconciled_at=now,
                    batch_ids=(batch.id,),
                )
                counts["recovered"] += 1
                continue

            # All-planned executing legs prove the crash happened before the
            # durable per-leg reservation and therefore before an API call.
            if not allow_execution:
                counts["skipped"] += 1
                continue
            try:
                restart_validator(
                    session_factory,
                    batch_id=batch.id,
                    snapshot=current_snapshot,
                )
            except ManagementBatchExecutionError:
                frozen = transition_batch(
                    session_factory,
                    batch.id,
                    expected_statuses={"executing"},
                    new_status="recovery_required",
                    transitioned_at=now,
                    reason_code="management_restart_snapshot_validation_failed",
                )
                if not frozen:
                    raise RuntimeError(
                        "management_restart_snapshot_freeze_conflict"
                    )
                counts["recovered"] += 1
                continue
            executor(
                session_factory,
                batch_id=batch.id,
                deepcoin_client=get_client(),
                executed_at=now,
            )
            counts["recovered"] += 1
        except ManagementBatchExecutionError:
            counts["failed"] += 1
            _persist_deterministic_pre_submit_failure(
                session_factory, batch_id=batch.id, failed_at=now
            )
            logger.exception("strategy management batch %s failed", batch.id)
        except Exception:
            counts["failed"] += 1
            logger.exception("strategy management batch %s failed", batch.id)
    return StrategyManagementWorkerResult(**counts)


async def run_strategy_management_worker_loop(
    *,
    session_factory,
    deepcoin_client_factory,
    interval_seconds: float = 5.0,
    max_batches: int = 10,
    now_provider=None,
) -> None:
    """Run bounded ticks forever; cancellation is owned by the Web lifespan."""

    while True:
        try:
            settings = load_trading_settings(session_factory)
            run_strategy_management_worker_tick(
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
                max_batches=max_batches,
                allow_execution=settings.live_management_execution_enabled,
                processed_at=(
                    now_provider() if now_provider is not None else datetime.now(UTC)
                ),
            )
        except Exception:
            logger.exception("strategy management worker tick failed")
        await asyncio.sleep(max(0.01, float(interval_seconds)))


def _has_submission_evidence(batch: ManagementBatchRecord) -> bool:
    return any(
        str(leg.status or "")
        in {"reserved", "submitted", "submit_unknown", "partial", "confirmed"}
        for leg in batch.legs
    )


def _is_composite_protection_phase(batch: ManagementBatchRecord) -> bool:
    if batch.effective_action in _PROTECTION_ACTIONS:
        return True
    if batch.effective_action != "partial_then_break_even":
        return False
    reason = str(batch.reason_code or "")
    return (
        reason.startswith("protection_")
        or reason.startswith("all_position_protection_")
        or any(
            str(leg.status or "") in _PROTECTION_PHASE_LEG_STATUSES
            for leg in batch.legs
        )
    )


def _persist_deterministic_pre_submit_failure(
    session_factory, *, batch_id: int, failed_at: datetime
) -> bool:
    """Make deterministic pre-submit failures durable without guessing unknowns."""

    try:
        batch = load_management_batch(session_factory, int(batch_id))
    except LookupError:
        return False
    if batch.status != "executing" or any(
        leg.status
        in {
            "reserved",
            "submitted",
            "submit_unknown",
            "partial",
            "succeeded",
            "restored",
            "recovery_required",
        }
        for leg in batch.legs
    ):
        return False
    return transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status="blocked",
        transitioned_at=failed_at,
        reason_code="management_pre_submit_validation_failed",
    )
