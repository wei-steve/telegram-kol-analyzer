"""Named readers for Deepcoin trigger-order and position rows.

A-14. The venue answers with three different vocabularies for the same idea and
the codebase had learned all three -- separately, in six places, with four
different key tuples and one module that had learned none of them. That module
was ``break_even_convergence_executor``, and the cost was that automatic
break-even convergence refused every candidate from its first deploy to
2026-08-03 without ever succeeding once (A-13).

The rules the venue actually follows, established by reading raw responses
rather than documentation:

* A **position** row carries ``slTriggerPx`` / ``tpTriggerPx``. Both are
  present as keys even when empty (``"tpTriggerPx": ""`` on a stop-only
  position), and they reflect only the **most recent** TPSL write -- so they
  answer "what price did the last write put here" and never "does this
  position have a stop", nor even "what is this position's stop".

  Measured on 2026-09-10, minutes after a backup stop was added to one of two
  otherwise identical positions:

  =================  ====================  ==========================
  position           row's ``slTriggerPx``  pending TPSL orders
  =================  ====================  ==========================
  ``…216121996``     ``75548.6``            ``…216121995`` at 75700
                                            ``…219289222`` at 75548.6
  ``…216153672``     ``75700``              ``…216153671`` at 75700
  =================  ====================  ==========================

  The same field is the primary stop on one row and the backup on the other,
  and nothing in the field says which. ``set-position-sltp`` accumulates: the
  pending table is the full set, the position row is the last write. **Reading
  it and believing it is the primary stop yields a legal, correctly formatted,
  wrong value** -- and unlike a string comparison, which refuses and goes red,
  this one goes green.
* A **TPSL row** from ``trigger-orders-pending`` carries ``slTriggerPrice`` /
  ``tpTriggerPrice`` and **no ``posId`` at all** -- only ``instId`` and
  ``posSide``. Attribution has to come from the order id or the trade unit.
* A **conditional entry** row from the same endpoint carries the stop it will
  install as ``closeSLTriggerPrice`` / ``closeTPTriggerPrice``. That is the
  entry's *attached* stop, not a stop protecting a position, and conflating the
  two is its own defect: an unfilled entry's attached stop must never be
  counted as protection for a live position.

So the readers below are named for which of those three questions they answer,
and the entry-attached fields are deliberately a separate function rather than
another key in the same tuple. Import one of these instead of writing a key
tuple: forgetting the vocabulary then becomes an ImportError rather than a
silently wrong value.

**These readers return the venue's own text, and prices must never be compared
as text.** The venue writes ``"75700"`` where this repository, having
formatted the same number itself, writes ``"75700.0"`` -- and a string
comparison between those two rejected 27 different positions from 2026-07-26
onward before anyone noticed (the B line's 6f-1). Parse to ``Decimal`` before
comparing; text is returned only so that callers which must pass the value
back to the venue send exactly what the venue said.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

#: Position rows and TPSL rows, in the order the venue prefers them. The legacy
#: ``stopLossPrice`` / ``takeProfitPrice`` spellings are kept because rows
#: recorded before phase 5 still carry them.
_STOP_KEYS = ("slTriggerPx", "slTriggerPrice", "stopLossPrice")
_TAKE_PROFIT_KEYS = ("tpTriggerPx", "tpTriggerPrice", "takeProfitPrice")
#: The stop a conditional entry will install once it triggers -- not protection
#: for any position that exists now.
_ENTRY_ATTACHED_STOP_KEYS = ("closeSLTriggerPrice",)
_ENTRY_ATTACHED_TAKE_PROFIT_KEYS = ("closeTPTriggerPrice",)
#: ``posId`` is absent from TPSL rows; ``pos_id`` and ``id`` appear in rows this
#: repository has persisted itself.
_POSITION_ID_KEYS = ("posId", "pos_id", "id")
_TRIGGER_PRICE_KEYS = ("triggerPx", "triggerPrice")


def _first_present_text(row: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    """First key present with a non-empty value, as text. ``None`` if none is."""

    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def stop_trigger_price(row: Mapping[str, Any]) -> str | None:
    """The stop trigger price on a position row or a TPSL row.

    Not a test for whether a stop exists: a position row's ``slTriggerPx``
    reflects the most recent TPSL pair only. To ask whether a position is
    protected, read ``trigger-orders-pending`` and match by order id.
    """

    return _first_present_text(row, _STOP_KEYS)


def take_profit_trigger_price(row: Mapping[str, Any]) -> str | None:
    """The take-profit trigger price on a position row or a TPSL row."""

    return _first_present_text(row, _TAKE_PROFIT_KEYS)


def entry_attached_stop_trigger_price(row: Mapping[str, Any]) -> str | None:
    """The stop a conditional *entry* row will install when it triggers.

    Separate from :func:`stop_trigger_price` on purpose. This price protects
    nothing yet; counting it as a live position's stop is a defect of its own.
    """

    return _first_present_text(row, _ENTRY_ATTACHED_STOP_KEYS)


def entry_attached_take_profit_trigger_price(
    row: Mapping[str, Any],
) -> str | None:
    """The take-profit a conditional entry row will install when it triggers."""

    return _first_present_text(row, _ENTRY_ATTACHED_TAKE_PROFIT_KEYS)


def position_id_or_none(row: Mapping[str, Any]) -> str | None:
    """The row's position id, or ``None`` when the row does not carry one.

    A TPSL row from ``trigger-orders-pending`` never carries one. ``None`` is
    the answer, not an error and not the empty string: a caller that compares
    ``""`` against a real position id gets ``False`` forever and reads it as
    drift, which is exactly how automatic break-even convergence refused every
    candidate it ever saw. Attribute such rows by order id or trade unit.
    """

    return _first_present_text(row, _POSITION_ID_KEYS)


def trigger_price(row: Mapping[str, Any]) -> str | None:
    """The price at which a conditional order triggers."""

    return _first_present_text(row, _TRIGGER_PRICE_KEYS)


# --- The union reader, and why it is named the way it is ---------------------
#
# A-14. Four sites want "whatever trigger price this row carries, from any of
# the venue's spellings, including the one a conditional entry uses for the
# stop it will install". That union is exactly what A-13 flagged as a risk of
# its own: ``closeSLTriggerPrice`` on an unfilled entry protects nothing yet,
# so treating it as interchangeable with a live position's stop is how an
# unfilled entry's attached stop gets counted as protection. The four sites are
# display and attribution-shadow paths where the merge is what they mean, so
# the function exists -- with the merge in its name, so that nobody reaches for
# it while looking for "this position's stop".
#
# Zero counts as absent here, matching every call site it replaces: the venue
# writes "0" for a leg it is not using (a TPSL row protecting only the downside
# carries ``tpTriggerPrice: "0"``).

_UNION_STOP_KEYS = ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
_UNION_TAKE_PROFIT_KEYS = ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")


def _nonzero_number_or_none(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if number == 0 else number


def any_trigger_price_including_entry_attached(
    row: Mapping[str, Any],
    *,
    kind: str,
    include_position_field: bool = True,
) -> Any:
    """First present, non-zero trigger price under any of the venue's spellings.

    ``kind`` is ``"sl"`` or ``"tp"``. The value is returned exactly as the row
    carried it, so callers that expect the venue's own string keep getting it.

    ``include_position_field=False`` drops ``slTriggerPx`` / ``tpTriggerPx``,
    which belong to *position* rows -- an order row that carries them is not a
    shape this venue produces, and one call site reads order rows only.

    This merges a live position's stop with the stop an unfilled conditional
    entry will install. That is deliberate for display and shadow-comparison
    callers and wrong for anything deciding whether a position is protected;
    for the latter use :func:`stop_trigger_price` and match by order id.
    """

    keys = _UNION_STOP_KEYS if kind == "sl" else _UNION_TAKE_PROFIT_KEYS
    if not include_position_field:
        keys = keys[1:]
    for key in keys:
        value = row.get(key)
        if _nonzero_number_or_none(value) is not None:
            return value
    return None


def take_profit_present_failing_closed(row: Mapping[str, Any]) -> bool:
    """Is a take-profit present on this row, treating unreadable as present?

    A-14. This is a *predicate*, not a price read, and it deliberately does not
    share :func:`any_trigger_price_including_entry_attached`'s rule: a value
    that cannot be parsed at all counts as present, so an unrecognised payload
    keeps failing closed rather than being waved through as "no take profit".
    Only a positive number, or an unparseable value, means present; ``""``,
    missing and ``0`` mean absent -- the venue writes ``"0"`` for a leg it is
    not using, and an earlier defect had a zero read as a live take-profit,
    which rejected every convergence on an instrument that merely carried a
    stop.
    """

    for key in _UNION_TAKE_PROFIT_KEYS:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return True
        # "NaN" and "inf" parse as floats but are not prices. The predicate this
        # replaces used Decimal, which rejects them, and an existing test locks
        # that: unreadable must stay "present" so the caller fails closed.
        if not math.isfinite(parsed):
            return True
        if parsed > 0:
            return True
    return False
