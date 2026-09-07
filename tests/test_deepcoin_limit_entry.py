"""The limit-entry payload and the rule for which legs may use it.

Every assertion here traces to the 2026-09-07 controlled live experiment; a
change that makes one of these fail is a change to real exchange-write
semantics, not a refactor.
"""
import pytest

from telegram_kol_research.deepcoin_limit_entry import (
    FORBIDDEN_LIMIT_ENTRY_FIELDS,
    LIMIT_ENTRY_PAYLOAD_FIELDS,
    DeepcoinLimitEntryError,
    build_deepcoin_limit_entry_payload,
    limit_leg_requires_trigger_order,
)

DRAFT = {"instrument_id": "ETH-USDT-SWAP"}
LONG_LEG = {
    "side": "buy",
    "position_side": "long",
    "order_type": "limit",
    "price": 2500.0,
    "quantity": 0.1,
    "client_order_id": "KOL-1-1",
    "allocation_pct": 50.0,
}
SHORT_LEG = {**LONG_LEG, "side": "sell", "position_side": "short"}


def build(leg=None, **kwargs):
    params = {
        "margin_mode": "cross",
        "position_mode": "split",
        "stop_loss": 2490.0,
        "take_profit": 2510.0,
    }
    params.update(kwargs)
    return build_deepcoin_limit_entry_payload(DRAFT, leg or LONG_LEG, **params)


def test_the_payload_is_exactly_the_accepted_field_set():
    payload = build()
    assert set(payload) == LIMIT_ENTRY_PAYLOAD_FIELDS
    assert payload == {
        "instId": "ETH-USDT-SWAP",
        "tdMode": "cross",
        "mrgPosition": "split",
        "side": "buy",
        "posSide": "long",
        "ordType": "limit",
        "px": "2500.0",
        "sz": "0.1",
        "tpTriggerPx": "2510.0",
        "slTriggerPx": "2490.0",
    }


def test_no_client_order_id_even_though_the_leg_carries_one():
    # Cells 6b and 6d: the field's presence alone drew sCode=14 DuplicateAction.
    payload = build()
    assert "clOrdId" not in payload
    assert LONG_LEG["client_order_id"], "the leg still has one; it is simply not sent"


def test_none_of_the_trigger_order_vocabulary_leaks_in():
    payload = build()
    assert not set(payload) & FORBIDDEN_LIMIT_ENTRY_FIELDS
    assert "clOrdId" in FORBIDDEN_LIMIT_ENTRY_FIELDS
    assert {"slOrdPx", "tpOrdPx", "triggerPrice", "triggerPxType"} <= FORBIDDEN_LIMIT_ENTRY_FIELDS


def test_a_missing_take_profit_still_sends_the_stop():
    payload = build(take_profit=None)
    assert payload["slTriggerPx"] == "2490.0"
    assert "tpTriggerPx" not in payload
    assert set(payload) <= LIMIT_ENTRY_PAYLOAD_FIELDS


def test_short_leg_inverts_the_protection_sides():
    payload = build(SHORT_LEG, stop_loss=2510.0, take_profit=2490.0)
    assert (payload["side"], payload["posSide"]) == ("sell", "short")
    assert float(payload["tpTriggerPx"]) < float(payload["px"]) < float(payload["slTriggerPx"])


@pytest.mark.parametrize(
    "leg,kwargs,reason",
    [
        ({**LONG_LEG, "quantity": 0}, {}, "non_positive_quantity"),
        ({**LONG_LEG, "quantity": None}, {}, "non_positive_quantity"),
        ({**LONG_LEG, "price": 0}, {}, "non_positive_price"),
        (LONG_LEG, {"stop_loss": 0}, "missing_stop_loss_for_protection"),
        (LONG_LEG, {"stop_loss": None}, "missing_stop_loss_for_protection"),
        ({**LONG_LEG, "position_side": "flat"}, {}, "unsupported_position_side"),
        ({**LONG_LEG, "side": "short"}, {}, "unsupported_side"),
    ],
)
def test_an_unusable_leg_refuses_instead_of_sending(leg, kwargs, reason):
    with pytest.raises(DeepcoinLimitEntryError) as excinfo:
        build(leg, **kwargs)
    assert reason in str(excinfo.value)


