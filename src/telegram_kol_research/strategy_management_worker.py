"""Bounded, crash-safe orchestration for durable strategy-management batches."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot,
    reconcile_deepcoin_execution_bindings,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    ManagementBatchRecord,
    TEMPORARY_VISIBILITY_REASONS,
    claim_worker_batch,
    create_race_resolved_successor_batch,
    list_worker_batches,
    load_management_batch,
    transition_batch,
)
from telegram_kol_research.strategy_management_executor import (
    ManagementBatchExecutionError,
    execute_management_batch,
    validate_management_restart_snapshot,
)
from telegram_kol_research.trigger_take_profit_convergence_executor import (
    execute_ready_trigger_take_profit_convergences,
)
from telegram_kol_research.strategy_management_reconciliation import (
    reconcile_strategy_management_batches,
)
from telegram_kol_research.strategy_management_planner import plan_strategy_management_batch
from telegram_kol_research.message_instruction_items import (
    claim_next_visibility_retry_instruction_item,
    defer_message_instruction_item_for_visibility,
    finish_message_instruction_item,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyManagementBatch,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    canonical_live_position_economics,
    require_equivalent_live_position_economics,
    require_verified_position_ownership,
)
from telegram_kol_research.strategy_management_sizing import (
    ManagementSizingError,
    allocate_close_sizes,
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
_CLOSE_ACTIONS = frozenset(
    {"partial_close", "full_close", "full_exit", "partial_then_break_even"}
)


@dataclass(frozen=True, slots=True)
class StrategyManagementWorkerResult:
    discovered: int = 0
    recovered: int = 0
    executed: int = 0
    paused: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class StrategyManagementWorkerCursor:
    """In-process lane rotation; database claims remain concurrency authority."""

    next_lane: str = "executable"


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
    binding_reconciler: Callable[..., Any] = reconcile_deepcoin_execution_bindings,
    race_successor_resolver: Callable[..., bool] | None = None,
    restart_validator: Callable[..., None] = validate_management_restart_snapshot,
    cursor: StrategyManagementWorkerCursor | None = None,
    contract_spec_provider=None,
    take_profit_convergence_runner: Callable[..., int] = execute_ready_trigger_take_profit_convergences,
    instruction_planner: Callable[..., Any] = plan_strategy_management_batch,
) -> StrategyManagementWorkerResult:
    """Process a bounded amount of work, isolating every durable batch failure."""

    limit = max(0, int(max_batches))
    if limit == 0:
        return StrategyManagementWorkerResult()
    prefer_recovery = not allow_execution or (
        cursor is not None and cursor.next_lane == "recovery"
    )
    batches = list(
        batch_lister(
            session_factory,
            limit=limit,
            prefer_recovery=prefer_recovery,
        )
    )[:limit]
    if cursor is not None and allow_execution and limit == 1:
        cursor.next_lane = "executable" if prefer_recovery else "recovery"
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

    if (
        allow_execution
        and contract_spec_provider is not None
        and callable(session_factory)
    ):
        for _ in range(limit):
            item = claim_next_visibility_retry_instruction_item(
                session_factory,
                now=now,
            )
            if item is None:
                break
            counts["discovered"] += 1
            try:
                prior_result = json.loads(item.result_json or "{}")
                execution_mode = str(
                    prior_result.get("execution_mode") or "live"
                ).lower()
                planning_result = instruction_planner(
                    session_factory,
                    raw_message_id=item.raw_message_id,
                    candidate_id=item.signal_candidate_id,
                    deepcoin_client=get_client(),
                    contract_spec_provider=contract_spec_provider,
                    planned_at=now,
                    execution_mode=execution_mode,
                )
                if (
                    planning_result.status == "deferred"
                    and planning_result.reason_code
                    == "target_strategy_binding_not_visible_yet"
                ):
                    defer_message_instruction_item_for_visibility(
                        session_factory,
                        item_id=item.id,
                        result={
                            "status": "deferred",
                            "reason": planning_result.reason_code,
                            "execution_mode": execution_mode,
                        },
                        now=now,
                    )
                    counts["recovered"] += 1
                    continue
                if planning_result.status != "ready" or planning_result.batch is None:
                    finish_message_instruction_item(
                        session_factory,
                        item_id=item.id,
                        status="failed",
                        result={
                            "status": planning_result.status,
                            "reason": planning_result.reason_code,
                            "batch_id": planning_result.batch_id,
                        },
                        now=now,
                    )
                    counts["failed"] += 1
                    continue
                if execution_mode == "shadow":
                    finish_message_instruction_item(
                        session_factory,
                        item_id=item.id,
                        status="succeeded",
                        result={
                            "status": "shadow_planned",
                            "batch_id": planning_result.batch.id,
                        },
                        now=now,
                    )
                    counts["recovered"] += 1
                    continue
                execution_result = executor(
                    session_factory,
                    batch_id=planning_result.batch.id,
                    deepcoin_client=get_client(),
                    executed_at=now,
                )
                finish_status = (
                    "submitted"
                    if execution_result.get("submitted") is True
                    else "failed"
                    if str(execution_result.get("status") or "").lower()
                    in {"failed", "partial_failed", "blocked"}
                    else "succeeded"
                )
                finish_message_instruction_item(
                    session_factory,
                    item_id=item.id,
                    status=finish_status,
                    result=execution_result,
                    now=now,
                )
                if finish_status == "failed":
                    counts["failed"] += 1
                else:
                    counts["executed"] += 1
            except DeepcoinRequestOutcomeUnknown as exc:
                finish_message_instruction_item(
                    session_factory,
                    item_id=item.id,
                    status="unknown",
                    result={"type": type(exc).__name__, "message": str(exc)},
                    now=now,
                )
                counts["failed"] += 1
                logger.exception(
                    "management visibility retry instruction %s outcome unknown",
                    item.id,
                )
            except Exception as exc:
                finish_message_instruction_item(
                    session_factory,
                    item_id=item.id,
                    status="failed",
                    result={"type": type(exc).__name__, "message": str(exc)},
                    now=now,
                )
                counts["failed"] += 1
                logger.exception(
                    "management visibility retry instruction %s failed",
                    item.id,
                )

    if allow_execution and contract_spec_provider is not None:
        try:
            binding_reconciler(
                session_factory,
                client=get_client(),
                recovered_at=now,
                snapshot=get_snapshot(),
                contract_spec_provider=contract_spec_provider,
            )
        except Exception:
            logger.exception("backup-stop reconciliation before take-profit lane failed")

    if allow_execution:
        try:
            counts["executed"] += int(
                take_profit_convergence_runner(
                    session_factory,
                    deepcoin_client=get_client(),
                    contract_spec_provider=contract_spec_provider,
                    processed_at=now,
                    limit=limit,
                )
                or 0
            )
        except Exception:
            logger.exception("trigger take-profit convergence worker lane failed")

    def resolve_race_successor(*, batch_id: int, current_snapshot: Any) -> bool:
        if race_successor_resolver is None:
            return _resolve_deferred_entry_cancel_race_successor(
                session_factory,
                batch_id=batch_id,
                snapshot=current_snapshot,
                resolved_at=now,
            )
        return bool(
            race_successor_resolver(
                session_factory,
                batch_id=batch_id,
                snapshot=current_snapshot,
                resolved_at=now,
            )
        )

    for batch in batches:
        try:
            if batch.status == "blocked" and batch.reason_code in TEMPORARY_VISIBILITY_REASONS:
                if contract_spec_provider is None:
                    counts["skipped"] += 1
                    continue
                result = plan_strategy_management_batch(
                    session_factory,
                    raw_message_id=batch.raw_message_id,
                    deepcoin_client=get_client(),
                    contract_spec_provider=contract_spec_provider,
                    planned_at=now,
                    execution_mode=batch.execution_mode,
                )
                if result.status == "blocked" and result.reason_code in TEMPORARY_VISIBILITY_REASONS:
                    _advance_temporary_visibility_retry(session_factory, batch_id=batch.id, now=now)
                counts["recovered"] += 1
                continue
            if (
                batch.status == "recovery_required"
                and batch.reason_code == "deferred_entry_cancel_race_detected"
            ):
                current_snapshot = get_snapshot()
                binding_reconciler(
                    session_factory,
                    client=get_client(),
                    recovered_at=now,
                    snapshot=current_snapshot,
                )
                resolve_race_successor(batch_id=batch.id, current_snapshot=current_snapshot)
                counts["recovered"] += 1
                continue
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
                if batch.status == "ready" and batch.effective_action in _CLOSE_ACTIONS:
                    try:
                        restart_validator(
                            session_factory,
                            batch_id=batch.id,
                            snapshot=snapshot_loader(
                                session_factory, client=get_client()
                            ),
                        )
                    except ManagementBatchExecutionError as exc:
                        if not str(exc).startswith("restart_snapshot"):
                            raise
                        _freeze_restart_snapshot_failure(
                            session_factory, batch_id=batch.id, frozen_at=now
                        )
                        counts["recovered"] += 1
                        continue
                    except Exception:
                        _freeze_restart_snapshot_failure(
                            session_factory, batch_id=batch.id, frozen_at=now
                        )
                        counts["recovered"] += 1
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
            if _is_composite_protection_phase(batch):
                if not allow_execution:
                    counts["skipped"] += 1
                    continue
                current_snapshot = get_snapshot()
                executor(
                    session_factory,
                    batch_id=batch.id,
                    deepcoin_client=get_client(),
                    executed_at=now,
                )
                counts["recovered"] += 1
                continue
            if batch.status != "executing" or _has_submission_evidence(batch):
                current_snapshot = get_snapshot()
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
                current_snapshot = snapshot_loader(
                    session_factory, client=get_client()
                )
                restart_validator(
                    session_factory,
                    batch_id=batch.id,
                    snapshot=current_snapshot,
                )
            except ManagementBatchExecutionError as exc:
                if _is_deferred_entry_set_failure(batch, exc):
                    _persist_deferred_entry_cancel_preflight_failure(
                        session_factory, batch_id=batch.id, failed_at=now
                    )
                else:
                    _freeze_restart_snapshot_failure(
                        session_factory, batch_id=batch.id, frozen_at=now
                    )
                counts["recovered"] += 1
                continue
            except Exception:
                _freeze_restart_snapshot_failure(
                    session_factory, batch_id=batch.id, frozen_at=now
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
        except ManagementBatchExecutionError as exc:
            counts["failed"] += 1
            if _is_deferred_entry_set_failure(batch, exc):
                _persist_deferred_entry_cancel_preflight_failure(
                    session_factory, batch_id=batch.id, failed_at=now
                )
            else:
                _persist_deterministic_pre_submit_failure(
                    session_factory, batch_id=batch.id, failed_at=now
                )
            logger.exception("strategy management batch %s failed", batch.id)
        except Exception:
            counts["failed"] += 1
            logger.exception("strategy management batch %s failed", batch.id)
    return StrategyManagementWorkerResult(**counts)


def _advance_temporary_visibility_retry(session_factory, *, batch_id: int, now: datetime) -> None:
    """Persist bounded backoff; a stopped retry is never converted into a trade."""

    delays = (5, 15, 30, 60, 120)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if batch is None or batch.status != "blocked" or batch.reason_code not in TEMPORARY_VISIBILITY_REASONS:
            return
        first = batch.visibility_first_failed_at or batch.planned_at
        if first.tzinfo is None:
            first = first.replace(tzinfo=UTC)
        if now >= first + timedelta(minutes=5):
            batch.visibility_next_attempt_at = None
            batch.reason_code = "protection_visibility_retry_expired"
            batch.notification_state = "pending"
            batch.notification_fingerprint = None
            batch.updated_at = now
            session.commit()
            return
        attempts = max(1, int(batch.visibility_retry_attempts or 1)) + 1
        delay = delays[min(attempts - 1, len(delays) - 1)]
        batch.visibility_retry_attempts = attempts
        batch.visibility_next_attempt_at = now + timedelta(seconds=delay)
        batch.updated_at = now
        session.commit()


async def run_strategy_management_worker_loop(
    *,
    session_factory,
    deepcoin_client_factory,
    interval_seconds: float = 5.0,
    max_batches: int = 10,
    now_provider=None,
    contract_spec_provider=None,
) -> None:
    """Run bounded ticks forever; cancellation is owned by the Web lifespan."""

    cursor = StrategyManagementWorkerCursor()
    while True:
        try:
            settings = load_trading_settings(session_factory)
            run_strategy_management_worker_tick(
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
                max_batches=max_batches,
                allow_execution=settings.live_management_execution_enabled,
                cursor=cursor,
                processed_at=(
                    now_provider() if now_provider is not None else datetime.now(UTC)
                ),
                contract_spec_provider=contract_spec_provider,
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


def _persist_deferred_entry_cancel_preflight_failure(
    session_factory, *, batch_id: int, failed_at: datetime
) -> bool:
    return transition_batch(
        session_factory,
        int(batch_id),
        expected_statuses={"executing"},
        new_status="recovery_required",
        transitioned_at=failed_at,
        reason_code="deferred_entry_cancel_preflight_failed",
    )


def _is_deferred_entry_set_failure(
    batch: ManagementBatchRecord, exc: ManagementBatchExecutionError
) -> bool:
    return bool(
        batch.effective_action in {"full_close", "full_exit"}
        and str(exc) == "batch_entry_set_not_exact"
    )


def _freeze_restart_snapshot_failure(
    session_factory, *, batch_id: int, frozen_at: datetime
) -> None:
    if not transition_batch(
        session_factory,
        int(batch_id),
        expected_statuses={"executing"},
        new_status="recovery_required",
        transitioned_at=frozen_at,
        reason_code="management_restart_snapshot_validation_failed",
    ):
        raise RuntimeError("management_restart_snapshot_freeze_conflict")


def _resolve_deferred_entry_cancel_race_successor(
    session_factory, *, batch_id: int, snapshot: Any, resolved_at: datetime
) -> bool:
    """Atomically replace a proven cancellation race with an exact close batch."""

    try:
        parent = load_management_batch(session_factory, int(batch_id))
        if (
            parent.status != "recovery_required"
            or parent.reason_code != "deferred_entry_cancel_race_detected"
            or not isinstance(parent.target_snapshot, dict)
        ):
            return False
        identity = parent.target_snapshot.get("identity")
        contract_spec = parent.target_snapshot.get("contract_spec")
        if not isinstance(identity, dict) or not isinstance(contract_spec, dict):
            return False
        deferred_ids = {
            int(value)
            for value in identity.get("deferred_entry_leg_ids", [])
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
        }
        expected_leg_ids = deferred_ids | {
            int(leg.execution_order_leg_id) for leg in parent.legs
        }
        if not deferred_ids or not expected_leg_ids:
            return False
        quantity_step = str(contract_spec["quantity_step"])
        min_quantity = str(contract_spec["min_quantity"])
        with session_factory() as session:
            binding = session.get(ExecutionBinding, parent.execution_binding_id)
            if (
                binding is None
                or binding.strategy_instance_id != parent.strategy_instance_id
                or str(binding.venue or "").lower() != "deepcoin"
            ):
                return False
            entries = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
                .filter(ExecutionOrderLeg.purpose == "entry")
                .all()
            )
            if {int(entry.id) for entry in entries} != expected_leg_ids:
                return False
            entries_by_id = {int(entry.id): entry for entry in entries}
            ordered_entries = [entries_by_id[leg_id] for leg_id in sorted(expected_leg_ids)]
            for entry in ordered_entries:
                if (
                    entry.strategy_instance_id != parent.strategy_instance_id
                    or str(entry.status or "").lower() != "active"
                    or entry.terminal_reason is not None
                    or not str(entry.pos_id or "")
                ):
                    return False
                owner = require_verified_position_ownership(
                    session, venue="deepcoin", pos_id=str(entry.pos_id)
                )
                if int(owner.id) != int(entry.id):
                    return False
                require_equivalent_live_position_economics(
                    owner, live_positions=snapshot.positions, session=session
                )
            economics = canonical_live_position_economics(
                snapshot.positions,
                target_pos_ids=[str(entry.pos_id) for entry in ordered_entries],
                instrument_id=f"{str(binding.symbol).upper()}-USDT-SWAP",
                side=str(binding.side),
            )
        close_sizes = allocate_close_sizes(
            (position["size"] for position in economics),
            fraction=1.0,
            quantity_step=quantity_step,
            min_quantity=min_quantity,
        )
    except (KeyError, TypeError, ValueError, PositionAttributionError, ManagementSizingError):
        return False
    except Exception:
        logger.exception("deferred entry cancel race resolution failed for batch %s", batch_id)
        return False

    positions_by_id = {position["pos_id"]: position for position in economics}
    close_sizes_by_pos_id = {
        position["pos_id"]: close_size
        for position, close_size in zip(economics, close_sizes)
    }
    successor_snapshot = {
        "execution_mode": parent.execution_mode,
        "identity": {
            "target_lifecycle_id": parent.target_lifecycle_id,
            "execution_binding_id": parent.execution_binding_id,
            "strategy_instance_id": parent.strategy_instance_id,
            "manageable_entry_leg_ids": [int(entry.id) for entry in ordered_entries],
            "deferred_entry_leg_ids": [],
        },
        "positions": [
            {**positions_by_id[str(entry.pos_id)], "execution_order_leg_id": int(entry.id)}
            for entry in ordered_entries
        ],
        "deferred_entry_legs": [],
        "contract_spec": contract_spec,
        "protection": {},
    }
    legs = [
        ManagementLegCreate(
            execution_order_leg_id=int(entry.id),
            pos_id=str(entry.pos_id),
            leg_index=index,
            preflight_size=positions_by_id[str(entry.pos_id)]["size"],
            planned_close_size=close_sizes_by_pos_id[str(entry.pos_id)],
            avg_entry_price=positions_by_id[str(entry.pos_id)]["avg_entry_price"],
            quantity_step=quantity_step,
            last_exchange_snapshot=positions_by_id[str(entry.pos_id)],
        )
        for index, entry in enumerate(ordered_entries)
    ]
    try:
        create_race_resolved_successor_batch(
            session_factory,
            parent_batch_id=parent.id,
            resolved_position_ids=[str(entry.pos_id) for entry in ordered_entries],
            target_snapshot=successor_snapshot,
            legs=legs,
            planned_at=resolved_at,
        )
    except Exception:
        try:
            return load_management_batch(session_factory, parent.id).status == "resolved"
        except LookupError:
            return False
    return True
