from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from telegram_kol_research.semantic_disagreement_review import (
    decide_semantic_severity,
    normalize_mimo_action,
    normalize_price,
    validate_review_payload,
)


def mimo_payload(
    *,
    recognition_result: str = "非策略",
    symbol: str | None = None,
    side: str | None = None,
    stop_loss: object = None,
    take_profit: object = None,
    event_type: str = "none",
    target_lifecycle_id: object = None,
    management_action: str | None = None,
    reason: str = "MiMo reason",
) -> dict[str, object]:
    return {
        "recognition_result": recognition_result,
        "reason": reason,
        "strategy": {
            "symbol": symbol,
            "side": side,
            "entry": None,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_type": None,
        },
        "lifecycle_event": {
            "event_type": event_type,
            "target_lifecycle_id": target_lifecycle_id,
            "symbol": symbol,
            "side": side,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "management_action": management_action,
            "confidence": 0.96,
            "reason": reason,
        },
        "confidence": 0.96,
    }


def review_payload(
    *,
    action_type: str = "none",
    target_lifecycle_id: object = None,
    symbol: str | None = None,
    side: str | None = None,
    stop_loss: object = None,
    take_profit: object = None,
    management_action: str | None = None,
    evidence: list[str] | None = None,
    conflict_types: list[str] | None = None,
    material_disagreement: bool = False,
    suggested_severity: str = "none",
    confidence: float = 0.9,
    reason: str = "DeepSeek reason",
) -> dict[str, object]:
    return {
        "independent_action": {
            "action_type": action_type,
            "target_lifecycle_id": target_lifecycle_id,
            "symbol": symbol,
            "side": side,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "management_action": management_action,
        },
        "evidence": evidence or [],
        "conflict_types": conflict_types or [],
        "material_disagreement": material_disagreement,
        "suggested_severity": suggested_severity,
        "confidence": confidence,
        "reason": reason,
    }


def decide(mimo: dict[str, object], review: dict[str, object], **kwargs):
    return decide_semantic_severity(
        mimo_payload=mimo,
        review_payload=review,
        automation=kwargs.pop("automation", {"text_evidence": "message body"}),
        input_kind=kwargs.pop("input_kind", "text"),
        current_message_text=kwargs.pop(
            "current_message_text", "message body 止损推到62800"
        ),
        **kwargs,
    )


def test_equivalent_numeric_formats_are_none():
    mimo = mimo_payload(
        recognition_result="是策略",
        symbol=" btc-usdt ",
        side="LONG",
        stop_loss="61,000.0",
        take_profit="63,000 / 64,000.00",
    )
    review = review_payload(
        action_type="entry",
        symbol="BTC",
        side="多",
        stop_loss=61000,
        take_profit="63000.0 / 64000",
    )

    decision = decide(mimo, review)

    assert normalize_price(["62,800.00"]) == Decimal("62800")
    assert decision.agreement_status == "agreed"
    assert decision.severity == "none"
    assert decision.differences == ()


def test_same_action_with_different_reason_is_none():
    decision = decide(
        mimo_payload(event_type="entry_confirm", target_lifecycle_id=17, reason="现在进场"),
        review_payload(action_type="entry_confirm", target_lifecycle_id="17", reason="已经上车"),
    )

    assert decision.severity == "none"
    assert decision.agreement_status == "agreed"


def test_noncritical_take_profit_detail_is_normal():
    decision = decide(
        mimo_payload(
            recognition_result="是策略",
            symbol="ETH",
            side="long",
            take_profit="1880/1920",
        ),
        review_payload(
            action_type="entry",
            symbol="ethusdt",
            side="long",
            take_profit="1880",
            conflict_types=["non_material_price_detail"],
            material_disagreement=False,
            suggested_severity="normal",
        ),
    )

    assert decision.agreement_status == "disagreed"
    assert decision.severity == "normal"
    assert decision.differences == ("take_profit",)


def test_exit_versus_none_is_critical():
    # Fengge: “现价62800附近出局，空仓等待。”
    decision = decide(
        mimo_payload(
            event_type="exit_position",
            target_lifecycle_id=41,
            symbol="BTC",
            side="short",
            management_action="exit_requested",
            reason="现价62800附近出局，空仓等待。",
        ),
        review_payload(
            action_type="none",
            evidence=["现价62800附近出局"],
            conflict_types=["actionability", "urgent_exit_missed"],
            material_disagreement=True,
            suggested_severity="normal",
        ),
    )

    assert decision.severity == "critical"
    assert "actionability" in decision.conflict_types


