"""Immutable public candle evidence for KOL PnL audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

import httpx


_INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_SYMBOLS = frozenset({"BTC", "ETH"})


class AuditMarketDataError(RuntimeError):
    """Raised when public candle evidence is incomplete or untrustworthy."""


@dataclass(frozen=True, slots=True)
class AuditCandle:
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class CandleEvidenceManifest:
    symbol: str
    interval: str
    start_at: datetime
    end_at: datetime
    row_count: int
    sha256: str


class BinanceAuditMarketData:
    """Read-only Binance spot candle loader with immutable local evidence."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.binance.com",
        page_limit: int = 1000,
        timeout_seconds: float = 20,
    ) -> None:
        if page_limit <= 0 or page_limit > 1000:
            raise AuditMarketDataError("page limit must be between 1 and 1000")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
        )
        self._base_url = base_url.rstrip("/")
        self._page_limit = page_limit

    def capture_candles(
        self,
        *,
        symbol: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
        cache_path: str | Path,
    ) -> tuple[tuple[AuditCandle, ...], CandleEvidenceManifest]:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_interval = str(interval or "").strip().lower()
        if normalized_symbol not in _SYMBOLS:
            raise AuditMarketDataError("audit symbol must be BTC or ETH")
        if normalized_interval not in _INTERVAL_MILLISECONDS:
            raise AuditMarketDataError("unsupported audit candle interval")
        start = _utc_timestamp(start_at)
        end = _utc_timestamp(end_at)
        if end <= start:
            raise AuditMarketDataError("candle end must be after start")

        interval_ms = _INTERVAL_MILLISECONDS[normalized_interval]
        cursor = _epoch_milliseconds(start)
        end_ms = _epoch_milliseconds(end)
        by_opened_ms: dict[int, AuditCandle] = {}
        while cursor <= end_ms:
            response = self._client.get(
                f"{self._base_url}/api/v3/klines",
                params={
                    "symbol": f"{normalized_symbol}USDT",
                    "interval": normalized_interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": self._page_limit,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise AuditMarketDataError("Binance candle response must be an array")
            if not payload:
                break
            last_opened_ms: int | None = None
            for raw in payload:
                candle = _candle_from_binance(raw)
                opened_ms = _epoch_milliseconds(candle.opened_at)
                if opened_ms < _epoch_milliseconds(start) or opened_ms > end_ms:
                    continue
                by_opened_ms[opened_ms] = candle
                last_opened_ms = max(last_opened_ms or opened_ms, opened_ms)
            if last_opened_ms is None:
                raise AuditMarketDataError("candle page did not advance the cursor")
            next_cursor = last_opened_ms + interval_ms
            if next_cursor <= cursor:
                raise AuditMarketDataError("candle pagination did not advance")
            cursor = next_cursor
            if len(payload) < self._page_limit:
                break

        candles = tuple(by_opened_ms[key] for key in sorted(by_opened_ms))
        if not candles:
            raise AuditMarketDataError("no candle evidence returned")
        _validate_contiguous(candles, interval_ms=interval_ms)
        candle_payload = [_candle_dict(item) for item in candles]
        digest = _candle_digest(candle_payload)
        manifest = CandleEvidenceManifest(
            symbol=normalized_symbol,
            interval=normalized_interval,
            start_at=start,
            end_at=end,
            row_count=len(candles),
            sha256=digest,
        )
        _write_cache(Path(cache_path), manifest=manifest, candles=candle_payload)
        return candles, manifest

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BinanceAuditMarketData":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def load_cached_candles(
    cache_path: str | Path,
) -> tuple[tuple[AuditCandle, ...], CandleEvidenceManifest]:
    """Load cached candles only when their canonical digest still matches."""

    path = Path(cache_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_manifest = payload["manifest"]
        raw_candles = payload["candles"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuditMarketDataError("invalid candle cache") from exc
    if not isinstance(raw_manifest, Mapping) or not isinstance(raw_candles, list):
        raise AuditMarketDataError("invalid candle cache shape")
    expected_digest = str(raw_manifest.get("sha256") or "")
    actual_digest = _candle_digest(raw_candles)
    if not expected_digest or actual_digest != expected_digest:
        raise AuditMarketDataError("candle cache digest mismatch")

    candles = tuple(_candle_from_dict(item) for item in raw_candles)
    interval = str(raw_manifest.get("interval") or "")
    if interval not in _INTERVAL_MILLISECONDS:
        raise AuditMarketDataError("unsupported cached candle interval")
    if len(candles) != int(raw_manifest.get("row_count") or -1):
        raise AuditMarketDataError("candle cache row count mismatch")
    _validate_contiguous(candles, interval_ms=_INTERVAL_MILLISECONDS[interval])
    manifest = CandleEvidenceManifest(
        symbol=str(raw_manifest.get("symbol") or ""),
        interval=interval,
        start_at=_parse_timestamp(raw_manifest.get("start_at")),
        end_at=_parse_timestamp(raw_manifest.get("end_at")),
        row_count=len(candles),
        sha256=actual_digest,
    )
    return candles, manifest


def _candle_from_binance(raw: Any) -> AuditCandle:
    if not isinstance(raw, (list, tuple)) or len(raw) < 7:
        raise AuditMarketDataError("malformed Binance candle row")
    opened_ms = int(raw[0])
    closed_ms = int(raw[6])
    return AuditCandle(
        opened_at=_from_epoch_milliseconds(opened_ms),
        closed_at=_from_epoch_milliseconds(closed_ms),
        open=_decimal(raw[1], "open"),
        high=_decimal(raw[2], "high"),
        low=_decimal(raw[3], "low"),
        close=_decimal(raw[4], "close"),
    )


def _candle_from_dict(raw: Any) -> AuditCandle:
    if not isinstance(raw, Mapping):
        raise AuditMarketDataError("malformed cached candle row")
    return AuditCandle(
        opened_at=_parse_timestamp(raw.get("opened_at")),
        closed_at=_parse_timestamp(raw.get("closed_at")),
        open=_decimal(raw.get("open"), "open"),
        high=_decimal(raw.get("high"), "high"),
        low=_decimal(raw.get("low"), "low"),
        close=_decimal(raw.get("close"), "close"),
    )


def _candle_dict(candle: AuditCandle) -> dict[str, str]:
    return {
        "opened_at": _timestamp_text(candle.opened_at),
        "closed_at": _timestamp_text(candle.closed_at),
        "open": _decimal_text(candle.open),
        "high": _decimal_text(candle.high),
        "low": _decimal_text(candle.low),
        "close": _decimal_text(candle.close),
    }


def _write_cache(
    path: Path,
    *,
    manifest: CandleEvidenceManifest,
    candles: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest": {
            "symbol": manifest.symbol,
            "interval": manifest.interval,
            "start_at": _timestamp_text(manifest.start_at),
            "end_at": _timestamp_text(manifest.end_at),
            "row_count": manifest.row_count,
            "sha256": manifest.sha256,
        },
        "candles": candles,
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.replace(path)


def _validate_contiguous(
    candles: tuple[AuditCandle, ...], *, interval_ms: int
) -> None:
    previous: int | None = None
    for candle in candles:
        opened_ms = _epoch_milliseconds(candle.opened_at)
        if previous is not None and opened_ms - previous != interval_ms:
            raise AuditMarketDataError("candle evidence contains a gap")
        if candle.low > candle.high:
            raise AuditMarketDataError("candle low exceeds high")
        if not candle.low <= candle.open <= candle.high:
            raise AuditMarketDataError("candle open is outside its range")
        if not candle.low <= candle.close <= candle.high:
            raise AuditMarketDataError("candle close is outside its range")
        previous = opened_ms


def _candle_digest(candles: Any) -> str:
    canonical = json.dumps(
        candles,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AuditMarketDataError("candle timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditMarketDataError("invalid candle timestamp") from exc
    return _utc_timestamp(parsed)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _epoch_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_epoch_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AuditMarketDataError(f"invalid candle {label}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AuditMarketDataError(f"invalid candle {label}")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
