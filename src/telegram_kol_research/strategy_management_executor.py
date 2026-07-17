"""Crash-safe submission of immutable Deepcoin strategy-management batches."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinTradingClientProtocol,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_margin_mode,
    normalize_deepcoin_position_mode,
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.protection_attribution import (
    match_position_protection,
    snapshot_protection_rows,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    claim_ready_batch,
    load_management_batch,
    transition_batch,
    transition_leg,
)


DEEPCOIN_CLIENT_ORDER_ID_MAX_LENGTH = 20
_CLOSE_ACTIONS = frozenset(
    {"partial_close", "full_close", "full_exit", "partial_then_break_even"}
)
_PROTECTION_ACTIONS = frozenset({"adjust_stop_loss", "move_stop_to_break_even"})
_PROTECTION_PHASE_LEG_STATES = frozenset(
    {"succeeded", "restored", "recovery_required"}
)


class ManagementBatchExecutionError(RuntimeError):
    """Raised when a batch cannot be submitted without guessing its state."""


def build_management_client_order_id(*, batch_id: int, leg_id: int) -> str:
    """Return a stable Deepcoin-safe close ID derived only from durable IDs."""

    digest = hashlib.sha256(f"management:{batch_id}:{leg_id}".encode()).hexdigest()
    value = f"TM{digest[:18]}".upper()
    return value[:DEEPCOIN_CLIENT_ORDER_ID_MAX_LENGTH]


@serialized_position_authority_mutation
def execute_management_batch(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Submit close legs by durable batch ID; exchange truth closes positions later."""

    now = executed_at or datetime.now(UTC)
    batch = load_management_batch(session_factory, int(batch_id))
    if batch.effective_action in _PROTECTION_ACTIONS or (
        batch.effective_action == "partial_then_break_even"
        and (
            batch.status == "protection_ready"
            or batch.reason_code == "protection_phase_executing"
            or any(
                leg.status in _PROTECTION_PHASE_LEG_STATES for leg in batch.legs
            )
        )
    ):
        return _execute_protection_batch(
            session_factory,
            batch=batch,
            deepcoin_client=deepcoin_client,
            executed_at=now,
        )
    if batch.effective_action not in _CLOSE_ACTIONS:
        raise ManagementBatchExecutionError(
            f"batch_action_not_close:{batch.effective_action}"
        )
    if not batch.legs or any(leg.planned_close_size is None for leg in batch.legs):
        raise ManagementBatchExecutionError("batch_close_plan_incomplete")

    if batch.status == "ready":
        claimed = claim_ready_batch(session_factory, batch.id, claimed_at=now)
        if claimed is None:
            batch = load_management_batch(session_factory, batch.id)
        else:
            batch = claimed
    if batch.status == "reconciling":
        return _result(batch, reason="batch_already_reconciling")
    if batch.status != "executing":
        raise ManagementBatchExecutionError(f"batch_not_executable:{batch.status}")
    binding = _load_exact_binding(session_factory, batch)
    _require_exact_entry_legs(session_factory, batch)
    try:
        _require_fresh_close_write_boundary(
            session_factory,
            batch=batch,
            binding=binding,
            deepcoin_client=deepcoin_client,
        )
    except Exception as exc:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=now,
            reason_code=(
                "close_final_preflight_failed"
                if isinstance(exc, ManagementBatchExecutionError)
                else "close_final_preflight_unavailable"
            ),
        ):
            raise ManagementBatchExecutionError(
                "management_batch_final_preflight_transition_conflict"
            ) from exc
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="close_final_preflight_failed",
        )
    # A durable reservation cannot distinguish a crash before the call from a
    # crash after a successful call. Never retry it automatically.
    for leg in batch.legs:
        if leg.status == "reserved":
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                last_error={"reason": "reserved_submission_outcome_unknown"},
            )

    batch = load_management_batch(session_factory, batch.id)
    for leg in batch.legs:
        if leg.status != "planned":
            continue
        client_order_id = build_management_client_order_id(
            batch_id=batch.id, leg_id=leg.id
        )
        request = _close_payload(
            binding=binding,
            pos_id=leg.pos_id,
            close_size=str(leg.planned_close_size),
            client_order_id=client_order_id,
        )
        reserved = transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status="reserved",
            transitioned_at=now,
            client_order_id=client_order_id,
            request=request,
            last_error=None,
        )
        if not reserved:
            raise ManagementBatchExecutionError(
                f"management_leg_reservation_conflict:{leg.id}"
            )
        try:
            response = deepcoin_client.place_order(request)
        except DeepcoinDefiniteRejection as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="failed",
                transitioned_at=now,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="failed",
                reason="submission_rejected",
                created_at=now,
            )
            continue
        except Exception as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="submit_unknown",
                reason="submission_outcome_unknown",
                created_at=now,
            )
            continue

        order_id = _extract_order_id(response)
        if not order_id:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                response=response,
                last_error={"reason": "submission_response_missing_order_id"},
            )
            event_status = "submit_unknown"
            event_reason = "submission_response_missing_order_id"
        else:
            persisted = transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submitted",
                transitioned_at=now,
                exchange_order_id=order_id,
                response=response,
                last_error=None,
            )
            if not persisted:
                raise ManagementBatchExecutionError(
                    f"management_leg_response_persist_conflict:{leg.id}"
                )
            event_status = "submitted"
            event_reason = "management_close_submitted"
        _record_leg_event(
            session_factory,
            batch=batch,
            binding=binding,
            leg_id=leg.id,
            pos_id=leg.pos_id,
            client_order_id=client_order_id,
            request=request,
            response=response,
            order_id=order_id,
            status=event_status,
            reason=event_reason,
            created_at=now,
        )

    batch = load_management_batch(session_factory, batch.id)
    statuses = {leg.status for leg in batch.legs}
    if "failed" in statuses:
        final_status = "partial_failed"
        reason = "one_or_more_close_submissions_failed"
    else:
        final_status = "reconciling"
        reason = (
            "one_or_more_close_submissions_unknown"
            if "submit_unknown" in statuses
            else "close_submissions_pending_reconciliation"
        )
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=now,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError("management_batch_finalization_conflict")
    return _result(load_management_batch(session_factory, batch.id), reason=reason)