def test_full_exit_versus_partial_exit_is_critical():
    decision = decide(
        mimo_payload(
            event_type="exit_position",
            target_lifecycle_id=9,
            symbol="BTC",
            side="short",
            management_action="close_position",
        ),
        review_payload(
            action_type="exit_partial",
            target_lifecycle_id=9,
            symbol="BTCUSDT",
            side="short",
            management_action="partial_take_profit",
        ),
    )

    assert decision.severity == "critical"
    assert "full_vs_partial_exit" in decision.conflict_types


@pytest.mark.parametrize(
    ("review_overrides", "expected_conflict"),
    [
        ({"action_type": "cancel_entry"}, "action_family"),
        ({"symbol": "ETH"}, "symbol"),
        ({"side": "short"}, "side"),
        ({"target_lifecycle_id": 8}, "target_lifecycle"),
    ],
)
def test_actionable_symbol_side_or_target_mismatch_is_critical(
    review_overrides, expected_conflict
):
    values = {
        "action_type": "position_update",
        "target_lifecycle_id": 7,
        "symbol": "BTC",
        "side": "long",
        "management_action": "move_stop_to_protect, partial_take_profit",
    }
    values.update(review_overrides)
    decision = decide(
        mimo_payload(
            event_type="position_update",
            target_lifecycle_id=7,
            symbol="BTCUSDT",
            side="多",
            management_action="partial_take_profit,move_stop_to_protect",
        ),
        review_payload(**values),
    )

    assert decision.severity == "critical"
    assert expected_conflict in decision.conflict_types


def test_deepseek_cannot_downgrade_code_critical_floor():
    decision = decide(
        mimo_payload(event_type="cancel_entry", target_lifecycle_id=5),
        review_payload(
            action_type="entry_confirm",
            target_lifecycle_id=5,
            conflict_types=["wording_only"],
            suggested_severity="none",
            confidence=0.2,
        ),
    )

    assert decision.severity == "critical"
    assert "action_family" in decision.conflict_types


@pytest.mark.parametrize(
    ("mimo_action", "review_action"),
    [
        ("move_stop_to_protect", "remove_stop_loss"),
        ("tighten_stop_loss", "widen_stop_loss"),
        ("remove_stop_loss", "move_stop_to_entry"),
    ],
)
def test_opposite_stop_protection_intent_is_deterministically_critical(
    mimo_action, review_action
):
    decision = decide(
        mimo_payload(
            event_type="position_update",
            management_action=mimo_action,
        ),
        review_payload(
            action_type="position_update",
            management_action=review_action,
            suggested_severity="none",
            confidence=0.1,
        ),
    )

    assert decision.severity == "critical"
    assert "stop_intent" in decision.conflict_types


@pytest.mark.parametrize("automation_status", ["skipped", "failed"])
def test_grounded_urgent_action_with_unresolved_execution_is_critical(
    automation_status,
):
    message = "现价62800附近全部出局，空仓等待。"
    decision = decide(
        mimo_payload(
            event_type="exit_position",
            target_lifecycle_id=41,
            symbol="BTC",
            side="short",
            management_action="exit_requested",
        ),
        review_payload(
            action_type="exit_full",
            target_lifecycle_id=41,
            symbol="BTC",
            side="short",
            management_action="exit_requested",
            evidence=["全部出局"],
            suggested_severity="none",
            confidence=0.1,
        ),
        automation={"status": automation_status, "text_evidence": message},
        current_message_text=message,
    )

    assert decision.severity == "critical"
    assert decision.agreement_status == "disagreed"
    assert "execution_unresolved" in decision.conflict_types


@pytest.mark.parametrize(
    "changes",
    [
        {"conflict_types": ["wording_only"]},
        {"evidence": []},
        {"confidence": 0.79},
        {"material_disagreement": False},
    ],
)
def test_unsupported_deepseek_critical_escalation_is_normal(changes):
    values = {
        "action_type": "position_update",
        "management_action": "move_stop_to_entry",
        "stop_loss": "62800",
        "evidence": ["止损推到62800"],
        "conflict_types": ["stop_intent"],
        "material_disagreement": True,
        "suggested_severity": "critical",
        "confidence": 0.95,
    }
    values.update(changes)
    decision = decide(
        mimo_payload(
            event_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss="62,800",
        ),
        review_payload(**values),
    )

    assert decision.severity == "normal"


