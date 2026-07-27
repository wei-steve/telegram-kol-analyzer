"""Fingerprint-guarded, database-only canonical TPSL ledger backfill."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Callable, Iterable

from sqlalchemy.exc import IntegrityError

from telegram_kol_research.models import (
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    RepairConfirmationToken,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


@dataclass(frozen=True, slots=True)
class TpslLedgerBackfillAction:
    order_id: str
    pos_id: str
    source_table: str
    source_row_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    strategy_instance_id: str | None
    instrument_id: str
    side: str
    purpose: str
    trigger_price: str | None
    size_text: str | None
    action_id: str


@dataclass(frozen=True, slots=True)
class TpslLedgerBackfillRefusal:
    order_id: str
    pos_id: str
    source_table: str
    reason: str


@dataclass(frozen=True, slots=True)
class TpslLedgerBackfillPlan:
    actions: tuple[TpslLedgerBackfillAction, ...]
    refusals: tuple[TpslLedgerBackfillRefusal, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class TpslLedgerBackfillResult:
    applied: int
    exchange_write_count: int = 0


def build_tpsl_ledger_backfill_plan(
    session_factory,
    *,
    positions: Iterable[dict[str, Any]],
    pending_orders: Iterable[dict[str, Any]],
    snapshot_complete: bool,
    venue: str = "deepcoin",
) -> TpslLedgerBackfillPlan:
    """Plan exact business-record promotion without inferring any owner."""

    if not snapshot_complete:
        refusal = TpslLedgerBackfillRefusal(
            order_id="",
            pos_id="",
            source_table="pending_tpsl_snapshot",
            reason="pending_snapshot_incomplete",
        )
        return _plan((), (refusal,))

    normalized_venue = str(venue or "deepcoin").lower()
    positions_by_id = {
        pos_id: row
        for row in positions
        if isinstance(row, dict)
        if (pos_id := _text(row, "PositionID", "posId", "pos_id", "id"))
        and _nonzero_position(row)
    }
    pending_by_id = {
        order_id: row
        for row in pending_orders
        if isinstance(row, dict)
        and str(row.get("triggerOrderType") or "").upper() == "TPSL"
        if (
            order_id := _text(
                row,
                "OrderSysID",
                "ordId",
                "orderId",
                "order_id",
                "algoId",
                "triggerOrderId",
                "id",
            )
        )
    }
    with session_factory() as session:
        legs = {
            int(row.id): row
            for row in (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.venue == normalized_venue)
                .all()
            )
        }
        existing = {
            str(row.order_id): str(row.pos_id)
            for row in (
                session.query(PositionProtectionLedger)
                .filter(PositionProtectionLedger.venue == normalized_venue)
                .all()
            )
            if str(row.order_id or "").strip()
        }
        sources: list[tuple[str, object]] = [
            *[
                ("position_backup_stop_orders", row)
                for row in (
                    session.query(PositionBackupStopOrder)
                    .filter(PositionBackupStopOrder.venue == normalized_venue)
                    .filter(PositionBackupStopOrder.status == "active")
                    .all()
                )
            ],
            *[
                ("position_take_profit_orders", row)
                for row in (
                    session.query(PositionTakeProfitOrder)
                    .filter(PositionTakeProfitOrder.venue == normalized_venue)
                    .filter(PositionTakeProfitOrder.status == "active")
                    .all()
                )
            ],
        ]

    actions: list[TpslLedgerBackfillAction] = []
    refusals: list[TpslLedgerBackfillRefusal] = []
    seen: dict[str, str] = dict(existing)
    for source_table, source in sources:
        order_id = str(getattr(source, "order_id", "") or "").strip()
        pos_id = str(getattr(source, "pos_id", "") or "").strip()
        refusal = _validate_source(
            source_table=source_table,
            source=source,
            order_id=order_id,
            pos_id=pos_id,
            positions_by_id=positions_by_id,
            pending_by_id=pending_by_id,
            legs=legs,
            seen=seen,
        )
        if refusal is not None:
            refusals.append(refusal)
            continue
        if order_id in existing:
            continue
        position = positions_by_id[pos_id]
        leg = legs[int(getattr(source, "execution_order_leg_id"))]
        purpose = (
            "stop_loss"
            if source_table == "position_backup_stop_orders"
            else "take_profit"
        )
        action_payload = {
            "order_id": order_id,
            "pos_id": pos_id,
            "source_table": source_table,
            "source_row_id": int(getattr(source, "id")),
            "execution_binding_id": int(getattr(source, "execution_binding_id")),
            "execution_order_leg_id": int(getattr(source, "execution_order_leg_id")),
            "strategy_instance_id": getattr(leg, "strategy_instance_id", None),
            "instrument_id": _instrument(position),
            "side": _side(position),
            "purpose": purpose,
            "trigger_price": str(getattr(source, "trigger_price", "") or "") or None,
            "size_text": (
                "0"
                if source_table == "position_backup_stop_orders"
                else (
                    str(getattr(source, "size_text", "") or "") or None
                )
            ),
        }
        actions.append(
            TpslLedgerBackfillAction(
                **action_payload,
                action_id=_fingerprint(action_payload),
            )
        )
        seen[order_id] = pos_id

    return _plan(
        tuple(sorted(actions, key=lambda row: (row.order_id, row.pos_id))),
        tuple(
            sorted(
                refusals,
                key=lambda row: (
                    row.order_id,
                    row.pos_id,
                    row.source_table,
                    row.reason,
                ),
            )
        ),
    )


def apply_tpsl_ledger_backfill_plan(
    session_factory,
    plan: TpslLedgerBackfillPlan,
    *,
    expected_fingerprint: str,
    confirmation_token: str,
    fresh_plan_builder: Callable[[], TpslLedgerBackfillPlan],
    applied_at: datetime | None = None,
) -> TpslLedgerBackfillResult:
    """Apply one unchanged plan atomically without any exchange client."""

    if not expected_fingerprint or expected_fingerprint != plan.fingerprint:
        raise ValueError("TPSL ledger backfill fingerprint mismatch")
    if plan.refusals:
        raise ValueError("TPSL ledger backfill plan contains refusals")
    clean_token = str(confirmation_token or "").strip()
    if len(clean_token) < 8:
        raise ValueError("confirmation_token is required")
    fresh = fresh_plan_builder()
    if fresh.fingerprint != plan.fingerprint:
        raise ValueError("TPSL ledger backfill plan changed")
    if fresh.refusals:
        raise ValueError("TPSL ledger backfill fresh plan contains refusals")
    now = applied_at or datetime.now(UTC)
    token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
    with session_factory() as session:
        session.add(
            RepairConfirmationToken(
                token_hash=token_hash,
                action_kind="canonical_tpsl_ledger_backfill",
                action_id=plan.fingerprint,
                pos_id="account",
                consumed_at=now,
            )
        )
        try:
            for action in fresh.actions:
                row = upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=action.execution_binding_id,
                    execution_order_leg_id=action.execution_order_leg_id,
                    strategy_instance_id=action.strategy_instance_id,
                    pos_id=action.pos_id,
                    instrument_id=action.instrument_id,
                    side=action.side,
                    order_id=action.order_id,
                    purpose=action.purpose,
                    trigger_price=action.trigger_price,
                    size_text=action.size_text,
                    status="verified",
                    evidence_source=f"canonical_backfill:{action.source_table}",
                    evidence={
                        "source_table": action.source_table,
                        "source_row_id": action.source_row_id,
                        "plan_fingerprint": plan.fingerprint,
                    },
                    seen_at=now,
                )
                if row is None:
                    raise ValueError("canonical TPSL ledger order ID missing")
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("TPSL ledger backfill identity conflict") from exc
        except Exception:
            session.rollback()
            raise
    return TpslLedgerBackfillResult(applied=len(fresh.actions))


def _validate_source(
    *,
    source_table: str,
    source: object,
    order_id: str,
    pos_id: str,
    positions_by_id: dict[str, dict[str, Any]],
    pending_by_id: dict[str, dict[str, Any]],
    legs: dict[int, ExecutionOrderLeg],
    seen: dict[str, str],
) -> TpslLedgerBackfillRefusal | None:
    reason = None
    if not order_id or not pos_id:
        reason = "business_identity_missing"
    elif pos_id not in positions_by_id:
        reason = "active_position_missing"
    elif order_id not in pending_by_id:
        reason = "pending_order_missing"
    elif order_id in seen and seen[order_id] != pos_id:
        reason = "duplicate_order_owner"
    else:
        leg = legs.get(int(getattr(source, "execution_order_leg_id", 0) or 0))
        if (
            leg is None
            or str(leg.pos_id or "") != pos_id
            or str(leg.attribution_status or "") != "verified"
            or str(leg.purpose or "") != "entry"
        ):
            reason = "verified_entry_leg_missing"
        else:
            position = positions_by_id[pos_id]
            order = pending_by_id[order_id]
            exchange_pos_id = _text(
                order,
                "PositionID",
                "closePosId",
                "close_pos_id",
                "closePositionId",
                "posId",
                "pos_id",
                "positionId",
            )
            if exchange_pos_id and exchange_pos_id != pos_id:
                reason = "exchange_position_conflict"
            elif (
                _instrument(position) != _instrument(order)
                or _side(position) != _side(order)
            ):
                reason = "exchange_identity_mismatch"
    if reason is None:
        return None
    return TpslLedgerBackfillRefusal(
        order_id=order_id,
        pos_id=pos_id,
        source_table=source_table,
        reason=reason,
    )


def _plan(
    actions: tuple[TpslLedgerBackfillAction, ...],
    refusals: tuple[TpslLedgerBackfillRefusal, ...],
) -> TpslLedgerBackfillPlan:
    payload = {
        "actions": [asdict(row) for row in actions],
        "refusals": [asdict(row) for row in refusals],
    }
    return TpslLedgerBackfillPlan(
        actions=actions,
        refusals=refusals,
        fingerprint=_fingerprint(payload),
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _instrument(payload: dict[str, Any]) -> str:
    return _text(payload, "instId", "instrumentId", "InstrumentID").upper()


def _side(payload: dict[str, Any]) -> str:
    value = _text(payload, "posSide", "side", "PosiDirection").lower()
    return {"buy": "long", "sell": "short"}.get(value, value)


def _nonzero_position(payload: dict[str, Any]) -> bool:
    try:
        return abs(float(_text(payload, "pos", "size", "positionSize", "Volume"))) > 0
    except (TypeError, ValueError):
        return False
