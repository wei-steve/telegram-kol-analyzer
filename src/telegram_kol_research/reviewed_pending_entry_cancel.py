"""Fail-closed cancellation planning for reviewed pending Deepcoin entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    StrategyLifecycle,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)


_GOVERNED_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)
_PENDING_LEG_STATES = frozenset({"pending", "open", "submitted"})
_TERMINAL_LEG_STATES = frozenset(
    {"cancelled", "canceled", "expired", "rejected"}
)
_CANCELLED_HISTORY_STATES = frozenset(
    {"cancelled", "canceled", "cancel", "expired", "rejected"}
)
_FILLED_HISTORY_STATES = frozenset(
    {"filled", "partially_filled", "partially-filled", "partial_filled"}
)


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryTarget:
    order_id: str
    instrument_id: str
    lifecycle_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    trigger_price: str
    size: str
    embedded_stop_price: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelAction:
    order_id: str
    instrument_id: str
    lifecycle_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    strategy_instance_id: str
    trigger_price: str
    size: str
    embedded_stop_price: str
    request_fingerprint: str
    request_json_fingerprint: str
    exchange_row_fingerprint: str
    action_id: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelPlan:
    created_at: datetime
    actions: tuple[ReviewedPendingEntryCancelAction, ...]
    conflicts: tuple[dict[str, str], ...]
    completed_order_ids: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelResult:
    status: str
    order_id: str
    reason_code: str | None = None


REVIEWED_PENDING_ENTRY_TARGETS = (
    ReviewedPendingEntryTarget(
        order_id="1001124718697641",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=479,
        trigger_price="1827",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="7f9f86c10c30936a062984b6a5839b5db293f9dcbd0222d45a85b90c37f06130",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124718698413",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=480,
        trigger_price="1812",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="a05cae373185d2b221b47297b23c25cd854affc402310588ed4a19e3f8ffb3e6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022605",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=494,
        trigger_price="61890",
        size="13",
        embedded_stop_price="60900",
        request_fingerprint="fa3c307a5da05743b1bfc861757bab70713ed0b642699726ff86a8d516d982b0",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022650",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=495,
        trigger_price="61390",
        size="14",
        embedded_stop_price="60900",
        request_fingerprint="ca8806acf87c2b8d34354aea4e0538f71e952196fdf7f443effed7ec4654c401",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124898942178",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=911,
        execution_binding_id=308,
        execution_order_leg_id=532,
        trigger_price="2250",
        size="2.3",
        embedded_stop_price="2186",
        request_fingerprint="1f5a6157ee1fbc697c69ba164ff8bfc23f11a0def0916aabfaa5dca62579f99a",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905627977",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=533,
        trigger_price="73690",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a1838c649c7b17d2368c71d035719915700c7cd0e759c694c442134c49b787d6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905628046",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=534,
        trigger_price="73390",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a33495361faf3ea1e7a90436a2cd8f6b716d3477a394f4628d2c7a7d47d11786",
    ),
)


def build_reviewed_pending_entry_cancel_plan(
    session_factory,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    now: datetime | None = None,
) -> ReviewedPendingEntryCancelPlan:
    """Build a fresh, read-only plan for the closed reviewed target set."""

    created_at = now or datetime.now(UTC)
    reviewed = tuple(targets)
    order_ids = {target.order_id for target in reviewed}
    if len(order_ids) != len(reviewed):
        return _plan(
            created_at,
            (),
            ({"order_id": "*", "reason": "duplicate_reviewed_target"},),
        )

    instruments = tuple(
        sorted({*_GOVERNED_INSTRUMENTS, *(target.instrument_id for target in reviewed)})
    )
    try:
        snapshots = {
            instrument_id: {
                "positions": tuple(
                    row
                    for row in deepcoin_client.list_positions(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "regular": tuple(
                    row
                    for row in deepcoin_client.list_open_orders(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "pending": tuple(
                    row
                    for row in deepcoin_client.list_trigger_orders_pending(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "history": tuple(
                    row
                    for row in deepcoin_client.list_trigger_order_history(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "fills": tuple(
                    row
                    for row in deepcoin_client.list_trade_fills(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
            }
            for instrument_id in instruments
        }
    except Exception:
        return _plan(
            created_at,
            (),
            tuple(
                {"order_id": target.order_id, "reason": "exchange_snapshot_unavailable"}
                for target in reviewed
            ),
        )

    global_conflicts: list[dict[str, str]] = []
    if any(snapshot["positions"] for snapshot in snapshots.values()):
        global_conflicts.append(
            {"order_id": "*", "reason": "live_position_present"}
        )
    if any(snapshot["regular"] for snapshot in snapshots.values()):
        global_conflicts.append(
            {"order_id": "*", "reason": "regular_order_present"}
        )
    if any(
        not _order_id(row)
        for snapshot in snapshots.values()
        for row in snapshot["pending"]
    ):
        global_conflicts.append(
            {"order_id": "*", "reason": "unidentified_pending_trigger"}
        )
    unreviewed = sorted(
        {
            order_id
            for snapshot in snapshots.values()
            for row in snapshot["pending"]
            if (order_id := _order_id(row)) and order_id not in order_ids
        }
    )
    if unreviewed:
        global_conflicts.append(
            {"order_id": "*", "reason": "unreviewed_pending_trigger"}
        )
    if global_conflicts:
        return _plan(created_at, (), tuple(global_conflicts))

    actions: list[ReviewedPendingEntryCancelAction] = []
    conflicts: list[dict[str, str]] = []
    completed: list[str] = []
    with session_factory() as session:
        for target in reviewed:
            snapshot = snapshots.get(target.instrument_id, {})
            pending_rows = [
                row
                for row in snapshot.get("pending", ())
                if _order_id(row) == target.order_id
            ]
            history_rows = [
                row
                for row in snapshot.get("history", ())
                if _matches_order(row, target.order_id)
            ]
            fill_rows = [
                row
                for row in snapshot.get("fills", ())
                if _matches_order(row, target.order_id)
            ]

            leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
            binding = session.get(ExecutionBinding, target.execution_binding_id)
            lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
            intent_rows = (
                session.query(TriggerProtectionIntent)
                .filter(
                    TriggerProtectionIntent.venue == "deepcoin",
                    TriggerProtectionIntent.execution_order_leg_id
                    == target.execution_order_leg_id,
                )
                .all()
            )
            if not _local_identity_matches(
                target,
                leg=leg,
                binding=binding,
                lifecycle=lifecycle,
                intent_rows=intent_rows,
            ):
                conflicts.append(_conflict(target, "local_ownership_mismatch"))
                continue

            if not pending_rows:
                if _completed_state_matches(
                    session,
                    target=target,
                    leg=leg,
                    binding=binding,
                    lifecycle=lifecycle,
                    history_rows=history_rows,
                    fill_rows=fill_rows,
                ):
                    completed.append(target.order_id)
                else:
                    conflicts.append(_conflict(target, "reviewed_order_not_pending"))
                continue
            if len(pending_rows) != 1:
                conflicts.append(_conflict(target, "reviewed_order_not_unique"))
                continue
            row = pending_rows[0]
            if not _exchange_row_matches(row, target):
                conflicts.append(_conflict(target, "reviewed_exchange_row_changed"))
                continue
            if fill_rows or _history_has_filled_state(history_rows):
                conflicts.append(_conflict(target, "reviewed_order_has_fill_evidence"))
                continue
            if not _local_pending_state_matches(
                session,
                target=target,
                leg=leg,
                lifecycle=lifecycle,
                intent=intent_rows[0],
            ):
                conflicts.append(_conflict(target, "reviewed_local_state_changed"))
                continue

            request = _json_object(leg.request_json)
            if not _request_matches(request, target):
                conflicts.append(_conflict(target, "reviewed_request_changed"))
                continue
            base = {
                "order_id": target.order_id,
                "instrument_id": target.instrument_id,
                "lifecycle_id": target.lifecycle_id,
                "execution_binding_id": target.execution_binding_id,
                "execution_order_leg_id": target.execution_order_leg_id,
                "strategy_instance_id": str(leg.strategy_instance_id or ""),
                "trigger_price": target.trigger_price,
                "size": target.size,
                "embedded_stop_price": target.embedded_stop_price,
                "request_fingerprint": target.request_fingerprint,
                "request_json_fingerprint": _fingerprint(request),
                "exchange_row_fingerprint": _fingerprint(row),
            }
            actions.append(
                ReviewedPendingEntryCancelAction(
                    **base,
                    action_id=_fingerprint(base),
                )
            )

    return _plan(
        created_at,
        actions,
        conflicts,
        completed_order_ids=completed,
    )


def _local_identity_matches(
    target: ReviewedPendingEntryTarget,
    *,
    leg: ExecutionOrderLeg | None,
    binding: ExecutionBinding | None,
    lifecycle: StrategyLifecycle | None,
    intent_rows: list[TriggerProtectionIntent],
) -> bool:
    return bool(
        leg is not None
        and binding is not None
        and lifecycle is not None
        and len(intent_rows) == 1
        and int(leg.execution_binding_id) == target.execution_binding_id
        and int(lifecycle.execution_binding_id or 0) == target.execution_binding_id
        and str(leg.order_id or "") == target.order_id
        and str(leg.venue or "").lower() == "deepcoin"
        and leg.purpose == "entry"
        and str(binding.venue or "").lower() == "deepcoin"
        and str(binding.symbol or "").upper()
        == target.instrument_id.removesuffix("-USDT-SWAP")
        and str(binding.side or "").lower() == "long"
        and int(intent_rows[0].execution_binding_id)
        == target.execution_binding_id
        and str(intent_rows[0].parent_trigger_order_id or "")
        == target.order_id
        and str(intent_rows[0].request_fingerprint)
        == target.request_fingerprint
    )


def _local_pending_state_matches(
    session,
    *,
    target: ReviewedPendingEntryTarget,
    leg: ExecutionOrderLeg,
    lifecycle: StrategyLifecycle,
    intent: TriggerProtectionIntent,
) -> bool:
    if (
        str(leg.status or "").lower() not in _PENDING_LEG_STATES
        or leg.pos_id not in (None, "")
        or str(lifecycle.lifecycle_status or "") != "pending_entry"
        or str(intent.recovery_state or "") not in {"pending", "retrying"}
        or intent.adopted_order_id not in (None, "")
    ):
        return False
    primary = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.venue == "deepcoin",
            PositionProtectionLeg.execution_order_leg_id
            == target.execution_order_leg_id,
            PositionProtectionLeg.role == "primary_stop",
        )
        .all()
    )
    backup = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.venue == "deepcoin",
            PositionProtectionLeg.execution_order_leg_id
            == target.execution_order_leg_id,
            PositionProtectionLeg.role == "backup_stop",
        )
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter(
            TriggerTakeProfitConvergence.venue == "deepcoin",
            TriggerTakeProfitConvergence.execution_order_leg_id
            == target.execution_order_leg_id,
        )
        .all()
    )
    return bool(
        len(primary) == 1
        and _numbers_equal(
            primary[0].planned_trigger_price,
            target.embedded_stop_price,
        )
        and str(primary[0].status or "") in {"planned", "waiting_fill"}
        and len(backup) == 1
        and str(backup[0].status or "") in {"planned", "waiting_fill"}
        and len(convergence) == 1
        and str(convergence[0].status or "")
        in {"waiting_backup_stop", "waiting_position", "ready"}
    )


def _completed_state_matches(
    session,
    *,
    target: ReviewedPendingEntryTarget,
    leg: ExecutionOrderLeg,
    binding: ExecutionBinding,
    lifecycle: StrategyLifecycle,
    history_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> bool:
    events = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id
            == target.execution_binding_id,
            ExecutionEvent.order_id == target.order_id,
            ExecutionEvent.action
            == "cancel_reviewed_pending_entry",
            ExecutionEvent.status == "confirmed",
        )
        .all()
    )
    return bool(
        not fill_rows
        and _history_has_cancelled_state(history_rows)
        and str(leg.status or "").lower() in _TERMINAL_LEG_STATES
        and len(events) == 1
        and (
            str(binding.status or "").lower() in {"open", "active", "cancelled"}
        )
        and (
            str(lifecycle.lifecycle_status or "")
            in {"pending_entry", "expired", "cancelled", "exited"}
        )
    )


def _exchange_row_matches(
    row: dict[str, Any],
    target: ReviewedPendingEntryTarget,
) -> bool:
    return bool(
        str(row.get("instId") or "").upper() == target.instrument_id
        and _order_id(row) == target.order_id
        and str(row.get("triggerOrderType") or "").lower() == "conditional"
        and str(row.get("side") or "").lower() == "buy"
        and str(row.get("posSide") or "").lower() == "long"
        and _numbers_equal(
            row.get("triggerPx") or row.get("triggerPrice"),
            target.trigger_price,
        )
        and _numbers_equal(row.get("sz") or row.get("size"), target.size)
        and _numbers_equal(
            row.get("closeSLTriggerPrice")
            or row.get("slTriggerPx")
            or row.get("slTriggerPrice"),
            target.embedded_stop_price,
        )
    )


def _request_matches(
    request: dict[str, Any],
    target: ReviewedPendingEntryTarget,
) -> bool:
    return bool(
        str(request.get("instId") or "").upper() == target.instrument_id
        and str(request.get("side") or "").lower() == "buy"
        and str(request.get("posSide") or "").lower() == "long"
        and _numbers_equal(
            request.get("triggerPrice") or request.get("triggerPx"),
            target.trigger_price,
        )
        and _numbers_equal(request.get("sz") or request.get("size"), target.size)
        and _numbers_equal(
            request.get("slTriggerPx")
            or request.get("slTriggerPrice")
            or request.get("closeSLTriggerPrice"),
            target.embedded_stop_price,
        )
    )


def _history_has_filled_state(rows: Iterable[dict[str, Any]]) -> bool:
    return any(_state(row) in _FILLED_HISTORY_STATES for row in rows)


def _history_has_cancelled_state(rows: Iterable[dict[str, Any]]) -> bool:
    return any(_state(row) in _CANCELLED_HISTORY_STATES for row in rows)


def _state(row: dict[str, Any]) -> str:
    return str(
        row.get("state")
        or row.get("status")
        or row.get("orderStatus")
        or ""
    ).lower()


def _matches_order(row: dict[str, Any], order_id: str) -> bool:
    return _order_id(row) == order_id


def _order_id(row: dict[str, Any]) -> str:
    return str(
        row.get("ordId")
        or row.get("orderId")
        or row.get("triggerOrderId")
        or row.get("order_id")
        or ""
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _numbers_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _conflict(
    target: ReviewedPendingEntryTarget,
    reason: str,
) -> dict[str, str]:
    return {"order_id": target.order_id, "reason": reason}


def _fingerprint(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan(
    created_at: datetime,
    actions: Iterable[ReviewedPendingEntryCancelAction],
    conflicts: Iterable[dict[str, str]],
    *,
    completed_order_ids: Iterable[str] = (),
) -> ReviewedPendingEntryCancelPlan:
    ordered_actions = tuple(sorted(actions, key=lambda item: item.order_id))
    ordered_conflicts = tuple(
        sorted(
            (dict(item) for item in conflicts),
            key=lambda item: (item.get("order_id", ""), item.get("reason", "")),
        )
    )
    completed = tuple(sorted(str(value) for value in completed_order_ids))
    material = {
        "actions": [asdict(action) for action in ordered_actions],
        "conflicts": ordered_conflicts,
        "completed_order_ids": completed,
    }
    return ReviewedPendingEntryCancelPlan(
        created_at=created_at,
        actions=ordered_actions,
        conflicts=ordered_conflicts,
        completed_order_ids=completed,
        fingerprint=_fingerprint(material),
    )
