from decimal import Decimal
import json
from pathlib import Path

import pytest

from telegram_kol_research.kol_pnl_audit import (
    AuditValidationError,
    NormalizedAuditStrategy,
    load_audit_messages,
    load_reviewed_decisions,
    reconstruct_audit_strategies,
)


FIXTURES = Path(__file__).parent / "fixtures" / "kol_pnl_audit"


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


def _reconstruction():
    messages = load_audit_messages(
        json.loads((FIXTURES / "messages.json").read_text(encoding="utf-8"))
    )
    decisions = load_reviewed_decisions(
        json.loads((FIXTURES / "decisions.json").read_text(encoding="utf-8"))
    )
    return reconstruct_audit_strategies(messages, decisions)


def test_reconstruction_splits_composite_messages_and_corrects_reviewed_typo():
    result = _reconstruction()

    assert len(result.strategies) == 6
    assert [
        row.audit_id for row in result.strategies if row.evidence[0].message_id == 1003
    ] == [
        "-1002368892075:1003:BTC:short:1",
        "-1002368892075:1003:BTC:long:2",
    ]
    corrected = next(row for row in result.strategies if row.evidence[0].message_id == 1006)
    assert [item.price for item in corrected.take_profits] == [
        Decimal("59300"), Decimal("59600"), Decimal("60350")
    ]
    assert corrected.reason_codes == ("reviewed_numeric_correction",)


def test_reconstruction_merges_duplicate_and_links_management_events():
    result = _reconstruction()
    strategy = next(row for row in result.strategies if row.evidence[0].message_id == 1001)

    assert [(item.message_id, item.role) for item in strategy.evidence] == [
        (1001, "strategy"),
        (1004, "duplicate_continuation"),
        (1005, "management"),
        (1007, "management"),
    ]
    assert [item.event_type for item in strategy.management_events] == [
        "move_stop_to_break_even",
        "target_update",
    ]
    assert strategy.management_events[1].price == Decimal("65300")


def test_reconstruction_records_exclusions_and_unresolved_events():
    result = _reconstruction()

    assert [(item.message_id, item.reason) for item in result.excluded] == [
        (1002, "promotional_excluded")
    ]
    assert [(item.message_id, item.reason) for item in result.unresolved] == [
        (1010, "ambiguous_event_target")
    ]


def test_reconstruction_fails_closed_for_unreviewed_strategy_candidate():
    messages = load_audit_messages(
        json.loads((FIXTURES / "messages.json").read_text(encoding="utf-8"))
        + [{
            "chat_id": -1002368892075,
            "message_id": 1011,
            "posted_at": "2026-07-29T01:43:19Z",
            "text": "比特币65100附近空，止损65800，止盈64600-64250。",
        }]
    )
    decisions = load_reviewed_decisions(
        json.loads((FIXTURES / "decisions.json").read_text(encoding="utf-8"))
    )

    result = reconstruct_audit_strategies(messages, decisions)

    assert (1011, "unreviewed_strategy_candidate") in [
        (item.message_id, item.reason) for item in result.unresolved
    ]
