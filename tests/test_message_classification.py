"""The ``message_classes`` contract, one case per cell of the design.

Specification: ``docs/plans/2026-09-24-first-pass-classification-contract-design.md``.
Every constraint in §2.1, every row of the §2.4 table, every row of the §4
derivation, and the §3.5 precondition gets at least one passing and one failing
case, because a one-sided test of a validator cannot tell "the rule holds" from
"the rule was never reached".
"""

from __future__ import annotations

import pytest

from telegram_kol_research.message_classification import (
    CLASS_IMAGE_UNREADABLE,
    CLASS_NEW_STRATEGY,
    CLASS_POSITION_MANAGEMENT,
    CLASS_SMALL_TALK,
    CLASS_STRATEGY_MANAGEMENT,
    MAX_MESSAGE_CLASSES,
    MESSAGE_CLASSES,
    MESSAGE_CLASS_VIOLATIONS,
    TARGET_RESOLUTIONS,
    compare_message_classes,
    derive_message_classes,
    parse_message_classes,
)


def _strategy(**overrides):
    strategy = {
        "symbol": "BTC",
        "side": "long",
        "entry": "58900-59300",
        "stop_loss": "57800",
        "take_profit": None,
    }
    strategy.update(overrides)
    return strategy


def _payload(classes, **overrides):
    payload = {
        "message_classes": classes,
        "recognition_result": "非策略",
        "strategy": None,
        "lifecycle_event": {"event_type": "none"},
        "input_reading": {"observed_text": "x", "image_quality": "none"},
    }
    payload.update(overrides)
    return payload


def _element(message_class, **target):
    return {"class": message_class, "target": target or None}


def _target(resolution, lifecycle_id=None, symbol=None, side=None):
    return {
        "resolution": resolution,
        "lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "side": side,
    }


def _violations(payload, **kwargs):
    return set(parse_message_classes(payload, **kwargs).violations)


# --------------------------------------------------------------------------
# The constants themselves (§2.2, §2.3)
# --------------------------------------------------------------------------


def test_the_five_class_values_and_three_resolutions_are_the_design_s():
    assert MESSAGE_CLASSES == (
        "新策略",
        "策略管理",
        "仓位管理",
        "闲话",
        "图片不可读",
    )
    assert TARGET_RESOLUTIONS == ("exact", "forthcoming", "unknown")


def test_every_violation_this_module_can_emit_is_in_the_closed_vocabulary():
    """A code that escapes ``MESSAGE_CLASS_VIOLATIONS`` is an unnamed failure."""

    bad_payloads = [
        _payload("not-a-list"),
        _payload([]),
        _payload([_element(CLASS_SMALL_TALK)] * 5),
        _payload([_element(CLASS_SMALL_TALK), _element(CLASS_NEW_STRATEGY)]),
        _payload(["nope"]),
        _payload([{"class": None}]),
        _payload([_element("新分类")]),
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": None}]),
        _payload([{"class": CLASS_SMALL_TALK, "target": _target("unknown")}]),
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": "x"}]),
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target(None)}]),
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("soon")}]),
        _payload(
            [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("forthcoming", symbol="BTC")}]
        ),
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact")}]),
        _payload(
            [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown", lifecycle_id=3)}]
        ),
        _payload(
            [{"class": CLASS_STRATEGY_MANAGEMENT, "target": _target("forthcoming")}]
        ),
        _payload([_element(CLASS_NEW_STRATEGY)], recognition_result="是策略"),
        _payload(
            [_element(CLASS_NEW_STRATEGY)],
            strategy=_strategy(stop_loss=None),
        ),
        _payload([_element(CLASS_SMALL_TALK)], strategy=_strategy()),
        _payload([_element(CLASS_IMAGE_UNREADABLE)]),
        _payload(
            [
                {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 9)},
                {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 9)},
            ]
        ),
    ]
    emitted: set[str] = set()
    for payload in bad_payloads:
        emitted |= _violations(
            payload,
            allowed_lifecycle_ids=[9],
        )
    emitted |= _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 99)}]),
        allowed_lifecycle_ids=[1],
    )

    assert emitted
    assert emitted <= MESSAGE_CLASS_VIOLATIONS


