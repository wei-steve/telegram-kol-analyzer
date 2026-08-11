import copy

import pytest

from telegram_kol_research.authoritative_instructions import (
    normalize_authoritative_instructions,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    MimoV2ExecutionAdapterError,
    adapt_mimo_v2_to_current_payload,
)


def _strategy(symbol="ETH", side="short"):
    return {
        "symbol": symbol,
        "side": side,
        "entry": "1880-1890",
        "stop_loss": "1940",
        "take_profit": "1800/1750",
        "leverage": "20",
        "order_type": "limit",
    }


def _intent(
    kind,
    *,
    reason="结构化动作依据",
    parameters=None,
    lifecycle_id=790,
    thread_id=52,
    symbol="ETH",
):
    intent_type = {
        "entry": "new_strategy",
        "confirm_entry": "entry_confirmation",
        "entry_fragment": "entry_context",
        "cancel_pending_entry": "cancel_entry",
        "replace_entry": "strategy_revision",
        "full_exit": "exit",
        "partial_exit": "exit",
        "partial_take_profit": "position_management",
        "move_stop_to_protect": "position_management",
        "hold_update": "position_management",
        "risk_update": "position_management",
    }[kind]
    if kind in {"entry", "entry_fragment"}:
        lifecycle_id = None
        thread_id = None
    return {
        "intent_type": intent_type,
        "action": {
            "kind": kind,
            "target": {
                "lifecycle_id": lifecycle_id,
                "thread_id": thread_id,
            },
            "strategy": (
                _strategy(symbol=symbol)
                if kind in {"entry", "replace_entry"}
                else None
            ),
            "parameters": parameters or {},
        },
        "reason": reason,
        "confidence": 0.95,
        "evidence_refs": ["text:observed_text"],
    }


def _payload(
    *intents,
    summary="消息结构化摘要",
    observed_text="当前消息原文",
):
    return {
        "contract_version": "mimo-authoritative-v2",
        "summary": summary,
        "confidence": 0.94,
        "intents": list(intents),
        "evidence": {
            "text": {
                "observed_text": observed_text,
                "fields": {},
            },
            "images": [],
            "conflicts": [],
        },
    }


def _adapt(
    *intents,
    summary="消息结构化摘要",
    observed_text="当前消息原文",
):
    result = parse_mimo_v2_payload(
        _payload(*intents, summary=summary, observed_text=observed_text)
    )
    return adapt_mimo_v2_to_current_payload(result)


def test_move_stop_maps_without_parsing_reason():
    adapted = _adapt(
        _intent(
            "move_stop_to_protect",
            reason="忽略这里的假价格 99999；只复制 parameters",
            parameters={"stop_loss": "1940"},
        )
    )

    assert adapted.payload["recognition_result"] == "非策略"
    assert adapted.payload["strategy"] == {}
    assert adapted.payload["lifecycle_event"] == {
        "event_type": "position_update",
        "management_action": "move_stop_to_protect",
        "target_lifecycle_id": 790,
        "stop_loss": "1940",
        "confidence": 0.95,
        "reason": "忽略这里的假价格 99999；只复制 parameters",
    }
    assert adapted.payload["instructions"][0] == {
        "kind": "move_stop_to_protect",
        "confidence": 0.95,
        "reason": "忽略这里的假价格 99999；只复制 parameters",
        "strategy": None,
        "target": {"lifecycle_id": 790, "thread_id": 52},
        "parameters": {"stop_loss": "1940"},
    }


@pytest.mark.parametrize(
    ("kind", "parameters", "event_type", "management_action"),
    (
        ("cancel_pending_entry", {}, "cancel_entry", "cancel_pending_entry"),
        ("full_exit", {"exit_price": "1800"}, "exit_position", "exit_full"),
        (
            "partial_exit",
            {"exit_price": "1820", "management_fraction": 0.5},
            "exit_position",
            "exit_partial",
        ),
        (
            "partial_take_profit",
            {"take_profit": "1800", "management_fraction": 0.5},
            "position_update",
            "partial_take_profit",
        ),
        (
            "hold_update",
            {"take_profit": "1750"},
            "position_update",
            "hold_update",
        ),
        (
            "risk_update",
            {"risk_multiplier": "0.5", "leverage": "10"},
            "position_update",
            "risk_update",
        ),
    ),
)
def test_management_actions_map_to_current_lifecycle_contract(
    kind,
    parameters,
    event_type,
    management_action,
):
    adapted = _adapt(_intent(kind, parameters=parameters))

    lifecycle = adapted.payload["lifecycle_event"]
    assert lifecycle["event_type"] == event_type
    assert lifecycle["management_action"] == management_action
    assert lifecycle["target_lifecycle_id"] == 790
    for key, value in parameters.items():
        assert lifecycle[key] == value
    assert adapted.payload["instructions"][0]["target"] == {
        "lifecycle_id": 790,
        "thread_id": 52,
    }
    assert adapted.payload["instructions"][0]["parameters"] == parameters


