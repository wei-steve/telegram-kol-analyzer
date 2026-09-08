"""An explicit price the instrument's own market contradicts is not a price."""

import json

import pytest

from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES
from telegram_kol_research.management_price_plausibility import (
    IMPLAUSIBLE_PRICE_RATIO,
    PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
    price_is_implausible,
    quote_reference_price,
    sanitize_management_prices,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    load_management_contract,
    management_contract_fingerprint,
    serialize_management_contract,
)


def _quote(price="2484.67", price_field="last"):
    return {
        "instrument_id": "ETH-USDT-SWAP",
        "price": price,
        "price_field": price_field,
        "observed_at": "2026-09-08T06:25:00+00:00",
    }


def _contract(stop_price="2530", stop_mode="explicit_price"):
    return ManagementInstructionContract(
        version=2,
        target_lifecycle_id=345,
        strategy_instance_id="strategy-345",
        symbol="ETH",
        side="short",
        close_fraction="0.5",
        stop_mode=stop_mode,
        stop_price=stop_price,
        stop_price_source=(
            "current_message_text" if stop_mode == "explicit_price" else None
        ),
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text="第一止盈位已过，注意锁定利润，及时移动止损！",
    )


_DEFAULT_QUOTE = object()


def _sanitize(contract=None, stop_loss_text=None, quote=_DEFAULT_QUOTE):
    return sanitize_management_prices(
        stop_loss_text=stop_loss_text,
        stop_price_source=(
            "current_message_text" if stop_loss_text is not None else None
        ),
        management_contract_json=(
            serialize_management_contract(contract)
            if contract is not None
            else None
        ),
        management_contract_fingerprint_value=(
            management_contract_fingerprint(contract)
            if contract is not None
            else None
        ),
        quote=_quote() if quote is _DEFAULT_QUOTE else quote,
    )


def test_the_incident_type_is_always_notified():
    assert PRICE_IMPLAUSIBLE_INCIDENT_TYPE in ALWAYS_NOTIFIED_INCIDENT_TYPES


@pytest.mark.parametrize(
    "value,expected",
    [
        ("158241758", True),
        ("2530", False),
        ("2484.67", False),
        # Exactly ten times is still believable; past it is not.
        ("24846.7", False),
        ("24846.71", True),
        ("248.467", False),
        ("248.466", True),
        ("0", False),
        ("not-a-price", False),
        (None, False),
    ],
)
def test_the_magnitude_rule_is_a_ten_times_band(value, expected):
    assert price_is_implausible(value, quote_reference_price(_quote())) is expected
    assert IMPLAUSIBLE_PRICE_RATIO == 10


@pytest.mark.parametrize(
    "quote",
    [
        None,
        {},
        _quote(price="0"),
        _quote(price=""),
        # ``get_ticker_quote`` only ever proves a last trade; any other field
        # is not the reference this rule is defined against.
        _quote(price_field="markPx"),
    ],
)
def test_no_usable_quote_means_no_check_at_all(quote):
    contract = _contract(stop_price="158241758")

    result = _sanitize(contract=contract, stop_loss_text="158241758", quote=quote)

    assert not result.changed
    assert result.findings == ()
    assert result.stop_loss_text == "158241758"
    assert result.management_contract_json == serialize_management_contract(
        contract
    )
    assert result.management_contract_fingerprint == (
        management_contract_fingerprint(contract)
    )


def test_an_implausible_contract_stop_becomes_the_actual_entry_price():
    result = _sanitize(contract=_contract(stop_price="158241758"))

    assert result.changed
    reloaded = load_management_contract(result.management_contract_json)
    assert reloaded.stop_mode == "actual_entry_price"
    assert reloaded.stop_price is None
    assert reloaded.stop_price_source is None
    # The fingerprint has to follow the contract it names, or every downstream
    # identity check rejects the instruction instead of executing it.
    assert result.management_contract_fingerprint == (
        management_contract_fingerprint(reloaded)
    )
    # Nothing else about the instruction moved.
    assert reloaded.close_fraction == "0.5"
    assert reloaded.cancel_deferred_entries is True
    assert reloaded.current_message_text == _contract().current_message_text


def test_an_implausible_stop_loss_text_is_dropped_with_its_provenance():
    result = _sanitize(stop_loss_text="158241758")

    assert result.changed
    assert result.stop_loss_text is None
    assert result.stop_price_source is None
    assert [finding.field for finding in result.findings] == ["stop_loss_text"]


def test_a_plausible_price_is_returned_verbatim():
    contract = _contract(stop_price="2530")

    result = _sanitize(contract=contract, stop_loss_text="2530")

    assert not result.changed
    assert result.stop_loss_text == "2530"
    assert result.stop_price_source == "current_message_text"
    assert load_management_contract(
        result.management_contract_json
    ).stop_mode == "explicit_price"


def test_an_implicit_stop_contract_is_left_alone():
    contract = _contract(stop_price=None, stop_mode="actual_entry_price")

    result = _sanitize(contract=contract)

    assert not result.changed
    assert result.management_contract_json == serialize_management_contract(
        contract
    )


def test_unparseable_contract_json_is_not_turned_into_a_decision():
    result = sanitize_management_prices(
        stop_loss_text=None,
        stop_price_source=None,
        management_contract_json="{not json",
        management_contract_fingerprint_value="deadbeef",
        quote=_quote(),
    )

    assert not result.changed
    assert result.management_contract_json == "{not json"


def test_the_evidence_names_the_value_and_the_reference():
    result = _sanitize(
        contract=_contract(stop_price="158241758"), stop_loss_text="158241758"
    )

    evidence = result.as_evidence()
    assert evidence["reference_price"] == "2484.67"
    assert evidence["reference_price_source"] == "current_market_last"
    assert evidence["max_ratio"] == "10"
    assert {row["field"] for row in evidence["removed"]} == {
        "contract_stop_price",
        "stop_loss_text",
    }
    for row in evidence["removed"]:
        assert row["value"] == "158241758"
        assert row["reference_price"] == "2484.67"
    # Serializable, because it is written into the batch's target snapshot.
    assert json.loads(json.dumps(evidence, ensure_ascii=False)) == evidence
