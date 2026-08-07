from datetime import UTC, datetime, timedelta
from decimal import Decimal


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def _strategy(*, raw_id: int = 20, message_id: int = 9902):
    from telegram_kol_research.adjacent_entry_assembly import EntryStrategyFact

    return EntryStrategyFact(
        raw_message_id=raw_id,
        message_id=message_id,
        posted_at=NOW + timedelta(seconds=20),
        symbol="BTC",
        side="short",
    )


def _fact(
    *,
    raw_id: int,
    message_id: int,
    kind: str = "fragment",
    fragment_id: int | None = None,
    fragment_kind: str | None = None,
    payload: dict[str, object] | None = None,
    symbol: str | None = "BTC",
    side: str | None = "short",
):
    from telegram_kol_research.adjacent_entry_assembly import AdjacentEntryFact

    return AdjacentEntryFact(
        raw_message_id=raw_id,
        message_id=message_id,
        posted_at=NOW + timedelta(seconds=raw_id),
        kind=kind,
        symbol=symbol,
        side=side,
        fragment_id=fragment_id,
        fragment_kind=fragment_kind,
        payload=payload or {},
    )


def _select(facts, *, cutoff_seconds: int = 60):
    from telegram_kol_research.adjacent_entry_assembly import (
        select_adjacent_entry_fragments,
    )

    return select_adjacent_entry_fragments(
        strategy=_strategy(),
        facts=facts,
        cutoff=(NOW + timedelta(seconds=cutoff_seconds), 99999, 99999),
    )


def test_selects_preceding_and_following_explicit_risk_fragments():
    preceding = _select(
        [
            _fact(
                raw_id=19,
                message_id=9901,
                fragment_id=1,
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": "0.5"},
            )
        ]
    )
    following = _select(
        [
            _fact(
                raw_id=21,
                message_id=9936,
                fragment_id=2,
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": "1"},
            )
        ]
    )

    assert preceding.status == "ready"
    assert preceding.fragment_ids == (1,)
    assert preceding.risk_multiplier == Decimal("0.5")
    assert following.status == "ready"
    assert following.fragment_ids == (2,)
    assert following.risk_multiplier == Decimal("1")


def test_selects_following_half_and_supplemental_entry():
    decision = _select(
        [
            _fact(
                raw_id=21,
                message_id=559,
                fragment_id=3,
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": "0.5"},
            ),
            _fact(
                raw_id=22,
                message_id=4155,
                fragment_id=4,
                fragment_kind="supplemental_entry",
                payload={"entry_price": "63400"},
            ),
        ]
    )

    assert decision.risk_multiplier == Decimal("0.5")
    assert decision.supplemental_prices == (Decimal("63400"),)
    assert decision.fragment_ids == (3, 4)


def test_unresolved_following_fact_defers_but_completed_unrelated_does_not():
    pending = _select([_fact(raw_id=21, message_id=559, kind="unresolved")])
    unrelated = _select([_fact(raw_id=21, message_id=559, kind="unrelated")])

    assert pending.status == "pending"
    assert pending.reason_code == "adjacent_entry_context_pending"
    assert unrelated.status == "ready"
    assert unrelated.risk_multiplier == Decimal("1")


def test_hard_boundary_stops_following_selection():
    decision = _select(
        [
            _fact(raw_id=21, message_id=9903, kind="complete_entry"),
            _fact(
                raw_id=22,
                message_id=9904,
                fragment_id=5,
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": "0.5"},
            ),
        ]
    )

    assert decision.fragment_ids == ()
    assert decision.risk_multiplier == Decimal("1")
    assert decision.boundary_evidence == (21,)


def test_fragment_on_following_complete_entry_message_belongs_after_boundary():
    decision = _select(
        [
            _fact(
                raw_id=21,
                message_id=9903,
                fragment_id=50,
                fragment_kind="risk_multiplier",
                payload={"risk_multiplier": "0.5"},
            ),
            _fact(raw_id=21, message_id=9903, kind="complete_entry"),
        ]
    )

    assert decision.fragment_ids == ()
    assert decision.risk_multiplier == Decimal("1")


def test_conflicting_explicit_multipliers_block():
    decision = _select(
        [
            _fact(raw_id=19, message_id=9901, fragment_id=6, fragment_kind="risk_multiplier", payload={"risk_multiplier": "0.5"}),
            _fact(raw_id=21, message_id=9903, fragment_id=7, fragment_kind="risk_multiplier", payload={"risk_multiplier": "1"}),
        ]
    )

    assert decision.status == "blocked"
    assert decision.reason_code == "entry_risk_multiplier_conflict"


def test_no_fragment_does_not_infer_half_from_narrow_range():
    decision = _select([])

    assert decision.status == "ready"
    assert decision.risk_multiplier == Decimal("1")


def test_two_points_each_half_preserves_full_budget_and_allocations():
    decision = _select(
        [
            _fact(raw_id=19, message_id=9901, fragment_id=8, fragment_kind="risk_multiplier", payload={"risk_multiplier": "1"}),
            _fact(raw_id=19, message_id=9901, fragment_id=9, fragment_kind="leg_allocation", payload={"allocations": ["0.5", "0.5"]}),
        ]
    )

    assert decision.risk_multiplier == Decimal("1")
    assert decision.allocations == (Decimal("0.5"), Decimal("0.5"))


def test_cutoff_excludes_later_unresolved_work():
    decision = _select(
        [_fact(raw_id=70, message_id=10000, kind="unresolved")],
        cutoff_seconds=60,
    )

    assert decision.status == "ready"
