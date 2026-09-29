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


def test_cancel_entry_directive_requires_explicit_targets_for_fanout():
    directive = resolve_management_directive(
        text="BTC ETH 挂单全部取消",
        lifecycle_event={
            "event_type": "cancel_entry",
            "management_action": "cancel_pending_entry",
        },
    )

    assert directive.intent == "cancel_entry"
    assert directive.risk_reducing is True
    assert directive.fanout_allowed is False


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
    assert directive.fanout_allowed is False
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


@pytest.mark.parametrize("value", [0, -0.2, "150%", "1.5", "50", "oops", "NaN", "inf", False, [], {}])
def test_supplied_invalid_fraction_is_not_defaulted(value):
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text="减仓", lifecycle_event={
            "event_type": "position_update", "management_action": "partial_take_profit",
            "management_fraction": value,
        })


@pytest.mark.parametrize("text", ["减仓0%", "减仓-20%", "平仓150%", "保留150%",
    "剩余-20%", "留0%", "保留100%", "止盈abc%", "减仓1..5%", "减仓%"])
def test_invalid_body_percentage_is_not_dropped(text):
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text=text, lifecycle_event={
            "event_type": "position_update", "management_action": "partial_take_profit",
        })


@pytest.mark.parametrize("missing", [None, "", "  "])
def test_only_missing_fraction_keeps_half_default(missing):
    result = resolve_management_directive(text="减仓", lifecycle_event={
        "event_type": "position_update", "management_action": "partial_take_profit",
        "management_fraction": missing,
    })
    assert result.fraction == 0.5


def test_invalid_field_cannot_be_hidden_by_valid_field_or_half_text():
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text="减半", lifecycle_event={
            "management_action": "partial_take_profit", "fraction": "bad", "close_fraction": 0.5,
        })


@pytest.mark.parametrize("text", ["减仓 - 10%", "减仓 − 10%", "减仓 -\t10%", "减仓，比例120%", "减仓1,5%", "保留，比例150%"])
def test_percentage_separators_never_hide_invalid_content(text):
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text=text, lifecycle_event={"management_action": "partial_take_profit"})


def test_extreme_percent_is_normalized_to_fraction_rejection():
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text="减仓", lifecycle_event={"management_action": "partial_take_profit", "fraction": "1e9999999%"})


@pytest.mark.parametrize("text", ["减仓\n150%", "减仓；比例120%", "保留\n-20%", "减仓（150）%", "减仓 -\n10%"])
def test_multiline_or_punctuated_invalid_percent_does_not_become_missing(text):
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text=text, lifecycle_event={"management_action": "partial_take_profit"})


def test_percentage_is_bound_to_nearest_quantity_verb():
    result = resolve_management_directive(text="止盈位还差10点，减仓20%", lifecycle_event={"management_action": "partial_take_profit"})
    assert result.fraction == 0.2


@pytest.mark.parametrize("text", ["减仓10%%", "减仓20%至120%", "减仓比例20%/120%", "保留80%/150%"])
def test_malformed_continued_percentage_is_rejected(text):
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(text=text, lifecycle_event={"management_action": "partial_take_profit"})


def test_unrelated_market_percentage_does_not_change_close_fraction():
    result = resolve_management_directive(text="减仓20%，行情涨了150%", lifecycle_event={"management_action": "partial_take_profit"})
    assert result.fraction == 0.2


# --- 2026-09-28 audit fixes, problem 2: percent misbinding across sentences ---
# Raw text and the model's observed_text are joined with "\n" before fraction
# validation, so a verb in the raw text used to bind to a percent in the
# observed text (docs/plans/2026-09-28-audit-fixes-design.md section 2).

_R2_19597_RAW = (
    "目前BTC现价83800，加仓后浮盈600点，相当于正常仓位1200点收益，加仓后仓位比较大，"
    "减50%仓位，剩余仓位止损位上移至83200！\n@Tarderfengge QQ:158241758"
)
_R2_19597_OBSERVED = "BTC现价83800；加仓后浮盈600点；减50%仓位；剩余仓位止损上移至83200。"
_R2_19597_EVENT = {
    "event_type": "position_update",
    "management_action": "partial_take_profit,move_stop_to_protect",
    "side": "long",
    "stop_loss": "83200",
    "symbol": "BTC",
    "target_lifecycle_id": 1343,
    "confidence": 0.99,
}