# --------------------------------------------------------------------------
# §8 compatibility: the field may be absent, and absence is not a violation
# --------------------------------------------------------------------------


def test_a_payload_without_the_field_is_missing_not_violating():
    """Rolling the prompt back to v8 must not look like a contract breach."""

    parsed = parse_message_classes(
        {"recognition_result": "非策略", "strategy": None, "lifecycle_event": {}}
    )

    assert parsed.present is False
    assert parsed.violations == ()
    assert parsed.elements == ()
    assert parsed.valid is False


def test_a_payload_with_the_field_and_no_broken_rule_is_valid():
    parsed = parse_message_classes(_payload([_element(CLASS_SMALL_TALK)]))

    assert parsed.present is True
    assert parsed.violations == ()
    assert parsed.valid is True
    assert parsed.to_payload() == [{"class": CLASS_SMALL_TALK, "target": None}]


def test_parsing_never_raises_on_hostile_input():
    for hostile in (None, {}, {"message_classes": 5}, {"message_classes": {"a": 1}}):
        parse_message_classes(hostile)


# --------------------------------------------------------------------------
# §2.1 list constraints, each with a passing and a failing case
# --------------------------------------------------------------------------


def test_the_list_must_be_a_non_empty_array():
    assert "classes_not_a_list" in _violations(_payload(None))
    assert "classes_not_a_list" in _violations(_payload({"class": CLASS_SMALL_TALK}))
    assert "classes_empty" in _violations(_payload([]))
    assert _violations(_payload([_element(CLASS_SMALL_TALK)])) == set()


def test_the_list_holds_at_most_four_elements():
    four = [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", index)}
        for index in range(1, 5)
    ]
    five = four + [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 5)}
    ]

    assert MAX_MESSAGE_CLASSES == 4
    assert "too_many_classes" not in _violations(_payload(four))
    assert "too_many_classes" in _violations(_payload(five))


@pytest.mark.parametrize("exclusive", [CLASS_SMALL_TALK, CLASS_IMAGE_UNREADABLE])
def test_small_talk_and_unreadable_image_must_stand_alone(exclusive):
    alone = _payload(
        [_element(exclusive)],
        input_reading={"observed_text": "x", "image_quality": "blurry"},
    )
    accompanied = _payload(
        [
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown")},
            _element(exclusive),
        ],
        input_reading={"observed_text": "x", "image_quality": "blurry"},
    )

    assert "exclusive_class_not_alone" not in _violations(alone)
    assert "exclusive_class_not_alone" in _violations(accompanied)


def test_a_class_repeats_only_when_its_target_differs():
    """「BTC 和 ETH 都平掉」＝两个仓位管理元素，同一个目标重复才是违规。"""

    distinct = _payload(
        [
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 11, "BTC")},
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 12, "ETH")},
        ]
    )
    identical = _payload(
        [
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 11, "BTC")},
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 11, "ETH")},
        ]
    )

    assert "duplicate_class_target" not in _violations(distinct)
    assert "duplicate_class_target" in _violations(identical)


def test_management_sorts_before_a_new_entry():
    ordered = _payload(
        [
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 3)},
            _element(CLASS_NEW_STRATEGY),
        ],
        recognition_result="是策略",
        strategy=_strategy(),
    )
    reversed_order = _payload(
        [
            _element(CLASS_NEW_STRATEGY),
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 3)},
        ],
        recognition_result="是策略",
        strategy=_strategy(),
    )

    assert "class_order_violation" not in _violations(ordered)
    assert "class_order_violation" in _violations(reversed_order)


def test_an_element_must_be_an_object_carrying_a_known_class():
    assert "element_not_an_object" in _violations(_payload(["仓位管理"]))
    assert "class_missing" in _violations(_payload([{"target": None}]))
    assert "class_unknown" in _violations(_payload([_element("识别失败")]))
    assert "class_unknown" not in _violations(_payload([_element(CLASS_SMALL_TALK)]))


