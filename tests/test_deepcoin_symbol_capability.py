from datetime import datetime, timedelta, timezone

import pytest

from telegram_kol_research.deepcoin_contract_spec_cache import (
    validate_deepcoin_instrument_snapshot,
)
from telegram_kol_research.deepcoin_symbol_capability import (
    decide_deepcoin_symbol_capability,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _snapshot(*, instrument_id="BTC-USDT-SWAP", state="live", age=timedelta(0)):
    return validate_deepcoin_instrument_snapshot(
        [
            {
                "instType": "SWAP",
                "instId": instrument_id,
                "ctVal": "1",
                "lotSz": "1",
                "minSz": "1",
                "tickSz": "0.001",
                "state": state,
            }
        ],
        fetched_at=NOW - age,
        ttl=timedelta(hours=24),
    )


@pytest.mark.parametrize(
    ("symbol", "global_allowed", "snapshot", "reason", "tradable"),
    [
        ("BTC", {"BTC"}, _snapshot(), "tradable", True),
        ("BTC", set(), _snapshot(), "global_not_allowed", False),
        (
            "ABC",
            {"ABC"},
            _snapshot(),
            "venue_instrument_unsupported",
            False,
        ),
        (
            "SOL",
            {"SOL"},
            _snapshot(instrument_id="SOL-USDT-SWAP", state="suspend"),
            "venue_instrument_not_live",
            False,
        ),
        (
            "SOL",
            {"SOL"},
            _snapshot(instrument_id="SOL-USDT-SWAP", age=timedelta(days=2)),
            "contract_spec_stale",
            False,
        ),
    ],
)
def test_decide_intersects_global_allowlist_with_fresh_live_deepcoin_capability(
    symbol, global_allowed, snapshot, reason, tradable
):
    decision = decide_deepcoin_symbol_capability(
        symbol,
        global_allowed=global_allowed,
        snapshot=snapshot,
        now=NOW,
    )

    assert decision.reason == reason
    assert decision.tradable is tradable


def test_decide_normalizes_symbol_and_allowlist_case_without_fuzzy_instrument_matching():
    snapshot = _snapshot()

    normalized = decide_deepcoin_symbol_capability(
        " btc ", global_allowed={" btc "}, snapshot=snapshot, now=NOW
    )
    instrument_shaped_symbol = decide_deepcoin_symbol_capability(
        "BTC-USDT-SWAP",
        global_allowed={"BTC-USDT-SWAP"},
        snapshot=snapshot,
        now=NOW,
    )

    assert normalized.reason == "tradable"
    assert normalized.symbol == "BTC"
    assert normalized.instrument_id == "BTC-USDT-SWAP"
    assert instrument_shaped_symbol.reason == "venue_instrument_unsupported"
    assert instrument_shaped_symbol.instrument_id == "BTC-USDT-SWAP-USDT-SWAP"


def test_decide_reports_sync_unavailable_without_a_valid_snapshot():
    decision = decide_deepcoin_symbol_capability(
        "BTC", global_allowed={"BTC"}, snapshot=None, now=NOW
    )

    assert decision.reason == "contract_spec_sync_unavailable"
    assert decision.tradable is False
    assert decision.contract_spec is None