def _r2_combined(raw: str, observed: str) -> str:
    from telegram_kol_research.message_recognition import (
        _authoritative_current_message_text,
    )

    return _authoritative_current_message_text(
        raw, {"input_reading": {"observed_text": observed}}
    )


def test_r2a_19597_partial_then_break_even_passes_both_gates() -> None:
    from telegram_kol_research.management_fraction_gate import (
        validate_management_fraction_payload,
    )

    combined = _r2_combined(_R2_19597_RAW, _R2_19597_OBSERVED)
    assert "\n" in combined and _R2_19597_OBSERVED in combined
    validate_management_fraction_payload({"lifecycle_event": _R2_19597_EVENT}, combined)

    for text in (_R2_19597_RAW, combined):
        directive = resolve_management_directive(
            text=text, lifecycle_event=_R2_19597_EVENT
        )
        assert directive.intent == "partial_then_break_even"
        assert directive.reason_code == "partial_then_break_even"
        assert directive.fraction == 0.5
        assert directive.stop_loss == "83200"
        assert directive.stop_price_source == "current_message_text"
        assert directive.risk_reducing is True


@pytest.mark.parametrize(
    ("raw", "observed", "action", "expected"),
    [
        (  # 17900
            "BTC多单目前获利600点，止盈40%，剩余仓位上移至80600，夜晚风险较大，做无风险持仓！"
            "\n@Tarderfengge QQ:158241758",
            "BTC多单获利600点，止盈40%，剩余仓位止损上移至80600，做无风险持仓；"
            "图片显示BTCUSDT行情，最新价81146.2。",
            "partial_take_profit",
            0.4,
        ),
        (  # 18153
            "恭喜跟上BTC多单的朋友，目前获利1100点，止盈60%，剩余仓位止损位上移至64100，做无风险持仓！"
            "\n@Tarderfengge QQ:158241758",
            "BTC多单获利1100点，止盈60%，剩余仓位止损位上移至64100，做无风险持仓。"
            "图片显示BTCUSDT行情，最新价85154.7。",
            "move_stop_to_protect",
            0.6,
        ),
    ],
    ids=["17900", "18153"],
)
def test_r2b_joined_raw_and_observed_keep_the_stated_fraction(
    raw, observed, action, expected
) -> None:
    from telegram_kol_research.management_fraction_gate import (
        validate_management_fraction_payload,
    )

    event = {
        "event_type": "position_update",
        "management_action": action,
        "side": "long",
        "symbol": "BTC",
    }
    combined = _r2_combined(raw, observed)
    validate_management_fraction_payload({"lifecycle_event": event}, combined)
    directive = resolve_management_directive(text=combined, lifecycle_event=event)
    assert directive.fraction == expected


def test_r2c_18603_full_exit_is_not_blocked_by_a_later_profit_percent() -> None:
    from telegram_kol_research.management_fraction_gate import (
        validate_management_fraction_payload,
    )

    raw = (
        "🔥直接触发止盈价！准不准？🔥\n🔥全部出局！全部出局！🔥\n🔥本轮空单最大获利95点！🔥\n"
        "🔥持仓收益高达370％！🔥\n🔥🔥🔥\n@Tarderfengge QQ:158241758"
    )
    event = {"event_type": "exit_full", "management_action": "exit_full", "symbol": "BTC"}
    validate_management_fraction_payload({"lifecycle_event": event}, raw)
    directive = resolve_management_directive(text=raw, lifecycle_event=event)
    assert directive.fraction != 3.7


def test_r2c_a_number_already_stated_before_the_comma_unbinds_the_percent() -> None:
    result = resolve_management_directive(
        text="平仓78031.7，盈利126.05%",
        lifecycle_event={"event_type": "exit_full", "management_action": "exit_full"},
    )
    assert result.fraction != 1.2605


