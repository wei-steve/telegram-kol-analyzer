"""Deterministic, evidence-led attribution of exchange positions to entry legs."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
from math import inf, isclose
import re
from typing import Iterable, Mapping


DIRECT_POS_ID = 0
EXACT_REGULAR_ORDER_ID = 1
EXACT_CLIENT_ORDER_ID = 2
UNIQUE_TRIGGER_FILL = 3
MAX_TRIGGER_POSITION_LINK_TIME_DISTANCE_MS = 5_000
ATTRIBUTION_POLICY_VERSION = 2
ECONOMIC_REL_TOLERANCE = 1e-6
ECONOMIC_ABS_TOLERANCE = 1e-8

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
    strategy_instance_id: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profits: tuple[float, ...] = ()
    margin_mode: str | None = None
    position_mode: str | None = None
    order_kind: str | None = None
    has_successful_entry_evidence: bool = False
    protection_mutated: bool = False


@dataclass(frozen=True, slots=True)
class PositionEvidence:
    pos_id: str
    symbol: str
    side: str
    size: float | None
    average_price: float | None
    created_at_ms: int | None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profits: tuple[float, ...] = ()
    margin_mode: str | None = None
    position_mode: str | None = None
    order_kind: str | None = None


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


@dataclass(frozen=True, slots=True)
class EquivalentAttributionComponent:
    """A closed equivalent candidate population, never an ownership assignment."""

    leg_ids: tuple[int, ...]
    position_ids: tuple[str, ...]


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
    if not has_authoritative_persisted_position(leg):
        raise PositionAttributionError("position_ownership_evidence_not_authoritative")
    if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
        raise PositionAttributionError("position_ownership_terminal")
    return leg


def has_authoritative_persisted_position(leg) -> bool:
    """Return whether a persisted leg may authorize live position mutation."""

    pos_id = str(getattr(leg, "pos_id", None) or "")
    if not pos_id:
        return False
    order_id = str(getattr(leg, "order_id", None) or "")
    if order_id and order_id == pos_id:
        return True
    response_pos_id = _find_position_id_in_json(getattr(leg, "response_json", None))
    if response_pos_id == pos_id:
        return True
    try:
        evidence = json.loads(getattr(leg, "attribution_evidence_json", None) or "{}")
    except (TypeError, ValueError):
        return False
    if (
        isinstance(evidence, dict)
        and evidence.get("evidence_type") == "equivalent_permutation_assignment"
    ):
        return _has_authoritative_equivalent_permutation_assignment(leg, evidence)
    if (
        str(getattr(leg, "order_kind", None) or "") == "manual_bind"
        and isinstance(evidence, dict)
        and evidence.get("source") == "manual_operator_bind"
    ):
        return True
    return bool(
        isinstance(evidence, dict)
        and evidence.get("policy_version") == ATTRIBUTION_POLICY_VERSION
    )


def _has_authoritative_equivalent_permutation_assignment(
    leg, evidence: Mapping[str, object]
) -> bool:
    """Trust only the complete reviewed canonical evidence emitted by repair."""

    if evidence.get("policy_version") != ATTRIBUTION_POLICY_VERSION:
        return False
    if evidence.get("mapping_basis") != "stable_sorted_canonicalization":
        return False
    if evidence.get("ownership_statement") != (
        "binding owner proven; parent-child mapping canonicalized"
    ):
        return False

    component_leg_ids = evidence.get("component_leg_ids")
    component_position_ids = evidence.get("component_position_ids")
    if not isinstance(component_leg_ids, list) or not isinstance(
        component_position_ids, list
    ):
        return False
    if len(component_leg_ids) < 2 or len(component_leg_ids) != len(
        component_position_ids
    ):
        return False
    if any(
        isinstance(leg_id, bool) or not isinstance(leg_id, int)
        for leg_id in component_leg_ids
    ):
        return False
    if any(
        not isinstance(position_id, str) or not position_id
        for position_id in component_position_ids
    ):
        return False
    if len(set(component_leg_ids)) != len(component_leg_ids) or len(
        set(component_position_ids)
    ) != len(component_position_ids):
        return False
    if component_leg_ids != sorted(component_leg_ids):
        return False
    if component_position_ids != sorted(
        component_position_ids, key=_numeric_aware_identifier_key
    ):
        return False

    current_leg_id = getattr(leg, "id", None)
    current_pos_id = str(getattr(leg, "pos_id", None) or "")
    if current_leg_id not in component_leg_ids:
        return False
    pair_index = component_leg_ids.index(current_leg_id)
    if component_position_ids[pair_index] != current_pos_id:
        return False

    signature = evidence.get("equivalence_signature")
    if not isinstance(signature, dict):
        return False
    signature_keys = {
        "binding_id",
        "strategy_instance_id",
        "venue",
        "symbol",
        "side",
        "requested_size",
        "entry_price",
        "stop_loss",
        "take_profits",
        "protection_mutated",
        "margin_mode",
        "position_mode",
        "order_kind",
        "leg_population",
        "position_population",
    }
    if not signature_keys.issubset(signature):
        return False
    leg_population = signature.get("leg_population")
    position_population = signature.get("position_population")
    if not isinstance(leg_population, list) or not isinstance(
        position_population, list
    ):
        return False
    if len(leg_population) != len(component_leg_ids) or len(
        position_population
    ) != len(component_position_ids):
        return False

    leg_population_keys = {
        "leg_id",
        "binding_id",
        "strategy_instance_id",
        "venue",
        "symbol",
        "side",
        "requested_size",
        "entry_price",
        "stop_loss",
        "take_profits",
        "margin_mode",
        "position_mode",
        "order_kind",
        "protection_mutated",
    }
    position_population_keys = {
        "position_id",
        "symbol",
        "side",
        "size",
        "entry_price",
        "stop_loss",
        "take_profits",
        "margin_mode",
        "position_mode",
    }
    if any(
        not isinstance(row, dict) or not leg_population_keys.issubset(row)
        for row in leg_population
    ):
        return False
    if any(
        not isinstance(row, dict) or not position_population_keys.issubset(row)
        for row in position_population
    ):
        return False
    if [row["leg_id"] for row in leg_population] != component_leg_ids:
        return False
    if [row["position_id"] for row in position_population] != component_position_ids:
        return False

    current_leg_row = leg_population[pair_index]
    return bool(
        current_leg_row["binding_id"]
        == getattr(leg, "execution_binding_id", None)
        and current_leg_row["strategy_instance_id"]
        == getattr(leg, "strategy_instance_id", None)
        and str(current_leg_row["venue"] or "").lower()
        == str(getattr(leg, "venue", None) or "").lower()
        and current_leg_row["order_kind"] == getattr(leg, "order_kind", None)
    )


def _find_position_id_in_json(value: str | None) -> str | None:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None

    def find(item) -> str | None:
        if isinstance(item, dict):
            for key in ("posId", "pos_id", "positionId"):
                if item.get(key) not in (None, ""):
                    return str(item[key])
            for nested in item.values():
                found = find(nested)
                if found:
                    return found
        elif isinstance(item, list):
            for nested in item:
                found = find(nested)
                if found:
                    return found
        return None

    return find(payload)


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
    if state in {"manually_cancelled", "exchange_cancelled"}:
        return False
    if str(row.get("_evidence_source") or "").lower() == "trigger_fill":
        try:
            trigger_time = int(float(row.get("triggerTime") or 0))
        except (TypeError, ValueError):
            trigger_time = 0
        return trigger_time > 0 and str(row.get("errorCode") or "") == "0"
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
    *,
    allow_equivalent_permutation: bool = False,
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
                "policy_version": ATTRIBUTION_POLICY_VERSION,
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

    if allow_equivalent_permutation and remaining:
        equivalent_components = classify_equivalent_attribution_components(
            eligible_legs,
            position_rows,
            ((edge.leg_id, edge.pos_id) for edge in remaining),
            authoritative_owner_by_position={
                str(leg.pos_id): leg.leg_id
                for leg in eligible_legs
                if leg.pos_id not in (None, "")
            },
        )
        canonical_leg_ids: set[int] = set()
        canonical_position_ids: set[str] = set()
        legs_by_id = {leg.leg_id: leg for leg in eligible_legs}
        positions_by_id = {
            position.pos_id: position for position in position_rows
        }
        for component in equivalent_components:
            if len(component.leg_ids) < 2:
                continue
            sorted_leg_ids = sorted(component.leg_ids)
            sorted_position_ids = sorted(
                component.position_ids, key=_numeric_aware_identifier_key
            )
            component_evidence = {
                "policy_version": ATTRIBUTION_POLICY_VERSION,
                "evidence_type": "equivalent_permutation_assignment",
                "component_leg_ids": sorted_leg_ids,
                "component_position_ids": sorted_position_ids,
                "equivalence_signature": _equivalence_signature(
                    [legs_by_id[leg_id] for leg_id in sorted_leg_ids],
                    [positions_by_id[pos_id] for pos_id in sorted_position_ids],
                ),
                "mapping_basis": "stable_sorted_canonicalization",
                "ownership_statement": (
                    "binding owner proven; parent-child mapping canonicalized"
                ),
            }
            for leg_id, pos_id in zip(
                sorted_leg_ids, sorted_position_ids, strict=True
            ):
                assignments[leg_id] = pos_id
                evidence_by_leg[leg_id] = dict(component_evidence)
            canonical_leg_ids.update(sorted_leg_ids)
            canonical_position_ids.update(sorted_position_ids)
        remaining = [
            edge
            for edge in remaining
            if edge.leg_id not in canonical_leg_ids
            and edge.pos_id not in canonical_position_ids
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


def _numeric_aware_identifier_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", str(value))
        if part
    )


def _equivalence_signature(
    legs: list[LegEvidence], positions: list[PositionEvidence]
) -> dict[str, object]:
    leg = legs[0]
    protection_mutated = any(item.protection_mutated for item in legs)
    return {
        "binding_id": leg.binding_id,
        "strategy_instance_id": leg.strategy_instance_id,
        "venue": leg.venue.lower(),
        "symbol": _normalize_instrument(leg.symbol),
        "side": _normalize_side(leg.side),
        "requested_size": leg.requested_size,
        "entry_price": leg.entry_price,
        "stop_loss": None if protection_mutated else leg.stop_loss,
        "take_profits": (
            [] if protection_mutated else sorted(leg.take_profits)
        ),
        "protection_mutated": protection_mutated,
        "margin_mode": leg.margin_mode,
        "position_mode": leg.position_mode,
        "order_kind": leg.order_kind,
        "leg_population": [
            {
                "leg_id": item.leg_id,
                "binding_id": item.binding_id,
                "strategy_instance_id": item.strategy_instance_id,
                "venue": item.venue.lower(),
                "symbol": _normalize_instrument(item.symbol),
                "side": _normalize_side(item.side),
                "requested_size": item.requested_size,
                "entry_price": item.entry_price,
                "stop_loss": item.stop_loss,
                "take_profits": sorted(item.take_profits),
                "margin_mode": item.margin_mode,
                "position_mode": item.position_mode,
                "order_kind": item.order_kind,
                "protection_mutated": item.protection_mutated,
            }
            for item in legs
        ],
        "position_population": [
            {
                "position_id": item.pos_id,
                "symbol": _normalize_instrument(item.symbol),
                "side": _normalize_side(item.side),
                "size": item.size,
                "entry_price": item.entry_price,
                "stop_loss": item.stop_loss,
                "take_profits": sorted(item.take_profits),
                "margin_mode": item.margin_mode,
                "position_mode": item.position_mode,
            }
            for item in positions
        ],
    }


def classify_equivalent_attribution_components(
    legs: Iterable[LegEvidence],
    positions: Iterable[PositionEvidence],
    candidate_edges: Iterable[tuple[int, str]],
    *,
    authoritative_owner_by_position: Mapping[str, int] | None = None,
    evidence_available: bool = True,
) -> tuple[EquivalentAttributionComponent, ...]:
    """Classify closed equivalent components without assigning their members."""

    if not evidence_available:
        return ()
    legs_by_id = {leg.leg_id: leg for leg in legs}
    positions_by_id = {position.pos_id: position for position in positions}
    edge_rows = {(int(leg_id), str(pos_id)) for leg_id, pos_id in candidate_edges}
    if not edge_rows:
        return ()

    known_edges = {
        edge
        for edge in edge_rows
        if edge[0] in legs_by_id and edge[1] in positions_by_id
    }
    if len(known_edges) != len(edge_rows):
        return ()
    known_edges = set(
        filter_candidate_edges_by_entry_protection(
            legs_by_id.values(), positions_by_id.values(), known_edges
        )
    )
    if not known_edges:
        return ()

    adjacency: dict[tuple[str, object], set[tuple[str, object]]] = defaultdict(set)
    for leg_id, pos_id in known_edges:
        leg_node = ("leg", leg_id)
        position_node = ("position", pos_id)
        adjacency[leg_node].add(position_node)
        adjacency[position_node].add(leg_node)

    owners = {
        str(pos_id): int(leg_id)
        for pos_id, leg_id in (authoritative_owner_by_position or {}).items()
    }
    result: list[EquivalentAttributionComponent] = []
    seen: set[tuple[str, object]] = set()
    for start in sorted(adjacency, key=lambda item: (item[0], str(item[1]))):
        if start in seen:
            continue
        queue = deque([start])
        component_leg_ids: set[int] = set()
        component_position_ids: set[str] = set()
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if node[0] == "leg":
                component_leg_ids.add(int(node[1]))
            else:
                component_position_ids.add(str(node[1]))
            queue.extend(adjacency[node] - seen)

        component_legs = [legs_by_id[leg_id] for leg_id in component_leg_ids]
        component_positions = [
            positions_by_id[pos_id] for pos_id in component_position_ids
        ]
        if len(component_legs) != len(component_positions):
            continue
        if len({leg.binding_id for leg in component_legs}) != 1:
            continue
        strategy_ids = {leg.strategy_instance_id for leg in component_legs}
        if None in strategy_ids or len(strategy_ids) != 1:
            continue
        if any(
            leg.terminal or not leg.has_successful_entry_evidence
            for leg in component_legs
        ):
            continue
        if any(
            owners.get(position.pos_id) is not None
            and owners[position.pos_id] not in component_leg_ids
            for position in component_positions
        ):
            continue
        if not _equivalent_economic_population(component_legs, component_positions):
            continue
        result.append(
            EquivalentAttributionComponent(
                leg_ids=tuple(sorted(component_leg_ids)),
                position_ids=tuple(sorted(component_position_ids)),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.leg_ids, item.position_ids)))


def filter_candidate_edges_by_entry_protection(
    legs: Iterable[LegEvidence],
    positions: Iterable[PositionEvidence],
    candidate_edges: Iterable[tuple[int, str]],
) -> frozenset[tuple[int, str]]:
    """Use immutable direct TP/SL only to remove incompatible candidate edges."""

    legs_by_id = {leg.leg_id: leg for leg in legs}
    positions_by_id = {position.pos_id: position for position in positions}
    result: set[tuple[int, str]] = set()
    for leg_id, pos_id in candidate_edges:
        edge = (int(leg_id), str(pos_id))
        leg = legs_by_id.get(edge[0])
        position = positions_by_id.get(edge[1])
        if leg is None or position is None:
            result.add(edge)
            continue
        if leg.protection_mutated or _entry_protection_compatible(leg, position):
            result.add(edge)
    return frozenset(result)


def _entry_protection_compatible(
    leg: LegEvidence, position: PositionEvidence
) -> bool:
    if (
        leg.stop_loss is not None
        and position.stop_loss is not None
        and not _numbers_equivalent(leg.stop_loss, position.stop_loss)
    ):
        return False
    if (
        leg.take_profits
        and position.take_profits
        and not _number_tuples_equivalent(leg.take_profits, position.take_profits)
    ):
        return False
    return True


def _equivalent_economic_population(
    legs: list[LegEvidence], positions: list[PositionEvidence]
) -> bool:
    if not legs or not positions:
        return False
    first_leg = legs[0]
    ignore_protection = any(leg.protection_mutated for leg in legs)
    if any(
        not _leg_signatures_equivalent(
            first_leg, leg, ignore_protection=ignore_protection
        )
        for leg in legs[1:]
    ):
        return False
    return all(
        _leg_position_signatures_equivalent(
            first_leg, position, ignore_protection=ignore_protection
        )
        for position in positions
    )


def _leg_signatures_equivalent(
    left: LegEvidence,
    right: LegEvidence,
    *,
    ignore_protection: bool,
) -> bool:
    return bool(
        left.venue.lower() == right.venue.lower()
        and _normalize_instrument(left.symbol) == _normalize_instrument(right.symbol)
        and _normalize_side(left.side) == _normalize_side(right.side)
        and _numbers_equivalent(left.requested_size, right.requested_size)
        and _numbers_equivalent(left.entry_price, right.entry_price)
        and (
            ignore_protection
            or (
                _numbers_equivalent(left.stop_loss, right.stop_loss)
                and _number_tuples_equivalent(left.take_profits, right.take_profits)
            )
        )
        and left.margin_mode == right.margin_mode
        and left.position_mode == right.position_mode
        and left.order_kind == right.order_kind
    )


def _leg_position_signatures_equivalent(
    leg: LegEvidence,
    position: PositionEvidence,
    *,
    ignore_protection: bool,
) -> bool:
    required_values = (
        leg.requested_size,
        leg.entry_price,
        position.size,
        position.entry_price,
        leg.margin_mode,
        leg.position_mode,
        position.margin_mode,
        position.position_mode,
    )
    if any(value is None for value in required_values):
        return False
    if not ignore_protection and (
        leg.stop_loss is None
        or position.stop_loss is None
        or not leg.take_profits
        or not position.take_profits
    ):
        return False
    return bool(
        _normalize_instrument(leg.symbol) == _normalize_instrument(position.symbol)
        and _normalize_side(leg.side) == _normalize_side(position.side)
        and _numbers_equivalent(leg.requested_size, position.size)
        and _numbers_equivalent(leg.entry_price, position.entry_price)
        and (
            ignore_protection
            or (
                _numbers_equivalent(leg.stop_loss, position.stop_loss)
                and _number_tuples_equivalent(
                    leg.take_profits, position.take_profits
                )
            )
        )
        and leg.margin_mode == position.margin_mode
        and leg.position_mode == position.position_mode
    )


def _numbers_equivalent(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return isclose(
        float(left),
        float(right),
        rel_tol=ECONOMIC_REL_TOLERANCE,
        abs_tol=ECONOMIC_ABS_TOLERANCE,
    )


def _number_tuples_equivalent(
    left: tuple[float, ...], right: tuple[float, ...]
) -> bool:
    if len(left) != len(right):
        return False
    return all(
        _numbers_equivalent(left_value, right_value)
        for left_value, right_value in zip(sorted(left), sorted(right), strict=True)
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
        direct_order_position_id = bool(
            fill.order_id and str(fill.order_id) == str(position.pos_id)
        )
        if fill.pos_id is None and not direct_order_position_id:
            if fill.source != "trigger_fill":
                continue
            if fill.size is None or position.size is None:
                continue
            if fill.created_at_ms is None or position.created_at_ms is None:
                continue
            if (
                abs(fill.created_at_ms - position.created_at_ms)
                > MAX_TRIGGER_POSITION_LINK_TIME_DISTANCE_MS
            ):
                continue
        if not _compatible_size(fill.size, position.size):
            continue
        candidates.append(
            _Edge(
                leg_id=leg.leg_id,
                pos_id=position.pos_id,
                rank=MatchRank(
                    DIRECT_POS_ID if fill.pos_id or direct_order_position_id else tier,
                    _distance(fill.created_at_ms, position.created_at_ms),
                    _distance(fill.size, position.size),
                    _distance(fill.price, position.average_price),
                ),
                evidence_type=(
                    "direct_pos_id"
                    if fill.pos_id
                    else (
                        "direct_order_position_id"
                        if direct_order_position_id
                        else evidence_type
                    )
                ),
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
