"""Recognise a KOL's new take-profit structure and plan it for one position.

Design: ``docs/plans/2026-09-29-take-profit-adjustment-design.md`` (section 5.5
holds the user's rulings and the tightened percentage rule, and wins over the
earlier sections wherever they differ).

Two pure functions and nothing else. Neither reads the database or the
exchange; the planner and the executor hand them everything they need.

``classify_take_profit_instruction``
    Is this management message a *take-profit adjustment* -- a new set of
    take-profit prices or a new split of the remaining position between them --
    rather than "close X% now"? The answer is positive evidence only. A
    percentage becomes a take-profit *allocation* when, in the same clause, it
    is used with 各/每个/每档/分别 plus a tier word, or with 设置/设好/挂/提前/
    自动止盈, or as ``P附近…止盈Y%`` with ``P`` written in this message. An
    immediate word (现价, 市价, 目前获利, 获利/浮盈 N 点, 剩余仓位/持仓, 已到, 到了,
    已经) in that same clause turns it back into a reduction, and any
    reduction percentage anywhere in the message means the message is left to
    the existing reduction path. Anything that is not positively a take-profit
    adjustment returns ``None`` and today's behaviour applies unchanged.

``plan_take_profit_adjustment``
    For one exact position (one ``pos_id``): given the live remaining size, the
    last price, this position's own live take-profit orders, the ledger's
    filled take-profit prices and the strategy's take-profit prices, say which
    take-profit orders the position should carry -- or why it cannot say.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.contact_digit_scrubbing import scrub_contact_identifiers

TAKE_PROFIT_ADJUST_INTENT = "adjust_take_profit"

MODE_FULL_RESET = "full_reset"  # 整套重设：每一档都给了价位和比例
MODE_SINGLE_TIER = "single_tier"  # 只说一档：一个价位 + 一个不足 100% 的比例
MODE_RATIOS_ONLY = "ratios_only"  # 只给比例：没有价位
MODE_PRICES_ONLY = "prices_only"  # 只给价位：没有比例

PLAN_READY = "ready"
PLAN_ALREADY_SATISFIED = "already_satisfied"
PLAN_REFUSED = "refused"

REASON_PRICE_MISSING = "take_profit_adjust_price_missing"
REASON_ALL_TIERS_CROSSED = "take_profit_adjust_all_tiers_crossed"
REASON_SIZE_BELOW_MINIMUM = "take_profit_adjust_size_below_minimum"
REASON_ALREADY_SATISFIED = "take_profit_adjust_already_satisfied"
REASON_ALLOCATION_INVALID = "take_profit_adjust_allocation_invalid"
REASON_TIER_COUNT_AMBIGUOUS = "take_profit_adjust_tier_count_ambiguous"
REASON_TIER_ALREADY_FILLED = "take_profit_adjust_tier_already_filled"
REASON_SIZE_INVALID = "take_profit_adjust_size_invalid"
REASON_POSITION_EMPTY = "take_profit_adjust_position_empty"
REASON_INPUT_INVALID = "take_profit_adjust_input_invalid"

#: The same hard boundary ``management_directives`` puts between the raw text
#: and the model's ``observed_text`` (U+2029). Each part is read on its own.
_TEXT_PART_BOUNDARY = " "

# Sentence and clause breaks. An ASCII comma between two digits is a thousands
# separator, not a clause break. "、" is deliberately not a break here: a
# take-profit list is written "84000、82000".
_CLAUSE_BREAK = re.compile(r"[\n\r。！!？?；;‼，]|(?<!\d),|,(?!\d)")

_NUM = r"(?<![\d.])(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d.])"
# A number followed by one of these is not a price: 30%, 600点, 2个, 3倍.
_NOT_PRICE_SUFFIX = re.compile(r"\s*(?:[%％点個个倍张u]|usdt)", re.IGNORECASE)
_LIST_SEPARATOR = r"\s*[/／、]\s*"
_ORDINAL = r"第\s*([一二三四五12345])\s*(?:个)?"

_TIER_WORD = r"(?:位|价|点位|点|目标位|目标)"
_P_LABEL = re.compile(
    rf"(?:{_ORDINAL})?止盈{_TIER_WORD}?\s*[:：]\s*{_NUM}((?:{_LIST_SEPARATOR}\d+(?:,\d{{3}})*(?:\.\d+)?)*)"
)
_P_MOVE = re.compile(
    rf"(?:{_ORDINAL})?止盈{_TIER_WORD}?\s*"
    r"(?:改到|改为|改成|改至|调到|调为|调至|调整到|调整至|调整为|移到|移至|"
    r"上移到|上移至|下移到|下移至|上调到|上调至|下调到|下调至|设在|设到|设为|"
    r"设置在|设置为|设置到|挂在)\s*"
    rf"{_NUM}((?:{_LIST_SEPARATOR}\d+(?:,\d{{3}})*(?:\.\d+)?)*)"
)
_P_ORDINAL = re.compile(
    rf"{_ORDINAL}止盈{_TIER_WORD}\s*(?:在|看|是|为)?\s*{_NUM}"
)
_P_REMAINING = re.compile(
    r"(?:剩下|剩余仓位|剩余持仓|剩余|其余仓位|其余)(?:的)?(?:仓位)?\s*"
    r"(?:看向|目标看向|目标看|目标位|目标|止盈看向|止盈看|止盈位)\s*"
    rf"{_NUM}"
)
_PCT = r"(\d+(?:\.\d+)?)\s*[%％]"
_P_NEAR = re.compile(
    rf"{_NUM}\s*(?:附近|位置|左右|一带)\s*(?:可以|就|可|先|再)?\s*"
    rf"(?:减仓止盈|止盈)\s*{_PCT}"
)
_PERCENT = re.compile(_PCT)
_STOP_LABEL = re.compile(
    rf"(?<!止盈)(?:止损位|止损价|止损|损位|保护价)\s*[:：]\s*{_NUM}"
)
_EACH_COUNT = re.compile(
    r"(两|二|三|四|五|2|3|4|5)\s*个\s*(?:止盈位|止盈点位|止盈目标|止盈|目标位|目标|档)"
)
_IMMEDIATE_PATTERN = re.compile(r"(?:获利|浮盈)\s*\d+(?:\.\d+)?\s*点")
_IMMEDIATE_TERMS = (
    "现价",
    "市价",
    "目前获利",
    "剩余仓位",
    "剩余持仓",
    "已到",
    "到了",
    "已经",
)
_EACH_TERMS = ("各", "每个", "每档", "分别")
_TIER_TERMS = ("止盈位", "止盈点位", "档", "目标")
_SETTING_TERMS = ("设置", "设好", "挂", "提前", "自动止盈")
_QUANTITY_VERBS = ("止盈", "减仓", "平仓", "平掉", "出掉", "出局", "减")
_CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
# A model action carrying any of these is an exit, a cancel or an add. It is
# never re-read as a take-profit plan, whatever the text around it says.
_VETO_ACTION_WORDS = (
    "exit",
    "close",
    "cancel",
    "add_position",
    "increase",
    "reverse",
    "open_position",
)
_POSITION_UPDATE_EVENTS = frozenset({"", "position_update"})


@dataclass(frozen=True, slots=True)
class TakeProfitInstruction:
    """What one message asks of the take profits, as read from its own text."""

    mode: str
    #: Explicit take-profit prices, in the order the message names them.
    prices: tuple[str, ...]
    #: Percentages as text. ``full_reset``: one per price. ``single_tier``:
    #: exactly one. ``ratios_only``: one per tier. ``prices_only``: empty.
    allocations: tuple[str, ...]
    #: ``第N止盈位`` when the message named the tier it changes.
    tier_index: int | None
    #: A stop written in label form in the same message (``止损位：78700``).
    stop_loss: str | None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "prices": list(self.prices),
            "allocations": list(self.allocations),
            "tier_index": self.tier_index,
            "stop_loss": self.stop_loss,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TakeProfitInstruction":
        mode = str(payload.get("mode") or "")
        if mode not in {
            MODE_FULL_RESET,
            MODE_SINGLE_TIER,
            MODE_RATIOS_ONLY,
            MODE_PRICES_ONLY,
        }:
            raise ValueError("take_profit_instruction_mode_invalid")
        tier_index = payload.get("tier_index")
        return cls(
            mode=mode,
            prices=tuple(str(value) for value in payload.get("prices") or ()),
            allocations=tuple(
                str(value) for value in payload.get("allocations") or ()
            ),
            tier_index=int(tier_index) if tier_index not in (None, "") else None,
            stop_loss=(
                str(payload["stop_loss"])
                if payload.get("stop_loss") not in (None, "")
                else None
            ),
            evidence=tuple(str(value) for value in payload.get("evidence") or ()),
        )


@dataclass(frozen=True, slots=True)
class _Allocation:
    percent: Decimal
    price: str | None
    each: bool
    count: int | None
    tier_index: int | None
    rule: str


def classify_take_profit_instruction(
    text: str | None,
    lifecycle_event: Mapping[str, Any] | None,
) -> TakeProfitInstruction | None:
    """Return the take-profit adjustment this message asks for, or ``None``."""

    event = lifecycle_event if isinstance(lifecycle_event, Mapping) else {}
    if event.get("_explicit_multi_target") is True:
        # "Only when the target is unique" (design 1.3, #17526): one text
        # addressed to several strategies would give each of them the same
        # price, and a BTC price is not an ETH take profit.
        return None
    event_type = str(event.get("event_type") or "").strip().lower()
    if event_type not in _POSITION_UPDATE_EVENTS:
        return None
    action = str(event.get("management_action") or "").strip().lower()
    if any(word in action for word in _VETO_ACTION_WORDS):
        return None
    for part in str(text or "").split(_TEXT_PART_BOUNDARY):
        instruction = _classify_part(part, event)
        if instruction is not None:
            return instruction
    return None


def _classify_part(
    part: str, event: Mapping[str, Any]
) -> TakeProfitInstruction | None:
    scrubbed = scrub_contact_identifiers(part)
    if "止盈" not in scrubbed and "目标" not in scrubbed:
        return None
    evidence: list[str] = []
    prices: list[str] = []
    tier_index: int | None = None

    def add_price(value: str | None) -> None:
        if value is not None and value not in prices:
            prices.append(value)

    for rule, pattern in (
        ("price_label", _P_LABEL),
        ("price_move", _P_MOVE),
    ):
        for match in pattern.finditer(scrubbed):
            first = _price_text(scrubbed, match, 2)
            if first is None:
                continue
            if rule not in evidence:
                evidence.append(rule)
            add_price(first)
            for extra in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", match.group(3) or ""):
                add_price(_canonical_price(extra))
            if match.group(1) and tier_index is None:
                tier_index = _ordinal(match.group(1))
    # "第一止盈位 60950" with no colon is how an entry message *describes* its
    # plan ("第一止盈位 60950 移动止损至成本价"). It supplies a price, but it is
    # not by itself evidence that the take profits should change now; it only
    # counts together with a label, a move verb or an allocation below.
    ordinal_prices: list[str] = []
    for match in _P_ORDINAL.finditer(scrubbed):
        value = _price_text(scrubbed, match, 2)
        if value is None:
            continue
        if value not in ordinal_prices:
            ordinal_prices.append(value)
        if tier_index is None:
            tier_index = _ordinal(match.group(1))
    for match in _P_REMAINING.finditer(scrubbed):
        value = _price_text(scrubbed, match, 1)
        if value is None:
            continue
        if "price_remaining_target" not in evidence:
            evidence.append("price_remaining_target")
        add_price(value)

    allocations: list[_Allocation] = []
    for clause in _CLAUSE_BREAK.split(scrubbed):
        if not _PERCENT.search(clause):
            continue
        near = _P_NEAR.search(clause)
        each = any(term in clause for term in _EACH_TERMS) and any(
            term in clause for term in _TIER_TERMS
        )
        setting = any(term in clause for term in _SETTING_TERMS)
        quantity = any(verb in clause for verb in _QUANTITY_VERBS)
        immediate = any(term in clause for term in _IMMEDIATE_TERMS) or bool(
            _IMMEDIATE_PATTERN.search(clause)
        )
        if not (near or each or setting):
            if quantity:
                # A percentage that says "close this much now". The message is
                # the reduction path's, entirely: splitting one message
                # between "close now" and "rearrange the take profits" is
                # exactly the ambiguity that must not be guessed at.
                return None
            continue
        if immediate:
            return None
        percent_text = near.group(2) if near else _PERCENT.search(clause).group(1)
        try:
            percent = Decimal(percent_text)
        except InvalidOperation:
            return None
        if not percent.is_finite() or not 0 < percent <= 100:
            return None
        clause_price: str | None = None
        clause_tier: int | None = None
        rule = "allocation_near_price" if near else (
            "allocation_each" if each else "allocation_setting"
        )
        if near:
            clause_price = _price_text(clause, near, 1)
        else:
            for pattern, group in ((_P_ORDINAL, 2), (_P_LABEL, 2), (_P_MOVE, 2)):
                found = pattern.search(clause)
                if found is not None:
                    clause_price = _price_text(clause, found, group)
                    if found.group(1):
                        clause_tier = _ordinal(found.group(1))
                    if clause_price is not None:
                        break
        count_match = _EACH_COUNT.search(clause)
        count = (
            _ordinal(count_match.group(1)) if count_match is not None else None
        )
        allocations.append(
            _Allocation(
                percent=percent,
                price=clause_price,
                each=each and not near,
                count=count,
                tier_index=clause_tier,
                rule=rule,
            )
        )
        if rule not in evidence:
            evidence.append(rule)
        if clause_price is not None:
            add_price(clause_price)
            if near and "price_near" not in evidence:
                evidence.append("price_near")

    if not prices and not allocations:
        return None
    for value in ordinal_prices:
        add_price(value)
    if any(value in ordinal_prices for value in prices) and "price_ordinal" not in evidence:
        evidence.append("price_ordinal")
    stop_loss = _label_stop(scrubbed) or _event_stop_in_text(event, part)
    return _instruction(
        prices=tuple(prices),
        allocations=allocations,
        tier_index=tier_index,
        stop_loss=stop_loss,
        evidence=tuple(evidence),
    )


def _instruction(
    *,
    prices: tuple[str, ...],
    allocations: list[_Allocation],
    tier_index: int | None,
    stop_loss: str | None,
    evidence: tuple[str, ...],
) -> TakeProfitInstruction:
    def build(mode, mode_prices, mode_allocations, mode_tier=None):
        return TakeProfitInstruction(
            mode=mode,
            prices=tuple(mode_prices),
            allocations=tuple(_decimal_text(value) for value in mode_allocations),
            tier_index=mode_tier,
            stop_loss=stop_loss,
            evidence=evidence,
        )

    if not allocations:
        return build(MODE_PRICES_ONLY, prices, (), tier_index)
    each_rows = [row for row in allocations if row.each]
    if each_rows:
        percent = each_rows[0].percent
        if any(row.percent != percent for row in allocations):
            # Two different shares in one message: report both and let the
            # planner refuse it rather than pick one.
            return build(
                MODE_FULL_RESET if prices else MODE_RATIOS_ONLY,
                prices,
                [row.percent for row in allocations],
            )
        count = next((row.count for row in each_rows if row.count), None)
        if count is None and prices:
            count = len(prices)
        if count is None:
            ratio = Decimal("100") / percent
            count = int(ratio) if ratio == ratio.to_integral_value() else None
        if count is None or not 1 <= count <= 5:
            return build(
                MODE_FULL_RESET if prices else MODE_RATIOS_ONLY,
                prices,
                [percent],
            )
        shares = [percent] * count
        if prices:
            return build(MODE_FULL_RESET, prices, shares)
        return build(MODE_RATIOS_ONLY, (), shares)
    if len(allocations) == 1:
        row = allocations[0]
        price = row.price or (prices[0] if len(prices) == 1 else None)
        if price is None:
            return build(MODE_RATIOS_ONLY, (), [row.percent])
        if row.percent == Decimal("100"):
            return build(MODE_FULL_RESET, (price,), [row.percent])
        return build(
            MODE_SINGLE_TIER,
            (price,),
            [row.percent],
            row.tier_index or tier_index,
        )
    if all(row.price is not None for row in allocations):
        return build(
            MODE_FULL_RESET,
            [row.price for row in allocations],
            [row.percent for row in allocations],
        )
    return build(
        MODE_RATIOS_ONLY if not prices else MODE_FULL_RESET,
        prices,
        [row.percent for row in allocations],
    )


def _price_text(text: str, match: re.Match[str], group: int) -> str | None:
    raw = match.group(group)
    if raw is None:
        return None
    if _NOT_PRICE_SUFFIX.match(text, match.end(group)):
        return None
    return _canonical_price(raw)


def _canonical_price(raw: str) -> str | None:
    try:
        value = Decimal(str(raw).replace(",", ""))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return _decimal_text(value)


def _ordinal(token: str) -> int | None:
    token = str(token or "").strip()
    if token.isdigit():
        return int(token)
    return _CHINESE_DIGITS.get(token)


def _label_stop(scrubbed: str) -> str | None:
    match = _STOP_LABEL.search(scrubbed)
    if match is None:
        return None
    return _price_text(scrubbed, match, 1)


def _event_stop_in_text(event: Mapping[str, Any], text: str) -> str | None:
    """A model-supplied stop, kept only when this message names it as a stop."""

    value = event.get("stop_loss")
    if value in (None, ""):
        return None
    canonical = _canonical_price(str(value))
    if canonical is None:
        return None
    from telegram_kol_research.management_directives import (
        _text_contains_explicit_stop_value,
    )

    return canonical if _text_contains_explicit_stop_value(text, canonical) else None


# --------------------------------------------------------------------------
# Planning


@dataclass(frozen=True, slots=True)
class ExistingTakeProfit:
    price: str
    size: str
    order_id: str


@dataclass(frozen=True, slots=True)
class TakeProfitAdjustmentPlan:
    status: str
    reason_code: str | None = None
    #: ``(price, size)`` per tier, nearest first.
    targets: tuple[tuple[str, str], ...] = ()
    #: Share of each target, summing to exactly 100, in the same order. This
    #: is what the take-profit convergence plan is rewritten to (R4).
    allocations: tuple[str, ...] = ()
    keep_order_ids: tuple[str, ...] = ()
    cancel_order_ids: tuple[str, ...] = ()
    place: tuple[tuple[str, str], ...] = ()
    dropped_crossed: tuple[str, ...] = ()
    dropped_filled: tuple[str, ...] = ()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "targets": [list(item) for item in self.targets],
            "allocations": list(self.allocations),
            "keep_order_ids": list(self.keep_order_ids),
            "cancel_order_ids": list(self.cancel_order_ids),
            "place": [list(item) for item in self.place],
            "dropped_crossed": list(self.dropped_crossed),
            "dropped_filled": list(self.dropped_filled),
        }


def plan_take_profit_adjustment(
    *,
    instruction: TakeProfitInstruction,
    side: str,
    remaining_size: Any,
    last_price: Any,
    existing_take_profits: Sequence[ExistingTakeProfit | tuple[Any, Any, Any]],
    filled_prices: Sequence[Any] = (),
    strategy_take_profit_prices: Sequence[Any] = (),
    min_quantity: Any,
    quantity_step: Any,
) -> TakeProfitAdjustmentPlan:
    """Rebuild one position's take profits for what the message asked."""

    normalized_side = str(side or "").strip().lower()
    remaining = _decimal(remaining_size)
    last = _decimal(last_price)
    minimum = _decimal(min_quantity)
    step = _decimal(quantity_step)
    if (
        normalized_side not in {"long", "short"}
        or last is None
        or last <= 0
        or minimum is None
        or minimum <= 0
        or step is None
        or step <= 0
    ):
        return _refused(REASON_INPUT_INVALID)
    if remaining is None or remaining <= 0:
        return _refused(REASON_POSITION_EMPTY)
    try:
        existing = _existing(existing_take_profits)
    except ValueError:
        return _refused(REASON_INPUT_INVALID)
    filled = {
        value for value in (_decimal(item) for item in filled_prices)
        if value is not None
    }

    if instruction.mode == MODE_SINGLE_TIER:
        return _plan_single_tier(
            instruction=instruction,
            side=normalized_side,
            remaining=remaining,
            last=last,
            existing=existing,
            filled=filled,
            minimum=minimum,
            step=step,
        )

    try:
        prices, shares = _prices_and_shares(
            instruction=instruction,
            existing=existing,
            filled=filled,
            strategy_prices=strategy_take_profit_prices,
        )
    except _Refusal as refusal:
        return _refused(refusal.reason_code)

    dropped_filled = tuple(
        _decimal_text(price) for price in prices if price in filled
    )
    tiers = [
        (price, share)
        for price, share in zip(prices, shares)
        if price not in filled
    ]
    if not tiers:
        return _refused(REASON_TIER_ALREADY_FILLED, dropped_filled=dropped_filled)
    tiers.sort(key=lambda item: item[0], reverse=normalized_side == "short")
    dropped_crossed = tuple(
        _decimal_text(price)
        for price, _ in tiers
        if _crossed(price, side=normalized_side, last=last)
    )
    tiers = [
        (price, share)
        for price, share in tiers
        if not _crossed(price, side=normalized_side, last=last)
    ]
    if not tiers:
        return _refused(
            REASON_ALL_TIERS_CROSSED,
            dropped_crossed=dropped_crossed,
            dropped_filled=dropped_filled,
        )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        _allocate_sizes,
        _rescale_allocations,
    )

    rescaled = _rescale_allocations([share for _, share in tiers])
    if rescaled is None:
        return _refused(REASON_ALLOCATION_INVALID)
    sizes = _allocate_sizes(
        remaining,
        rescaled,
        quantity_step=step,
        minimum_quantity=minimum,
    )
    if isinstance(sizes, str):
        return _refused(
            REASON_SIZE_BELOW_MINIMUM
            if sizes == "convergence_target_size_below_minimum"
            else REASON_SIZE_INVALID,
            dropped_crossed=dropped_crossed,
            dropped_filled=dropped_filled,
        )
    targets = [
        (price, Decimal(str(size)))
        for (price, _), size in zip(tiers, sizes)
        if Decimal(str(size)) > 0
    ]
    return _finish(
        targets=targets,
        existing=existing,
        dropped_crossed=dropped_crossed,
        dropped_filled=dropped_filled,
    )


