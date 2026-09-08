"""A KOL signature is message text; it must never supply a price."""

import pytest

from telegram_kol_research.contact_digit_scrubbing import (
    scrub_contact_identifiers,
    text_carries_contact_identifier,
)
from telegram_kol_research.management_directives import (
    _text_contains_explicit_stop_value,
    build_management_instruction_contract,
    resolve_management_directive,
)
from telegram_kol_research.message_recognition import (
    _clean_bitcoin_junzhang_text,
    _extract_entry_confirmation_price,
    _extract_explicit_stop_loss_from_management_text,
    _extract_labeled_entry_text,
)


# Production text, read from ``raw_messages`` on 2026-09-08. Both messages had
# their signature's QQ number parsed as an explicit stop price: 15013 as a BTC
# stop against an entry near 79519, 15402 as an ETH stop against an entry near
# 2484.67. Both are fixed cases; do not paraphrase them.
RAW_15402 = (
    "大镖客·Andy\n"
    "第一止盈位已过，注意锁定利润，及时移动止损！\n"
    "@Tarderfengge QQ:158241758"
)
RAW_15013 = (
    "大镖客·Andy\n"
    "可以收了，太久了，第一止盈位只差几十点了，注意锁定利润，及时移动止损！\n"
    "@Tarderfengge QQ:158241758"
)
SIGNATURE_DIGITS = "158241758"


@pytest.mark.parametrize("text", [RAW_15402, RAW_15013])
def test_signature_digits_are_not_read_as_a_stop_price(text):
    """Path one: the extractor's 20-character window.

    ``！\n@Tarderfengge QQ:`` is 19 non-digit characters, so ``止损`` reached
    the signature's digits and returned them as the stop price.
    """

    assert _extract_explicit_stop_loss_from_management_text(text) is None


# The two injection paths are independent and each is covered on its own: the
# extractor reads a number out of the text, the provenance check endorses a
# number the model supplied. Blocking one would have left the other open.


@pytest.mark.parametrize("text", [RAW_15402, RAW_15013])
def test_provenance_no_longer_endorses_a_signature_number(text):
    """Path two: ``_text_contains_explicit_stop_value``'s 32-character window.

    The QQ number really is in the message and really does sit within 32
    characters of ``止损``, so the window used to endorse it as coming from the
    current message -- which is exactly the evidence the contract requires
    before a stop counts as explicit.
    """

    lowered = text.strip().lower()

    assert _text_contains_explicit_stop_value(lowered, SIGNATURE_DIGITS) is False
    # The check still does its job for a price the message really names.
    assert (
        _text_contains_explicit_stop_value("eth 空单，止损上移到 2530", "2530")
        is True
    )


@pytest.mark.parametrize("text", [RAW_15402, RAW_15013])
def test_signature_digits_do_not_become_the_contract_stop_price(text):
    event = {
        "event_type": "position_update",
        "management_action": "partial_take_profit",
        "stop_loss": SIGNATURE_DIGITS,
        "symbol": "ETH",
        "side": "short",
    }

    directive = resolve_management_directive(text=text, lifecycle_event=event)

    # The instruction itself survives: half off, then protect the rest.
    assert directive.intent == "partial_then_break_even"
    assert directive.fraction == pytest.approx(0.5)
    assert directive.stop_loss is None
    assert directive.stop_price_source is None

    contract = build_management_instruction_contract(
        text=text, lifecycle_event=event
    )
    assert contract.stop_mode == "actual_entry_price"
    assert contract.stop_price is None
    assert contract.stop_price_source is None


@pytest.mark.parametrize("text", [RAW_15402, RAW_15013])
def test_scrubbing_leaves_the_stored_text_untouched(text):
    scrubbed = scrub_contact_identifiers(text)

    assert scrubbed != text
    assert SIGNATURE_DIGITS not in scrubbed
    # Length preserving, so every proximity window downstream still measures
    # the same distances it measured before.
    assert len(scrubbed) == len(text)
    # Only the signature is removed; the instruction's own words remain.
    assert "移动止损" in scrubbed
    assert "锁定利润" in scrubbed


def test_a_real_stop_price_is_unaffected():
    text = "ETH 空单，第一止盈位已过，减仓一半，止损上移到 2530"
    event = {
        "event_type": "position_update",
        "management_action": "partial_take_profit",
        "stop_loss": "2530",
        "symbol": "ETH",
        "side": "short",
    }

    assert _extract_explicit_stop_loss_from_management_text(text) == 2530.0

    contract = build_management_instruction_contract(
        text=text, lifecycle_event=event
    )
    assert contract.stop_mode == "explicit_price"
    assert contract.stop_price == "2530"
    assert contract.stop_price_source == "current_message_text"


@pytest.mark.parametrize(
    "text",
    [
        "@Tarderfengge ETH 空单，止损上移到 2530",
        "ETH 空单，止损上移到 2530 @Tarderfengge",
        "@Tarderfengge ETH 空单，止损上移到 2530 @andy_trader",
    ],
)
def test_a_handle_before_or_after_the_price_does_not_move_it(text):
    assert _extract_explicit_stop_loss_from_management_text(text) == 2530.0