@pytest.mark.parametrize("text", ["减仓-20%", "止盈50-60%", "保留120%", "剩余100%"])
def test_r2d_invalid_percentages_are_still_rejected(text) -> None:
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(
            text=text, lifecycle_event={"management_action": "partial_take_profit"}
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("止盈，70%", 0.7),
        ("减仓约30%", 0.3),
        ("分批止盈80%！留尾仓", 0.8),
        ("保留剩余30％冲击止盈位", 0.7),
    ],
)
def test_r2e_ordinary_wording_keeps_its_fraction(text, expected) -> None:
    result = resolve_management_directive(
        text=text, lifecycle_event={"management_action": "partial_take_profit"}
    )
    assert result.fraction == pytest.approx(expected)


def test_r2g_17936_join_is_a_hard_boundary_even_when_only_a_qq_number_sits_between() -> None:
    from telegram_kol_research.management_directives import (
        AUTHORITATIVE_TEXT_PART_SEPARATOR,
    )
    from telegram_kol_research.management_fraction_gate import (
        validate_management_fraction_payload,
    )

    raw = "一对一指导ZEC多单止盈70%保留底仓做成本保护。\n@Tarderfengge QQ:158241758"
    observed = "一对一指导ZEC多单止盈70%保留底仓做成本保护。"
    event = {
        "event_type": "position_update",
        "management_action": "partial_take_profit, move_stop_to_protect",
    }
    combined = _r2_combined(raw, observed)
    assert combined == raw + AUTHORITATIVE_TEXT_PART_SEPARATOR + observed

    validate_management_fraction_payload({"lifecycle_event": event}, combined)
    directive = resolve_management_directive(text=combined, lifecycle_event=event)
    # Same answer as the raw text alone gave before any of this: "保留底仓" is a
    # tail-retention term, and that branch precedes the percent branch and
    # closes DEFAULT_TAIL_CLOSE_FRACTION (0.8) rather than the stated 止盈70%.
    # Pre-existing semantics, not changed here.
    raw_only = resolve_management_directive(text=raw, lifecycle_event=event)
    assert (directive.intent, directive.fraction, directive.reason_code) == (
        raw_only.intent,
        raw_only.fraction,
        raw_only.reason_code,
    ) == ("partial_take_profit", DEFAULT_TAIL_CLOSE_FRACTION, "tail_retention")


def test_r2g_a_verb_never_claims_a_percent_across_the_join() -> None:
    combined = _r2_combined("保留仓位", "止盈70%")
    # Before the boundary, 保留 claimed 70% as a retained share (0.3 close),
    # contradicting 止盈70% (0.7 close) -> management_fraction_ambiguous.
    assert management_directives_module._retained_percentage_values(combined) == []
    assert management_directives_module._close_percentage_values(combined) == [0.7]
    result = resolve_management_directive(
        text=combined, lifecycle_event={"management_action": "partial_take_profit"}
    )
    assert result.fraction == 0.7


@pytest.mark.parametrize("raw", ["减仓\n150%", "减仓；比例120%", "保留\n-20%"])
def test_r2g_malformed_content_inside_one_part_is_still_rejected(raw) -> None:
    from telegram_kol_research.management_fraction_gate import (
        validate_management_fraction_payload,
    )

    event = {"management_action": "partial_take_profit"}
    for text in (raw, _r2_combined(raw, "BTC多单"), _r2_combined("BTC多单", raw)):
        with pytest.raises(ValueError, match="management_fraction_invalid"):
            resolve_management_directive(text=text, lifecycle_event=event)
        with pytest.raises(ValueError, match="management_fraction_invalid"):
            validate_management_fraction_payload({"lifecycle_event": event}, text)


