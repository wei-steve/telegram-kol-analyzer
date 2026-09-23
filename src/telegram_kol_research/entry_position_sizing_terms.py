"""Read a position-sizing multiplier out of a message's own words.

Until 2026-09-23 the only way "半仓" could reach sizing was for MiMo to emit an
``entry_context`` block, and MiMo only emits one when it cannot map the message
onto an existing strategy. A sizing hint that *can* be mapped -- which is what a
confirmation message is -- was therefore guaranteed never to produce one. The
rule lives here instead, in deterministic code, so it does not depend on the
model changing its mind.

Only ``0 < m < 1`` is produced. A multiplier of exactly 1 and "no sizing word at
all" are the same instruction, so both answer ``None`` and no preamble row is
written for either; that keeps one code path instead of two and keeps the table
free of rows that change nothing. When a message carries two sizing words that
disagree, the answer is ``None`` as well: running at full size is the path the
system already takes, and guessing between them is not.
"""

from __future__ import annotations

import re
from decimal import Decimal


#: ``仓`` or ``仓位`` -- the noun that turns a quantity into a position size.
_POSITION_NOUN = r"仓位?"

#: ``半仓`` and ``轻仓``. ``轻仓`` is 0.5 because that is what the preambles
#: MiMo produced for this group in August already meant.
_NAMED_TERMS: tuple[tuple[str, Decimal], ...] = (
    ("半仓", Decimal("0.5")),
    ("轻仓", Decimal("0.5")),
)

_TENTHS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
#: ``三成仓``/``三成仓位``. ``十成`` is not listed, so a full position falls out
#: of the table on its own rather than needing a special case.
_TENTHS_RE = re.compile(rf"(?<![一二两三四五六七八九十])([{''.join(_TENTHS)}])成\s*{_POSITION_NOUN}")
#: ``20%仓位`` and ``仓位20%``. The percentage has to sit next to the position
#: noun, which is what keeps ``止盈了50%`` and ``回撤了30%`` out.
_PERCENT_BEFORE_RE = re.compile(rf"(\d{{1,3}})\s*(?:%|％)\s*的?\s*{_POSITION_NOUN}")
_PERCENT_AFTER_RE = re.compile(rf"{_POSITION_NOUN}\s*(?:是|为|用|开|到)?\s*(\d{{1,3}})\s*(?:%|％)")


def entry_position_risk_multiplier(text: str | None) -> Decimal | None:
    """Return the sizing multiplier this text states, or ``None``.

    ``None`` means "run the existing full-size path": either the text names no
    position size, or it names a full one, or it names two that disagree.
    """

    normalized = str(text or "").strip()
    if not normalized:
        return None

    found: list[Decimal] = []
    for term, multiplier in _NAMED_TERMS:
        if term in normalized:
            found.append(multiplier)
    for match in _TENTHS_RE.finditer(normalized):
        found.append(Decimal(_TENTHS[match.group(1)]) / Decimal(10))
    for pattern in (_PERCENT_BEFORE_RE, _PERCENT_AFTER_RE):
        for match in pattern.finditer(normalized):
            found.append(Decimal(match.group(1)) / Decimal(100))

    usable = [value for value in found if Decimal(0) < value < Decimal(1)]
    if len(usable) != len(found):
        # A full or out-of-range size was stated alongside a partial one; that
        # is a contradiction, not a partial size.
        return None
    distinct = {value for value in usable}
    if len(distinct) != 1:
        return None
    return distinct.pop()