# --------------------------------------------------------------------------
# §2.4 空值语义总表, one test per row
# --------------------------------------------------------------------------


def test_row_new_strategy_target_must_be_null_and_strategy_complete():
    good = _payload(
        [_element(CLASS_NEW_STRATEGY)],
        recognition_result="是策略",
        strategy=_strategy(),
    )
    with_target = _payload(
        [{"class": CLASS_NEW_STRATEGY, "target": _target("exact", 4)}],
        recognition_result="是策略",
        strategy=_strategy(),
    )

    assert _violations(good) == set()
    assert "target_not_allowed" in _violations(with_target)


@pytest.mark.parametrize("field", ["symbol", "side", "entry", "stop_loss"])
def test_row_new_strategy_requires_four_strategy_fields(field):
    payload = _payload(
        [_element(CLASS_NEW_STRATEGY)],
        recognition_result="是策略",
        strategy=_strategy(**{field: None}),
    )

    assert f"strategy_missing_{field}" in _violations(payload)


def test_row_new_strategy_tolerates_a_missing_take_profit():
    """§2.4 allows it and §3.1 makes the stop loss, not the target, the gate."""

    payload = _payload(
        [_element(CLASS_NEW_STRATEGY)],
        recognition_result="是策略",
        strategy=_strategy(take_profit=None),
    )

    assert _violations(payload) == set()


def test_row_new_strategy_without_a_strategy_object_is_a_violation():
    payload = _payload(
        [_element(CLASS_NEW_STRATEGY)], recognition_result="是策略", strategy=None
    )
    all_null = _payload(
        [_element(CLASS_NEW_STRATEGY)],
        recognition_result="是策略",
        strategy={"symbol": None, "side": None, "entry": None, "stop_loss": None},
    )

    assert "strategy_required" in _violations(payload)
    assert "strategy_required" in _violations(all_null)


def test_a_list_without_a_new_strategy_element_must_carry_no_strategy():
    """§2.4 "列表里没有 `新策略` 元素时，`strategy` 必须为 `null`"."""

    clean = _payload([_element(CLASS_SMALL_TALK)], strategy=None)
    all_null_is_the_prompt_s_own_null_shape = _payload(
        [_element(CLASS_SMALL_TALK)],
        strategy={"symbol": None, "side": None, "entry": None},
    )
    populated = _payload([_element(CLASS_SMALL_TALK)], strategy=_strategy())

    assert "strategy_not_allowed" not in _violations(clean)
    assert "strategy_not_allowed" not in _violations(
        all_null_is_the_prompt_s_own_null_shape
    )
    assert "strategy_not_allowed" in _violations(populated)


@pytest.mark.parametrize("resolution", ["exact", "forthcoming", "unknown"])
def test_row_strategy_management_accepts_all_three_resolutions(resolution):
    target = {
        "exact": _target("exact", 21),
        "forthcoming": _target("forthcoming", symbol="BTC"),
        "unknown": _target("unknown"),
    }[resolution]
    payload = _payload(
        [{"class": CLASS_STRATEGY_MANAGEMENT, "target": target}],
        entry_fragments=[{"kind": "risk_multiplier", "risk_multiplier": "0.5"}],
    )

    assert _violations(payload, allowed_lifecycle_ids=[21]) == set()


def test_row_strategy_management_target_may_not_be_null():
    payload = _payload([{"class": CLASS_STRATEGY_MANAGEMENT, "target": None}])

    assert "target_required" in _violations(payload)


def test_row_position_management_may_not_use_forthcoming():
    """持仓不可能还没发生。"""

    allowed = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown")}]
    )
    refused = _payload(
        [
            {
                "class": CLASS_POSITION_MANAGEMENT,
                "target": _target("forthcoming", symbol="BTC"),
            }
        ]
    )

    assert "forthcoming_not_allowed_for_position" not in _violations(allowed)
    assert "forthcoming_not_allowed_for_position" in _violations(refused)


