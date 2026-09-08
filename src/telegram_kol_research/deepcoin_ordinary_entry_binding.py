"""Attribute an ordinary-order entry to the position it opened, or refuse.

Phase 5 promotes phase 4's shadow criterion 2 into the live entry path. The
question it answers is the one REST cannot answer directly: *which position did
this order open?*

``POST /deepcoin/trade/order`` replies with ``ordId``, ``clOrdId``, ``tag``,
``sCode`` and ``sMsg`` -- and no ``posId``. No read endpoint returns an order id
and a position id together; on the stream, ``Order`` and ``Trade`` carry no
position field and ``Position`` carries ``PI`` with no order field. So the link
is an **equation**, not a foreign key: *in split mode the position an ordinary
order opens is identified by that order's own ordId.*

Because it is an equation rather than something the exchange hands back, it is
never taken on its own. All three of these must hold together -- the triple
confirmation the user approved on 2026-09-07:

1. the stream pushed a ``Position`` frame whose ``PI`` equals this ordId and
   whose ``Po`` reached a non-zero size (the position actually opened);
2. REST lists a position under that exact posId;
3. that position's direction, and its size against the leg that was submitted,
   agree.

Any one of them failing yields ``unverified`` with the reason that failed, and
``unverified`` is the existing repository-wide gate that forbids automatic
protection modification, cancellation and claiming
(``ExecutionOrderLeg.attribution_status == "verified"`` is checked before every
such action). Nothing here ever claims a position by symbol, side, size, price,
time proximity, adjacent id, ``clOrdId`` or ``tag``.

**The equation is specific to ordinary orders.** Production data on 2026-09-07,
read read-only from ``execution_order_legs``:

======================  =====  =============  ==============
``order_kind``          legs   ``pos==ord``   ``pos!=ord``
======================  =====  =============  ==============
``market``              154    153            0
``trigger_limit``       416    0              204
======================  =====  =============  ==============

153 of 153 market entry legs carrying both ids satisfy it with no counterexample
(the 154th recorded no order id at all), and not one of the 204 trigger legs
that carry both does -- a trigger order's position is named after the child
order the trigger spawns. That is why only ordinary-order entries use this
chain, and why the trigger path is left exactly as it was.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.deepcoin_shadow_binding import (
    split_pos_id_for_ordinary_entry,
)
from telegram_kol_research.models import DeepcoinWsEvent

logger = logging.getLogger(__name__)

ATTRIBUTION_VERIFIED = "verified"
ATTRIBUTION_UNVERIFIED = "unverified"

# Reasons this module may refuse with. The first three are re-exported from the
# shadow chain so a refusal reads the same in the ledger as it does in the
# shadow report; the last two are this module's own size/direction check.
ORDINARY_ENTRY_REFUSAL_REASONS = frozenset(
    {
        "no_ws_position_frame_for_pos_id",
        "ws_position_never_opened",
        "rest_pos_id_not_confirmed_by_rest",
        "rest_positions_unreadable",
        "rest_pos_side_missing",
        "rest_pos_side_mismatch",
        "rest_pos_size_mismatch",
    }
)

_WS_POSITION_QTY_KEY = "Po"

# Sizes cross the boundary as decimal text on one side and JSON floats on the
# other, so an exact comparison would report a difference that does not exist.
_RELATIVE_TOLERANCE = Decimal("1e-9")


@dataclass(frozen=True)
class OrdinaryEntryAttribution:
    """The verdict on one ordinary entry's position attribution."""

    pos_id: str | None
    status: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        return self.status == ATTRIBUTION_VERIFIED and bool(self.pos_id)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _close_enough(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    if left == right:
        return True
    scale = max(abs(left), abs(right))
    if scale == 0:
        return True
    return abs(left - right) / scale <= _RELATIVE_TOLERANCE


def _normalize_position_side(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"long", "buy", "1"}:
        return "long"
    if text in {"short", "sell", "2"}:
        return "short"
    return None


def _position_rows_for(rows: list[dict[str, Any]], pos_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("posId") or row.get("positionId") or row.get("pos_id") or "")
        == pos_id
    ]


def _row_size(row: dict[str, Any]) -> Decimal | None:
    for key in ("pos", "sz", "availPos", "size", "positionSize"):
        size = _decimal(row.get(key))
        if size is not None:
            return size
    return None


