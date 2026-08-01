"""Pure contracts and replay helpers for read-only KOL strategy audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from telegram_kol_research.take_profit_plan import (
    TakeProfitPlanError,
    build_take_profit_plan,
)


class AuditValidationError(ValueError):
    """Raised when audit evidence cannot form a safe normalized strategy."""


@dataclass(frozen=True, slots=True)
class AuditMessageEvidence:
    message_id: int
    posted_at: datetime
    role: str


@dataclass(frozen=True, slots=True)
class AuditEntryLeg:
    price: Decimal
    allocation_pct: Decimal


@dataclass(frozen=True, slots=True)
class AuditStopRule:
    price: Decimal
    trigger: str
    interval: str | None = None


@dataclass(frozen=True, slots=True)
class AuditTakeProfit:
    price: Decimal
    allocation_pct: Decimal


@dataclass(frozen=True, slots=True)
class AuditManagementEvent:
    event_type: str
    message_id: int
    occurred_at: datetime
    allocation_pct: Decimal | None = None
    price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NormalizedAuditStrategy:
    audit_id: str
    chat_id: int
    symbol: str
    side: str
    ordinal: int
    published_at: datetime
    evidence: tuple[AuditMessageEvidence, ...]
    entry_legs: tuple[AuditEntryLeg, ...]
    stop: AuditStopRule
    take_profits: tuple[AuditTakeProfit, ...]
    management_events: tuple[AuditManagementEvent, ...]
    confidence: str
    reason_codes: tuple[str, ...]

    @property
    def take_profit_allocations(self) -> tuple[Decimal, ...]:
        return tuple(item.allocation_pct for item in self.take_profits)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizedAuditStrategy":
        side = str(payload.get("side") or "").strip().lower()
        if side not in {"long", "short"}:
            raise AuditValidationError("side must be long or short")

        evidence = tuple(_message_evidence(item) for item in payload.get("evidence") or ())
        if not evidence:
            raise AuditValidationError("at least one message evidence row is required")

        entry_legs = tuple(_entry_leg(item) for item in payload.get("entry_legs") or ())
        if not entry_legs:
            raise AuditValidationError("at least one entry leg is required")
        if sum((item.allocation_pct for item in entry_legs), Decimal("0")) != Decimal("100"):
            raise AuditValidationError("entry allocations must total 100")

        entry_reference = sum(
            (item.price * item.allocation_pct / Decimal("100") for item in entry_legs),
            Decimal("0"),
        )
        stop = _stop_rule(payload.get("stop"))
        if side == "long" and stop.price >= entry_reference:
            raise AuditValidationError("stop must be on the loss side of entry")
        if side == "short" and stop.price <= entry_reference:
            raise AuditValidationError("stop must be on the loss side of entry")

        take_profits = _take_profits(payload.get("take_profits") or (), side=side)
        for item in take_profits:
            if side == "long" and item.price <= entry_reference:
                raise AuditValidationError("take profits must be in profitable order")
            if side == "short" and item.price >= entry_reference:
                raise AuditValidationError("take profits must be in profitable order")
        prices = [item.price for item in take_profits]
        expected = sorted(prices, reverse=side == "short")
        if prices != expected:
            raise AuditValidationError("take profits must be in profitable order")

        management_events = tuple(
            _management_event(item) for item in payload.get("management_events") or ()
        )
        return cls(
            audit_id=_required_text(payload.get("audit_id"), "audit_id"),
            chat_id=int(payload["chat_id"]),
            symbol=_required_text(payload.get("symbol"), "symbol").upper(),
            side=side,
            ordinal=int(payload.get("ordinal") or 1),
            published_at=_timestamp(payload.get("published_at")),
            evidence=evidence,
            entry_legs=entry_legs,
            stop=stop,
            take_profits=take_profits,
            management_events=management_events,
            confidence=str(payload.get("confidence") or "unresolved").lower(),
            reason_codes=tuple(str(value) for value in payload.get("reason_codes") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "chat_id": self.chat_id,
            "symbol": self.symbol,
            "side": self.side,
            "ordinal": self.ordinal,
            "published_at": _timestamp_text(self.published_at),
            "evidence": [
                {
                    "message_id": item.message_id,
                    "posted_at": _timestamp_text(item.posted_at),
                    "role": item.role,
                }
                for item in self.evidence
            ],
            "entry_legs": [
                {
                    "price": _decimal_text(item.price),
                    "allocation_pct": _decimal_text(item.allocation_pct),
                }
                for item in self.entry_legs
            ],
            "stop": {
                "price": _decimal_text(self.stop.price),
                "trigger": self.stop.trigger,
                **({"interval": self.stop.interval} if self.stop.interval else {}),
            },
            "take_profits": [
                {
                    "price": _decimal_text(item.price),
                    "allocation_pct": _decimal_text(item.allocation_pct),
                }
                for item in self.take_profits
            ],
            "management_events": [
                {
                    "event_type": item.event_type,
                    "message_id": item.message_id,
                    "occurred_at": _timestamp_text(item.occurred_at),
                    **(
                        {"allocation_pct": _decimal_text(item.allocation_pct)}
                        if item.allocation_pct is not None
                        else {}
                    ),
                    **(
                        {"price": _decimal_text(item.price)}
                        if item.price is not None
                        else {}
                    ),
                }
                for item in self.management_events
            ],
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
        }


def _message_evidence(payload: Mapping[str, Any]) -> AuditMessageEvidence:
    return AuditMessageEvidence(
        message_id=int(payload["message_id"]),
        posted_at=_timestamp(payload.get("posted_at")),
        role=_required_text(payload.get("role"), "evidence role"),
    )


def _entry_leg(payload: Mapping[str, Any]) -> AuditEntryLeg:
    return AuditEntryLeg(
        price=_positive_decimal(payload.get("price"), "entry price"),
        allocation_pct=_positive_decimal(
            payload.get("allocation_pct"), "entry allocation"
        ),
    )


def _stop_rule(payload: Any) -> AuditStopRule:
    if not isinstance(payload, Mapping):
        raise AuditValidationError("stop rule is required")
    trigger = str(payload.get("trigger") or "").lower()
    if trigger not in {"touch", "close"}:
        raise AuditValidationError("stop trigger must be touch or close")
    interval = str(payload.get("interval") or "").lower() or None
    if trigger == "close" and interval not in {"5m", "15m", "1h", "4h", "1d"}:
        raise AuditValidationError("close stop requires a supported interval")
    return AuditStopRule(
        price=_positive_decimal(payload.get("price"), "stop price"),
        trigger=trigger,
        interval=interval,
    )


def _take_profits(values: Iterable[Any], *, side: str) -> tuple[AuditTakeProfit, ...]:
    raw = tuple(values)
    if not raw or len(raw) > 5:
        raise AuditValidationError("take-profit plan must contain one through five targets")
    explicit = all(isinstance(item, Mapping) for item in raw)
    prices = [item.get("price") if isinstance(item, Mapping) else item for item in raw]
    configured = (
        [item.get("allocation_pct") for item in raw]
        if explicit
        else None
    )
    if explicit:
        allocations = tuple(
            _positive_decimal(value, "take-profit allocation") for value in configured or ()
        )
        if sum(allocations, Decimal("0")) != Decimal("100"):
            raise AuditValidationError("take-profit allocations must total 100")
    try:
        plan = build_take_profit_plan(
            prices=prices,
            side=side,
            configured_allocations=configured,
        )
    except TakeProfitPlanError as exc:
        raise AuditValidationError(str(exc)) from exc
    return tuple(
        AuditTakeProfit(
            price=_positive_decimal(item.price, "take-profit price"),
            allocation_pct=_positive_decimal(
                item.allocation_pct, "take-profit allocation"
            ),
        )
        for item in plan.legs
    )


def _management_event(payload: Mapping[str, Any]) -> AuditManagementEvent:
    allocation = payload.get("allocation_pct")
    price = payload.get("price")
    return AuditManagementEvent(
        event_type=_required_text(payload.get("event_type"), "management event type"),
        message_id=int(payload["message_id"]),
        occurred_at=_timestamp(payload.get("occurred_at")),
        allocation_pct=(
            _positive_decimal(allocation, "management allocation")
            if allocation not in (None, "")
            else None
        ),
        price=(
            _positive_decimal(price, "management price")
            if price not in (None, "")
            else None
        ),
    )


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AuditValidationError(f"{label} must be positive") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AuditValidationError(f"{label} must be positive")
    return parsed


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _required_text(value, "timestamp")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AuditValidationError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AuditValidationError(f"{label} is required")
    return text