def test_an_at_sign_price_is_not_a_handle():
    # ``@2530`` is a price written with an at sign, not a Telegram handle.
    assert scrub_contact_identifiers("止损@2530") == "止损@2530"
    assert _extract_explicit_stop_loss_from_management_text("止损改@2530") == 2530.0


@pytest.mark.parametrize(
    "text",
    [
        "qqqusdt现在的价格是：718.6，高于开仓价格717，平了吧",
        "纳斯达克指数QQQ临近了 680 支撑位",
        "谷歌和QQQ可以抄底吗？",
    ],
)
def test_the_qqq_ticker_is_not_a_qq_number(text):
    assert scrub_contact_identifiers(text) == text


@pytest.mark.parametrize(
    "text,removed",
    [
        ("QQ:158241758", "158241758"),
        ("QQ 158241758", "158241758"),
        ("扣扣：158241758", "158241758"),
        ("微信：trader_andy", "trader_andy"),
        ("VX: trader_andy", "trader_andy"),
        ("vx号 trader_andy", "trader_andy"),
        ("电话:13812345678", "13812345678"),
        ("手机号 138-1234-5678", "138-1234-5678"),
        ("@Tarderfengge", "@Tarderfengge"),
        ("订单号 1001125163581378", "1001125163581378"),
    ],
)
def test_each_contact_shape_is_removed(text, removed):
    assert text_carries_contact_identifier(text)
    assert removed not in scrub_contact_identifiers(text)


@pytest.mark.parametrize(
    "text",
    [
        "止损 2530",
        "入场 2484.67",
        "止盈 2470.15",
        "BTC 79519",
        # Eight digits is short of the bare-run threshold and stays readable.
        "12345678",
    ],
)
def test_prices_are_never_scrubbed(text):
    assert scrub_contact_identifiers(text) == text
    assert not text_carries_contact_identifier(text)


def test_quantity_extraction_sees_the_same_scrubbed_text():
    text = "第一止盈位已过，减仓50%\n@Tarderfengge QQ:158241758"
    event = {
        "event_type": "position_update",
        "management_action": "partial_take_profit",
        "symbol": "ETH",
        "side": "short",
    }

    directive = resolve_management_directive(text=text, lifecycle_event=event)

    assert directive.intent == "partial_take_profit"
    assert directive.fraction == pytest.approx(0.5)


def test_entry_confirmation_price_ignores_the_signature():
    text = "BTC 已进场\n@Tarderfengge QQ:158241758"

    assert _extract_entry_confirmation_price(text, "BTC") is None
    assert (
        _extract_entry_confirmation_price(
            "BTC 已进场 79519\n@Tarderfengge QQ:158241758", "BTC"
        )
        == 79519.0
    )


def test_labeled_entry_text_ignores_the_signature():
    assert (
        _extract_labeled_entry_text("入场 QQ:158241758") is None
    )
    assert _extract_labeled_entry_text("入场 2484.67") == "2484.67"


def test_line_breaks_survive_a_separator_that_reaches_across_one():
    # ``微信：\n2484.67`` must not become one line: a line-oriented extractor
    # would then read the label and the price as if they belonged together.
    scrubbed = scrub_contact_identifiers("入场\n微信：\n2484.67")

    assert scrubbed.splitlines() == ["入场", "   ", "       "]
    assert _extract_labeled_entry_text("入场\n微信：\n2484.67") is None


def test_a_distance_in_points_never_becomes_an_explicit_stop_price():
    """raw 13632, found by the 30-day replay. Locks the safe end state.

    With the signature gone, the second extraction pattern reaches ``只差2点``
    and reads the distance ``2`` as a price. That defect predates this change
    -- the signature merely won the race for it in this group -- and belongs
    with the relative-unit handling in ``entry_price_geometry``, not with a
    special case here. What must never regress is the outcome: two independent
    guards keep the ``2`` from becoming an explicit stop, and this pins that.
    """

    text = (
        "大镖客·Andy\n"
        "刚才2503，只差2点到第一止盈，注意锁定利润，及时移动止损！\n"
        "@Tarderfengge QQ:158241758"
    )
    event = {
        "event_type": "position_update",
        "management_action": "partial_take_profit",
        "stop_loss": "2.0",
        "symbol": "ETH",
        "side": "short",
    }

    # The QQ number is gone, which is what this step is for.
    assert _extract_explicit_stop_loss_from_management_text(text) != 158241758.0

    # The distance never earns current-message provenance, so no explicit
    # price reaches the contract or the gate.
    directive = resolve_management_directive(text=text, lifecycle_event=event)
    assert directive.stop_loss is None
    assert directive.stop_price_source is None
    contract = build_management_instruction_contract(
        text=text, lifecycle_event=event
    )
    assert contract.stop_mode == "actual_entry_price"
    assert contract.stop_price is None


def test_the_profile_cleaner_uses_the_shared_rule():
    cleaned = _clean_bitcoin_junzhang_text(
        "ETH 现价开一层空 止损 2530\n@Tarderfengge QQ:158241758"
    )

    assert SIGNATURE_DIGITS not in cleaned
    assert "@Tarderfengge" not in cleaned
    assert "止损 2530" in cleaned
