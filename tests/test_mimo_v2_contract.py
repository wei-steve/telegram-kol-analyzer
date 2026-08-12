import copy

import pytest

from telegram_kol_research.mimo_v2_contract import (
    MimoV2ContractError,
    parse_mimo_v2_payload,
)


def _field(value, *, source="image", confidence=0.98):
    return {"value": value, "source": source, "confidence": confidence}


def _valid_payload():
    return {
        "contract_version": "mimo-authoritative-v2",
        "summary": "管理已有 ETH 空单并移动止损",
        "confidence": 0.94,
        "intents": [
            {
                "intent_type": "position_management",
                "action": {
                    "kind": "move_stop_to_protect",
                    "target": {"lifecycle_id": 790, "thread_id": 52},
                    "strategy": None,
                    "parameters": {"stop_loss": "1940"},
                },
                "reason": "消息明确要求移动止损到1940",
                "confidence": 0.95,
                "evidence_refs": [
                    "text:stop_loss",
                    "image:381:symbol",
                    "image:381:side",
                ],
            }
        ],
        "evidence": {
            "text": {
                "observed_text": "移动止损到1940",
                "fields": {
                    "stop_loss": _field("1940", source="text", confidence=0.95)
                },
            },
            "images": [
                {
                    "asset_id": 381,
                    "image_type": "position_screenshot",
                    "quality": "clear",
                    "observed_text": "ETHUSDT 永续，空，止损1940",
                    "summary": "ETHUSDT空仓持仓截图",
                    "fields": {
                        "symbol": _field("ETH"),
                        "side": _field("short"),
                        "stop_loss": _field("1940", confidence=0.96),
                    },
                    "confidence": 0.97,
                }
            ],
            "conflicts": [],
        },
    }


def test_parse_position_management_intent_with_image_evidence():
    parsed = parse_mimo_v2_payload(_valid_payload())

    assert parsed.contract_version == "mimo-authoritative-v2"
    assert parsed.intents[0].intent_type == "position_management"
    assert parsed.intents[0].action is not None
    assert parsed.intents[0].action.kind == "move_stop_to_protect"
    assert parsed.intents[0].action.target_lifecycle_id == 790
    assert parsed.evidence.images[0].asset_id == 381
    assert parsed.evidence.images[0].quality == "clear"
    assert parsed.evidence.images[0].fields["side"]["value"] == "short"


def test_parse_preserves_ordered_actionable_and_informational_intents():
    payload = _valid_payload()
    payload["intents"].append(
        {
            "intent_type": "market_commentary",
            "action": None,
            "reason": "记录当前震荡观点",
            "confidence": 0.82,
            "evidence_refs": ["text:observed_text"],
        }
    )

    parsed = parse_mimo_v2_payload(payload)

    assert [row.intent_type for row in parsed.intents] == [
        "position_management",
        "market_commentary",
    ]
    assert parsed.intents[1].action is None


def test_parse_normalizes_exact_empty_informational_action_shell():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "market_commentary",
            "action": {
                "kind": None,
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": None,
                "parameters": {},
            },
            "reason": "仅是市场观点",
            "confidence": 0.82,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    parsed = parse_mimo_v2_payload(payload)

    assert parsed.intents[0].action is None


def test_parse_rejects_nonempty_informational_action_shell():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "market_commentary",
            "action": {
                "kind": None,
                "target": {"lifecycle_id": 790, "thread_id": None},
                "strategy": None,
                "parameters": {},
            },
            "reason": "仅是市场观点",
            "confidence": 0.82,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    with pytest.raises(MimoV2ContractError, match="action_kind_invalid"):
        parse_mimo_v2_payload(payload)