def test_row_position_management_target_may_not_be_null():
    assert "target_required" in _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": None}])
    )
    assert "target_required" not in _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown")}])
    )


@pytest.mark.parametrize("exclusive", [CLASS_SMALL_TALK, CLASS_IMAGE_UNREADABLE])
def test_rows_small_talk_and_unreadable_image_forbid_target_and_strategy(exclusive):
    reading = {"observed_text": "x", "image_quality": "blurry"}
    clean = _payload([_element(exclusive)], input_reading=reading)
    with_target = _payload(
        [{"class": exclusive, "target": _target("unknown")}], input_reading=reading
    )
    with_strategy = _payload(
        [_element(exclusive)], input_reading=reading, strategy=_strategy()
    )

    assert _violations(clean) == set()
    assert "target_not_allowed" in _violations(with_target)
    assert "strategy_not_allowed" in _violations(with_strategy)


# --------------------------------------------------------------------------
# §2.3 target internals
# --------------------------------------------------------------------------


def test_resolution_is_required_and_closed():
    assert "resolution_missing" in _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target(None)}])
    )
    assert "resolution_unknown" in _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("maybe")}])
    )
    assert _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown")}])
    ) == set()


def test_a_target_that_is_neither_null_nor_an_object_is_rejected():
    assert "target_not_an_object" in _violations(
        _payload([{"class": CLASS_POSITION_MANAGEMENT, "target": "策略 7"}])
    )


def test_lifecycle_id_belongs_to_exact_and_only_to_exact():
    exact_without = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact")}]
    )
    exact_with = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 13)}]
    )
    unknown_with = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown", 13)}]
    )

    assert "lifecycle_id_required" in _violations(exact_without)
    assert _violations(exact_with) == set()
    assert "lifecycle_id_not_allowed" in _violations(unknown_with)


def test_an_exact_lifecycle_id_outside_the_candidate_set_is_a_violation():
    payload = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 44)}]
    )

    assert _violations(payload, allowed_lifecycle_ids=[44]) == set()
    assert "lifecycle_id_outside_candidate_set" in _violations(
        payload, allowed_lifecycle_ids=[43]
    )
    # Without a candidate set the rule is simply not checked, rather than
    # silently deciding every id is wrong.
    assert _violations(payload) == set()


def test_forthcoming_needs_a_symbol_and_something_to_carry():
    """§2.3 symbol, and §2.4's last bullet on entry_context / entry_fragments."""

    complete = _payload(
        [
            {
                "class": CLASS_STRATEGY_MANAGEMENT,
                "target": _target("forthcoming", symbol="BTC"),
            }
        ],
        entry_context={"kind": "entry_preamble", "risk_multiplier": "0.5"},
    )
    without_symbol = _payload(
        [{"class": CLASS_STRATEGY_MANAGEMENT, "target": _target("forthcoming")}],
        entry_context={"kind": "entry_preamble", "risk_multiplier": "0.5"},
    )
    without_carrier = _payload(
        [
            {
                "class": CLASS_STRATEGY_MANAGEMENT,
                "target": _target("forthcoming", symbol="BTC"),
            }
        ]
    )

    assert _violations(complete) == set()
    assert "forthcoming_symbol_required" in _violations(without_symbol)
    assert "forthcoming_without_entry_context" in _violations(without_carrier)


# --------------------------------------------------------------------------
# §3.5 the 图片不可读 precondition
# --------------------------------------------------------------------------


@pytest.mark.parametrize("quality", ["blurry", "cropped", "unreadable", "clear"])
def test_unreadable_image_is_allowed_when_the_message_actually_carries_one(quality):
    payload = _payload(
        [_element(CLASS_IMAGE_UNREADABLE)],
        input_reading={"observed_text": "", "image_quality": quality},
    )

    assert "image_unreadable_without_image" not in _violations(payload)


