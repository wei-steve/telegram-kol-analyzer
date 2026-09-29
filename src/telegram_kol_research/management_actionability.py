"""Deterministic actionability gate for exchange-writing management intents.

First-pass phase 3 plan section 5.2
(``docs/plans/2026-09-29-first-pass-phase3-implementation-plan.md``).

A KOL commentary message ("今晚比特涨到86左右我们多单应该就要准备平仓了") is an
intention or a forecast, not an order. Nothing in the execution chain checked
that: ``resolve_management_directive`` turned any ``exit_position`` event into a
market ``full_exit`` on the event type alone. This module is the one place that
says "this text is not an instruction", independent of how the model classified
it, and it only ever answers in the refusing direction: a false positive costs
one management action that leaves a trace (``management_not_actionable:<rule>``),
never an unintended exchange write.

Pure and dependency-free (one leaf import): no database, no clock, no exchange.

Only intents that write to the exchange are judged (``JUDGED_INTENTS``);
``none`` / ``hold_update`` and the risk-increasing ("add") intents, which the
system refuses on its own terms (Q1), pass through untouched.

Rules, applied per clause. A clause is a run of text between ``，。；！？!?;,`` or
a line break:

1. ``intent_marker``: an intention / forecast marker (准备, 预计, 打算, 考虑,
   应该, 可能, 看情况, 做好…预期) in the same clause as an action verb.
2. ``hypothetical_condition``: a hypothetical (如果, 若, 假如, 万一, 一旦, 的话) in
   the same clause as an action verb.
3. ``price_trigger_immediate``: a price trigger (涨到/跌到/到/突破/跌破/站稳 +
   number, with 时/再/就) attached to an *immediate-fill* intent (full exit,
   market partial exit, cancel). For an *order-placing* intent (a stop, a
   break-even stop, a take-profit adjustment) the price is the order price and
   the instruction stands ("跌破84000就止损").
4. ``price_required``: a price-requiring intent with no concrete price anywhere
   (a stop "to cost / break-even" names its price in words and stands).
5. ``exit_verb_required``: a full exit derived only from
   ``event_type == exit_position`` needs an explicit exit verb in a clause
   that is not already a rule-1/2 clause.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from telegram_kol_research.contact_digit_scrubbing import scrub_contact_identifiers

#: Recorded reason prefix; the rule slug follows the colon.
REASON_PREFIX = "management_not_actionable"

RULE_INTENT_MARKER = "intent_marker"
RULE_HYPOTHETICAL = "hypothetical_condition"
RULE_PRICE_TRIGGER_IMMEDIATE = "price_trigger_immediate"
RULE_PRICE_REQUIRED = "price_required"
RULE_EXIT_VERB_REQUIRED = "exit_verb_required"

#: Intents that fill (or cancel) now: a price condition means "later", which we
#: cannot honour as an immediate market action.
IMMEDIATE_INTENTS = frozenset(
    {"full_exit", "partial_take_profit", "partial_then_break_even", "cancel_entry"}
)
#: Intents that rest an order on the exchange: the price *is* the order price.
ORDER_INTENTS = frozenset(
    {"adjust_stop_loss", "move_stop_to_break_even", "adjust_take_profit"}
)
#: Only these are judged; everything else passes through untouched.
JUDGED_INTENTS = IMMEDIATE_INTENTS | ORDER_INTENTS
#: Order-placing intents that cannot be placed without a price.
PRICE_REQUIRED_INTENTS = frozenset({"adjust_stop_loss", "adjust_take_profit"})

_FULL_EXIT_ACTIONS = frozenset({"exit_full", "full_exit", "close_position"})
_EXIT_EVENT_TYPES = frozenset({"exit_position"})

_CLAUSE_SPLIT_RE = re.compile(r"[，。；！？!?;,\n\r]+")

_INTENT_MARKER_RE = re.compile(r"准备|预计|打算|考虑|应该|可能|看情况|做好.*预期")
_HYPOTHETICAL_RE = re.compile(r"如果|若(?!干)|假如|万一|一旦|的话")

#: The action verbs the markers above attach to. A marker in a clause without
#: one of these is commentary about something else and is not this gate's
#: business (the rule is "same clause as the action verb").
_ACTION_VERB_RE = re.compile(
    r"平仓|平掉|全平|平(?![均稳台常时行衡])|出局|出场|离场|清仓|落袋|了结"
    r"|走(?![势向位出])|止盈|止损|减仓|加(?:一次|一点|点|个)?仓|补仓|保本|保护"
    r"|撤单|撤销|取消|add\s*more",
    re.IGNORECASE,
)
#: Explicit exit verbs for rule 5 (plan: 平, 出局, 离场, 走, 清仓, 止盈掉, 落袋,
#: 全部止盈). ``平`` and ``走`` carry look-aheads so 平均 / 走势 do not count.
_EXIT_VERB_RE = re.compile(
    r"平(?![均稳台常时行衡])|出局|离场|走(?![势向位出])|清仓|止盈掉|落袋|全部止盈"
)

#: ``涨到 / 跌到 / 到 / 突破 / 跌破 / 站稳`` immediately followed by a number.
_PRICE_TRIGGER_RE = re.compile(
    r"(?:涨到|跌到|冲到|回到|突破|跌破|站稳|到)\s*[约≈]?\s*\d"
)
_TRIGGER_TIMING_RE = re.compile(r"[时再就]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: A stop moved "to cost" names its price in words (the position's own entry
#: price): "修改止损到成本保护" is a complete instruction with no number in it.
_BREAK_EVEN_WORDS_RE = re.compile(r"成本|保本|保护|开仓价|入场价")


@dataclass(frozen=True, slots=True)
class ActionabilityRefusal:
    """Why a management text is not an instruction."""

    rule: str
    detail: str

    @property
    def reason_code(self) -> str:
        return f"{REASON_PREFIX}:{self.rule}"


def _clauses(text: str) -> list[str]:
    scrubbed = scrub_contact_identifiers(str(text or ""))
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(scrubbed) if part.strip()]


def _has_number(value: Any) -> bool:
    return value is not None and bool(_NUMBER_RE.search(str(value)))


def _first_hit(clauses: list[str], marker_re: re.Pattern[str]) -> tuple[str, str] | None:
    """The first clause carrying both ``marker_re`` and an action verb."""

    for clause in clauses:
        marker = marker_re.search(clause)
        if marker is not None and _ACTION_VERB_RE.search(clause):
            return marker.group(0), clause
    return None


_SENTENCE_SPLIT_RE = re.compile(r"[。；！？!?;\n\r]+")
_COMMA_SPLIT_RE = re.compile(r"[，,]+")
_MARKET_EVENT_RE = re.compile(r"突破|跌破|涨到|跌到|冲到|站稳|回踩|反弹|破位")


def _hypothetical_consequent_hit(text: str) -> tuple[str, str] | None:
    """A condition clause with no verb of its own governs the clause after it.

    Review of phase 3 batch 2: "如果突破84000，全部平仓" puts the condition and
    the order in two comma clauses of one sentence, so the same-clause check
    alone would let an immediate full exit through for what is a conditional
    order this system cannot place. Only a verb-less condition clause reaches
    forward, and only within its sentence.
    """

    scrubbed = scrub_contact_identifiers(str(text or ""))
    for sentence in _SENTENCE_SPLIT_RE.split(scrubbed):
        parts = [part.strip() for part in _COMMA_SPLIT_RE.split(sentence) if part.strip()]
        for index, part in enumerate(parts[:-1]):
            marker = _HYPOTHETICAL_RE.search(part)
            if marker is None or _ACTION_VERB_RE.search(part):
                continue
            # Only a market condition (a price or a price event) makes the
            # next clause a conditional order. "如果你还在场内，全部出局" is
            # addressed to whoever still holds, and is an order.
            if not (_NUMBER_RE.search(part) or _MARKET_EVENT_RE.search(part)):
                continue
            consequent = parts[index + 1]
            if _ACTION_VERB_RE.search(consequent):
                return marker.group(0), f"{part}，{consequent}"
    return None


def assess_management_actionability(
    text: str | None,
    lifecycle_event: Mapping[str, Any] | None,
    intent: str | None,
    *,
    check_price: bool = True,
) -> ActionabilityRefusal | None:
    """``None`` when ``text`` may drive ``intent``; otherwise the refusal.

    ``check_price=False`` skips rule 4 for callers that do not hold the
    instruction's parsed price (the planner backstop: a candidate reaches it
    with the price already resolved or gated by the stop gate).
    """

    intent_name = str(intent or "").strip().lower()
    if intent_name not in JUDGED_INTENTS:
        return None
    event = lifecycle_event if isinstance(lifecycle_event, Mapping) else {}
    clauses = _clauses(str(text or ""))

    hit = _first_hit(clauses, _INTENT_MARKER_RE)
    if hit is not None:
        return ActionabilityRefusal(RULE_INTENT_MARKER, f"{hit[0]}: {hit[1]}")
    hit = _first_hit(clauses, _HYPOTHETICAL_RE) or _hypothetical_consequent_hit(
        str(text or "")
    )
    if hit is not None:
        return ActionabilityRefusal(RULE_HYPOTHETICAL, f"{hit[0]}: {hit[1]}")

    if intent_name in IMMEDIATE_INTENTS:
        for clause in clauses:
            if _PRICE_TRIGGER_RE.search(clause) and _TRIGGER_TIMING_RE.search(clause):
                return ActionabilityRefusal(RULE_PRICE_TRIGGER_IMMEDIATE, clause)

    if check_price and intent_name in PRICE_REQUIRED_INTENTS:
        price_field = (
            event.get("take_profit")
            if intent_name == "adjust_take_profit"
            else event.get("stop_loss")
        )
        if (
            not _has_number(price_field)
            and not any(_NUMBER_RE.search(clause) for clause in clauses)
            and not (
                intent_name == "adjust_stop_loss"
                and _BREAK_EVEN_WORDS_RE.search(" ".join(clauses))
            )
        ):
            return ActionabilityRefusal(RULE_PRICE_REQUIRED, intent_name)

    if intent_name == "full_exit":
        event_type = str(event.get("event_type") or "").strip().lower()
        action = str(event.get("management_action") or "").strip().lower()
        if event_type in _EXIT_EVENT_TYPES and action not in _FULL_EXIT_ACTIONS:
            # Every clause here already passed rules 1 and 2 (they would have
            # returned above), so any exit verb found is in a clean clause.
            if not any(_EXIT_VERB_RE.search(clause) for clause in clauses):
                return ActionabilityRefusal(
                    RULE_EXIT_VERB_REQUIRED, "no explicit exit verb in the text"
                )
    return None
