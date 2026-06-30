"""Price text parsing and shorthand normalization helpers."""

from __future__ import annotations

import re
from typing import Any


_PRICE_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")


def extract_normalized_prices(
    text: Any,
    *,
    symbol: str | None = None,
    reference_price: float | None = None,
) -> list[float]:
    """Extract prices and expand common crypto shorthand.

    KOL messages sometimes write BTC prices in "wan" shorthand, for example
    ``5.89`` meaning ``58900``.  Explicit ``万`` always multiplies by 10000;
    otherwise the symbol's expected price band is used.
    """

    if text in (None, ""):
        return []
    raw_text = str(text)
    prices: list[float] = []
    for match in _PRICE_PATTERN.finditer(raw_text):
        raw_number = match.group(0)
        try:
            value = float(raw_number.replace(",", ""))
        except ValueError:
            continue
        suffix = raw_text[match.end() : match.end() + 1]
        if suffix == "万":
            value *= 10000
        else:
            value = normalize_price_value(
                value,
                symbol=symbol,
                reference_price=reference_price,
            )
        if value > 0:
            prices.append(float(f"{value:g}"))
    return prices


def normalize_price_value(
    value: float,
    *,
    symbol: str | None = None,
    reference_price: float | None = None,
) -> float:
    """Expand shorthand prices into the likely absolute venue price."""

    if value <= 0:
        return value
    reference = _positive_float(reference_price)
    if reference is not None:
        return _normalize_against_reference(value, reference)

    band = _symbol_price_band(symbol)
    if band is None:
        return value
    lower, upper = band
    normalized = value
    while normalized < lower:
        normalized *= 10
    if normalized <= upper:
        return normalized
    return value


def _normalize_against_reference(value: float, reference: float) -> float:
    if value >= reference * 0.2:
        return value
    candidates = [value * factor for factor in (1, 10, 100, 1000, 10000)]
    return min(candidates, key=lambda item: abs(item - reference) / reference)


def _symbol_price_band(symbol: str | None) -> tuple[float, float] | None:
    normalized = str(symbol or "").upper().replace("-USDT", "").replace("USDT", "")
    if normalized == "BTC":
        return 10000.0, 200000.0
    if normalized == "ETH":
        return 500.0, 20000.0
    return None


def _positive_float(value: float | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed and parsed > 0 else None