def test_protection_on_the_wrong_side_of_the_entry_refuses():
    # A stop above a long entry would arm the moment the order fills.
    with pytest.raises(DeepcoinLimitEntryError, match="stop_loss_on_wrong_side"):
        build(stop_loss=2510.0, take_profit=2520.0)
    with pytest.raises(DeepcoinLimitEntryError, match="take_profit_on_wrong_side"):
        build(take_profit=2490.0, stop_loss=2480.0)
    with pytest.raises(DeepcoinLimitEntryError, match="stop_loss_on_wrong_side"):
        build(SHORT_LEG, stop_loss=2490.0, take_profit=2480.0)


def test_a_plain_limit_leg_migrates():
    assert limit_leg_requires_trigger_order(LONG_LEG, DRAFT) is None
    assert limit_leg_requires_trigger_order(SHORT_LEG, DRAFT) is None


def test_a_market_leg_is_not_a_limit_leg():
    assert limit_leg_requires_trigger_order({**LONG_LEG, "order_type": "market"}) == (
        "not_a_plain_limit_leg"
    )
    assert limit_leg_requires_trigger_order({**LONG_LEG, "order_type": "market_on_trigger"}) == (
        "not_a_plain_limit_leg"
    )


def test_a_trigger_price_equal_to_the_limit_price_carries_no_trigger_semantics():
    # This is exactly the shape build_deepcoin_trigger_order_payload sends today.
    leg = {**LONG_LEG, "trigger_price": 2500.0}
    assert limit_leg_requires_trigger_order(leg, DRAFT) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"trigger_price": 2530.0},
        {"trigger_px": 2470.0},
        {"trigger_price_type": "mark"},
        {"triggerPxType": "index"},
        {"breakout_price": 2530.0},
        {"pullback_price": 2470.0},
        {"activation_price": 2530.0},
        {"price_source": "last"},
        {"condition": "close_above"},
        {"conditional_entry": True},
    ],
)
def test_a_leg_with_real_trigger_semantics_keeps_trigger_order(extra):
    reason = limit_leg_requires_trigger_order({**LONG_LEG, **extra}, DRAFT)
    assert reason is not None and "carries_trigger_semantics" in reason


def test_trigger_semantics_on_the_draft_also_block_migration():
    reason = limit_leg_requires_trigger_order(LONG_LEG, {**DRAFT, "trigger_price_type": "mark"})
    assert reason is not None and reason.startswith("draft_field_")


def test_an_empty_trigger_field_does_not_block_migration():
    for value in (None, "", [], {}, 0):
        assert limit_leg_requires_trigger_order({**LONG_LEG, "trigger_price": value}, DRAFT) is None


def test_a_leg_without_a_usable_price_never_migrates():
    assert limit_leg_requires_trigger_order({**LONG_LEG, "price": None}, DRAFT) == (
        "leg_price_not_usable_as_limit_price"
    )
    assert limit_leg_requires_trigger_order({**LONG_LEG, "price": -1}, DRAFT) == (
        "leg_price_not_usable_as_limit_price"
    )


def test_every_limit_leg_the_draft_builder_produces_today_migrates():
    from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft

    draft = build_deepcoin_order_draft(
        {
            "venue": "deepcoin",
            "order_type": "limit",
            "contract": "ETHUSDT",
            "open_side": "buy",
            "position_side": "long",
            "entry_range": "2480-2500",
            "risk_budget_usdt": 50,
            "stop_loss": 2400,
            "source": {"kol_id": "k", "chat_id": -1, "message_id": 1},
        }
    )
    limit_legs = [leg for leg in draft["order_legs"] if leg.get("order_type") == "limit"]
    assert limit_legs, "the fixture must produce at least one limit leg"
    for leg in limit_legs:
        assert limit_leg_requires_trigger_order(leg, draft) is None