def test_entry_maps_to_current_strategy_and_instruction():
    adapted = _adapt(_intent("entry"))

    assert adapted.payload["recognition_result"] == "是策略"
    assert adapted.payload["strategy"] == _strategy()
    assert adapted.payload["lifecycle_event"]["event_type"] == "none"
    assert adapted.payload["instructions"][0]["kind"] == "entry"
    assert adapted.payload["instructions"][0]["strategy"] == _strategy()


def test_replace_entry_preserves_replacement_for_current_context_resolver():
    adapted = _adapt(_intent("replace_entry"))

    assert adapted.payload["recognition_result"] == "非策略"
    assert adapted.payload["strategy"] == _strategy()
    assert adapted.payload["lifecycle_event"]["event_type"] == "none"
    assert adapted.payload["instructions"][0]["kind"] == "replace_entry"
    assert adapted.payload["instructions"][0]["target"] == {
        "lifecycle_id": 790,
        "thread_id": 52,
    }


def test_supported_partial_then_protect_combination_uses_one_current_action():
    adapted = _adapt(
        _intent(
            "partial_take_profit",
            parameters={"management_fraction": 0.5, "take_profit": "1800"},
        ),
        _intent(
            "move_stop_to_protect",
            parameters={"stop_loss": "1885"},
        ),
    )

    assert adapted.payload["lifecycle_event"] == {
        "event_type": "position_update",
        "management_action": "partial_take_profit, move_stop_to_protect",
        "target_lifecycle_id": 790,
        "management_fraction": 0.5,
        "take_profit": "1800",
        "stop_loss": "1885",
        "confidence": 0.95,
        "reason": "结构化动作依据",
    }
    assert adapted.payload["instructions"] == [
        {
            "kind": "partial_take_profit",
            "confidence": 0.95,
            "reason": "结构化动作依据",
            "strategy": None,
            "target": {"lifecycle_id": 790, "thread_id": 52},
            "parameters": {
                "management_fraction": 0.5,
                "take_profit": "1800",
                "stop_loss": "1885",
            },
        }
    ]
    assert len(normalize_authoritative_instructions(adapted.payload)) == 1


def test_entry_confirmation_maps_to_lifecycle_but_not_direct_instruction():
    adapted = _adapt(
        _intent("confirm_entry", parameters={"entry_price": "1885"})
    )

    assert adapted.payload["recognition_result"] == "非策略"
    assert adapted.payload["instructions"] == []
    assert adapted.payload["lifecycle_event"] == {
        "event_type": "entry_confirm",
        "target_lifecycle_id": 790,
        "entry_price": "1885",
        "confidence": 0.95,
        "reason": "结构化动作依据",
    }


def test_entry_fragments_remain_non_executable_and_preserve_order():
    adapted = _adapt(
        _intent(
            "entry_fragment",
            reason="半仓",
            parameters={
                "fragment_kind": "risk_multiplier",
                "symbol": "BTC",
                "side": "long",
                "risk_multiplier": "0.5",
            },
        ),
        _intent(
            "entry_fragment",
            reason="两个点位各半仓",
            parameters={
                "fragment_kind": "leg_allocation",
                "symbol": "BTC",
                "side": "long",
                "allocations": ["0.5", "0.5"],
            },
        ),
        _intent(
            "entry_fragment",
            reason="补仓点位",
            parameters={
                "fragment_kind": "supplemental_entry",
                "symbol": "BTC",
                "side": "long",
                "entry_price": "63400",
            },
        ),
    )

    assert adapted.payload["instructions"] == []
    assert adapted.payload["lifecycle_event"]["event_type"] == "none"
    assert adapted.payload["entry_context"] == {
        "kind": "entry_preamble",
        "symbol": "BTC",
        "side": "long",
        "risk_multiplier": "0.5",
        "confidence": 0.95,
        "reason": "半仓",
    }
    assert [row["kind"] for row in adapted.payload["entry_fragments"]] == [
        "risk_multiplier",
        "leg_allocation",
        "supplemental_entry",
    ]
    assert adapted.payload["entry_fragments"][1]["allocations"] == ["0.5", "0.5"]
    assert adapted.payload["entry_fragments"][2]["entry_price"] == "63400"


