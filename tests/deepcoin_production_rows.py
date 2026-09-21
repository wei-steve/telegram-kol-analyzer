"""Deepcoin rows in the shape the venue actually returns them.

Every field name and every convention here is copied from a document that
recorded a raw response, not invented:

* ``docs/2026-09-07-management-instruction-incident-server-notes.md`` section 2
  holds three raw ``trigger-orders-pending`` rows from 2026-09-04, 09-05 and
  09-07.  Their key set is **the same 23 keys** at all three moments.  Such a
  row carries ``side`` -- the **closing** direction, ``sell`` for a long -- next
  to ``posSide``, the position's own direction.  It **never carries
  ``posId``**.  It carries ``slTriggerPrice`` and ``closeSLTriggerPrice``
  together, and ``sz`` may be ``"0"``, which means "the whole position" rather
  than "no size".
* ``docs/2026-09-05-deepcoin-api-deterministic-link-research.md`` line 117 lists
  the ``trigger-orders-history`` fields.  There is **no ``state``**: whether a
  trigger fired is ``triggerTime``, and whether it failed is ``errorCode``.

The fixtures that these replace invented ``posId`` on pending rows, invented
``state: filled`` on history rows, and never once carried ``side: "sell"`` --
so every test passed against a row shape the exchange does not produce, while
production refused every real one.
"""

from __future__ import annotations

from typing import Any


#: The 23 keys of one ``GET /deepcoin/trade/trigger-orders-pending`` row, in the
#: order the endpoint documentation lists them.  A row with any other key set is
#: not a shape this venue has ever returned.
PENDING_TPSL_KEYS: tuple[str, ...] = (
    "instType",
    "instId",
    "ordId",
    "triggerPx",
    "ordPx",
    "sz",
    "ordType",
    "side",
    "posSide",
    "tdMode",
    "triggerOrderType",
    "triggerPxType",
    "lever",
    "slPrice",
    "slTriggerPrice",
    "tpPrice",
    "tpTriggerPrice",
    "closeSLPrice",
    "closeSLTriggerPrice",
    "closeTPPrice",
    "closeTPTriggerPrice",
    "cTime",
    "uTime",
)

#: ``GET /deepcoin/trade/trigger-orders-history``.  No ``state``, no ``posId``,
#: no ``clOrdId``.
TRIGGER_HISTORY_KEYS: tuple[str, ...] = (
    "instType",
    "instId",
    "ordId",
    "px",
    "sz",
    "triggerPx",
    "triggerPxType",
    "ordType",
    "side",
    "posSide",
    "tdMode",
    "lever",
    "triggerTime",
    "uTime",
    "cTime",
    "errorCode",
    "errorMsg",
    "slPrice",
    "slTriggerPrice",
    "tpPrice",
    "tpTriggerPrice",
    "closeSLPrice",
    "closeSLTriggerPrice",
    "closeTPPrice",
    "closeTPTriggerPrice",
)


def closing_side(position_side: str) -> str:
    """What the venue puts in ``side`` on a protection order for this position.

    A protection order closes, so a long position's protection is a ``sell``.
    Reading this as a position-direction alias is the defect the whole file
    exists to stop being re-introduced.
    """

    return "sell" if str(position_side).lower() == "long" else "buy"


def pending_stop_row(
    *,
    ord_id: str,
    inst_id: str,
    pos_side: str,
    trigger_price: str,
    size: str,
    close_trigger_price: str | None = None,
    created_time: str = "1788635962000",
    side: str | None = None,
) -> dict[str, Any]:
    """One pending ``TPSL`` stop, all 23 keys, as 2026-09-07 returned it."""

    return _pending_row(
        ord_id=ord_id,
        inst_id=inst_id,
        pos_side=pos_side,
        size=size,
        side=side,
        created_time=created_time,
        sl_trigger_price=trigger_price,
        close_sl_trigger_price=(
            trigger_price if close_trigger_price is None else close_trigger_price
        ),
        tp_trigger_price="0",
        close_tp_trigger_price="0",
    )


def pending_take_profit_row(
    *,
    ord_id: str,
    inst_id: str,
    pos_side: str,
    trigger_price: str,
    size: str,
    close_trigger_price: str | None = None,
    created_time: str = "1788635962000",
    side: str | None = None,
) -> dict[str, Any]:
    """One pending ``TPSL`` take profit, all 23 keys (2026-09-04 row 3)."""

    return _pending_row(
        ord_id=ord_id,
        inst_id=inst_id,
        pos_side=pos_side,
        size=size,
        side=side,
        created_time=created_time,
        sl_trigger_price="0",
        close_sl_trigger_price="0",
        tp_trigger_price=trigger_price,
        close_tp_trigger_price=(
            trigger_price if close_trigger_price is None else close_trigger_price
        ),
    )


