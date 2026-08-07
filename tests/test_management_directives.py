from __future__ import annotations

import pytest

from telegram_kol_research import management_directives as management_directives_module
from telegram_kol_research.management_directives import (
    DEFAULT_PARTIAL_CLOSE_FRACTION,
    DEFAULT_TAIL_CLOSE_FRACTION,
    build_management_instruction_contract,
    resolve_management_directive,
)


@pytest.mark.parametrize(
    "action",
    [
        "partial_take_profit",
        "exit_full",
        "exit_partial",
        "cancel_pending_entry",
    ],
)
def test_closed_multi_target_policy_allows_independent_risk_reductions(action):
    assert hasattr(management_directives_module, "multi_target_action_policy")
    policy = management_directives_module.multi_target_action_policy(action)

    assert policy.risk_reducing is True
    assert policy.fanout_allowed is True


@pytest.mark.parametrize(
    "action",
    ["add_position", "reverse", "revise_entry", "replace_shared_stop"],
)
def test_closed_multi_target_policy_rejects_unsafe_actions(action):
    assert hasattr(management_directives_module, "multi_target_action_policy")
    policy = management_directives_module.multi_target_action_policy(action)

    assert policy.fanout_allowed is False


def test_cancel_entry_directive_is_safe_for_independent_targets():
    directive = resolve_management_directive(
        text="BTC ETH 挂单全部取消",
        lifecycle_event={
            "event_type": "cancel_entry",
            "management_action": "cancel_pending_entry",
        },
    )

    assert directive.intent == "cancel_entry"
    assert directive.risk_reducing is True
    assert directive.fanout_allowed is True


def test_structured_partial_exit_uses_its_bounded_fraction():
    directive = resolve_management_directive(
        text="",
        lifecycle_event={
            "event_type": "exit_position",
            "management_action": "exit_partial",
            "management_fraction": 0.25,
        },
    )

    assert directive.intent == "partial_take_profit"
    assert directive.fraction == pytest.approx(0.25)
    assert directive.fanout_allowed is True


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


def test_partial_profit_mixed_with_add_position_is_not_fanout_safe() -> None:
    directive = resolve_management_directive(
        text="BTC ETH空单止盈一部分并加仓",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
    )

    assert directive.risk_reducing is False
    assert directive.fanout_allowed is False
    assert directive.reason_code == "risk_increasing_fanout_forbidden"


def test_miya_partial_with_explicit_stop_preserves_all_components():
    contract = build_management_instruction_contract(
        text="BTC多单目前浮盈1100点，止盈50%，剩余仓位止损位移动至62700，做无风险持仓",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "stop_loss": "62700",
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert contract.close_fraction == "0.5"
    assert contract.stop_mode == "explicit_price"
    assert contract.stop_price == "62700"
    assert contract.take_profit_consumption == "consume_first_stage"
    assert contract.required_components == (
        "consume_take_profit_stage",
        "converge_partial_close",
        "replace_remaining_protection",
    )


def test_sanjie_partial_to_entry_preserves_all_components():
    contract = build_management_instruction_contract(
        text="比特币多单止盈50%，止损位移动至开仓价！",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert contract.close_fraction == "0.5"
    assert contract.stop_mode == "actual_entry_price"
    assert contract.required_components == (
        "consume_take_profit_stage",
        "converge_partial_close",
        "replace_remaining_protection",
    )


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


@pytest.mark.parametrize(
    "action",
    ["exit_full", "full_exit", "close_position"],
)
def test_structured_full_exit_action_survives_position_update_alias(
    action: str,
) -> None:
    directive = resolve_management_directive(
        text="",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": action,
            "symbol": "BTC",
            "side": "short",
            "reason": "BTC 空单成本价附近出局",
        },
    )

    assert directive.intent == "full_exit"
    assert directive.risk_reducing is True
    assert directive.cancel_deferred_entries is True


def test_holding_language_near_cost_is_not_a_close() -> None:
    directive = resolve_management_directive(
        text="BTC空单目前成本价附近，继续拿着",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "short",
        },
    )

    assert directive.intent == "none"


def test_partial_protective_language_is_not_a_full_close() -> None:
    directive = resolve_management_directive(
        text="BTC空单减仓一半，剩余仓位保护成本",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "short",
        },
    )

    assert directive.intent == "partial_then_break_even"
    assert directive.intent != "full_exit"


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
    assert directive.fanout_allowed is True
    assert directive.strategy_thread_id == 42


@pytest.mark.parametrize("source", ["image", "historical_context"])
def test_break_even_ignores_non_current_message_stop_price(source: str) -> None:
    directive = resolve_management_directive(
        text="有入场的移动到保本",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_break_even",
            "stop_loss": "63600",
            "stop_price_source": source,
        },
    )

    assert directive.intent == "move_stop_to_break_even"
    assert directive.stop_loss is None
    assert directive.stop_price_source is None


def test_break_even_accepts_explicit_current_message_stop_price() -> None:
    directive = resolve_management_directive(
        text="有入场的移动保护到 64500",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_break_even",
            "stop_loss": "64500",
            "stop_price_source": "current_message_text",
        },
    )

    assert directive.intent == "adjust_stop_loss"
    assert directive.stop_loss == "64500"
    assert directive.stop_price_source == "current_message_text"


def test_explicit_stop_price_overrides_generic_protection_action() -> None:
    directive = resolve_management_directive(
        text="BTC市价62600附近，止损下移动500点，调整61900。",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_protect",
            "stop_loss": 61900.0,
        },
    )

    assert directive.intent == "adjust_stop_loss"
    assert directive.stop_loss == "61900.0"
    assert directive.stop_price_source == "current_message_text"


def test_market_quote_is_not_proof_of_explicit_stop_price() -> None:
    directive = resolve_management_directive(
        text="BTC市价61900附近，移动止损到成本价。",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_protect",
            "stop_loss": 61900,
            "stop_price_source": "current_message_text",
        },
    )

    assert directive.intent == "move_stop_to_break_even"
    assert directive.stop_loss is None
    assert directive.stop_price_source is None


def test_explicit_full_exit_precedes_stop_adjustment() -> None:
    directive = resolve_management_directive(
        text="止损调到61900，随后全部平仓。",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_protect",
            "stop_loss": 61900,
        },
    )

    assert directive.intent == "full_exit"


@pytest.mark.parametrize(
    "text",
    [
        "BTC 61900止损，移动保护。",
        "BTC 调整到61900作为止损，移动保护。",
        "BTC move stop to 61900",
    ],
)
def test_price_first_explicit_stop_never_becomes_break_even(text: str) -> None:
    directive = resolve_management_directive(
        text=text,
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "move_stop_to_protect",
            "stop_loss": 61900,
        },
    )

    assert directive.intent == "adjust_stop_loss"
    assert directive.stop_loss == "61900"
    assert directive.stop_price_source == "current_message_text"


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
