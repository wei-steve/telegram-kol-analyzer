"""A1 (2026-09-25): the candidate set's hard age filter.

Every test here builds its threads in one chat and asks for candidates with the
same symbol and side, so the only thing that can separate two rows is the
filter's three conditions. The pairs are deliberate: a positive assertion about
what survives next to a positive assertion about what does not, from one
fixture, because ``not in`` on its own cannot tell "the filter removed it" from
"the fixture never built it".
"""

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.strategy_thread_candidates import (
    STALE_FILTERABLE_LIFECYCLE_STATUSES,
    STALE_LIFECYCLE_MAX_AGE,
    generate_strategy_thread_candidates,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
)


NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)


def _thread(
    session_factory,
    *,
    chat_id: int,
    message_id: int,
    status: str,
    signal_at: datetime,
    symbol: str = "BTC",
    side: str = "long",
    with_binding: bool = False,
) -> tuple[int, int]:
    """One same-chat thread whose lifecycle is exactly as old as asked.

    ``with_binding`` is the point of the fixture: it is the single difference
    between the two halves of the A1 pair, so nothing but condition 3 can
    explain a different outcome.
    """

    with session_factory() as session:
        binding_id = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:{symbol}:{side}",
                kol_id=f"group:{chat_id}",
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side=side,
                status="active",
            )
            session.add(binding)
            session.flush()
            binding_id = binding.id
        session.add(
            RawMessage(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{symbol} {side}",
                posted_at=signal_at,
            )
        )
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side=side,
            lifecycle_status=status,
            signal_at=signal_at,
            entry_range_low=65000,
            entry_range_high=65500,
            execution_binding_id=binding_id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    return int(thread.id), int(lifecycle_id)


def _current_message(
    session_factory,
    *,
    chat_id: int,
    message_id: int,
    posted_at: datetime | None,
) -> int:
    with session_factory() as session:
        current = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="止损移到保本",
            posted_at=posted_at,
        )
        session.add(current)
        session.commit()
        return int(current.id)


def _candidates(session_factory, *, raw_message_id: int, symbol: str, side: str):
    with session_factory() as session:
        return generate_strategy_thread_candidates(
            session,
            raw_message_id=raw_message_id,
            symbol=symbol,
            side=side,
        )


def test_the_age_filter_reuses_the_recency_bonus_threshold():
    """72 hours, and only the two never-entered statuses."""

    assert STALE_LIFECYCLE_MAX_AGE == timedelta(hours=72)
    assert STALE_FILTERABLE_LIFECYCLE_STATUSES == frozenset(
        {"pending_entry", "expired"}
    )
    assert "entered" not in STALE_FILTERABLE_LIFECYCLE_STATUSES
    assert "holding" not in STALE_FILTERABLE_LIFECYCLE_STATUSES


def test_stale_unbound_pending_entry_leaves_the_set_but_a_bound_one_stays(tmp_path):
    """The A1 pair: same age, one execution binding apart, two outcomes."""

    session_factory = create_session_factory(tmp_path / "research.db")
    stale_at = NOW - timedelta(hours=73)
    unbound_thread_id, unbound_lifecycle_id = _thread(
        session_factory,
        chat_id=90,
        message_id=6001,
        status="pending_entry",
        signal_at=stale_at,
    )
    bound_thread_id, bound_lifecycle_id = _thread(
        session_factory,
        chat_id=90,
        message_id=6002,
        status="pending_entry",
        signal_at=stale_at,
        with_binding=True,
    )
    current_id = _current_message(
        session_factory, chat_id=90, message_id=6003, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        bound_lifecycle_id
    ]
    assert candidates[0].thread_id == bound_thread_id
    assert candidates[0].status == "pending_entry"
    assert candidates[0].binding_summary is not None
    # The survivor is old, so it must not carry the recency bonus either. If it
    # did, the filter and the bonus would be reading two different clocks.
    assert "recent_active_thread" not in candidates[0].reasons
    assert unbound_thread_id not in {
        candidate.thread_id for candidate in candidates
    }
    assert unbound_lifecycle_id != bound_lifecycle_id


def test_a_pending_entry_just_inside_the_window_still_survives(tmp_path):
    """71 hours is not stale; the boundary belongs to the surviving side."""

    session_factory = create_session_factory(tmp_path / "research.db")
    _, fresh_lifecycle_id = _thread(
        session_factory,
        chat_id=96,
        message_id=6501,
        status="pending_entry",
        signal_at=NOW - timedelta(hours=71),
    )
    _, stale_lifecycle_id = _thread(
        session_factory,
        chat_id=96,
        message_id=6502,
        status="pending_entry",
        signal_at=NOW - timedelta(hours=73),
    )
    current_id = _current_message(
        session_factory, chat_id=96, message_id=6503, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        fresh_lifecycle_id
    ]
    assert "recent_active_thread" in candidates[0].reasons
    assert stale_lifecycle_id != fresh_lifecycle_id


def test_stale_expired_lifecycle_without_binding_leaves_the_set(tmp_path):
    """``expired`` is still in ``ACTIVE_LIFECYCLE_STATUSES``, so it needs the filter."""

    session_factory = create_session_factory(tmp_path / "research.db")
    _, expired_lifecycle_id = _thread(
        session_factory,
        chat_id=91,
        message_id=6101,
        status="expired",
        signal_at=NOW - timedelta(days=30),
    )
    _, recent_lifecycle_id = _thread(
        session_factory,
        chat_id=91,
        message_id=6102,
        status="expired",
        signal_at=NOW - timedelta(hours=2),
    )
    current_id = _current_message(
        session_factory, chat_id=91, message_id=6103, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        recent_lifecycle_id
    ]
    assert candidates[0].status == "expired"
    assert expired_lifecycle_id != recent_lifecycle_id


