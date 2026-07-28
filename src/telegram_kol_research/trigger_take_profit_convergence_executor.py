"""Read-before-write planning for exact-leg staged take-profit convergence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    TriggerTakeProfitConvergence,
    utc_now,
)
from telegram_kol_research.position_authority_lock import serialized_position_authority_mutation
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
    submit_exact_position_sltp,
)
from telegram_kol_research.position_take_profit_orders import (
    record_take_profit_order,
)
from telegram_kol_research.position_protection_legs import bind_verified_exchange_order
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.take_profit_plan import TakeProfitPlanError, build_take_profit_plan
from telegram_kol_research.native_tpsl import (
    NativeTpslExpectation,
    NativeTpslOrder,
    match_native_tpsl_order,
    native_tpsl_take_profit_is_market,
    normalize_native_tpsl,
)


@dataclass(frozen=True, slots=True)
class TriggerTakeProfitConvergencePlan:
    status: str
    reason_code: str | None = None
    cancel_order_ids: tuple[str, ...] = ()
    payloads: tuple[dict[str, str], ...] = ()


def plan_trigger_take_profit_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client,
    contract_spec_provider=None,
    planned_at: datetime | None = None,
) -> TriggerTakeProfitConvergencePlan:
    """Produce a TP-only plan or fail closed before any exchange mutation."""

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, int(convergence_id))
        if convergence is None:
            return TriggerTakeProfitConvergencePlan("blocked", "convergence_not_found")
        if str(convergence.status) != "ready":
            return TriggerTakeProfitConvergencePlan(
                "blocked", convergence.reason_code or "convergence_not_ready"
            )
        prepared = _prepare_plan(
            session,
            convergence=convergence,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=(
                contract_spec_provider
                if contract_spec_provider is not None
                else getattr(deepcoin_client, "contract_spec_provider", None)
            ),
        )
        if isinstance(prepared, str):
            if prepared == "convergence_take_profit_already_converged":
                return TriggerTakeProfitConvergencePlan("already_converged", prepared)
            convergence.status = (
                "waiting_backup_stop"
                if prepared == "convergence_waiting_backup_stop"
                else "conflicted" if prepared.startswith("convergence_") else "blocked"
            )
            convergence.reason_code = prepared
            if planned_at is not None:
                convergence.updated_at = planned_at
            session.commit()
            return TriggerTakeProfitConvergencePlan(convergence.status, prepared)
        cancel_order_ids, payloads = prepared
        session.commit()
        return TriggerTakeProfitConvergencePlan(
            "ready", cancel_order_ids=tuple(cancel_order_ids), payloads=tuple(payloads)
        )


@serialized_position_authority_mutation
def execute_trigger_take_profit_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client,
    contract_spec_provider=None,
    executed_at: datetime | None = None,
) -> dict[str, object]:
    """Cancel exact-leg TP orders, then create the replacement TP set once."""

    now = executed_at or utc_now()
    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        planned_at=now,
    )
    if plan.status != "ready":
        if plan.status == "already_converged":
            with session_factory() as session:
                convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
                if convergence is not None and convergence.status == "ready":
                    convergence.status = "submitted"
                    convergence.reason_code = None
                    convergence.completed_at = now
                    convergence.updated_at = now
                    session.commit()
                    return {"convergence_id": convergence_id, "status": "submitted", "reason": None}
        return {"convergence_id": convergence_id, "status": plan.status, "reason": plan.reason_code}
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or convergence.status != "ready":
            return {"convergence_id": convergence_id, "status": "blocked", "reason": "convergence_not_ready"}
        convergence.status = "reserved"
        convergence.reason_code = None
        convergence.reserved_at = now
        convergence.updated_at = now
        session.commit()

    for payload_index, payload in enumerate(plan.payloads):
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(payload["posId"]),
                payload=payload,
                idempotency_key=(
                    f"tp-convergence:{convergence_id}:set:{payload_index}"
                ),
                live_execution_gate=lambda target_pos_id=str(
                    payload["posId"]
                ): exact_position_write_gate(
                    session_factory, pos_id=target_pos_id
                ),
                now_provider=lambda: now,
            )
        except Exception as exc:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error=exc)
        order_id = _response_order_id(response)
        if order_id is None:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error="missing order ID")
        try:
            open_positions = list(deepcoin_client.list_positions())
            exact_position = _exact_live_position(
                open_positions,
                pos_id=str(payload["posId"]),
                inst_id=str(payload["instId"]),
                side=str(payload["posSide"]),
            )
            pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=str(payload["instId"])))
            verified = _verified_native_take_profit(
                position=exact_position,
                open_positions=open_positions,
                pending=pending,
                order_id=order_id,
                payload=payload,
            )
        except Exception as exc:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_take_profit_pending_readback",
                error=exc,
            )
        if verified is None:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_take_profit_pending_readback",
                error="native TPSL take-profit was not verified in pending orders",
            )
        with session_factory() as session:
            convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
            if convergence is None or convergence.status != "reserved":
                return _freeze(session_factory, convergence_id, now, "convergence_response_persist_conflict")
            record_take_profit_order(
                session,
                venue="deepcoin",
                execution_binding_id=int(convergence.execution_binding_id),
                execution_order_leg_id=int(convergence.execution_order_leg_id),
                trigger_take_profit_convergence_id=int(convergence.id),
                pos_id=str(convergence.pos_id),
                order_id=order_id,
                trigger_price=str(payload["tpTriggerPx"]),
                size_text=str(payload["sz"]),
                created_at=now,
                evidence={
                    "source": "native_tpsl_pending_readback",
                    "response": _response_dict(response),
                    "native_tpsl": verified.raw,
                },
            )
            protection_leg = (
                session.query(PositionProtectionLeg)
                .filter(PositionProtectionLeg.execution_order_leg_id == convergence.execution_order_leg_id)
                .filter(PositionProtectionLeg.role == "take_profit")
                .filter(PositionProtectionLeg.planned_trigger_price == str(payload["tpTriggerPx"]))
                .one_or_none()
            )
            if protection_leg is not None:
                bind_verified_exchange_order(
                    session,
                    protection_leg,
                    exchange_order_id=order_id,
                    readback_evidence={"response": _response_dict(response), "native_tpsl": verified.raw},
                )
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=int(convergence.execution_binding_id),
                execution_order_leg_id=int(convergence.execution_order_leg_id),
                strategy_instance_id=session.get(ExecutionOrderLeg, convergence.execution_order_leg_id).strategy_instance_id,
                pos_id=str(convergence.pos_id),
                instrument_id=str(payload["instId"]), side=str(payload["posSide"]),
                order_id=order_id, purpose="take_profit", trigger_price=str(payload["tpTriggerPx"]),
                size_text=str(payload["sz"]), status="verified",
                evidence_source="trigger_take_profit_pending_readback",
                evidence={"native_tpsl": verified.raw}, seen_at=now,
            )
            session.commit()
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or convergence.status != "reserved":
            return _freeze(session_factory, convergence_id, now, "convergence_completion_persist_conflict")
        convergence.status = "submitted"
        convergence.completed_at = now
        convergence.updated_at = now
        session.commit()
    return {"convergence_id": convergence_id, "status": "submitted", "reason": None}


def execute_ready_trigger_take_profit_convergences(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    contract_spec_provider=None,
    processed_at: datetime | None = None,
    limit: int = 5,
) -> int:
    """Run a bounded set of durable ready tasks; terminal tasks are skipped."""

    with session_factory() as session:
        identifiers = [
            int(row.id)
            for row in (
                session.query(TriggerTakeProfitConvergence.id)
                .filter(TriggerTakeProfitConvergence.status == "ready")
                .order_by(TriggerTakeProfitConvergence.created_at, TriggerTakeProfitConvergence.id)
                .limit(max(0, int(limit)))
                .all()
            )
        ]
    completed = 0
    for convergence_id in identifiers:
        result = execute_trigger_take_profit_convergence(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            executed_at=processed_at,
        )
        if result.get("status") in {"submitted", "conflicted", "submit_unknown"}:
            completed += 1
    return completed


def _freeze(session_factory, convergence_id: int, now: datetime, reason: str, error: object | None = None):
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is not None and convergence.status in {"ready", "reserved"}:
            convergence.status = "submit_unknown" if reason.endswith("unknown") else "conflicted"
            convergence.reason_code = reason
            convergence.error_json = (
                json.dumps({"type": type(error).__name__, "message": str(error)[:512]}, ensure_ascii=False)
                if error is not None else None
            )
            convergence.completed_at = now
            convergence.updated_at = now
            session.commit()
            return {"convergence_id": convergence_id, "status": convergence.status, "reason": reason}
    return {"convergence_id": convergence_id, "status": "conflicted", "reason": reason}


def _response_dict(response: object) -> dict[str, object]:
    return response if isinstance(response, dict) else {"raw": str(response)[:512]}


def _response_order_id(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    data = data if isinstance(data, dict) else response
    value = data.get("ordId") or data.get("orderId")
    return str(value) if value not in (None, "") else None


def _prepare_plan(session, *, convergence, deepcoin_client, contract_spec_provider):
    if str(convergence.status) != "ready":
        return "convergence_not_ready"
    leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
    binding = session.get(ExecutionBinding, convergence.execution_binding_id)
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.purpose) != "entry"
        or str(leg.order_kind) not in {"trigger_limit", "market"}
        or str(leg.status).lower() != "active"
        or str(leg.attribution_status) != "verified"
        or not str(convergence.pos_id or "").strip()
        or str(leg.pos_id or "") != str(convergence.pos_id)
        or str(convergence.pos_id) not in _split_ids(binding.pos_id)
    ):
        return "convergence_exact_leg_not_verified"
    pos_id = str(convergence.pos_id)
    inst_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    try:
        positions = deepcoin_client.list_positions(inst_id=inst_id)
        pending = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
    except Exception:
        return "convergence_exchange_preflight_unavailable"
    matches = [
        row for row in positions if isinstance(row, dict)
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and str(row.get("posSide") or row.get("pos_side") or "").lower() == str(binding.side).lower()
        and str(row.get("mrgPosition") or row.get("posMode") or "").lower() == "split"
        and _positive_decimal(row.get("pos")) is not None
    ]
    if len(matches) != 1:
        return "convergence_exact_live_position_not_verified"
    size = _positive_decimal(matches[0].get("pos"))
    assert size is not None
    stop_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.execution_binding_id == binding.id)
        .filter(PositionProtectionLedger.execution_order_leg_id == leg.id)
        .filter(PositionProtectionLedger.pos_id == pos_id)
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.purpose.in_(("stop_loss", "combined")))
        .all()
    )
    has_stop = _has_verified_native_primary_stop(
        stop_rows=stop_rows,
        position=matches[0],
        open_positions=positions,
        pending=pending,
        position_size=size,
    )
    if not has_stop:
        return "convergence_verified_stop_missing"
    if not has_verified_exact_backup_stop(
        session,
        binding_id=int(binding.id),
        leg_id=int(leg.id),
        pos_id=pos_id,
        inst_id=inst_id,
        side=str(binding.side).lower(),
        pending=pending,
        position=matches[0],
        open_positions=positions,
    ):
        return "convergence_waiting_backup_stop"
    targets = _targets(convergence.desired_take_profits_json)
    if isinstance(targets, str):
        return targets
    try:
        spec = (
            contract_spec_provider.get_contract_spec(inst_id)
            if contract_spec_provider is not None
            else None
        )
    except Exception:
        return "convergence_target_contract_spec_unavailable"
    if spec is None:
        return "convergence_target_contract_spec_unavailable"
    sizes = _allocate_sizes(
        size,
        [allocation for _, allocation in targets],
        quantity_step=getattr(spec, "quantity_step", None),
        minimum_quantity=getattr(spec, "min_quantity", None),
    )
    if isinstance(sizes, str):
        return sizes
    common = {
        "instType": "SWAP", "instId": inst_id, "posId": pos_id,
        "posSide": str(binding.side).lower(), "mrgPosition": "split",
        "tdMode": str(binding.margin_mode).lower(),
    }
    payloads = [
        {
            **common, "tpTriggerPx": price, "tpTriggerPxType": "last",
            "tpOrdPx": "-1", "sz": _decimal_text(quantity),
        }
        for (price, _), quantity in zip(targets, sizes)
        if quantity > 0
    ]
    if not payloads:
        return "convergence_target_size_invalid"
    existing_protection_targets = (
        session.query(PositionProtectionLeg.id)
        .filter(PositionProtectionLeg.execution_order_leg_id == leg.id)
        .filter(PositionProtectionLeg.role == "take_profit")
        .count()
    )
    if existing_protection_targets == 0:
        primary = next(
            (
                row
                for row in stop_rows
                if str(row.order_id or "").strip() and row.trigger_price is not None
            ),
            None,
        )
        if primary is None:
            return "convergence_verified_stop_missing"
        try:
            from telegram_kol_research.position_protection_legs import (
                materialize_verified_position_protection,
            )

            materialize_verified_position_protection(
                session,
                venue="deepcoin",
                execution_order_leg_id=int(leg.id),
                pos_id=pos_id,
                primary_order_id=str(primary.order_id),
                primary_stop=str(primary.trigger_price),
                take_profits=[
                    (payload["tpTriggerPx"], payload["sz"]) for payload in payloads
                ],
            )
        except ValueError:
            return "convergence_protection_leg_conflict"
    active_orders = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.execution_binding_id == binding.id)
        .filter(PositionTakeProfitOrder.execution_order_leg_id == leg.id)
        .filter(PositionTakeProfitOrder.pos_id == pos_id)
        .filter(PositionTakeProfitOrder.status == "active")
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    desired_by_price = {payload["tpTriggerPx"]: payload for payload in payloads}
    known_order_position_ids = {
        str(row.order_id): str(row.pos_id)
        for row in session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.order_id.is_not(None))
        .all()
        if str(row.order_id or "").strip() and str(row.pos_id or "").strip()
    }
    satisfied_order_ids: set[str] = set()
    for order in active_orders:
        payload = desired_by_price.get(str(order.trigger_price))
        if payload is None or str(order.size_text or "") != payload["sz"]:
            return "convergence_owned_take_profit_mismatch"
        if _verified_native_take_profit(
            position=matches[0],
            open_positions=positions,
            pending=pending,
            order_id=str(order.order_id),
            payload=payload,
        ) is None:
            return "convergence_take_profit_missing_on_exchange"
        satisfied_order_ids.add(str(order.order_id))
    if _unowned_pending_take_profit_present(
        pending=pending,
        inst_id=inst_id,
        side=str(binding.side).lower(),
        pos_id=pos_id,
        owned_order_ids=satisfied_order_ids,
        known_order_position_ids=known_order_position_ids,
    ):
        return "convergence_unowned_take_profit_present"
    missing_payloads = [
        payload
        for payload in payloads
        if not any(
            str(order.trigger_price) == payload["tpTriggerPx"]
            and str(order.size_text or "") == payload["sz"]
            for order in active_orders
        )
    ]
    if not missing_payloads:
        return "convergence_take_profit_already_converged"
    return [], missing_payloads


def has_verified_exact_backup_stop(
    session,
    *,
    binding_id: int,
    leg_id: int,
    pos_id: str,
    inst_id: str,
    side: str,
    pending: list[dict[str, object]],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
) -> bool:
    """Require persisted exact ownership plus a same-order pending exchange read-back."""

    rows = (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.execution_binding_id == binding_id)
        .filter(PositionBackupStopOrder.execution_order_leg_id == leg_id)
        .filter(PositionBackupStopOrder.pos_id == pos_id)
        .filter(PositionBackupStopOrder.status == "active")
        .filter(PositionBackupStopOrder.order_id.is_not(None))
        .order_by(PositionBackupStopOrder.id.asc())
        .all()
    )
    if (
        str(position.get("instId") or "").upper() != inst_id
        or str(position.get("posId") or position.get("pos_id") or "") != pos_id
        or str(position.get("posSide") or position.get("side") or "").lower() != side
    ):
        return False
    for row in rows:
        try:
            request = json.loads(str(row.request_json or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        if (
            str(request.get("instId") or "").upper() != inst_id
            or str(request.get("posId") or request.get("closePosId") or "") != pos_id
            or str(request.get("posSide") or "").lower() != side
            or str(request.get("slOrdPx") or request.get("price") or "") != "-1"
            or not _present(request, "slTriggerPx", "slTriggerPrice", "triggerPrice")
        ):
            continue
        match = match_native_tpsl_order(
            position,
            [item for item in pending if isinstance(item, dict)],
            NativeTpslExpectation(
                purpose="stop_loss", trigger_price=str(row.trigger_price), size="0",
                ord_id=str(row.order_id),
            ),
            open_positions=[item for item in open_positions if isinstance(item, dict)],
        )
        if match.status == "verified":
            return True
    return False


def _has_verified_native_primary_stop(
    *,
    stop_rows: list[PositionProtectionLedger],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    pending: list[dict[str, object]],
    position_size: Decimal,
) -> bool:
    for row in stop_rows:
        if not row.order_id or row.trigger_price is None:
            continue
        for size in (position_size, Decimal("0")):
            match = match_native_tpsl_order(
                position,
                [item for item in pending if isinstance(item, dict)],
                NativeTpslExpectation(
                    purpose="stop_loss",
                    trigger_price=str(row.trigger_price),
                    size=size,
                    ord_id=str(row.order_id),
                ),
                open_positions=[item for item in open_positions if isinstance(item, dict)],
            )
            if match.status == "verified" and match.order is not None:
                return True
    return False


def _verified_native_take_profit(
    *,
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    pending: list[dict[str, object]],
    order_id: str,
    payload: dict[str, str],
) -> NativeTpslOrder | None:
    match = match_native_tpsl_order(
        position,
        [item for item in pending if isinstance(item, dict)],
        NativeTpslExpectation(
            purpose="take_profit",
            trigger_price=payload["tpTriggerPx"], size=payload["sz"], ord_id=order_id,
        ),
        open_positions=[item for item in open_positions if isinstance(item, dict)],
    )
    if match.status != "verified" or match.order is None:
        return None
    if not native_tpsl_take_profit_is_market(match.order.raw):
        return None
    return match.order


def _unowned_pending_take_profit_present(
    *,
    pending: list[dict[str, object]],
    inst_id: str,
    side: str,
    pos_id: str,
    owned_order_ids: set[str],
    known_order_position_ids: dict[str, str],
) -> bool:
    """Fail closed on any TP that could affect this exact side but lacks local ownership."""

    for raw in pending:
        if not isinstance(raw, dict):
            continue
        order = normalize_native_tpsl(raw)
        if (
            order is None
            or order.take_profit_trigger_price is None
            or order.inst_id != inst_id
            or order.pos_side != side
        ):
            continue
        if order.pos_id is not None and order.pos_id != pos_id:
            continue
        if (
            order.ord_id is not None
            and known_order_position_ids.get(order.ord_id) not in (None, pos_id)
        ):
            continue
        if order.ord_id not in owned_order_ids:
            return True
    return False


def _exact_live_position(
    positions: list[dict[str, object]],
    *,
    pos_id: str,
    inst_id: str,
    side: str,
) -> dict[str, object]:
    matches = [
        row for row in positions
        if isinstance(row, dict)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and str(row.get("instId") or "").upper() == inst_id.upper()
        and str(row.get("posSide") or row.get("side") or "").lower() == side.lower()
        and str(row.get("mrgPosition") or row.get("posMode") or "").lower() == "split"
        and _positive_decimal(row.get("pos") or row.get("size")) is not None
    ]
    if len(matches) != 1:
        raise RuntimeError("live_position_snapshot_not_unique_or_mismatched")
    return matches[0]


def _targets(value: str):
    try:
        rows = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return "convergence_target_plan_invalid"
    if not isinstance(rows, list) or not rows:
        return "convergence_target_plan_invalid"
    targets: list[tuple[str, Decimal]] = []
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, dict):
            return "convergence_target_plan_invalid"
        price = _positive_decimal(row.get("price"))
        allocation = _positive_decimal(row.get("allocation_pct"))
        if price is None or allocation is None:
            return "convergence_target_plan_invalid"
        targets.append((_decimal_text(price), allocation))
        total += allocation
    return targets if total == Decimal("100") else "convergence_target_plan_invalid"


def _allocate_sizes(
    size: Decimal,
    allocations: list[Decimal],
    *,
    quantity_step: object,
    minimum_quantity: object,
):
    try:
        plan = build_take_profit_plan(
            prices=range(1, len(allocations) + 1),
            side="long",
            configured_allocations=allocations,
            quantity=size,
            quantity_step=quantity_step,
            minimum_quantity=minimum_quantity,
        )
    except TakeProfitPlanError as exc:
        if "minimum" in str(exc):
            return "convergence_target_size_below_minimum"
        return "convergence_target_size_step_unverified"
    quantities = [Decimal(str(leg.quantity)) for leg in plan.legs]
    return quantities if all(quantity > 0 for quantity in quantities) else "convergence_target_size_invalid"


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _split_ids(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _present(row: dict, *keys: str) -> bool:
    return any(row.get(key) not in (None, "", 0, "0") for key in keys)