@pytest.mark.parametrize(
    "mutator",
    (
        lambda action: action.update(extra=None),
        lambda action: action["target"].update(extra=None),
        lambda action: action["target"].update(thread_id=52),
        lambda action: action.update(kind="full_exit"),
        lambda action: action.update(strategy={"symbol": "BTC"}),
        lambda action: action.update(parameters={"exit_price": "100000"}),
    ),
)
def test_parse_rejects_near_empty_informational_action_shell(mutator):
    payload = _valid_payload()
    action = {
        "kind": None,
        "target": {"lifecycle_id": None, "thread_id": None},
        "strategy": None,
        "parameters": {},
    }
    mutator(action)
    payload["intents"] = [
        {
            "intent_type": "market_commentary",
            "action": action,
            "reason": "仅是市场观点",
            "confidence": 0.82,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    with pytest.raises(MimoV2ContractError):
        parse_mimo_v2_payload(payload)


def test_parse_rejects_empty_action_shell_for_actionable_intent():
    payload = _valid_payload()
    payload["intents"][0]["action"] = {
        "kind": None,
        "target": {"lifecycle_id": None, "thread_id": None},
        "strategy": None,
        "parameters": {},
    }

    with pytest.raises(MimoV2ContractError, match="action_kind_invalid"):
        parse_mimo_v2_payload(payload)


def test_parse_complete_entry_strategy():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "new_strategy",
            "action": {
                "kind": "entry",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "1880-1890",
                    "stop_loss": "1850",
                    "take_profit": "1950",
                    "leverage": "20",
                    "order_type": "limit",
                },
                "parameters": {},
            },
            "reason": "完整入场参数",
            "confidence": 0.93,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    parsed = parse_mimo_v2_payload(payload)

    assert parsed.intents[0].action is not None
    assert parsed.intents[0].action.strategy["symbol"] == "ETH"
    assert parsed.intents[0].action.strategy["side"] == "long"


def test_parse_entry_confirmation_for_pending_strategy():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "entry_confirmation",
            "action": {
                "kind": "confirm_entry",
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "strategy": None,
                "parameters": {"entry_price": "1730"},
            },
            "reason": "消息明确说现价进场",
            "confidence": 0.96,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    parsed = parse_mimo_v2_payload(payload)

    assert parsed.intents[0].intent_type == "entry_confirmation"
    assert parsed.intents[0].action.kind == "confirm_entry"
    assert parsed.intents[0].action.parameters["entry_price"] == "1730"


@pytest.mark.parametrize(
    ("parameters", "expected_kind"),
    (
        (
            {
                "fragment_kind": "risk_multiplier",
                "symbol": "btc",
                "side": "long",
                "risk_multiplier": "0.5",
            },
            "risk_multiplier",
        ),
        (
            {
                "fragment_kind": "leg_allocation",
                "symbol": "ETH",
                "side": "short",
                "allocations": ["0.5", "0.5"],
            },
            "leg_allocation",
        ),
        (
            {
                "fragment_kind": "supplemental_entry",
                "symbol": "ETH",
                "side": "long",
                "entry_price": "63400",
            },
            "supplemental_entry",
        ),
    ),
)
def test_parse_non_executable_entry_fragment(parameters, expected_kind):
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "entry_context",
            "action": {
                "kind": "entry_fragment",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": None,
                "parameters": parameters,
            },
            "reason": "相邻入场片段",
            "confidence": 0.95,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    parsed = parse_mimo_v2_payload(payload)

    assert parsed.intents[0].action.kind == "entry_fragment"
    assert parsed.intents[0].action.parameters["fragment_kind"] == expected_kind


@pytest.mark.parametrize(
    ("mutator", "error"),
    (
        (
            lambda payload: payload["intents"][0]["action"]["strategy"].update(
                order_type="stop"
            ),
            "strategy_order_type_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"]["strategy"].update(
                entry={"price": "1880"}
            ),
            "strategy_entry_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"]["strategy"].update(
                leverage={"value": 20}
            ),
            "strategy_leverage_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"].update(
                parameters={"nested": {"unsafe": True}}
            ),
            "parameters_fields_invalid",
        ),
    ),
)
def test_rejects_invalid_entry_fields_and_parameters(mutator, error):
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "new_strategy",
            "action": {
                "kind": "entry",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "1880",
                    "stop_loss": "1850",
                    "take_profit": "1950",
                    "leverage": "20",
                    "order_type": "limit",
                },
                "parameters": {},
            },
            "reason": "完整策略",
            "confidence": 0.9,
            "evidence_refs": ["text:observed_text"],
        }
    ]
    mutator(payload)

    with pytest.raises(MimoV2ContractError, match=error):
        parse_mimo_v2_payload(payload)


def test_rejects_action_specific_parameter_mismatch():
    payload = _valid_payload()
    payload["intents"][0]["action"]["parameters"] = {
        "stop_loss": "1940",
        "nested": {"unsafe": True},
    }

    with pytest.raises(MimoV2ContractError, match="parameters_fields_invalid"):
        parse_mimo_v2_payload(payload)


@pytest.mark.parametrize(
    "parameters",
    (
        {
            "fragment_kind": "leg_allocation",
            "symbol": "BTC",
            "side": "long",
            "allocations": ["0.6", "0.5"],
        },
        {
            "fragment_kind": "risk_multiplier",
            "symbol": "BTC",
            "side": "long",
            "risk_multiplier": "1.5",
        },
        {
            "fragment_kind": "supplemental_entry",
            "symbol": "BTC",
            "side": "long",
            "entry_price": {"value": "63400"},
        },
        {
            "fragment_kind": "supplemental_entry",
            "symbol": "BTC",
            "side": "long",
            "entry_price": "9" * 129,
        },
        {
            "fragment_kind": "supplemental_entry",
            "symbol": "BTC",
            "side": "long",
            "entry_price": "1e999999999",
        },
    ),
)
def test_rejects_invalid_entry_fragment_parameters(parameters):
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "entry_context",
            "action": {
                "kind": "entry_fragment",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": None,
                "parameters": parameters,
            },
            "reason": "invalid fragment",
            "confidence": 0.9,
            "evidence_refs": ["text:observed_text"],
        }
    ]

    with pytest.raises(MimoV2ContractError, match="entry_fragment"):
        parse_mimo_v2_payload(payload)