class _Refusal(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _prices_and_shares(
    *,
    instruction: TakeProfitInstruction,
    existing: list[tuple[Decimal, Decimal, str]],
    filled: set[Decimal],
    strategy_prices: Sequence[Any],
) -> tuple[list[Decimal], list[Decimal]]:
    from telegram_kol_research.take_profit_plan import _allocations as default_table

    prices = [_decimal(value) for value in instruction.prices]
    if any(value is None or value <= 0 for value in prices):
        raise _Refusal(REASON_PRICE_MISSING)
    shares = [_decimal(value) for value in instruction.allocations]
    if any(value is None or value <= 0 for value in shares):
        raise _Refusal(REASON_ALLOCATION_INVALID)
    if len(set(prices)) != len(prices):
        raise _Refusal(REASON_ALLOCATION_INVALID)

    if instruction.mode == MODE_PRICES_ONLY:
        if not prices:
            raise _Refusal(REASON_PRICE_MISSING)
        live_prices = [price for price in prices if price not in filled]
        if existing and len(live_prices) == len(existing):
            # Same number of tiers: keep our existing split (user ruling Q2).
            return prices, _shares_for_rank(prices, existing, filled)
        # The tier count changed: the entry-time default table (Q2). Filled
        # prices are removed by the caller, which rescales what is left.
        return live_prices + [
            price for price in prices if price in filled
        ], list(default_table(None, len(live_prices))) + [
            Decimal("1") for price in prices if price in filled
        ]

    if instruction.mode == MODE_RATIOS_ONLY:
        total = sum(shares, Decimal("0"))
        if total != Decimal("100"):
            # "至少50%自动止盈" with no price (#18294): which tier, at what
            # price, is exactly what the message did not say.
            raise _Refusal(REASON_PRICE_MISSING)
        strategy = [
            value
            for value in (_decimal(item) for item in strategy_prices)
            if value is not None and value > 0
        ]
        strategy = list(dict.fromkeys(strategy))
        if not strategy:
            raise _Refusal(REASON_PRICE_MISSING)
        unfilled = [value for value in strategy if value not in filled]
        if len(unfilled) == len(shares):
            return unfilled, shares
        if len(strategy) == len(shares):
            return strategy, shares
        raise _Refusal(REASON_TIER_COUNT_AMBIGUOUS)

    # MODE_FULL_RESET
    if not prices:
        raise _Refusal(REASON_PRICE_MISSING)
    if len(shares) != len(prices) or sum(shares, Decimal("0")) != Decimal("100"):
        raise _Refusal(REASON_ALLOCATION_INVALID)
    return prices, shares


def _shares_for_rank(
    prices: list[Decimal],
    existing: list[tuple[Decimal, Decimal, str]],
    filled: set[Decimal],
) -> list[Decimal]:
    """Existing sizes as shares, handed to the new prices by distance rank.

    Both sides are ranked by ascending price here. For a short the
    nearest-first order is the reverse of both, so the pairing is the same:
    the nearest new tier inherits the nearest old tier's size. Filled prices
    get a placeholder share; the caller removes them before sizing.
    """

    ranked_existing = [row[1] for row in sorted(existing, key=lambda row: row[0])]
    live_prices = sorted(price for price in prices if price not in filled)
    by_price = dict(zip(live_prices, ranked_existing))
    return [by_price.get(price, Decimal("1")) for price in prices]


def _plan_single_tier(
    *,
    instruction: TakeProfitInstruction,
    side: str,
    remaining: Decimal,
    last: Decimal,
    existing: list[tuple[Decimal, Decimal, str]],
    filled: set[Decimal],
    minimum: Decimal,
    step: Decimal,
) -> TakeProfitAdjustmentPlan:
    if len(instruction.prices) != 1 or len(instruction.allocations) != 1:
        return _refused(REASON_ALLOCATION_INVALID)
    price = _decimal(instruction.prices[0])
    share = _decimal(instruction.allocations[0])
    if price is None or price <= 0:
        return _refused(REASON_PRICE_MISSING)
    if share is None or not 0 < share < 100:
        return _refused(REASON_ALLOCATION_INVALID)
    if price in filled:
        return _refused(
            REASON_TIER_ALREADY_FILLED, dropped_filled=(_decimal_text(price),)
        )
    if _crossed(price, side=side, last=last):
        return _refused(
            REASON_ALL_TIERS_CROSSED, dropped_crossed=(_decimal_text(price),)
        )
    size = _round_down(remaining * share / Decimal("100"), step)
    if size < minimum:
        return _refused(REASON_SIZE_BELOW_MINIMUM)

    nearest_first = sorted(existing, key=lambda row: row[0], reverse=side == "short")
    replaced: int | None = next(
        (index for index, row in enumerate(nearest_first) if row[0] == price),
        None,
    )
    if (
        replaced is None
        and instruction.tier_index is not None
        and 1 <= instruction.tier_index <= len(nearest_first)
    ):
        replaced = instruction.tier_index - 1
    others = [
        [row[0], row[1]]
        for index, row in enumerate(nearest_first)
        if index != replaced
    ]
    excess = sum((row[1] for row in others), Decimal("0")) + size - remaining
    # Trim the farthest of the other tiers first: the tier the message named
    # is the one thing here the KOL actually asked for.
    for row in sorted(others, key=lambda item: item[0], reverse=side == "long"):
        if excess <= 0:
            break
        cut = min(excess, row[1])
        row[1] -= cut
        excess -= cut
        if 0 < row[1] < minimum:
            excess -= row[1]
            row[1] = Decimal("0")
    if excess > 0:
        return _refused(REASON_SIZE_INVALID)
    targets = [(row[0], row[1]) for row in others if row[1] > 0]
    targets.append((price, size))
    targets.sort(key=lambda item: item[0], reverse=side == "short")
    return _finish(
        targets=targets, existing=existing, dropped_crossed=(), dropped_filled=()
    )


def _finish(
    *,
    targets: list[tuple[Decimal, Decimal]],
    existing: list[tuple[Decimal, Decimal, str]],
    dropped_crossed: tuple[str, ...],
    dropped_filled: tuple[str, ...],
) -> TakeProfitAdjustmentPlan:
    if not targets:
        return _refused(REASON_SIZE_BELOW_MINIMUM)
    unmatched_existing = list(existing)
    keep: list[str] = []
    place: list[tuple[str, str]] = []
    for price, size in targets:
        match = next(
            (
                row
                for row in unmatched_existing
                if row[0] == price and row[1] == size
            ),
            None,
        )
        if match is not None:
            unmatched_existing.remove(match)
            keep.append(match[2])
        else:
            place.append((_decimal_text(price), _decimal_text(size)))
    cancel = tuple(row[2] for row in unmatched_existing)
    status = PLAN_READY if (cancel or place) else PLAN_ALREADY_SATISFIED
    return TakeProfitAdjustmentPlan(
        status=status,
        reason_code=None if status == PLAN_READY else REASON_ALREADY_SATISFIED,
        targets=tuple(
            (_decimal_text(price), _decimal_text(size)) for price, size in targets
        ),
        allocations=proportional_allocations([size for _, size in targets]),
        keep_order_ids=tuple(keep),
        cancel_order_ids=cancel,
        place=tuple(place),
        dropped_crossed=dropped_crossed,
        dropped_filled=dropped_filled,
    )


def proportional_allocations(sizes: Sequence[Any]) -> tuple[str, ...]:
    """Shares summing to exactly 100 that reproduce ``sizes`` when re-sized.

    Every share but the last is rounded *up* to six places and the last takes
    the remainder. The convergence sizer floors each tier and gives the rest
    to the last one, so rounding up means each floor lands on the size it came
    from (for any position under a million contracts) instead of one step
    short of it.
    """

    values = [Decimal(str(value)) for value in sizes]
    total = sum(values, Decimal("0"))
    if not values or total <= 0:
        return ()
    quantum = Decimal("0.000001")
    shares = [
        (value * Decimal("100") / total).quantize(quantum, rounding=ROUND_CEILING)
        for value in values[:-1]
    ]
    last = Decimal("100") - sum(shares, Decimal("0"))
    if last <= 0:
        return ()
    shares.append(last)
    return tuple(_decimal_text(value) for value in shares)


def _existing(
    rows: Sequence[ExistingTakeProfit | tuple[Any, Any, Any]],
) -> list[tuple[Decimal, Decimal, str]]:
    result = []
    for row in rows:
        if isinstance(row, ExistingTakeProfit):
            price, size, order_id = row.price, row.size, row.order_id
        else:
            price, size, order_id = row
        parsed_price = _decimal(price)
        parsed_size = _decimal(size)
        if (
            parsed_price is None
            or parsed_price <= 0
            or parsed_size is None
            or parsed_size <= 0
            or not str(order_id or "").strip()
        ):
            raise ValueError("existing_take_profit_invalid")
        result.append((parsed_price, parsed_size, str(order_id)))
    return result


def _crossed(price: Decimal, *, side: str, last: Decimal) -> bool:
    return price <= last if side == "long" else price >= last


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _refused(
    reason_code: str,
    *,
    dropped_crossed: tuple[str, ...] = (),
    dropped_filled: tuple[str, ...] = (),
) -> TakeProfitAdjustmentPlan:
    return TakeProfitAdjustmentPlan(
        status=PLAN_REFUSED,
        reason_code=reason_code,
        dropped_crossed=dropped_crossed,
        dropped_filled=dropped_filled,
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return text


# --------------------------------------------------------------------------
# Entry legs of the same strategy that have not filled yet


@dataclass(frozen=True, slots=True)
class UnfilledTakeProfitPlan:
    """The share plan an entry leg that has not filled should carry.

    An unfilled leg has no position and no size yet; its take-profit plan is
    a list of ``(price, share-of-position %)`` that the convergence worker
    turns into order sizes once the leg fills. Shares sum to exactly 100.
    """

    status: str
    reason_code: str | None = None
    targets: tuple[tuple[str, str], ...] = ()
    dropped_crossed: tuple[str, ...] = ()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "targets": [list(item) for item in self.targets],
            "dropped_crossed": list(self.dropped_crossed),
        }


def plan_unfilled_take_profit_allocations(
    *,
    instruction: TakeProfitInstruction,
    side: str,
    last_price: Any,
    current_plan: Sequence[tuple[Any, Any]],
    strategy_take_profit_prices: Sequence[Any] = (),
) -> UnfilledTakeProfitPlan:
    """New ``(price, %)`` plan for an unfilled entry leg of the same strategy.

    Same reading of the instruction as :func:`plan_take_profit_adjustment`,
    in shares instead of contracts:

    * full reset / ratios only / prices only: the prices and shares the
      instruction gives (prices only keeps this leg's own split when the tier
      count is unchanged, else the entry-time default table);
    * single tier: that tier is replaced (same price, else the named ``第N``
      tier) or added in this leg's current plan at the named share, and the
      other tiers are scaled so the plan still totals 100;
    * tiers the market has already crossed are dropped and their share goes
      to the rest, exactly as for a filled position.
    """

    normalized_side = str(side or "").strip().lower()
    last = _decimal(last_price)
    if normalized_side not in {"long", "short"} or last is None or last <= 0:
        return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_INPUT_INVALID)
    current: list[tuple[Decimal, Decimal]] = []
    for price, share in current_plan:
        parsed_price, parsed_share = _decimal(price), _decimal(share)
        if (
            parsed_price is None
            or parsed_price <= 0
            or parsed_share is None
            or parsed_share <= 0
        ):
            return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_INPUT_INVALID)
        current.append((parsed_price, parsed_share))

    if instruction.mode == MODE_SINGLE_TIER:
        if len(instruction.prices) != 1 or len(instruction.allocations) != 1:
            return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_ALLOCATION_INVALID)
        price = _decimal(instruction.prices[0])
        share = _decimal(instruction.allocations[0])
        if price is None or price <= 0:
            return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_PRICE_MISSING)
        if share is None or not 0 < share < 100:
            return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_ALLOCATION_INVALID)
        if _crossed(price, side=normalized_side, last=last):
            return UnfilledTakeProfitPlan(
                PLAN_REFUSED,
                REASON_ALL_TIERS_CROSSED,
                dropped_crossed=(_decimal_text(price),),
            )
        nearest_first = sorted(
            current, key=lambda row: row[0], reverse=normalized_side == "short"
        )
        replaced = next(
            (index for index, row in enumerate(nearest_first) if row[0] == price),
            None,
        )
        if (
            replaced is None
            and instruction.tier_index is not None
            and 1 <= instruction.tier_index <= len(nearest_first)
        ):
            replaced = instruction.tier_index - 1
        others = [
            row for index, row in enumerate(nearest_first) if index != replaced
        ]
        if not others:
            tiers = [(price, Decimal("100"))]
        else:
            rest = Decimal("100") - share
            scaled = _scaled_to(
                [row[1] for row in others], total=rest
            )
            if scaled is None:
                return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_ALLOCATION_INVALID)
            tiers = [(row[0], value) for row, value in zip(others, scaled)]
            tiers.append((price, share))
        tiers.sort(key=lambda item: item[0], reverse=normalized_side == "short")
        return UnfilledTakeProfitPlan(
            PLAN_READY,
            targets=tuple(
                (_decimal_text(price), _decimal_text(value)) for price, value in tiers
            ),
        )

    pseudo_existing = [
        (price, share, f"plan-{index}") for index, (price, share) in enumerate(current)
    ]
    try:
        prices, shares = _prices_and_shares(
            instruction=instruction,
            existing=pseudo_existing,
            filled=set(),
            strategy_prices=strategy_take_profit_prices,
        )
    except _Refusal as refusal:
        return UnfilledTakeProfitPlan(PLAN_REFUSED, refusal.reason_code)
    tiers = sorted(
        zip(prices, shares), key=lambda item: item[0], reverse=normalized_side == "short"
    )
    dropped_crossed = tuple(
        _decimal_text(price)
        for price, _ in tiers
        if _crossed(price, side=normalized_side, last=last)
    )
    tiers = [
        (price, share)
        for price, share in tiers
        if not _crossed(price, side=normalized_side, last=last)
    ]
    if not tiers:
        return UnfilledTakeProfitPlan(
            PLAN_REFUSED, REASON_ALL_TIERS_CROSSED, dropped_crossed=dropped_crossed
        )
    scaled = _scaled_to([share for _, share in tiers], total=Decimal("100"))
    if scaled is None:
        return UnfilledTakeProfitPlan(PLAN_REFUSED, REASON_ALLOCATION_INVALID)
    return UnfilledTakeProfitPlan(
        PLAN_READY,
        targets=tuple(
            (_decimal_text(price), _decimal_text(value))
            for (price, _), value in zip(tiers, scaled)
        ),
        dropped_crossed=dropped_crossed,
    )


def _scaled_to(values: Sequence[Decimal], *, total: Decimal) -> list[Decimal] | None:
    """Scale positive shares to sum to exactly ``total`` (six places, remainder last)."""

    current = sum(values, Decimal("0"))
    if not values or current <= 0 or total <= 0:
        return None
    quantum = Decimal("0.000001")
    scaled = [
        (value * total / current).quantize(quantum) for value in values[:-1]
    ]
    last = total - sum(scaled, Decimal("0"))
    if last <= 0 or any(value <= 0 for value in scaled):
        return None
    scaled.append(last)
    return scaled
