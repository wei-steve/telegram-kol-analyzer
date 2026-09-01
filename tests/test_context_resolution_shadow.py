import pytest

from telegram_kol_research.context_resolution_shadow import (
    DEFAULT_CONTEXT_RESOLUTION_SHADOW_POLICY,
    evaluate_context_resolution_shadow,
)


def _request(
    *,
    text="",
    observed_text="",
    recognition_result="非策略",
    event_type="none",
    image_types=(),
):
    return {
        "current_message": {"text": text},
        "mimo_first_pass": {
            "recognition_result": recognition_result,
            "lifecycle_event": {"event_type": event_type},
            "input_reading": {"observed_text": observed_text},
        },
        "saved_evidence": {
            "images": [
                {"image_type": image_type} for image_type in image_types
            ]
        },
    }


@pytest.mark.parametrize(
    ("request_payload", "expected_condition"),
    [
        (_request(text="BTC 空单继续持有"), "raw_text_action"),
        (_request(text="名称错了，看价格"), "raw_text_action"),
        (_request(observed_text="触发入场价"), "observed_text_action"),
        (
            _request(image_types=("market_chart",)),
            "empty_text_market_chart",
        ),
    ],
)
def test_shadow_multiple_gate_keeps_each_approved_fallback(
    request_payload,
    expected_condition,
):
    result = evaluate_context_resolution_shadow(
        request_payload=request_payload,
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
    )

    assert result.would_trigger is True
    assert expected_condition in result.conditions
    assert result.agrees_with_authoritative is True
    assert result.disagreement_direction is None


def test_shadow_multiple_gate_would_skip_plain_commentary():
    result = evaluate_context_resolution_shadow(
        request_payload=_request(text="今天行情有点热闹"),
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
    )

    assert result.would_trigger is False
    assert result.conditions == ()
    assert result.agrees_with_authoritative is False
    assert result.disagreement_direction == "shadow_would_skip"


@pytest.mark.parametrize(
    ("request_payload", "expected_condition"),
    [
        (_request(recognition_result="是策略"), "first_pass_strategy"),
        (_request(event_type="position_update"), "lifecycle_event_present"),
    ],
)
def test_shadow_multiple_gate_keeps_rule_b_authority_signals(
    request_payload,
    expected_condition,
):
    result = evaluate_context_resolution_shadow(
        request_payload=request_payload,
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
    )

    assert result.would_trigger is True
    assert expected_condition in result.conditions


def test_market_chart_fallback_requires_both_text_sources_to_be_empty():
    result = evaluate_context_resolution_shadow(
        request_payload=_request(
            text="普通评论",
            image_types=("market_chart",),
        ),
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
    )

    assert result.would_trigger is False
    assert "empty_text_market_chart" not in result.conditions


def test_shadow_preserves_non_multiple_authoritative_trigger():
    result = evaluate_context_resolution_shadow(
        request_payload=_request(text="普通评论"),
        authoritative_triggers=(
            "multiple_same_source_candidates",
            "text_image_conflict",
        ),
        authoritative_would_trigger=True,
    )

    assert result.would_trigger is True
    assert result.conditions == ("authoritative:text_image_conflict",)
    assert result.agrees_with_authoritative is True


def test_shadow_policy_parameters_are_centralized_and_replaceable():
    custom = DEFAULT_CONTEXT_RESOLUTION_SHADOW_POLICY.replace(
        action_patterns=("custom-action",),
        market_chart_types=frozenset({"custom-chart"}),
    )

    action = evaluate_context_resolution_shadow(
        request_payload=_request(text="custom-action"),
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
        policy=custom,
    )
    chart = evaluate_context_resolution_shadow(
        request_payload=_request(image_types=("custom-chart",)),
        authoritative_triggers=("multiple_same_source_candidates",),
        authoritative_would_trigger=True,
        policy=custom,
    )

    assert action.would_trigger is True
    assert chart.would_trigger is True