def test_unreadable_image_on_a_text_only_message_is_a_violation():
    """纯文字消息永远不可能是 图片不可读（image_quality = none）。"""

    payload = _payload(
        [_element(CLASS_IMAGE_UNREADABLE)],
        input_reading={"observed_text": "行情看空", "image_quality": "none"},
    )

    assert "image_unreadable_without_image" in _violations(payload)


def test_an_unrecorded_image_quality_is_not_treated_as_proof_of_no_image():
    """``none`` is the only value that proves there was nothing to read."""

    payload = _payload(
        [_element(CLASS_IMAGE_UNREADABLE)], input_reading={"observed_text": "x"}
    )

    assert "image_unreadable_without_image" not in _violations(payload)


def test_the_precondition_only_constrains_the_unreadable_class():
    payload = _payload(
        [_element(CLASS_SMALL_TALK)],
        input_reading={"observed_text": "x", "image_quality": "none"},
    )

    assert _violations(payload) == set()


# --------------------------------------------------------------------------
# §4 derivation, one test per row of the table
# --------------------------------------------------------------------------


def test_derive_row_recognition_failure_becomes_unreadable_image():
    derived = derive_message_classes(
        {
            "recognition_result": "识别失败",
            "lifecycle_event": {"event_type": "exit_position", "target_lifecycle_id": 9},
        }
    )

    assert derived == [{"class": CLASS_IMAGE_UNREADABLE, "target": None}]


@pytest.mark.parametrize("event_type", ["position_update", "exit_position"])
def test_derive_row_position_events_become_position_management(event_type):
    derived = derive_message_classes(
        {
            "recognition_result": "非策略",
            "lifecycle_event": {"event_type": event_type, "target_lifecycle_id": 42},
        }
    )

    assert derived == [
        {
            "class": CLASS_POSITION_MANAGEMENT,
            "target": {
                "resolution": "exact",
                "lifecycle_id": 42,
                "symbol": None,
                "side": None,
            },
        }
    ]


@pytest.mark.parametrize("event_type", ["cancel_entry", "entry_confirm"])
def test_derive_row_entry_events_become_strategy_management(event_type):
    derived = derive_message_classes(
        {
            "recognition_result": "非策略",
            "lifecycle_event": {"event_type": event_type, "target_lifecycle_id": None},
        }
    )

    assert derived == [
        {
            "class": CLASS_STRATEGY_MANAGEMENT,
            "target": {
                "resolution": "unknown",
                "lifecycle_id": None,
                "symbol": None,
                "side": None,
            },
        }
    ]


def test_derive_row_is_strategy_appends_a_new_strategy_element():
    derived = derive_message_classes(
        {"recognition_result": "是策略", "lifecycle_event": {"event_type": "none"}}
    )

    assert derived == [{"class": CLASS_NEW_STRATEGY, "target": None}]


def test_derive_row_nothing_matched_becomes_small_talk():
    derived = derive_message_classes(
        {"recognition_result": "非策略", "lifecycle_event": {"event_type": "none"}}
    )

    assert derived == [{"class": CLASS_SMALL_TALK, "target": None}]


def test_derive_expands_the_multi_target_fanout():
    derived = derive_message_classes(
        {
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "target_lifecycle_id": None,
                "targets": [
                    {"target_lifecycle_id": 7, "symbol": "BTC", "side": "long"},
                    {"target_lifecycle_id": 8, "symbol": "ETH", "side": "short"},
                ],
            },
        }
    )

    assert [item["class"] for item in derived] == [
        CLASS_POSITION_MANAGEMENT,
        CLASS_POSITION_MANAGEMENT,
    ]
    assert [item["target"]["lifecycle_id"] for item in derived] == [7, 8]
    assert [item["target"]["symbol"] for item in derived] == ["BTC", "ETH"]


def test_derive_never_writes_back_to_the_payload():
    payload = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "exit_position", "target_lifecycle_id": 5},
    }
    before = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "exit_position", "target_lifecycle_id": 5},
    }

    derive_message_classes(payload)

    assert payload == before
    assert "message_classes" not in payload


