"""Read-before-write planning for exact-leg staged take-profit convergence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
    utc_now,
)
from telegram_kol_research.position_authority_lock import serialized_position_authority_mutation
from telegram_kol_research.position_take_profit_orders import (
    record_take_profit_order,
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
        prepared = _prepare_plan(session, convergence=convergence, deepcoin_client=deepcoin_client)
        if isinstance(prepared, str):
            convergence.status = "conflicted" if prepared.startswith("convergence_") else "blocked"
            convergence.reason_code = prepared
            if planned_at is not None:
                convergence.updated_at = planned_at
            session.commit()
            return TriggerTakeProfitConvergencePlan(convergence.status, prepared)
        cancel_order_ids, payloads = prepared
        return TriggerTakeProfitConvergencePlan(
            "ready", cancel_order_ids=tuple(cancel_order_ids), payloads=tuple(payloads)
        )


@serialized_position_authority_mutation
def execute_trigger_take_profit_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client,
    executed_at: datetime | None = None,
) -> dict[str, object]:
    """Cancel exact-leg TP orders, then create the replacement TP set once."""

    now = executed_at or utc_now()
    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=deepcoin_client,
        planned_at=now,
    )
    if plan.status != "ready":
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

    for payload in plan.payloads:
        try:
            response = deepcoin_client.set_position_sltp(dict(payload))
        except Exception as exc:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error=exc)
        order_id = _response_order_id(response)
        if order_id is None:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error="missing order ID")
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
                evidence={"source": "trigger_take_profit_convergence", "response": _response_dict(response)},
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


def _prepare_plan(session, *, convergence, deepcoin_client):
    if str(convergence.status) != "ready":
        return "convergence_not_ready"
    leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
    binding = session.get(ExecutionBinding, convergence.execution_binding_id)
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.purpose) != "entry"
        or str(leg.order_kind) != "trigger_limit"
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
    pending_stop_prices = {
        str(row.get("ordId") or row.get("orderId") or ""): str(
            row.get("slTriggerPx") or row.get("slTriggerPrice") or row.get("closeSLTriggerPrice") or ""
        )
        for row in pending if isinstance(row, dict)
        and str(row.get("instId") or "").upper() == inst_id
        and _present(row, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
    }
    pending_stops = {
        str(row.get("ordId") or row.get("orderId") or ""): str(
            row.get("slTriggerPx") or row.get("slTriggerPrice") or row.get("closeSLTriggerPrice") or ""
        )
        for row in pending if isinstance(row, dict)
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and _present(row, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
    }
    has_stop = any(
        str(row.order_id) in pending_stops
        and (row.trigger_price is None or _same_numeric(str(row.trigger_price), pending_stops[str(row.order_id)]))
        for row in stop_rows
    ) or _has_parent_intent_backed_stop(
        session,
        leg_id=int(leg.id),
        stop_rows=stop_rows,
        pending_stop_prices=pending_stop_prices,
    )
    if not has_stop:
        return "convergence_verified_stop_missing"
    active_orders = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.execution_binding_id == binding.id)
        .filter(PositionTakeProfitOrder.execution_order_leg_id == leg.id)
        .filter(PositionTakeProfitOrder.pos_id == pos_id)
        .filter(PositionTakeProfitOrder.status == "active")
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    pending_ids = {
        str(row.get("ordId") or row.get("orderId") or "")
        for row in pending if isinstance(row, dict)
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and _present(row, "tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    }
    ledger_ids = {str(row.order_id) for row in active_orders}
    if pending_ids or ledger_ids:
        return "convergence_take_profit_already_present"
    targets = _targets(convergence.desired_take_profits_json)
    if isinstance(targets, str):
        return targets
    sizes = _allocate_sizes(size, [allocation for _, allocation in targets])
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
    return [], payloads


def _has_parent_intent_backed_stop(
    session,
    *,
    leg_id: int,
    stop_rows: list[PositionProtectionLedger],
    pending_stop_prices: dict[str, str],
) -> bool:
    """Accept a stop adopted from the exact parent trigger when Deepcoin omits posId."""

    intent = (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.execution_order_leg_id == leg_id)
        .one_or_none()
    )
    parent_order_id = str(intent.parent_trigger_order_id or "") if intent is not None else ""
    if not parent_order_id:
        return False
    for row in stop_rows:
        order_id = str(row.order_id or "")
        if (
            str(row.evidence_source) != "reconciliation_trigger_protection_intent"
            or str(intent.adopted_order_id or "") != order_id
            or order_id not in pending_stop_prices
            or (row.trigger_price is not None and not _same_numeric(str(row.trigger_price), pending_stop_prices[order_id]))
        ):
            continue
        try:
            evidence = json.loads(str(row.evidence_json or "{}"))
        except json.JSONDecodeError:
            continue
        if isinstance(evidence, dict) and str(evidence.get("parent_trigger_order_id") or "") == parent_order_id:
            return True
    return False


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


def _allocate_sizes(size: Decimal, allocations: list[Decimal]):
    sizes: list[Decimal] = []
    remaining = size
    for index, allocation in enumerate(allocations):
        quantity = (
            remaining if index == len(allocations) - 1
            else (size * allocation / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN)
        )
        if quantity <= 0:
            return "convergence_target_size_invalid"
        sizes.append(quantity)
        remaining -= quantity
    return sizes


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


def _same_numeric(left: str, right: str) -> bool:
    try:
        return Decimal(left) == Decimal(right)
    except (InvalidOperation, ValueError):
        return False
