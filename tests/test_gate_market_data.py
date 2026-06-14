from datetime import datetime

import httpx

from telegram_kol_research.gate_market_data import GateMarketDataProvider


def test_gate_market_data_loads_futures_candles_with_second_bounds():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v4/futures/usdt/candlesticks"
        params = dict(request.url.params)
        assert params["contract"] == "BTC_USDT"
        assert params["interval"] == "1m"
        assert params["from"] == "1781244000"
        assert params["to"] == "1781247600"
        return httpx.Response(
            200,
            json=[
                {
                    "t": 1781244000,
                    "h": "68200.5",
                    "l": "67900.5",
                    "c": "68100.0",
                }
            ],
        )

    provider = GateMarketDataProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.gateio.test",
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


def test_gate_market_data_get_current_price_uses_futures_ticker_last():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/futures/usdt/tickers"
        assert dict(request.url.params)["contract"] == "ETH_USDT"
        return httpx.Response(
            200,
            json=[
                {
                    "contract": "ETH_USDT",
                    "last": "2500.25",
                }
            ],
        )

    provider = GateMarketDataProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.gateio.test",
        )
    )

    assert provider.get_current_price(symbol="ETH") == 2500.25
