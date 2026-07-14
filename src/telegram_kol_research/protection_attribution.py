"""Pure, fail-closed association of Deepcoin TPSL evidence to live positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isclose
from typing import Any


DEFAULT_PROTECTION_TIME_TOLERANCE_MS = 5_000


@dataclass(slots=True)
class PositionProtection:
    status: str
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def can_mutate(self) -> bool:
        return self.status == "verified"


@dataclass(slots=True)
class ProtectionMatchResult:
    by_pos_id: dict[str, PositionProtection]


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
    unscoped_groups: dict[tuple[str, str, int | None], _ProtectionGroup] = {}
    for order in tpsl_orders:
        if str(order.get("triggerOrderType") or "TPSL").upper() != "TPSL":
            continue
        if not _row_has_protection(order):
            continue
        pos_id = _first_text(order, "posId", "pos_id", "positionId")
        if pos_id and pos_id in positions_by_id:
            exact_rows[pos_id].append(order)
            continue
        instrument_id = _instrument_id(order)
        side = _side(order)
        created_at_ms = _timestamp_ms(order)
        key = (instrument_id, side, created_at_ms)
        group = unscoped_groups.setdefault(
            key,
            _ProtectionGroup(
                instrument_id=instrument_id,
                side=side,
                created_at_ms=created_at_ms,
            ),
        )
        group.rows.append(order)

    for pos_id, rows in exact_rows.items():
        if rows:
            by_pos_id[pos_id] = _verified_protection(
                rows,
                evidence={"match": "exact_pos_id", "pos_id": pos_id},
            )

    for group in unscoped_groups.values():
        candidates: list[tuple[tuple[float, int], _Position]] = []
        for position in parsed_positions:
            if position.instrument_id != group.instrument_id or position.side != group.side:
                continue
            time_distance = _time_distance(position.created_at_ms, group.created_at_ms)
            if time_distance != inf and time_distance > max(0, int(time_tolerance_ms)):
                continue
            candidates.append(
                ((time_distance, _size_penalty(position.size, group.rows)), position)
            )
        if not candidates:
            continue
        best_rank = min(rank for rank, _position in candidates)
        winners = [position for rank, position in candidates if rank == best_rank]
        if len(winners) != 1:
            for position in winners:
                by_pos_id[position.pos_id] = PositionProtection(
                    status="present_but_ambiguous",
                    evidence={
                        "match": "ambiguous",
                        "candidate_pos_ids": sorted(item.pos_id for item in winners),
                        "order_ids": _order_ids(group.rows),
                        "has_stop_loss": any(
                            _protection_price(row, "sl") is not None
                            for row in group.rows
                        ),
                        "has_take_profit": any(
                            _protection_price(row, "tp") is not None
                            for row in group.rows
                        ),
                    },
                )
            continue

        winner = winners[0]
        current = by_pos_id[winner.pos_id]
        if current.status == "verified":
            combined_rows = [*exact_rows[winner.pos_id], *group.rows]
        else:
            combined_rows = list(group.rows)
        exact_rows[winner.pos_id] = combined_rows
        by_pos_id[winner.pos_id] = _verified_protection(
            combined_rows,
            evidence={
                "match": "unique_instrument_side_time",
                "time_distance_ms": best_rank[0],
                "size_penalty": best_rank[1],
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
    sizes = [value for _row, value in row_sizes]
    if any(value == 0 for value in sizes):
        return 0
    tp_sizes = [
        size
        for row, size in row_sizes
        if _protection_price(row, "tp") is not None
    ]
    if tp_sizes and isclose(sum(tp_sizes), position_size, rel_tol=1e-9, abs_tol=1e-9):
        return 0
    if any(isclose(value, position_size, rel_tol=1e-9, abs_tol=1e-9) for value in sizes):
        return 0
    return 1


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


def _time_distance(first: int | None, second: int | None) -> float:
    if first is None or second is None:
        return inf
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
