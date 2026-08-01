"""Pure contracts and replay helpers for read-only KOL strategy audits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from telegram_kol_research.kol_audit_market_data import AuditCandle
from telegram_kol_research.take_profit_plan import (
    TakeProfitPlanError,
    build_take_profit_plan,
)


class AuditValidationError(ValueError):
    """Raised when audit evidence cannot form a safe normalized strategy."""


@dataclass(frozen=True, slots=True)
class AuditSourceMessage:
    chat_id: int
    message_id: int
    posted_at: datetime
    text: str
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class AuditDisposition:
    message_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class AuditDecisionSet:
    strategies: tuple[Mapping[str, Any], ...]
    excluded_messages: tuple[Mapping[str, Any], ...]
    duplicate_messages: tuple[Mapping[str, Any], ...]
    event_links: tuple[Mapping[str, Any], ...]
    unresolved_events: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class AuditReconstruction:
    strategies: tuple["NormalizedAuditStrategy", ...]
    excluded: tuple[AuditDisposition, ...]
    unresolved: tuple[AuditDisposition, ...]


@dataclass(frozen=True, slots=True)
class AuditEntryFill:
    price: Decimal
    allocation_pct: Decimal
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AuditExitFill:
    price: Decimal
    allocation_pct: Decimal
    occurred_at: datetime
    reason: str
    realized_r: Decimal
    return_pct: Decimal


@dataclass(frozen=True, slots=True)
class AuditStrategyResult:
    audit_id: str
    symbol: str
    side: str
    status: str
    entry_price: Decimal | None
    entered_at: datetime | None
    filled_entry_allocation_pct: Decimal
    initial_risk: Decimal | None
    exits: tuple[AuditExitFill, ...]
    targets_reached: int
    realized_r: Decimal
    realized_return_pct: Decimal
    open_allocation_pct: Decimal
    exit_reason: str | None
    confidence: str
    reason_codes: tuple[str, ...]


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
    trigger: str | None = None
    interval: str | None = None


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
                    **({"trigger": item.trigger} if item.trigger else {}),
                    **({"interval": item.interval} if item.interval else {}),
                }
                for item in self.management_events
            ],
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
        }


def load_audit_messages(payload: Any) -> tuple[AuditSourceMessage, ...]:
    """Validate and chronologically order a raw-message JSON snapshot."""

    if not isinstance(payload, list):
        raise AuditValidationError("audit messages must be a JSON array")
    result: list[AuditSourceMessage] = []
    seen: set[tuple[int, int]] = set()
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise AuditValidationError("each audit message must be an object")
        row = AuditSourceMessage(
            chat_id=int(raw["chat_id"]),
            message_id=int(raw["message_id"]),
            posted_at=_timestamp(raw.get("posted_at")),
            text=str(raw.get("text") or ""),
            reply_to_message_id=(
                int(raw["reply_to_message_id"])
                if raw.get("reply_to_message_id") not in (None, "")
                else None
            ),
        )
        identity = (row.chat_id, row.message_id)
        if identity in seen:
            raise AuditValidationError("audit message identities must be unique")
        seen.add(identity)
        result.append(row)
    return tuple(sorted(result, key=lambda row: (row.posted_at, row.message_id)))


def load_reviewed_decisions(payload: Any) -> AuditDecisionSet:
    """Load explicit human-reviewed reconstruction decisions."""

    if not isinstance(payload, Mapping):
        raise AuditValidationError("reviewed decisions must be a JSON object")

    def rows(name: str) -> tuple[Mapping[str, Any], ...]:
        value = payload.get(name) or ()
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in value
        ):
            raise AuditValidationError(f"{name} must be an array of objects")
        return tuple(dict(item) for item in value)

    return AuditDecisionSet(
        strategies=rows("strategies"),
        excluded_messages=rows("excluded_messages"),
        duplicate_messages=rows("duplicate_messages"),
        event_links=rows("event_links"),
        unresolved_events=rows("unresolved_events"),
    )


def reconstruct_audit_strategies(
    messages: Iterable[AuditSourceMessage],
    decisions: AuditDecisionSet,
) -> AuditReconstruction:
    """Apply reviewed decisions and fail closed on unreviewed candidates."""

    ordered_messages = tuple(sorted(messages, key=lambda row: (row.posted_at, row.message_id)))
    by_id = {row.message_id: row for row in ordered_messages}
    if len(by_id) != len(ordered_messages):
        raise AuditValidationError("message IDs must be unique within an audit snapshot")

    strategies: dict[str, NormalizedAuditStrategy] = {}
    reviewed_message_ids: set[int] = set()
    for decision in decisions.strategies:
        source_id = int(decision["source_message_id"])
        source = _decision_message(by_id, source_id)
        strategy_payload = {
            **dict(decision),
            "evidence": [
                {
                    "message_id": source.message_id,
                    "posted_at": _timestamp_text(source.posted_at),
                    "role": "strategy",
                }
            ],
            "management_events": [],
        }
        strategy_payload.pop("source_message_id", None)
        strategy = NormalizedAuditStrategy.from_dict(strategy_payload)
        if strategy.audit_id in strategies:
            raise AuditValidationError("audit strategy IDs must be unique")
        if strategy.chat_id != source.chat_id:
            raise AuditValidationError("strategy chat does not match source evidence")
        strategies[strategy.audit_id] = strategy
        reviewed_message_ids.add(source_id)

    excluded: list[AuditDisposition] = []
    for decision in decisions.excluded_messages:
        message_id = int(decision["message_id"])
        _decision_message(by_id, message_id)
        excluded.append(
            AuditDisposition(
                message_id=message_id,
                reason=_required_text(decision.get("reason"), "exclusion reason"),
            )
        )
        reviewed_message_ids.add(message_id)

    for decision in decisions.duplicate_messages:
        message_id = int(decision["message_id"])
        message = _decision_message(by_id, message_id)
        strategy = _decision_strategy(strategies, decision.get("target_audit_id"))
        reason = _required_text(decision.get("reason"), "duplicate reason")
        evidence = (*strategy.evidence, AuditMessageEvidence(
            message_id=message.message_id,
            posted_at=message.posted_at,
            role=reason,
        ))
        strategies[strategy.audit_id] = replace(strategy, evidence=_ordered_evidence(evidence))
        reviewed_message_ids.add(message_id)

    for decision in decisions.event_links:
        message_id = int(decision["message_id"])
        message = _decision_message(by_id, message_id)
        strategy = _decision_strategy(strategies, decision.get("target_audit_id"))
        event = AuditManagementEvent(
            event_type=_required_text(decision.get("event_type"), "event type"),
            message_id=message.message_id,
            occurred_at=message.posted_at,
            allocation_pct=(
                _positive_decimal(decision.get("allocation_pct"), "event allocation")
                if decision.get("allocation_pct") not in (None, "")
                else None
            ),
            price=(
                _positive_decimal(decision.get("price"), "event price")
                if decision.get("price") not in (None, "")
                else None
            ),
            trigger=(
                _stop_trigger(decision.get("trigger"))
                if decision.get("trigger") not in (None, "")
                else None
            ),
            interval=(
                _stop_interval(decision.get("interval"))
                if decision.get("interval") not in (None, "")
                else None
            ),
        )
        _validate_management_event(event)
        strategies[strategy.audit_id] = replace(
            strategy,
            evidence=_ordered_evidence((*strategy.evidence, AuditMessageEvidence(
                message_id=message.message_id,
                posted_at=message.posted_at,
                role="management",
            ))),
            management_events=tuple(sorted(
                (*strategy.management_events, event),
                key=lambda item: (item.occurred_at, item.message_id),
            )),
        )
        reviewed_message_ids.add(message_id)

    unresolved: list[AuditDisposition] = []
    for decision in decisions.unresolved_events:
        message_id = int(decision["message_id"])
        _decision_message(by_id, message_id)
        unresolved.append(AuditDisposition(
            message_id=message_id,
            reason=_required_text(decision.get("reason"), "unresolved reason"),
        ))
        reviewed_message_ids.add(message_id)

    for message in ordered_messages:
        if message.message_id in reviewed_message_ids:
            continue
        if _looks_like_strategy_candidate(message.text):
            unresolved.append(AuditDisposition(
                message_id=message.message_id,
                reason="unreviewed_strategy_candidate",
            ))

    return AuditReconstruction(
        strategies=tuple(sorted(
            strategies.values(),
            key=lambda item: (
                item.published_at,
                item.evidence[0].message_id,
                item.ordinal,
                item.audit_id,
            ),
        )),
        excluded=tuple(sorted(excluded, key=lambda item: item.message_id)),
        unresolved=tuple(sorted(unresolved, key=lambda item: item.message_id)),
    )


def replay_audit_strategy(
    strategy: NormalizedAuditStrategy,
    candles: Iterable[AuditCandle],
    *,
    cutoff: datetime,
) -> AuditStrategyResult:
    """Replay one reviewed strategy without mutating operational state."""

    audit_cutoff = _timestamp(cutoff)
    ordered_candles = tuple(sorted(
        (
            candle
            for candle in candles
            if candle.opened_at >= strategy.published_at
            and candle.opened_at <= audit_cutoff
        ),
        key=lambda candle: candle.opened_at,
    ))
    events = tuple(sorted(
        (
            event
            for event in strategy.management_events
            if event.occurred_at <= audit_cutoff
        ),
        key=lambda event: (event.occurred_at, event.message_id),
    ))
    event_index = 0
    cancelled = False
    entries_frozen = False
    protected = False
    active_stop_price = strategy.stop.price
    active_stop_trigger = strategy.stop.trigger
    active_stop_interval = strategy.stop.interval
    entry_fills: dict[int, AuditEntryFill] = {}
    exits: list[AuditExitFill] = []
    next_target = 0
    remaining = Decimal("100")
    reasons = list(strategy.reason_codes)
    exit_reason: str | None = None
    entered_at: datetime | None = None

    for candle in ordered_candles:
        while event_index < len(events) and events[event_index].occurred_at <= candle.opened_at:
            event = events[event_index]
            event_index += 1
            if event.event_type in {"cancel_pending_entry", "replace_pending_entry"}:
                entries_frozen = True
                if not entry_fills:
                    cancelled = True
                    exit_reason = "cancelled"
                reasons.append(event.event_type)
                continue
            if event.event_type == "move_stop_to_break_even":
                if entry_fills and remaining > 0:
                    protected = True
                    reasons.append("explicit_break_even")
                continue
            if event.event_type == "stop_update":
                active_stop_price = event.price or active_stop_price
                active_stop_trigger = event.trigger or active_stop_trigger
                active_stop_interval = event.interval
                protected = False
                reasons.append("explicit_stop_update")
                continue
            if event.event_type in {"partial_exit", "full_exit"} and entry_fills and remaining > 0:
                allocation = (
                    remaining
                    if event.event_type == "full_exit"
                    else min(event.allocation_pct or Decimal("0"), remaining)
                )
                if allocation <= 0:
                    reasons.append("invalid_management_allocation")
                    continue
                entry_price = _weighted_entry(entry_fills.values())
                initial_risk = abs(entry_price - strategy.stop.price)
                exits.append(_exit_fill(
                    strategy=strategy,
                    entry_price=entry_price,
                    initial_risk=initial_risk,
                    price=candle.open,
                    allocation=allocation,
                    occurred_at=candle.opened_at,
                    reason=(
                        "kol_full_exit"
                        if event.event_type == "full_exit"
                        else "kol_partial_exit"
                    ),
                ))
                remaining -= allocation
                entries_frozen = True
                exit_reason = exits[-1].reason
                if remaining == 0:
                    break
        if remaining == 0:
            break
        if cancelled and not entry_fills:
            continue

        if not entries_frozen:
            for index, leg in enumerate(strategy.entry_legs):
                if index in entry_fills:
                    continue
                if candle.low <= leg.price <= candle.high:
                    entry_fills[index] = AuditEntryFill(
                        price=leg.price,
                        allocation_pct=leg.allocation_pct,
                        occurred_at=candle.opened_at,
                    )
                    entered_at = entered_at or candle.opened_at
        if not entry_fills:
            continue

        entry_price = _weighted_entry(entry_fills.values())
        initial_risk = abs(entry_price - strategy.stop.price)
        if initial_risk <= 0:
            reasons.append("zero_initial_risk")
            return _result(
                strategy=strategy,
                status="unresolved",
                entry_fills=entry_fills,
                entered_at=entered_at,
                exits=exits,
                targets_reached=next_target,
                remaining=remaining,
                exit_reason="invalid_initial_risk",
                reasons=reasons,
            )

        stop_price = entry_price if protected else active_stop_price
        stop_hit = _stop_hit(
            strategy=strategy,
            candle=candle,
            price=stop_price,
            protected=protected,
            trigger=active_stop_trigger,
            interval=active_stop_interval,
        )
        target_hit = (
            next_target < len(strategy.take_profits)
            and _target_hit(strategy, candle, strategy.take_profits[next_target].price)
        )
        if stop_hit:
            if target_hit:
                reasons.append("intrabar_order_uncertain")
            reason = "break_even" if protected else "stop_loss"
            exits.append(_exit_fill(
                strategy=strategy,
                entry_price=entry_price,
                initial_risk=initial_risk,
                price=stop_price,
                allocation=remaining,
                occurred_at=candle.closed_at,
                reason=reason,
            ))
            remaining = Decimal("0")
            entries_frozen = True
            exit_reason = reason
            break

        while next_target < len(strategy.take_profits):
            target = strategy.take_profits[next_target]
            if not _target_hit(strategy, candle, target.price):
                break
            allocation = min(target.allocation_pct, remaining)
            if allocation <= 0:
                break
            exits.append(_exit_fill(
                strategy=strategy,
                entry_price=entry_price,
                initial_risk=initial_risk,
                price=target.price,
                allocation=allocation,
                occurred_at=candle.opened_at,
                reason=f"target_{next_target + 1}",
            ))
            remaining -= allocation
            next_target += 1
            entries_frozen = True
            reasons.append(exits[-1].reason)
            if remaining == 0:
                exit_reason = "take_profit"
                break
        if remaining == 0:
            break

    while event_index < len(events):
        event = events[event_index]
        event_index += 1
        if event.event_type in {"cancel_pending_entry", "replace_pending_entry"}:
            entries_frozen = True
            if not entry_fills:
                cancelled = True
                exit_reason = "cancelled"
            reasons.append(event.event_type)

    status = (
        "closed"
        if entry_fills and remaining == 0
        else "open"
        if entry_fills
        else "cancelled"
        if cancelled
        else "unfilled"
    )
    if entry_fills and len(entry_fills) < len(strategy.entry_legs) and not entries_frozen:
        reasons.append("pending_entry_leg")
    return _result(
        strategy=strategy,
        status=status,
        entry_fills=entry_fills,
        entered_at=entered_at,
        exits=exits,
        targets_reached=next_target,
        remaining=remaining,
        exit_reason=exit_reason,
        reasons=reasons,
    )


def _result(
    *,
    strategy: NormalizedAuditStrategy,
    status: str,
    entry_fills: Mapping[int, AuditEntryFill],
    entered_at: datetime | None,
    exits: Iterable[AuditExitFill],
    targets_reached: int,
    remaining: Decimal,
    exit_reason: str | None,
    reasons: Iterable[str],
) -> AuditStrategyResult:
    ordered_exits = tuple(exits)
    entry_price = _weighted_entry(entry_fills.values()) if entry_fills else None
    initial_risk = (
        abs(entry_price - strategy.stop.price) if entry_price is not None else None
    )
    return AuditStrategyResult(
        audit_id=strategy.audit_id,
        symbol=strategy.symbol,
        side=strategy.side,
        status=status,
        entry_price=entry_price,
        entered_at=entered_at,
        filled_entry_allocation_pct=sum(
            (item.allocation_pct for item in entry_fills.values()), Decimal("0")
        ),
        initial_risk=initial_risk,
        exits=ordered_exits,
        targets_reached=targets_reached,
        realized_r=sum((item.realized_r for item in ordered_exits), Decimal("0")),
        realized_return_pct=sum(
            (item.return_pct for item in ordered_exits), Decimal("0")
        ),
        open_allocation_pct=remaining if entry_fills else Decimal("0"),
        exit_reason=exit_reason,
        confidence=strategy.confidence,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _weighted_entry(fills: Iterable[AuditEntryFill]) -> Decimal:
    rows = tuple(fills)
    total = sum((item.allocation_pct for item in rows), Decimal("0"))
    return sum(
        (item.price * item.allocation_pct for item in rows), Decimal("0")
    ) / total


def _exit_fill(
    *,
    strategy: NormalizedAuditStrategy,
    entry_price: Decimal,
    initial_risk: Decimal,
    price: Decimal,
    allocation: Decimal,
    occurred_at: datetime,
    reason: str,
) -> AuditExitFill:
    signed_move = (
        price - entry_price if strategy.side == "long" else entry_price - price
    )
    weight = allocation / Decimal("100")
    return AuditExitFill(
        price=price,
        allocation_pct=allocation,
        occurred_at=occurred_at,
        reason=reason,
        realized_r=signed_move / initial_risk * weight,
        return_pct=signed_move / entry_price * Decimal("100") * weight,
    )


def _stop_hit(
    *,
    strategy: NormalizedAuditStrategy,
    candle: AuditCandle,
    price: Decimal,
    protected: bool,
    trigger: str,
    interval: str | None,
) -> bool:
    if protected or trigger == "touch":
        return candle.low <= price if strategy.side == "long" else candle.high >= price
    if not _is_interval_close(candle, interval):
        return False
    return candle.close < price if strategy.side == "long" else candle.close > price


def _target_hit(
    strategy: NormalizedAuditStrategy,
    candle: AuditCandle,
    price: Decimal,
) -> bool:
    return candle.high >= price if strategy.side == "long" else candle.low <= price


def _is_interval_close(candle: AuditCandle, interval: str | None) -> bool:
    seconds = {
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }.get(str(interval or ""))
    if seconds is None:
        return False
    closed_boundary = int(candle.closed_at.timestamp() * 1000) + 1
    return closed_boundary % (seconds * 1000) == 0


def _decision_message(
    by_id: Mapping[int, AuditSourceMessage], message_id: int
) -> AuditSourceMessage:
    message = by_id.get(message_id)
    if message is None:
        raise AuditValidationError(f"reviewed message {message_id} is missing")
    return message


def _decision_strategy(
    strategies: Mapping[str, NormalizedAuditStrategy], audit_id: Any
) -> NormalizedAuditStrategy:
    identity = _required_text(audit_id, "target audit ID")
    strategy = strategies.get(identity)
    if strategy is None:
        raise AuditValidationError(f"reviewed strategy {identity} is missing")
    return strategy


def _ordered_evidence(
    evidence: Iterable[AuditMessageEvidence],
) -> tuple[AuditMessageEvidence, ...]:
    return tuple(sorted(evidence, key=lambda item: (item.posted_at, item.message_id)))


def _looks_like_strategy_candidate(text: str) -> bool:
    normalized = str(text or "").lower()
    has_symbol = any(token in normalized for token in ("比特币", "btc", "以太币", "eth"))
    has_risk = "止损" in normalized
    has_entry = any(
        token in normalized
        for token in ("做多", "开多", "反弹多", "做空", "开空", "附近空", "附近反弹")
    )
    return has_symbol and has_risk and has_entry


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
    event = AuditManagementEvent(
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
        trigger=(
            _stop_trigger(payload.get("trigger"))
            if payload.get("trigger") not in (None, "")
            else None
        ),
        interval=(
            _stop_interval(payload.get("interval"))
            if payload.get("interval") not in (None, "")
            else None
        ),
    )
    _validate_management_event(event)
    return event


def _validate_management_event(event: AuditManagementEvent) -> None:
    if event.event_type != "stop_update":
        return
    if event.price is None or event.trigger is None:
        raise AuditValidationError("stop update requires price and trigger")
    if event.trigger == "close" and event.interval is None:
        raise AuditValidationError("close stop update requires a supported interval")


def _stop_trigger(value: Any) -> str:
    trigger = str(value or "").strip().lower()
    if trigger not in {"touch", "close"}:
        raise AuditValidationError("stop trigger must be touch or close")
    return trigger


def _stop_interval(value: Any) -> str:
    interval = str(value or "").strip().lower()
    if interval not in {"5m", "15m", "1h", "4h", "1d"}:
        raise AuditValidationError("stop interval must be supported")
    return interval


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
