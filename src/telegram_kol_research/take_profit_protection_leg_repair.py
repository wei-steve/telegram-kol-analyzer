"""Supervised convergence of verified TP orders onto logical protection legs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    PositionProtectionLeg,
    PositionTakeProfitOrder,
    RepairConfirmationToken,
)
from telegram_kol_research.native_tpsl import (
    NativeTpslExpectation,
    match_native_tpsl_order,
    native_tpsl_take_profit_is_market,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.position_protection_legs import (
    bind_verified_exchange_order,
)
from telegram_kol_research.repair_confirmation import (
    require_repair_confirmation_token_unused,
)


@dataclass(frozen=True, slots=True)
class TakeProfitProtectionLegRepairAction:
    logical_leg_id: int
    venue: str
    role: str
    leg_index: int
    binding_id: int
    entry_leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    order_id: str
    planned_trigger_price: str
    submitted_trigger_price: str
    submitted_size: str
    evidence_fingerprint: str
    action_id: str


@dataclass(frozen=True, slots=True)
class TakeProfitProtectionLegRepairRefusal:
    logical_leg_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class TakeProfitProtectionLegRepairPlan:
    created_at: datetime
    actions: tuple[TakeProfitProtectionLegRepairAction, ...]
    refusals: tuple[TakeProfitProtectionLegRepairRefusal, ...]
    fingerprint: str
    confirmation_token: str


@dataclass(frozen=True, slots=True)
class TakeProfitProtectionLegRepairResult:
    applied: int


def build_take_profit_protection_leg_repair_plan(
    session_factory,
    *,
    deepcoin_client,
    observed_at: datetime | None = None,
) -> TakeProfitProtectionLegRepairPlan:
    """Build a read-only repair plan from exact durable and exchange evidence."""

    created_at = observed_at or datetime.now(UTC)
    actions: list[TakeProfitProtectionLegRepairAction] = []
    refusals: list[TakeProfitProtectionLegRepairRefusal] = []
    positions_cache: dict[str, list[dict[str, Any]]] = {}
    pending_cache: dict[str, list[dict[str, Any]]] = {}

    with session_factory() as session:
        logical_legs = (
            session.query(PositionProtectionLeg)
            .filter(PositionProtectionLeg.venue == "deepcoin")
            .filter(PositionProtectionLeg.role == "take_profit")
            .filter(PositionProtectionLeg.status == "protection_recovery_pending")
            .filter(PositionProtectionLeg.exchange_order_id.is_(None))
            .order_by(PositionProtectionLeg.id.asc())
            .all()
        )
        for logical_leg in logical_legs:
            refusal = _plan_one(
                session,
                logical_leg=logical_leg,
                deepcoin_client=deepcoin_client,
                positions_cache=positions_cache,
                pending_cache=pending_cache,
            )
            if isinstance(refusal, TakeProfitProtectionLegRepairRefusal):
                refusals.append(refusal)
            else:
                actions.append(refusal)

    actions_tuple = tuple(sorted(actions, key=lambda row: row.logical_leg_id))
    refusals_tuple = tuple(
        sorted(refusals, key=lambda row: (row.logical_leg_id, row.reason))
    )
    fingerprint = _fingerprint(
        {
            "actions": [asdict(row) for row in actions_tuple],
            "refusals": [asdict(row) for row in refusals_tuple],
        }
    )
    confirmation_token = "tp-leg-repair-" + hashlib.sha256(
        ("confirm:" + fingerprint).encode("utf-8")
    ).hexdigest()[:32]
    return TakeProfitProtectionLegRepairPlan(
        created_at=created_at,
        actions=actions_tuple,
        refusals=refusals_tuple,
        fingerprint=fingerprint,
        confirmation_token=confirmation_token,
    )


def _plan_one(
    session,
    *,
    logical_leg: PositionProtectionLeg,
    deepcoin_client,
    positions_cache: dict[str, list[dict[str, Any]]],
    pending_cache: dict[str, list[dict[str, Any]]],
) -> TakeProfitProtectionLegRepairAction | TakeProfitProtectionLegRepairRefusal:
    def refuse(reason: str) -> TakeProfitProtectionLegRepairRefusal:
        return TakeProfitProtectionLegRepairRefusal(
            logical_leg_id=int(logical_leg.id),
            reason=reason,
        )

    planned_price = _positive_decimal(logical_leg.planned_trigger_price)
    if planned_price is None:
        return refuse("logical_take_profit_price_invalid")
    numeric_siblings = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.execution_order_leg_id
            == int(logical_leg.execution_order_leg_id)
        )
        .filter(PositionProtectionLeg.role == "take_profit")
        .all()
    )
    if sum(
        _positive_decimal(row.planned_trigger_price) == planned_price
        for row in numeric_siblings
    ) != 1:
        return refuse("logical_take_profit_price_ambiguous")

    entry_leg = session.get(
        ExecutionOrderLeg, int(logical_leg.execution_order_leg_id)
    )
    if (
        entry_leg is None
        or str(entry_leg.venue or "").lower() != "deepcoin"
        or str(entry_leg.purpose or "") != "entry"
        or str(entry_leg.status or "").lower() != "active"
        or str(entry_leg.attribution_status or "").lower() != "verified"
        or not str(entry_leg.pos_id or "").strip()
        or str(entry_leg.pos_id) != str(logical_leg.pos_id or "")
        or int(entry_leg.execution_binding_id)
        != int(logical_leg.execution_binding_id)
    ):
        return refuse("entry_leg_not_active_verified")
    binding = session.get(ExecutionBinding, int(entry_leg.execution_binding_id))
    if (
        binding is None
        or str(binding.venue or "").lower() != "deepcoin"
        or str(binding.status or "").lower() != "active"
        or str(entry_leg.pos_id) not in _split_ids(binding.pos_id)
    ):
        return refuse("execution_binding_owner_mismatch")

    durable_orders = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.venue == "deepcoin")
        .filter(
            PositionTakeProfitOrder.execution_binding_id
            == int(logical_leg.execution_binding_id)
        )
        .filter(
            PositionTakeProfitOrder.execution_order_leg_id
            == int(logical_leg.execution_order_leg_id)
        )
        .filter(PositionTakeProfitOrder.pos_id == str(logical_leg.pos_id))
        .filter(PositionTakeProfitOrder.status == "active")
        .all()
    )
    durable_matches = [
        row
        for row in durable_orders
        if _positive_decimal(row.trigger_price) == planned_price
    ]
    if len(durable_matches) != 1:
        return refuse("active_take_profit_order_not_unique")
    durable_order = durable_matches[0]
    submitted_size = _nonnegative_decimal(durable_order.size_text)
    if submitted_size is None:
        return refuse("active_take_profit_size_invalid")

    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.order_id == str(durable_order.order_id))
        .all()
    )
    if len(ledger_rows) != 1:
        return refuse("verified_ledger_owner_mismatch")
    ledger = ledger_rows[0]
    if (
        int(ledger.execution_binding_id) != int(logical_leg.execution_binding_id)
        or int(ledger.execution_order_leg_id)
        != int(logical_leg.execution_order_leg_id)
        or str(ledger.pos_id or "") != str(logical_leg.pos_id)
        or str(ledger.status or "").lower() != "verified"
        or str(ledger.purpose or "").lower() != "take_profit"
        or _positive_decimal(ledger.trigger_price) != planned_price
        or _nonnegative_decimal(ledger.size_text) != submitted_size
        or str(ledger.side or "").lower() != str(binding.side or "").lower()
        or not str(ledger.instrument_id or "").strip()
    ):
        return refuse("verified_ledger_owner_mismatch")
    if (
        session.query(PositionProtectionLeg.id)
        .filter(PositionProtectionLeg.venue == "deepcoin")
        .filter(PositionProtectionLeg.exchange_order_id == str(durable_order.order_id))
        .filter(PositionProtectionLeg.id != int(logical_leg.id))
        .first()
        is not None
    ):
        return refuse("logical_order_owner_collision")

    instrument_id = str(ledger.instrument_id).upper()
    side = str(ledger.side).lower()
    if instrument_id not in positions_cache:
        try:
            positions_cache[instrument_id] = [
                row
                for row in deepcoin_client.list_positions(inst_id=instrument_id)
                if isinstance(row, dict)
            ]
        except Exception:
            return refuse("exchange_position_snapshot_unavailable")
    if instrument_id not in pending_cache:
        try:
            pending_cache[instrument_id] = [
                row
                for row in deepcoin_client.list_trigger_orders_pending(
                    inst_id=instrument_id
                )
                if isinstance(row, dict)
            ]
        except Exception:
            return refuse("exchange_take_profit_snapshot_unavailable")
    positions = [
        row
        for row in positions_cache[instrument_id]
        if str(row.get("instId") or "").upper() == instrument_id
        and str(row.get("posId") or row.get("pos_id") or "")
        == str(logical_leg.pos_id)
        and str(row.get("posSide") or row.get("pos_side") or "").lower()
        == side
        and str(row.get("mrgPosition") or row.get("posMode") or "").lower()
        == "split"
        and _positive_decimal(row.get("pos")) is not None
    ]
    if len(positions) != 1:
        return refuse("exchange_position_owner_mismatch")
    match = match_native_tpsl_order(
        positions[0],
        pending_cache[instrument_id],
        NativeTpslExpectation(
            purpose="take_profit",
            trigger_price=planned_price,
            size=submitted_size,
            ord_id=str(durable_order.order_id),
        ),
        open_positions=positions_cache[instrument_id],
    )
    if (
        match.status != "verified"
        or match.order is None
        or match.order.pos_id != str(logical_leg.pos_id)
    ):
        return refuse("exchange_take_profit_mismatch")
    if not native_tpsl_take_profit_is_market(match.order.raw):
        return refuse("exchange_take_profit_not_market")

    evidence = {
        "logical_leg_id": int(logical_leg.id),
        "venue": str(logical_leg.venue),
        "role": str(logical_leg.role),
        "leg_index": int(logical_leg.leg_index),
        "binding_id": int(logical_leg.execution_binding_id),
        "entry_leg_id": int(logical_leg.execution_order_leg_id),
        "pos_id": str(logical_leg.pos_id),
        "instrument_id": instrument_id,
        "side": side,
        "order_id": str(durable_order.order_id),
        "planned_trigger_price": str(logical_leg.planned_trigger_price),
        "submitted_trigger_price": str(durable_order.trigger_price),
        "submitted_size": str(durable_order.size_text),
    }
    evidence_fingerprint = _fingerprint(evidence)
    return TakeProfitProtectionLegRepairAction(
        **evidence,
        evidence_fingerprint=evidence_fingerprint,
        action_id=_fingerprint(
            {
                "kind": "take_profit_protection_leg_repair",
                "evidence_fingerprint": evidence_fingerprint,
            }
        ),
    )


def apply_take_profit_protection_leg_repair_plan(
    session_factory,
    plan: TakeProfitProtectionLegRepairPlan,
    *,
    deepcoin_client,
    action_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    applied_at: datetime | None = None,
) -> TakeProfitProtectionLegRepairResult:
    """Bind one reviewed existing order; never call an exchange writer."""

    if str(expected_fingerprint or "") != plan.fingerprint:
        raise ValueError("repair plan fingerprint mismatch")
    selected = [row for row in plan.actions if row.action_id == str(action_id)]
    if len(selected) != 1:
        raise ValueError("exactly one reviewed repair action is required")
    if str(confirmation_token or "") != plan.confirmation_token:
        raise ValueError("confirmation token mismatch")
    require_repair_confirmation_token_unused(
        session_factory,
        confirmation_token=confirmation_token,
    )
    with position_authority_lock():
        fresh = build_take_profit_protection_leg_repair_plan(
            session_factory,
            deepcoin_client=deepcoin_client,
            observed_at=applied_at,
        )
        if fresh.fingerprint != expected_fingerprint:
            raise ValueError("repair plan fingerprint changed")
        fresh_selected = [
            row for row in fresh.actions if row.action_id == str(action_id)
        ]
        if len(fresh_selected) != 1:
            raise ValueError("exactly one fresh repair action is required")
        action = fresh_selected[0]
        consumed_at = applied_at or datetime.now(UTC)
        token_hash = hashlib.sha256(
            str(confirmation_token).encode("utf-8")
        ).hexdigest()
        with session_factory() as session:
            logical_leg = session.get(
                PositionProtectionLeg, int(action.logical_leg_id)
            )
            numeric_siblings = (
                session.query(PositionProtectionLeg)
                .filter(
                    PositionProtectionLeg.execution_order_leg_id
                    == int(action.entry_leg_id)
                )
                .filter(PositionProtectionLeg.role == "take_profit")
                .all()
            )
            if (
                logical_leg is None
                or str(logical_leg.venue or "").lower() != action.venue
                or str(logical_leg.role or "") != action.role
                or int(logical_leg.leg_index) != action.leg_index
                or str(logical_leg.status) != "protection_recovery_pending"
                or logical_leg.exchange_order_id is not None
                or int(logical_leg.execution_binding_id) != action.binding_id
                or int(logical_leg.execution_order_leg_id) != action.entry_leg_id
                or str(logical_leg.pos_id or "") != action.pos_id
                or _positive_decimal(logical_leg.planned_trigger_price)
                != _positive_decimal(action.planned_trigger_price)
                or sum(
                    _positive_decimal(row.planned_trigger_price)
                    == _positive_decimal(action.planned_trigger_price)
                    for row in numeric_siblings
                )
                != 1
            ):
                raise ValueError("logical protection leg changed")
            if not _durable_action_still_exact(session, action=action):
                raise ValueError("durable repair evidence changed")
            session.add(
                RepairConfirmationToken(
                    token_hash=token_hash,
                    action_kind="take_profit_protection_leg_repair",
                    action_id=action.action_id,
                    pos_id=action.pos_id,
                    consumed_at=consumed_at,
                )
            )
            bind_verified_exchange_order(
                session,
                logical_leg,
                exchange_order_id=action.order_id,
                readback_evidence={
                    "source": "supervised_take_profit_protection_leg_repair",
                    "evidence_fingerprint": action.evidence_fingerprint,
                    "order_id": action.order_id,
                    "pos_id": action.pos_id,
                },
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("confirmation_token already consumed") from exc
    return TakeProfitProtectionLegRepairResult(applied=1)


def _durable_action_still_exact(
    session,
    *,
    action: TakeProfitProtectionLegRepairAction,
) -> bool:
    entry_leg = session.get(ExecutionOrderLeg, int(action.entry_leg_id))
    binding = session.get(ExecutionBinding, int(action.binding_id))
    if (
        entry_leg is None
        or binding is None
        or str(entry_leg.venue or "").lower() != "deepcoin"
        or str(entry_leg.purpose or "") != "entry"
        or str(entry_leg.status or "").lower() != "active"
        or str(entry_leg.attribution_status or "").lower() != "verified"
        or int(entry_leg.execution_binding_id) != action.binding_id
        or str(entry_leg.pos_id or "") != action.pos_id
        or str(binding.venue or "").lower() != "deepcoin"
        or str(binding.status or "").lower() != "active"
        or str(binding.side or "").lower() != action.side
        or action.pos_id not in _split_ids(binding.pos_id)
    ):
        return False
    durable_orders = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.venue == "deepcoin")
        .filter(PositionTakeProfitOrder.order_id == action.order_id)
        .all()
    )
    if len(durable_orders) != 1:
        return False
    durable_order = durable_orders[0]
    if (
        int(durable_order.execution_binding_id) != action.binding_id
        or int(durable_order.execution_order_leg_id) != action.entry_leg_id
        or str(durable_order.pos_id or "") != action.pos_id
        or str(durable_order.status or "").lower() != "active"
        or _positive_decimal(durable_order.trigger_price)
        != _positive_decimal(action.submitted_trigger_price)
        or _nonnegative_decimal(durable_order.size_text)
        != _nonnegative_decimal(action.submitted_size)
    ):
        return False
    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.order_id == action.order_id)
        .all()
    )
    if len(ledger_rows) != 1:
        return False
    ledger = ledger_rows[0]
    if (
        int(ledger.execution_binding_id) != action.binding_id
        or int(ledger.execution_order_leg_id) != action.entry_leg_id
        or str(ledger.pos_id or "") != action.pos_id
        or str(ledger.instrument_id or "").upper() != action.instrument_id
        or str(ledger.side or "").lower() != action.side
        or str(ledger.purpose or "").lower() != "take_profit"
        or str(ledger.status or "").lower() != "verified"
        or _positive_decimal(ledger.trigger_price)
        != _positive_decimal(action.submitted_trigger_price)
        or _nonnegative_decimal(ledger.size_text)
        != _nonnegative_decimal(action.submitted_size)
    ):
        return False
    return (
        session.query(PositionProtectionLeg.id)
        .filter(PositionProtectionLeg.venue == "deepcoin")
        .filter(PositionProtectionLeg.exchange_order_id == action.order_id)
        .filter(PositionProtectionLeg.id != int(action.logical_leg_id))
        .first()
        is None
    )


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _split_ids(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
