"""Deterministic policy for authoritative position-management instructions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.contact_digit_scrubbing import scrub_contact_identifiers
from telegram_kol_research.strategy_management_contracts import (
    COMPOSITE_MANAGEMENT_CONTRACT_VERSION,
    ManagementInstructionContract,
)


class ManagementFractionInvalid(ValueError):
    """A supplied value is invalid, not absent; it must never select a default."""

    reason_code = "management_fraction_invalid"

    def __init__(self, classification="invalid_format", source="fraction"):
        super().__init__(self.reason_code)
        self.classification = classification
        self.source = source


DEFAULT_PARTIAL_CLOSE_FRACTION = 0.50
DEFAULT_TAIL_CLOSE_FRACTION = 0.80
FULL_EXIT_ACTIONS = frozenset({"exit_full", "full_exit", "close_position"})


@dataclass(frozen=True, slots=True)
class MultiTargetActionPolicy:
    action: str
    risk_reducing: bool
    fanout_allowed: bool
    requires_fraction: bool = False


_MULTI_TARGET_ACTIONS = {
    "partial_take_profit": MultiTargetActionPolicy(
        "partial_take_profit", True, True, True
    ),
    "exit_full": MultiTargetActionPolicy("exit_full", True, True),
    "exit_partial": MultiTargetActionPolicy(
        "exit_partial", True, True, True
    ),
    "cancel_pending_entry": MultiTargetActionPolicy(
        "cancel_pending_entry", True, True
    ),
}
MULTI_TARGET_ACTION_NAMES = frozenset(_MULTI_TARGET_ACTIONS)
# Actions graduate to live independently after shadow parity.  The remaining
# closed actions stay recognizable and projectable, but cannot execute yet.
MULTI_TARGET_LIVE_ACTION_NAMES = frozenset({"partial_take_profit"})
_MULTI_TARGET_ACTION_ALIASES = {
    "full_exit": "exit_full",
    "close_position": "exit_full",
    "cancel_entry": "cancel_pending_entry",
}


def multi_target_action_policy(action: str | None) -> MultiTargetActionPolicy:
    """Return the closed fanout policy shared by parsing and persistence."""

    normalized = str(action or "").strip().lower()
    canonical = _MULTI_TARGET_ACTION_ALIASES.get(normalized, normalized)
    return _MULTI_TARGET_ACTIONS.get(
        canonical,
        MultiTargetActionPolicy(
            action=canonical or "none",
            risk_reducing=False,
            fanout_allowed=False,
        ),
    )

_PARTIAL_TERMS = (
    "第一止盈",
    "第一个止盈",
    "首个止盈",
    "止盈一部分",
    "部分止盈",
    "分批止盈",
    "提前止盈",
    "减仓",
    "减半",
    "平加仓",
    "移动止盈",
)
_TAIL_TERMS = ("只留一点尾仓", "只留尾仓", "留一点尾仓", "保留底仓", "留底仓")
_BREAK_EVEN_TERMS = (
    "移动止损",
    "止损移动到开仓价",
    "移动止损至开仓价",
    "移动止损到开仓价",
    "止损到成本",
    "止损好成本",
    "成本保护",
    "保护成本",
    "保本止损",
    "带保护",
    "推保护",
    "上推保护",
    "保护价",
    "保护止损",
    # M1-c (2026-09-29 Mia design): "做无风险持仓" / "做无风险" is Mia's
    # standard closing phrase for a partial-take-profit-plus-protection
    # message ("止盈40%，剩余仓位止损位上移至80600，做无风险持仓"). It only
    # matters when there is no explicit move price to attach (M1-a below
    # supplies the price when one is written); this term makes the intent
    # a protection intent so the strategy price is used as a fallback.
    "无风险持仓",
    "做无风险",
)
#: M1-a/d (2026-09-29 Mia design): "止损位上移至 P" / "剩余仓位上移至 P" /
#: "止损移动至 P" / "止损下移至 P". Anchored on an explicit stop term, or on
#: "剩余仓位/剩余持仓" *without* an intervening "止盈" (so "剩余仓位止盈上移至
#: 2710" -- a take-profit move -- is never read as a stop move). Requires the
#: movement verb, so it does not touch the R3 "止盈止损\n止盈位：73070" defect
#: in ``message_recognition._extract_explicit_stop_loss_from_management_text``
#: pattern 1 (that pattern has no movement-verb requirement at all).
_MANAGEMENT_STOP_MOVE_RE = re.compile(
    r"(?:止损(?:位)?|保护价)[^0-9]{0,20}?(?:上移|下移|移动|移至|挪动|调整)"
    r"[^0-9]{0,10}?(?:至|到)?[^0-9]{0,6}?([0-9]+(?:\.\d+)?)"
    r"|(?:剩余(?:仓位|持仓))(?:(?!止盈)[^0-9]){0,20}?(?:上移|下移|移动|移至|挪动|调整)"
    r"[^0-9]{0,10}?(?:至|到)?[^0-9]{0,6}?([0-9]+(?:\.\d+)?)"
)


def _management_stop_move_price(text: str) -> float | None:
    """The single unambiguous "move stop to P" price named in this text.

    Returns ``None`` when nothing matches, or when two matches disagree --
    ambiguous content must never silently pick one price over another.
    """

    scrubbed = scrub_contact_identifiers(str(text or ""))
    values: list[float] = []
    for match in _MANAGEMENT_STOP_MOVE_RE.finditer(scrubbed):
        token = match.group(1) or match.group(2)
        if token is None:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if value not in values:
            values.append(value)
    if len(values) != 1:
        return None
    return values[0]
_FULL_EXIT_TERMS = (
    "全部止盈出局",
    "全部平仓",
    "全部平",
    "全平",
    "全部出局",
    "清仓",
    "止损出局",
    "止盈出局",
    "出局吧",
    # M8 (2026-09-29 Mia design): "保本出局" / "先保本出局" (#19383, #17821).
    # Only fires when the model already gave a non-full-exit label -- this
    # branch only runs when the earlier explicit exit_type checks did not
    # already match -- and is still excluded by the existing "剩余仓位" etc.
    # guard just below, so "剩余仓位保本出局" is unaffected.
    "保本出局",
    "先保本出局",
)
_CANCEL_ENTRY_TERMS = (
    "策略先取消",
    "取消策略",
    "撤销入场",
    "取消入场",
    "取消挂单",
    "撤销挂单",
)
_RISK_INCREASING_TERMS = ("加仓", "补仓", "再做一次", "重新进场", "反手")


@dataclass(frozen=True, slots=True)
class ManagementDirective:
    intent: str
    fraction: float | None
    symbol: str | None
    side: str | None
    stop_loss: str | None
    risk_reducing: bool
    fanout_allowed: bool
    cancel_deferred_entries: bool
    reason_code: str
    strategy_thread_id: int | None = None
    stop_price_source: str | None = None


def resolve_management_directive(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementDirective:
    """Convert one authoritative lifecycle event into deterministic policy."""

    validate_management_fraction_inputs(lifecycle_event, str(text or ""))
    normalized_text = str(text or "").strip().lower()
    event_type = str(lifecycle_event.get("event_type") or "").strip().lower()
    raw_action = str(lifecycle_event.get("management_action") or "").strip().lower()
    combined = " ".join((normalized_text, raw_action))
    symbol = _normalized_optional(lifecycle_event.get("symbol"), upper=True)
    side = _normalized_optional(lifecycle_event.get("side"), upper=False)
    stop_loss = _normalized_optional(lifecycle_event.get("stop_loss"), upper=False)
    strategy_thread_id = _positive_int_or_none(
        lifecycle_event.get("strategy_thread_id")
    )
    current_message_stop = (
        stop_loss
        if stop_loss is not None
        and _text_contains_explicit_stop_value(normalized_text, stop_loss)
        else None
    )
    current_message_stop_source = (
        "current_message_text" if current_message_stop is not None else None
    )
    has_partial = _has_partial_clause(combined, raw_action)
    has_break_even = _has_break_even_clause(combined, raw_action)
    if has_partial and current_message_stop is None:
        # M1-a (2026-09-29 Mia design): the model usually tags this class of
        # message as a plain partial_take_profit with an empty stop_loss
        # field. Read the move price straight from this message's own text
        # (never the payload) so "止盈40%，剩余仓位止损位上移至80600" still
        # becomes a composite partial_then_break_even instead of silently
        # dropping the stop move.
        text_move_price = _management_stop_move_price(str(text or ""))
        if text_move_price is not None:
            current_message_stop = text_move_price
            current_message_stop_source = "current_message_text"
    has_protection = has_break_even or current_message_stop is not None

    if any(term in combined for term in _CANCEL_ENTRY_TERMS) or event_type == "cancel_entry":
        return ManagementDirective(
            intent="cancel_entry",
            fraction=None,
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            risk_reducing=True,
            fanout_allowed=False,
            cancel_deferred_entries=True,
            reason_code="explicit_cancel_entry",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    risk_increasing = raw_action in {
        "add_position", "increase_position", "open_position", "reverse_position",
    } or any(
        # "平加仓" and the narrative "加仓后" (#19597 "加仓后浮盈600点")
        # describe past adds, not a new one. Only that exact form is
        # stripped; "可以加仓" / "加仓了" / "补仓" still count.
        term in combined.replace("平加仓", "").replace("加仓后", "")
        for term in _RISK_INCREASING_TERMS
    )
    if risk_increasing:
        return ManagementDirective(
            intent=raw_action or "risk_increasing",
            fraction=None,
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            risk_reducing=False,
            fanout_allowed=False,
            cancel_deferred_entries=False,
            reason_code="risk_increasing_fanout_forbidden",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if raw_action == "exit_partial":
        fraction = _management_fraction(lifecycle_event, combined)
        if fraction is None:
            return ManagementDirective(
                intent="partial_take_profit",
                fraction=None,
                symbol=symbol,
                side=side,
                stop_loss=stop_loss,
                risk_reducing=False,
                fanout_allowed=False,
                cancel_deferred_entries=False,
                reason_code="partial_exit_fraction_required",
                strategy_thread_id=strategy_thread_id,
                stop_price_source=current_message_stop_source,
            )
        return _directive(
            "partial_take_profit",
            fraction=fraction,
            symbol=symbol,
            side=side,
            reason_code="explicit_partial_exit",
            strategy_thread_id=strategy_thread_id,
        )

    if event_type in {
        "exit_position", "exit_full", "full_exit", "close_position",
    } or raw_action in FULL_EXIT_ACTIONS or (
        any(term in combined for term in _FULL_EXIT_TERMS)
        and not any(
            term in combined
            for term in ("剩余仓位", "剩余持仓", "其余仓位", "剩下仓位")
        )
    ):
        return _directive(
            "full_exit",
            symbol=symbol,
            side=side,
            reason_code="explicit_full_exit",
            strategy_thread_id=strategy_thread_id,
        )

    if (
        raw_action in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"}
        and not has_partial
    ):
        if stop_loss is None:
            return ManagementDirective(
                intent="adjust_stop_loss",
                fraction=None,
                symbol=symbol,
                side=side,
                stop_loss=None,
                risk_reducing=False,
                fanout_allowed=False,
                cancel_deferred_entries=False,
                reason_code="stop_adjustment_direction_not_verified",
                strategy_thread_id=strategy_thread_id,
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if (
        event_type == "position_update"
        and current_message_stop is not None
        and not has_partial
    ):
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=current_message_stop,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
            stop_price_source=current_message_stop_source,
        )

    if any(term in combined for term in _TAIL_TERMS):
        # M3 (2026-09-29 Mia design): an explicit percentage in this message
        # ("止盈70%保留底仓做成本保护") names the close fraction the KOL
        # actually meant; only fall back to the 0.8 default when the message
        # gives no number at all ("只保留底仓"). `_management_fraction`
        # already tells "平掉 X%" (close) apart from "保留 X%" (retained,
        # inverted) and raises `management_fraction_ambiguous` on conflict.
        explicit_fraction = _management_fraction(lifecycle_event, combined)
        tail_intent = (
            "partial_then_break_even" if has_protection else "partial_take_profit"
        )
        return _directive(
            tail_intent,
            fraction=(
                DEFAULT_TAIL_CLOSE_FRACTION
                if explicit_fraction is None
                else explicit_fraction
            ),
            symbol=symbol,
            side=side,
            reason_code=(
                "tail_retention_explicit_percentage"
                if explicit_fraction is not None
                else "tail_retention_preferred_over_optional_exit"
                if any(term in combined for term in _FULL_EXIT_TERMS)
                or "出局" in combined
                else "tail_retention"
            ),
            strategy_thread_id=strategy_thread_id,
            stop_loss=current_message_stop if has_protection else None,
            stop_price_source=(
                current_message_stop_source if has_protection else None
            ),
        )

    if has_partial:
        fraction = _management_fraction(lifecycle_event, combined)
        intent = "partial_then_break_even" if has_protection else "partial_take_profit"
        return _directive(
            intent,
            fraction=(
                DEFAULT_PARTIAL_CLOSE_FRACTION if fraction is None else fraction
            ),
            symbol=symbol,
            side=side,
            reason_code=(
                "partial_then_break_even"
                if has_protection
                else "partial_risk_reduction"
            ),
            strategy_thread_id=strategy_thread_id,
            stop_loss=current_message_stop if has_protection else None,
            stop_price_source=(
                current_message_stop_source if has_protection else None
            ),
        )

    if has_break_even:
        return _directive(
            "move_stop_to_break_even",
            symbol=symbol,
            side=side,
            reason_code="break_even_protection",
            strategy_thread_id=strategy_thread_id,
            stop_loss=current_message_stop,
            stop_price_source=current_message_stop_source,
        )

    if raw_action in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"} or (
        stop_loss is not None and event_type == "position_update"
    ):
        if stop_loss is None:
            return ManagementDirective(
                intent="adjust_stop_loss",
                fraction=None,
                symbol=symbol,
                side=side,
                stop_loss=None,
                risk_reducing=False,
                fanout_allowed=False,
                cancel_deferred_entries=False,
                reason_code="stop_adjustment_direction_not_verified",
                strategy_thread_id=strategy_thread_id,
            )
        return _directive(
            "adjust_stop_loss",
            symbol=symbol,
            side=side,
            stop_loss=stop_loss,
            reason_code="explicit_stop_adjustment_requires_position_validation",
            strategy_thread_id=strategy_thread_id,
        )

    return ManagementDirective(
        intent="none",
        fraction=None,
        symbol=symbol,
        side=side,
        stop_loss=stop_loss,
        risk_reducing=False,
        fanout_allowed=False,
        cancel_deferred_entries=False,
        reason_code="no_actionable_risk_reduction",
        strategy_thread_id=strategy_thread_id,
    )


def build_management_instruction_contract(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementInstructionContract:
    """Build the complete immutable contract for one composite instruction."""

    directive = resolve_management_directive(
        text=text,
        lifecycle_event=lifecycle_event,
    )
    if directive.intent != "partial_then_break_even" or directive.fraction is None:
        raise ValueError("management_instruction_is_not_composite")
    if directive.stop_loss is not None:
        stop_mode = "explicit_price"
        stop_price = directive.stop_loss
        stop_price_source = directive.stop_price_source
    else:
        stop_mode = "actual_entry_price"
        stop_price = None
        stop_price_source = None
    return ManagementInstructionContract(
        version=COMPOSITE_MANAGEMENT_CONTRACT_VERSION,
        target_lifecycle_id=_positive_int_or_none(
            lifecycle_event.get("target_lifecycle_id")
        ),
        strategy_instance_id=(
            str(lifecycle_event.get("strategy_instance_id")).strip()
            if lifecycle_event.get("strategy_instance_id") not in (None, "")
            else None
        ),
        symbol=directive.symbol,
        side=directive.side,
        close_fraction=str(directive.fraction),
        stop_mode=stop_mode,
        stop_price=stop_price,
        stop_price_source=stop_price_source,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=directive.cancel_deferred_entries,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text=str(text or "").strip(),
    )


def _has_partial_clause(combined: str, raw_action: str) -> bool:
    return (
        "partial_take_profit" in raw_action
        or any(term in combined for term in _PARTIAL_TERMS)
        or bool(_close_percentage_values(combined))
        or bool(_retained_percentage_values(combined))
        or "一半" in combined
        or "半仓" in combined
    )


def _has_break_even_clause(combined: str, raw_action: str) -> bool:
    return (
        any(
            term in raw_action
            for term in (
                "move_stop_to_protect",
                "move_stop_to_break_even",
                "breakeven",
                "break_even",
            )
        )
        or any(term in combined for term in _BREAK_EVEN_TERMS)
        or (
            "止损" in combined
            and any(term in combined for term in ("开仓价", "入场价", "成本价"))
        )
    )


def _directive(
    intent: str,
    *,
    fraction: float | None = None,
    symbol: str | None,
    side: str | None,
    stop_loss: str | None = None,
    reason_code: str,
    strategy_thread_id: int | None = None,
    stop_price_source: str | None = None,
) -> ManagementDirective:
    return ManagementDirective(
        intent=intent,
        fraction=fraction,
        symbol=symbol,
        side=side,
        stop_loss=stop_loss,
        risk_reducing=True,
        fanout_allowed=True,
        cancel_deferred_entries=intent in {
            "cancel_entry",
            "partial_take_profit",
            "partial_then_break_even",
            "full_exit",
        },
        reason_code=reason_code,
        strategy_thread_id=strategy_thread_id,
        stop_price_source=stop_price_source,
    )


def _positive_int_or_none(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _management_fraction(
    lifecycle_event: Mapping[str, Any], combined_text: str
) -> float | None:
    values: list[float] = []
    for key in ("management_fraction", "close_fraction", "fraction"):
        value = _fraction_value(lifecycle_event.get(key))
        if value is not None:
            values.append(value)
    values.extend(_close_percentage_values(combined_text))
    values.extend(1.0 - value for value in _retained_percentage_values(combined_text))
    if values:
        first = values[0]
        if any(abs(value - first) > 1e-9 for value in values[1:]):
            raise ValueError("management_fraction_ambiguous")
        return first
    if "一半" in combined_text or "半仓" in combined_text:
        return 0.5
    return None


#: Joins the raw message text and the model's ``observed_text`` into the one
#: authoritative current-message text (``message_recognition.
#: _authoritative_current_message_text``). The newlines keep every line-based
#: reader seeing two separate lines, as the former plain "\n" join did; the
#: U+2029 PARAGRAPH SEPARATOR between them is a hard boundary for percent/verb
#: binding. #17936 "止盈70%保留底仓…QQ:…\n止盈70%…": the only digits between
#: the raw part's 保留 and the observed part's 70% were the QQ number, which
#: is scrubbed, so without this boundary the retained verb claimed that 70%.
AUTHORITATIVE_TEXT_PART_SEPARATOR = "\n \n"
_TEXT_PART_BOUNDARY = " "


def _percentage_values(text: str, verbs: str, *, source: str) -> list[float]:
    # Each part of an authoritative raw+observed text is read on its own: a
    # verb in one part never claims a percent in the other. Splitting happens
    # before scrubbing so a contact span can never blank the boundary.
    values: list[float] = []
    for part in str(text or "").split(_TEXT_PART_BOUNDARY):
        values.extend(_part_percentage_values(part, verbs, source=source))
    return values


def _part_percentage_values(text: str, verbs: str, *, source: str) -> list[float]:
    # Keep the entire percentage token, including signs/malformed content.
    # The old nondigit prefix swallowed '-' and silently discarded >100%.
    values = []
    previous_quantity = False
    # Quantity extraction runs on the same scrubbed input as price extraction.
    for percent in re.finditer(r"([^%％]*)[%％]", scrub_contact_identifiers(text)):
        matches = list(re.finditer(rf"(?:{verbs})", percent.group(1), flags=re.IGNORECASE))
        if not matches:
            # A repeated percent or connected range is supplied content, not
            # a missing quantity. Do not silently execute its first endpoint.
            if previous_quantity and re.match(r"^\s*(?:[/／~～\-—至到]|$)", percent.group(1)):
                raise ManagementFractionInvalid("invalid_format", source)
            previous_quantity = False
            continue
        # Bind to the nearest quantity verb, not an earlier price discussion.
        clause = percent.group(1)[matches[-1].end():]
        if not _percent_belongs_to_verb(clause):
            # The verb sits in an earlier sentence (or an earlier comma
            # chunk that already carried its own number), so this percent
            # is not its quantity: "剩余…83200！…减50%" or "出局！…收益370％".
            previous_quantity = False
            continue
        previous_quantity = True
        token = re.search(r"([+\-−－\d.a-zA-Z].*)$", clause.strip(), flags=re.DOTALL)
        raw_value = token.group(1) if token else clause.strip()
        try:
            value = _fraction_value(f"{raw_value}%")
            if value is None:
                raise ManagementFractionInvalid()
        except ManagementFractionInvalid as exc:
            exc.source = source
            raise
        values.append(value)
    return values


# Sentence boundaries, clause commas and the enumeration comma "、"
# ("平仓价78,136.4、盈利+136.13%"). An ASCII comma between two digits is a
# number separator ("减仓1,5%"), not a clause break, so it never splits.
_CLAUSE_BREAK = re.compile(r"[\n。！!？?；;‼，、]|(?<!\d),|,(?!\d)")


def _percent_belongs_to_verb(clause: str) -> bool:
    """Whether the text between a quantity verb and its '%' keeps them bound.

    The clause is split at sentence boundaries and clause commas. If any
    chunk before the percent's own chunk already carries a digit, the verb
    had its own statement ("剩余仓位止损位上移至83200！\\n…减50%",
    "平仓78031.7，盈利126.05%") and the percent describes something else.
    A break with no number in between still binds, so malformed content
    such as "减仓\\n150%" or "减仓；比例120%" is rejected, never defaulted.
    """
    chunks = _CLAUSE_BREAK.split(clause)
    return not any(re.search(r"\d", chunk) for chunk in chunks[:-1])


def _close_percentage_values(text: str) -> list[float]:
    return _percentage_values(text, "止盈|减仓|平仓|平掉|出掉|出局", source="close_percentage")


def _retained_percentage_values(text: str) -> list[float]:
    values = _percentage_values(text, "保留|剩余|留下|留", source="retained_percentage")
    if any(value == 1 for value in values):
        # Retaining 100% implies a zero close: refuse instead of inventing a trade.
        raise ManagementFractionInvalid("zero_close_fraction", "retained_percentage")
    return values


def _fraction_value(value: Any) -> float | None:
    # Three distinct outcomes: valid float, genuinely absent None, or an
    # explicit error. Returning None for invalid content would activate 50%.
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ManagementFractionInvalid()
    text = str(value).strip().replace("−", "-").replace("－", "-")
    is_percent = text.endswith(("%", "％"))
    if is_percent:
        text = text[:-1].strip()
    try:
        numeric = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManagementFractionInvalid() from exc
    if not numeric.is_finite():
        raise ManagementFractionInvalid("nonfinite")
    if is_percent:
        if not 0 < numeric <= 100:
            raise ManagementFractionInvalid("out_of_range")
        numeric /= 100
    # Bare values are fractions, not implicit percentages: 50 != "50%".
    if not 0 < numeric <= 1:
        raise ManagementFractionInvalid("out_of_range")
    result = float(numeric)
    if result == 0:
        raise ManagementFractionInvalid("underflow")
    return result


def validate_management_fraction_inputs(event: Mapping[str, Any], text: str) -> None:
    """Validate supplied inputs even if an earlier action branch would ignore them."""
    for key in ("management_fraction", "close_fraction", "fraction"):
        try:
            _fraction_value(event.get(key))
        except ManagementFractionInvalid as exc:
            exc.source = key
            raise
    _close_percentage_values(text)
    _retained_percentage_values(text)


def _normalized_optional(value: Any, *, upper: bool) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.upper() if upper else normalized.lower()


def _text_contains_explicit_stop_value(text: str, value: str) -> bool:
    try:
        expected = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not expected.is_finite() or expected <= 0:
        return False
    # A signature number is genuinely present in the message, so the provenance
    # window below would endorse it. Remove contact spans before looking.
    text = scrub_contact_identifiers(text)
    for match in re.finditer(
        r"(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])",
        text,
    ):
        if (
            match.start() > 0
            and text[match.start() - 1] in "百千万亿"
        ) or (
            match.end() < len(text)
            and text[match.end()] in "百千万亿"
        ):
            continue
        token = match.group(0)
        try:
            observed = Decimal(token.replace(",", ""))
        except InvalidOperation:
            continue
        if observed == expected:
            prefix = text[max(0, match.start() - 32):match.start()]
            suffix = text[match.end():min(len(text), match.end() + 16)]
            if re.search(
                r"(?:止损|stop\s*loss|\bmove\s+stop(?:\s+to)?|\bsl\b|"
                r"保护(?:到|至|价)|保本(?:到|至))",
                prefix,
            ) or re.match(
                r"\s*(?:(?:作为|设为|当作)\s*)?(?:止损|stop\s*loss|\bsl\b)",
                suffix,
            ):
                return True
    return False


# M2 guardrail (2026-09-29 Mia design, Q5/7.1): a percentage preceded by a
# *future* price level ("82500附近可以止盈30%先", #19670) names a plan for
# when the market gets there, not an instruction to reduce right now. This is
# distinct from M1's "剩余仓位止损位上移至P": that phrase is about the stop on
# what remains *after* an immediate reduction, never a price to wait for.
_FUTURE_TAKE_PROFIT_LEVEL_RE = re.compile(
    r"([0-9]+(?:\.\d+)?)附近[^0-9%％]{0,8}止盈"
    r"|到([0-9]+(?:\.\d+)?)[^0-9%％]{0,8}止盈"
    r"|([0-9]+(?:\.\d+)?)位置[^0-9%％]{0,8}止盈"
    r"|([0-9]+(?:\.\d+)?)止盈"
)
#: Q5/7.1: these say "reduce now", so a future-level match beside one of them
#: is not this guardrail's business -- ordinary planning handles it.
_IMMEDIATE_REDUCTION_TERMS = ("现价", "目前", "市价", "现在", "先出", "平掉")
#: Q6/7.1: "剩余仓位/剩余持仓" or an explicit profit-in-points phrase in the
#: same message also says "reduce now" -- this is M1's idiom, not a future
#: price to wait for.
_CURRENT_REDUCTION_SCOPE_TERMS = ("剩余仓位", "剩余持仓")
_PROFIT_POINTS_RE = re.compile(r"(?:获利|浮盈)[^0-9]{0,4}[0-9]+(?:\.\d+)?\s*点")


def future_take_profit_level(text: str) -> float | None:
    """The future price level a percentage is waiting for, or ``None``.

    Pure function: no market read, no lifecycle lookup. Returns ``None``
    whenever any Q6 "reduce now" signal is present (an immediate-action term,
    "剩余仓位/剩余持仓", or an explicit profit-in-points phrase), so the
    strategy_management_planner guardrail (M2/Q5) only fires for a message
    that names a level with none of those -- the #19670 shape, not Mia's
    standard "剩余仓位止损位上移至P" one.
    """

    normalized = str(text or "")
    if any(term in normalized for term in _IMMEDIATE_REDUCTION_TERMS):
        return None
    if any(term in normalized for term in _CURRENT_REDUCTION_SCOPE_TERMS):
        return None
    if _PROFIT_POINTS_RE.search(normalized):
        return None
    match = _FUTURE_TAKE_PROFIT_LEVEL_RE.search(normalized)
    if not match:
        return None
    token = next((group for group in match.groups() if group is not None), None)
    if token is None:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return value if value > 0 else None