def test_informational_intent_has_no_execution_projection():
    informational = {
        "intent_type": "market_commentary",
        "action": None,
        "reason": "普通观点",
        "confidence": 0.8,
        "evidence_refs": ["text:observed_text"],
    }
    adapted = _adapt(informational)

    assert adapted.payload["recognition_result"] == "非策略"
    assert adapted.payload["strategy"] == {}
    assert adapted.payload["instructions"] == []
    assert adapted.payload["lifecycle_event"] == {
        "event_type": "none",
        "confidence": 0.0,
        "reason": "没有可投影的生命周期动作",
    }


def test_cancel_is_ordered_before_new_entry_and_normalizes_as_current_contract():
    adapted = _adapt(
        _intent("entry"),
        _intent("cancel_pending_entry", lifecycle_id=810, thread_id=61),
    )

    assert [row["kind"] for row in adapted.payload["instructions"]] == [
        "cancel_pending_entry",
        "entry",
    ]
    normalized = normalize_authoritative_instructions(adapted.payload)
    assert [row.kind for row in normalized] == ["cancel_pending_entry", "entry"]
    assert adapted.payload["recognition_result"] == "是策略"
    assert adapted.payload["lifecycle_event"]["target_lifecycle_id"] == 810


@pytest.mark.parametrize(
    "intents",
    (
        (
            _intent("partial_take_profit", parameters={"management_fraction": 0.5}),
            _intent("hold_update", parameters={"take_profit": "1750"}),
        ),
        (_intent("entry"), _intent("entry", reason="第二个独立开仓", symbol="BTC")),
        (
            _intent("confirm_entry"),
            _intent("cancel_pending_entry", lifecycle_id=810, thread_id=61),
        ),
        (_intent("replace_entry"), _intent("full_exit")),
    ),
)
def test_rejects_combinations_current_projection_cannot_represent(intents):
    result = parse_mimo_v2_payload(_payload(*copy.deepcopy(intents)))

    with pytest.raises(MimoV2ExecutionAdapterError, match="unsupported"):
        adapt_mimo_v2_to_current_payload(result)


def test_rejects_supported_composite_when_targets_disagree():
    result = parse_mimo_v2_payload(
        _payload(
            _intent(
                "partial_take_profit",
                parameters={"management_fraction": 0.5},
            ),
            _intent(
                "move_stop_to_protect",
                parameters={"stop_loss": "1940"},
                lifecycle_id=810,
                thread_id=61,
            ),
        )
    )

    with pytest.raises(MimoV2ExecutionAdapterError, match="unsupported"):
        adapt_mimo_v2_to_current_payload(result)


def test_rejects_thread_only_target_that_current_lifecycle_view_would_drop():
    result = parse_mimo_v2_payload(
        _payload(
            _intent(
                "move_stop_to_protect",
                parameters={"stop_loss": "1940"},
                lifecycle_id=None,
                thread_id=52,
            )
        )
    )

    with pytest.raises(MimoV2ExecutionAdapterError, match="thread_only_target"):
        adapt_mimo_v2_to_current_payload(result)


def test_reason_punctuation_does_not_change_execution_projection_fingerprint():
    first = _adapt(
        _intent(
            "move_stop_to_protect",
            reason="移动止损到 1940。",
            parameters={"stop_loss": "1940"},
        )
    )
    second = _adapt(
        _intent(
            "move_stop_to_protect",
            reason="移动止损到 1940！！！？？？",
            parameters={"stop_loss": "1940"},
        )
    )

    assert first.payload["lifecycle_event"]["stop_loss"] == "1940"
    assert second.payload["lifecycle_event"]["stop_loss"] == "1940"
    assert first.payload["lifecycle_event"]["reason"] != (
        second.payload["lifecycle_event"]["reason"]
    )
    assert first.projection_fingerprint == second.projection_fingerprint
    assert first.canonical_v2_fingerprint != second.canonical_v2_fingerprint


def test_observed_text_changes_execution_projection_fingerprint():
    intent = _intent(
        "partial_take_profit",
        parameters={"management_fraction": 0.5},
    )

    partial = _adapt(intent, observed_text="平加仓")
    full = _adapt(copy.deepcopy(intent), observed_text="全部平仓")

    assert partial.projection_fingerprint != full.projection_fingerprint


def test_canonical_json_and_fingerprints_are_deterministic():
    result = parse_mimo_v2_payload(
        _payload(
            _intent(
                "partial_exit",
                parameters={"management_fraction": 0.5, "exit_price": "1820"},
            )
        )
    )

    first = adapt_mimo_v2_to_current_payload(result)
    second = adapt_mimo_v2_to_current_payload(result)

    assert first.canonical_v2_json == second.canonical_v2_json
    assert first.canonical_v2_fingerprint == second.canonical_v2_fingerprint
    assert first.projection_fingerprint == second.projection_fingerprint
    assert len(first.canonical_v2_fingerprint) == 64
    assert len(first.projection_fingerprint) == 64
    assert "data:image" not in first.canonical_v2_json