# --------------------------------------------------------------------------
# The user's named scenario: 离场 + 反手
# --------------------------------------------------------------------------


def test_exit_then_reverse_is_two_elements_on_both_sides_and_they_agree():
    """「XX 平掉 + 反手做多，入场…止损…」是一条消息里的两个独立动作。

    This is the case the first design version got wrong by picking one class by
    priority; both the explicit list and the §4 derivation must carry both.
    """

    payload = _payload(
        [
            {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 1319)},
            _element(CLASS_NEW_STRATEGY),
        ],
        recognition_result="是策略",
        strategy=_strategy(symbol="ETH", side="long", entry="3120", stop_loss="3040"),
        lifecycle_event={"event_type": "exit_position", "target_lifecycle_id": 1319},
    )

    parsed = parse_message_classes(payload, allowed_lifecycle_ids=[1319])
    derived = derive_message_classes(payload)
    comparison = compare_message_classes(parsed.to_payload(), derived)

    assert parsed.violations == ()
    assert [element.message_class for element in parsed.elements] == [
        CLASS_POSITION_MANAGEMENT,
        CLASS_NEW_STRATEGY,
    ]
    assert [item["class"] for item in derived] == [
        CLASS_POSITION_MANAGEMENT,
        CLASS_NEW_STRATEGY,
    ]
    assert comparison["agrees"] is True


def test_exit_then_reverse_disagrees_when_the_explicit_list_drops_the_entry():
    """The same message with only one element must *not* read as agreement."""

    payload = _payload(
        [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 1319)}],
        recognition_result="是策略",
        lifecycle_event={"event_type": "exit_position", "target_lifecycle_id": 1319},
    )

    comparison = compare_message_classes(
        payload["message_classes"], derive_message_classes(payload)
    )

    assert comparison["agrees"] is False
    assert comparison["only_in_derived"] == [
        {"class": CLASS_NEW_STRATEGY, "target": None}
    ]
    assert comparison["only_in_explicit"] == []


# --------------------------------------------------------------------------
# compare_message_classes
# --------------------------------------------------------------------------


def test_comparison_ignores_symbol_and_side_but_not_resolution_or_lifecycle_id():
    explicit = [
        {
            "class": CLASS_POSITION_MANAGEMENT,
            "target": _target("exact", 5, symbol="BTC", side="long"),
        }
    ]
    same_but_no_evidence_fields = [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 5)}
    ]
    different_target = [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("unknown")}
    ]

    assert compare_message_classes(explicit, same_but_no_evidence_fields)["agrees"]
    assert not compare_message_classes(explicit, different_target)["agrees"]


def test_comparison_is_order_independent():
    left = [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 1)},
        {"class": CLASS_NEW_STRATEGY, "target": None},
    ]
    right = list(reversed(left))

    assert compare_message_classes(left, right)["agrees"] is True


def test_comparison_reports_both_sides_of_a_difference():
    result = compare_message_classes(
        [{"class": CLASS_SMALL_TALK, "target": None}],
        [{"class": CLASS_NEW_STRATEGY, "target": None}],
    )

    assert result["agrees"] is False
    assert result["only_in_explicit"] == [{"class": CLASS_SMALL_TALK, "target": None}]
    assert result["only_in_derived"] == [{"class": CLASS_NEW_STRATEGY, "target": None}]


def test_comparison_counts_repeats_rather_than_collapsing_them():
    twice = [
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 1)},
        {"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 2)},
    ]
    once = [{"class": CLASS_POSITION_MANAGEMENT, "target": _target("exact", 1)}]

    result = compare_message_classes(twice, once)

    assert result["agrees"] is False
    assert result["only_in_explicit"] == [
        {
            "class": CLASS_POSITION_MANAGEMENT,
            "target": {"resolution": "exact", "lifecycle_id": 2},
        }
    ]


def test_comparison_of_two_empty_sides_agrees():
    assert compare_message_classes(None, None)["agrees"] is True
    assert compare_message_classes([], [])["agrees"] is True
