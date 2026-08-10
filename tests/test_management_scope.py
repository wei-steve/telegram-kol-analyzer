from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.management_directives import (
    ManagementDirective,
    resolve_management_directive,
)
from telegram_kol_research.management_scope import (
    ManagementScopeError,
    resolve_management_scope_in_session,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyThread,
)


NOW = datetime(2026, 7, 26, 10, tzinfo=UTC)


def _persist_live_strategy(
    session,
    *,
    chat_id: int,
    message_id: int,
    symbol: str = "BTC",
    side: str = "long",
    lifecycle_status: str = "entered",
    binding_status: str = "active",
    attribution_status: str = "verified",
    pos_id: str,
    strategy_thread_id: int | None = None,
) -> StrategyLifecycle:
    strategy_id = f"deepcoin:{chat_id}:{message_id}:{symbol}:{side}"
    binding = ExecutionBinding(
        strategy_instance_id=strategy_id,
        kol_id=f"group:{chat_id}",
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        venue="deepcoin",
        pos_id=pos_id,
        status=binding_status,
    )
    session.add(binding)
    session.flush()
    lifecycle = StrategyLifecycle(
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        lifecycle_status=lifecycle_status,
        signal_at=NOW,
        entered_at=NOW,
        execution_binding_id=binding.id,
        strategy_thread_id=strategy_thread_id,
    )
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=strategy_id,
        leg_index=1,
        purpose="entry",
        order_kind="market",
        order_id=f"order-{message_id}",
        pos_id=pos_id,
        status="active",
        attribution_status=attribution_status,
    )
    session.add_all([lifecycle, leg])
    session.flush()
    return lifecycle


def _break_even_directive() -> ManagementDirective:
    return resolve_management_directive(
        text="BTC多单修改止损到成本保护",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
        },
    )


def test_reply_target_wins_over_group_fanout(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        first = _persist_live_strategy(
            session, chat_id=88, message_id=1, pos_id="pos-1"
        )
        _persist_live_strategy(
            session, chat_id=88, message_id=2, pos_id="pos-2"
        )
        message = RawMessage(chat_id=88, message_id=3, text="BTC多单成本保护")
        session.add(message)
        session.flush()

        targets = resolve_management_scope_in_session(
            session,
            raw_message=message,
            directive=_break_even_directive(),
            explicit_target_lifecycle_id=None,
            reply_target_lifecycle_id=first.id,
        )

    assert [row.lifecycle_id for row in targets] == [first.id]
    assert targets[0].scope_source == "reply"


def test_unscoped_break_even_fans_out_same_chat_symbol_and_side(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        thread = StrategyThread(
            chat_id=88,
            root_message_id=1,
            symbol="BTC",
            side="long",
        )
        session.add(thread)
        session.flush()
        first = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            strategy_thread_id=thread.id,
        )
        second = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=2,
            pos_id="pos-2",
            strategy_thread_id=thread.id,
        )
        message = RawMessage(chat_id=88, message_id=3, text="BTC多单成本保护")
        session.add(message)
        session.flush()

        targets = resolve_management_scope_in_session(
            session,
            raw_message=message,
            directive=_break_even_directive(),
            explicit_target_lifecycle_id=None,
            reply_target_lifecycle_id=None,
        )

    assert [row.lifecycle_id for row in targets] == [first.id, second.id]
    assert {row.scope_source for row in targets} == {"verified_group_fanout"}


def test_fanout_excludes_other_scope_and_unverified_binding(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        thread = StrategyThread(
            chat_id=88, root_message_id=1, symbol="BTC", side="long"
        )
        session.add(thread)
        session.flush()
        included = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            strategy_thread_id=thread.id,
        )
        _persist_live_strategy(
            session, chat_id=99, message_id=2, pos_id="pos-2"
        )
        _persist_live_strategy(
            session, chat_id=88, message_id=3, side="short", pos_id="pos-3"
        )
        _persist_live_strategy(
            session, chat_id=88, message_id=4, symbol="ETH", pos_id="pos-4"
        )
        _persist_live_strategy(
            session,
            chat_id=88,
            message_id=5,
            pos_id="pos-5",
            attribution_status="unassigned",
            strategy_thread_id=thread.id,
        )
        message = RawMessage(chat_id=88, message_id=6, text="BTC多单成本保护")
        session.add(message)
        session.flush()

        targets = resolve_management_scope_in_session(
            session,
            raw_message=message,
            directive=_break_even_directive(),
            explicit_target_lifecycle_id=None,
            reply_target_lifecycle_id=None,
        )

    assert [row.lifecycle_id for row in targets] == [included.id]