def test_r2g_contact_scrubbing_cannot_blank_the_join() -> None:
    from telegram_kol_research.contact_digit_scrubbing import (
        scrub_contact_identifiers,
    )
    from telegram_kol_research.management_directives import (
        AUTHORITATIVE_TEXT_PART_SEPARATOR,
    )

    combined = "保留仓位 QQ:" + AUTHORITATIVE_TEXT_PART_SEPARATOR + "158241758 止盈70%"
    # Whatever the scrubber does to the whole string, the binding split
    # happens before it, so the verb in part one cannot reach part two.
    scrub_contact_identifiers(combined)
    assert management_directives_module._retained_percentage_values(combined) == []
    result = resolve_management_directive(
        text=combined, lifecycle_event={"management_action": "partial_take_profit"}
    )
    assert result.fraction == 0.7


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("平仓价78,136.4、盈利+136.13%", None),  # number before 、 -> unbound
        ("减仓、20%", 0.2),  # nothing numeric before 、 -> still binds
    ],
)
def test_r2g_enumeration_comma_is_a_clause_break(text, expected) -> None:
    result = resolve_management_directive(
        text=text,
        lifecycle_event={"event_type": "exit_full", "management_action": "exit_full"}
        if expected is None
        else {"management_action": "partial_take_profit"},
    )
    if expected is None:
        assert result.fraction != 1.3613
    else:
        assert result.fraction == expected


def test_r2g_enumeration_comma_with_no_number_keeps_invalid_content_rejected() -> None:
    with pytest.raises(ValueError, match="management_fraction_invalid"):
        resolve_management_directive(
            text="减仓、150%", lifecycle_event={"management_action": "partial_take_profit"}
        )


def test_r2f_add_position_wording_is_still_risk_increasing() -> None:
    result = resolve_management_directive(
        text="可以加仓同等仓位",
        lifecycle_event={"event_type": "position_update", "symbol": "BTC", "side": "long"},
    )
    assert result.reason_code == "risk_increasing_fanout_forbidden"
    assert result.fanout_allowed is False


def test_r2f_narrative_add_is_only_stripped_in_its_exact_form() -> None:
    narrative = resolve_management_directive(
        text="加仓后浮盈600点，减仓50%",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
    )
    assert narrative.reason_code != "risk_increasing_fanout_forbidden"
    assert narrative.fraction == 0.5
    for text in ("加仓后浮盈600点，可以加仓", "加仓了", "补仓"):
        result = resolve_management_directive(
            text=text, lifecycle_event={"event_type": "position_update"}
        )
        assert result.reason_code == "risk_increasing_fanout_forbidden"

# --- 2026-09-29 Mia design, M1: "止盈X%，剩余仓位止损位上移至P" -------------
#
# Production text and the model's *actual* top-level lifecycle_event payload
# (`_context_resolution.first_pass.lifecycle_event` is a separate, richer
# object the production code never reads for this decision -- only the
# top-level one, which is what these fixtures reproduce). Before M1, the
# model tags this class of message as a plain partial_take_profit with no
# stop_loss at all, so the directive never becomes composite and the price
# never gets a provenance tag -- confirmed against the pre-fix module
# (git show HEAD~n:.../management_directives.py) before this test was added,
# see the implementation report for the captured before/after transcript.

_M1_17900_RAW = (
    "BTC多单目前获利600点，止盈40%，剩余仓位上移至80600，夜晚风险较大，做无风险持仓！"
    "\n@Tarderfengge QQ:158241758"
)
_M1_17900_EVENT = {
    "confidence": 0.95,
    "event_type": "position_update",
    "management_action": "partial_take_profit",
    "reason": "当前消息明确管理已有的BTC多单策略（thread_id 619, lifecycle_id 1250），"
    "执行部分止盈40%并移动止损至80600，属于仓位管理操作，无冲突或新增风险。",
    "target_lifecycle_id": 1250,
}

_M1_17901_RAW = (
    "BTC多单目前获利600点，止盈40%，剩余仓位止损位上移至80600，夜晚风险较大，做无风险持仓！"
    "\n@Tarderfengge QQ:158241758"
)
_M1_17901_EVENT = dict(_M1_17900_EVENT, confidence=0.99, target_lifecycle_id=1250)

_M1_18154_RAW = (
    "恭喜跟上BTC多单的朋友，目前获利1100点，止盈60%，剩余仓位止损位上移至64100，做无风险持仓！"
    "\n@Tarderfengge QQ:158241758"
)
_M1_18154_EVENT = {
    "confidence": 0.99,
    "event_type": "position_update",
    "management_action": "partial_take_profit",
    "reason": "消息明确管理已有BTC多单（thread_id 629），执行部分止盈60%并移动止损至64100，"
    "属于降风险仓位管理动作。",
    "target_lifecycle_id": 1260,
}


