"""Read-only account-wide TPSL ownership coverage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from telegram_kol_research.protection_ledger import (
    build_account_protection_ownership,
)


@dataclass(frozen=True, slots=True)
class TpslOwnershipConflict:
    order_id: str
    ledger_pos_id: str | None
    exchange_pos_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class TpslOwnershipAudit:
    live_position_count: int
    pending_tpsl_count: int
    owned_pending_count: int
    owned_pending_order_ids: tuple[str, ...]
    unowned_pending_order_ids: tuple[str, ...]
    conflicts: tuple[TpslOwnershipConflict, ...]
    stale_ledger_order_ids: tuple[str, ...]
    exchange_write_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_tpsl_ownership_audit(
    *,
    positions: Iterable[dict[str, Any]],
    pending_orders: Iterable[dict[str, Any]],
    ledger_rows: Iterable[object],
    venue: str = "deepcoin",
) -> TpslOwnershipAudit:
    """Classify every pending TPSL only by exchange or ledger identity."""

    live_positions = [
        row for row in positions
        if isinstance(row, dict) and _nonzero_position(row)
    ]
    live_pos_ids = {
        pos_id for row in live_positions
        if (pos_id := _text(row, "PositionID", "posId", "pos_id", "id"))
    }
    ownership = build_account_protection_ownership(
        ledger_rows,
        venue=venue,
        live_pos_ids=live_pos_ids,
    )
    ownership_conflicts = {
        conflict.order_id: conflict
        for conflict in ownership.conflicts
    }

    pending = [
        row for row in pending_orders
        if isinstance(row, dict)
        and str(row.get("triggerOrderType") or "").upper() == "TPSL"
        and _text(
            row,
            "OrderSysID",
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
    ]
    pending_ids = {
        _text(
            row,
            "OrderSysID",
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        for row in pending
    }
    owned: list[str] = []
    unowned: list[str] = []
    conflicts: list[TpslOwnershipConflict] = []
    for row in pending:
        order_id = _text(
            row,
            "OrderSysID",
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        ledger_conflict = ownership_conflicts.get(order_id)
        if ledger_conflict is not None:
            conflicts.append(
                TpslOwnershipConflict(
                    order_id=order_id,
                    ledger_pos_id=",".join(ledger_conflict.pos_ids),
                    exchange_pos_id=None,
                    reason="multiple_ledger_position_owners",
                )
            )
            continue
        owner_row = ownership.owner_for_order(order_id)
        exchange_pos_id = _text(
            row,
            "PositionID",
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        ) or None
        ledger_pos_id = owner_row.pos_id if owner_row is not None else None
        if (
            ledger_pos_id is not None
            and exchange_pos_id is not None
            and exchange_pos_id != ledger_pos_id
        ):
            conflicts.append(
                TpslOwnershipConflict(
                    order_id=order_id,
                    ledger_pos_id=ledger_pos_id,
                    exchange_pos_id=exchange_pos_id,
                    reason="exchange_position_conflicts_with_ledger",
                )
            )
            continue
        owner = ledger_pos_id or exchange_pos_id
        if owner is None:
            unowned.append(order_id)
            continue
        if owner not in live_pos_ids:
            conflicts.append(
                TpslOwnershipConflict(
                    order_id=order_id,
                    ledger_pos_id=ledger_pos_id,
                    exchange_pos_id=exchange_pos_id,
                    reason="owner_position_not_live",
                )
            )
            continue
        owned.append(order_id)

    stale = sorted(
        order_id
        for order_id in ownership.by_order_id
        if order_id not in pending_ids
    )
    stale.extend(
        order_id
        for order_id in ownership.stale_order_ids
        if order_id not in stale
    )
    return TpslOwnershipAudit(
        live_position_count=len(live_positions),
        pending_tpsl_count=len(pending),
        owned_pending_count=len(owned),
        owned_pending_order_ids=tuple(sorted(owned)),
        unowned_pending_order_ids=tuple(sorted(unowned)),
        conflicts=tuple(sorted(conflicts, key=lambda item: (item.order_id, item.reason))),
        stale_ledger_order_ids=tuple(stale),
    )


def load_readonly_protection_ledger(
    database_path: str | Path,
    *,
    venue: str = "deepcoin",
) -> list[dict[str, Any]]:
    """Read active protection-ledger identity rows without creating a database."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT venue, order_id, pos_id, status, purpose
                FROM position_protection_ledger
                WHERE venue = ?
                ORDER BY order_id ASC
                """,
                (str(venue or "deepcoin").lower(),),
            )
        ]
    finally:
        connection.close()


def _text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _nonzero_position(payload: dict[str, Any]) -> bool:
    try:
        return abs(float(_text(payload, "pos", "size", "positionSize", "Volume"))) > 0
    except (TypeError, ValueError):
        return False
