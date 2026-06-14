"""Public Binance market-data provider for recovery dry-runs."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from telegram_kol_research.recovery_scan import PriceCandle


class BinanceMarketDataProvider:
    """Read-only Binance spot market data adapter for BTC/ETH USDT recovery checks."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.binance.com",
        interval: str = "1m",
        timeout_seconds: float = 10,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_seconds)
        self._interval = interval

    def load_candles(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[PriceCandle]:
        response = self._client.get(
            "/api/v3/klines",
            params={
                "symbol": _to_usdt_symbol(symbol),
                "interval": self._interval,
                "startTime": _to_epoch_millis(start_at),
                "endTime": _to_epoch_millis(end_at),
                "limit": 1000,
            },
        )
        response.raise_for_status()
        return [
            PriceCandle(
                opened_at=_from_epoch_millis(row[0]),
                high=float(row[2]),
                low=float(row[3]),
            )
            for row in response.json()
        ]

    def get_current_price(self, *, symbol: str) -> float | None:
        response = self._client.get(
            "/api/v3/ticker/price",
            params={"symbol": _to_usdt_symbol(symbol)},
        )
        response.raise_for_status()
        payload = response.json()
        price = payload.get("price")
        return float(price) if price not in (None, "") else None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _to_usdt_symbol(symbol: str) -> str:
    normalized = symbol.upper().replace("-", "").replace("_", "")
    if normalized.endswith("USDT"):
        return normalized
    return f"{normalized}USDT"


def _to_epoch_millis(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _from_epoch_millis(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).replace(tzinfo=None)
