"""Crash-safe submission of immutable Deepcoin strategy-management batches."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
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
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    claim_ready_batch,
    load_management_batch,
    transition_batch,
    transition_leg,
)


DEEPCOIN_CLIENT_ORDER_ID_MAX_LENGTH = 20
_CLOSE_ACTIONS = frozenset({"partial_close", "full_close", "full_exit"})


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
        "submitted": any(leg.status == "submitted" for leg in batch.legs),
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
