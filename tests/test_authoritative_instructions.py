import pytest

from telegram_kol_research.authoritative_instructions import (
    AuthoritativeInstructionError,
    normalize_authoritative_instructions,
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