def read_ws_position_frames(
    session_factory: Callable[[], Any],
    *,
    pos_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read the inbox's ``Position`` frames carrying this ``PI``.

    The column is indexed, so this is a point read rather than a window scan.
    Duplicate-marked rows are included on purpose: a re-delivery is still
    evidence that the exchange pushed the fact, and the de-duplication marking
    exists to stop it being *counted* twice, not to hide it.
    """

    from sqlalchemy import select

    with session_factory() as session:
        rows = session.execute(
            select(
                DeepcoinWsEvent.channel,
                DeepcoinWsEvent.position_id,
                DeepcoinWsEvent.raw_payload,
                DeepcoinWsEvent.received_at,
                DeepcoinWsEvent.received_ms,
            )
            .where(
                DeepcoinWsEvent.channel == "Position",
                DeepcoinWsEvent.position_id == pos_id,
            )
            .order_by(DeepcoinWsEvent.id.desc())
            .limit(limit)
        ).all()
    # Shaped exactly like the phase 4 chain's own frames, so the same
    # ``_frame_payload`` decoding applies -- including ``channel``, without which
    # the decoder cannot tell which table in the envelope is the one asked for.
    return [
        {
            "channel": row[0],
            "position_id": row[1],
            "raw_payload": row[2],
            "received_at": row[3],
            "received_ms": row[4],
        }
        for row in reversed(rows)
    ]


def resolve_ordinary_entry_attribution(
    session_factory: Callable[[], Any],
    *,
    deepcoin_client: Any,
    ord_id: str,
    inst_id: str,
    expected_position_side: str,
    expected_size: Any = None,
    attempts: int = 5,
    delay_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> OrdinaryEntryAttribution:
    """Apply the identity equation and its triple confirmation to one entry.

    Retries are for *timing*, not for persuasion: the stream frame and the REST
    row for a position that has just opened arrive within a moment of each
    other, and the loop stops at the first attempt where all three hold. It
    never widens what counts as a match between attempts.

    An unreadable ``list_positions`` is unknown, never "no such position": it
    refuses with ``rest_positions_unreadable`` rather than concluding against an
    empty list.
    """

    ord_id = str(ord_id or "").strip()
    if not ord_id:
        return OrdinaryEntryAttribution(
            pos_id=None,
            status=ATTRIBUTION_UNVERIFIED,
            reason="no_ws_position_frame_for_pos_id",
            evidence={"ord_id": ord_id},
        )
    expected_side = _normalize_position_side(expected_position_side)
    expected = _decimal(expected_size)
    evidence: dict[str, Any] = {
        "ord_id": ord_id,
        "inst_id": inst_id,
        "equation": "split_pos_id_equals_ordinary_ord_id",
        "expected_position_side": expected_side,
    }
    reason = "no_ws_position_frame_for_pos_id"

    for attempt in range(max(1, int(attempts))):
        frames = read_ws_position_frames(session_factory, pos_id=ord_id)
        if not frames:
            # No frame means no opened position to corroborate, and the answer
            # cannot become "verified" on this attempt however REST replies. So
            # the REST read is not issued at all: under the phase 5b read quota
            # a request that cannot change the verdict is a request that costs
            # another caller its token. This is the ordinary case for a limit
            # order that is live and unfilled.
            reason = "no_ws_position_frame_for_pos_id"
            if attempt + 1 < max(1, int(attempts)):
                sleep(delay_seconds)
            continue
        try:
            rest_positions = [
                row
                for row in (deepcoin_client.list_positions(inst_id=inst_id) or [])
                if isinstance(row, dict)
            ]
        except Exception:
            # Unknown, never zero. A failed read must not be allowed to look
            # like "the exchange says that position does not exist".
            logger.exception(
                "Deepcoin list_positions failed while attributing entry %s", ord_id
            )
            reason = "rest_positions_unreadable"
            rest_positions = None

        if rest_positions is not None:
            pos_id, refusal = split_pos_id_for_ordinary_entry(
                ord_id,
                position_frames=frames,
                rest_positions=rest_positions,
                rest_position_history=[],
            )
            if pos_id is None:
                reason = refusal or "no_ws_position_frame_for_pos_id"
            else:
                rows = _position_rows_for(rest_positions, pos_id)
                sides = {
                    side
                    for row in rows
                    if (side := _normalize_position_side(row.get("posSide")))
                }
                if not sides:
                    reason = "rest_pos_side_missing"
                elif len(sides) > 1 or (
                    expected_side is not None and expected_side not in sides
                ):
                    reason = "rest_pos_side_mismatch"
                    evidence["rest_position_sides"] = sorted(sides)
                else:
                    sizes = [size for row in rows if (size := _row_size(row)) is not None]
                    evidence["rest_position_sizes"] = [str(size) for size in sizes]
                    # Consistent means "this position could have been opened by
                    # this order and nothing else": non-zero, and no larger than
                    # what was ordered. Smaller is a partial fill and still ours;
                    # larger means something other than this order contributed to
                    # it, and then it is not ours to claim. Requiring exact
                    # equality instead would refuse every partial fill and leave
                    # a real filled position unattributed.
                    if expected is not None and not any(
                        size > 0 and (size <= expected or _close_enough(size, expected))
                        for size in sizes
                    ):
                        reason = "rest_pos_size_mismatch"
                    else:
                        evidence.update(
                            {
                                "ws_position_frame_count": len(frames),
                                "rest_position_row_count": len(rows),
                                "confirmed_side": next(iter(sides)),
                                "attempts_used": attempt + 1,
                            }
                        )
                        return OrdinaryEntryAttribution(
                            pos_id=pos_id,
                            status=ATTRIBUTION_VERIFIED,
                            reason="",
                            evidence=evidence,
                        )
        if attempt + 1 < max(1, int(attempts)):
            sleep(delay_seconds)

    evidence["attempts_used"] = max(1, int(attempts))
    assert reason in ORDINARY_ENTRY_REFUSAL_REASONS, reason
    return OrdinaryEntryAttribution(
        pos_id=None,
        status=ATTRIBUTION_UNVERIFIED,
        reason=reason,
        evidence=evidence,
    )
