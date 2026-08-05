from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json

import pytest


def test_composite_semantic_review_input_preserves_mimo_authority_and_outcomes():
    from telegram_kol_research.semantic_disagreement_review import (
        build_composite_semantic_review_input,
    )

    value = build_composite_semantic_review_input(
        authoritative_payload={"management_action": "partial_then_break_even"},
        contract={"close_fraction": "0.5", "stop_mode": "actual_entry_price"},
        components=[{"component_kind": "converge_partial_close", "status": "confirmed"}],
        outcomes={"remaining_size": "5"},
    )
    assert value["authority"] == "mimo"
    assert value["advisory_only"] is True
    assert value["authoritative_payload"]["management_action"] == "partial_then_break_even"
    assert value["contract"]["close_fraction"] == "0.5"
    assert value["actual_outcomes"]["remaining_size"] == "5"

from telegram_kol_research.semantic_disagreement_review import (
    decide_semantic_severity,
    normalize_mimo_action,
    normalize_price,
    run_deepseek_semantic_review,
    validate_review_payload,
)
from telegram_kol_research.ai_recognition_config import AiProviderConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import AiPromptInvocation, RawMessage, RecognitionDecision, StrategyLifecycle
from telegram_kol_research.prompt_defaults import (
    SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
    seed_default_prompt_registry,
)
from telegram_kol_research.prompt_registry import resolve_active_prompt


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


def test_review_request_is_grounded_safe_strict_and_audited(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example/v1",
            api_key="SUPER-SECRET-KEY",
            model="deepseek-review",
            timeout_seconds=7,
        )
    )
    seed_default_prompt_registry(session_factory, config)
    active_prompt = resolve_active_prompt(
        session_factory, SEMANTIC_DISAGREEMENT_REVIEW_PROMPT
    )
    authoritative = mimo_payload(
        event_type="exit_position",
        target_lifecycle_id=41,
        symbol="BTC",
        side="short",
    )
    authoritative["input_reading"] = {
        "observed_text": "62800附近全部出局",
        "image_quality": "none",
    }
    with session_factory() as session:
        raw = RawMessage(
            chat_id=991,
            message_id=778,
            sender_id=51,
            sender_name="峰哥",
            posted_at=datetime(2026, 7, 13, 12, 0),
            text="现价62800附近全部出局，空仓等待。",
        )
        session.add(raw)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=991,
            message_id=700,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 10, 0),
            entry_range_low=63100,
            entry_range_high=63300,
            stop_loss=64000,
            take_profit="62000/61000",
            management_action="exit_requested",
            management_note="safe note",
        )
        session.add(lifecycle)
        session.flush()
        lifecycle_id = lifecycle.id
        later_exited = StrategyLifecycle(
            chat_id=991,
            message_id=701,
            symbol="ETH",
            side="long",
            lifecycle_status="exited",
            signal_at=datetime(2026, 7, 13, 11, 0),
            exited_at=datetime(2026, 7, 13, 13, 0),
        )
        future = StrategyLifecycle(
            chat_id=991,
            message_id=702,
            symbol="SOL",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 13, 0),
        )
        already_exited = StrategyLifecycle(
            chat_id=991,
            message_id=703,
            symbol="DOGE",
            side="long",
            lifecycle_status="exited",
            signal_at=datetime(2026, 7, 13, 9, 0),
            exited_at=datetime(2026, 7, 13, 11, 0),
        )
        already_cancelled = StrategyLifecycle(
            chat_id=991,
            message_id=704,
            symbol="BNB",
            side="short",
            lifecycle_status="cancelled",
            signal_at=datetime(2026, 7, 13, 9, 30),
            exited_at=datetime(2026, 7, 13, 11, 30),
        )
        enters_after_message = StrategyLifecycle(
            chat_id=991,
            message_id=705,
            symbol="XRP",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 13, 11, 30),
            entered_at=datetime(2026, 7, 13, 12, 30),
        )
        expired_without_timestamp = StrategyLifecycle(
            chat_id=991,
            message_id=706,
            symbol="ADA",
            side="long",
            lifecycle_status="expired",
            signal_at=datetime(2026, 7, 13, 11, 40),
            exited_at=None,
        )
        expires_after_message = StrategyLifecycle(
            chat_id=991,
            message_id=707,
            symbol="LTC",
            side="short",
            lifecycle_status="expired",
            signal_at=datetime(2026, 7, 13, 11, 20),
            exited_at=datetime(2026, 7, 13, 12, 40),
        )
        session.add_all(
            [
                later_exited,
                future,
                already_exited,
                already_cancelled,
                enters_after_message,
                expired_without_timestamp,
                expires_after_message,
            ]
        )
        for index in range(25):
            session.add(
                StrategyLifecycle(
                    chat_id=991,
                    message_id=800 + index,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=datetime(2026, 7, 13, 10, index + 1),
                )
            )
        session.flush()
        later_exited_id = later_exited.id
        enters_after_message_id = enters_after_message.id
        expires_after_message_id = expires_after_message.id
        excluded_ids = {
            future.id,
            already_exited.id,
            already_cancelled.id,
            expired_without_timestamp.id,
        }
        authoritative["lifecycle_event"]["target_lifecycle_id"] = lifecycle_id
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(authoritative, ensure_ascii=False),
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json=json.dumps({"mimo": {"trading.analysis.shared": 5}}),
                comparison_status="running",
                automation_status="submitted",
                automation_reason="close_position pos_id redacted",
            )
        )
        session.commit()
        raw_message_id = raw.id

    captured = {}

    def requester(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [
                {"message": {"content": json.dumps(review_payload(
                    action_type="exit_full",
                    target_lifecycle_id=lifecycle_id,
                    symbol="BTC",
                    side="short",
                    evidence=["全部出局"],
                ), ensure_ascii=False)}}
            ]
        }

    result = run_deepseek_semantic_review(
        session_factory,
        raw_message_id=raw_message_id,
        config=config,
        requester=requester,
    )

    assert captured["url"] == "https://api.deepseek.example/v1/chat/completions"
    assert captured["timeout"] == 7
    request_json = json.dumps(captured["json"], ensure_ascii=False, sort_keys=True)
    request_context = json.loads(captured["json"]["messages"][1]["content"])
    assert "现价62800附近全部出局，空仓等待。" in request_json
    assert request_context["source"]["chat_id"] == 991
    assert request_context["source"]["message_id"] == 778
    assert request_context["source"]["sender_name"] == "峰哥"
    context_ids = {
        item["lifecycle_id"] for item in request_context["active_strategies"]
    }
    assert len(request_context["active_strategies"]) == 20
    assert later_exited_id in context_ids
    assert expires_after_message_id in context_ids
    assert context_ids.isdisjoint(excluded_ids)
    historical_statuses = {
        item["lifecycle_id"]: item["lifecycle_status"]
        for item in request_context["active_strategies"]
    }
    assert historical_statuses[enters_after_message_id] == "pending_entry"
    assert request_context["mimo"]["authoritative_payload"] == authoritative
    assert request_context["mimo"]["input_reading"] == authoritative["input_reading"]
    assert request_context["automation"]["automation_status"] == "submitted"
    assert str(active_prompt.version_id) in request_json
    assert "SUPER-SECRET-KEY" not in request_json
    assert "Authorization" not in request_json
    assert result.prompt_versions == {
        SEMANTIC_DISAGREEMENT_REVIEW_PROMPT: active_prompt.version_id
    }
    assert result.decision.severity == "none"

    with session_factory() as session:
        audit = session.query(AiPromptInvocation).one()
        assert audit.feature == "semantic_disagreement_review"
        assert audit.raw_message_id == raw_message_id
        assert audit.status == "completed"
        assert json.loads(audit.prompt_versions_json) == result.prompt_versions


