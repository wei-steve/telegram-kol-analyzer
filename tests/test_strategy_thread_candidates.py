from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
)
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


def test_candidate_marks_entered_lifecycle_without_current_risk_as_inactive(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        current_message = RawMessage(
            chat_id=81,
            message_id=4168,
            text="63100没站稳，求稳就找机会出局",
            posted_at=NOW,
        )
        old_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:81:4139:BTC:long",
            kol_id="group:81",
            chat_id=81,
            message_id=4139,
            symbol="BTC",
            side="long",
            status="active",
            pos_id="pos-old-compatibility-only",
        )
        current_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:81:4167:BTC:long",
            kol_id="group:81",
            chat_id=81,
            message_id=4167,
            symbol="BTC",
            side="long",
            status="active",
        )
        session.add_all([current_message, old_binding, current_binding])
        session.flush()
        old_lifecycle = StrategyLifecycle(
            chat_id=81,
            message_id=4139,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=12),
            execution_binding_id=old_binding.id,
        )
        current_lifecycle = StrategyLifecycle(
            chat_id=81,
            message_id=4167,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(minutes=10),
            execution_binding_id=current_binding.id,
        )
        session.add_all([old_lifecycle, current_lifecycle])
        session.flush()
        old_leg = ExecutionOrderLeg(
            execution_binding_id=old_binding.id,
            strategy_instance_id=old_binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="order-old",
            pos_id="pos-old",
            attribution_status="verified",
            status="manually_closed",
            terminal_reason="manual_position_missing",
            last_verified_at=NOW - timedelta(hours=1),
        )
        live_leg = ExecutionOrderLeg(
            execution_binding_id=current_binding.id,
            strategy_instance_id=current_binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="order-current",
            pos_id="pos-current",
            attribution_status="verified",
            status="active",
            last_verified_at=NOW,
        )
        pending_leg = ExecutionOrderLeg(
            execution_binding_id=current_binding.id,
            strategy_instance_id=current_binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-current",
            attribution_status="unassigned",
            status="submitted",
        )
        session.add_all([old_leg, live_leg, pending_leg])
        session.commit()
        raw_message_id = current_message.id
        old_lifecycle_id = old_lifecycle.id
        current_lifecycle_id = current_lifecycle.id
        pending_leg_id = pending_leg.id

    create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=old_lifecycle_id,
    )
    create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=current_lifecycle_id,
    )

    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=raw_message_id,
            symbol="BTC",
            side="long",
        )

    by_lifecycle = {candidate.lifecycle_id: candidate for candidate in candidates}
    old = by_lifecycle[old_lifecycle_id]
    current = by_lifecycle[current_lifecycle_id]
    assert old.risk_state == "no_current_risk"
    assert old.live_verified_pos_ids == ()
    assert old.pending_entry_leg_ids == ()
    assert old.uncertain_entry_leg_ids == ()
    assert current.risk_state == "current_risk"
    assert current.live_verified_pos_ids == ("pos-current",)
    assert current.pending_entry_leg_ids == (pending_leg_id,)
    assert current.uncertain_entry_leg_ids == ()