def test_fanout_is_limited_to_resolved_strategy_thread(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "thread-scope.db")
    with session_factory() as session:
        selected_thread = StrategyThread(
            chat_id=88,
            root_message_id=1,
            symbol="BTC",
            side="long",
        )
        other_thread = StrategyThread(
            chat_id=88,
            root_message_id=3,
            symbol="BTC",
            side="long",
        )
        session.add_all([selected_thread, other_thread])
        session.flush()
        first = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            strategy_thread_id=selected_thread.id,
        )
        second = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=2,
            pos_id="pos-2",
            strategy_thread_id=selected_thread.id,
        )
        _persist_live_strategy(
            session,
            chat_id=88,
            message_id=3,
            pos_id="pos-other-thread",
            strategy_thread_id=other_thread.id,
        )
        message = RawMessage(
            chat_id=88,
            message_id=4,
            text="有入场的止盈一半带保护",
        )
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
                "strategy_thread_id": selected_thread.id,
            },
        )

        targets = resolve_management_scope_in_session(
            session,
            raw_message=message,
            directive=directive,
            explicit_target_lifecycle_id=None,
            reply_target_lifecycle_id=None,
        )

    assert [target.lifecycle_id for target in targets] == [first.id, second.id]
    assert {target.strategy_thread_id for target in targets} == {
        selected_thread.id
    }


def test_fanout_without_unique_strategy_thread_fails_closed(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "ambiguous-thread.db")
    with session_factory() as session:
        first_thread = StrategyThread(
            chat_id=88, root_message_id=1, symbol="BTC", side="long"
        )
        second_thread = StrategyThread(
            chat_id=88, root_message_id=2, symbol="BTC", side="long"
        )
        session.add_all([first_thread, second_thread])
        session.flush()
        _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            strategy_thread_id=first_thread.id,
        )
        _persist_live_strategy(
            session,
            chat_id=88,
            message_id=2,
            pos_id="pos-2",
            strategy_thread_id=second_thread.id,
        )
        message = RawMessage(
            chat_id=88,
            message_id=3,
            text="有入场的止盈一半带保护",
        )
        session.add(message)
        session.flush()

        with pytest.raises(
            ManagementScopeError,
            match="management_scope_strategy_thread_ambiguous",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=resolve_management_directive(
                    text=message.text or "",
                    lifecycle_event={
                        "event_type": "position_update",
                        "symbol": "BTC",
                        "side": "long",
                    },
                ),
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )


def test_explicit_target_must_match_message_scope(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        target = _persist_live_strategy(
            session, chat_id=99, message_id=1, pos_id="pos-1"
        )
        message = RawMessage(chat_id=88, message_id=2, text="BTC多单成本保护")
        session.add(message)
        session.flush()

        with pytest.raises(ManagementScopeError, match="target_source_identity_mismatch"):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=_break_even_directive(),
                explicit_target_lifecycle_id=target.id,
                reply_target_lifecycle_id=None,
            )


def test_risk_increasing_action_never_fans_out(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _persist_live_strategy(
            session, chat_id=88, message_id=1, pos_id="pos-1"
        )
        message = RawMessage(chat_id=88, message_id=2, text="BTC多单再加仓一半")
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
                "management_action": "add_position",
            },
        )

        with pytest.raises(
            ManagementScopeError, match="risk_increasing_fanout_forbidden"
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )


def test_unscoped_cancel_entry_never_fans_out_to_entered_positions(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _persist_live_strategy(
            session, chat_id=88, message_id=1, pos_id="pos-1"
        )
        message = RawMessage(chat_id=88, message_id=2, text="BTC多单取消策略")
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "cancel_entry",
                "symbol": "BTC",
                "side": "long",
            },
        )

        with pytest.raises(
            ManagementScopeError, match="risk_increasing_fanout_forbidden"
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )


def test_fanout_excludes_positions_created_after_management_message(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = _persist_live_strategy(
            session, chat_id=88, message_id=2, pos_id="pos-2"
        )
        lifecycle.signal_at = NOW + timedelta(hours=1)
        message = RawMessage(
            chat_id=88,
            message_id=1,
            posted_at=NOW,
            text="BTC多单成本保护",
        )
        session.add(message)
        session.flush()

        with pytest.raises(
            ManagementScopeError,
            match="verified_group_management_target_not_found",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=_break_even_directive(),
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )


def test_long_stop_widening_cannot_fan_out(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = _persist_live_strategy(
            session, chat_id=88, message_id=1, pos_id="pos-1"
        )
        lifecycle.stop_loss = 64000
        message = RawMessage(chat_id=88, message_id=2, text="BTC多单止损改到60000")
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
                "management_action": "adjust_stop_loss",
                "stop_loss": 60000,
            },
        )

        with pytest.raises(
            ManagementScopeError,
            match="group_stop_adjustment_direction_not_verified",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )


