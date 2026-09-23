"""One name for "this candidate only confirms an entry we already know about".

An entry confirmation is materialised as an ordinary ``entry_signal`` candidate
(``message_recognition._upsert_entry_confirmation_candidate``) so the existing
instruction, alert and audit machinery carries it. That shape is indistinguishable
from a real new entry unless something says otherwise, and on 2026-09-23 it was
not: a confirmation message opened 29 contracts of its own, with the stop loss it
had inherited from the lifecycle it was confirming.

Before that, the four gates that needed the distinction each tested
``parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}`` directly. The
authoritative path passes ``parse_source="mimo_authoritative"``, which is in
neither set, so all four were silently bypassed. The candidate now carries
``management_action = "entry_confirm"`` as well, and the four gates ask this
module instead of re-deriving the answer.

This is deliberately *not* in ``duplicate_entry_confirmation.py``: A-16b's
"confirmation" is a duplicate-entry parking decision, an unrelated meaning of
the same word.
"""

from __future__ import annotations

from typing import Any


#: Parse sources that only ever produce confirmation candidates. Retained
#: because rows written before 2026-09-23 carry no ``management_action``.
ENTRY_CONFIRMATION_PARSE_SOURCES = frozenset(
    {"entry_confirm_heuristic", "lifecycle_ai"}
)
#: The marker written on the candidate itself. It is not a management action --
#: the row keeps ``event_type='entry_signal'`` and ``target_lifecycle_id IS
#: NULL``, so no management loader can ever select it.
ENTRY_CONFIRMATION_MANAGEMENT_ACTION = "entry_confirm"


def is_entry_confirmation_signature(
    *,
    event_type: Any,
    parse_source: Any,
    management_action: Any,
) -> bool:
    """Whether these three candidate columns describe an entry confirmation."""

    if str(event_type or "") != "entry_signal":
        return False
    return (
        str(parse_source or "") in ENTRY_CONFIRMATION_PARSE_SOURCES
        or str(management_action or "") == ENTRY_CONFIRMATION_MANAGEMENT_ACTION
    )


def is_entry_confirmation_candidate(candidate: Any) -> bool:
    """Whether this ``SignalCandidate`` only confirms an existing entry."""

    return is_entry_confirmation_signature(
        event_type=getattr(candidate, "event_type", None),
        parse_source=getattr(candidate, "parse_source", None),
        management_action=getattr(candidate, "management_action", None),
    )
