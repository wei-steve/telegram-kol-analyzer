"""A-14. Fixtures are verbatim production responses, not echoes of our requests.

A-13 found that the break-even executor's tests passed while production refused
every candidate, because the fake client built its pending rows by echoing the
request payload back -- so the fixtures carried ``posId`` and ``slTriggerPx``,
neither of which the real endpoint returns. Every row below was captured from
the live venue on 2026-09-10 and is pasted unchanged.
"""

from __future__ import annotations

from telegram_kol_research.deepcoin_trigger_rows import (
    entry_attached_stop_trigger_price,
    position_id_or_none,
    stop_trigger_price,
    take_profit_trigger_price,
    trigger_price,
)

# A live long position, stop attached, no take-profit. Note that tpTriggerPx is
# present as a key and empty as a value -- "absent" and "empty" are the same
# answer here and both must read as None.
LIVE_POSITION_ROW = {
    "avgPx": "77000", "cTime": "1789056476000", "ccy": "USDT",
    "instId": "BTC-USDT-SWAP", "instType": "SWAP", "isFollow": False,
    "isLeading": False, "lastPx": "77156.4", "lever": "125",
    "liqPx": "68116.6", "mgnMode": "cross", "mrgPosition": "split",
    "pos": "15", "posId": "1001125216121996", "posSide": "long",
    "slTriggerPx": "75700", "tpTriggerPx": "", "uTime": "1789056476000",
    "unrealizedProfit": "2.3459999999999126", "useMargin": "9.24",
}

# The TPSL row protecting that position. It carries no posId of any kind.
LIVE_TPSL_ROW = {
    "cTime": "1789056476000", "closeSLPrice": "", "closeSLTriggerPrice": "",
    "closeTPPrice": "", "closeTPTriggerPrice": "", "instId": "BTC-USDT-SWAP",
    "instType": "SWAP", "lever": "125", "ordId": "1001125216121995",
    "ordPx": "0", "ordType": "", "posSide": "long", "side": "sell",
    "slPrice": "0", "slTriggerPrice": "75700", "sz": "15", "tdMode": "cross",
    "tpPrice": "0", "tpTriggerPrice": "0", "triggerOrderType": "TPSL",
    "triggerPx": "0", "triggerPxType": "last", "uTime": "1789056476000",
}

# An unfilled conditional entry. Its stop lives in closeSLTriggerPrice and
# protects nothing yet.
LIVE_CONDITIONAL_ENTRY_ROW = {
    "cTime": "1788798775000", "closeSLPrice": "0",
    "closeSLTriggerPrice": "83000", "closeTPPrice": "0",
    "closeTPTriggerPrice": "0", "instId": "BTC-USDT-SWAP", "instType": "SWAP",
    "lever": "125", "ordId": "1001125173252560", "ordPx": "81910",
    "ordType": "", "posSide": "short", "side": "sell", "slPrice": "",
    "slTriggerPrice": "", "sz": "6", "tdMode": "cross", "tpPrice": "",
    "tpTriggerPrice": "", "triggerOrderType": "Conditional",
    "triggerPx": "81910", "triggerPxType": "last", "uTime": "1788798775000",
}


def test_a_tpsl_row_has_no_position_id_and_says_so():
    """The read that broke automatic break-even convergence.

    Comparing "" against a real position id is False forever, and the caller
    read that as drift. None is the honest answer, and it forces the caller to
    attribute by order id or trade unit instead.
    """

    assert position_id_or_none(LIVE_TPSL_ROW) is None
    assert position_id_or_none(LIVE_CONDITIONAL_ENTRY_ROW) is None
    assert position_id_or_none(LIVE_POSITION_ROW) == "1001125216121996"


def test_the_stop_price_is_found_under_whichever_name_the_row_uses():
    assert stop_trigger_price(LIVE_POSITION_ROW) == "75700"
    assert stop_trigger_price(LIVE_TPSL_ROW) == "75700"


def test_an_empty_value_reads_as_absent_not_as_empty_text():
    """``tpTriggerPx`` is present and empty on a stop-only position."""

    assert take_profit_trigger_price(LIVE_POSITION_ROW) is None


def test_an_entry_attached_stop_is_not_the_position_stop():
    """Kept separate on purpose: this price protects nothing yet."""

    assert stop_trigger_price(LIVE_CONDITIONAL_ENTRY_ROW) is None
    assert entry_attached_stop_trigger_price(LIVE_CONDITIONAL_ENTRY_ROW) == "83000"
    # And the reverse: a real TPSL row carries no entry-attached stop.
    assert entry_attached_stop_trigger_price(LIVE_TPSL_ROW) is None


def test_the_conditional_trigger_price_is_read_from_the_right_key():
    assert trigger_price(LIVE_CONDITIONAL_ENTRY_ROW) == "81910"
    # A TPSL row's triggerPx is "0" -- present, meaningless, and not a price.
    assert trigger_price(LIVE_TPSL_ROW) == "0"


def test_the_readers_never_raise_on_a_row_missing_every_key():
    empty: dict[str, object] = {}
    assert stop_trigger_price(empty) is None
    assert take_profit_trigger_price(empty) is None
    assert entry_attached_stop_trigger_price(empty) is None
    assert position_id_or_none(empty) is None
    assert trigger_price(empty) is None


def test_the_union_reader_keeps_the_venue_value_and_skips_zeroes():
    """The four sites it replaced all wanted "first present, non-zero"."""

    from telegram_kol_research.deepcoin_trigger_rows import (
        any_trigger_price_including_entry_attached as any_price,
    )

    assert any_price(LIVE_TPSL_ROW, kind="sl") == "75700"
    # tpTriggerPrice is "0" on this row: a leg the venue is not using.
    assert any_price(LIVE_TPSL_ROW, kind="tp") is None
    # A conditional entry's stop is found only through the union reader.
    assert any_price(LIVE_CONDITIONAL_ENTRY_ROW, kind="sl") == "83000"
    # Order rows never carry the position spelling, and one caller reads only
    # order rows, so it must be possible to exclude it.
    assert any_price(LIVE_POSITION_ROW, kind="sl") == "75700"
    assert (
        any_price(LIVE_POSITION_ROW, kind="sl", include_position_field=False)
        is None
    )


def test_unreadable_take_profit_still_counts_as_present():
    """Fail-closed, and NaN is unreadable however willingly float() parses it.

    The predicate this replaced used Decimal, which rejects NaN outright. A
    float-based rewrite quietly turned "unreadable" into "absent", which is the
    unsafe direction; an existing branch test caught it.
    """

    from telegram_kol_research.deepcoin_trigger_rows import (
        take_profit_present_failing_closed as present,
    )

    assert present({"tpTriggerPrice": "not-a-number"}) is True
    assert present({"tpTriggerPrice": "NaN"}) is True
    assert present({"tpTriggerPrice": "63900"}) is True
    assert present({"tpTriggerPrice": "0", "closeTPTriggerPrice": "0"}) is False
    assert present({"tpTriggerPx": "-1"}) is False
    assert present(LIVE_POSITION_ROW) is False
