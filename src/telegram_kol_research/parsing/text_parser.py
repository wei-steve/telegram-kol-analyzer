"""Rule-based text parsing for Telegram trade signals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


SYMBOL_ALIASES = {
    "XBT": "BTC",
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}

SIDE_ALIASES = {
    "long": "long",
    "buy": "long",
    "bullish": "long",
    "short": "short",
    "sell": "short",
    "bearish": "short",
}


@dataclass(slots=True)
class ParsedSignalText:
    symbol: str | None
    side: str | None
    leverage: str | None
    entry_range: tuple[float, float] | None
    stop_loss: float | None
    take_profits: list[float] = field(default_factory=list)
    event_type: str = "signal"
    confidence: float = 0.0
    source_text: str = ""


def _normalize_symbol(text: str) -> str | None:
    tagged_match = re.search(r"[#$]([A-Za-z]{2,10})\b", text)
    if tagged_match:
        raw_symbol = tagged_match.group(1).upper()
        return SYMBOL_ALIASES.get(raw_symbol, raw_symbol)

    match = re.search(r"\b([A-Z]{2,10})\b", text)
    if not match:
        return None
    raw_symbol = match.group(1).upper()
    return SYMBOL_ALIASES.get(raw_symbol, raw_symbol)


def _extract_side(text: str) -> str | None:
    lowered = text.lower()
    for alias, normalized in SIDE_ALIASES.items():
        if re.search(rf"\b{alias}\b", lowered):
            return normalized
    long_bias_patterns = [
        r"\badd more\b",
        r"\badding\b",
        r"\bready to take off\b",
        r"\btake off\b",
    ]
    for pattern in long_bias_patterns:
        if re.search(pattern, lowered):
            return "long"
    return None


def _extract_entry_range(text: str) -> tuple[float, float] | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\b", text)
    if not match:
        return None
    return (float(match.group(1)), float(match.group(2)))


def _extract_stop_loss(text: str) -> float | None:
    match = re.search(r"\bSL\b[:\s]*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1))


def _extract_take_profits(text: str) -> list[float]:
    match = re.search(r"\bTP\b[:\s]*([0-9./\s]+)", text, flags=re.IGNORECASE)
    if not match:
        return []
    values = re.findall(r"\d+(?:\.\d+)?", match.group(1))
    return [float(value) for value in values]


def _extract_leverage(text: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?x)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower()


def _detect_event_type(text: str) -> str:
    lowered = text.lower()
    if "sl moved" in lowered or "move sl" in lowered or "stop moved" in lowered:
        return "stop_loss_update"
    if "tp hit" in lowered or "take profit hit" in lowered:
        return "take_profit_update"
    if "closed" in lowered or "close trade" in lowered:
        return "close_signal"
    return "entry_signal"


def _compute_confidence(parsed: ParsedSignalText) -> float:
    score = 0.0
    if parsed.symbol:
        score += 0.2
    if parsed.side:
        score += 0.2
    if parsed.entry_range:
        score += 0.2
    if parsed.stop_loss is not None:
        score += 0.2
    if parsed.take_profits:
        score += 0.2
    return round(score, 2)


def parse_signal_text(text: str) -> ParsedSignalText:
    """Parse a free-form trade signal into a normalized candidate."""

    parsed = ParsedSignalText(
        symbol=_normalize_symbol(text),
        side=_extract_side(text),
        leverage=_extract_leverage(text),
        entry_range=_extract_entry_range(text),
        stop_loss=_extract_stop_loss(text),
        take_profits=_extract_take_profits(text),
        event_type=_detect_event_type(text),
        source_text=text,
    )
    parsed.confidence = _compute_confidence(parsed)
    return parsed
