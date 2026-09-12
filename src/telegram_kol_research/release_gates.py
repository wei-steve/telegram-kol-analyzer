"""What is released right now, in one place, readable from outside the process.

Four constants decide whether a path that can write to the exchange actually
writes. Each was approved separately and each is deliberately a constant rather
than a setting, so that releasing one costs a code change, a full suite and a
deploy. That design is good and it has one gap: **a released gate announces
itself to nobody.**

Its state lives in two places only -- the source file, and the memory of
whoever deployed it. On 2026-09-11 that cost a round: one session believed a
gate was still closed while it had been open and running for some time, and
found out by reading the production file. Nothing had gone wrong; nothing had
said anything either, which is the point.

So the values are collected here and surfaced twice: the worker logs them once
at startup, and ``/api/runtime/release-gates`` returns them on demand. An
observation script can then carry a ``gates_ok`` beside its ``head_ok`` --
same shape, same meaning: the window is only healthy while production's gates
are the ones the window was opened against.

**This module decides nothing.** It imports the constants and reports them. If
it ever starts deciding, the thing it reports stops being the thing that acts.
"""

from __future__ import annotations

from typing import Any

from telegram_kol_research.break_even_convergence_executor import (
    BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS,
    BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS,
)
from telegram_kol_research.source_release import (
    SOURCE_RELEASE_BLOCKED_POS_IDS,
    describe_source_release,
)

#: Name -> what releasing it permits. The description is part of the report on
#: purpose: a reader seeing a position id appear should not have to find the
#: constant to learn what it now allows.
GATE_DESCRIPTIONS = {
    "break_even_replacement": (
        "move this position's primary stop to the entry price "
        "(place new, read back, cancel only the primary)"
    ),
    "break_even_full_exit": (
        "close this position at market when the price has crossed back "
        "through the entry price"
    ),
    "take_profit_limit_entry": (
        "place a limit entry's planned take-profits on the exchange"
    ),
    "adopted_primary_backup_stop": (
        "place a backup stop beside a primary stop that was adopted from the "
        "exchange rather than submitted here"
    ),
}


def current_release_gates() -> dict[str, Any]:
    """Every release gate's current value, as this process holds it."""

    values = {
        "break_even_replacement": BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS,
        "break_even_full_exit": BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS,
    }
    gates = {
        name: {
            "released_pos_ids": sorted(str(item) for item in released),
            "count": len(released),
            "permits": GATE_DESCRIPTIONS[name],
        }
        for name, released in values.items()
    }
    # Phase 6k. Two gates stopped being lists. A list renders as its contents;
    # a predicate has no contents to render, so it renders its conditions --
    # "all" would be the least informative possible line about the widest
    # possible permission, which is the opposite of what this module is for.
    source_shaped = describe_source_release()
    for name, conditions in source_shaped.items():
        if name == "blocked_pos_ids":
            continue
        gates[name] = {
            "shape": "predicate",
            "conditions": conditions,
            "blocked_pos_ids": source_shaped["blocked_pos_ids"],
            "permits": GATE_DESCRIPTIONS[name],
        }
    return {
        "gates": gates,
        "total_released": sum(
            gate.get("count", 0) for gate in gates.values()
        ),
        # A single line an observation script can compare against, so a window
        # notices a gate changing under it the way it notices the sha changing.
        "fingerprint": release_gate_fingerprint(),
    }


def release_gate_fingerprint() -> str:
    """One short stable string naming every released id.

    Deliberately readable rather than hashed: a mismatch should say *what*
    changed, and a digest would only say *that* something did -- which is the
    same shortfall as recording "held" without the plan (phase 6f).
    """

    values = {
        "break_even_replacement": BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS,
        "break_even_full_exit": BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS,
    }
    parts = [
        f"{name}={','.join(sorted(str(item) for item in released)) or '-'}"
        for name, released in sorted(values.items())
    ]
    # A predicate-shaped gate renders as "by-source", plus the ids it is
    # holding back. Rendering it as "-" would say "nothing is released", which
    # is the exact opposite of what it means, and an observation script
    # comparing fingerprints would go on matching while the shape changed
    # underneath it.
    blocked = ",".join(
        sorted(str(item) for item in SOURCE_RELEASE_BLOCKED_POS_IDS)
    )
    for name in sorted(("adopted_primary_backup_stop", "take_profit_limit_entry")):
        parts.append(f"{name}=by-source(blocked:{blocked or 'none'})")
    return ";".join(sorted(parts))


def format_release_gates_for_log() -> str:
    """The one line the worker logs at startup."""

    return f"release_gates {release_gate_fingerprint()}"


__all__ = [
    "GATE_DESCRIPTIONS",
    "current_release_gates",
    "format_release_gates_for_log",
    "release_gate_fingerprint",
]
