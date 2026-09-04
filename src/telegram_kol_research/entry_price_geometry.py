"""Fail-closed direction and price geometry validation for new entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable, Literal, Mapping

from telegram_kol_research.price_normalization import extract_normalized_prices


GeometryStatus = Literal["valid", "invalid", "indeterminate"]

STOP_SIDE_INVALID = "entry_price_geometry_stop_side_invalid"
TAKE_PROFIT_SIDE_INVALID = "entry_price_geometry_take_profit_side_invalid"
EQUAL_BOUNDARY = "entry_price_geometry_equal_boundary"
AMBIGUOUS = "entry_price_geometry_ambiguous"
REQUIRED_VALUE_MISSING = "entry_price_geometry_required_value_missing"

_RELATIVE_MARKERS = (
    "%",
    "百分",
    "分钟",
    "小时",
    "个点",
    "点数",
)
_RELATIVE_UNIT_RE = re.compile(
    r"(?:\d(?:[\d.,]*\d)?\s*(?:points?|pts?|percent(?:age)?|pct|bps?|基点|点|刀))\b",
    re.IGNORECASE,
)
_RELATIVE_TO_ENTRY_RE = re.compile(
    r"(?:below|above|under|over|from)\s+(?:the\s+)?entry|"
    r"(?:entry(?:\s+price)?|open(?:ing)?\s+price)\s*(?:[-+]|minus|plus|below|above)|"
    r"(?:入场|进场)(?:价)?\s*(?:[-+]|上方|下方|之上|之下|加|减)|"
    r"(?:高于|低于)(?:入场|进场)(?:价)?",
    re.IGNORECASE,
)
_ABSOLUTE_NUMBER_RE = re.compile(
    r"\d+(?:,\d{3})*(?:\.\d+)?(?:万)?|\d+(?:\.\d+)?(?:万)?"
)
_SIGNED_NUMBER_RE = re.compile(r"(?<![\d.,])[-+−＋]\s*\$?\s*\d")
_ABSOLUTE_RANGE_HYPHEN_RE = re.compile(r"(?<=\d)\s*-\s*(?=\d)")
_MALFORMED_DECIMAL_RE = re.compile(
    r"(?<!\d)\.\d|\d\.{2,}\d|\d+\.\d+\.\d+"
)
_MARKET_LABEL_RE = re.compile(
    r"市价|现价|market(?:\s+price)?|current\s+price",
    re.IGNORECASE,
)
_DIRECT_PRICE_BEFORE_RE = re.compile(
    r"\d+(?:,\d{3})*(?:\.\d+)?(?:万)?\s*(?:u|usdt|usd|美元)?\s*$",
    re.IGNORECASE,
)
_DIRECT_PRICE_AFTER_RE = re.compile(
    r"^\s*[:：=]?\s*\$?\s*\d+(?:,\d{3})*(?:\.\d+)?(?:万)?",
    re.IGNORECASE,
)
_FIELD_LABELS = {
    "entry_prices": re.compile(
        r"entry\s*prices?|entries|entry|open(?:ing)?\s*prices?|"
        r"average|avg|limit|market\s*price|current\s*price|market|"
        r"平均价|均价|入场价?|进场价?|建仓|首仓|补仓|加仓|低接|接|"
        r"附近|区域|区间|分批|挂单|市价|现价|"
        r"做多|做空|点位|价格|价",
        re.IGNORECASE,
    ),
    "stop_loss": re.compile(
        r"stop\s*loss|stoploss|stop|sl|"
        r"prices?|"
        r"止损价位|止损位|止损|防守价?|附近|点位|价格|价",
        re.IGNORECASE,
    ),
    "take_profit": re.compile(
        r"take\s*profits?|takeprofits?|targets?|tp|"
        r"止盈|目标|附近|分批|点位|价格|价",
        re.IGNORECASE,
    ),
}
_ABSOLUTE_CURRENCY_RE = re.compile(
    r"\$|(?<![a-z])(?:usdt|usd|u)(?![a-z])|美元",
    re.IGNORECASE,
)
_FIELD_SEPARATORS_RE = re.compile(
    r"[\s\-\u2013\u2014~～至到/\\|,，、:：;；.!\uff01?\uff1f\u3002()\uff08\uff09\[\]{}]+"
)


@dataclass(frozen=True, slots=True)
class EntryPriceGeometryResult:
    status: GeometryStatus
    reason_code: str | None
    normalized_entry_prices: tuple[str, ...] = ()
    entry_min: str | None = None
    entry_max: str | None = None
    normalized_stop_loss: str | None = None
    normalized_take_profit_prices: tuple[str, ...] = ()
    normalized_explicit_average_entry: str | None = None
    offending_field: str | None = None
    offending_value: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "valid"

    def bounded_evidence(self) -> dict[str, Any]:
        return {
            "geometry_status": self.status,
            "reason_code": self.reason_code,
            "entry_domain": [self.entry_min, self.entry_max],
            "entry_prices": list(self.normalized_entry_prices),
            "stop_loss": self.normalized_stop_loss,
            "take_profit_prices": list(self.normalized_take_profit_prices),
            "explicit_average_entry": self.normalized_explicit_average_entry,
            "offending_field": self.offending_field,
            "offending_value": self.offending_value,
        }


def validate_entry_price_geometry(
    *,
    side: Any,
    entry_prices: Iterable[Any] | None,
    stop_loss: Any,
    take_profit_prices: Iterable[Any] | None = None,
    explicit_average_entry: Any = None,
    price_tick: Any = None,
    require_stop_loss: bool = True,
) -> EntryPriceGeometryResult:
    """Validate strict side/entry/protection relations without guessing values."""

    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"long", "short"}:
        return _indeterminate(AMBIGUOUS, "side", side)

    tick = _positive_decimal(price_tick) if price_tick is not None else None
    if price_tick is not None and tick is None:
        return _indeterminate(AMBIGUOUS, "price_tick", price_tick)

    raw_entries = _bounded_iterable(entry_prices)
    if raw_entries is None:
        return _indeterminate(AMBIGUOUS, "entry_prices", entry_prices)
    if not raw_entries:
        return _indeterminate(REQUIRED_VALUE_MISSING, "entry_prices", None)
    entries = _normalize_values(raw_entries, tick=tick)
    if entries is None:
        return _indeterminate(AMBIGUOUS, "entry_prices", entry_prices)

    raw_take_profits = _bounded_iterable(
        () if take_profit_prices is None else take_profit_prices
    )
    if raw_take_profits is None:
        return _indeterminate(AMBIGUOUS, "take_profit", take_profit_prices)
    take_profits = _normalize_values(raw_take_profits, tick=tick)
    if take_profits is None:
        return _indeterminate(AMBIGUOUS, "take_profit", take_profit_prices)

    if stop_loss in (None, ""):
        if require_stop_loss:
            return _indeterminate(
                REQUIRED_VALUE_MISSING,
                "stop_loss",
                None,
                entries=entries,
                take_profits=take_profits,
            )
        stop = None
    else:
        stop = _normalize_value(stop_loss, tick=tick)
        if stop is None:
            return _indeterminate(
                AMBIGUOUS,
                "stop_loss",
                stop_loss,
                entries=entries,
                take_profits=take_profits,
            )

    average = None
    if explicit_average_entry not in (None, ""):
        average = _normalize_value(explicit_average_entry, tick=tick)
        if average is None:
            return _indeterminate(
                AMBIGUOUS,
                "explicit_average_entry",
                explicit_average_entry,
                entries=entries,
                stop=stop,
                take_profits=take_profits,
            )

    entry_min = min(entries)
    entry_max = max(entries)
    common = {
        "entries": entries,
        "stop": stop,
        "take_profits": take_profits,
        "average": average,
    }
    if average is not None and not entry_min <= average <= entry_max:
        return _indeterminate(
            AMBIGUOUS,
            "explicit_average_entry",
            average,
            **common,
        )

    if stop is not None:
        stop_boundary = entry_min if normalized_side == "long" else entry_max
        if stop == stop_boundary:
            return _invalid(EQUAL_BOUNDARY, "stop_loss", stop, **common)
        stop_valid = (
            stop < entry_min if normalized_side == "long" else stop > entry_max
        )
        if not stop_valid:
            return _invalid(STOP_SIDE_INVALID, "stop_loss", stop, **common)

    for take_profit in take_profits:
        take_profit_boundary = (
            entry_max if normalized_side == "long" else entry_min
        )
        if take_profit == take_profit_boundary:
            return _invalid(
                EQUAL_BOUNDARY, "take_profit", take_profit, **common
            )
        take_profit_valid = (
            take_profit > entry_max
            if normalized_side == "long"
            else take_profit < entry_min
        )
        if not take_profit_valid:
            return _invalid(
                TAKE_PROFIT_SIDE_INVALID,
                "take_profit",
                take_profit,
                **common,
            )

    return _result(
        status="valid",
        reason_code=None,
        entries=entries,
        stop=stop,
        take_profits=take_profits,
        average=average,
    )


def validate_candidate_entry_price_geometry(
    *,
    side: Any,
    entry_text: Any,
    stop_loss_text: Any,
    take_profit_text: Any,
    symbol: str | None,
    reference_price: float | None = None,
    resolved_entry_prices: Iterable[Any] | None = None,
    price_tick: Any = None,
) -> EntryPriceGeometryResult:
    """Strictly parse one candidate and delegate to the shared validator."""

    normalized_entry_text = _strip_entry_ordinals(entry_text)
    if not _proves_absolute_candidate_field(
        normalized_entry_text,
        field="entry_prices",
        symbol=symbol,
    ):
        return _indeterminate(AMBIGUOUS, "entry_prices", entry_text)
    entry_values = extract_normalized_prices(
        normalized_entry_text,
        symbol=symbol,
        reference_price=reference_price,
    )
    average = None
    explicit_average = _parse_explicit_average(
        normalized_entry_text,
        symbol=symbol,
        reference_price=reference_price,
    )
    if len(entry_values) == 3 and explicit_average is not None:
        entry_values, average = explicit_average
    elif len(entry_values) > 2:
        return _indeterminate(AMBIGUOUS, "entry_prices", entry_text)
    elif not entry_values and resolved_entry_prices is not None:
        resolved = _bounded_iterable(resolved_entry_prices)
        if resolved is None:
            return _indeterminate(AMBIGUOUS, "entry_prices", resolved_entry_prices)
        entry_values = list(resolved)

    if not _proves_absolute_candidate_field(
        stop_loss_text,
        field="stop_loss",
        symbol=symbol,
    ):
        return _indeterminate(AMBIGUOUS, "stop_loss", stop_loss_text)
    stop_values = extract_normalized_prices(
        stop_loss_text,
        symbol=symbol,
        reference_price=reference_price,
    )
    if stop_loss_text not in (None, "") and len(stop_values) != 1:
        return _indeterminate(AMBIGUOUS, "stop_loss", stop_loss_text)
    stop = stop_values[0] if stop_values else None

    if not _proves_absolute_candidate_field(
        take_profit_text,
        field="take_profit",
        symbol=symbol,
    ):
        return _indeterminate(AMBIGUOUS, "take_profit", take_profit_text)
    normalized_take_profit_text = _strip_take_profit_ordinals(take_profit_text)
    take_profits = extract_normalized_prices(
        normalized_take_profit_text,
        symbol=symbol,
        reference_price=reference_price,
    )
    if take_profit_text not in (None, "") and not take_profits:
        return _indeterminate(AMBIGUOUS, "take_profit", take_profit_text)

    return validate_entry_price_geometry(
        side=side,
        entry_prices=entry_values,
        explicit_average_entry=average,
        stop_loss=stop,
        take_profit_prices=take_profits,
        price_tick=price_tick,
    )


def validate_order_draft_price_geometry(
    draft: Mapping[str, Any] | Any,
    *,
    expected_position_side: Any = None,
) -> EntryPriceGeometryResult:
    """Recompute geometry from every final normalized order-draft leg."""

    if not isinstance(draft, Mapping):
        return _indeterminate(REQUIRED_VALUE_MISSING, "order_draft", None)
    legs = draft.get("order_legs")
    if not isinstance(legs, list) or not legs:
        return _indeterminate(REQUIRED_VALUE_MISSING, "entry_prices", None)
    if any(not isinstance(leg, Mapping) for leg in legs):
        return _indeterminate(AMBIGUOUS, "order_legs", None)
    expected_side = str(expected_position_side or "").strip().lower()
    if expected_position_side is not None and expected_side not in {"long", "short"}:
        return _indeterminate(AMBIGUOUS, "side", expected_position_side)
    stated_position_sides = {
        str(leg.get("position_side") or "").strip().lower()
        for leg in legs
        if leg.get("position_side") not in (None, "")
    }
    if expected_side:
        if any(leg.get("position_side") in (None, "") for leg in legs) or any(
            value != expected_side for value in stated_position_sides
        ):
            return _indeterminate(AMBIGUOUS, "side", sorted(stated_position_sides))
        position_side = expected_side
    else:
        if len(stated_position_sides) != 1 or any(
            leg.get("position_side") in (None, "") for leg in legs
        ):
            return _indeterminate(
                AMBIGUOUS, "side", sorted(stated_position_sides)
            )
        position_side = next(iter(stated_position_sides))
    if position_side not in {"long", "short"}:
        return _indeterminate(AMBIGUOUS, "side", position_side)
    expected_open_side = "buy" if position_side == "long" else "sell"
    for leg in legs:
        leg_open_side = str(leg.get("side") or "").strip().lower()
        if not leg_open_side or leg_open_side != expected_open_side:
            return _indeterminate(AMBIGUOUS, "side", leg_open_side)
    top_position_side = draft.get("position_side")
    if top_position_side not in (None, "") and (
        str(top_position_side).strip().lower() != position_side
    ):
        return _indeterminate(AMBIGUOUS, "side", top_position_side)
    top_open_side = draft.get("open_side")
    if top_open_side not in (None, "") and (
        str(top_open_side).strip().lower() != expected_open_side
    ):
        return _indeterminate(AMBIGUOUS, "side", top_open_side)
    top_side = draft.get("side")
    if top_side not in (None, ""):
        normalized_top_side = str(top_side).strip().lower()
        if normalized_top_side not in {position_side, expected_open_side}:
            return _indeterminate(AMBIGUOUS, "side", top_side)
    entry_prices = [leg.get("price") for leg in legs]
    take_profit_legs = draft.get("take_profit_legs")
    if take_profit_legs is None:
        take_profit_legs = []
    if not isinstance(take_profit_legs, list) or any(
        not isinstance(leg, Mapping) for leg in take_profit_legs
    ):
        return _indeterminate(AMBIGUOUS, "take_profit", None)
    contract_spec = draft.get("contract_spec")
    if not isinstance(contract_spec, Mapping) or contract_spec.get("price_tick") is None:
        return _indeterminate(REQUIRED_VALUE_MISSING, "price_tick", None)
    tick = contract_spec.get("price_tick")
    return validate_entry_price_geometry(
        side=position_side,
        entry_prices=entry_prices,
        stop_loss=draft.get("stop_loss"),
        take_profit_prices=[leg.get("price") for leg in take_profit_legs],
        price_tick=tick,
    )


def _bounded_iterable(value: Iterable[Any] | None) -> list[Any] | None:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return None
    try:
        items = list(value)
    except TypeError:
        return None
    return items if len(items) <= 32 else None


def _normalize_values(
    values: list[Any], *, tick: Decimal | None
) -> tuple[Decimal, ...] | None:
    normalized: list[Decimal] = []
    for value in values:
        item = _normalize_value(value, tick=tick)
        if item is None:
            return None
        normalized.append(item)
    return tuple(normalized)


def _normalize_value(value: Any, *, tick: Decimal | None) -> Decimal | None:
    parsed = _positive_decimal(value)
    if parsed is None:
        return None
    if tick is not None:
        steps = (parsed / tick).to_integral_value(rounding=ROUND_DOWN)
        parsed = steps * tick
    return parsed if parsed.is_finite() and parsed > 0 else None


def _positive_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _has_relative_numeric_expression(value: Any) -> bool:
    text = str(value or "").lower()
    if not any(character.isdigit() for character in text):
        return False
    return (
        any(marker in text for marker in _RELATIVE_MARKERS)
        or bool(_RELATIVE_UNIT_RE.search(text))
        or bool(_RELATIVE_TO_ENTRY_RE.search(text))
        or bool(re.search(r"(?:\d\s*[x倍]|[x]\s*\d)", text))
    )


def _proves_absolute_candidate_field(
    value: Any,
    *,
    field: str,
    symbol: str | None,
) -> bool:
    if value in (None, ""):
        return True
    text = str(value).strip().lower()
    if _MALFORMED_DECIMAL_RE.search(text):
        return False
    signed_number_text = _ABSOLUTE_RANGE_HYPHEN_RE.sub(" ", text)
    if _SIGNED_NUMBER_RE.search(signed_number_text):
        return False
    if _has_relative_numeric_expression(value):
        return False
    if field == "entry_prices" and _has_unpriced_market_leg(text):
        return False
    if field == "entry_prices":
        text = str(_strip_entry_ordinals(text))
    if field == "take_profit":
        text = str(_strip_take_profit_ordinals(text))
    text = _strip_matching_symbol_aliases(text, symbol=symbol)
    text = _ABSOLUTE_NUMBER_RE.sub(" ", text)
    text = _ABSOLUTE_CURRENCY_RE.sub(" ", text)
    label_pattern = _FIELD_LABELS.get(field)
    if label_pattern is None:
        return False
    text = label_pattern.sub(" ", text)
    return not _FIELD_SEPARATORS_RE.sub("", text).strip()


def _has_unpriced_market_leg(text: str) -> bool:
    for match in _MARKET_LABEL_RE.finditer(text):
        if _DIRECT_PRICE_BEFORE_RE.search(text[: match.start()]):
            continue
        if _DIRECT_PRICE_AFTER_RE.search(text[match.end() :]):
            continue
        return True
    return False


def _strip_matching_symbol_aliases(text: str, *, symbol: str | None) -> str:
    normalized = str(symbol or "").strip().upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        normalized = normalized[:-5]
    if normalized.endswith("-USDT"):
        base = normalized[:-5]
    elif normalized.endswith("USDT"):
        base = normalized[:-4]
    else:
        base = normalized
    if not base or not re.fullmatch(r"[A-Z0-9]{2,16}", base):
        return text
    aliases = sorted(
        {
            base,
            f"{base}USDT",
            f"{base}-USDT",
            f"{base}/USDT",
            f"{base}_USDT",
            f"{base}-USDT-SWAP",
        },
        key=len,
        reverse=True,
    )
    pattern = re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(re.escape(alias) for alias in aliases)
        + r")(?![a-z0-9])",
        re.IGNORECASE,
    )
    return pattern.sub(" ", text)


def _strip_take_profit_ordinals(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    patterns = (
        r"\b(?:tp|take\s*profit)\s*#?\s*\d{1,2}(?!\d)(?=\s*[:：=,，、-]?\s*\d)",
        r"(?:止盈|目标)\s*#?\s*\d{1,2}(?!\d)(?=\s*[:：=,，、-]?\s*\d)",
    )
    normalized = value
    for pattern in patterns:
        normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)
    return normalized


def _strip_entry_ordinals(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    patterns = (
        r"\b(?:entry|leg)\s*#?\s*\d{1,2}(?!\d)(?=\s*[:：=,，、-]?\s*\d)",
        r"(?:入场|进场|建仓|首仓|补仓|加仓)\s*#?\s*\d{1,2}(?!\d)(?=\s*[:：=,，、-]?\s*\d)",
    )
    normalized = value
    for pattern in patterns:
        normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)
    return normalized


def _parse_explicit_average(
    value: Any,
    *,
    symbol: str | None,
    reference_price: float | None,
) -> tuple[list[float], float] | None:
    text = str(value or "").lower()
    locations = [
        (text.find(marker), marker)
        for marker in ("均价", "average", "avg")
        if text.find(marker) >= 0
    ]
    if len(locations) != 1:
        return None
    index, marker = locations[0]
    before = extract_normalized_prices(
        text[:index], symbol=symbol, reference_price=reference_price
    )
    after = extract_normalized_prices(
        text[index + len(marker) :],
        symbol=symbol,
        reference_price=reference_price,
    )
    if len(before) != 2 or len(after) != 1:
        return None
    return before, after[0]


def _display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    return str(value)[:128]


def _result(
    *,
    status: GeometryStatus,
    reason_code: str | None,
    entries: tuple[Decimal, ...] = (),
    stop: Decimal | None = None,
    take_profits: tuple[Decimal, ...] = (),
    average: Decimal | None = None,
    offending_field: str | None = None,
    offending_value: Any = None,
) -> EntryPriceGeometryResult:
    return EntryPriceGeometryResult(
        status=status,
        reason_code=reason_code,
        normalized_entry_prices=tuple(_display(value) or "" for value in entries),
        entry_min=_display(min(entries)) if entries else None,
        entry_max=_display(max(entries)) if entries else None,
        normalized_stop_loss=_display(stop),
        normalized_take_profit_prices=tuple(
            _display(value) or "" for value in take_profits
        ),
        normalized_explicit_average_entry=_display(average),
        offending_field=offending_field,
        offending_value=_display(offending_value),
    )


def _indeterminate(
    reason_code: str,
    offending_field: str,
    offending_value: Any,
    *,
    entries: tuple[Decimal, ...] = (),
    stop: Decimal | None = None,
    take_profits: tuple[Decimal, ...] = (),
    average: Decimal | None = None,
) -> EntryPriceGeometryResult:
    return _result(
        status="indeterminate",
        reason_code=reason_code,
        entries=entries,
        stop=stop,
        take_profits=take_profits,
        average=average,
        offending_field=offending_field,
        offending_value=offending_value,
    )


def _invalid(
    reason_code: str,
    offending_field: str,
    offending_value: Any,
    *,
    entries: tuple[Decimal, ...],
    stop: Decimal | None,
    take_profits: tuple[Decimal, ...],
    average: Decimal | None,
) -> EntryPriceGeometryResult:
    return _result(
        status="invalid",
        reason_code=reason_code,
        entries=entries,
        stop=stop,
        take_profits=take_profits,
        average=average,
        offending_field=offending_field,
        offending_value=offending_value,
    )