def test_parsed_contract_is_deeply_immutable():
    payload = _valid_payload()
    parsed = parse_mimo_v2_payload(payload)

    with pytest.raises(TypeError):
        parsed.intents[0].action.parameters["stop_loss"] = "2000"
    with pytest.raises(TypeError):
        parsed.evidence.images[0].fields["side"]["value"] = "long"

    entry_payload = _valid_payload()
    entry_payload["intents"] = [
        {
            "intent_type": "new_strategy",
            "action": {
                "kind": "entry",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "1880",
                    "stop_loss": "1850",
                    "take_profit": "1950",
                    "leverage": None,
                    "order_type": "limit",
                },
                "parameters": {},
            },
            "reason": "完整策略",
            "confidence": 0.9,
            "evidence_refs": ["text:observed_text"],
        }
    ]
    parsed_entry = parse_mimo_v2_payload(entry_payload)
    with pytest.raises(TypeError):
        parsed_entry.intents[0].action.strategy["stop_loss"] = "1800"


def test_rejects_empty_intent_list():
    payload = _valid_payload()
    payload["intents"] = []

    with pytest.raises(MimoV2ContractError, match="intent_count_invalid"):
        parse_mimo_v2_payload(payload)


def test_rejects_unbounded_evidence_fields_and_values():
    payload = _valid_payload()
    payload["evidence"]["text"]["fields"] = {
        f"field_{index}": _field(str(index), source="text")
        for index in range(33)
    }
    with pytest.raises(MimoV2ContractError, match="evidence_field_count_exceeded"):
        parse_mimo_v2_payload(payload)

    payload = _valid_payload()
    payload["evidence"]["text"]["fields"]["oversized"] = _field(
        "x" * 2001,
        source="text",
    )
    with pytest.raises(MimoV2ContractError, match="field_value_too_long"):
        parse_mimo_v2_payload(payload)


def test_actionable_intent_requires_evidence_reference():
    payload = _valid_payload()
    payload["intents"][0]["evidence_refs"] = []

    with pytest.raises(MimoV2ContractError, match="evidence_refs_missing"):
        parse_mimo_v2_payload(payload)


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda payload: payload.update(contract_version="v1"),
            "contract_version_invalid",
        ),
        (
            lambda payload: payload["intents"][0].update(intent_type="other"),
            "intent_0_type_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"].update(kind="other"),
            "intent_0_action_kind_invalid",
        ),
        (
            lambda payload: payload["intents"][0].update(confidence=1.1),
            "intent_0_confidence_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"]["target"].update(
                lifecycle_id=0
            ),
            "intent_0_target_lifecycle_id_invalid",
        ),
        (
            lambda payload: payload["intents"][0]["action"]["target"].update(
                lifecycle_id=2**63
            ),
            "intent_0_target_lifecycle_id_invalid",
        ),
        (
            lambda payload: payload["evidence"]["images"][0].update(
                quality="invented"
            ),
            "image_0_quality_invalid",
        ),
        (
            lambda payload: payload["intents"][0].update(
                evidence_refs=["image:999:side"]
            ),
            "intent_0_evidence_ref_image_missing",
        ),
        (
            lambda payload: payload["intents"][0].update(
                evidence_refs=["image:381:not_present"]
            ),
            "intent_0_evidence_ref_field_missing",
        ),
    ],
)
def test_rejects_invalid_contract_values(mutator, error):
    payload = _valid_payload()
    mutator(payload)

    with pytest.raises(MimoV2ContractError, match=error):
        parse_mimo_v2_payload(payload)


def test_rejects_incomplete_entry_strategy():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "new_strategy",
            "action": {
                "kind": "entry",
                "target": {"lifecycle_id": None, "thread_id": None},
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "1880",
                    "stop_loss": "1850",
                },
                "parameters": {},
            },
            "reason": "missing take profit",
            "confidence": 0.9,
            "evidence_refs": [],
        }
    ]

    with pytest.raises(MimoV2ContractError, match="intent_0_strategy_incomplete"):
        parse_mimo_v2_payload(payload)


def test_rejects_duplicate_action_identity():
    payload = _valid_payload()
    duplicate = copy.deepcopy(payload["intents"][0])
    duplicate["reason"] = "same action with different wording"
    payload["intents"].append(duplicate)

    with pytest.raises(MimoV2ContractError, match="duplicate_action"):
        parse_mimo_v2_payload(payload)


def test_rejects_unbounded_intent_count():
    payload = _valid_payload()
    payload["intents"] = [
        {
            "intent_type": "market_commentary",
            "action": None,
            "reason": str(index),
            "confidence": 0.8,
            "evidence_refs": [],
        }
        for index in range(9)
    ]

    with pytest.raises(MimoV2ContractError, match="intent_count_exceeded"):
        parse_mimo_v2_payload(payload)


def test_rejects_unexpected_top_level_field():
    payload = _valid_payload()
    payload["display_only"] = {"label": "仓位管理"}

    with pytest.raises(MimoV2ContractError, match="top_level_fields_invalid"):
        parse_mimo_v2_payload(payload)
