"""Durable per-position market decisions for break-even management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    StrategyManagementBatch,
    StrategyManagementLeg,
    StrategyManagementMarketDecision,
)
from telegram_kol_research.strategy_management_market_policy import (
    assess_break_even_market,
)


DECISION_VERSION = 1
_ACTIONS = frozenset({"set_break_even", "full_exit"})
_QUOTE_FIELDS = frozenset({"last", "lastPx"})


class BreakEvenMarketDecisionConflict(RuntimeError):
    """Raised when a batch cannot reserve one exact immutable market choice."""


@dataclass(frozen=True, slots=True)
class BreakEvenMarketDecisionRecord:
    id: int
    management_batch_id: int
    strategy_instance_id: str
    instrument_id: str
    quote_price: str
    quote_price_field: str
    observed_at: datetime
    decisions: tuple[dict[str, Any], ...]
    decision_fingerprint: str


def load_break_even_market_decision(
    session_factory: sessionmaker, *, batch_id: int
) -> BreakEvenMarketDecisionRecord | None:
    """Load a previously reserved decision without consulting market data."""

    with session_factory() as session:
        row = (
            session.query(StrategyManagementMarketDecision)
            .filter(
                StrategyManagementMarketDecision.management_batch_id
                == int(batch_id)
            )
            .one_or_none()
        )
        return None if row is None else _to_record(row)


def reserve_break_even_market_decision(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    instrument_id: str,
    quote_price: Any,
    quote_price_field: str,
    observed_at: datetime,
    decisions: Iterable[Mapping[str, Any]],
) -> BreakEvenMarketDecisionRecord:
    """Create or idempotently reload one exact batch market decision."""

    normalized_instrument = str(instrument_id or "").strip().upper()
    normalized_field = str(quote_price_field or "").strip()
    normalized_price = _positive_decimal(
        quote_price, reason="break_even_quote_price_invalid"
    )
    if (
        not normalized_instrument.endswith("-USDT-SWAP")
        or normalized_field not in _QUOTE_FIELDS
        or not isinstance(observed_at, datetime)
    ):
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_quote_invalid"
        )

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if (
            batch is None
            or batch.intent != "move_stop_to_break_even"
            or batch.effective_action != "break_even_by_market"
            or batch.status != "executing"
        ):
            raise BreakEvenMarketDecisionConflict(
                "break_even_market_decision_batch_invalid"
            )
        legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .all()
        )
        normalized_decisions = _normalize_decisions(
            decisions,
            legs=legs,
            quote_price=normalized_price,
        )
        payload = {
            "version": DECISION_VERSION,
            "management_batch_id": int(batch.id),
            "strategy_instance_id": str(batch.strategy_instance_id),
            "instrument_id": normalized_instrument,
            "quote_price": normalized_price,
            "quote_price_field": normalized_field,
            "positions": normalized_decisions,
        }
        fingerprint = _fingerprint(payload)
        existing = (
            session.query(StrategyManagementMarketDecision)
            .filter(
                StrategyManagementMarketDecision.management_batch_id == batch.id
            )
            .one_or_none()
        )
        if existing is not None:
            return _require_same_decision(existing, fingerprint=fingerprint)
        row = StrategyManagementMarketDecision(
            management_batch_id=batch.id,
            strategy_instance_id=str(batch.strategy_instance_id),
            instrument_id=normalized_instrument,
            quote_price=normalized_price,
            quote_price_field=normalized_field,
            observed_at=observed_at,
            decisions_json=json.dumps(
                normalized_decisions,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            decision_fingerprint=fingerprint,
            created_at=observed_at,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(StrategyManagementMarketDecision)
                .filter(
                    StrategyManagementMarketDecision.management_batch_id
                    == batch.id
                )
                .one_or_none()
            )
            if existing is None:
                raise BreakEvenMarketDecisionConflict(
                    "break_even_market_decision_reservation_conflict"
                ) from None
            return _require_same_decision(existing, fingerprint=fingerprint)
        session.refresh(row)
        return _to_record(row)


def _normalize_decisions(
    decisions: Iterable[Mapping[str, Any]],
    *,
    legs: list[StrategyManagementLeg],
    quote_price: str,
) -> list[dict[str, Any]]:
    rows = list(decisions)
    legs_by_identity = {
        (int(leg.id), int(leg.execution_order_leg_id), str(leg.pos_id)): leg
        for leg in legs
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for row in rows:
        try:
            identity = (
                int(row["management_leg_id"]),
                int(row["execution_order_leg_id"]),
                str(row["pos_id"]),
            )
        except (KeyError, TypeError, ValueError):
            raise BreakEvenMarketDecisionConflict(
                "break_even_market_decision_leg_invalid"
            ) from None
        leg = legs_by_identity.get(identity)
        side = str(row.get("side") or "").strip().lower()
        action = str(row.get("action") or "").strip()
        comparison = str(row.get("comparison") or "").strip()
        entry_price = _positive_decimal(
            row.get("entry_price"), reason="break_even_entry_price_invalid"
        )
        if side not in {"long", "short"}:
            raise BreakEvenMarketDecisionConflict(
                "break_even_market_decision_leg_invalid"
            )
        policy = assess_break_even_market(
            side=side,
            entry_price=entry_price,
            market_price=quote_price,
        )
        expected_action = "set_break_even" if policy.allowed else "full_exit"
        if (
            leg is None
            or identity in seen
            or action not in _ACTIONS
            or comparison
            not in {
                "entry_below_market",
                "entry_equal_market",
                "entry_above_market",
            }
            or comparison != policy.comparison
            or action != expected_action
            or entry_price
            != _positive_decimal(
                leg.avg_entry_price, reason="break_even_leg_entry_price_invalid"
            )
        ):
            raise BreakEvenMarketDecisionConflict(
                "break_even_market_decision_leg_invalid"
            )
        protection = row.get("protection")
        normalized_protection = None
        if action == "set_break_even":
            if not isinstance(protection, Mapping):
                raise BreakEvenMarketDecisionConflict(
                    "break_even_market_decision_protection_invalid"
                )
            order_ids = [
                str(order_id)
                for order_id in protection.get("order_ids") or []
                if str(order_id)
            ]
            row_snapshots = protection.get("row_snapshots")
            if (
                not order_ids
                or len(order_ids) != len(set(order_ids))
                or not isinstance(row_snapshots, list)
                or len(row_snapshots) != len(order_ids)
                or [
                    str(snapshot.get("order_id") or "")
                    for snapshot in row_snapshots
                    if isinstance(snapshot, Mapping)
                ]
                != order_ids
            ):
                raise BreakEvenMarketDecisionConflict(
                    "break_even_market_decision_protection_invalid"
                )
            normalized_protection = {
                "order_ids": order_ids,
                "row_snapshots": [
                    dict(snapshot) for snapshot in row_snapshots
                ],
            }
        elif protection not in (None, {}):
            raise BreakEvenMarketDecisionConflict(
                "break_even_market_decision_protection_invalid"
            )
        seen.add(identity)
        normalized_row = {
                "management_leg_id": identity[0],
                "execution_order_leg_id": identity[1],
                "pos_id": identity[2],
                "side": side,
                "entry_price": entry_price,
                "comparison": comparison,
                "action": action,
            }
        if normalized_protection is not None:
            normalized_row["protection"] = normalized_protection
        normalized.append(normalized_row)
    if seen != set(legs_by_identity):
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_leg_set_not_exact"
        )
    return sorted(
        normalized,
        key=lambda row: (
            row["pos_id"],
            row["execution_order_leg_id"],
            row["management_leg_id"],
        ),
    )


def _require_same_decision(
    row: StrategyManagementMarketDecision, *, fingerprint: str
) -> BreakEvenMarketDecisionRecord:
    if row.decision_fingerprint != fingerprint:
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_conflict"
        )
    return _to_record(row)


def _to_record(
    row: StrategyManagementMarketDecision,
) -> BreakEvenMarketDecisionRecord:
    decisions = json.loads(row.decisions_json)
    if not isinstance(decisions, list):
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_corrupt"
        )
    expected_fingerprint = _fingerprint(
        {
            "version": DECISION_VERSION,
            "management_batch_id": int(row.management_batch_id),
            "strategy_instance_id": str(row.strategy_instance_id),
            "instrument_id": str(row.instrument_id),
            "quote_price": str(row.quote_price),
            "quote_price_field": str(row.quote_price_field),
            "positions": decisions,
        }
    )
    if expected_fingerprint != str(row.decision_fingerprint):
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_corrupt"
        )
    return BreakEvenMarketDecisionRecord(
        id=int(row.id),
        management_batch_id=int(row.management_batch_id),
        strategy_instance_id=str(row.strategy_instance_id),
        instrument_id=str(row.instrument_id),
        quote_price=str(row.quote_price),
        quote_price_field=str(row.quote_price_field),
        observed_at=row.observed_at,
        decisions=tuple(dict(item) for item in decisions),
        decision_fingerprint=str(row.decision_fingerprint),
    )


def _positive_decimal(value: Any, *, reason: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BreakEvenMarketDecisionConflict(reason) from None
    if not number.is_finite() or number <= 0:
        raise BreakEvenMarketDecisionConflict(reason)
    normalized = format(number.normalize(), "f")
    return "0" if normalized == "-0" else normalized


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
