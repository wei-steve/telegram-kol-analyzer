"""Public Gate futures market-data provider for recovery dry-runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from telegram_kol_research.recovery_scan import PriceCandle


class GateMarketDataProvider:
    """Read-only Gate USDT futures market data adapter for BTC/ETH recovery checks."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.gateio.ws",
        settle: str = "usdt",
        interval: str = "1m",
        timeout_seconds: float = 10,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_seconds)
        self._settle = settle.lower()
        self._interval = interval

    def load_candles(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[PriceCandle]:
        response = self._client.get(
            f"/api/v4/futures/{self._settle}/candlesticks",
            params={
                "contract": _to_gate_contract(symbol),
                "interval": self._interval,
                "from": _to_epoch_seconds(start_at),
                "to": _to_epoch_seconds(end_at),
            },
        )
        response.raise_for_status()
        return [_candle_from_payload(row) for row in response.json()]

    def get_current_price(self, *, symbol: str) -> float | None:
        response = self._client.get(
            f"/api/v4/futures/{self._settle}/tickers",
            params={"contract": _to_gate_contract(symbol)},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            return None
        first = payload[0] if isinstance(payload, list) else payload
        price = first.get("last")
        return float(price) if price not in (None, "") else None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _to_gate_contract(symbol: str) -> str:
    normalized = symbol.upper().replace("-", "_")
    if normalized.endswith("_USDT"):
        return normalized
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}_USDT"
    return f"{normalized}_USDT"


def _to_epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _from_epoch_seconds(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)


def _candle_from_payload(row: dict[str, Any] | list[Any]) -> PriceCandle:
    if isinstance(row, dict):
        opened_at = row.get("t") or row.get("time")
        high = row.get("h") or row.get("high")
        low = row.get("l") or row.get("low")
    else:
        opened_at = row[0]
        high = row[3]
        low = row[4]
    return PriceCandle(
        opened_at=_from_epoch_seconds(opened_at),
        high=float(high),
        low=float(low),
    )
