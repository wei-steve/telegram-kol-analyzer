"""Crash-safe orchestration for deleted Telegram strategy sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import or_, update

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TelegramSourceMessageEvent,
    TradeSignal,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.source_message_deletion import (
    source_message_execution_authority,
)
from telegram_kol_research.terminal_entry_cleanup import (
    cleanup_terminal_entry_legs,
)


_ACTIVE_STATES = (
    "pending",
    "cancelling_entries",
    "closing_positions",
    "reconciling",
)


@dataclass(frozen=True, slots=True)
class SourceMessageDeletionWorkerResult:
    discovered: int = 0
    cancelled: int = 0
    planned_exits: int = 0
    finalized: int = 0
    waiting: int = 0
    recovery_required: int = 0


def run_source_message_deletion_worker_tick(
    session_factory,
    *,
    deepcoin_client_factory,
    contract_spec_provider=None,
    max_jobs: int = 10,
    processed_at: datetime | None = None,
    cleanup_executor=cleanup_terminal_entry_legs,
    exit_planner=None,
    snapshot_loader=None,
    binding_reconciler=None,
) -> SourceMessageDeletionWorkerResult:
    """Process a bounded number of exact deleted-source jobs."""

    now = processed_at or datetime.now(UTC)
    counts = {
        "discovered": 0,
        "cancelled": 0,
        "planned_exits": 0,
        "finalized": 0,
        "waiting": 0,
        "recovery_required": 0,
    }
    client = None
    processed_exit_ids: set[int] = set()

    def get_client():
        nonlocal client
        if client is None:
            client = deepcoin_client_factory()
        return client

    for _ in range(max(0, int(max_jobs))):
        claim = _claim_next_job(
            session_factory,
            claimed_at=now,
            excluded_exit_ids=processed_exit_ids,
        )
        if claim is None:
            break
        counts["discovered"] += 1
        exit_id, state, claim_token = claim
        processed_exit_ids.add(exit_id)
        if state == "reconciling":
            if snapshot_loader is None:
                from telegram_kol_research.execution_bindings import (
                    load_deepcoin_execution_reconciliation_snapshot,
                )

                loader = load_deepcoin_execution_reconciliation_snapshot
            else:
                loader = snapshot_loader
            try:
                with position_authority_lock():
                    snapshot = loader(session_factory, client=get_client())
                    if binding_reconciler is None:
                        from telegram_kol_research.execution_bindings import (
                            reconcile_deepcoin_execution_bindings,
                        )

                        reconcile_bindings = reconcile_deepcoin_execution_bindings
                    else:
                        reconcile_bindings = binding_reconciler
                    reconcile_bindings(
                        session_factory,
                        client=get_client(),
                        recovered_at=now,
                        snapshot=snapshot,
                    )
                    final_state = finalize_source_message_deletion_exit(
                        session_factory,
                        deletion_exit_id=exit_id,
                        snapshot=snapshot,
                        finalized_at=now,
                        expected_claim_token=claim_token,
                    )
            except Exception as exc:
                _transition_claimed(
                    session_factory,
                    exit_id=exit_id,
                    claim_token=claim_token,
                    new_state="reconciling",
                    reason="flat_reconciliation_retry",
                    error=f"flat_reconciliation_exception:{type(exc).__name__}:{exc}",
                    updated_at=now,
                )
                counts["waiting"] += 1
                continue
            if final_state == "succeeded":
                counts["finalized"] += 1
            elif final_state == "recovery_required":
                counts["recovery_required"] += 1
            else:
                counts["waiting"] += 1
            continue

        if state == "closing_positions":
            with session_factory() as session:
                deletion_exit = session.get(SourceMessageDeletionExit, exit_id)
                management_batch_id = (
                    int(deletion_exit.management_batch_id)
                    if deletion_exit is not None
                    and deletion_exit.management_batch_id is not None
                    else None
                )
                batch = (
                    session.get(StrategyManagementBatch, management_batch_id)
                    if management_batch_id is not None
                    else None
                )
            if batch is not None:
                if batch.status == "succeeded":
                    _transition_claimed(
                        session_factory,
                        exit_id=exit_id,
                        claim_token=claim_token,
                        new_state="reconciling",
                        reason="position_exit_exchange_confirmed",
                        updated_at=now,
                    )
                elif batch.status in {
                    "blocked",
                    "partial_failed",
                    "submit_unknown",
                    "recovery_required",
                }:
                    _transition_claimed(
                        session_factory,
                        exit_id=exit_id,
                        claim_token=claim_token,
                        new_state="recovery_required",
                        reason="position_exit_batch_requires_recovery",
                        error=f"management_batch_{batch.status}",
                        updated_at=now,
                    )
                    counts["recovery_required"] += 1
                    continue
                else:
                    _release_claim(
                        session_factory,
                        exit_id=exit_id,
                        claim_token=claim_token,
                        state="closing_positions",
                        reason="position_exit_batch_in_progress",
                        updated_at=now,
                    )
                counts["waiting"] += 1
                continue
            if contract_spec_provider is None:
                _transition_claimed(
                    session_factory,
                    exit_id=exit_id,
                    claim_token=claim_token,
                    new_state="recovery_required",
                    reason="position_exit_contract_spec_missing",
                    error="position_exit_contract_spec_missing",
                    updated_at=now,
                )
                counts["recovery_required"] += 1
                continue
            if exit_planner is None:
                from telegram_kol_research.strategy_management_planner import (
                    plan_source_deletion_full_exit,
                )

                planner = plan_source_deletion_full_exit
            else:
                planner = exit_planner
            try:
                planning = planner(
                    session_factory,
                    deletion_exit_id=exit_id,
                    deepcoin_client=get_client(),
                    contract_spec_provider=contract_spec_provider,
                    planned_at=now,
                )
            except Exception as exc:
                _transition_claimed(
                    session_factory,
                    exit_id=exit_id,
                    claim_token=claim_token,
                    new_state="recovery_required",
                    reason="position_exit_planning_exception",
                    error=f"position_exit_planning_exception:{type(exc).__name__}:{exc}",
                    updated_at=now,
                )
                counts["recovery_required"] += 1
                continue
            if planning.status == "ready" and planning.batch is not None:
                _release_claim(
                    session_factory,
                    exit_id=exit_id,
                    claim_token=claim_token,
                    state="closing_positions",
                    reason="position_exit_planned",
                    updated_at=now,
                )
                counts["planned_exits"] += 1
                continue
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="position_exit_planning_blocked",
                error=f"position_exit_planning_blocked:{planning.reason_code}",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue

        if state != "cancelling_entries":
            _release_claim(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                state=state,
                reason="stage_not_implemented",
                updated_at=now,
            )
            counts["waiting"] += 1
            continue

        with session_factory() as session:
            deletion_exit = session.get(SourceMessageDeletionExit, exit_id)
            lifecycle_id = (
                int(deletion_exit.target_lifecycle_id)
                if deletion_exit is not None
                and deletion_exit.target_lifecycle_id is not None
                else None
            )
            binding_id = (
                int(deletion_exit.execution_binding_id)
                if deletion_exit is not None
                and deletion_exit.execution_binding_id is not None
                else None
            )
        if lifecycle_id is None:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="exact_lifecycle_missing",
                error="exact_lifecycle_missing",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue
        if binding_id is None:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="reconciling",
                reason="no_execution_binding",
                updated_at=now,
            )
            counts["waiting"] += 1
            continue

        try:
            cleanup = cleanup_executor(
                session_factory,
                lifecycle_id=lifecycle_id,
                deepcoin_client=get_client(),
                reason="source_message_deleted",
                cleaned_at=now,
                expected_binding_id=binding_id,
                allow_position_bound_remainder=True,
            )
        except Exception as exc:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="entry_cancel_exception",
                error=f"entry_cancel_exception:{type(exc).__name__}:{exc}",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue

        if cleanup.status not in {"resolved", "already_absent"}:
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason=f"entry_cancel_{cleanup.status}",
                error=f"entry_cancel_outcome_{cleanup.status}",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue
        if int(cleanup.binding_id) != int(binding_id):
            _transition_claimed(
                session_factory,
                exit_id=exit_id,
                claim_token=claim_token,
                new_state="recovery_required",
                reason="entry_cancel_binding_identity_changed",
                error="entry_cancel_binding_identity_changed",
                updated_at=now,
            )
            counts["recovery_required"] += 1
            continue

        signal_ids = _trade_signal_ids_for_events(
            session_factory,
            event_ids=cleanup.event_ids,
        )
        with session_factory() as session:
            owned_positions = (
                session.query(ExecutionOrderLeg.id)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == binding_id,
                    ExecutionOrderLeg.purpose == "entry",
                    ExecutionOrderLeg.pos_id.is_not(None),
                    ExecutionOrderLeg.attribution_status == "verified",
                )
                .first()
                is not None
            )
            if cleanup.leg_ids:
                (
                    session.query(ExecutionOrderLeg)
                    .filter(ExecutionOrderLeg.id.in_(cleanup.leg_ids))
                    .filter(ExecutionOrderLeg.pos_id.is_(None))
                    .update(
                        {
                            ExecutionOrderLeg.terminal_reason: (
                                "source_message_deleted_entry_cancelled"
                            )
                        },
                        synchronize_session=False,
                    )
                )
            session.commit()
        _transition_claimed(
            session_factory,
            exit_id=exit_id,
            claim_token=claim_token,
            new_state="closing_positions" if owned_positions else "reconciling",
            reason="entry_cancellation_confirmed",
            cancellation_signal_ids=signal_ids,
            updated_at=now,
        )
        counts["cancelled"] += 1

    return SourceMessageDeletionWorkerResult(**counts)


def finalize_source_message_deletion_exit(
    session_factory,
    *,
    deletion_exit_id: int,
    snapshot,
    finalized_at: datetime | None = None,
    expected_claim_token: str | None = None,
) -> str:
    """Commit terminal source-deletion state only after exact exchange-flat proof."""

    now = finalized_at or datetime.now(UTC)
    errors = dict(getattr(snapshot, "errors", {}) or {})
    with position_authority_lock():
        with source_message_execution_authority(session_factory):
            with session_factory() as session:
                deletion_exit = session.get(
                    SourceMessageDeletionExit, int(deletion_exit_id)
                )
                if deletion_exit is None:
                    return "skipped"
                if (
                    expected_claim_token is not None
                    and deletion_exit.claim_token != expected_claim_token
                ):
                    return "skipped"

                event = session.get(
                    TelegramSourceMessageEvent, deletion_exit.source_event_id
                )
                lifecycle = (
                    session.get(
                        StrategyLifecycle, deletion_exit.target_lifecycle_id
                    )
                    if deletion_exit.target_lifecycle_id is not None
                    else None
                )
                binding = (
                    session.get(
                        ExecutionBinding, deletion_exit.execution_binding_id
                    )
                    if deletion_exit.execution_binding_id is not None
                    else None
                )
                legs = (
                    session.query(ExecutionOrderLeg)
                    .filter(
                        ExecutionOrderLeg.execution_binding_id
                        == deletion_exit.execution_binding_id,
                        ExecutionOrderLeg.purpose == "entry",
                    )
                    .order_by(ExecutionOrderLeg.id.asc())
                    .all()
                    if deletion_exit.execution_binding_id is not None
                    else []
                )
                batch = (
                    session.get(
                        StrategyManagementBatch,
                        deletion_exit.management_batch_id,
                    )
                    if deletion_exit.management_batch_id is not None
                    else None
                )
                exact_pos_ids = {
                    value
                    for leg in legs
                    if leg.attribution_status == "verified"
                    for value in (_clean_id(leg.pos_id),)
                    if value is not None
                }
                active_exact_pos_ids = {
                    value
                    for leg in legs
                    if (
                        leg.attribution_status == "verified"
                        and str(leg.status or "").strip().lower()
                        not in TERMINAL_ENTRY_LEG_STATES
                    )
                    for value in (_clean_id(leg.pos_id),)
                    if value is not None
                }
                leg_order_ids = {
                    value
                    for leg in legs
                    for value in _split_ids(leg.order_id)
                }
                leg_client_order_ids = {
                    value
                    for leg in legs
                    for value in _split_ids(leg.client_order_id)
                }
                if deletion_exit.execution_binding_id is None:
                    unexpected_binding = (
                        session.query(ExecutionBinding.id)
                        .filter(
                            ExecutionBinding.chat_id == event.chat_id,
                            ExecutionBinding.message_id == event.message_id,
                        )
                        .first()
                        if event is not None
                        else None
                    )
                    hazardous_signal = (
                        session.query(TradeSignal.id)
                        .filter(
                            TradeSignal.chat_id == event.chat_id,
                            TradeSignal.message_id == event.message_id,
                            or_(
                                TradeSignal.attempts > 0,
                                TradeSignal.status.in_(
                                    {
                                        "processing",
                                        "executing",
                                        "submitted",
                                        "submit_unknown",
                                    }
                                ),
                                TradeSignal.result_json.is_not(None),
                            ),
                        )
                        .first()
                        if event is not None
                        else None
                    )
                    hazardous_event = (
                        session.query(ExecutionEvent.id)
                        .filter(
                            ExecutionEvent.chat_id == event.chat_id,
                            ExecutionEvent.message_id == event.message_id,
                            or_(
                                ExecutionEvent.order_id.is_not(None),
                                ExecutionEvent.client_order_id.is_not(None),
                                ExecutionEvent.pos_id.is_not(None),
                                ExecutionEvent.request_json.is_not(None),
                                ExecutionEvent.response_json.is_not(None),
                            ),
                        )
                        .first()
                        if event is not None
                        else None
                    )
                    identity_invalid = (
                        lifecycle is None
                        or lifecycle.execution_binding_id is not None
                        or binding is not None
                        or bool(legs)
                        or unexpected_binding is not None
                        or hazardous_signal is not None
                        or hazardous_event is not None
                    )
                else:
                    binding_pos_ids = _split_ids(binding.pos_id) if binding else set()
                    binding_order_ids = (
                        _split_ids(binding.order_id) if binding else set()
                    )
                    binding_client_order_ids = (
                        _split_ids(binding.client_order_id) if binding else set()
                    )
                    identity_invalid = (
                        lifecycle is None
                        or binding is None
                        or lifecycle.execution_binding_id
                        != deletion_exit.execution_binding_id
                        or binding.strategy_instance_id
                        != deletion_exit.strategy_instance_id
                        or any(
                            leg.strategy_instance_id
                            != deletion_exit.strategy_instance_id
                            for leg in legs
                        )
                        or any(
                            leg.pos_id and leg.attribution_status != "verified"
                            for leg in legs
                        )
                        or not binding_pos_ids.issubset(exact_pos_ids)
                        or not binding_order_ids.issubset(leg_order_ids)
                        or not binding_client_order_ids.issubset(
                            leg_client_order_ids
                        )
                        or (
                            not legs
                            and bool(
                                binding.order_id
                                or binding.client_order_id
                                or binding.pos_id
                            )
                        )
                    )
                if identity_invalid:
                    deletion_exit.state = "recovery_required"
                    deletion_exit.last_reason = "frozen_ledger_identity_unverified"
                    deletion_exit.last_error = "frozen_ledger_identity_unverified"
                    deletion_exit.last_reconciled_at = now
                    deletion_exit.claim_token = None
                    deletion_exit.claimed_at = None
                    deletion_exit.updated_at = now
                    session.commit()
                    return "recovery_required"

                if errors:
                    deletion_exit.state = "reconciling"
                    deletion_exit.last_reason = "flat_snapshot_retry"
                    deletion_exit.last_error = json.dumps(
                        errors, sort_keys=True, default=str
                    )
                    deletion_exit.last_reconciled_at = now
                    deletion_exit.claim_token = None
                    deletion_exit.claimed_at = None
                    deletion_exit.updated_at = now
                    session.commit()
                    return "waiting"

                if batch is not None and batch.status in {
                    "blocked",
                    "partial_failed",
                    "submit_unknown",
                    "recovery_required",
                }:
                    deletion_exit.state = "recovery_required"
                    deletion_exit.last_reason = "position_exit_batch_requires_recovery"
                    deletion_exit.last_error = f"management_batch_{batch.status}"
                    deletion_exit.last_reconciled_at = now
                    deletion_exit.claim_token = None
                    deletion_exit.claimed_at = None
                    deletion_exit.updated_at = now
                    session.commit()
                    return "recovery_required"
                if deletion_exit.management_batch_id is not None and batch is None:
                    deletion_exit.state = "recovery_required"
                    deletion_exit.last_reason = "position_exit_batch_missing"
                    deletion_exit.last_error = "position_exit_batch_missing"
                    deletion_exit.last_reconciled_at = now
                    deletion_exit.claim_token = None
                    deletion_exit.claimed_at = None
                    deletion_exit.updated_at = now
                    session.commit()
                    return "recovery_required"
                if batch is not None and batch.status != "succeeded":
                    return _mark_reconciliation_waiting(
                        session,
                        deletion_exit=deletion_exit,
                        reason="position_exit_batch_not_terminal",
                        reconciled_at=now,
                    )

                if batch is not None:
                    management_legs = (
                        session.query(StrategyManagementLeg)
                        .filter(StrategyManagementLeg.management_batch_id == batch.id)
                        .order_by(StrategyManagementLeg.id.asc())
                        .all()
                    )
                    prefixes = tuple(
                        f"management:{batch.id}:{leg.id}:"
                        for leg in management_legs
                    )
                    mutation_intents = (
                        session.query(PositionMutationIntent)
                        .filter(
                            PositionMutationIntent.execution_binding_id
                            == deletion_exit.execution_binding_id
                        )
                        .all()
                    )
                    related_intents = [
                        intent
                        for intent in mutation_intents
                        if any(
                            intent.idempotency_key.startswith(prefix)
                            for prefix in prefixes
                        )
                    ]
                    management_terminal = bool(management_legs) and all(
                        leg.status == "confirmed" for leg in management_legs
                    )
                    close_intents_complete = all(
                        any(
                            intent.idempotency_key.startswith(
                                f"management:{batch.id}:{leg.id}:close:"
                            )
                            and intent.status == "confirmed"
                            for intent in related_intents
                        )
                        for leg in management_legs
                    )
                    all_intents_terminal = bool(related_intents) and all(
                        intent.status == "confirmed" for intent in related_intents
                    )
                    management_pos_ids = {
                        _clean_id(leg.pos_id) for leg in management_legs
                    }
                    exact_management_ownership = all(
                        leg.execution_order_leg_id in {entry.id for entry in legs}
                        and _clean_id(leg.pos_id) in exact_pos_ids
                        for leg in management_legs
                    ) and not (
                        active_exact_pos_ids - management_pos_ids
                    )
                    if (
                        management_terminal
                        and close_intents_complete
                        and all_intents_terminal
                        and bool(active_exact_pos_ids - management_pos_ids)
                    ):
                        deletion_exit.management_batch_id = None
                        deletion_exit.state = "closing_positions"
                        deletion_exit.last_reason = "position_exit_scope_expanded"
                        deletion_exit.last_error = None
                        deletion_exit.last_reconciled_at = now
                        deletion_exit.claim_token = None
                        deletion_exit.claimed_at = None
                        deletion_exit.updated_at = now
                        session.commit()
                        return "waiting"
                    if not (
                        management_terminal
                        and close_intents_complete
                        and all_intents_terminal
                        and exact_management_ownership
                    ):
                        return _mark_reconciliation_waiting(
                            session,
                            deletion_exit=deletion_exit,
                            reason="position_exit_mutations_not_terminal",
                            reconciled_at=now,
                        )

                live_order_rows = list(getattr(snapshot, "open_orders", []) or [])
                live_order_rows.extend(
                    list(getattr(snapshot, "pending_trigger_orders", []) or [])
                )
                exact_order_ids = {
                    value
                    for leg in legs
                    for value in (_clean_id(leg.order_id), _clean_id(leg.client_order_id))
                    if value is not None
                }
                visible_order_rows = [
                    row
                    for row in live_order_rows
                    if exact_order_ids.intersection(_row_order_ids(row))
                ]
                locally_pending = [
                    leg.id
                    for leg in legs
                    if str(leg.status or "").strip().lower()
                    in {"pending", "open", "live", "submitted", "partially_filled", "partial_filled", "partial"}
                    and not leg.terminal_reason
                ]
                if visible_order_rows or locally_pending:
                    return _mark_reconciliation_waiting(
                        session,
                        deletion_exit=deletion_exit,
                        reason="exact_entry_order_still_live",
                        reconciled_at=now,
                    )

                visible_position_rows = [
                    row
                    for row in list(getattr(snapshot, "positions", []) or [])
                    if _clean_id(_row_value(row, "posId", "pos_id")) in exact_pos_ids
                    and _position_is_not_proven_zero(row)
                ]
                if visible_position_rows:
                    return _mark_reconciliation_waiting(
                        session,
                        deletion_exit=deletion_exit,
                        reason="exact_position_still_live",
                        reconciled_at=now,
                    )

                if exact_pos_ids and batch is None:
                    deletion_exit.state = "recovery_required"
                    deletion_exit.last_reason = "position_exit_batch_not_planned"
                    deletion_exit.last_error = "position_exit_batch_not_planned"
                    deletion_exit.last_reconciled_at = now
                    deletion_exit.claim_token = None
                    deletion_exit.claimed_at = None
                    deletion_exit.updated_at = now
                    session.commit()
                    return "recovery_required"

                had_position = bool(exact_pos_ids)
                proof = {
                    "proved_at": now.isoformat(),
                    "errors": {},
                    "entry_order_ids": sorted(exact_order_ids),
                    "verified_pos_ids": sorted(exact_pos_ids),
                    "visible_entry_orders": visible_order_rows,
                    "visible_positions": visible_position_rows,
                    "management_batch_id": deletion_exit.management_batch_id,
                    "management_batch_status": batch.status if batch else None,
                }
                deletion_exit.state = "succeeded"
                deletion_exit.flat_proof_json = json.dumps(
                    proof, sort_keys=True, default=str
                )
                deletion_exit.last_reason = "exchange_flat_confirmed"
                deletion_exit.last_error = None
                deletion_exit.last_reconciled_at = now
                deletion_exit.completed_at = now
                deletion_exit.claim_token = None
                deletion_exit.claimed_at = None
                deletion_exit.updated_at = now
                if event is not None:
                    event.processing_status = "completed"
                    event.reason_code = "source_message_deleted"
                    event.completed_at = now
                    event.updated_at = now
                if lifecycle is not None:
                    lifecycle.lifecycle_status = "exited" if had_position else "cancelled"
                    lifecycle.exit_reason = "source_message_deleted"
                    lifecycle.exited_at = now if had_position else lifecycle.exited_at
                    lifecycle.last_checked_at = now
                    lifecycle.management_action = "source_deletion_exit_completed"
                    lifecycle.management_note = "Exact deleted-source exposure confirmed flat"
                    lifecycle.updated_at = now
                if binding is not None:
                    binding.status = "closed"
                    binding.updated_at = now
                for leg in legs:
                    if _clean_id(leg.pos_id) in exact_pos_ids:
                        leg.status = "closed"
                        leg.terminal_reason = "source_message_deleted_exit_confirmed"
                        leg.updated_at = now
                session.commit()
                return "succeeded"


def _mark_reconciliation_waiting(
    session,
    *,
    deletion_exit: SourceMessageDeletionExit,
    reason: str,
    reconciled_at: datetime,
) -> str:
    deletion_exit.state = "reconciling"
    deletion_exit.last_reason = reason
    deletion_exit.last_reconciled_at = reconciled_at
    deletion_exit.claim_token = None
    deletion_exit.claimed_at = None
    deletion_exit.updated_at = reconciled_at
    session.commit()
    return "waiting"


def _clean_id(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _split_ids(value) -> set[str]:
    return {
        item
        for raw in str(value or "").split(",")
        if (item := raw.strip())
    }


def _row_value(row, *names):
    if not isinstance(row, dict):
        return None
    for name in names:
        if row.get(name) is not None:
            return row.get(name)
    return None


def _row_order_ids(row) -> set[str]:
    return {
        value
        for value in (
            _clean_id(
                _row_value(
                    row,
                    "orderId",
                    "order_id",
                    "ordId",
                    "triggerOrderId",
                    "trigger_order_id",
                )
            ),
            _clean_id(
                _row_value(row, "clientOrderId", "client_order_id", "clOrdId")
            ),
        )
        if value is not None
    }


def _position_is_not_proven_zero(row) -> bool:
    value = _row_value(row, "pos", "size", "sz", "positionSize", "position_size")
    try:
        return abs(float(value)) > 0
    except (TypeError, ValueError):
        return True


def _claim_next_job(
    session_factory,
    *,
    claimed_at: datetime,
    excluded_exit_ids: set[int] | None = None,
):
    stale_before = claimed_at - timedelta(minutes=5)
    with session_factory() as session:
        query = session.query(
            SourceMessageDeletionExit.id, SourceMessageDeletionExit.state
        ).filter(SourceMessageDeletionExit.state.in_(_ACTIVE_STATES))
        if excluded_exit_ids:
            query = query.filter(
                SourceMessageDeletionExit.id.not_in(tuple(excluded_exit_ids))
            )
        candidates = (
            query
            .filter(
                or_(
                    SourceMessageDeletionExit.claim_token.is_(None),
                    SourceMessageDeletionExit.claimed_at <= stale_before,
                )
            )
            .order_by(SourceMessageDeletionExit.id.asc())
            .limit(20)
            .all()
        )
    for exit_id, current_state in candidates:
        token = uuid4().hex
        claimed_state = (
            "cancelling_entries" if current_state == "pending" else current_state
        )
        with session_factory() as session:
            result = session.execute(
                update(SourceMessageDeletionExit)
                .where(
                    SourceMessageDeletionExit.id == int(exit_id),
                    SourceMessageDeletionExit.state == current_state,
                    or_(
                        SourceMessageDeletionExit.claim_token.is_(None),
                        SourceMessageDeletionExit.claimed_at <= stale_before,
                    ),
                )
                .values(
                    state=claimed_state,
                    claim_token=token,
                    claimed_at=claimed_at,
                    attempt_count=SourceMessageDeletionExit.attempt_count + 1,
                    updated_at=claimed_at,
                )
            )
            session.commit()
            if result.rowcount == 1:
                return int(exit_id), str(claimed_state), token
    return None


def _transition_claimed(
    session_factory,
    *,
    exit_id: int,
    claim_token: str,
    new_state: str,
    reason: str,
    updated_at: datetime,
    error: str | None = None,
    cancellation_signal_ids: tuple[int, ...] | None = None,
) -> bool:
    values = {
        "state": new_state,
        "claim_token": None,
        "claimed_at": None,
        "last_reason": reason,
        "last_error": error,
        "updated_at": updated_at,
    }
    if cancellation_signal_ids is not None:
        values["cancellation_signal_ids_json"] = json.dumps(
            list(cancellation_signal_ids)
        )
    with session_factory() as session:
        result = session.execute(
            update(SourceMessageDeletionExit)
            .where(
                SourceMessageDeletionExit.id == int(exit_id),
                SourceMessageDeletionExit.claim_token == claim_token,
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def _release_claim(
    session_factory,
    *,
    exit_id: int,
    claim_token: str,
    state: str,
    reason: str,
    updated_at: datetime,
) -> bool:
    return _transition_claimed(
        session_factory,
        exit_id=exit_id,
        claim_token=claim_token,
        new_state=state,
        reason=reason,
        updated_at=updated_at,
    )


def _trade_signal_ids_for_events(
    session_factory,
    *,
    event_ids,
) -> tuple[int, ...]:
    if not event_ids:
        return ()
    with session_factory() as session:
        rows = (
            session.query(ExecutionEvent.trade_signal_id)
            .filter(
                ExecutionEvent.id.in_(tuple(int(value) for value in event_ids)),
                ExecutionEvent.trade_signal_id.is_not(None),
            )
            .all()
        )
    return tuple(sorted({int(value) for (value,) in rows}))
