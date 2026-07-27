from __future__ import annotations

import pytest

from telegram_kol_research.management_directives import (
    DEFAULT_PARTIAL_CLOSE_FRACTION,
    DEFAULT_TAIL_CLOSE_FRACTION,
    resolve_management_directive,
)


def test_unspecified_partial_defaults_to_half() -> None:
    directive = resolve_management_directive(
        text="BTC多单止盈一部分，继续持有",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "partial_take_profit",
        },
    )

    assert directive.intent == "partial_take_profit"
    assert directive.fraction == DEFAULT_PARTIAL_CLOSE_FRACTION == 0.5
    assert directive.risk_reducing is True
    assert directive.fanout_allowed is True


def test_tail_and_optional_exit_choose_tail_reduction() -> None:
    directive = resolve_management_directive(
        text="建议只留一点尾仓，求稳也可以出局",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert directive.intent == "partial_take_profit"
    assert directive.fraction == DEFAULT_TAIL_CLOSE_FRACTION == 0.8
    assert directive.cancel_deferred_entries is True
    assert directive.reason_code == "tail_retention_preferred_over_optional_exit"


@pytest.mark.parametrize(
    ("text", "event", "expected"),
    [
        (
            "BTC多单减半，继续持有",
            {"event_type": "position_update", "symbol": "BTC", "side": "long"},
            0.5,
        ),
        (
            "BTC多单止盈30%",
            {"event_type": "position_update", "symbol": "BTC", "side": "long"},
            0.3,
        ),
        (
            "BTC多单保留25%底仓",
            {"event_type": "position_update", "symbol": "BTC", "side": "long"},
            0.75,
        ),
    ],
)
def test_partial_fraction_normalization(
    text: str, event: dict[str, object], expected: float
) -> None:
    directive = resolve_management_directive(text=text, lifecycle_event=event)

    assert directive.intent == "partial_take_profit"
    assert directive.fraction == expected
    assert directive.cancel_deferred_entries is True


def test_partial_then_break_even_defaults_to_half() -> None:
    directive = resolve_management_directive(
        text="移动止盈，止损移动到开仓价",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "partial_take_profit, move_stop_to_protect",
        },
    )

    assert directive.intent == "partial_then_break_even"
    assert directive.fraction == 0.5
    assert directive.fanout_allowed is True


def test_break_even_is_risk_reducing_but_add_position_is_not() -> None:
    break_even = resolve_management_directive(
        text="BTC多单修改止损到成本保护",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
        },
    )
    add_position = resolve_management_directive(
        text="BTC多单再加仓一半",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "add_position",
        },
    )

    assert break_even.intent == "move_stop_to_break_even"
    assert break_even.risk_reducing is True
    assert break_even.fanout_allowed is True
    assert add_position.risk_reducing is False
    assert add_position.fanout_allowed is False


def test_unresolved_risk_update_is_not_marked_safe_for_fanout() -> None:
    directive = resolve_management_directive(
        text="BTC多单修改止损到成本保护",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "risk_update",
        },
    )

    assert directive.intent == "adjust_stop_loss"
    assert directive.risk_reducing is False
    assert directive.fanout_allowed is False


def test_mixed_reduce_then_add_message_fails_closed_for_fanout() -> None:
    directive = resolve_management_directive(
        text="BTC多单先止盈一半，然后继续加仓",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "partial_take_profit",
        },
    )

    assert directive.risk_reducing is False
    assert directive.fanout_allowed is False
    assert directive.reason_code == "risk_increasing_fanout_forbidden"


def test_full_exit_and_cancel_entry_are_risk_reducing() -> None:
    full_exit = resolve_management_directive(
        text="BTC多单全部止盈出局",
        lifecycle_event={
            "event_type": "exit_position",
            "symbol": "BTC",
            "side": "long",
        },
    )
    cancel_entry = resolve_management_directive(
        text="策略先取消，等回调到位再派",
        lifecycle_event={
            "event_type": "cancel_entry",
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert full_exit.intent == "full_exit"
    assert full_exit.cancel_deferred_entries is True
    assert cancel_entry.intent == "cancel_entry"
    assert cancel_entry.cancel_deferred_entries is True


def test_cancel_pending_orders_wording_is_a_cancel_entry_directive() -> None:
    directive = resolve_management_directive(
        text="已经有入场的继续拿着，取消挂单",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "strategy_thread_id": 42,
        },
    )

    assert directive.intent == "cancel_entry"
    assert directive.cancel_deferred_entries is True
    assert directive.fanout_allowed is False
    assert directive.strategy_thread_id == 42


def test_commentary_and_optional_new_short_do_not_become_actions() -> None:
    directive = resolve_management_directive(
        text="激进的可以在6.5万附近做空，个人会再观察",
        lifecycle_event={
            "event_type": "none",
            "symbol": "BTC",
            "side": "short",
        },
    )

    assert directive.intent == "none"
    assert directive.risk_reducing is False
    assert directive.fanout_allowed is False


def test_conflicting_explicit_partial_fractions_are_rejected() -> None:
    with pytest.raises(ValueError, match="management_fraction_ambiguous"):
        resolve_management_directive(
            text="BTC多单止盈30%，保留50%",
            lifecycle_event={
                "event_type": "position_update",
                "symbol": "BTC",
                "side": "long",
            },
        )