def test_supported_evidenced_high_confidence_escalation_is_critical():
    decision = decide(
        mimo_payload(
            event_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss="62800",
        ),
        review_payload(
            action_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss=62800,
            evidence=["止损推到62800"],
            conflict_types=["stop_intent"],
            material_disagreement=True,
            suggested_severity="critical",
            confidence=0.80,
        ),
        current_message_text="现在把止损推到62800保护利润",
    )

    assert decision.severity == "critical"
    assert decision.agreement_status == "disagreed"


def test_image_review_without_text_evidence_cannot_be_critical():
    decision = decide(
        mimo_payload(
            event_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss="62800",
        ),
        review_payload(
            action_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss=62800,
            evidence=["图片显示止损推到62800"],
            conflict_types=["stop_intent"],
            material_disagreement=True,
            suggested_severity="critical",
            confidence=0.99,
        ),
        input_kind="image",
        automation={"text_evidence": ""},
        current_message_text="",
    )

    assert decision.severity == "normal"


def test_hallucinated_or_automation_only_evidence_cannot_escalate_critical():
    decision = decide(
        mimo_payload(
            event_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss="62800",
        ),
        review_payload(
            action_type="position_update",
            management_action="move_stop_to_entry",
            stop_loss=62800,
            evidence=["全部出局"],
            conflict_types=["stop_intent"],
            material_disagreement=True,
            suggested_severity="critical",
            confidence=0.99,
        ),
        current_message_text="现在把止损推到62800保护利润",
        automation={
            "has_independent_text_evidence": True,
            "text_evidence": "全部出局",
        },
    )

    assert decision.severity == "normal"


def test_image_bearing_input_requires_grounded_caption_text_to_escalate():
    review = review_payload(
        action_type="position_update",
        management_action="move_stop_to_entry",
        stop_loss=62800,
        evidence=["止损推到62800"],
        conflict_types=["stop_intent"],
        material_disagreement=True,
        suggested_severity="critical",
        confidence=0.99,
    )
    mimo = mimo_payload(
        event_type="position_update",
        management_action="move_stop_to_entry",
        stop_loss="62800",
    )

    without_caption = decide(
        mimo,
        review,
        input_kind="text+image",
        current_message_text="",
        automation={"has_independent_text_evidence": True},
    )
    with_caption = decide(
        mimo,
        review,
        input_kind="text+image",
        current_message_text="图中策略补充：止损推到62800",
        automation={},
    )

    assert without_caption.severity == "normal"
    assert with_caption.severity == "critical"


def test_action_synonyms_and_management_token_order_normalize():
    normalized = normalize_mimo_action(
        mimo_payload(
            event_type="position_update",
            symbol=" btc/usdt ",
            side="多单",
            management_action=" Move Stop To Protect，Partial Take Profit ",
        )
    )

    assert normalized["action_type"] == "exit_partial"
    assert normalized["symbol"] == "BTC"
    assert normalized["side"] == "long"
    assert normalized["management_action"] == (
        "move_stop_to_protect",
        "partial_take_profit",
    )


def test_validate_review_payload_rejects_open_vocabulary():
    payload = review_payload(conflict_types=["invented_conflict"])

    with pytest.raises(ValueError, match="conflict_types"):
        validate_review_payload(payload)


@pytest.mark.parametrize("invalid_confidence", ["0.9", True, Decimal("0.9")])
def test_validate_review_payload_rejects_non_json_number_confidence(
    invalid_confidence,
):
    payload = review_payload(confidence=invalid_confidence)

    with pytest.raises(ValueError, match="confidence"):
        validate_review_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("target_lifecycle_id", {"id": 1}),
        ("target_lifecycle_id", [1]),
        ("symbol", {"value": "BTC"}),
        ("symbol", ["BTC"]),
        ("side", {"value": "long"}),
        ("stop_loss", {"value": 62800}),
        ("stop_loss", [62800]),
        ("take_profit", [64000, 65000]),
        ("management_action", ["move_stop_to_entry"]),
    ],
)
def test_validate_review_payload_rejects_action_container_fields(
    field, invalid_value
):
    payload = review_payload()
    payload = deepcopy(payload)
    payload["independent_action"][field] = invalid_value

    with pytest.raises(ValueError, match=field):
        validate_review_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("evidence", "not-an-array"),
        ("evidence", [1]),
        ("conflict_types", "wording_only"),
        ("conflict_types", [1]),
        ("material_disagreement", "false"),
        ("reason", ["not", "a", "string"]),
    ],
)
def test_validate_review_payload_rejects_invalid_array_boolean_and_string_types(
    field, invalid_value
):
    payload = review_payload()
    payload[field] = invalid_value

    with pytest.raises(ValueError, match=field):
        validate_review_payload(payload)
