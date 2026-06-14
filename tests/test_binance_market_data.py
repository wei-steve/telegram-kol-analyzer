from datetime import datetime

import httpx

from telegram_kol_research.binance_market_data import BinanceMarketDataProvider


def test_binance_market_data_loads_candles_with_utc_millisecond_bounds():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v3/klines"
        params = dict(request.url.params)
        assert params["symbol"] == "BTCUSDT"
        assert params["interval"] == "1m"
        assert params["startTime"] == "1781244000000"
        assert params["endTime"] == "1781247600000"
        assert params["limit"] == "1000"
        return httpx.Response(
            200,
            json=[
                [
                    1781244000000,
                    "68000.0",
                    "68200.5",
                    "67900.5",
                    "68100.0",
                    "12.5",
                ]
            ],
        )

    provider = BinanceMarketDataProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.binance.test",
        )
    )

    candles = provider.load_candles(
        symbol="BTC",
        start_at=datetime(2026, 6, 12, 6, 0),
        end_at=datetime(2026, 6, 12, 7, 0),
    )

    assert len(requests) == 1
    assert candles[0].opened_at == datetime(2026, 6, 12, 6, 0)
    assert candles[0].high == 68200.5
    assert candles[0].low == 67900.5


def test_binance_market_data_get_current_price_uses_symbol_price_ticker():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/ticker/price"
        assert dict(request.url.params)["symbol"] == "ETHUSDT"
        return httpx.Response(200, json={"symbol": "ETHUSDT", "price": "2500.25"})

    provider = BinanceMarketDataProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.binance.test",
        )
    )

    assert provider.get_current_price(symbol="ETH") == 2500.25