def _execute_protection_batch(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> dict[str, Any]:
    """Replace every exact position's complete TPSL set, compensating per leg."""

    if not batch.legs or any(
        not leg.old_tpsl or not leg.planned_tpsl for leg in batch.legs
    ):
        raise ManagementBatchExecutionError("batch_protection_plan_incomplete")
    if batch.status == "ready":
        claimed = claim_ready_batch(session_factory, batch.id, claimed_at=executed_at)
        batch = claimed or load_management_batch(session_factory, batch.id)
    elif batch.status == "protection_ready":
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"protection_ready"},
            new_status="executing",
            transitioned_at=executed_at,
            reason_code="protection_phase_executing",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_phase_claim_conflict"
            )
        batch = load_management_batch(session_factory, batch.id)
    if batch.status != "executing":
        raise ManagementBatchExecutionError(f"batch_not_executable:{batch.status}")
    durable_statuses = {leg.status for leg in batch.legs}
    if "recovery_required" in durable_statuses:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=executed_at,
            reason_code="protection_recovery_required",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_recovery_finalization_conflict"
            )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="protection_recovery_required",
        )
    if "restored" in durable_statuses:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="partial_failed",
            transitioned_at=executed_at,
            reason_code="protection_replacement_failed_and_restored",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_restore_finalization_conflict"
            )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="protection_replacement_failed_and_restored",
        )
    if durable_statuses == {"succeeded"}:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="succeeded",
            transitioned_at=executed_at,
            reason_code="all_position_protection_replaced",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_success_finalization_conflict"
            )
        succeeded_batch = load_management_batch(session_factory, batch.id)
        _confirm_protection_lifecycle(
            session_factory, batch=succeeded_batch, confirmed_at=executed_at
        )
        return _result(
            succeeded_batch,
            reason="all_position_protection_replaced",
        )
    if any(leg.status == "reserved" for leg in batch.legs):
        for leg in batch.legs:
            if leg.status == "reserved":
                transition_leg(
                    session_factory,
                    leg.id,
                    expected_statuses={"reserved"},
                    new_status="recovery_required",
                    transitioned_at=executed_at,
                    last_error={"reason": "reserved_protection_outcome_unknown"},
                )
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=executed_at,
            reason_code="reserved_protection_outcome_unknown",
        )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="reserved_protection_outcome_unknown",
        )

    try:
        binding = _load_exact_binding(session_factory, batch)
        _require_exact_entry_legs(session_factory, batch)
        inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
        live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
        positions_by_id = _preflight_exact_protection_positions(
            session_factory=session_factory,
            batch=batch,
            binding=binding,
            live_positions=live_positions,
            inst_id=inst_id,
        )
        # This is deliberately the final read before the first exchange cancel.
        pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=inst_id))
        _preflight_exact_protection_rows(
            batch=batch,
            live_positions=live_positions,
            pending=pending,
        )
        prepared_legs = []
        for leg in batch.legs:
            old_rows = list(leg.old_tpsl["row_snapshots"])
            prepared_legs.append(
                (
                    leg,
                    old_rows,
                    _adjusted_protection_rows(
                        batch=batch, leg=leg, old_rows=old_rows
                    ),
                    _protection_payload_common(
                        binding=binding,
                        position=positions_by_id[leg.pos_id],
                        inst_id=inst_id,
                    ),
                )
            )
    except ManagementBatchExecutionError:
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="blocked",
            transitioned_at=executed_at,
            reason_code="protection_preflight_failed",
        )
        raise
    except Exception as exc:
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="blocked",
            transitioned_at=executed_at,
            reason_code="protection_preflight_unavailable",
        )
        raise ManagementBatchExecutionError(
            f"protection_preflight_unavailable:{type(exc).__name__}"
        ) from exc

    failed_status: str | None = None
    for leg, old_rows, new_rows, common in prepared_legs:
        if leg.status == "succeeded":
            continue
        expected_leg_statuses = (
            {"confirmed"}
            if batch.effective_action == "partial_then_break_even"
            else {"planned"}
        )
        if not transition_leg(
            session_factory,
            leg.id,
            expected_statuses=expected_leg_statuses,
            new_status="reserved",
            transitioned_at=executed_at,
            request={"cancel_order_ids": [row["order_id"] for row in old_rows]},
        ):
            raise ManagementBatchExecutionError(
                f"management_protection_leg_reservation_conflict:{leg.id}"
            )

        try:
            for row in old_rows:
                deepcoin_client.cancel_position_sltp(
                    {"instType": "SWAP", "instId": inst_id, "ordId": str(row["order_id"])}
                )
        except Exception as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=executed_at,
                last_error={
                    "stage": "cancel_old_protection",
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            failed_status = "recovery_required"
            break

        created_order_ids: list[str] = []
        replacement_responses: list[dict[str, Any]] = []
        replacement_error: Exception | None = None
        for row in new_rows:
            payload = _protection_row_payload(common=common, row=row)
            try:
                response = deepcoin_client.set_position_sltp(payload)
                order_id = _extract_order_id(response)
                if not order_id:
                    raise ManagementBatchExecutionError(
                        "protection_replacement_missing_order_id"
                    )
                created_order_ids.append(order_id)
                replacement_responses.append(response)
            except Exception as exc:
                replacement_error = exc
                break
        if replacement_error is None:
            _record_management_tpsl_ledger_rows(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                inst_id=inst_id,
                rows=new_rows,
                order_ids=created_order_ids,
                seen_at=executed_at,
            )
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="succeeded",
                transitioned_at=executed_at,
                response={"rows": replacement_responses},
                last_error=None,
            )
            continue

        if not isinstance(replacement_error, DeepcoinDefiniteRejection):
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=executed_at,
                response={"rows": replacement_responses},
                last_error={
                    "stage": "replace_protection_outcome_unknown",
                    "type": type(replacement_error).__name__,
                    "message": str(replacement_error),
                    "created_order_ids": created_order_ids,
                },
            )
            failed_status = "recovery_required"
            break

        restoration_error: Exception | None = None
        try:
            for order_id in created_order_ids:
                deepcoin_client.cancel_position_sltp(
                    {"instType": "SWAP", "instId": inst_id, "ordId": order_id}
                )
            restore_responses = []
            for row in old_rows:
                response = deepcoin_client.set_position_sltp(
                    _protection_row_payload(common=common, row=row)
                )
                if not _extract_order_id(response):
                    raise ManagementBatchExecutionError(
                        "protection_restore_missing_order_id"
                    )
                restore_responses.append(response)
        except Exception as exc:
            restoration_error = exc
            restore_responses = []

        if restoration_error is None:
            leg_status = "restored"
            failed_status = "partial_failed"
        else:
            leg_status = "recovery_required"
            failed_status = "recovery_required"
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=leg_status,
            transitioned_at=executed_at,
            response={"restore_rows": restore_responses},
            last_error={
                "stage": "replace_protection",
                "type": type(replacement_error).__name__,
                "message": str(replacement_error),
                "restore_error": (
                    None
                    if restoration_error is None
                    else {
                        "type": type(restoration_error).__name__,
                        "message": str(restoration_error),
                    }
                ),
            },
        )
        break

    final_status = failed_status or "succeeded"
    reason = {
        "succeeded": "all_position_protection_replaced",
        "partial_failed": "protection_replacement_failed_and_restored",
        "recovery_required": "protection_recovery_required",
    }[final_status]
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=executed_at,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError("management_batch_finalization_conflict")
    completed_batch = load_management_batch(session_factory, batch.id)
    if final_status == "succeeded":
        _confirm_protection_lifecycle(
            session_factory, batch=completed_batch, confirmed_at=executed_at
        )
    return _result(completed_batch, reason=reason)


