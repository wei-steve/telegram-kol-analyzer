"""Deterministic, evidence-led attribution of exchange positions to entry legs."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from math import inf, isclose
from typing import Iterable


DIRECT_POS_ID = 0
EXACT_REGULAR_ORDER_ID = 1
EXACT_CLIENT_ORDER_ID = 2
UNIQUE_TRIGGER_FILL = 3

TERMINAL_ENTRY_LEG_STATES = frozenset(
    {
        "cancelled",
        "manually_cancelled",
        "exchange_cancelled",
        "manually_closed",
        "closed",
        "expired",
        "invalidated",
    }
)

_FILLED_STATES = frozenset({"filled", "done", "completed"})
_PARTIAL_FILL_STATES = frozenset({"partially_filled", "partial_filled", "partial"})
_CANCELLED_STATES = frozenset(
    {"cancelled", "canceled", "cancel", "revoked", "rejected", "expired"}
)
_OPEN_STATES = frozenset({"live", "open", "pending", "submitted"})
_CANCEL_EVENT_ACTIONS = frozenset({"cancel_trigger_entry", "cancel_regular_entry"})


@dataclass(frozen=True, slots=True)
class LegEvidence:
    leg_id: int
    binding_id: int
    venue: str
    symbol: str
    side: str
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    requested_size: float | None
    terminal: bool


@dataclass(frozen=True, slots=True)
class PositionEvidence:
    pos_id: str
    symbol: str
    side: str
    size: float | None
    average_price: float | None
    created_at_ms: int | None


@dataclass(frozen=True, slots=True)
class FillEvidence:
    source: str
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    symbol: str
    side: str
    size: float | None
    price: float | None
    created_at_ms: int | None


@dataclass(frozen=True, order=True, slots=True)
class MatchRank:
    evidence_tier: int
    time_distance_ms: float
    size_distance: float
    price_distance: float


@dataclass(slots=True)
class AttributionResult:
    assignments: dict[int, str] = field(default_factory=dict)
    evidence_by_leg: dict[int, dict[str, object]] = field(default_factory=dict)
    conflicts: list[dict[str, object]] = field(default_factory=list)
    unassigned_position_ids: set[str] = field(default_factory=set)


class PositionAttributionError(RuntimeError):
    """Raised when a live mutation lacks unique persisted position ownership."""


def require_verified_position_ownership(session, *, venue: str, pos_id: str):
    """Return the sole nonterminal verified leg owning an exact exchange position."""

    from telegram_kol_research.models import ExecutionOrderLeg

    rows = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == str(venue or "deepcoin").lower())
        .filter(ExecutionOrderLeg.pos_id == str(pos_id))
        .all()
    )
    if len(rows) != 1:
        raise PositionAttributionError("position_ownership_not_unique")
    leg = rows[0]
    state = str(leg.attribution_status or "unassigned")
    if state != "verified":
        raise PositionAttributionError(f"position_ownership_not_verified:{state}")
    if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
        raise PositionAttributionError("position_ownership_terminal")
    return leg


def require_manual_position_attribution_allowed(
    session, *, venue: str, pos_id: str
) -> None:
    """Allow operator binding only when no unresolved ownership evidence exists."""

    from telegram_kol_research.models import ExecutionOrderLeg

    rows = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == str(venue or "deepcoin").lower())
        .filter(ExecutionOrderLeg.pos_id == str(pos_id))
        .all()
    )
    states = {str(row.attribution_status or "unassigned") for row in rows}
    for blocked_state in ("attribution_conflict", "evidence_unavailable"):
        if blocked_state in states:
            raise PositionAttributionError(
                f"position attribution cannot be manually overridden:{blocked_state}"
            )
    if rows:
        raise PositionAttributionError("position attribution is already recorded")


@dataclass(frozen=True, slots=True)
class _Edge:
    leg_id: int
    pos_id: str
    rank: MatchRank
    evidence_type: str
    evidence_source: str


def classify_leg_exchange_state(
    row: dict[str, object],
    *,
    cancel_event_action: str | None = None,
) -> str:
    """Classify an entry order without inferring fills from numeric fields."""

    if str(cancel_event_action or "").lower() in _CANCEL_EVENT_ACTIONS:
        return "exchange_cancelled"
    state = str(row.get("state") or row.get("status") or "").strip().lower()
    if state in _CANCELLED_STATES:
        return "manually_cancelled"
    if state in _FILLED_STATES:
        return "filled"
    if state in _PARTIAL_FILL_STATES:
        return "partially_filled"
    if state in _OPEN_STATES:
        return "pending"
    return "unknown"


def is_fill_evidence(row: dict[str, object]) -> bool:
    """Return true only for explicit fill state or a row known to come from fills."""

    state = classify_leg_exchange_state(row)
    if state in {"filled", "partially_filled"}:
        return True
    if str(row.get("_evidence_source") or "").lower() != "trade_fill":
        return False
    return any(
        row.get(key) not in (None, "")
        for key in ("tradeId", "fillId", "execId", "ordId", "orderId", "fillSz")
    )


def match_entry_legs_to_positions(
    legs: Iterable[LegEvidence],
    positions: Iterable[PositionEvidence],
    evidence: Iterable[FillEvidence],
) -> AttributionResult:
    """Return only mutual-unique assignments backed by direct entry evidence."""

    eligible_legs = sorted((leg for leg in legs if not leg.terminal), key=lambda item: item.leg_id)
    position_rows = sorted(positions, key=lambda item: item.pos_id)
    fill_rows = tuple(evidence)
    edges: list[_Edge] = []

    for leg in eligible_legs:
        for position in position_rows:
            edge = _build_best_edge(leg, position, fill_rows)
            if edge is not None:
                edges.append(edge)

    assignments: dict[int, str] = {}
    evidence_by_leg: dict[int, dict[str, object]] = {}
    remaining = edges
    while remaining:
        leg_best = _unique_best_edges(remaining, key=lambda edge: edge.leg_id)
        position_best = _unique_best_edges(remaining, key=lambda edge: edge.pos_id)
        accepted = sorted(
            (
                edge
                for edge in remaining
                if leg_best.get(edge.leg_id) == edge
                and position_best.get(edge.pos_id) == edge
            ),
            key=lambda edge: (edge.leg_id, edge.pos_id),
        )
        if not accepted:
            break
        accepted_leg_ids = {edge.leg_id for edge in accepted}
        accepted_position_ids = {edge.pos_id for edge in accepted}
        for edge in accepted:
            assignments[edge.leg_id] = edge.pos_id
            evidence_by_leg[edge.leg_id] = {
                "evidence_type": edge.evidence_type,
                "evidence_source": edge.evidence_source,
                "rank": {
                    "evidence_tier": edge.rank.evidence_tier,
                    "time_distance_ms": edge.rank.time_distance_ms,
                    "size_distance": edge.rank.size_distance,
                    "price_distance": edge.rank.price_distance,
                },
            }
        remaining = [
            edge
            for edge in remaining
            if edge.leg_id not in accepted_leg_ids and edge.pos_id not in accepted_position_ids
        ]

    conflicts = _connected_conflicts(remaining)
    assigned_position_ids = set(assignments.values())
    return AttributionResult(
        assignments=assignments,
        evidence_by_leg=evidence_by_leg,
        conflicts=conflicts,
        unassigned_position_ids={position.pos_id for position in position_rows}
        - assigned_position_ids,
    )


def _build_best_edge(
    leg: LegEvidence,
    position: PositionEvidence,
    evidence: tuple[FillEvidence, ...],
) -> _Edge | None:
    if leg.venue.lower() != "deepcoin":
        return None
    if _normalize_instrument(leg.symbol) != _normalize_instrument(position.symbol):
        return None
    if _normalize_side(leg.side) != _normalize_side(position.side):
        return None
    if not _compatible_size(leg.requested_size, position.size):
        return None

    candidates: list[_Edge] = []
    if leg.pos_id and leg.pos_id == position.pos_id:
        candidates.append(
            _Edge(
                leg_id=leg.leg_id,
                pos_id=position.pos_id,
                rank=MatchRank(DIRECT_POS_ID, 0, 0, 0),
                evidence_type="direct_pos_id",
                evidence_source="persisted_leg",
            )
        )

    for fill in evidence:
        match = _leg_fill_match(leg, fill)
        if match is None:
            continue
        tier, evidence_type = match
        if fill.pos_id and fill.pos_id != position.pos_id:
            continue
        if _normalize_instrument(fill.symbol) != _normalize_instrument(position.symbol):
            continue
        if _normalize_side(fill.side) != _normalize_side(position.side):
            continue
        if (
            fill.pos_id is None
            and fill.created_at_ms is None
            and fill.size is None
            and fill.price is None
        ):
            continue
        if not _compatible_size(fill.size, position.size):
            continue
        candidates.append(
            _Edge(
                leg_id=leg.leg_id,
                pos_id=position.pos_id,
                rank=MatchRank(
                    DIRECT_POS_ID if fill.pos_id else tier,
                    _distance(fill.created_at_ms, position.created_at_ms),
                    _distance(fill.size, position.size),
                    _distance(fill.price, position.average_price),
                ),
                evidence_type="direct_pos_id" if fill.pos_id else evidence_type,
                evidence_source=fill.source,
            )
        )

    return min(candidates, key=lambda edge: (edge.rank, edge.evidence_type, edge.evidence_source)) if candidates else None


def _leg_fill_match(leg: LegEvidence, fill: FillEvidence) -> tuple[int, str] | None:
    if leg.order_id and fill.order_id and leg.order_id == fill.order_id:
        if fill.source == "trigger_fill":
            return UNIQUE_TRIGGER_FILL, "unique_trigger_fill"
        return EXACT_REGULAR_ORDER_ID, "exact_regular_order_id"
    if (
        leg.client_order_id
        and fill.client_order_id
        and leg.client_order_id == fill.client_order_id
    ):
        return EXACT_CLIENT_ORDER_ID, "exact_client_order_id"
    return None


def _unique_best_edges(edges: Iterable[_Edge], *, key) -> dict[object, _Edge]:
    grouped: dict[object, list[_Edge]] = defaultdict(list)
    for edge in edges:
        grouped[key(edge)].append(edge)
    result: dict[object, _Edge] = {}
    for group_key, group in grouped.items():
        best_rank = min(edge.rank for edge in group)
        best = [edge for edge in group if edge.rank == best_rank]
        if len(best) == 1:
            result[group_key] = best[0]
    return result


def _connected_conflicts(edges: Iterable[_Edge]) -> list[dict[str, object]]:
    edge_rows = list(edges)
    adjacency: dict[tuple[str, object], set[tuple[str, object]]] = defaultdict(set)
    for edge in edge_rows:
        leg_node = ("leg", edge.leg_id)
        position_node = ("position", edge.pos_id)
        adjacency[leg_node].add(position_node)
        adjacency[position_node].add(leg_node)

    conflicts: list[dict[str, object]] = []
    seen: set[tuple[str, object]] = set()
    for start in sorted(adjacency, key=lambda item: (item[0], str(item[1]))):
        if start in seen:
            continue
        queue = deque([start])
        leg_ids: set[int] = set()
        position_ids: set[str] = set()
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if node[0] == "leg":
                leg_ids.add(int(node[1]))
            else:
                position_ids.add(str(node[1]))
            queue.extend(adjacency[node] - seen)
        conflicts.append(
            {"leg_ids": sorted(leg_ids), "position_ids": sorted(position_ids)}
        )
    return conflicts


def _normalize_instrument(value: str) -> str:
    text = str(value or "").upper().replace("_", "-")
    return text if "-" in text else f"{text}-USDT-SWAP"


def _normalize_side(value: str) -> str:
    return str(value or "").lower()


def _compatible_size(expected: float | None, actual: float | None) -> bool:
    if expected is None or actual is None:
        return True
    return isclose(abs(float(expected)), abs(float(actual)), rel_tol=1e-9, abs_tol=1e-9)


def _distance(left: float | int | None, right: float | int | None) -> float:
    if left is None or right is None:
        return inf
    return abs(float(left) - float(right))
