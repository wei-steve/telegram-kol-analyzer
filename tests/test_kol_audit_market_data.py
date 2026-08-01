from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from telegram_kol_research.kol_audit_market_data import (
    AuditMarketDataError,
    BinanceAuditMarketData,
    load_cached_candles,
)


START = datetime(2026, 7, 1, tzinfo=UTC)


def _kline(opened_ms, *, close="101"):
    return [
        opened_ms,
        "100",
        "103",
        "99",
        close,
        "10",
        opened_ms + 899_999,
        "1000",
        20,
        "4",
        "400",
        "0",
    ]


def test_provider_paginates_deduplicates_and_replays_verified_cache(tmp_path):
    start_ms = int(START.timestamp() * 1000)
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        cursor = int(request.url.params["startTime"])
        if cursor == start_ms:
            payload = [_kline(start_ms), _kline(start_ms + 900_000)]
        else:
            payload = [
                _kline(start_ms + 900_000),
                _kline(start_ms + 1_800_000, close="102"),
            ]
        return httpx.Response(200, json=payload)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.binance.test"
    )
    cache_path = tmp_path / "btc-15m.json"
    provider = BinanceAuditMarketData(client=client, page_limit=2)

    candles, manifest = provider.capture_candles(
        symbol="BTC",
        interval="15m",
        start_at=START,
        end_at=START + timedelta(minutes=44, seconds=59),
        cache_path=cache_path,
    )

    assert [item.opened_at for item in candles] == [
        START,
        START + timedelta(minutes=15),
        START + timedelta(minutes=30),
    ]
    assert candles[-1].close == 102
    assert len(calls) == 2
    assert manifest.row_count == 3
    assert manifest.sha256
    replayed, replay_manifest = load_cached_candles(cache_path)
    assert replayed == candles
    assert replay_manifest == manifest


def test_cache_digest_detects_modified_evidence(tmp_path):
    path = tmp_path / "candles.json"
    payload = {
        "manifest": {
            "symbol": "BTC",
            "interval": "15m",
            "start_at": "2026-07-01T00:00:00Z",
            "end_at": "2026-07-01T00:14:59.999000Z",
            "row_count": 1,
            "sha256": "wrong",
        },
        "candles": [{
            "opened_at": "2026-07-01T00:00:00Z",
            "closed_at": "2026-07-01T00:14:59.999000Z",
            "open": "100",
            "high": "103",
            "low": "99",
            "close": "101",
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AuditMarketDataError, match="digest"):
        load_cached_candles(path)


@pytest.mark.parametrize(
    ("symbol", "interval", "error"),
    [
        ("SOL", "15m", "symbol"),
        ("BTC", "3m", "interval"),
    ],
)
def test_provider_rejects_unapproved_symbols_and_intervals(
    tmp_path, symbol, interval, error
):
    provider = BinanceAuditMarketData(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None))
    )

    with pytest.raises(AuditMarketDataError, match=error):
        provider.capture_candles(
            symbol=symbol,
            interval=interval,
            start_at=START,
            end_at=START + timedelta(minutes=15),
            cache_path=tmp_path / "unused.json",
        )


def test_provider_fails_closed_when_candle_interval_has_gap(tmp_path):
    start_ms = int(START.timestamp() * 1000)

    def handler(request):
        return httpx.Response(
            200,
            json=[_kline(start_ms), _kline(start_ms + 1_800_000)],
        )

    provider = BinanceAuditMarketData(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AuditMarketDataError, match="gap"):
        provider.capture_candles(
            symbol="ETH",
            interval="15m",
            start_at=START,
            end_at=START + timedelta(minutes=44),
            cache_path=tmp_path / "eth-15m.json",
        )
