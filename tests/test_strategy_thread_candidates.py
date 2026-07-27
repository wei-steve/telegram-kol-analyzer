from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, StrategyLifecycle
from telegram_kol_research.strategy_thread_candidates import (
    generate_strategy_thread_candidates,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
    link_message_to_strategy_thread,
)


NOW = datetime(2026, 7, 27, 8, tzinfo=UTC)


def _add_strategy(
    session_factory,
    *,
    chat_id: int,
    message_id: int,
    symbol: str = "BTC",
    side: str = "long",
    status: str = "pending_entry",
    entry_low: float = 65000,
    entry_high: float = 65500,
    signal_minutes_ago: int = 30,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{symbol} {side}",
            posted_at=NOW - timedelta(minutes=signal_minutes_ago),
        )
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side=side,
            lifecycle_status=status,
            signal_at=NOW - timedelta(minutes=signal_minutes_ago),
            entry_range_low=entry_low,
            entry_range_high=entry_high,
        )
        session.add_all([raw, lifecycle])
        session.commit()
        return raw.id, lifecycle.id


def test_revision_language_ranks_matching_recent_active_thread(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    root_raw_id, lifecycle_id = _add_strategy(
        session_factory,
        chat_id=77,
        message_id=1460,
    )
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=thread.id,
        raw_message_id=root_raw_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    with session_factory() as session:
        current = RawMessage(
            chat_id=77,
            message_id=1462,
            text="更新 BTC 多单，入场 65100-65400",
            posted_at=NOW,
        )
        session.add(current)
        session.commit()
        current_id = current.id

    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=current_id,
            symbol="BTC",
            side="long",
            entry_range_low=65100,
            entry_range_high=65400,
        )

    assert candidates[0].thread_id == thread.id
    assert candidates[0].reasons == (
        "same_chat",
        "same_symbol",
        "same_side",
        "revision_language",
        "overlapping_entry",
        "recent_active_thread",
    )


def test_direct_reply_ranks_ahead_of_temporal_similarity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    old_raw_id, old_lifecycle_id = _add_strategy(
        session_factory,
        chat_id=78,
        message_id=100,
        signal_minutes_ago=120,
    )
    _, recent_lifecycle_id = _add_strategy(
        session_factory,
        chat_id=78,
        message_id=101,
        signal_minutes_ago=5,
    )
    old_thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=old_lifecycle_id,
    )
    create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=recent_lifecycle_id,
    )
    link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=old_thread.id,
        raw_message_id=old_raw_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    with session_factory() as session:
        current = RawMessage(
            chat_id=78,
            message_id=102,
            text="策略先取消",
            reply_to_message_id=100,
            posted_at=NOW,
        )
        session.add(current)
        session.commit()
        current_id = current.id

    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=current_id,
            symbol="BTC",
            side="long",
        )

    assert candidates[0].thread_id == old_thread.id
    assert candidates[0].reasons[0] == "direct_reply_link"


def test_contradictory_symbol_or_side_and_exited_threads_are_excluded(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, btc_long_id = _add_strategy(
        session_factory,
        chat_id=79,
        message_id=200,
    )
    _, eth_long_id = _add_strategy(
        session_factory,
        chat_id=79,
        message_id=201,
        symbol="ETH",
    )
    _, btc_short_id = _add_strategy(
        session_factory,
        chat_id=79,
        message_id=202,
        side="short",
    )
    _, exited_id = _add_strategy(
        session_factory,
        chat_id=79,
        message_id=203,
        status="exited",
    )
    expected = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=btc_long_id,
    )
    for lifecycle_id in (eth_long_id, btc_short_id, exited_id):
        create_strategy_thread_for_lifecycle(
            session_factory,
            lifecycle_id=lifecycle_id,
        )
    with session_factory() as session:
        current = RawMessage(
            chat_id=79,
            message_id=204,
            text="更新 BTC 多单",
            posted_at=NOW,
        )
        session.add(current)
        session.commit()
        current_id = current.id

    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=current_id,
            symbol="BTC",
            side="long",
        )

    assert [item.thread_id for item in candidates] == [expected.id]


def test_thread_and_message_link_helpers_are_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, lifecycle_id = _add_strategy(
        session_factory,
        chat_id=80,
        message_id=300,
    )

    first = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    repeated = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    first_link = link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=first.id,
        raw_message_id=raw_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )
    repeated_link = link_message_to_strategy_thread(
        session_factory,
        strategy_thread_id=first.id,
        raw_message_id=raw_id,
        relation_kind="root",
        resolver="deterministic",
        confidence=1.0,
        decision_version="v1",
    )

    assert repeated.id == first.id
    assert repeated_link.id == first_link.id