@pytest.mark.parametrize(
    ("symbol", "side", "current_stop", "requested_stop", "limit"),
    [
        ("BTC", "long", 64000, 63300, 700),
        ("BTC", "short", 64000, 64700, 700),
        ("ETH", "long", 1900, 1879, 21),
        ("ETH", "short", 1900, 1921, 21),
    ],
)
def test_exact_verified_target_allows_bounded_stop_widening(
    tmp_path,
    symbol: str,
    side: str,
    current_stop: float,
    requested_stop: float,
    limit: float,
) -> None:
    session_factory = create_session_factory(tmp_path / f"{symbol}-{side}.db")
    with session_factory() as session:
        lifecycle = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            symbol=symbol,
            side=side,
            pos_id="pos-1",
        )
        lifecycle.stop_loss = current_stop
        message = RawMessage(
            chat_id=88,
            message_id=2,
            text=f"{symbol} {side} stop {requested_stop}",
        )
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": symbol,
                "side": side,
                "management_action": "adjust_stop_loss",
                "stop_loss": requested_stop,
            },
        )

        targets = resolve_management_scope_in_session(
            session,
            raw_message=message,
            directive=directive,
            explicit_target_lifecycle_id=lifecycle.id,
            reply_target_lifecycle_id=None,
        )

    assert [target.lifecycle_id for target in targets] == [lifecycle.id]
    assert abs(requested_stop - current_stop) == limit
    assert targets[0].scope_source == "explicit"


@pytest.mark.parametrize(
    ("symbol", "side", "current_stop", "requested_stop"),
    [
        ("BTC", "long", 64000, 63299.9),
        ("BTC", "short", 64000, 64700.1),
        ("ETH", "long", 1900, 1878.9),
        ("ETH", "short", 1900, 1921.1),
        ("SOL", "long", 150, 149.9),
        ("SOL", "short", 150, 150.1),
    ],
)
def test_exact_target_rejects_unbounded_or_unsupported_stop_widening(
    tmp_path,
    symbol: str,
    side: str,
    current_stop: float,
    requested_stop: float,
) -> None:
    session_factory = create_session_factory(tmp_path / f"{symbol}-{side}.db")
    with session_factory() as session:
        lifecycle = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            symbol=symbol,
            side=side,
            pos_id="pos-1",
        )
        lifecycle.stop_loss = current_stop
        message = RawMessage(
            chat_id=88,
            message_id=2,
            text=f"{symbol} {side} stop {requested_stop}",
        )
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": symbol,
                "side": side,
                "management_action": "adjust_stop_loss",
                "stop_loss": requested_stop,
            },
        )

        with pytest.raises(
            ManagementScopeError,
            match="stop_adjustment_direction_not_verified",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=lifecycle.id,
                reply_target_lifecycle_id=None,
            )


def test_exact_unverified_target_cannot_use_bounded_stop_widening(tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "unverified.db")
    with session_factory() as session:
        lifecycle = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            attribution_status="unassigned",
        )
        lifecycle.stop_loss = 63800
        message = RawMessage(chat_id=88, message_id=2, text="BTC止损改到63300")
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
                "management_action": "adjust_stop_loss",
                "stop_loss": 63300,
            },
        )

        with pytest.raises(
            ManagementScopeError,
            match="stop_widening_position_ownership_not_verified",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=lifecycle.id,
                reply_target_lifecycle_id=None,
            )


def test_group_stop_update_fails_closed_if_any_verified_target_would_widen(
    tmp_path,
) -> None:
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        thread = StrategyThread(
            chat_id=88, root_message_id=1, symbol="BTC", side="long"
        )
        session.add(thread)
        session.flush()
        tighter = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=1,
            pos_id="pos-1",
            strategy_thread_id=thread.id,
        )
        wider = _persist_live_strategy(
            session,
            chat_id=88,
            message_id=2,
            pos_id="pos-2",
            strategy_thread_id=thread.id,
        )
        tighter.stop_loss = 60000
        wider.stop_loss = 65000
        message = RawMessage(chat_id=88, message_id=3, text="BTC多单止损改到64000")
        session.add(message)
        session.flush()
        directive = resolve_management_directive(
            text=message.text or "",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
                "management_action": "adjust_stop_loss",
                "stop_loss": 64000,
            },
        )

        with pytest.raises(
            ManagementScopeError,
            match="group_stop_adjustment_direction_not_verified",
        ):
            resolve_management_scope_in_session(
                session,
                raw_message=message,
                directive=directive,
                explicit_target_lifecycle_id=None,
                reply_target_lifecycle_id=None,
            )
