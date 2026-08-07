"""Pure source-order selection for adjacent entry instructions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping


SourceOrderKey = tuple[datetime, int, int]
HARD_BOUNDARY_KINDS = frozenset(
    {
        "complete_entry",
        "cancel_entry",
        "opposite_entry",
        "replacement",
        "expired_adjacency",
    }
)
MAX_SUPPLEMENTAL_ENTRY_PRICES = 3


def source_order_key(
    posted_at: datetime | None,
    message_id: int,
    raw_message_id: int,
) -> SourceOrderKey:
    value = posted_at or datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value, int(message_id), int(raw_message_id)


@dataclass(frozen=True, slots=True)
class EntryStrategyFact:
    raw_message_id: int
    message_id: int
    posted_at: datetime | None
    symbol: str
    side: str

    @property
    def source_key(self) -> SourceOrderKey:
        return source_order_key(self.posted_at, self.message_id, self.raw_message_id)


@dataclass(frozen=True, slots=True)
class AdjacentEntryFact:
    raw_message_id: int
    message_id: int
    posted_at: datetime | None
    kind: str
    symbol: str | None = None
    side: str | None = None
    fragment_id: int | None = None
    fragment_kind: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence_version_id: int | None = None

    @property
    def source_key(self) -> SourceOrderKey:
        return source_order_key(self.posted_at, self.message_id, self.raw_message_id)


@dataclass(frozen=True, slots=True)
class AdjacentEntryDecision:
    status: str
    reason_code: str | None
    fragment_ids: tuple[int, ...]
    risk_multiplier: Decimal
    allocations: tuple[Decimal, ...]
    supplemental_prices: tuple[Decimal, ...]
    boundary_evidence: tuple[int, ...]
    pending_raw_message_ids: tuple[int, ...] = ()
    legacy_preamble_ids: tuple[int, ...] = ()


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _matching_fragment(
    fact: AdjacentEntryFact,
    *,
    symbol: str,
    side: str,
) -> bool:
    return (
        fact.kind == "fragment"
        and fact.fragment_id is not None
        and str(fact.symbol or "").strip().upper() == symbol
        and str(fact.side or "").strip().lower() == side
    )


def _bounded_segment(
    *,
    strategy: EntryStrategyFact,
    facts: list[AdjacentEntryFact],
    cutoff: SourceOrderKey,
) -> tuple[list[AdjacentEntryFact], list[int]]:
    eligible = sorted(
        (fact for fact in facts if fact.source_key <= cutoff),
        key=lambda fact: fact.source_key,
    )
    before = [fact for fact in eligible if fact.source_key < strategy.source_key]
    after = [fact for fact in eligible if fact.source_key > strategy.source_key]
    boundary_ids: list[int] = []

    before_boundaries = [fact for fact in before if fact.kind in HARD_BOUNDARY_KINDS]
    if before_boundaries:
        nearest_before_key = max(fact.source_key for fact in before_boundaries)
        bounded_before = [
            fact for fact in before if fact.source_key > nearest_before_key
        ]
        boundary_ids.extend(int(fact.raw_message_id) for fact in before_boundaries)
    else:
        bounded_before = before

    after_boundaries = [fact for fact in after if fact.kind in HARD_BOUNDARY_KINDS]
    if after_boundaries:
        nearest_after_key = min(fact.source_key for fact in after_boundaries)
        bounded_after = [fact for fact in after if fact.source_key < nearest_after_key]
        nearest_boundaries = [
            fact for fact in after_boundaries if fact.source_key == nearest_after_key
        ]
        boundary_ids.extend(int(fact.raw_message_id) for fact in nearest_boundaries)
    else:
        bounded_after = after
    return [*bounded_before, *bounded_after], boundary_ids


def select_adjacent_entry_fragments(
    *,
    strategy: EntryStrategyFact,
    facts: list[AdjacentEntryFact],
    cutoff: SourceOrderKey,
) -> AdjacentEntryDecision:
    """Select explicit adjacent facts without consulting completion timestamps."""

    normalized_cutoff = source_order_key(*cutoff)
    segment, boundary_ids = _bounded_segment(
        strategy=strategy,
        facts=facts,
        cutoff=normalized_cutoff,
    )
    pending_raw_message_ids = tuple(
        int(fact.raw_message_id) for fact in segment if fact.kind == "unresolved"
    )
    if pending_raw_message_ids:
        return AdjacentEntryDecision(
            status="pending",
            reason_code="adjacent_entry_context_pending",
            fragment_ids=(),
            risk_multiplier=Decimal("1"),
            allocations=(),
            supplemental_prices=(),
            boundary_evidence=tuple(boundary_ids),
            pending_raw_message_ids=pending_raw_message_ids,
        )

    symbol = str(strategy.symbol).strip().upper()
    side = str(strategy.side).strip().lower()
    selected = [
        fact
        for fact in segment
        if _matching_fragment(fact, symbol=symbol, side=side)
    ]
    multipliers: list[Decimal] = []
    allocations: tuple[Decimal, ...] = ()
    supplemental_prices: list[Decimal] = []
    for fact in selected:
        if fact.fragment_kind == "risk_multiplier":
            value = _decimal(fact.payload.get("risk_multiplier"))
            if value is None or value <= 0 or value > 1:
                return AdjacentEntryDecision(
                    "blocked",
                    "entry_risk_multiplier_invalid",
                    (),
                    Decimal("1"),
                    (),
                    (),
                    tuple(boundary_ids),
                )
            multipliers.append(value)
        elif fact.fragment_kind == "leg_allocation":
            raw_allocations = fact.payload.get("allocations")
            if not isinstance(raw_allocations, (list, tuple)):
                continue
            parsed = tuple(_decimal(value) for value in raw_allocations)
            if (
                any(value is None or value <= 0 for value in parsed)
                or sum(value for value in parsed if value is not None) != Decimal("1")
            ):
                return AdjacentEntryDecision(
                    "blocked",
                    "entry_leg_allocation_invalid",
                    (),
                    Decimal("1"),
                    (),
                    (),
                    tuple(boundary_ids),
                )
            normalized = tuple(value for value in parsed if value is not None)
            if allocations and allocations != normalized:
                return AdjacentEntryDecision(
                    "blocked",
                    "entry_leg_allocation_conflict",
                    (),
                    Decimal("1"),
                    (),
                    (),
                    tuple(boundary_ids),
                )
            allocations = normalized
        elif fact.fragment_kind == "supplemental_entry":
            entry_price = fact.payload.get("entry_price")
            prices = (
                [entry_price]
                if entry_price not in (None, "")
                else fact.payload.get("prices")
            )
            if not isinstance(prices, (list, tuple)):
                continue
            for value in prices:
                parsed = _decimal(value)
                if parsed is not None and parsed > 0 and parsed not in supplemental_prices:
                    supplemental_prices.append(parsed)

    fragment_ids = tuple(
        int(fact.fragment_id)
        for fact in selected
        if int(fact.fragment_id) > 0
    )
    legacy_preamble_ids = tuple(
        abs(int(fact.fragment_id))
        for fact in selected
        if int(fact.fragment_id) < 0
    )
    if len(supplemental_prices) > MAX_SUPPLEMENTAL_ENTRY_PRICES:
        return AdjacentEntryDecision(
            "blocked",
            "entry_leg_count_exceeded",
            fragment_ids,
            Decimal("1"),
            allocations,
            (),
            tuple(boundary_ids),
            legacy_preamble_ids=legacy_preamble_ids,
        )
    distinct_multipliers = tuple(dict.fromkeys(multipliers))
    if len(distinct_multipliers) > 1:
        return AdjacentEntryDecision(
            "blocked",
            "entry_risk_multiplier_conflict",
            fragment_ids,
            Decimal("1"),
            allocations,
            tuple(supplemental_prices),
            tuple(boundary_ids),
            legacy_preamble_ids=legacy_preamble_ids,
        )
    return AdjacentEntryDecision(
        status="ready",
        reason_code=None,
        fragment_ids=fragment_ids,
        risk_multiplier=(distinct_multipliers[0] if distinct_multipliers else Decimal("1")),
        allocations=allocations,
        supplemental_prices=tuple(supplemental_prices),
        boundary_evidence=tuple(boundary_ids),
        legacy_preamble_ids=legacy_preamble_ids,
    )