def pending_conditional_entry_row(
    *,
    ord_id: str,
    inst_id: str,
    pos_side: str,
    trigger_price: str,
    size: str,
    attached_stop: str,
    created_time: str = "1788635962000",
) -> dict[str, Any]:
    """A resting *entry*: ``side`` equals ``posSide`` and the type is Conditional."""

    row = _pending_row(
        ord_id=ord_id,
        inst_id=inst_id,
        pos_side=pos_side,
        size=size,
        side="buy" if str(pos_side).lower() == "long" else "sell",
        created_time=created_time,
        sl_trigger_price="0",
        close_sl_trigger_price=attached_stop,
        tp_trigger_price="0",
        close_tp_trigger_price="0",
    )
    row["triggerOrderType"] = "Conditional"
    row["triggerPx"] = trigger_price
    return row


def _pending_row(
    *,
    ord_id: str,
    inst_id: str,
    pos_side: str,
    size: str,
    side: str | None,
    created_time: str,
    sl_trigger_price: str,
    close_sl_trigger_price: str,
    tp_trigger_price: str,
    close_tp_trigger_price: str,
) -> dict[str, Any]:
    normalized_side = str(pos_side).lower()
    row = {
        "instType": "SWAP",
        "instId": inst_id.upper(),
        "ordId": str(ord_id),
        "triggerPx": "0",
        "ordPx": "0",
        "sz": str(size),
        "ordType": "trigger",
        "side": side if side is not None else closing_side(normalized_side),
        "posSide": normalized_side,
        "tdMode": "cross",
        "triggerOrderType": "TPSL",
        "triggerPxType": "last",
        "lever": "20",
        "slPrice": "0",
        "slTriggerPrice": sl_trigger_price,
        "tpPrice": "0",
        "tpTriggerPrice": tp_trigger_price,
        "closeSLPrice": "0",
        "closeSLTriggerPrice": close_sl_trigger_price,
        "closeTPPrice": "0",
        "closeTPTriggerPrice": close_tp_trigger_price,
        "cTime": created_time,
        "uTime": created_time,
    }
    assert tuple(row) == PENDING_TPSL_KEYS, "pending TPSL row is not the venue's shape"
    return row


def trigger_history_row(
    *,
    ord_id: str,
    inst_id: str,
    pos_side: str,
    trigger_price: str,
    size: str,
    trigger_time: str = "1788527258",
    error_code: str = "",
    error_message: str = "",
    purpose: str = "take_profit",
) -> dict[str, Any]:
    """One ``trigger-orders-history`` row.  There is no ``state`` to read."""

    is_stop = purpose in {"stop_loss", "backup_stop"}
    row = {
        "instType": "SWAP",
        "instId": inst_id.upper(),
        "ordId": str(ord_id),
        "px": "0",
        "sz": str(size),
        "triggerPx": "0",
        "triggerPxType": "last",
        "ordType": "trigger",
        "side": closing_side(pos_side),
        "posSide": str(pos_side).lower(),
        "tdMode": "cross",
        "lever": "20",
        "triggerTime": str(trigger_time),
        "uTime": "1788527258000",
        "cTime": "1788520000000",
        "errorCode": error_code,
        "errorMsg": error_message,
        "slPrice": "0",
        "slTriggerPrice": trigger_price if is_stop else "0",
        "tpPrice": "0",
        "tpTriggerPrice": "0" if is_stop else trigger_price,
        "closeSLPrice": "0",
        "closeSLTriggerPrice": trigger_price if is_stop else "0",
        "closeTPPrice": "0",
        "closeTPTriggerPrice": "0" if is_stop else trigger_price,
    }
    assert tuple(row) == TRIGGER_HISTORY_KEYS, "history row is not the venue's shape"
    return row


def position_row(
    *, pos_id: str, inst_id: str, pos_side: str, size: str, avg_price: str
) -> dict[str, Any]:
    """One ``GET /account/positions`` row.

    ``slTriggerPx`` / ``tpTriggerPx`` reflect only the most recent TPSL write
    (ARCHITECTURE 4.8), so they are deliberately left at the last-written value
    rather than at "this position's stop" -- nothing may read them as that.
    """

    return {
        "instType": "SWAP",
        "mgnMode": "cross",
        "instId": inst_id.upper(),
        "posId": str(pos_id),
        "posSide": str(pos_side).lower(),
        "pos": str(size),
        "avgPx": str(avg_price),
        "lever": "20",
        "liqPx": "0",
        "useMargin": "0",
        "unrealizedProfit": "0",
        "lastPx": str(avg_price),
        "tpTriggerPx": "",
        "slTriggerPx": "",
        "mrgPosition": "split",
        "isLeading": False,
        "isFollow": False,
        "ccy": "USDT",
        "cTime": "1788520000000",
        "uTime": "1788527258000",
    }