def test_month_old_entered_lifecycle_stays_in_the_set(tmp_path):
    """A position opened a month ago is still a position. Never filter it.

    The ``entered`` row carries no execution binding either, so condition 1 is
    the only thing keeping it in the set -- exactly the case the design says
    must never be touched.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    entered_thread_id, entered_lifecycle_id = _thread(
        session_factory,
        chat_id=92,
        message_id=6201,
        status="entered",
        signal_at=NOW - timedelta(days=31),
    )
    _, stale_pending_lifecycle_id = _thread(
        session_factory,
        chat_id=92,
        message_id=6202,
        status="pending_entry",
        signal_at=NOW - timedelta(days=31),
    )
    current_id = _current_message(
        session_factory, chat_id=92, message_id=6203, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        entered_lifecycle_id
    ]
    assert candidates[0].thread_id == entered_thread_id
    assert candidates[0].status == "entered"
    assert stale_pending_lifecycle_id != entered_lifecycle_id


def test_month_old_holding_lifecycle_stays_in_the_set(tmp_path):
    """The other status the filter may never reach."""

    session_factory = create_session_factory(tmp_path / "research.db")
    _, holding_lifecycle_id = _thread(
        session_factory,
        chat_id=97,
        message_id=6601,
        status="holding",
        signal_at=NOW - timedelta(days=31),
    )
    current_id = _current_message(
        session_factory, chat_id=97, message_id=6602, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        holding_lifecycle_id
    ]
    assert candidates[0].status == "holding"


def test_stale_pending_entry_survives_when_the_current_message_has_no_posted_at(
    tmp_path,
):
    """No timestamp on the incoming message means no age comparison exists.

    A missing ``posted_at`` is not evidence that anything is stale, so the
    filter fails open -- and so does the ``recent_active_thread`` bonus, which
    has always treated a null the same way.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    _, lifecycle_id = _thread(
        session_factory,
        chat_id=93,
        message_id=6301,
        status="pending_entry",
        signal_at=NOW - timedelta(days=40),
    )
    current_id = _current_message(
        session_factory, chat_id=93, message_id=6302, posted_at=None
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [lifecycle_id]
    assert "recent_active_thread" in candidates[0].reasons


def test_stale_pending_entry_with_a_resting_entry_leg_stays_in_the_set(tmp_path):
    """Condition 3 is the last gate: exchange exposure beats any age.

    Forty days old, so conditions 1 and 2 both hold; the resting entry leg is
    the only reason it must still be offered.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    _, bound_lifecycle_id = _thread(
        session_factory,
        chat_id=94,
        message_id=6401,
        status="pending_entry",
        signal_at=NOW - timedelta(days=40),
        with_binding=True,
    )
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, bound_lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=0,
                purpose="entry",
                order_kind="limit",
                order_id="resting-entry-order",
                attribution_status="unassigned",
                status="open",
            )
        )
        session.commit()
    current_id = _current_message(
        session_factory, chat_id=94, message_id=6402, posted_at=NOW
    )

    candidates = _candidates(
        session_factory, raw_message_id=current_id, symbol="BTC", side="long"
    )

    assert [candidate.lifecycle_id for candidate in candidates] == [
        bound_lifecycle_id
    ]
    assert candidates[0].risk_state == "current_risk"
    assert candidates[0].pending_entry_leg_ids != ()


def test_replay_of_raw_message_19030_no_longer_offers_the_month_old_eth_long(
    tmp_path,
):
    """Equivalent replay of the 2026-09-25 production miss.

    Production shape: a management message arrives in a group whose *only* ETH
    long candidate is a ``pending_entry`` signalled over a month earlier with
    no execution binding (lifecycle 909, 2026-08-20, long since
    ``expiry_review_requested``). The model did not guess -- it picked the one
    thing the contract offered it. After A1 the contract offers nothing, which
    is the answer that makes the caller ask a person.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    _, zombie_lifecycle_id = _thread(
        session_factory,
        chat_id=95,
        message_id=8200,
        status="pending_entry",
        signal_at=NOW - timedelta(days=36),
        symbol="ETH",
        side="long",
    )
    with session_factory() as session:
        zombie = session.get(StrategyLifecycle, zombie_lifecycle_id)
        zombie.management_action = "expiry_review_requested"
        zombie.expiry_review_notified_at = NOW - timedelta(days=36)
        session.commit()
    current_id = _current_message(
        session_factory, chat_id=95, message_id=9030, posted_at=NOW
    )

    with session_factory() as session:
        zombie = session.get(StrategyLifecycle, zombie_lifecycle_id)
        # Still present, still nominally active: marking it ``expired`` would
        # not have removed it either, because ``expired`` is in
        # ``ACTIVE_LIFECYCLE_STATUSES``. The age filter is what removes it.
        assert zombie.lifecycle_status == "pending_entry"
        assert zombie.execution_binding_id is None
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=current_id,
            symbol="ETH",
            side="long",
        )

    assert candidates == ()
