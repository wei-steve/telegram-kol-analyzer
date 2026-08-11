import pytest

from telegram_kol_research.authoritative_instructions import (
    AuthoritativeInstructionError,
    normalize_authoritative_instructions,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.message_recognition import (
    _apply_instruction_compatibility_view,
)


def test_normalizes_cancel_and_entry_from_one_payload():
    payload = {
        "instructions": [
            {
                "kind": "cancel_pending_entry",
                "confidence": 0.95,
                "reason": "撤，不挂了，没挂到",
            },
            {
                "kind": "entry",
                "confidence": 0.95,
                "strategy": {
                    "symbol": "BTC",
                    "side": "long",
                    "entry": "64700-63800",
                    "stop_loss": "63400",
                    "take_profit": "65400-66100-66800",
                },
            },
        ]
    }

    instructions = normalize_authoritative_instructions(payload)

    assert [row.kind for row in instructions] == [
        "cancel_pending_entry",
        "entry",
    ]
    assert instructions[1].strategy == {
        "symbol": "BTC",
        "side": "long",
        "entry": "64700-63800",
        "stop_loss": "63400",
        "take_profit": "65400-66100-66800",
    }


def test_legacy_strategy_and_lifecycle_event_are_both_preserved():
    instructions = normalize_authoritative_instructions(
        {
            "recognition_result": "是策略",
            "confidence": 0.91,
            "strategy": {
                "symbol": "BTC",
                "side": "long",
                "entry": "64700-63800",
                "stop_loss": "63400",
                "take_profit": "65400-66100-66800",
            },
            "lifecycle_event": {
                "event_type": "cancel_entry",
                "target_lifecycle_id": 764,
                "confidence": 0.95,
                "reason": "撤销旧空单",
            },
        }
    )

    assert [row.kind for row in instructions] == [
        "cancel_pending_entry",
        "entry",
    ]
    assert instructions[0].target_lifecycle_id == 764
    assert instructions[1].strategy["side"] == "long"


@pytest.mark.parametrize(
    "payload",
    [
        {"instructions": [{"kind": "unsupported", "confidence": 0.9}]},
        {"instructions": [{"kind": "entry", "confidence": 1.1, "strategy": {}}]},
        {
            "instructions": [
                {"kind": "cancel_pending_entry", "confidence": 0.9},
                {"kind": "cancel_pending_entry", "confidence": 0.9},
            ]
        },
        {"instructions": [{"kind": "entry", "confidence": 0.9, "strategy": {}}]},
    ],
)
def test_rejects_invalid_instruction_contract(payload):
    with pytest.raises(AuthoritativeInstructionError):
        normalize_authoritative_instructions(payload)


def test_rejects_unbounded_instruction_count():
    with pytest.raises(AuthoritativeInstructionError):
        normalize_authoritative_instructions(
            {
                "instructions": [
                    {
                        "kind": "hold_update",
                        "confidence": 0.8,
                        "reason": str(index),
                    }
                    for index in range(9)
                ]
            }
        )


def test_mimo_v2_adapter_output_uses_existing_instruction_contract():
    result = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "撤销旧挂单并建立新策略",
            "confidence": 0.95,
            "intents": [
                {
                    "intent_type": "new_strategy",
                    "action": {
                        "kind": "entry",
                        "target": {"lifecycle_id": None, "thread_id": None},
                        "strategy": {
                            "symbol": "BTC",
                            "side": "long",
                            "entry": "64700-63800",
                            "stop_loss": "63400",
                            "take_profit": "65400/66100/66800",
                            "leverage": None,
                            "order_type": "limit",
                        },
                        "parameters": {},
                    },
                    "reason": "完整新策略",
                    "confidence": 0.95,
                    "evidence_refs": ["text:observed_text"],
                },
                {
                    "intent_type": "cancel_entry",
                    "action": {
                        "kind": "cancel_pending_entry",
                        "target": {"lifecycle_id": 764, "thread_id": 31},
                        "strategy": None,
                        "parameters": {},
                    },
                    "reason": "撤销旧挂单",
                    "confidence": 0.96,
                    "evidence_refs": ["text:observed_text"],
                },
            ],
            "evidence": {
                "text": {"observed_text": "撤旧单，换新计划", "fields": {}},
                "images": [],
                "conflicts": [],
            },
        }
    )

    adapted = adapt_mimo_v2_to_current_payload(result)
    instructions = normalize_authoritative_instructions(adapted.payload)

    assert [row.kind for row in instructions] == [
        "cancel_pending_entry",
        "entry",
    ]
    assert instructions[0].target_lifecycle_id == 764
    assert instructions[0].target_thread_id == 31
    assert instructions[1].strategy["symbol"] == "BTC"


def test_mimo_v2_replace_does_not_become_new_entry_in_compatibility_view():
    result = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "修改旧策略的入场参数",
            "confidence": 0.95,
            "intents": [
                {
                    "intent_type": "strategy_revision",
                    "action": {
                        "kind": "replace_entry",
                        "target": {"lifecycle_id": 764, "thread_id": 31},
                        "strategy": {
                            "symbol": "BTC",
                            "side": "long",
                            "entry": "64700-63800",
                            "stop_loss": "63400",
                            "take_profit": "65400/66100/66800",
                            "leverage": None,
                            "order_type": "limit",
                        },
                        "parameters": {},
                    },
                    "reason": "修订现有策略",
                    "confidence": 0.95,
                    "evidence_refs": ["text:observed_text"],
                }
            ],
            "evidence": {
                "text": {"observed_text": "改为新入场价", "fields": {}},
                "images": [],
                "conflicts": [],
            },
        }
    )

    adapted = adapt_mimo_v2_to_current_payload(result)
    instructions = normalize_authoritative_instructions(adapted.payload)
    compatibility = _apply_instruction_compatibility_view(
        dict(adapted.payload),
        instructions,
    )

    assert compatibility["recognition_result"] == "非策略"
    assert compatibility["strategy"]["entry"] == "64700-63800"
    assert compatibility["lifecycle_event"]["event_type"] == "none"
