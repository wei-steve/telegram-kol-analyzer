"""An explicit price the instrument's own market contradicts is not a price."""

import json

import pytest

from telegram_kol_research.break_even_reference import BreakEvenReference
from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES
from telegram_kol_research.management_price_plausibility import (
    IMPLAUSIBLE_PRICE_RATIO,
    PRICE_DISPOSED_INCIDENT_TYPE,
    PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
    dispose_pending_break_even_prices,
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


def _sanitize(contract=None, stop_loss_text=None, quote=_DEFAULT_QUOTE, side="short"):
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
        side=side,
    )


def test_the_incident_type_is_always_notified():
    assert PRICE_IMPLAUSIBLE_INCIDENT_TYPE in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_the_disposed_type_is_never_notified():
    """A number that was never a stop is a note, not an alert to act on."""

    assert PRICE_DISPOSED_INCIDENT_TYPE not in ALWAYS_NOTIFIED_INCIDENT_TYPES


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
def test_no_usable_quote_still_drops_the_number_and_breaks_even(quote):
    """Not being able to judge must never refuse a risk-reducing instruction.

    The magnitude-only version of this module returned the inputs verbatim
    here and let the gate refuse the whole message.  The user's rule is the
    opposite: a group message that wants out is followed, and an unreadable
    quote only costs us the chance to adopt the number, never the reduction.
    """

    contract = _contract(stop_price="158241758")

    result = _sanitize(contract=contract, stop_loss_text="158241758", quote=quote)

    assert result.changed
    assert result.pending == ()
    assert [finding.disposition for finding in result.findings] == [
        "ignored_quote_unavailable",
        "ignored_quote_unavailable",
    ]
    assert result.stop_loss_text is None
    assert result.reference_price is None
    assert load_management_contract(
        result.management_contract_json
    ).stop_mode == "actual_entry_price"


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


def test_a_placeable_price_is_removed_but_left_pending():
    """The value goes; what it *meant* waits for the strategy's own price."""

    contract = _contract(stop_price="2530")

    result = _sanitize(contract=contract, stop_loss_text="2530")

    assert result.changed
    assert result.findings == ()
    assert [
        (pending.field, pending.value, pending.source)
        for pending in result.pending
    ] == [
        ("contract_stop_price", "2530", "current_message_text"),
        ("stop_loss_text", "2530", "current_message_text"),
    ]
    assert result.stop_loss_text is None
    assert result.stop_price_source is None
    assert load_management_contract(
        result.management_contract_json
    ).stop_mode == "actual_entry_price"


def test_a_price_on_the_wrong_side_of_the_market_is_not_a_stop():
    """raw 15475: "最低2450" on a short is below the market, so it is not a stop."""

    result = _sanitize(stop_loss_text="2450", quote=_quote(price="2455"))

    assert [finding.disposition for finding in result.findings] == [
        "not_a_possible_stop"
    ]
    assert result.pending == ()
    assert result.stop_loss_text is None
    # The mirror image: on a long the same number is perfectly placeable.
    assert _sanitize(
        stop_loss_text="2450", quote=_quote(price="2455"), side="long"
    ).findings == ()


@pytest.mark.parametrize("side", [None, "", "buy"])
def test_a_side_we_cannot_read_drops_the_number_rather_than_adopting_it(side):
    result = _sanitize(stop_loss_text="2530", side=side)

    assert [finding.disposition for finding in result.findings] == [
        "superseded_by_strategy_price"
    ]
    assert result.pending == ()


def test_magnitude_is_judged_before_the_market_side():
    """A number that is both is recorded once, as the more specific of the two."""

    result = _sanitize(stop_loss_text="158241758", side="long")

    assert [finding.disposition for finding in result.findings] == [
        "implausible_magnitude"
    ]


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
        assert row["disposition"] == "implausible_magnitude"
    # Serializable, because it is written into the batch's target snapshot.
    assert json.loads(json.dumps(evidence, ensure_ascii=False)) == evidence


def _reference(price="2500", source="strategy_first_leg"):
    return BreakEvenReference(
        price=price,
        source=source,
        evidence={"side": "short", "open_entry_leg_indexes": [1]},
    )


def _dispose(stop_loss_text, *, reference=None, validator=lambda value, source: None,
             side="short", quote=_DEFAULT_QUOTE):
    sanitized = _sanitize(stop_loss_text=stop_loss_text, side=side, quote=quote)
    return dispose_pending_break_even_prices(
        sanitized,
        side=side,
        reference=reference or _reference(),
        explicit_price_validator=validator,
    )


def test_a_tighter_placeable_price_the_message_named_becomes_the_target():
    """The one case where the number wins: the KOL asked for more protection."""

    disposed, reference = _dispose("2490")

    assert [finding.disposition for finding in disposed.findings] == [
        "explicit_tighter_adopted"
    ]
    assert (reference.price, reference.source) == (
        "2490",
        "message_explicit_tighter",
    )
    assert reference.evidence["superseded_price"] == "2500"


def test_a_looser_price_is_superseded_by_the_strategy_price():
    """raw 17813: 81200 against a first-leg price of 80500, in miniature."""

    disposed, reference = _dispose("2520")

    assert [finding.disposition for finding in disposed.findings] == [
        "superseded_by_strategy_price"
    ]
    assert (reference.price, reference.source) == ("2500", "strategy_first_leg")


def test_a_tighter_price_the_gate_refuses_falls_back_to_the_strategy_price():
    disposed, reference = _dispose(
        "2490",
        validator=lambda value, source: "management_stop_provenance_invalid",
    )

    assert [finding.disposition for finding in disposed.findings] == [
        "superseded_by_strategy_price"
    ]
    assert reference.price == "2500"


def test_without_a_validator_no_price_can_be_adopted():
    """Fail closed: adoption needs the explicit-price checks to have been run."""

    disposed, reference = _dispose("2490", validator=None)

    assert [finding.disposition for finding in disposed.findings] == [
        "superseded_by_strategy_price"
    ]
    assert reference.price == "2500"


def test_the_validator_is_given_the_provenance_the_candidate_no_longer_carries():
    seen = []

    _dispose("2490", validator=lambda value, source: seen.append((value, source)))

    assert seen == [("2490", "current_message_text")]


def test_a_reference_without_a_price_can_never_be_undercut():
    disposed, reference = _dispose(
        "2490",
        reference=BreakEvenReference(
            price=None, source="actual_fill_no_strategy_price", evidence={}
        ),
    )

    assert [finding.disposition for finding in disposed.findings] == [
        "superseded_by_strategy_price"
    ]
    assert reference.price is None


def test_two_adoptable_prices_resolve_to_the_tighter_one():
    sanitized = _sanitize(contract=_contract(stop_price="2495"), stop_loss_text="2490")

    disposed, reference = dispose_pending_break_even_prices(
        sanitized,
        side="short",
        reference=_reference(),
        explicit_price_validator=lambda value, source: None,
    )

    assert {finding.disposition for finding in disposed.findings} == {
        "explicit_tighter_adopted"
    }
    assert reference.price == "2490"


def test_nothing_pending_leaves_the_reference_exactly_as_it_was():
    reference = _reference()

    assert dispose_pending_break_even_prices(
        None, side="short", reference=reference
    ) == (None, reference)
    assert dispose_pending_break_even_prices(
        _sanitize(stop_loss_text="158241758"), side="short", reference=reference
    )[1] is reference
