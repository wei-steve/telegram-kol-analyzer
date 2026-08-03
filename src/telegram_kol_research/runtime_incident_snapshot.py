"""Immutable bounded snapshot container for proactive invariant rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Any


@dataclass(frozen=True, slots=True)
class InvariantSnapshot:
    observed_at: datetime
    complete: bool
    facts_by_rule: Mapping[str, tuple[Mapping[str, Any], ...]]

    def __post_init__(self) -> None:
        if len(self.facts_by_rule) > 16:
            raise ValueError("snapshot rule set is unbounded")
        bounded: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for rule_id, items in self.facts_by_rule.items():
            if len(items) > 100:
                raise ValueError("snapshot objects are unbounded")
            bounded[rule_id] = tuple(MappingProxyType(dict(item)) for item in items)
        object.__setattr__(self, "facts_by_rule", MappingProxyType(bounded))
