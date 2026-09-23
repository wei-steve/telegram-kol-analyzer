"""Phase 2.1: the deterministic position-sizing word table.

Section 4.2.1 of
``docs/plans/2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md``.
The table only ever produces ``0 < m < 1``; "full size" and "no sizing word"
are the same answer -- nothing -- so no preamble row is written for either.
"""

from decimal import Decimal

import pytest

from telegram_kol_research.entry_position_sizing_terms import (
    entry_position_risk_multiplier,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("比特币市价86500附近，半仓入场做个短线空单", Decimal("0.5")),
        ("半仓", Decimal("0.5")),
        ("轻仓进场", Decimal("0.5")),
        ("三成仓位入场", Decimal("0.3")),
        ("一成仓", Decimal("0.1")),
        ("两成仓位", Decimal("0.2")),
        ("二成仓位", Decimal("0.2")),
        ("九成仓", Decimal("0.9")),
        ("20%仓位入场", Decimal("0.2")),
        ("仓位 20%", Decimal("0.2")),
        ("50%仓", Decimal("0.5")),
        ("BTC 现价入场，5%仓位", Decimal("0.05")),
    ],
)
def test_sizing_words_map_to_their_multiplier(text, expected):
    assert entry_position_risk_multiplier(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "正常仓位操作",
        "重仓干",
        "满仓入场",
        "十成仓",
        "100%仓位",
        "BTC 86500 做空，止损 87200",
        "",
        None,
        "0%仓位",
        "120%仓位",
        "半仓还是轻仓无所谓，三成仓位也行",  # conflicting words -> nothing
        "五成仓，另外20%仓位补",  # conflicting words -> nothing
        "止盈了50%",  # a percentage that is not a position size
        "回撤了30%",
    ],
)
def test_texts_without_a_single_usable_sizing_word_produce_nothing(text):
    assert entry_position_risk_multiplier(text) is None


def test_the_same_multiplier_stated_twice_is_not_a_conflict():
    assert entry_position_risk_multiplier("半仓入场，就半仓") == Decimal("0.5")
    assert entry_position_risk_multiplier("五成仓位，50%仓位") == Decimal("0.5")
