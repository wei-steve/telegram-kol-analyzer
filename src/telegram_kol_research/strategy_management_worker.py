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
from telegram_kol_research.runtime_worker_executor import (
    run_on_management_worker,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    ManagementBatchRecord,
    TEMPORARY_VISIBILITY_REASONS,
    claim_worker_batch,
    create_capability_deferred_successor_batch,
    create_race_resolved_successor_batch,
    list_worker_batches,
    load_management_batch,
    transition_batch,
)
from telegram_kol_research.strategy_management_executor import (
    _MANAGEABLE_ENTRY_LEG_STATES,
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
from telegram_kol_research.instruction_execution_outcomes import (
    InstructionOutcomeContractError,
    legacy_status_for_instruction_result,
)
from telegram_kol_research.instruction_execution_projection import (
    instruction_execution_mode_for_item,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    ManagementMessageTarget,
    MessageInstructionItem,
    PositionMutationIntent,
    StrategyLifecycle,
    StrategyManagementBatch,
    SignalCandidate,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_management_target_failure,
    capture_runtime_incident_best_effort,
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
from telegram_kol_research.strategy_management_composite_reconciliation import (
    has_recoverable_composite_components,
    reconcile_composite_management_components,
)
from telegram_kol_research.strategy_management_composite_executor import (
    execute_composite_management_batch,
)


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
    capability_successor_resolver: Callable[..., bool] | None = None,
    restart_validator: Callable[..., None] = validate_management_restart_snapshot,
    cursor: StrategyManagementWorkerCursor | None = None,
    contract_spec_provider=None,
    take_profit_convergence_runner: Callable[..., int] = execute_ready_trigger_take_profit_convergences,
    instruction_planner: Callable[..., Any] = plan_strategy_management_batch,
    composite_reconciler: Callable[..., Any] = reconcile_composite_management_components,
    composite_executor: Callable[..., Any] = execute_composite_management_batch,
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
    try:
        from telegram_kol_research.instruction_execution_management_adapter import (
            converge_unknown_management_instruction_contracts,
        )

        converge_unknown_management_instruction_contracts(
            session_factory,
            converged_at=now,
            limit=limit,
        )
    except Exception:
        logger.exception("unknown management execution-contract convergence failed")
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

    should_reconcile_composite = (
        composite_reconciler is not reconcile_composite_management_components
        or (
            callable(session_factory)
            and has_recoverable_composite_components(session_factory)
        )
    )
    if should_reconcile_composite:
        try:
            composite_reconciler(
                session_factory,
                deepcoin_client=get_client(),
                reconciled_at=now,
                allow_new_writes=bool(allow_execution),
            )
        except Exception:
            logger.exception("composite management reconciliation failed")

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
            enforcement_mode = "disabled"
            planning_result = None
            try:
                execution_settings = load_trading_settings(session_factory)
                enforcement_mode = instruction_execution_mode_for_item(
                    item,
                    execution_settings,
                )
                if enforcement_mode != "disabled":
                    with session_factory() as session:
                        parse_source = session.query(
                            SignalCandidate.parse_source
                        ).filter(
                            SignalCandidate.id == int(item.signal_candidate_id)
                        ).scalar()
                    if parse_source != "mimo_authoritative":
                        enforcement_mode = "disabled"
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
                    defer_status = defer_message_instruction_item_for_visibility(
                        session_factory,
                        item_id=item.id,
                        result={
                            "status": "deferred",
                            "reason": planning_result.reason_code,
                            "execution_mode": execution_mode,
                        },
                        now=now,
                    )
                    if defer_status == "failed":
                        _capture_committed_instruction_target_failure(
                            session_factory,
                            item_id=item.id,
                            incident_type="management_target_visibility_exhausted",
                            reason_code=(
                                "target_strategy_binding_visibility_retry_expired"
                            ),
                            severity="high",
                            occurred_at=now,
                        )
                    counts["recovered"] += 1
                    continue
                if planning_result.status != "ready" or planning_result.batch is None:
                    if (
                        enforcement_mode != "disabled"
                        and planning_result.batch_id is not None
                    ):
                        from telegram_kol_research.instruction_execution_management_adapter import (
                            project_management_instruction_contract,
                        )

                        project_management_instruction_contract(
                            session_factory,
                            message_instruction_item_id=int(item.id),
                            management_batch_id=int(planning_result.batch_id),
                            projected_at=now,
                            mode=enforcement_mode,
                        )
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
                        execution_contract_mode=enforcement_mode,
                    )
                    _capture_committed_instruction_target_failure(
                        session_factory,
                        item_id=item.id,
                        incident_type="management_target_orchestration_failed",
                        reason_code=(
                            planning_result.reason_code
                            or "management_planning_failed"
                        ),
                        severity="high",
                        occurred_at=now,
                    )
                    counts["failed"] += 1
                    continue
                if execution_mode == "shadow":
                    shadow_result = {
                        "status": "shadow_planned",
                        "batch_id": planning_result.batch.id,
                    }
                    if enforcement_mode != "disabled":
                        from telegram_kol_research.instruction_execution_management_adapter import (
                            project_management_instruction_contract,
                        )

                        project_management_instruction_contract(
                            session_factory,
                            message_instruction_item_id=int(item.id),
                            management_batch_id=int(planning_result.batch.id),
                            projected_at=now,
                            mode=enforcement_mode,
                        )
                    finish_message_instruction_item(
                        session_factory,
                        item_id=item.id,
                        status=legacy_status_for_instruction_result(
                            shadow_result,
                            intent_kind="management",
                            enforcement_mode=enforcement_mode,
                        ),
                        result=shadow_result,
                        now=now,
                        execution_contract_mode=enforcement_mode,
                    )
                    counts["recovered"] += 1
                    continue
                execution_result = executor(
                    session_factory,
                    batch_id=planning_result.batch.id,
                    deepcoin_client=get_client(),
                    executed_at=now,
                )
                if enforcement_mode != "disabled":
                    from telegram_kol_research.instruction_execution_management_adapter import (
                        project_management_instruction_contract,
                    )

                    project_management_instruction_contract(
                        session_factory,
                        message_instruction_item_id=int(item.id),
                        management_batch_id=int(planning_result.batch.id),
                        projected_at=now,
                        mode=enforcement_mode,
                    )
                try:
                    finish_status = legacy_status_for_instruction_result(
                        execution_result,
                        intent_kind="management",
                        enforcement_mode=enforcement_mode,
                    )
                except InstructionOutcomeContractError as exc:
                    finish_status = "failed"
                    execution_result = {
                        "status": "failed",
                        "reason": "instruction_outcome_contract_invalid",
                        "observed_status": str(
                            execution_result.get("status") or ""
                        )[:64],
                        "error": str(exc)[:256],
                    }
                finish_message_instruction_item(
                    session_factory,
                    item_id=item.id,
                    status=finish_status,
                    result=execution_result,
                    now=now,
                    execution_contract_mode=enforcement_mode,
                )
                if finish_status == "failed":
                    _capture_committed_instruction_target_failure(
                        session_factory,
                        item_id=item.id,
                        incident_type="management_target_orchestration_failed",
                        reason_code=str(
                            execution_result.get("reason")
                            or execution_result.get("status")
                            or "management_execution_failed"
                        ),
                        severity="high",
                        occurred_at=now,
                    )
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
                    execution_contract_mode=enforcement_mode,
                )
                _capture_committed_instruction_target_failure(
                    session_factory,
                    item_id=item.id,
                    incident_type="management_target_orchestration_failed",
                    reason_code=type(exc).__name__,
                    severity="critical",
                    occurred_at=now,
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
                    execution_contract_mode=enforcement_mode,
                )
                _capture_committed_instruction_target_failure(
                    session_factory,
                    item_id=item.id,
                    incident_type="management_target_orchestration_failed",
                    reason_code=type(exc).__name__,
                    severity="high",
                    occurred_at=now,
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

    def resolve_capability_successor(
        *, batch_id: int, current_snapshot: Any
    ) -> bool:
        if capability_successor_resolver is None:
            return _resolve_capability_deferred_successor(
                session_factory,
                batch_id=batch_id,
                snapshot=current_snapshot,
                resolved_at=now,
            )
        return bool(
            capability_successor_resolver(
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
            if batch.management_contract_json and batch.status == "executing":
                if not allow_execution:
                    counts["skipped"] += 1
                    continue
                settings = load_trading_settings(session_factory)
                composite_executor(
                    session_factory,
                    batch_id=batch.id,
                    deepcoin_client=get_client(),
                    contract_spec_provider=contract_spec_provider,
                    live_execution_gate=lambda: (
                        load_trading_settings(
                            session_factory
                        ).effective_composite_management_v2_mode
                        == "live"
                    ),
                    now_provider=lambda: now,
                    backup_buffer_bps=str(
                        settings.trigger_backup_stop_buffer_bps
                    ),
                )
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
            if (
                batch.status == "reconciling"
                and batch.reason_code
                == "management_subset_close_exchange_confirmed"
            ):
                current_snapshot = get_snapshot()
                reconciler(
                    session_factory,
                    snapshot=current_snapshot,
                    reconciled_at=now,
                    batch_ids=(batch.id,),
                )
                refreshed = load_management_batch(session_factory, batch.id)
                if (
                    refreshed.status == "reconciling"
                    and refreshed.reason_code
                    == "management_subset_close_exchange_confirmed"
                ):
                    resolve_capability_successor(
                        batch_id=batch.id,
                        current_snapshot=current_snapshot,
                    )
                counts["recovered"] += 1
                continue
            if (
                batch.status == "recovery_required"
                and "protection" in str(batch.reason_code or "")
            ):
                reconciler(
                    session_factory,
                    snapshot=get_snapshot(),
                    reconciled_at=now,
                    batch_ids={batch.id},
                )
                refreshed = load_management_batch(
                    session_factory, batch.id
                )
                if refreshed.status != "recovery_required":
                    counts["recovered"] += 1
                else:
                    counts["paused"] += 1
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
                if batch.management_contract_json:
                    settings = load_trading_settings(session_factory)
                    composite_executor(
                        session_factory,
                        batch_id=batch.id,
                        deepcoin_client=get_client(),
                        contract_spec_provider=contract_spec_provider,
                        live_execution_gate=lambda: (
                            load_trading_settings(
                                session_factory
                            ).effective_composite_management_v2_mode
                            == "live"
                        ),
                        now_provider=lambda: now,
                        backup_buffer_bps=str(
                            settings.trigger_backup_stop_buffer_bps
                        ),
                    )
                    counts["executed"] += 1
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
        finally:
            _synchronize_linked_management_item_best_effort(
                session_factory,
                batch_id=int(batch.id),
                synchronized_at=now,
            )
    return StrategyManagementWorkerResult(**counts)


def _synchronize_linked_management_item_best_effort(
    session_factory,
    *,
    batch_id: int,
    synchronized_at: datetime,
) -> None:
    """Converge a linked item from durable evidence without retrying a writer."""

    try:
        from telegram_kol_research.instruction_execution_management_adapter import (
            project_linked_management_batch_contract,
        )

        linked = project_linked_management_batch_contract(
            session_factory,
            management_batch_id=int(batch_id),
            projected_at=synchronized_at,
        )
        if (
            linked is None
            or linked.mode != "live"
            or linked.contract.state not in {
                "submitting",
                "submit_unknown",
                "verified",
                "failed",
                "expired",
            }
        ):
            return
        with session_factory() as session:
            item_status = session.query(MessageInstructionItem.status).filter(
                MessageInstructionItem.id
                == int(linked.message_instruction_item_id)
            ).scalar()
        if item_status not in {"executing", "unknown"}:
            return
        finish_message_instruction_item(
            session_factory,
            item_id=int(linked.message_instruction_item_id),
            status="executing",
            result={
                "status": {
                    "submitting": "reconciling",
                    "submit_unknown": "submit_unknown",
                    "verified": "succeeded",
                    "failed": "failed",
                    "expired": "expired",
                }[linked.contract.state],
                "reason": "durable_management_reconciled",
                "batch_id": int(batch_id),
            },
            now=synchronized_at,
            execution_contract_mode=linked.mode,
            expected_current_statuses=("executing", "unknown"),
        )
    except Exception:
        logger.exception(
            "management execution-contract synchronization failed: batch_id=%s",
            int(batch_id),
        )


def _capture_committed_instruction_target_failure(
    session_factory,
    *,
    item_id: int,
    incident_type: str,
    reason_code: str,
    severity: str,
    occurred_at: datetime,
) -> None:
    """Capture only after the instruction and target state transaction commits."""

    try:
        with session_factory() as session:
            target_id = (
                session.query(ManagementMessageTarget.id)
                .filter(
                    ManagementMessageTarget.message_instruction_item_id
                    == int(item_id)
                )
                .scalar()
            )
    except Exception as exc:
        logger.warning(
            "Management target incident discovery failed open: item_id=%s error=%s",
            int(item_id),
            type(exc).__name__,
        )
        return
    if target_id is None:
        return
    capture_runtime_incident_best_effort(
        capture_management_target_failure,
        session_factory,
        target_id=int(target_id),
        incident_type=incident_type,
        reason_code=reason_code,
        severity=severity,
        occurred_at=occurred_at,
    )


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


def _load_settings_and_run_strategy_management_tick(
    session_factory,
    *,
    deepcoin_client_factory,
    max_batches: int,
    cursor: StrategyManagementWorkerCursor,
    now_provider=None,
    contract_spec_provider=None,
    authority_observer=None,
) -> None:
    """Run one settings read plus one tick as a single blocking unit.

    Both are blocking, and they are submitted together so they stay on the same
    thread and the pairing remains atomic, exactly as it was when both ran
    inline on the event loop.
    """

    settings = load_trading_settings(session_factory)
    observed_at = now_provider() if now_provider is not None else datetime.now(UTC)
    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=deepcoin_client_factory,
        max_batches=max_batches,
        allow_execution=settings.live_management_execution_enabled,
        cursor=cursor,
        processed_at=observed_at,
        contract_spec_provider=contract_spec_provider,
    )
    if authority_observer is not None:
        authority_observer(
            management_enabled=settings.live_management_execution_enabled,
            rescue_enabled=(
                settings.effective_trigger_protection_stop_rescue_mode == "live"
            ),
            observed_at=observed_at,
        )


async def run_strategy_management_worker_loop(
    *,
    session_factory,
    deepcoin_client_factory,
    interval_seconds: float = 5.0,
    max_batches: int = 10,
    now_provider=None,
    contract_spec_provider=None,
    authority_observer=None,
    authority_failure_observer=None,
) -> None:
    """Run bounded ticks forever; cancellation is owned by the Web lifespan."""

    cursor = StrategyManagementWorkerCursor()
    while True:
        try:
            await run_on_management_worker(
                _load_settings_and_run_strategy_management_tick,
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
                max_batches=max_batches,
                cursor=cursor,
                now_provider=now_provider,
                contract_spec_provider=contract_spec_provider,
                authority_observer=authority_observer,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("strategy management worker tick failed")
            if authority_failure_observer is not None:
                authority_failure_observer(
                    observed_at=(
                        now_provider() if now_provider is not None else datetime.now(UTC)
                    )
                )
        await asyncio.sleep(max(0.01, float(interval_seconds)))


def _has_submission_evidence(batch: ManagementBatchRecord) -> bool:
    return any(
        str(leg.status or "")
        in {"reserved", "submitted", "submit_unknown", "partial", "confirmed"}
        for leg in batch.legs
    )


def _is_composite_protection_phase(batch: ManagementBatchRecord) -> bool:
    if batch.effective_action == "break_even_by_market":
        return batch.status == "executing"
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


def _resolve_capability_deferred_successor(
    session_factory, *, batch_id: int, snapshot: Any, resolved_at: datetime
) -> bool:
    """Plan the still-open exact subset after its mutation barrier clears."""

    try:
        if (
            load_trading_settings(
                session_factory
            ).effective_position_management_liveness_v2_mode
            != "live"
        ):
            return False
        parent = load_management_batch(session_factory, int(batch_id))
        if (
            parent.status != "reconciling"
            or parent.reason_code
            != "management_subset_close_exchange_confirmed"
            or parent.effective_action not in {"full_close", "full_exit"}
            or not isinstance(parent.target_snapshot, dict)
            or getattr(snapshot, "errors", None)
        ):
            return False
        identity = parent.target_snapshot.get("identity")
        contract_spec = parent.target_snapshot.get("contract_spec")
        if not isinstance(identity, dict) or not isinstance(contract_spec, dict):
            return False
        deferred_ids = {
            int(value)
            for value in identity.get(
                "capability_deferred_entry_leg_ids", []
            )
            if isinstance(value, int)
            or (isinstance(value, str) and value.isdigit())
        }
        deferred_pos_ids = {
            str(value)
            for value in identity.get("capability_deferred_pos_ids", [])
            if isinstance(value, str) and value.strip()
        }
        if not deferred_ids or not deferred_pos_ids:
            return False
        quantity_step = str(contract_spec["quantity_step"])
        min_quantity = str(contract_spec["min_quantity"])
        with session_factory() as session:
            binding = session.get(ExecutionBinding, parent.execution_binding_id)
            if (
                binding is None
                or binding.strategy_instance_id != parent.strategy_instance_id
                or str(binding.venue or "").lower() != "deepcoin"
                or str(binding.status or "").lower()
                not in {"active", "open", "partial"}
            ):
                return False
            entries = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.id.in_(deferred_ids))
                .all()
            )
            if {int(entry.id) for entry in entries} != deferred_ids:
                return False
            entries_by_id = {int(entry.id): entry for entry in entries}
            ordered_entries = [
                entries_by_id[leg_id] for leg_id in sorted(deferred_ids)
            ]
            if {
                str(entry.pos_id) for entry in ordered_entries if entry.pos_id
            } != deferred_pos_ids:
                return False
            bound_pos_ids = {
                value.strip()
                for value in str(binding.pos_id or "").split(",")
                if value.strip()
            }
            if bound_pos_ids != deferred_pos_ids:
                return False
            unresolved = (
                session.query(PositionMutationIntent.id)
                .filter(PositionMutationIntent.venue == "deepcoin")
                .filter(PositionMutationIntent.pos_id.in_(deferred_pos_ids))
                .filter(
                    PositionMutationIntent.status.in_(
                        (
                            "reserved",
                            "submitted",
                            "submit_unknown",
                            "recovery_required",
                        )
                    )
                )
                .first()
            )
            if unresolved is not None:
                return False
            live_rows_by_pos_id: dict[str, list[dict[str, Any]]] = {
                pos_id: [] for pos_id in deferred_pos_ids
            }
            for row in getattr(snapshot, "positions", ()):
                if not isinstance(row, dict):
                    continue
                pos_id = str(
                    row.get("posId")
                    or row.get("pos_id")
                    or row.get("id")
                    or ""
                )
                if pos_id in live_rows_by_pos_id:
                    live_rows_by_pos_id[pos_id].append(row)
            if any(
                len(rows) > 1 for rows in live_rows_by_pos_id.values()
            ):
                return False
            gone_pos_ids = {
                pos_id
                for pos_id, rows in live_rows_by_pos_id.items()
                if not rows
            }
            live_pos_ids = deferred_pos_ids - gone_pos_ids
            entries_by_pos_id = {
                str(entry.pos_id): entry for entry in ordered_entries
            }
            for pos_id in gone_pos_ids:
                entry = entries_by_pos_id[pos_id]
                if (
                    entry.execution_binding_id != binding.id
                    or entry.strategy_instance_id
                    != parent.strategy_instance_id
                    or str(entry.status or "").lower()
                    not in _MANAGEABLE_ENTRY_LEG_STATES
                    or entry.attribution_status != "verified"
                    or entry.terminal_reason is not None
                ):
                    return False
                confirmed_close = (
                    session.query(PositionMutationIntent.id)
                    .filter(
                        PositionMutationIntent.venue == "deepcoin",
                        PositionMutationIntent.operation == "close_position",
                        PositionMutationIntent.strategy_instance_id
                        == parent.strategy_instance_id,
                        PositionMutationIntent.execution_binding_id
                        == binding.id,
                        PositionMutationIntent.execution_order_leg_id
                        == entry.id,
                        PositionMutationIntent.pos_id == pos_id,
                        PositionMutationIntent.status == "confirmed",
                    )
                    .first()
                )
                if confirmed_close is None:
                    return False
            live_entries = [
                entry
                for entry in ordered_entries
                if str(entry.pos_id) in live_pos_ids
            ]
            for entry in live_entries:
                if (
                    entry.execution_binding_id != binding.id
                    or entry.strategy_instance_id != parent.strategy_instance_id
                    or str(entry.status or "").lower()
                    not in _MANAGEABLE_ENTRY_LEG_STATES
                    or entry.attribution_status != "verified"
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
                    owner,
                    live_positions=snapshot.positions,
                    session=session,
                )
            if not live_entries:
                lifecycle = session.get(
                    StrategyLifecycle, parent.target_lifecycle_id
                )
                stored_parent = session.get(
                    StrategyManagementBatch, parent.id
                )
                if lifecycle is None or stored_parent is None:
                    return False
                for pos_id in gone_pos_ids:
                    entry = entries_by_pos_id[pos_id]
                    entry.status = "closed"
                    entry.terminal_reason = (
                        "position_mutation_close_confirmed"
                    )
                    entry.last_verified_at = resolved_at
                    entry.updated_at = resolved_at
                binding.pos_id = None
                binding.status = "closed"
                binding.last_exchange_status = (
                    "management_full_close_confirmed"
                )
                binding.recovered_at = resolved_at
                lifecycle.lifecycle_status = "exited"
                lifecycle.exit_reason = "kol_signal"
                lifecycle.exited_at = resolved_at
                lifecycle.management_action = "full_close_confirmed"
                lifecycle.updated_at = resolved_at
                stored_parent.status = "succeeded"
                stored_parent.reason_code = (
                    "management_full_close_exchange_confirmed"
                )
                stored_parent.reconciled_at = resolved_at
                stored_parent.completed_at = resolved_at
                stored_parent.updated_at = resolved_at
                session.commit()
                return True
            ordered_entries = live_entries
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
    except (
        KeyError,
        TypeError,
        ValueError,
        PositionAttributionError,
        ManagementSizingError,
    ):
        return False
    except Exception:
        logger.exception(
            "capability-deferred resolution failed for batch %s", batch_id
        )
        return False

    positions_by_id = {
        position["pos_id"]: position for position in economics
    }
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
            "manageable_entry_leg_ids": [
                int(entry.id) for entry in ordered_entries
            ],
            "deferred_entry_leg_ids": [],
            "capability_deferred_entry_leg_ids": [],
            "capability_deferred_pos_ids": [],
        },
        "positions": [
            {
                **positions_by_id[str(entry.pos_id)],
                "execution_order_leg_id": int(entry.id),
            }
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
            avg_entry_price=positions_by_id[str(entry.pos_id)][
                "avg_entry_price"
            ],
            quantity_step=quantity_step,
            last_exchange_snapshot=positions_by_id[str(entry.pos_id)],
        )
        for index, entry in enumerate(ordered_entries)
    ]
    try:
        create_capability_deferred_successor_batch(
            session_factory,
            parent_batch_id=parent.id,
            resolved_position_ids=[
                str(entry.pos_id) for entry in ordered_entries
            ],
            target_snapshot=successor_snapshot,
            legs=legs,
            terminalized_entry_leg_ids=[
                int(entries_by_pos_id[pos_id].id)
                for pos_id in sorted(gone_pos_ids)
            ],
            remaining_binding_pos_ids=[
                str(entry.pos_id) for entry in ordered_entries
            ],
            planned_at=resolved_at,
        )
    except Exception:
        logger.exception(
            "capability-deferred successor creation failed for batch %s",
            batch_id,
        )
        return False
    return True