def test_review_rejects_non_strict_json_and_audits_failure(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example",
            model="deepseek-review",
        )
    )
    seed_default_prompt_registry(session_factory, config)
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short")
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(mimo_payload()),
                agreement_status="pending",
                differences_json="[]",
                comparison_status="running",
            )
        )
        session.commit()
        raw_id = raw.id

    with pytest.raises(ValueError, match="closed contract"):
        run_deepseek_semantic_review(
            session_factory,
            raw_message_id=raw_id,
            config=config,
            requester=lambda **_: {
                "choices": [{"message": {"content": '{"unexpected": true}'}}]
            },
        )

    with session_factory() as session:
        audit = session.query(AiPromptInvocation).one()
        assert audit.status == "failed"
        assert "closed contract" in audit.error_message


@pytest.mark.parametrize(
    "content",
    [
        "```json\n" + json.dumps(review_payload()) + "\n```",
        "analysis first\n" + json.dumps(review_payload()),
        json.dumps(review_payload()) + "\ntrailing prose",
    ],
)
def test_review_rejects_fenced_or_prose_wrapped_json(tmp_path, content):
    session_factory = create_session_factory(tmp_path / "research.db")
    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example",
            model="deepseek-review",
        )
    )
    seed_default_prompt_registry(session_factory, config)
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short")
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(mimo_payload()),
                agreement_status="pending",
                differences_json="[]",
                comparison_status="running",
            )
        )
        session.commit()
        raw_id = raw.id

    with pytest.raises(ValueError, match="strict JSON"):
        run_deepseek_semantic_review(
            session_factory,
            raw_message_id=raw_id,
            config=config,
            requester=lambda **_: {"choices": [{"message": {"content": content}}]},
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


def test_wording_only_conflict_with_equivalent_actions_is_none():
    decision = decide(
        mimo_payload(event_type="entry_confirm", target_lifecycle_id=17),
        review_payload(
            action_type="entry_confirm",
            target_lifecycle_id="17",
            conflict_types=["wording_only"],
            material_disagreement=False,
            suggested_severity="none",
            reason="Only the explanation wording differs",
        ),
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
