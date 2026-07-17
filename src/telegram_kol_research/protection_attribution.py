"""Pure, fail-closed association of Deepcoin TPSL evidence to live positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isclose
from typing import Any


DEFAULT_PROTECTION_TIME_TOLERANCE_MS = 5_000


@dataclass(slots=True)
class PositionProtection:
    status: str
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def can_mutate(self) -> bool:
        return self.status == "verified"


@dataclass(slots=True)
class ProtectionMatchResult:
    by_pos_id: dict[str, PositionProtection]


def snapshot_protection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable, ordered TPSL rows suitable for mutation preflight.

    The snapshot intentionally keeps each exchange row separate.  In
    particular, multiple partial take-profit rows must never be collapsed into
    the last observed trigger price.
    """

    snapshots: list[dict[str, Any]] = []
    for row in rows:
        order_id = _first_text(
            row,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        size = _first_text(row, "sz", "size") or "0"
        tp_price = _first_text(
            row, "tpTriggerPx", "tpTriggerPrice", "takeProfitPrice"
        )
        sl_price = _first_text(
            row, "slTriggerPx", "slTriggerPrice", "stopLossPrice"
        )
        if tp_price is not None and sl_price is not None:
            snapshots.append(
                {
                    "order_id": order_id,
                    "purpose": "combined",
                    "take_profit": {
                        "trigger_price": tp_price,
                        "trigger_type": _first_text(row, "tpTriggerPxType") or "last",
                        "order_price": _first_text(row, "tpOrdPx") or "-1",
                    },
                    "stop_loss": {
                        "trigger_price": sl_price,
                        "trigger_type": _first_text(row, "slTriggerPxType") or "last",
                        "order_price": _first_text(row, "slOrdPx") or "-1",
                    },
                    "size": size,
                    "full_position": _float_or_none(size) == 0,
                }
            )
            continue
        if tp_price is not None:
            purpose = "take_profit"
            trigger_price = tp_price
            trigger_type = _first_text(row, "tpTriggerPxType") or "last"
            order_price = _first_text(row, "tpOrdPx") or "-1"
        elif sl_price is not None:
            purpose = "stop_loss"
            trigger_price = sl_price
            trigger_type = _first_text(row, "slTriggerPxType") or "last"
            order_price = _first_text(row, "slOrdPx") or "-1"
        else:
            continue
        snapshots.append(
            {
                "order_id": order_id,
                "purpose": purpose,
                "trigger_price": trigger_price,
                "size": size,
                "full_position": _float_or_none(size) == 0,
                "trigger_type": trigger_type,
                "order_price": order_price,
            }
        )
    return snapshots


@dataclass(frozen=True, slots=True)
class _Position:
    pos_id: str
    instrument_id: str
    side: str
    size: float | None
    created_at_ms: int | None
    raw: dict[str, Any]


@dataclass(slots=True)
class _ProtectionGroup:
    instrument_id: str
    side: str
    created_at_ms: int | None
    rows: list[dict[str, Any]] = field(default_factory=list)


def match_position_protection(
    positions: list[dict[str, Any]],
    tpsl_orders: list[dict[str, Any]],
    *,
    evidence_available: bool = True,
    time_tolerance_ms: int = DEFAULT_PROTECTION_TIME_TOLERANCE_MS,
) -> ProtectionMatchResult:
    """Match TPSL rows without borrowing strategy-ownership assumptions."""

    parsed_positions = [_parse_position(row) for row in positions]
    parsed_positions = [row for row in parsed_positions if row is not None]
    by_pos_id = {
        row.pos_id: PositionProtection(
            status="absent" if evidence_available else "evidence_unavailable"
        )
        for row in parsed_positions
    }
    positions_by_id = {row.pos_id: row for row in parsed_positions}

    exact_rows: dict[str, list[dict[str, Any]]] = {
        row.pos_id: _inline_position_protection_rows(row.raw) for row in parsed_positions
    }
    unscoped_groups: list[_ProtectionGroup] = []
    for order in tpsl_orders:
        if str(order.get("triggerOrderType") or "TPSL").upper() != "TPSL":
            continue
        if not _row_has_protection(order):
            continue
        pos_id = _first_text(
            order,
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        )
        if pos_id:
            if pos_id in positions_by_id:
                exact_rows[pos_id].append(order)
            continue
        instrument_id = _instrument_id(order)
        side = _side(order)
        created_at_ms = _timestamp_ms(order)
        merge_candidates = [
            candidate
            for candidate in unscoped_groups
            if _can_merge_protection_row(
                    candidate,
                    order,
                    instrument_id=instrument_id,
                    side=side,
                    created_at_ms=created_at_ms,
                    time_tolerance_ms=time_tolerance_ms,
                )
        ]
        group = None
        if merge_candidates:
            best_distance = min(
                _time_distance(candidate.created_at_ms, created_at_ms)
                for candidate in merge_candidates
            )
            nearest = [
                candidate
                for candidate in merge_candidates
                if _time_distance(candidate.created_at_ms, created_at_ms)
                == best_distance
            ]
            if len(nearest) == 1:
                group = nearest[0]
        if group is None:
            group = _ProtectionGroup(
                instrument_id=instrument_id,
                side=side,
                created_at_ms=created_at_ms,
            )
            unscoped_groups.append(group)
        group.rows.append(order)

    for pos_id, rows in exact_rows.items():
        if rows:
            by_pos_id[pos_id] = _verified_protection(
                rows,
                evidence={"match": "exact_pos_id", "pos_id": pos_id},
            )

    groups = list(unscoped_groups)
    edges: list[tuple[int, str, tuple[float, int]]] = []
    plausible_by_group: dict[int, list[_Position]] = {}
    for group_index, group in enumerate(groups):
        plausible: list[_Position] = []
        for position in parsed_positions:
            if position.instrument_id != group.instrument_id or position.side != group.side:
                continue
            plausible.append(position)
            time_distance = _time_distance(position.created_at_ms, group.created_at_ms)
            if time_distance is None or time_distance > max(0, int(time_tolerance_ms)):
                continue
            size_penalty = _size_penalty(position.size, group.rows)
            if size_penalty != 0:
                continue
            edges.append((group_index, position.pos_id, (time_distance, size_penalty)))
        plausible_by_group[group_index] = plausible

    assignments: dict[int, str] = {}
    remaining = list(edges)
    while remaining:
        group_best = _unique_best_protection_edges(remaining, key_index=0)
        position_best = _unique_best_protection_edges(remaining, key_index=1)
        accepted = [
            edge
            for edge in remaining
            if group_best.get(edge[0]) == edge and position_best.get(edge[1]) == edge
        ]
        if not accepted:
            break
        accepted_groups = {edge[0] for edge in accepted}
        accepted_positions = {edge[1] for edge in accepted}
        for group_index, pos_id, _rank in accepted:
            assignments[group_index] = pos_id
        remaining = [
            edge
            for edge in remaining
            if edge[0] not in accepted_groups and edge[1] not in accepted_positions
        ]

    unresolved_groups = set(range(len(groups))) - set(assignments)
    ambiguous_pos_ids = {
        position.pos_id
        for group_index in unresolved_groups
        for position in plausible_by_group.get(group_index, [])
    }
    # An accepted assignment is also unsafe if another protection group plausibly
    # competes for that same position. Never expose order IDs in this case.
    for group_index, pos_id in assignments.items():
        if any(
            candidate.pos_id == pos_id
            for unresolved_index in unresolved_groups
            for candidate in plausible_by_group.get(unresolved_index, [])
        ):
            ambiguous_pos_ids.add(pos_id)

    for group_index, pos_id in assignments.items():
        if pos_id in ambiguous_pos_ids:
            continue
        group = groups[group_index]
        rows = [*exact_rows[pos_id], *group.rows]
        exact_rows[pos_id] = rows
        rank = next(edge[2] for edge in edges if edge[0] == group_index and edge[1] == pos_id)
        by_pos_id[pos_id] = _verified_protection(
            rows,
            evidence={
                "match": "mutual_unique_instrument_side_time_size",
                "time_distance_ms": rank[0],
                "size_penalty": rank[1],
            },
        )

    for pos_id in ambiguous_pos_ids:
        ambiguous_rows = [
            row
            for group_index, group in enumerate(groups)
            if any(
                candidate.pos_id == pos_id
                for candidate in plausible_by_group.get(group_index, [])
            )
            for row in group.rows
        ]
        inline = _verified_protection(
            exact_rows[pos_id],
            evidence={"match": "inline_position"},
        )
        by_pos_id[pos_id] = PositionProtection(
            status="present_but_ambiguous",
            stop_loss=inline.stop_loss,
            take_profits=inline.take_profits,
            evidence={
                "match": "ambiguous_global_assignment",
                "has_inline_position_protection": bool(exact_rows[pos_id]),
                "has_stop_loss": any(
                    _protection_price(row, "sl") is not None for row in ambiguous_rows
                ),
                "has_take_profit": any(
                    _protection_price(row, "tp") is not None for row in ambiguous_rows
                ),
            },
        )

    return ProtectionMatchResult(by_pos_id=by_pos_id)


def _parse_position(row: dict[str, Any]) -> _Position | None:
    pos_id = _first_text(row, "posId", "pos_id", "id")
    if not pos_id:
        return None
    return _Position(
        pos_id=pos_id,
        instrument_id=_instrument_id(row),
        side=_side(row),
        size=_float_or_none(row.get("pos") or row.get("size") or row.get("sz")),
        created_at_ms=_timestamp_ms(row),
        raw=row,
    )


def _inline_position_protection_rows(position: dict[str, Any]) -> list[dict[str, Any]]:
    if not _row_has_protection(position):
        return []
    row = dict(position)
    row["posId"] = _first_text(position, "posId", "pos_id", "id")
    row["_evidence_source"] = "position"
    return [row]


def _verified_protection(
    rows: list[dict[str, Any]], *, evidence: dict[str, object]
) -> PositionProtection:
    stop_losses = [_protection_price(row, "sl") for row in rows]
    take_profits = [_protection_price(row, "tp") for row in rows]
    stop_losses = [value for value in stop_losses if value is not None]
    take_profits = [value for value in take_profits if value is not None]
    return PositionProtection(
        status="verified",
        stop_loss=stop_losses[-1] if stop_losses else None,
        take_profits=_unique_floats(take_profits),
        order_ids=_order_ids(rows),
        rows=[dict(row) for row in rows],
        evidence=evidence,
    )


def _size_penalty(position_size: float | None, rows: list[dict[str, Any]]) -> int:
    if position_size is None or position_size <= 0:
        return 1
    row_sizes = [
        (row, _float_or_none(row.get("sz") or row.get("size"))) for row in rows
    ]
    row_sizes = [
        (row, value) for row, value in row_sizes if value is not None and value >= 0
    ]
    tp_sizes = [
        size
        for row, size in row_sizes
        if _protection_price(row, "tp") is not None
    ]
    if tp_sizes:
        if any(value == 0 for value in tp_sizes):
            return 0
        return (
            0
            if isclose(sum(tp_sizes), position_size, rel_tol=1e-9, abs_tol=1e-9)
            else 1
        )
    sizes = [value for _row, value in row_sizes]
    if any(value == 0 for value in sizes):
        return 0
    if any(isclose(value, position_size, rel_tol=1e-9, abs_tol=1e-9) for value in sizes):
        return 0
    return 1


def _can_merge_protection_row(
    group: _ProtectionGroup,
    row: dict[str, Any],
    *,
    instrument_id: str,
    side: str,
    created_at_ms: int | None,
    time_tolerance_ms: int,
) -> bool:
    if group.instrument_id != instrument_id or group.side != side:
        return False
    distance = _time_distance(group.created_at_ms, created_at_ms)
    if distance is None or distance > max(0, int(time_tolerance_ms)):
        return False
    row_has_stop = _protection_price(row, "sl") is not None
    group_has_stop = any(_protection_price(item, "sl") is not None for item in group.rows)
    # One evidence group may contain one full stop and multiple partial targets.
    # A second stop is a competing group and must remain globally ambiguous.
    return not (row_has_stop and group_has_stop)


def _row_has_protection(row: dict[str, Any]) -> bool:
    return _protection_price(row, "sl") is not None or _protection_price(row, "tp") is not None


def _protection_price(row: dict[str, Any], kind: str) -> float | None:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if kind == "sl"
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _order_ids(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        order_id = _first_text(row, "ordId", "orderId", "order_id")
        if order_id and order_id not in result:
            result.append(order_id)
    return result


def _unique_floats(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _instrument_id(row: dict[str, Any]) -> str:
    return str(row.get("instId") or row.get("instrument_id") or "").upper()


def _side(row: dict[str, Any]) -> str:
    value = str(row.get("posSide") or row.get("side") or "").lower()
    return {"buy": "long", "sell": "short"}.get(value, value)


def _timestamp_ms(row: dict[str, Any]) -> int | None:
    value = row.get("cTime") or row.get("uTime") or row.get("created_at")
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _unique_best_protection_edges(
    edges: list[tuple[int, str, tuple[float, int]]], *, key_index: int
) -> dict[object, tuple[int, str, tuple[float, int]]]:
    result: dict[object, tuple[int, str, tuple[float, int]]] = {}
    keys = {edge[key_index] for edge in edges}
    for value in keys:
        candidates = [edge for edge in edges if edge[key_index] == value]
        best_rank = min(edge[2] for edge in candidates)
        winners = [edge for edge in candidates if edge[2] == best_rank]
        if len(winners) == 1:
            result[value] = winners[0]
    return result


def _time_distance(first: int | None, second: int | None) -> float | None:
    if first is None or second is None:
        return None
    return float(abs(first - second))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None
