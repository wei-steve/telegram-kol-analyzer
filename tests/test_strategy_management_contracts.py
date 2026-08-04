import json

import pytest

from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    load_management_contract,
    management_contract_fingerprint,
    serialize_management_contract,
)


def _contract(**overrides):
    values = {
        "version": 2,
        "target_lifecycle_id": 694,
        "strategy_instance_id": "deepcoin:miya:btc-long",
        "symbol": "BTC",
        "side": "long",
        "close_fraction": "0.5000",
        "stop_mode": "explicit_price",
        "stop_price": "62700.00",
        "stop_price_source": "current_message_text",
        "take_profit_consumption": "consume_first_stage",
        "cancel_deferred_entries": True,
        "required_components": (
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        "current_message_text": "止盈50%，剩余仓位止损位移动至62700",
    }
    values.update(overrides)
    return ManagementInstructionContract(**values)


def test_contract_serialization_is_canonical_and_fingerprint_is_stable():
    contract = _contract()

    serialized = serialize_management_contract(contract)
    restored = load_management_contract(serialized)

    assert restored.close_fraction == "0.5"
    assert restored.stop_price == "62700"
    assert serialize_management_contract(restored) == serialized
    assert management_contract_fingerprint(restored) == (
        management_contract_fingerprint(contract)
    )
    assert serialized == json.dumps(
        json.loads(serialized), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize("fraction", ["0", "-0.1", "1.0001", "nan"])
def test_contract_rejects_invalid_close_fraction(fraction):
    with pytest.raises(ValueError, match="close_fraction"):
        _contract(close_fraction=fraction)


def test_contract_rejects_unknown_version_and_duplicate_components():
    with pytest.raises(ValueError, match="version"):
        _contract(version=3)
    with pytest.raises(ValueError, match="duplicate"):
        _contract(
            required_components=(
                "converge_partial_close",
                "converge_partial_close",
            )
        )


def test_explicit_stop_requires_current_message_provenance():
    with pytest.raises(ValueError, match="current_message_text"):
        _contract(stop_price_source="recognition_context")
    with pytest.raises(ValueError, match="current_message_text"):
        _contract(current_message_text="")
