"""Execution gate for a first pass that ran without its images.

2026-10-01 image-unavailable design
(docs/plans/2026-10-01-image-unavailable-text-fallback-design.md §4.3, option 乙,
approved by the user).

When a declared image could not be read, the first pass judges the text alone
(``recognition_experiments.run_mimo_authoritative_for_message``). The model then
saw the text and the chat context, so any price it returns came either from
this message's text or from an *older* message -- and the image, which might
have carried a different price, a correction or "已撤", was never seen. The
rule is therefore about provenance, not about the model's confidence:

* an entry (top-level ``strategy``, an ``entry`` / ``replace_entry``
  instruction): its entry and stop-loss prices must appear in this message's
  text;
* a management action carrying a new price (stop loss, take profit, entry
  price in an instruction's parameters, the lifecycle event's stop / take
  profit): that price must appear in the text;
* a management action without a price of its own -- full exit, partial exit,
  cancel, break-even / protect-to-cost (its price is named in words) -- is not
  judged here; the existing target resolution and actionability gate still
  apply to it.

Pure function, no I/O. It only ever refuses; it never makes anything
executable that was not already.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.contact_digit_scrubbing import scrub_contact_identifiers
from telegram_kol_research.management_directives import _has_break_even_clause

__all__ = ["ImageMissingPriceRefusal", "assess_image_missing_prices"]

_NUMBER = re.compile(r"(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])")

#: Instruction kinds whose ``strategy`` is an order to place.
_ENTRY_INSTRUCTION_KINDS = frozenset({"entry", "replace_entry"})
#: Instruction kinds that carry no price of their own.
_PRICELESS_INSTRUCTION_KINDS = frozenset(
    {
        "cancel_pending_entry",
        "full_exit",
        "partial_exit",
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
    }
)
#: Lifecycle events whose price fields are informational, not order prices.
_PRICELESS_EVENT_TYPES = frozenset({"none", "cancel_entry", "exit_position", "entry_confirm"})
#: ``parameters`` keys of an instruction that name an order price.
_PRICE_PARAMETER_MARKERS = ("price", "stop", "take_profit", "entry")


@dataclass(frozen=True, slots=True)
class ImageMissingPriceRefusal:
    field: str
    value: str

    @property
    def detail(self) -> str:
        return f"{self.field}={self.value}"


def _numbers(value: str) -> list[Decimal]:
    found: list[Decimal] = []
    for match in _NUMBER.finditer(value):
        try:
            number = Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        if number.is_finite():
            found.append(number.normalize())
    return found


def _text_numbers(text: str) -> set[Decimal]:
    return set(_numbers(scrub_contact_identifiers(text)))


def _price_values(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float, Decimal)):
        return [str(value)]
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [item for element in value for item in _price_values(element)]
    return []


def _first_unsupported(
    field: str, value: Any, text_numbers: set[Decimal]
) -> ImageMissingPriceRefusal | None:
    for raw in _price_values(value):
        for number in _numbers(raw):
            if number not in text_numbers:
                return ImageMissingPriceRefusal(field=field, value=raw)
    return None


def _strategy_refusal(
    prefix: str, strategy: Any, text_numbers: set[Decimal]
) -> ImageMissingPriceRefusal | None:
    if not isinstance(strategy, Mapping):
        return None
    for key in ("entry", "stop_loss"):
        refusal = _first_unsupported(f"{prefix}.{key}", strategy.get(key), text_numbers)
        if refusal is not None:
            return refusal
    return None


def assess_image_missing_prices(
    text: str | None, payload: Mapping[str, Any] | None
) -> ImageMissingPriceRefusal | None:
    """First order price in ``payload`` that does not appear in ``text``."""

    if not isinstance(payload, Mapping):
        return None
    text = str(text or "")
    text_numbers = _text_numbers(text)

    refusal = _strategy_refusal("strategy", payload.get("strategy"), text_numbers)
    if refusal is not None:
        return refusal

    instructions = payload.get("instructions")
    if isinstance(instructions, list):
        for index, item in enumerate(instructions):
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind in _ENTRY_INSTRUCTION_KINDS:
                refusal = _strategy_refusal(
                    f"instructions[{index}].strategy", item.get("strategy"), text_numbers
                )
                if refusal is not None:
                    return refusal
            if kind in _PRICELESS_INSTRUCTION_KINDS:
                continue
            parameters = item.get("parameters")
            if isinstance(parameters, Mapping):
                for key, value in parameters.items():
                    if any(marker in str(key).lower() for marker in _PRICE_PARAMETER_MARKERS):
                        refusal = _first_unsupported(
                            f"instructions[{index}].parameters.{key}", value, text_numbers
                        )
                        if refusal is not None:
                            return refusal

    event = payload.get("lifecycle_event")
    if isinstance(event, Mapping):
        event_type = str(event.get("event_type") or "none").strip().lower()
        if event_type not in _PRICELESS_EVENT_TYPES:
            raw_action = str(event.get("management_action") or "").strip().lower()
            combined = " ".join((text.lower(), raw_action))
            fields = ["take_profit"]
            if not _has_break_even_clause(combined, raw_action):
                fields.insert(0, "stop_loss")
            for key in fields:
                refusal = _first_unsupported(
                    f"lifecycle_event.{key}", event.get(key), text_numbers
                )
                if refusal is not None:
                    return refusal
    return None