def _confirm_protection_lifecycle(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    confirmed_at: datetime,
) -> None:
    """Promote protection intent only after every replacement was accepted."""

    requested_stops = {
        str(leg.planned_tpsl.get("stop_loss_text")).strip()
        for leg in batch.legs
        if isinstance(leg.planned_tpsl, dict)
        and leg.planned_tpsl.get("stop_loss_text") not in {None, ""}
    }
    confirmed_stop: float | None = None
    if len(requested_stops) == 1:
        try:
            parsed = Decimal(next(iter(requested_stops)))
        except InvalidOperation:
            parsed = Decimal("NaN")
        if parsed.is_finite() and parsed > 0:
            confirmed_stop = float(parsed)

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        raw = session.get(RawMessage, batch.raw_message_id)
        if lifecycle is None or raw is None:
            raise ManagementBatchExecutionError(
                "management_protection_confirmation_identity_missing"
            )
        if confirmed_stop is not None:
            lifecycle.stop_loss = confirmed_stop
        lifecycle.management_signal_message_id = int(raw.message_id)
        lifecycle.management_action = "protection_update_confirmed"
        lifecycle.management_note = (
            "Deepcoin accepted every planned position-protection replacement."
        )
        lifecycle.updated_at = confirmed_at
        session.commit()


