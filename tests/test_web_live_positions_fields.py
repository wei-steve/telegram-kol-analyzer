"""Position cards show last price / unrealized PnL / liquidation price.

The values come only from the raw position rows the worker already cached in
the live position snapshot; the web process never asks the exchange.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app

NOW = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)


class _ExplodingExchange:
    def __getattr__(self, name):  # pragma: no cover - only hit on a regression
        raise AssertionError(f"web process must not call the exchange: {name}")


def _snapshot(**overrides):
    row = {
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-live-1",
        "posSide": "long",
        "pos": "3",
        "avgPx": "59000",
        "lastPx": "60123.4",
        "unrealizedProfit": "-12.5",
        "liqPx": "50100.7",
    }
    row.update(overrides)
    return {
        "_live_source": {
            "positions": [row],
            "tpsl_orders": [],
            "tpsl_evidence_available": True,
        }
    }


def _render(tmp_path, payload):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        now_provider=lambda: NOW,
        position_snapshot_now_provider=lambda: NOW,
        deepcoin_client_factory=lambda: _ExplodingExchange(),
    )
    app.state.live_position_snapshot_store.finish_success(payload, captured_at=NOW)
    response = TestClient(app).get("/positions-panel?initial=positions")
    assert response.status_code == 200
    return response.text


def test_position_card_shows_price_pnl_and_liquidation_from_cached_snapshot(tmp_path):
    body = _render(tmp_path, _snapshot())

    assert 'data-position-pos-id="pos-live-1"' in body
    assert "现价" in body and "60123.4" in body
    assert "浮动盈亏" in body and "-12.5" in body
    assert "强平价" in body and "50100.7" in body
    assert "exchange-pnl-negative" in body
    assert "exchange-pnl-positive" not in body


def test_position_card_marks_positive_pnl_green(tmp_path):
    body = _render(tmp_path, _snapshot(unrealizedProfit="8.25"))

    assert "exchange-pnl-positive" in body
    assert "exchange-pnl-negative" not in body


def test_position_card_omits_fields_the_cache_does_not_carry(tmp_path):
    payload = _snapshot()
    row = payload["_live_source"]["positions"][0]
    for key in ("lastPx", "unrealizedProfit", "liqPx"):
        del row[key]

    body = _render(tmp_path, payload)

    assert 'data-position-pos-id="pos-live-1"' in body
    assert "现价" not in body
    assert "浮动盈亏" not in body
    assert "强平价" not in body


def test_position_card_hides_zero_liquidation_and_garbage_values(tmp_path):
    body = _render(
        tmp_path,
        _snapshot(liqPx="0", lastPx="", unrealizedProfit="not-a-number"),
    )

    assert 'data-position-pos-id="pos-live-1"' in body
    assert "强平价" not in body
    assert "现价" not in body
    assert "浮动盈亏" not in body
