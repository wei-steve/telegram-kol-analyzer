from datetime import UTC, datetime, timedelta
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
    replay_audit_strategy,
)
from telegram_kol_research.kol_audit_market_data import AuditCandle


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


def _candle(minutes, *, open, high, low, close, width=5):
    opened_at = datetime(2026, 6, 8, 1, 20, tzinfo=UTC) + timedelta(minutes=minutes)
    return AuditCandle(
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=width) - timedelta(milliseconds=1),
        open=Decimal(str(open)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
    )


def _replay_strategy(**overrides):
    return NormalizedAuditStrategy.from_dict(_strategy_payload(**overrides))


def test_replay_does_not_fill_from_candle_that_opened_before_publication():
    strategy = _replay_strategy(published_at="2026-06-08T01:22:00Z")
    candles = [
        _candle(0, open=62500, high=62500, low=62400, close=62450),
        _candle(5, open=62600, high=62650, low=62550, close=62600),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 30, tzinfo=UTC)
    )

    assert result.status == "unfilled"
    assert result.entry_price is None


def test_replay_uses_weighted_price_after_two_entry_legs_fill():
    strategy = _replay_strategy(
        entry_legs=[
            {"price": "62450", "allocation_pct": "60"},
            {"price": "62300", "allocation_pct": "40"},
        ]
    )
    candles = [
        _candle(0, open=62500, high=62520, low=62440, close=62460),
        _candle(5, open=62400, high=62410, low=62290, close=62310),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 30, tzinfo=UTC)
    )

    assert result.status == "open"
    assert result.entry_price == Decimal("62390")
    assert result.filled_entry_allocation_pct == Decimal("100")
    assert result.open_allocation_pct == Decimal("100")


def test_replay_keeps_unfilled_second_leg_pending_without_inventing_size():
    strategy = _replay_strategy(
        entry_legs=[
            {"price": "62450", "allocation_pct": "60"},
            {"price": "62000", "allocation_pct": "40"},
        ]
    )
    result = replay_audit_strategy(
        strategy,
        [_candle(0, open=62500, high=62520, low=62440, close=62460)],
        cutoff=datetime(2026, 6, 8, 1, 25, tzinfo=UTC),
    )

    assert result.status == "open"
    assert result.filled_entry_allocation_pct == Decimal("60")
    assert result.open_allocation_pct == Decimal("100")
    assert "pending_entry_leg" in result.reason_codes


def test_replay_applies_staged_targets_and_exact_r():
    strategy = _replay_strategy(
        take_profits=["62950", "63250"],
    )
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(5, open=62500, high=63000, low=62480, close=62900),
        _candle(10, open=63000, high=63300, low=62950, close=63200),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 40, tzinfo=UTC)
    )

    assert result.status == "closed"
    assert result.exit_reason == "take_profit"
    assert result.targets_reached == 2
    assert float(result.realized_r) == pytest.approx(13 / 15)
    assert result.open_allocation_pct == 0


def test_replay_does_not_move_stop_to_break_even_without_explicit_event():
    strategy = _replay_strategy(take_profits=["62950", "63250"])
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(5, open=62500, high=63000, low=62480, close=62900),
        _candle(10, open=62400, high=62420, low=61650, close=61680, width=15),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 40, tzinfo=UTC)
    )

    assert result.status == "closed"
    assert result.exit_reason == "stop_loss"
    assert float(result.realized_r) == pytest.approx(-1 / 6)


def test_replay_explicit_protection_moves_only_remaining_position_to_break_even():
    strategy = _replay_strategy(
        take_profits=["62950", "63250"],
        management_events=[{
            "event_type": "move_stop_to_break_even",
            "message_id": 6497,
            "occurred_at": "2026-06-08T01:31:00Z",
        }],
    )
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(5, open=62500, high=63000, low=62480, close=62900),
        _candle(15, open=62500, high=62510, low=62400, close=62440),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 40, tzinfo=UTC)
    )

    assert result.status == "closed"
    assert result.exit_reason == "break_even"
    assert float(result.realized_r) == pytest.approx(1 / 3)


def test_replay_applies_explicit_stop_price_and_close_interval_update():
    strategy = _replay_strategy(management_events=[{
        "event_type": "stop_update",
        "message_id": 6497,
        "occurred_at": "2026-06-08T01:26:00Z",
        "price": "62300",
        "trigger": "close",
        "interval": "5m",
    }])
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(10, open=62400, high=62420, low=62200, close=62250),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 40, tzinfo=UTC)
    )

    assert result.status == "closed"
    assert result.exit_reason == "stop_loss"
    assert result.exits[0].price == Decimal("62300")
    assert result.realized_r == Decimal("-0.2")
    serialized = strategy.to_dict()["management_events"][0]
    assert serialized["trigger"] == "close"
    assert serialized["interval"] == "5m"


@pytest.mark.parametrize(
    ("trigger", "interval", "expected"),
    [
        ("touch", None, "closed"),
        ("close", "15m", "open"),
    ],
)
def test_replay_distinguishes_touch_and_close_qualified_stops(
    trigger, interval, expected
):
    stop = {"price": "61700", "trigger": trigger}
    if interval:
        stop["interval"] = interval
    strategy = _replay_strategy(stop=stop)
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(5, open=62300, high=62320, low=61650, close=61800),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 31, tzinfo=UTC)
    )

    assert result.status == expected


def test_replay_explicit_partial_then_full_exit_uses_next_candle_open():
    strategy = _replay_strategy(management_events=[
        {
            "event_type": "partial_exit",
            "message_id": 6497,
            "occurred_at": "2026-06-08T01:26:00Z",
            "allocation_pct": "30",
        },
        {
            "event_type": "full_exit",
            "message_id": 6498,
            "occurred_at": "2026-06-08T01:31:00Z",
        },
    ])
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(10, open=62700, high=62720, low=62680, close=62710),
        _candle(15, open=62600, high=62620, low=62580, close=62610),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 40, tzinfo=UTC)
    )

    assert result.status == "closed"
    assert result.exit_reason == "kol_full_exit"
    assert [item.price for item in result.exits] == [Decimal("62700"), Decimal("62600")]
    assert [item.allocation_pct for item in result.exits] == [Decimal("30"), Decimal("70")]


def test_replay_cancel_before_entry_prevents_future_fill():
    strategy = _replay_strategy(management_events=[{
        "event_type": "cancel_pending_entry",
        "message_id": 6497,
        "occurred_at": "2026-06-08T01:21:00Z",
    }])

    result = replay_audit_strategy(
        strategy,
        [_candle(5, open=62450, high=62500, low=62400, close=62480)],
        cutoff=datetime(2026, 6, 8, 1, 30, tzinfo=UTC),
    )

    assert result.status == "cancelled"
    assert result.entry_price is None


def test_replay_same_candle_stop_and_target_uses_adverse_order():
    strategy = _replay_strategy(
        stop={"price": "61700", "trigger": "touch"},
        take_profits=["62950"],
    )
    candles = [
        _candle(0, open=62450, high=62500, low=62400, close=62480),
        _candle(5, open=62400, high=63000, low=61600, close=62500),
    ]

    result = replay_audit_strategy(
        strategy, candles, cutoff=datetime(2026, 6, 8, 1, 30, tzinfo=UTC)
    )

    assert result.exit_reason == "stop_loss"
    assert result.realized_r == Decimal("-1")
    assert "intrabar_order_uncertain" in result.reason_codes