@pytest.mark.parametrize(
    ("raw", "event", "expected_fraction", "expected_stop"),
    [
        (_M1_17900_RAW, _M1_17900_EVENT, 0.4, 80600.0),
        (_M1_17901_RAW, _M1_17901_EVENT, 0.4, 80600.0),
        (_M1_18154_RAW, _M1_18154_EVENT, 0.6, 64100.0),
    ],
    ids=["17900", "17901", "18154"],
)
def test_m1_partial_with_text_only_stop_move_becomes_composite(
    raw, event, expected_fraction, expected_stop
) -> None:
    # Before M1, resolve_management_directive returned a plain
    # partial_take_profit here with stop_loss=None (the model's payload
    # carries no stop_loss field for this class of message) -- captured with
    # the pre-fix module: intent="partial_take_profit", stop_loss=None,
    # stop_price_source=None, reason_code="partial_risk_reduction". This is
    # the positive half of the ARCHITECTURE.md #6 rule: assert what the
    # fixed gate produces, not merely that it differs.
    directive = resolve_management_directive(text=raw, lifecycle_event=event)
    assert directive.intent == "partial_then_break_even"
    assert directive.fraction == pytest.approx(expected_fraction)
    assert directive.stop_loss == expected_stop
    assert directive.stop_price_source == "current_message_text"

    contract = build_management_instruction_contract(text=raw, lifecycle_event=event)
    assert contract.stop_mode == "explicit_price"
    # ManagementInstructionContract.__post_init__ canonicalizes stop_price to
    # a decimal string.
    assert contract.stop_price == str(int(expected_stop))
    assert contract.stop_price_source == "current_message_text"


def test_m1_negative_no_price_stays_plain_partial_take_profit() -> None:
    # Same idiom, but the KOL never named a price: "剩余仓位继续持有" has no
    # move-price to extract, so the directive must not be upgraded to a
    # composite with an invented stop.
    directive = resolve_management_directive(
        text="止盈50%",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
    )
    assert directive.intent == "partial_take_profit"
    assert directive.stop_loss is None
    assert directive.stop_price_source is None


def test_m1_negative_bare_stop_move_without_fraction_stays_adjust_stop_loss() -> None:
    # "止损上移至83200" with no percentage is not this idiom at all (there is
    # no partial-take-profit clause, so M1's new branch never runs):
    # behaviour must stay exactly the pre-M1 adjust_stop_loss path, driven by
    # the model's own stop_loss field like every other adjust_stop_loss case.
    directive = resolve_management_directive(
        text="止损上移至83200",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "adjust_stop_loss",
            "stop_loss": "83200",
        },
    )
    assert directive.intent == "adjust_stop_loss"
    assert directive.stop_loss == "83200"
    assert directive.stop_price_source == "current_message_text"


def test_m1_negative_two_disagreeing_stop_moves_are_not_picked() -> None:
    # Two different explicit move-prices in the same message are ambiguous;
    # the directive must not silently pick either one.
    price = management_directives_module._management_stop_move_price(
        "止盈40%，剩余仓位止损位上移至80600，随后又改为止损下移至80000。"
    )
    assert price is None


def test_m1_r3_take_profit_heading_defect_is_not_widened() -> None:
    # docs/plans/2026-09-29-take-profit-adjustment-design.md R3: the existing
    # _extract_explicit_stop_loss_from_management_text pattern1 mis-reads the
    # "止盈止损" heading as a 止损 label and grabs the take-profit price on the
    # next line. M1's own extractor must not repeat that: it requires a
    # movement verb (上移/下移/移动/移至/挪动/调整), which this text has none
    # of, so it must return None, never 73070.
    price = management_directives_module._management_stop_move_price(
        "止盈止损\n止盈位：73070\n止损位：78700"
    )
    assert price is None
    # And the "剩余仓位" anchor must not claim a take-profit number either.
    assert (
        management_directives_module._management_stop_move_price(
            "剩余仓位……止盈位：2710 止损位：2624(成本价)"
        )
        is None
    )