def _preflight_exact_protection_positions(
    *,
    session_factory: sessionmaker,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    live_positions: list[dict[str, Any]],
    inst_id: str,
) -> dict[str, dict[str, Any]]:
    positions_by_id = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        error_prefix="protection_preflight",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    for leg in batch.legs:
        position = positions_by_id[leg.pos_id]
        expected_size = leg.preflight_size
        if batch.effective_action == "partial_then_break_even":
            try:
                expected_size = str(
                    Decimal(str(leg.preflight_size))
                    - Decimal(str(leg.planned_close_size))
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ManagementBatchExecutionError(
                    "protection_preflight_position_economics_drift"
                ) from exc
        if not _decimal_equal(
            _first_text(position, "pos", "size", "sz"), expected_size
        ) or not _decimal_equal(
            _first_text(position, "avgPx", "avgPrice", "avg_entry_price"),
            leg.avg_entry_price,
        ):
            raise ManagementBatchExecutionError(
                "protection_preflight_position_economics_drift"
            )
    return positions_by_id


def _require_fresh_close_write_boundary(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> None:
    """Re-read exact exchange positions at the shared close write boundary."""

    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
    positions = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        error_prefix="close_final_preflight",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    contract_spec = (
        batch.target_snapshot.get("contract_spec")
        if isinstance(batch.target_snapshot, dict)
        else None
    )
    if not isinstance(contract_spec, dict):
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_missing"
        )
    try:
        contract_step = Decimal(str(contract_spec["quantity_step"]))
        min_quantity = Decimal(str(contract_spec["min_quantity"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_invalid"
        ) from exc
    if (
        not contract_step.is_finite()
        or contract_step <= 0
        or not min_quantity.is_finite()
        or min_quantity <= 0
    ):
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_invalid"
        )
    for leg in batch.legs:
        try:
            planned_size = Decimal(str(leg.planned_close_size))
            persisted_step = Decimal(str(leg.quantity_step))
            preflight_size = Decimal(str(leg.preflight_size))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ManagementBatchExecutionError(
                "close_final_preflight_quantity_invalid"
            ) from exc
        if (
            not planned_size.is_finite()
            or not preflight_size.is_finite()
            or preflight_size <= 0
            or planned_size < min_quantity
            or planned_size > preflight_size
            or persisted_step != contract_step
            or planned_size % contract_step != 0
        ):
            raise ManagementBatchExecutionError(
                "close_final_preflight_quantity_invalid"
            )
        if not _decimal_equal(
            _first_text(positions[leg.pos_id], "pos", "size", "sz"),
            leg.preflight_size,
        ):
            raise ManagementBatchExecutionError(
                "close_final_preflight_position_size_drift"
            )


def _preflight_exact_position_identity(
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    live_positions: list[dict[str, Any]],
    inst_id: str,
    error_prefix: str,
    ignored_owned_pos_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    expected_pos_ids = {leg.pos_id for leg in batch.legs}
    ignored = set(ignored_owned_pos_ids or ()) - expected_pos_ids
    if any(
        _first_text(row, "instId", "inst_id") == inst_id
        and (_first_text(row, "posSide", "pos_side", "side") or "").lower()
        == binding.side.lower()
        and _first_text(row, "posId", "pos_id", "id") is None
        for row in live_positions
    ):
        raise ManagementBatchExecutionError(
            f"{error_prefix}_position_ambiguous"
        )
    bound_pos_ids = {
        value.strip()
        for value in str(binding.pos_id or "").split(",")
        if value.strip()
    }
    if bound_pos_ids != expected_pos_ids:
        raise ManagementBatchExecutionError(
            f"{error_prefix}_binding_position_set_drift"
        )
    relevant_live_ids = {
        str(pos_id)
        for row in live_positions
        if _first_text(row, "instId", "inst_id") == inst_id
        and (_first_text(row, "posSide", "pos_side", "side") or "").lower()
        == binding.side.lower()
        and (pos_id := _first_text(row, "posId", "pos_id", "id")) is not None
        and str(pos_id) not in ignored
    }
    if relevant_live_ids != expected_pos_ids:
        raise ManagementBatchExecutionError(
            f"{error_prefix}_live_position_set_drift"
        )
    positions_by_id: dict[str, dict[str, Any]] = {}
    for row in live_positions:
        pos_id = _first_text(row, "posId", "pos_id", "id")
        if pos_id in ignored:
            continue
        if pos_id not in expected_pos_ids:
            continue
        if (
            _first_text(row, "instId", "inst_id") != inst_id
            or (_first_text(row, "posSide", "pos_side", "side") or "").lower()
            != binding.side.lower()
            or pos_id in positions_by_id
        ):
            raise ManagementBatchExecutionError(
                f"{error_prefix}_position_ambiguous"
            )
        positions_by_id[str(pos_id)] = row
    if set(positions_by_id) != expected_pos_ids:
        raise ManagementBatchExecutionError(f"{error_prefix}_position_set_drift")
    return positions_by_id


def validate_management_restart_snapshot(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    snapshot: Any,
) -> None:
    """Require exact frozen exchange identity before resuming an all-planned batch."""

    if getattr(snapshot, "errors", {}):
        raise ManagementBatchExecutionError("restart_snapshot_exchange_read_failed")
    batch = load_management_batch(session_factory, int(batch_id))
    binding = _load_exact_binding(session_factory, batch)
    _require_exact_entry_legs(session_factory, batch)
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    positions = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=list(getattr(snapshot, "positions", [])),
        inst_id=inst_id,
        error_prefix="restart_snapshot",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    for leg in batch.legs:
        if not _decimal_equal(
            _first_text(positions[leg.pos_id], "pos", "size", "sz"),
            leg.preflight_size,
        ):
            raise ManagementBatchExecutionError(
                "restart_snapshot_position_size_drift"
            )


def _other_exact_strategy_position_ids(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
) -> set[str]:
    """Return only uniquely verified live positions owned by another binding."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.pos_id.is_not(None))
            .filter(ExecutionOrderLeg.execution_binding_id != batch.execution_binding_id)
            .all()
        )
        by_pos: dict[str, list[ExecutionOrderLeg]] = {}
        for row in rows:
            if (
                str(row.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
                or row.terminal_reason is not None
            ):
                continue
            by_pos.setdefault(str(row.pos_id), []).append(row)

        owned: set[str] = set()
        for pos_id, candidates in by_pos.items():
            if len(candidates) != 1:
                continue
            row = candidates[0]
            other = session.get(ExecutionBinding, row.execution_binding_id)
            bound_ids = {
                value.strip()
                for value in str(other.pos_id if other is not None else "").split(",")
                if value.strip()
            }
            if (
                other is not None
                and other.status == "active"
                and other.venue == binding.venue
                and row.venue == binding.venue
                and row.attribution_status == "verified"
                and str(row.status or "").lower()
                in {"active", "open", "filled", "partial_closed"}
                and bool(other.strategy_instance_id)
                and other.strategy_instance_id != batch.strategy_instance_id
                and row.strategy_instance_id == other.strategy_instance_id
                and other.symbol.upper() == binding.symbol.upper()
                and other.side.lower() == binding.side.lower()
                and pos_id in bound_ids
            ):
                owned.add(pos_id)
        return owned


def _preflight_exact_protection_rows(
    *,
    batch: ManagementBatchRecord,
    live_positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> None:
    matches = match_position_protection(live_positions, pending)
    seen_ids: set[str] = set()
    for leg in batch.legs:
        protection = matches.by_pos_id.get(leg.pos_id)
        expected = leg.old_tpsl or {}
        current_rows = (
            snapshot_protection_rows(protection.rows) if protection is not None else []
        )
        expected_rows = expected.get("row_snapshots") or []
        current_ids = [row.get("order_id") for row in current_rows]
        if (
            protection is None
            or protection.status != "verified"
            or current_rows != expected_rows
            or current_ids != expected.get("order_ids")
            or any(not order_id or order_id in seen_ids for order_id in current_ids)
        ):
            raise ManagementBatchExecutionError(
                "protection_preflight_rows_ambiguous_or_drifted"
            )
        seen_ids.update(str(order_id) for order_id in current_ids)


def _adjusted_protection_rows(
    *, batch: ManagementBatchRecord, leg: Any, old_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if batch.effective_action in {
        "move_stop_to_break_even",
        "partial_then_break_even",
    }:
        stop_price = leg.avg_entry_price
    else:
        stop_price = (leg.planned_tpsl or {}).get("stop_loss_text")
    if stop_price in (None, ""):
        raise ManagementBatchExecutionError("planned_stop_loss_missing")
    adjusted: list[dict[str, Any]] = []
    found_stop = False
    for old in old_rows:
        row = dict(old)
        if row.get("purpose") == "stop_loss":
            row["trigger_price"] = str(stop_price)
            found_stop = True
        elif row.get("purpose") == "combined":
            row["stop_loss"] = {
                **dict(row["stop_loss"]),
                "trigger_price": str(stop_price),
            }
            found_stop = True
        adjusted.append(row)
    if not found_stop:
        raise ManagementBatchExecutionError("existing_stop_loss_missing")
    return adjusted


def _protection_payload_common(
    *, binding: ExecutionBinding, position: dict[str, Any], inst_id: str
) -> dict[str, Any]:
    payload = {
        "instType": "SWAP",
        "instId": inst_id,
        "posSide": binding.side.lower(),
        "mrgPosition": normalize_deepcoin_position_mode(binding.position_mode),
        "tdMode": normalize_deepcoin_margin_mode(binding.margin_mode),
    }
    if payload["mrgPosition"] == "split":
        pos_id = _first_text(position, "posId", "pos_id", "id")
        if not pos_id:
            raise ManagementBatchExecutionError("protection_preflight_pos_id_missing")
        payload["posId"] = pos_id
    return payload


def _record_management_tpsl_ledger_rows(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg: Any,
    inst_id: str,
    rows: list[dict[str, Any]],
    order_ids: list[str],
    seen_at: datetime,
) -> None:
    with session_factory() as session:
        for row, order_id in zip(rows, order_ids, strict=False):
            upsert_protection_ledger_row(
                session,
                venue=binding.venue,
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=int(leg.execution_order_leg_id),
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=str(leg.pos_id),
                instrument_id=inst_id,
                side=binding.side,
                order_id=str(order_id),
                purpose=str(row.get("purpose") or ""),
                trigger_price=_ledger_trigger_price(row),
                size_text=str(row.get("size")) if row.get("size") is not None else None,
                status="verified",
                evidence_source="management_tpsl_replacement",
                evidence={
                    "match": "exchange_returned_order_id",
                    "management_batch_id": batch.id,
                    "management_leg_id": leg.id,
                },
                seen_at=seen_at,
            )
        session.commit()


def _ledger_trigger_price(row: dict[str, Any]) -> str | None:
    purpose = row.get("purpose")
    if purpose in {"take_profit", "stop_loss"}:
        value = row.get("trigger_price")
        return None if value is None else str(value)
    if purpose == "combined":
        stop = row.get("stop_loss")
        if isinstance(stop, dict) and stop.get("trigger_price") is not None:
            return str(stop["trigger_price"])
    return None


def _protection_row_payload(
    *, common: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(common)
    size = row.get("size")
    if not row.get("full_position"):
        try:
            parsed_size = Decimal(str(size))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ManagementBatchExecutionError("invalid_partial_protection_size") from exc
        if not parsed_size.is_finite() or parsed_size <= 0:
            raise ManagementBatchExecutionError("invalid_partial_protection_size")
        payload["sz"] = str(size)
    purpose = row.get("purpose")
    if purpose in {"take_profit", "stop_loss"}:
        prefix = "tp" if purpose == "take_profit" else "sl"
        payload[f"{prefix}TriggerPx"] = str(row["trigger_price"])
        payload[f"{prefix}TriggerPxType"] = str(row.get("trigger_type") or "last")
        payload[f"{prefix}OrdPx"] = str(row.get("order_price") or "-1")
    elif purpose == "combined":
        for key, prefix in (("take_profit", "tp"), ("stop_loss", "sl")):
            item = row[key]
            payload[f"{prefix}TriggerPx"] = str(item["trigger_price"])
            payload[f"{prefix}TriggerPxType"] = str(
                item.get("trigger_type") or "last"
            )
            payload[f"{prefix}OrdPx"] = str(item.get("order_price") or "-1")
    else:
        raise ManagementBatchExecutionError("unsupported_protection_row_purpose")
    return payload


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _decimal_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _load_exact_binding(
    session_factory: sessionmaker, batch: ManagementBatchRecord
) -> ExecutionBinding:
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        if (
            binding is None
            or binding.strategy_instance_id != batch.strategy_instance_id
            or binding.status not in {"active", "open", "partial"}
        ):
            raise ManagementBatchExecutionError("batch_binding_not_active_or_exact")
        session.expunge(binding)
        return binding


def _require_exact_entry_legs(
    session_factory: sessionmaker, batch: ManagementBatchRecord
) -> None:
    with session_factory() as session:
        all_entries = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        batch_identity = {
            (int(leg.execution_order_leg_id), str(leg.pos_id)) for leg in batch.legs
        }
        current_identity: set[tuple[int, str]] = set()
        if not all_entries:
            raise ManagementBatchExecutionError("batch_entry_set_not_exact")
        for entry in all_entries:
            status = str(entry.status or "").lower()
            if (
                entry.strategy_instance_id != batch.strategy_instance_id
                or entry.attribution_status != "verified"
                or not entry.pos_id
                or status in TERMINAL_ENTRY_LEG_STATES
                or status not in {"active", "open", "filled", "partial_closed"}
                or entry.terminal_reason is not None
            ):
                raise ManagementBatchExecutionError("batch_entry_set_not_exact")
            current_identity.add((int(entry.id), str(entry.pos_id)))
        if current_identity != batch_identity:
            raise ManagementBatchExecutionError("batch_entry_set_not_exact")
        for leg in batch.legs:
            entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
            if (
                entry is None
                or entry.execution_binding_id != batch.execution_binding_id
                or entry.strategy_instance_id != batch.strategy_instance_id
                or entry.pos_id != leg.pos_id
                or entry.status not in {"active", "open", "filled", "partial_closed"}
                or entry.attribution_status != "verified"
            ):
                raise ManagementBatchExecutionError(
                    f"batch_entry_leg_not_exact_or_active:{leg.id}"
                )


def _close_payload(
    *, binding: ExecutionBinding, pos_id: str, close_size: str, client_order_id: str
) -> dict[str, str]:
    return {
        "instId": normalize_deepcoin_swap_instrument(binding.symbol),
        "tdMode": normalize_deepcoin_margin_mode(binding.margin_mode),
        "side": "sell" if binding.side.lower() == "long" else "buy",
        "posSide": binding.side.lower(),
        "ordType": "market",
        "sz": close_size,
        "mrgPosition": normalize_deepcoin_position_mode(binding.position_mode),
        "closePosId": str(pos_id),
        "clOrdId": client_order_id,
    }


def _extract_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    rows = [response]
    data = response.get("data")
    if isinstance(data, dict):
        rows.append(data)
    elif isinstance(data, list):
        rows.extend(row for row in data if isinstance(row, dict))
    for row in rows:
        for key in ("ordId", "orderId", "order_id", "id", "orderSysID"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _record_leg_event(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg_id: int,
    pos_id: str,
    client_order_id: str,
    request: dict[str, Any],
    response: dict[str, Any] | None,
    order_id: str | None,
    status: str,
    reason: str,
    created_at: datetime,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            strategy_instance_id=batch.strategy_instance_id,
            venue="deepcoin",
            action="strategy_management_close_submit",
            status=status,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=batch.raw_message_id,
            symbol=binding.symbol,
            side=binding.side,
            order_id=order_id,
            client_order_id=client_order_id,
            pos_id=pos_id,
            reason=reason,
            before={"management_batch_id": batch.id, "management_leg_id": leg_id},
            after={"management_batch_id": batch.id, "management_leg_id": leg_id},
            request={**request, "managementBatchId": batch.id, "managementLegId": leg_id},
            response=response,
            created_at=created_at,
        ),
    )


def _result(batch: ManagementBatchRecord, *, reason: str) -> dict[str, Any]:
    return {
        "submitted": any(
            leg.status in {"submitted", "succeeded"} for leg in batch.legs
        ),
        "batch_id": batch.id,
        "status": batch.status,
        "reason": reason,
        "legs": [
            {
                "leg_id": leg.id,
                "pos_id": leg.pos_id,
                "status": leg.status,
                "client_order_id": leg.client_order_id,
                "order_id": leg.exchange_order_id,
            }
            for leg in batch.legs
        ],
    }
