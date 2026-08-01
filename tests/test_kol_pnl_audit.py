from decimal import Decimal

import pytest

from telegram_kol_research.kol_pnl_audit import (
    AuditValidationError,
    NormalizedAuditStrategy,
)


def _strategy_payload(**overrides):
    payload = {
        "audit_id": "-1002368892075:6496:BTC:long:1",
        "chat_id": -1002368892075,
        "symbol": "BTC",
        "side": "long",
        "ordinal": 1,
        "published_at": "2026-06-08T01:17:06Z",
        "evidence": [
            {
                "message_id": 6496,
                "posted_at": "2026-06-08T01:17:06Z",
                "role": "strategy",
            }
        ],
        "entry_legs": [{"price": "62450", "allocation_pct": "100"}],
        "stop": {"price": "61700", "trigger": "close", "interval": "15m"},
        "take_profits": ["62950", "63250", "64050", "66500"],
        "management_events": [],
        "confidence": "high",
        "reason_codes": [],
    }
    payload.update(overrides)
    return payload


def test_contract_applies_approved_take_profit_allocations():
    strategy = NormalizedAuditStrategy.from_dict(_strategy_payload())

    assert strategy.audit_id == "-1002368892075:6496:BTC:long:1"
    assert strategy.entry_legs[0].price == Decimal("62450")
    assert strategy.take_profit_allocations == (
        Decimal("40"),
        Decimal("20"),
        Decimal("20"),
        Decimal("20"),
    )
    assert strategy.to_dict()["published_at"] == "2026-06-08T01:17:06Z"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"evidence": []}, "message evidence"),
        ({"side": "flat"}, "side"),
        ({"entry_legs": [{"price": "0", "allocation_pct": "100"}]}, "positive"),
        ({"stop": {"price": "63000", "trigger": "touch"}}, "loss side"),
        ({"take_profits": ["62000"]}, "profitable order"),
        (
            {"take_profits": ["62500", "62600", "62700", "62800", "62900", "63000"]},
            "one through five",
        ),
        (
            {
                "take_profits": [
                    {"price": "62950", "allocation_pct": "60"},
                    {"price": "63250", "allocation_pct": "30"},
                ]
            },
            "total 100",
        ),
    ],
)
def test_validation_rejects_unsafe_strategy_contracts(change, error):
    with pytest.raises(AuditValidationError, match=error):
        NormalizedAuditStrategy.from_dict(_strategy_payload(**change))
