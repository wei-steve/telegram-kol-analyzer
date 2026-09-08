"""Blank contact identifiers before any price or quantity is read from text.

A signature line is message text like any other, so text provenance -- "this
number really does appear in the message" -- can prove where a number came
from but never what it means.  On 2026-09-05 (raw 15013) and again on
2026-09-08 (raw 15402) the same KOL signature ``@Tarderfengge QQ:158241758``
was read as an explicit stop price, the stop gate refused the whole batch,
and the instruction the KOL actually gave was not executed.

Scrubbing runs on the *extraction input* only.  The stored message text is
never rewritten, and nothing here decides anything about a message: it only
removes spans that cannot contain a price.

Replacement is length preserving -- every scrubbed character becomes a single
space, except a line break, which is kept.  Extraction sites bound how far a
number may sit from its label (``止损[^0-9]{0,20}``, a 32-character provenance
window) and some read the text line by line, so keeping both offsets and line
boundaries keeps every surviving number exactly where it was.
"""

from __future__ import annotations

import re

#: Nine digits is already an order of magnitude past any crypto price this
#: system trades, and QQ numbers land squarely in it.  A real price that ever
#: reaches this length is caught downstream by the magnitude check instead.
MIN_BARE_DIGIT_RUN = 9

_ASCII_MESSENGER_ALIASES = r"weixin|wechat|vx|v\s*信"
_CJK_MESSENGER_ALIASES = r"微信|威信|薇信|企鹅号"
_PHONE_LABELS = r"电话|手机|联系电话|联系方式|tel|phone|mobile|whatsapp"
_ID_TOKEN = r"[A-Za-z0-9_.\-]{3,}"
_LABEL_SEPARATOR = r"\s*(?:号码|号|id|ID)?\s*[:：=＝]?\s*"

_CONTACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # QQ, but never the ticker: ``QQQ``/``QQQUSDT`` are real instruments, so a
    # third Q disqualifies the match, and the digits must follow the label.
    re.compile(
        rf"(?<![A-Za-z0-9])[Qq]{{2}}(?![A-Za-z]){_LABEL_SEPARATOR}\d{{4,}}",
    ),
    re.compile(rf"扣扣{_LABEL_SEPARATOR}\d{{4,}}"),
    # Messenger handles.  The ASCII aliases are short enough to occur inside
    # base64 blobs and URLs, so they must stand alone as a word.
    re.compile(
        rf"(?<![A-Za-z0-9])(?:{_ASCII_MESSENGER_ALIASES})"
        rf"(?![A-Za-z0-9]){_LABEL_SEPARATOR}{_ID_TOKEN}",
        re.IGNORECASE,
    ),
    re.compile(rf"(?:{_CJK_MESSENGER_ALIASES}){_LABEL_SEPARATOR}{_ID_TOKEN}"),
    re.compile(
        rf"(?<![A-Za-z0-9])(?:{_PHONE_LABELS}){_LABEL_SEPARATOR}\+?[\d\-]{{6,}}",
        re.IGNORECASE,
    ),
    # A Telegram @handle must start with a letter or underscore, so ``@2530``
    # -- a price written with an at sign -- is deliberately left alone.
    re.compile(r"@[A-Za-z_][A-Za-z0-9_]{2,63}"),
    # Bare long digit runs.  The lookarounds keep the fractional part of a
    # decimal from being read as a run of its own.
    re.compile(rf"(?<![\d.])\d{{{MIN_BARE_DIGIT_RUN},}}(?![\d.])"),
)


def contact_identifier_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return the character spans that carry contact identifiers, merged."""

    spans: list[tuple[int, int]] = []
    for pattern in _CONTACT_PATTERNS:
        # Every pattern scans the original text: blanking one match must not
        # be able to hide another pattern's match behind it.
        spans.extend(match.span() for match in pattern.finditer(text))
    if not spans:
        return ()
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def scrub_contact_identifiers(text: str | None) -> str:
    """Return ``text`` with contact identifiers replaced by spaces.

    Length preserving, so any offset-sensitive extraction downstream keeps
    seeing the same distances it saw before.
    """

    if not text:
        return "" if text is None else str(text)
    normalized = str(text)
    spans = contact_identifier_spans(normalized)
    if not spans:
        return normalized
    characters = list(normalized)
    for start, end in spans:
        for index in range(start, end):
            # A separator's ``\s*`` can reach across a line break. Blanking the
            # break itself would merge two lines, and line-oriented extraction
            # would then read a label and a price that were never together.
            if characters[index] not in "\r\n":
                characters[index] = " "
    return "".join(characters)


def text_carries_contact_identifier(text: str | None) -> bool:
    """Whether scrubbing would remove anything from ``text``."""

    return bool(text) and bool(contact_identifier_spans(str(text)))
