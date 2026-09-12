"""Phase 6k. Releasing a write path by what a position *is*, not by its id.

Until now every exchange-write gate has been a list of position ids. That shape
was right and it is why phase 6 could be taken one position at a time: adding
an id costs a code change, a full suite and a deploy, so nothing is released by
accident and every release is reviewable as a diff.

It does not scale, and the reason it does not is worth stating precisely: the
enumeration has to be edited *while a position is live*, which means the cost
lands at exactly the moment when hurrying is most tempting. A predicate decided
in advance is reviewed once, in the quiet.

**What was narrowed out of this, and why it is not here.** The first proposal
was "release by source": every primary stop adopted from the exchange, every
limit entry's take profits. Two objections arrived independently -- one that
the enumeration's cost is the design rather than a defect, one that the
authority granted was per position and did not cover inverting the default --
and a third held on its own: ``evidence_source == "exchange_adopted_by_tu"``
and ``order_kind == "limit"`` say nothing about **who opened the position**. A
manually opened position, or one in a group this system only watches, satisfies
both. So the predicate below carries two further conditions that the original
did not:

* the binding's chat must be configured ``auto_trade``. A ``notify_only`` group
  is watched and never traded; writing to a position there would be acting in a
  group the user set up precisely so that nothing would act;
* the entry leg's attribution must be ``verified``. Everything downstream keys
  off ``pos_id``, and an unverified leg's ``pos_id`` is a guess.

**Unknown is never a release.** An unreadable group mode, a missing binding, an
attribution this module does not recognise -- each is a refusal with its own
name. The enumeration's safety came from a human typing an id; this predicate's
has to come from every unknown answering "no".

**The blacklist exists for the hour you cannot deploy in.** Default empty. It
is not a second gate to reason about -- a position in it is simply held, no
matter what the predicate says -- and it is checked first so that reading the
code answers "can this id be released?" without following the predicate.

This module decides. It fetches nothing, and it writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Positions held back regardless of the predicate. **Empty by default and
#: meant to stay that way**: this is the lever for an hour when something looks
#: wrong and a deploy is not possible, not a place to park decisions. An id
#: here should come with a line in the status file saying when it goes away.
SOURCE_RELEASE_BLOCKED_POS_IDS: frozenset[str] = frozenset()

#: The evidence source that means "the exchange holds this stop and nothing
#: here submitted it" -- the adoption phase 6e introduced.
ADOPTED_SOURCE = "exchange_adopted_by_tu"

#: The entry kind whose take profits phase A-15-1 withheld.
LIMIT_ENTRY_KIND = "limit"

#: The only group mode in which this system writes at all.
AUTO_TRADE_MODE = "auto_trade"

#: The only attribution that makes a ``pos_id`` a fact rather than a guess.
VERIFIED_ATTRIBUTION = "verified"


@dataclass(frozen=True)
class SourceReleaseVerdict:
    """Released or held, and the reason in the reason's own words."""

    released: bool
    reason: str
    pos_id: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.released


def _held(reason: str, pos_id: str) -> SourceReleaseVerdict:
    return SourceReleaseVerdict(released=False, reason=reason, pos_id=pos_id)


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def resolve_group_trading_mode(provider: Any, chat_id: Any) -> str:
    """The chat's configured mode, or "" when it cannot be read.

    Deliberately collapses "no provider", "unknown chat" and "the provider
    raised" into the same empty string. They differ in cause and not in
    consequence: none of them is evidence that the group trades, and the caller
    must refuse for all three. Returning a default of ``auto_trade`` for any of
    them would release on a guess.
    """

    if provider is None or chat_id is None:
        return ""
    try:
        return _normalized(provider(int(chat_id)))
    except Exception:
        return ""


def evaluate_source_release(
    *,
    pos_id: Any,
    kind: str,
    evidence_source: Any = None,
    entry_order_kind: Any = None,
    attribution_status: Any = None,
    group_trading_mode: Any = None,
    blocked_pos_ids: frozenset[str] = SOURCE_RELEASE_BLOCKED_POS_IDS,
) -> SourceReleaseVerdict:
    """Whether this position's write path is released by its properties.

    ``kind`` selects which source predicate applies:

    * ``"adopted_primary_backup_stop"`` -- the primary stop must have reached
      the ledger by adoption;
    * ``"take_profit_limit_entry"`` -- the entry leg must be a plain limit.

    Both then require an ``auto_trade`` group and a ``verified`` attribution.
    """

    identifier = str(pos_id or "").strip()
    if not identifier:
        return _held("no_pos_id", "")

    # Checked first on purpose: reading this function top to bottom should
    # answer "can this id be released at all?" before any predicate is reached.
    if identifier in (blocked_pos_ids or frozenset()):
        return _held("pos_id_blocked", identifier)

    if kind == "adopted_primary_backup_stop":
        if _normalized(evidence_source) != ADOPTED_SOURCE:
            return _held("primary_stop_not_adopted", identifier)
    elif kind == "take_profit_limit_entry":
        if _normalized(entry_order_kind) != LIMIT_ENTRY_KIND:
            return _held("entry_not_a_plain_limit", identifier)
    else:
        # A kind this module does not know about is not a release. Adding a
        # third write path must be a deliberate edit here, not something that
        # inherits permission by passing an unrecognised string.
        return _held("unknown_release_kind", identifier)

    mode = _normalized(group_trading_mode)
    if not mode:
        return _held("group_trading_mode_unknown", identifier)
    if mode != AUTO_TRADE_MODE:
        return _held("group_not_auto_trade", identifier)

    if _normalized(attribution_status) != VERIFIED_ATTRIBUTION:
        return _held("attribution_not_verified", identifier)

    return SourceReleaseVerdict(
        released=True, reason="released_by_source", pos_id=identifier
    )


def describe_source_release() -> dict[str, Any]:
    """What the source-shaped release currently permits, for the gate report.

    The per-id gates render as a list of ids; this one has no list to render,
    so it renders its conditions instead. "all" on its own would be the least
    informative possible line about the widest possible permission.
    """

    return {
        "adopted_primary_backup_stop": (
            f"evidence_source={ADOPTED_SOURCE} + group={AUTO_TRADE_MODE} "
            f"+ attribution={VERIFIED_ATTRIBUTION}"
        ),
        "take_profit_limit_entry": (
            f"entry_order_kind={LIMIT_ENTRY_KIND} + group={AUTO_TRADE_MODE} "
            f"+ attribution={VERIFIED_ATTRIBUTION}"
        ),
        "blocked_pos_ids": sorted(str(item) for item in SOURCE_RELEASE_BLOCKED_POS_IDS),
    }


__all__ = [
    "ADOPTED_SOURCE",
    "AUTO_TRADE_MODE",
    "LIMIT_ENTRY_KIND",
    "SOURCE_RELEASE_BLOCKED_POS_IDS",
    "VERIFIED_ATTRIBUTION",
    "SourceReleaseVerdict",
    "describe_source_release",
    "evaluate_source_release",
    "resolve_group_trading_mode",
]
