"""Crash-safe exact cancellation and rebuild for entry sizing revisions."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    EntryRevisionReplacement,
    EntryStrategyFragment,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
    TradingSetting,
)
from telegram_kol_research.entry_revision_risk import (
    EntryRevisionRiskError,
    assess_revision_risk,
)
from telegram_kol_research.entry_revision_planner import (
    plan_post_submit_entry_fragment_revisions,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    acquire_entry_revision_exchange_authority,
    release_entry_revision_exchange_authority,
)
from telegram_kol_research.deployment_entry_freeze import (
    deployment_entry_admission_frozen,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.recovery_live_submit import (
    build_deepcoin_market_order_payload,
    build_deepcoin_trigger_order_payload,
)
from telegram_kol_research.strategy_management_sizing import (
    ManagementSizingError,
    entry_revision_risk_reduction_delta,
)
from telegram_kol_research.strategy_management_executor import (
    execute_entry_revision_risk_reduction_live,
    execute_entry_revision_risk_reduction_via_management,
)
from telegram_kol_research.trading_settings import (
    ENTRY_REVISION_ACTIVATION_KEY,
    load_trading_settings,
)


TERMINAL_BATCH_STATES = frozenset({"succeeded", "recovery_required", "blocked"})
TERMINAL_ORDER_STATES = frozenset(
    {"cancelled", "canceled", "rejected", "expired", "failed"}
)
FILLED_ORDER_STATES = frozenset({"filled", "active", "position_open"})
ENTRY_REVISION_CLAIM_LEASE = timedelta(minutes=5)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EntryRevisionExecutionResult:
    status: str
    batch_id: int
    reason_code: str | None = None


@dataclass(slots=True)
class _EntryRevisionAuthorityState:
    write_started_or_ambiguous: bool = False

    def mark_write_boundary(self) -> None:
        self.write_started_or_ambiguous = True


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _response_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for row in rows:
        for key in ("ordId", "orderId", "order_id"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    for key in ("ordId", "orderId", "order_id"):
        if response.get(key) not in (None, ""):
            return str(response[key])
    return None


def _row_identity(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("ordId") or row.get("orderId") or row.get("order_id") or ""),
        str(row.get("clOrdId") or row.get("clientOrderId") or row.get("client_order_id") or ""),
    )


def _find_exact(rows: list[dict[str, Any]], *, order_id: str | None, client_order_id: str | None):
    matches = []
    for row in rows:
        row_order_id, row_client_id = _row_identity(row)
        if (order_id and row_order_id == str(order_id)) or (
            client_order_id and row_client_id == str(client_order_id)
        ):
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def _state(row: dict[str, Any] | None) -> str:
    if row is None:
        return ""
    return str(row.get("state") or row.get("status") or row.get("orderStatus") or "").lower()


def _filled_position_id(row: dict[str, Any]) -> str | None:
    return str(row.get("posId") or row.get("pos_id") or "") or None


def _row_has_fill(row: dict[str, Any]) -> bool:
    pos_id = _filled_position_id(row)
    filled_size = _decimal_field(
        row, "accFillSz", "filledSize", "fillSz", "filled_quantity"
    )
    return bool(pos_id or (filled_size is not None and filled_size > 0))


def _read_exact_order(client, *, instrument_id: str, order_kind: str, order_id: str | None, client_order_id: str | None):
    trigger = "trigger" in str(order_kind).lower() or str(order_kind).lower() == "limit"
    if trigger:
        pending_rows = client.list_trigger_orders_pending(inst_id=instrument_id)
        history_rows = client.list_trigger_order_history(inst_id=instrument_id)
    else:
        pending_rows = client.list_open_orders(inst_id=instrument_id)
        history_rows = client.list_order_history(inst_id=instrument_id)
    pending = _find_exact(
        pending_rows, order_id=order_id, client_order_id=client_order_id
    )
    history = _find_exact(
        history_rows, order_id=order_id, client_order_id=client_order_id
    )
    if pending is not None and history is not None:
        return {"classification": "conflict", "pending": pending, "history": history}
    if pending is not None:
        return {"classification": "pending", "row": pending}
    if history is not None:
        state = _state(history)
        if state in TERMINAL_ORDER_STATES:
            if _row_has_fill(history):
                return {
                    "classification": (
                        "filled" if _filled_position_id(history) else "unknown"
                    ),
                    "row": history,
                }
            return {"classification": "terminal", "row": history}
        if state in FILLED_ORDER_STATES:
            return {"classification": "filled", "row": history}
        return {"classification": "unknown", "row": history}
    return {"classification": "missing"}


def _mark_recovery(
    session_factory,
    *,
    batch_id: int,
    reason: str,
    now: datetime,
):
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            raise LookupError("entry revision batch not found")
        batch.status = "recovery_required"
        batch.reason_code = reason
        batch.advance_claim_token = None
        batch.advance_claimed_at = None
        batch.updated_at = now
        session.commit()
    _observe_linked_revision_contract_best_effort(
        session_factory,
        batch_id=int(batch_id),
        observed_at=now,
    )
    return EntryRevisionExecutionResult("recovery_required", int(batch_id), reason)


def _observe_linked_revision_contract_best_effort(
    session_factory,
    *,
    batch_id: int,
    observed_at: datetime,
) -> None:
    try:
        from telegram_kol_research.instruction_execution_management_adapter import (
            project_linked_revision_batch_contract,
        )

        project_linked_revision_batch_contract(
            session_factory,
            revision_batch_id=int(batch_id),
            projected_at=observed_at,
        )
    except Exception:
        logger.exception(
            "revision execution-contract observation failed: batch_id=%s",
            int(batch_id),
        )


def _replacement_payload(*, replacement: dict[str, Any], binding: ExecutionBinding, desired: dict[str, Any]):
    position_side = str(binding.side).lower()
    leg = {
        **desired,
        "side": "buy" if position_side == "long" else "sell",
        "position_side": position_side,
    }
    draft = {
        "instrument_id": replacement.get("instrument_id"),
        "margin_mode": binding.margin_mode,
        "position_mode": binding.position_mode,
        "stop_loss": replacement.get("stop_loss"),
    }
    if str(desired.get("order_type") or "limit").lower() == "market":
        return build_deepcoin_market_order_payload(draft, leg), False
    return build_deepcoin_trigger_order_payload(draft, leg), True


def _decimal_field(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        try:
            value = Decimal(str(row[key]))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return value if value.is_finite() else None
    return None


def _economics_match(
    row: dict[str, Any], payload: dict[str, Any], desired: dict[str, Any]
) -> bool:
    expected_price = Decimal(str(payload.get("price")))
    expected_size = Decimal(str(payload.get("sz")))
    actual_price = _decimal_field(row, "price", "px", "triggerPrice", "triggerPx")
    actual_size = _decimal_field(row, "sz", "quantity", "size")
    expected_stop = _decimal_field(payload, "slTriggerPx")
    actual_stop = _decimal_field(row, "slTriggerPx", "stop_loss")
    _, actual_client_id = _row_identity(row)
    return (
        actual_price == expected_price
        and actual_size == expected_size
        and actual_client_id == str(desired.get("client_order_id") or "")
        and str(row.get("side") or "").lower()
        == str(payload.get("side") or "").lower()
        and str(row.get("posSide") or row.get("position_side") or "").lower()
        == str(payload.get("posSide") or "").lower()
        and str(row.get("orderType") or row.get("order_type") or "").lower()
        == str(payload.get("orderType") or "").lower()
        and expected_stop is not None
        and actual_stop == expected_stop
    )


def _exact_position(rows: list[dict[str, Any]], *, pos_id: str) -> dict[str, Any] | None:
    matches = [
        row
        for row in rows
        if str(row.get("posId") or row.get("pos_id") or "") == str(pos_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _position_value(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if row.get(key) not in (None, ""):
            return str(row[key])
    return None


def _verified_stop_row(
    row: dict[str, Any],
    *,
    pos_id: str,
    instrument_id: str,
    position_side: str,
    required_size: object,
    owned_order_ids: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    row_pos_id = str(row.get("posId") or row.get("pos_id") or "")
    row_instrument = str(row.get("instId") or row.get("instrument_id") or "")
    purpose = str(
        row.get("purpose")
        or row.get("role")
        or row.get("orderRole")
        or row.get("planType")
        or ""
    ).lower()
    trigger_order_type = str(
        row.get("triggerOrderType") or row.get("trigger_order_type") or ""
    ).upper()
    row_position_side = str(
        row.get("posSide") or row.get("position_side") or ""
    ).lower()
    order_id = str(row.get("ordId") or row.get("order_id") or row.get("orderId") or "")
    lineage_ids = {
        order_id,
        str(
            row.get("parentOrdId")
            or row.get("parentOrderId")
            or row.get("parent_order_id")
            or ""
        ),
    } - {""}
    stop_value = _position_value(
        row, "slTriggerPx", "slTriggerPrice", "stop_loss"
    )
    if stop_value is None and purpose in {"stop_loss", "sl", "stop", "primary_stop"}:
        stop_value = _position_value(row, "triggerPrice", "triggerPx")
    size = _decimal_field(row, "sz", "size", "quantity", "size_text")
    required = _decimal_field({"size": required_size}, "size")
    if (
        (
            (row_pos_id and row_pos_id != str(pos_id))
            or (not row_pos_id and not lineage_ids.intersection(owned_order_ids))
        )
        or (row_instrument and row_instrument.upper() != str(instrument_id).upper())
        or not (
            purpose in {"stop_loss", "sl", "stop", "primary_stop"}
            or (stop_value is not None and trigger_order_type == "TPSL")
        )
        or row_position_side != str(position_side).lower()
        or not order_id
        or stop_value is None
        or size is None
        or required is None
        or (size != 0 and size < required)
    ):
        return None
    return {**row, "status": "verified"}


def _read_verified_stop(
    client,
    *,
    pos_id: str,
    instrument_id: str,
    position_side: str,
    required_size: object,
    owned_order_ids: frozenset[str] = frozenset(),
):
    reader = getattr(client, "read_entry_revision_stop", None)
    if reader is not None:
        row = reader(pos_id=pos_id, inst_id=instrument_id)
        if (
            isinstance(row, dict)
            and str(row.get("status") or "").lower() == "verified"
        ):
            verified = _verified_stop_row(
                row,
                pos_id=pos_id,
                instrument_id=instrument_id,
                position_side=position_side,
                required_size=required_size,
                owned_order_ids=owned_order_ids,
            )
            if verified is not None:
                return verified
    try:
        rows = client.list_trigger_orders_pending(inst_id=instrument_id)
    except Exception:
        return None
    matches = [
        verified
        for row in rows
        if (
            verified := _verified_stop_row(
                row,
                pos_id=pos_id,
                instrument_id=instrument_id,
                position_side=position_side,
                required_size=required_size,
                owned_order_ids=owned_order_ids,
            )
        )
        is not None
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _replacement_client_order_id(*, batch_id: int, leg_index: int) -> str:
    digest = hashlib.sha256(
        f"entry-revision:{int(batch_id)}:{int(leg_index)}".encode("utf-8")
    ).hexdigest()
    return f"ER{digest[:18]}".upper()


def _scale_replacement_legs(
    legs: list[dict[str, Any]],
    *,
    remaining_risk: Decimal,
    target_risk: Decimal,
    quantity_step: object,
) -> list[dict[str, Any]]:
    if remaining_risk <= 0:
        return []
    scale = remaining_risk / target_risk
    step = Decimal(str(quantity_step))
    if not step.is_finite() or step <= 0:
        return []
    scaled: list[dict[str, Any]] = []
    for leg in legs:
        quantity = Decimal(str(leg.get("quantity")))
        scaled_quantity = ((quantity * scale) / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
        if scaled_quantity <= 0:
            continue
        scaled.append(
            {
                **leg,
                "quantity": float(scaled_quantity),
                "risk_budget_usdt": (
                    float(Decimal(str(leg["risk_budget_usdt"])) * scale)
                    if leg.get("risk_budget_usdt") is not None
                    else None
                ),
            }
        )
    return scaled


def execute_entry_revision(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    deepcoin_client,
    risk_reduction_executor=None,
    contract_value: object = None,
    quantity_step: object = "0.000001",
    min_quantity: object | None = None,
    executed_at: datetime | None = None,
) -> EntryRevisionExecutionResult:
    """Run one live revision under durable and single-process authority."""

    now = executed_at or datetime.now(UTC)
    if deployment_entry_admission_frozen():
        return EntryRevisionExecutionResult(
            "in_progress",
            int(batch_id),
            "deployment_entry_frozen",
        )
    rollout_mode = load_trading_settings(session_factory).entry_revision_v2_mode
    if rollout_mode == "disabled":
        return EntryRevisionExecutionResult("disabled", int(batch_id))
    if rollout_mode == "shadow":
        return EntryRevisionExecutionResult("shadow_planned", int(batch_id))
    authority = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id=f"batch:{int(batch_id)}",
        acquired_at=now,
        require_cancel_quiescence=False,
    )
    if not authority.acquired or authority.generation is None:
        return EntryRevisionExecutionResult(
            "in_progress",
            int(batch_id),
            authority.reason_code or "entry_revision_exchange_authority_unavailable",
        )
    authority_state = _EntryRevisionAuthorityState()
    try:
        result = _execute_entry_revision_with_position_authority(
            session_factory,
            batch_id=batch_id,
            deepcoin_client=deepcoin_client,
            risk_reduction_executor=risk_reduction_executor,
            contract_value=contract_value,
            quantity_step=quantity_step,
            min_quantity=min_quantity,
            executed_at=now,
            authority_state=authority_state,
        )
    except BaseException:
        raise
    if (
        result.status != "succeeded"
        and authority_state.write_started_or_ambiguous
    ):
        return result
    released = release_entry_revision_exchange_authority(
        session_factory,
        token=str(authority.token),
        owner_kind="entry_revision_worker",
        expected_generation=authority.generation,
        released_at=now,
    )
    if not released.released:
        return EntryRevisionExecutionResult(
            "recovery_required",
            int(batch_id),
            released.reason_code
            or "entry_revision_exchange_authority_release_failed",
        )
    return result


@serialized_position_authority_mutation
def _execute_entry_revision_with_position_authority(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    deepcoin_client,
    risk_reduction_executor=None,
    contract_value: object = None,
    quantity_step: object = "0.000001",
    min_quantity: object | None = None,
    executed_at: datetime | None = None,
    authority_state: _EntryRevisionAuthorityState | None = None,
) -> EntryRevisionExecutionResult:
    """Cancel exact old legs, prove terminal state, then submit exact replacements."""

    now = executed_at or datetime.now(UTC)
    execution_authority = authority_state or _EntryRevisionAuthorityState()
    claim_token = uuid.uuid4().hex
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None or batch.revision_kind != "entry_sizing":
            raise LookupError("entry revision batch not found")
        prior_leg_progress = (
            session.query(StrategyRevisionLeg.id)
            .filter(
                StrategyRevisionLeg.revision_batch_id == int(batch.id),
                StrategyRevisionLeg.status != "planned",
            )
            .first()
            is not None
        )
        prior_replacement_progress = (
            session.query(EntryRevisionReplacement.id)
            .filter(
                EntryRevisionReplacement.revision_batch_id == int(batch.id)
            )
            .first()
            is not None
        )
        if prior_leg_progress or prior_replacement_progress or batch.status in {
            "cancelling_old_entries",
            "old_entries_terminal",
            "rebuilding",
            "reconciling",
        }:
            execution_authority.mark_write_boundary()
        if batch.status == "shadow_planned":
            return EntryRevisionExecutionResult("shadow_planned", int(batch.id))
        if batch.status in TERMINAL_BATCH_STATES:
            return EntryRevisionExecutionResult(
                str(batch.status),
                int(batch.id),
                batch.reason_code,
            )
        binding_state = session.get(
            ExecutionBinding, int(batch.execution_binding_id)
        )
        lifecycle_state = session.get(
            StrategyLifecycle, int(batch.target_lifecycle_id)
        )
        thread_state = session.get(StrategyThread, int(batch.strategy_thread_id))
        if (
            binding_state is None
            or binding_state.status not in {"open", "active"}
            or lifecycle_state is None
            or lifecycle_state.lifecycle_status not in {"pending_entry", "entered"}
            or int(lifecycle_state.execution_binding_id or 0)
            != int(batch.execution_binding_id)
            or thread_state is None
            or thread_state.status != "active"
            or int(thread_state.current_lifecycle_id or 0)
            != int(batch.target_lifecycle_id)
        ):
            return _mark_recovery(
                session_factory,
                batch_id=int(batch.id),
                reason="entry_revision_target_lifecycle_inactive",
                now=now,
            )
        if batch.status in {"cancelling_old_entries", "old_entries_terminal"}:
            frozen = json.loads(batch.target_snapshot_json or "{}")
            frozen_legs = {
                int(item["execution_order_leg_id"]): item
                for item in frozen.get("entry_legs", [])
            }
            current_legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.id.in_(frozen_legs),
                )
                .all()
            )
            unexpected_active = (
                session.query(ExecutionOrderLeg.id)
                .filter(
                    ExecutionOrderLeg.execution_binding_id
                    == int(batch.execution_binding_id),
                    ExecutionOrderLeg.purpose == "entry",
                    ExecutionOrderLeg.status.not_in(TERMINAL_ORDER_STATES),
                    ExecutionOrderLeg.id.not_in(frozen_legs),
                )
                .first()
            )
            if (
                unexpected_active is not None
                or {int(leg.id) for leg in current_legs} != set(frozen_legs)
                or any(
                str(leg.order_id or "")
                != str(frozen_legs[int(leg.id)].get("order_id") or "")
                or str(leg.client_order_id or "")
                != str(
                    frozen_legs[int(leg.id)].get("client_order_id") or ""
                )
                or leg.attribution_status != "verified"
                for leg in current_legs
                )
            ):
                return _mark_recovery(
                    session_factory,
                    batch_id=int(batch.id),
                    reason="entry_revision_frozen_target_drift",
                    now=now,
                )
        active_management = (
            session.query(StrategyManagementBatch.id)
            .filter(
                StrategyManagementBatch.execution_binding_id
                == int(batch.execution_binding_id),
                StrategyManagementBatch.status.not_in(
                    ("succeeded", "blocked", "resolved")
                ),
            )
            .first()
        )
        if active_management is not None:
            return _mark_recovery(
                session_factory,
                batch_id=int(batch.id),
                reason="entry_revision_management_in_progress",
                now=now,
            )
        if batch.status == "planned":
            frozen = json.loads(batch.target_snapshot_json or "{}")
            frozen_legs = {
                int(item["execution_order_leg_id"]): item
                for item in frozen.get("entry_legs", [])
            }
            current_legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.execution_binding_id
                    == int(batch.execution_binding_id),
                    ExecutionOrderLeg.purpose == "entry",
                    ExecutionOrderLeg.status.not_in(TERMINAL_ORDER_STATES),
                )
                .all()
            )
            current_by_id = {int(leg.id): leg for leg in current_legs}
            target_drift = set(current_by_id) != set(frozen_legs)
            if not target_drift:
                for leg_id, item in frozen_legs.items():
                    leg = current_by_id[leg_id]
                    if (
                        str(leg.order_id or "") != str(item.get("order_id") or "")
                        or str(leg.client_order_id or "")
                        != str(item.get("client_order_id") or "")
                        or str(leg.status or "") != str(item.get("status") or "")
                        or leg.attribution_status != "verified"
                    ):
                        target_drift = True
                        break
            frozen_protection = {
                (
                    int(item["id"]),
                    str(item.get("order_id") or ""),
                    str(item.get("purpose") or ""),
                    str(item.get("status") or ""),
                    str(item.get("last_verified_at") or ""),
                )
                for item in frozen.get("protection", [])
            }
            current_protection = {
                (
                    int(row.id),
                    str(row.order_id or ""),
                    str(row.purpose or ""),
                    str(row.status or ""),
                    row.last_verified_at.isoformat()
                    if row.last_verified_at is not None
                    else "",
                )
                for row in session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.execution_binding_id
                    == int(batch.execution_binding_id)
                )
                .all()
            }
            target_drift = target_drift or current_protection != frozen_protection
            if target_drift:
                return _mark_recovery(
                    session_factory,
                    batch_id=int(batch.id),
                    reason="entry_revision_frozen_target_drift",
                    now=now,
                )
        if batch.advance_claim_token is not None:
            claimed_at = batch.advance_claimed_at
            comparable_now = now
            if claimed_at is not None and claimed_at.tzinfo is None:
                comparable_now = now.replace(tzinfo=None)
            stale = (
                claimed_at is not None
                and comparable_now - claimed_at >= ENTRY_REVISION_CLAIM_LEASE
            )
            if not stale:
                return EntryRevisionExecutionResult(
                    "in_progress", int(batch.id), "entry_revision_already_claimed"
                )
            ambiguous_old_write = (
                session.query(StrategyRevisionLeg.id)
                .filter(
                    StrategyRevisionLeg.revision_batch_id == int(batch.id),
                    StrategyRevisionLeg.status == "cancel_submitting",
                )
                .first()
                is not None
            )
            ambiguous_new_write = (
                session.query(EntryRevisionReplacement.id)
                .filter(
                    EntryRevisionReplacement.revision_batch_id == int(batch.id),
                    EntryRevisionReplacement.status.in_(("submit_reserved", "submitted")),
                )
                .first()
                is not None
            )
            if ambiguous_old_write or ambiguous_new_write:
                stale_batch_id = int(batch.id)
                session.rollback()
                return _mark_recovery(
                    session_factory,
                    batch_id=stale_batch_id,
                    reason="entry_revision_stale_claim_write_ambiguous",
                    now=now,
                )
            batch.advance_claim_token = None
            batch.advance_claimed_at = None
            session.commit()
        if batch.status not in {"planned", "cancelling_old_entries", "old_entries_terminal", "rebuilding", "reconciling"}:
            return _mark_recovery(
                session_factory,
                batch_id=int(batch.id),
                reason="entry_revision_restart_state_unknown",
                now=now,
            )
        claimed = session.execute(
            update(StrategyRevisionBatch)
            .where(
                StrategyRevisionBatch.id == int(batch.id),
                StrategyRevisionBatch.advance_claim_token.is_(None),
            )
            .values(
                status="cancelling_old_entries",
                advance_claim_token=claim_token,
                advance_claimed_at=now,
                updated_at=now,
            )
        )
        session.commit()
        if int(claimed.rowcount or 0) != 1:
            return EntryRevisionExecutionResult(
                "in_progress", int(batch_id), "entry_revision_claim_conflict"
            )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        replacement = json.loads(batch.replacement_json or "{}")
        instrument_id = str(replacement.get("instrument_id") or "")
        revision_legs = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch_id))
            .order_by(StrategyRevisionLeg.id.asc())
            .all()
        )
        leg_ids = [int(row.id) for row in revision_legs]
        target_snapshot = json.loads(batch.target_snapshot_json or "{}")
        snapshots = {
            int(item["execution_order_leg_id"]): item
            for item in target_snapshot.get("entry_legs", [])
        }
        owned_stop_order_ids_by_pos: dict[str, frozenset[str]] = {}
        for protection in target_snapshot.get("protection", []):
            if (
                str(protection.get("purpose") or "") == "stop_loss"
                and str(protection.get("status") or "") == "verified"
            ):
                pos_key = str(protection.get("pos_id") or "")
                owned_stop_order_ids_by_pos[pos_key] = frozenset(
                    {
                        *owned_stop_order_ids_by_pos.get(pos_key, frozenset()),
                        str(protection.get("order_id") or ""),
                    }
                    - {""}
                )
    observations: dict[int, dict[str, Any]] = {}
    for revision_leg_id in leg_ids:
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            execution_leg = session.get(
                ExecutionOrderLeg, int(revision_leg.execution_order_leg_id)
            )
            if revision_leg.action != "cancel_pending":
                continue
            if revision_leg.status == "cancel_submitting":
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_cancel_restart_requires_reconciliation",
                    now=now,
                )
            snapshot = snapshots[int(execution_leg.id)]
        observed = _read_exact_order(
            deepcoin_client,
            instrument_id=instrument_id,
            order_kind=str(snapshot["order_kind"]),
            order_id=revision_leg.order_id,
            client_order_id=revision_leg.client_order_id,
        )
        observations[revision_leg_id] = observed
        if observed["classification"] not in {"pending", "terminal", "filled"}:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_old_leg_state_unverified",
                now=now,
            )
    for revision_leg_id in leg_ids:
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            if revision_leg.action != "cancel_pending" or revision_leg.status == "cancelled":
                continue
            execution_leg = session.get(
                ExecutionOrderLeg, int(revision_leg.execution_order_leg_id)
            )
            snapshot = snapshots[int(execution_leg.id)]
            observed = observations[revision_leg_id]
            if observed["classification"] == "terminal":
                revision_leg.status = "cancelled"
                execution_leg.status = "cancelled"
                session.commit()
                continue
            if observed["classification"] == "filled":
                filled_row = observed.get("row") or {}
                filled_pos_id = _filled_position_id(filled_row)
                if not filled_pos_id:
                    session.rollback()
                    return _mark_recovery(
                        session_factory,
                        batch_id=batch_id,
                        reason="entry_revision_filled_position_identity_missing",
                        now=now,
                    )
                revision_leg.action = "retain_filled"
                revision_leg.status = "retained"
                revision_leg.pos_id = filled_pos_id
                execution_leg.status = "filled"
                execution_leg.pos_id = filled_pos_id
                execution_leg.attribution_status = "verified"
                session.commit()
                continue
            revision_leg.status = "cancel_submitting"
            revision_leg.updated_at = now
            order_id = revision_leg.order_id
            client_order_id = revision_leg.client_order_id
            session.commit()
        payload = {"instType": "SWAP", "instId": instrument_id}
        if order_id:
            payload["ordId"] = order_id
        if client_order_id:
            payload["clOrdId"] = client_order_id
        execution_authority.mark_write_boundary()
        try:
            if "trigger" in str(snapshot["order_kind"]).lower() or str(snapshot["order_kind"]).lower() == "limit":
                response = deepcoin_client.cancel_trigger_order(payload)
            else:
                response = deepcoin_client.cancel_order(payload)
        except Exception:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_cancel_outcome_unknown",
                now=now,
            )
        readback = _read_exact_order(
            deepcoin_client,
            instrument_id=instrument_id,
            order_kind=str(snapshot["order_kind"]),
            order_id=order_id,
            client_order_id=client_order_id,
        )
        if readback["classification"] not in {"terminal", "filled"}:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_cancel_not_terminal",
                now=now,
            )
        with session_factory() as session:
            revision_leg = session.get(StrategyRevisionLeg, revision_leg_id)
            execution_leg = session.get(
                ExecutionOrderLeg, int(revision_leg.execution_order_leg_id)
            )
            revision_leg.response_json = _canonical_json(response)
            revision_leg.status = "retained" if readback["classification"] == "filled" else "cancelled"
            revision_leg.action = "retain_filled" if readback["classification"] == "filled" else "cancel_pending"
            execution_leg.status = "filled" if readback["classification"] == "filled" else "cancelled"
            if readback["classification"] == "filled":
                filled_row = readback.get("row") or {}
                filled_pos_id = str(
                    filled_row.get("posId") or filled_row.get("pos_id") or ""
                )
                if not filled_pos_id:
                    session.rollback()
                    return _mark_recovery(
                        session_factory,
                        batch_id=batch_id,
                        reason="entry_revision_filled_position_identity_missing",
                        now=now,
                    )
                revision_leg.pos_id = filled_pos_id
                execution_leg.pos_id = filled_pos_id
                execution_leg.attribution_status = "verified"
            revision_leg.updated_at = now
            session.commit()
    with session_factory() as session:
        retained = (
            session.query(StrategyRevisionLeg)
            .filter(
                StrategyRevisionLeg.revision_batch_id == int(batch_id),
                StrategyRevisionLeg.status == "retained",
            )
            .all()
        )
        retained_pos_ids = sorted(
            {str(row.pos_id) for row in retained if str(row.pos_id or "")}
        )
    desired_legs = list(replacement.get("order_legs") or [])
    retained_risk_reference = None
    if retained_pos_ids:
        if len(retained_pos_ids) != 1:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_multiple_positions_require_recovery",
                now=now,
            )
        pos_id = retained_pos_ids[0]
        position = _exact_position(
            deepcoin_client.list_positions(inst_id=instrument_id), pos_id=pos_id
        )
        if position is None:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_position_readback_missing",
                now=now,
            )
        current_size = _position_value(position, "pos", "size", "quantity")
        stop_row = _read_verified_stop(
            deepcoin_client,
            pos_id=pos_id,
            instrument_id=instrument_id,
            position_side=str(binding.side),
            required_size=current_size,
            owned_order_ids=owned_stop_order_ids_by_pos.get(
                pos_id, frozenset()
            ),
        )
        if stop_row is None:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_verified_stop_missing",
                now=now,
            )
        average_entry = _position_value(position, "avgPx", "average_entry", "entryPrice")
        verified_stop = _position_value(
            stop_row, "triggerPrice", "triggerPx", "stop_loss", "slTriggerPx"
        )
        target_stop = replacement.get("stop_loss")
        target_risk = replacement.get("risk_budget_usdt")
        side = str(binding.side).lower()
        try:
            verified_stop_decimal = Decimal(str(verified_stop))
            target_stop_decimal = Decimal(str(target_stop))
        except (InvalidOperation, TypeError, ValueError):
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_verified_stop_invalid",
                now=now,
            )
        if (side == "long" and verified_stop_decimal < target_stop_decimal) or (
            side == "short" and verified_stop_decimal > target_stop_decimal
        ):
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_stop_would_be_weakened",
                now=now,
            )
        try:
            risk_decision = assess_revision_risk(
                quantity=current_size,
                average_entry=average_entry,
                stop_loss=verified_stop,
                contract_value=contract_value,
                side=side,
                target_risk_usdt=target_risk,
                quantity_step=quantity_step,
            )
        except EntryRevisionRiskError as exc:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason=str(exc),
                now=now,
            )
        market_snapshot = {
            "position": position,
            "verified_stop": stop_row,
            "risk_decision": {
                "action": risk_decision.action,
                "filled_risk_usdt": str(risk_decision.filled_risk_usdt),
                "target_risk_usdt": str(risk_decision.target_risk_usdt),
                "remaining_risk_usdt": str(risk_decision.remaining_risk_usdt),
                "target_quantity": str(risk_decision.target_quantity),
                "reduce_quantity": str(risk_decision.reduce_quantity),
            },
        }
        with session_factory() as session:
            batch = session.get(StrategyRevisionBatch, int(batch_id))
            batch.market_snapshot_json = _canonical_json(market_snapshot)
            batch.updated_at = now
            session.commit()
        retained_risk_reference = {
            "pos_id": pos_id,
            "quantity": str(current_size),
            "average_entry": str(average_entry),
            "verified_stop": str(verified_stop),
        }
        if risk_decision.action == "reduce_to_target":
            if risk_reduction_executor is None:
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_risk_reducer_unavailable",
                    now=now,
                )
            minimum = min_quantity if min_quantity is not None else quantity_step
            try:
                reduce_quantity = entry_revision_risk_reduction_delta(
                    current_size=current_size,
                    target_size=risk_decision.target_quantity,
                    quantity_step=quantity_step,
                    min_quantity=minimum,
                )
            except ManagementSizingError as exc:
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason=str(exc),
                    now=now,
                )
            try:
                execution_authority.mark_write_boundary()
                response = execute_entry_revision_risk_reduction_via_management(
                    batch_id=int(batch_id),
                    execution_binding_id=int(binding.id),
                    pos_id=pos_id,
                    current_quantity=str(current_size),
                    target_quantity=str(risk_decision.target_quantity),
                    reduce_quantity=str(reduce_quantity),
                    verified_stop=stop_row,
                    management_executor=risk_reduction_executor,
                )
            except Exception:
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_risk_reduction_unconfirmed",
                    now=now,
                )
            if str(response.get("status") or "").lower() != "succeeded":
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_risk_reduction_unconfirmed",
                    now=now,
                )
            refreshed_position = _exact_position(
                deepcoin_client.list_positions(inst_id=instrument_id), pos_id=pos_id
            )
            refreshed_stop = _read_verified_stop(
                deepcoin_client,
                pos_id=pos_id,
                instrument_id=instrument_id,
                position_side=str(binding.side),
                required_size=risk_decision.target_quantity,
                owned_order_ids=owned_stop_order_ids_by_pos.get(
                    pos_id, frozenset()
                ),
            )
            if (
                refreshed_position is None
                or refreshed_stop is None
                or Decimal(
                    str(_position_value(refreshed_position, "pos", "size", "quantity"))
                )
                != risk_decision.target_quantity
            ):
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_reduction_readback_mismatch",
                    now=now,
                )
            refreshed_stop_value = _decimal_field(
                refreshed_stop,
                "triggerPrice",
                "triggerPx",
                "stop_loss",
                "slTriggerPx",
            )
            if refreshed_stop_value is None or (
                side == "long" and refreshed_stop_value < target_stop_decimal
            ) or (
                side == "short" and refreshed_stop_value > target_stop_decimal
            ):
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_stop_would_be_weakened",
                    now=now,
                )
            retained_risk_reference = {
                "pos_id": pos_id,
                "quantity": str(risk_decision.target_quantity),
                "average_entry": str(
                    _position_value(
                        refreshed_position, "avgPx", "average_entry", "entryPrice"
                    )
                ),
                "verified_stop": str(
                    _position_value(
                        refreshed_stop,
                        "triggerPrice",
                        "triggerPx",
                        "stop_loss",
                        "slTriggerPx",
                    )
                ),
            }
            desired_legs = []
        elif risk_decision.action == "retain_at_target":
            desired_legs = []
        else:
            desired_legs = _scale_replacement_legs(
                desired_legs,
                remaining_risk=risk_decision.remaining_risk_usdt,
                target_risk=risk_decision.target_risk_usdt,
                quantity_step=quantity_step,
            )
    if retained_risk_reference is not None and desired_legs:
        refreshed_position = _exact_position(
            deepcoin_client.list_positions(inst_id=instrument_id),
            pos_id=str(retained_risk_reference["pos_id"]),
        )
        refreshed_stop = _read_verified_stop(
            deepcoin_client,
            pos_id=str(retained_risk_reference["pos_id"]),
            instrument_id=instrument_id,
            position_side=str(binding.side),
            required_size=retained_risk_reference["quantity"],
            owned_order_ids=owned_stop_order_ids_by_pos.get(
                str(retained_risk_reference["pos_id"]), frozenset()
            ),
        )
        refreshed_reference = (
            {
                "pos_id": str(retained_risk_reference["pos_id"]),
                "quantity": str(
                    _position_value(refreshed_position or {}, "pos", "size", "quantity")
                ),
                "average_entry": str(
                    _position_value(
                        refreshed_position or {},
                        "avgPx",
                        "average_entry",
                        "entryPrice",
                    )
                ),
                "verified_stop": str(
                    _position_value(
                        refreshed_stop or {},
                        "triggerPrice",
                        "triggerPx",
                        "stop_loss",
                        "slTriggerPx",
                    )
                ),
            }
            if refreshed_position is not None and refreshed_stop is not None
            else None
        )
        if refreshed_reference != retained_risk_reference:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_state_changed_before_rebuild",
                now=now,
            )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        existing_count = (
            session.query(EntryRevisionReplacement)
            .filter(EntryRevisionReplacement.revision_batch_id == int(batch_id))
            .count()
        )
        if existing_count == 0:
            for index, desired in enumerate(desired_legs):
                desired = {
                    **desired,
                    "client_order_id": _replacement_client_order_id(
                        batch_id=batch_id, leg_index=index
                    ),
                }
                session.add(
                    EntryRevisionReplacement(
                        revision_batch_id=int(batch_id),
                        leg_index=index,
                        desired_json=_canonical_json(desired),
                        status="planned",
                        client_order_id=str(desired.get("client_order_id") or "") or None,
                        updated_at=now,
                    )
                )
        batch.status = "rebuilding"
        batch.updated_at = now
        session.commit()
    with session_factory() as session:
        replacement_ids = [
            int(row_id)
            for (row_id,) in session.query(EntryRevisionReplacement.id)
            .filter(EntryRevisionReplacement.revision_batch_id == int(batch_id))
            .order_by(EntryRevisionReplacement.leg_index.asc())
            .all()
        ]
    for replacement_id in replacement_ids:
        with session_factory() as session:
            row = session.get(EntryRevisionReplacement, replacement_id)
            if row.status == "verified":
                continue
            if row.status != "planned":
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_replacement_restart_requires_reconciliation",
                    now=now,
                )
            if deployment_entry_admission_frozen():
                return EntryRevisionExecutionResult(
                    "in_progress",
                    int(batch_id),
                    "deployment_entry_frozen",
                )
            desired = json.loads(row.desired_json)
            payload, is_trigger = _replacement_payload(
                replacement=replacement, binding=binding, desired=desired
            )
            if not is_trigger:
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_market_replacement_requires_protected_path",
                    now=now,
                )
            row.status = "submit_reserved"
            row.request_json = _canonical_json(payload)
            row.updated_at = now
            session.commit()
        execution_authority.mark_write_boundary()
        try:
            response = (
                deepcoin_client.trigger_order(payload)
                if is_trigger
                else deepcoin_client.place_order(payload)
            )
        except Exception:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_replacement_outcome_unknown",
                now=now,
            )
        order_id = _response_order_id(response)
        if not order_id:
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_replacement_identity_missing",
                now=now,
            )
        with session_factory() as session:
            row = session.get(EntryRevisionReplacement, replacement_id)
            row.status = "submitted"
            row.order_id = order_id
            row.response_json = _canonical_json(response)
            row.updated_at = now
            session.commit()
        readback = _read_exact_order(
            deepcoin_client,
            instrument_id=instrument_id,
            order_kind="trigger_limit" if is_trigger else "market",
            order_id=order_id,
            client_order_id=str(desired.get("client_order_id") or "") or None,
        )
        readback_row = readback.get("row")
        if readback["classification"] not in {"pending", "filled"} or not isinstance(readback_row, dict):
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_replacement_readback_missing",
                now=now,
            )
        if not _economics_match(readback_row, payload, desired):
            return _mark_recovery(
                session_factory,
                batch_id=batch_id,
                reason="entry_revision_replacement_economics_mismatch",
                now=now,
            )
        filled_pos_id = (
            _filled_position_id(readback_row)
            if readback["classification"] == "filled"
            else None
        )
        filled_stop = None
        if readback["classification"] == "filled":
            if filled_pos_id:
                filled_stop = _read_verified_stop(
                    deepcoin_client,
                    pos_id=filled_pos_id,
                    instrument_id=instrument_id,
                    position_side=str(binding.side),
                    required_size=desired.get("quantity"),
                    owned_order_ids=frozenset({str(order_id)}),
                )
            if not filled_pos_id or filled_stop is None:
                return _mark_recovery(
                    session_factory,
                    batch_id=batch_id,
                    reason="entry_revision_replacement_position_unprotected",
                    now=now,
                )
        with session_factory() as session:
            row = session.get(EntryRevisionReplacement, replacement_id)
            row.status = "verified"
            row.readback_json = _canonical_json(readback_row)
            row.updated_at = now
            next_leg_index = (
                session.query(ExecutionOrderLeg.leg_index)
                .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
                .order_by(ExecutionOrderLeg.leg_index.desc())
                .limit(1)
                .scalar()
            )
            execution_leg = ExecutionOrderLeg(
                execution_binding_id=int(binding.id),
                strategy_instance_id=str(binding.strategy_instance_id),
                leg_index=int(next_leg_index or 0) + 1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id=order_id,
                client_order_id=str(desired.get("client_order_id") or "") or None,
                pos_id=filled_pos_id,
                attribution_status="verified",
                last_verified_at=now,
                status="active" if filled_pos_id else "submitted",
                request_json=_canonical_json(payload),
                response_json=_canonical_json(response),
                updated_at=now,
            )
            session.add(execution_leg)
            session.flush()
            row.execution_order_leg_id = int(execution_leg.id)
            if filled_pos_id and filled_stop is not None:
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=int(binding.id),
                    execution_order_leg_id=int(execution_leg.id),
                    strategy_instance_id=str(binding.strategy_instance_id),
                    pos_id=filled_pos_id,
                    instrument_id=instrument_id,
                    side=str(binding.side),
                    order_id=str(
                        filled_stop.get("ordId")
                        or filled_stop.get("order_id")
                        or filled_stop.get("orderId")
                    ),
                    purpose="stop_loss",
                    trigger_price=_position_value(
                        filled_stop,
                        "slTriggerPx",
                        "stop_loss",
                        "triggerPrice",
                        "triggerPx",
                    ),
                    size_text=_position_value(
                        filled_stop, "sz", "size", "quantity", "size_text"
                    ),
                    status="verified",
                    evidence_source="entry_revision_readback",
                    evidence=filled_stop,
                    seen_at=now,
                )
            binding_row = session.get(ExecutionBinding, int(binding.id))
            binding_row.order_id = ",".join(
                filter(None, (binding_row.order_id, order_id))
            )
            binding_row.client_order_id = ",".join(
                filter(
                    None,
                    (
                        binding_row.client_order_id,
                        str(desired.get("client_order_id") or ""),
                    ),
                )
            )
            if filled_pos_id:
                binding_row.pos_id = ",".join(
                    filter(None, (binding_row.pos_id, filled_pos_id))
                )
                binding_row.status = "active"
                batch_row = session.get(StrategyRevisionBatch, int(batch_id))
                lifecycle = session.get(
                    StrategyLifecycle, int(batch_row.target_lifecycle_id)
                )
                lifecycle.lifecycle_status = "entered"
                lifecycle.entered_at = lifecycle.entered_at or now
                lifecycle.updated_at = now
            session.commit()
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch.advance_claim_token != claim_token:
            session.rollback()
            execution_authority.mark_write_boundary()
            return EntryRevisionExecutionResult(
                "in_progress",
                int(batch_id),
                "entry_revision_claim_lost",
            )
        batch.status = "succeeded"
        batch.advance_claim_token = None
        batch.advance_claimed_at = None
        batch.completed_at = now
        batch.updated_at = now
        session.commit()
    _observe_linked_revision_contract_best_effort(
        session_factory,
        batch_id=int(batch_id),
        observed_at=now,
    )
    return EntryRevisionExecutionResult("succeeded", int(batch_id))


def run_entry_revision_worker_once(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    contract_spec_provider=None,
    management_executor=None,
    limit: int = 5,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Advance a bounded set of durable live revisions from the runtime worker."""

    if deployment_entry_admission_frozen():
        return {"status": "deployment_entry_frozen", "batch_ids": []}
    settings = load_trading_settings(session_factory)
    if settings.entry_revision_v2_mode == "disabled":
        return {"status": settings.entry_revision_v2_mode, "batch_ids": []}
    with session_factory() as session:
        activation = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_ACTIVATION_KEY)
            .one_or_none()
        )
        activation_cutoff = activation.updated_at if activation is not None else None
        if activation_cutoff is None:
            return {"status": "no_activation_generation", "batch_ids": []}
        if settings.entry_revision_v2_mode == "live":
            session.execute(
                update(StrategyRevisionBatch)
                .where(
                    StrategyRevisionBatch.revision_kind == "entry_sizing",
                    StrategyRevisionBatch.status == "shadow_planned",
                    StrategyRevisionBatch.planned_at < activation_cutoff,
                )
                .values(
                    status="blocked",
                    reason_code="entry_revision_shadow_generation_retired",
                    completed_at=activation_cutoff,
                    updated_at=activation_cutoff,
                )
            )
            session.commit()
        pending_fragment_rows = (
            session.query(
                EntryStrategyFragment.raw_message_id,
                EntryStrategyFragment.id,
                EntryStrategyFragment.message_id,
            )
            .join(
                RawMessage,
                RawMessage.id == EntryStrategyFragment.raw_message_id,
            )
            .filter(
                EntryStrategyFragment.status == "pending",
                EntryStrategyFragment.created_at >= activation_cutoff,
                RawMessage.created_at >= activation_cutoff,
                RawMessage.posted_at.is_not(None),
                RawMessage.posted_at >= activation_cutoff,
            )
            .order_by(
                EntryStrategyFragment.created_at.desc(),
                EntryStrategyFragment.id.desc(),
            )
            .limit(100)
            .all()
        )
    fragment_groups: dict[int, list[int]] = {}
    for raw_message_id, fragment_id, _message_id in sorted(
        pending_fragment_rows,
        key=lambda row: (int(row[2]), int(row[0]), int(row[1])),
    ):
        fragment_groups.setdefault(int(raw_message_id), []).append(int(fragment_id))
    for fragment_ids in fragment_groups.values():
        plan_post_submit_entry_fragment_revisions(
            session_factory,
            fragment_ids=tuple(fragment_ids),
            mode=settings.entry_revision_v2_mode,
            planned_at=executed_at,
        )
    if settings.entry_revision_v2_mode == "shadow":
        return {"status": "shadow_planned", "batch_ids": []}
    with session_factory() as session:
        batch_ids = [
            int(row_id)
            for (row_id,) in session.query(StrategyRevisionBatch.id)
            .filter(
                StrategyRevisionBatch.revision_kind == "entry_sizing",
                StrategyRevisionBatch.status.in_(
                    (
                        "planned",
                        "cancelling_old_entries",
                        "old_entries_terminal",
                        "rebuilding",
                        "reconciling",
                    )
                ),
            )
            .order_by(StrategyRevisionBatch.id.asc())
            .limit(max(1, min(int(limit), 20)))
            .all()
        ]
    completed: list[int] = []
    effective_management_executor = management_executor
    if effective_management_executor is None:
        effective_management_executor = lambda **kwargs: (
            execute_entry_revision_risk_reduction_live(
                session_factory,
                deepcoin_client=deepcoin_client,
                executed_at=executed_at,
                **kwargs,
            )
        )
    for batch_id in batch_ids:
        with session_factory() as session:
            batch = session.get(StrategyRevisionBatch, batch_id)
            replacement = json.loads(batch.replacement_json or "{}")
        spec = (
            contract_spec_provider.get_contract_spec(
                str(replacement.get("instrument_id") or "")
            )
            if contract_spec_provider is not None
            else None
        )
        execute_entry_revision(
            session_factory,
            batch_id=batch_id,
            deepcoin_client=deepcoin_client,
            risk_reduction_executor=effective_management_executor,
            contract_value=(spec.contract_value if spec is not None else None),
            quantity_step=(spec.quantity_step if spec is not None else "0.000001"),
            min_quantity=(spec.min_quantity if spec is not None else None),
            executed_at=executed_at,
        )
        completed.append(batch_id)
    return {"status": "completed", "batch_ids": completed}
