"""Deepcoin private WebSocket inbox: connect, subscribe, persist raw frames.

Phase 1 of the REST+WebSocket program. This module does exactly three things:

1. acquire a listen key and open ``wss://stream.deepcoin.com/v1/private``,
2. subscribe to ``Order`` / ``Trade`` / ``Position`` / ``TriggerOrder``,
3. write every received frame verbatim into ``deepcoin_ws_events``.

What it deliberately does **not** do, and must not start doing here:

* no exchange write of any kind,
* no ledger write, no protection decision, no position attribution,
* no de-duplication, filtering, merging or normalisation of frames,
* no interpretation of a disconnect as "no orders" or "no positions" -- a gap
  is recorded as unknown in ``deepcoin_ws_connection_gaps`` and nothing else,
* no mode switch (``inline`` / ``shadow``) of any kind.

The listen key is a credential. It is never logged, never persisted, never
placed in exception text, and the stream URL built from it is treated the same
way because it embeds the key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from telegram_kol_research.models import DeepcoinWsConnectionGap, DeepcoinWsEvent

logger = logging.getLogger(__name__)


DEEPCOIN_PRIVATE_WS_URL = "wss://stream.deepcoin.com/v1/private"
DEEPCOIN_WS_TABLES = ("Order", "Trade", "Position", "TriggerOrder")
DEEPCOIN_WS_RECONNECT_INTERVAL_SECONDS = 5.0
DEEPCOIN_WS_OPEN_TIMEOUT_SECONDS = 15.0
DEEPCOIN_WS_CLOSE_TIMEOUT_SECONDS = 5.0
DEEPCOIN_WS_PING_INTERVAL_SECONDS = 10.0
DEEPCOIN_WS_PING_TIMEOUT_SECONDS = 10.0
DEEPCOIN_WS_MAX_FRAME_BYTES = 2_000_000

UNPARSED_CHANNEL = "unparsed"
UNKNOWN_ACTION = "unknown"

# Documented short keys. Phase 1 reads these and nothing else: guessing at long
# key spellings is exactly the inference this program exists to remove.
_ORDER_SYS_ID_KEY = "OS"
_TRADE_UNIT_ID_KEY = "TU"
_POSITION_ID_KEY = "PI"
_INSTRUMENT_KEY = "I"
# UpdateMillTime is the only key documented in milliseconds; UpdateTime and
# InsertTime are stored as received with their source recorded, never rescaled.
_EXCHANGE_TIME_KEYS = ("UM", "U", "IT")


def build_subscribe_frame() -> str:
    """Return the exact subscribe frame sent right after the socket opens."""

    return json.dumps(
        {"action": "subscribe", "tables": list(DEEPCOIN_WS_TABLES)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _payload_hash(raw_payload: str) -> str:
    return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()


def _short_text(data: dict[str, Any], key: str, *, limit: int) -> str | None:
    if key not in data:
        return None
    value = data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _exchange_time_ms(data: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return the first integer-like exchange timestamp and which key it came from.

    No unit conversion happens here. ``UM`` is milliseconds by documentation;
    ``U`` and ``IT`` may not be, so the source key travels with the value and
    the raw frame stays authoritative for phase 2.
    """

    for key in _EXCHANGE_TIME_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value, key
        text = str(value or "").strip()
        if text.lstrip("-").isdigit():
            return int(text), key
    return None, None


def decode_ws_frame(
    raw_payload: str,
    *,
    received_at: datetime,
    received_ms: int,
) -> list[dict[str, Any]]:
    """Project one raw frame into the rows to persist.

    Always returns at least one row. A frame that cannot be parsed, or whose
    ``result`` / ``data`` has an unexpected shape, still produces one row with
    ``channel='unparsed'`` carrying the original text. Frames are never dropped.
    """

    payload_hash = _payload_hash(raw_payload)
    base: dict[str, Any] = {
        "venue": "deepcoin",
        "received_at": received_at,
        "received_ms": received_ms,
        "raw_payload": raw_payload,
        "payload_hash": payload_hash,
        "processed_state": "unprocessed",
    }

    def _unparsed() -> list[dict[str, Any]]:
        return [
            {
                **base,
                "channel": UNPARSED_CHANNEL,
                "action": UNKNOWN_ACTION,
                "order_sys_id": None,
                "trade_unit_id": None,
                "position_id": None,
                "instrument_raw": None,
                "exchange_time_ms": None,
                "exchange_time_source": None,
            }
        ]

    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return _unparsed()
    if not isinstance(payload, dict):
        return _unparsed()

    action = _short_text(payload, "action", limit=64) or UNKNOWN_ACTION
    result = payload.get("result")
    # A single-object ``result`` is a shape the recorded experiment actually
    # observed; treating it as a one-item list is a shape fix, not a filter.
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list) or not result:
        return _unparsed()

    rows: list[dict[str, Any]] = []
    for item in result:
        if not isinstance(item, dict):
            rows.extend(_unparsed())
            continue
        data = item.get("data")
        channel = _short_text(item, "table", limit=32)
        if not isinstance(data, dict) or channel is None:
            rows.extend(_unparsed())
            continue
        exchange_time_ms, exchange_time_source = _exchange_time_ms(data)
        rows.append(
            {
                **base,
                "channel": channel,
                "action": action,
                "order_sys_id": _short_text(data, _ORDER_SYS_ID_KEY, limit=255),
                "trade_unit_id": _short_text(data, _TRADE_UNIT_ID_KEY, limit=255),
                "position_id": _short_text(data, _POSITION_ID_KEY, limit=255),
                "instrument_raw": _short_text(data, _INSTRUMENT_KEY, limit=64),
                "exchange_time_ms": exchange_time_ms,
                "exchange_time_source": exchange_time_source,
            }
        )
    return rows or _unparsed()


def persist_ws_frame(
    session_factory: Callable[[], Any],
    raw_payload: str,
    *,
    received_at: datetime,
    received_ms: int,
) -> list[int]:
    """Persist one frame and return the inbox row ids written, in order."""

    rows = decode_ws_frame(
        raw_payload,
        received_at=received_at,
        received_ms=received_ms,
    )
    models = [DeepcoinWsEvent(**row) for row in rows]
    with session_factory() as session:
        session.add_all(models)
        session.commit()
        return [int(model.id) for model in models]


def open_connection_gap(
    session_factory: Callable[[], Any],
    *,
    reason: str,
    detail: str | None,
    disconnected_at: datetime,
    disconnected_ms: int,
    last_event_id: int | None,
    last_event_received_ms: int | None,
    events_persisted_before_gap: int,
) -> int:
    """Record the start of an interval during which the stream is unknown."""

    gap = DeepcoinWsConnectionGap(
        venue="deepcoin",
        reason=reason[:64],
        detail=None if detail is None else str(detail)[:255],
        disconnected_at=disconnected_at,
        disconnected_ms=disconnected_ms,
        last_event_id=last_event_id,
        last_event_received_ms=last_event_received_ms,
        events_persisted_before_gap=events_persisted_before_gap,
    )
    with session_factory() as session:
        session.add(gap)
        session.commit()
        return int(gap.id)


def close_connection_gap(
    session_factory: Callable[[], Any],
    gap_id: int,
    *,
    reconnected_at: datetime,
    reconnected_ms: int,
) -> None:
    """Close one gap row once delivery has demonstrably resumed."""

    with session_factory() as session:
        gap = session.get(DeepcoinWsConnectionGap, gap_id)
        if gap is None:
            return
        gap.reconnected_at = reconnected_at
        gap.reconnected_ms = reconnected_ms
        session.commit()


def latest_event_watermark(
    session_factory: Callable[[], Any],
) -> tuple[int | None, int | None]:
    """Return the highest persisted inbox row id and its receive millisecond."""

    from sqlalchemy import select

    with session_factory() as session:
        row = session.execute(
            select(DeepcoinWsEvent.id, DeepcoinWsEvent.received_ms)
            .order_by(DeepcoinWsEvent.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None, None
    return int(row[0]), int(row[1])


def build_deepcoin_ws_health(
    *,
    session_factory: Callable[[], Any],
    inbox: Any | None,
    now: datetime,
) -> dict[str, Any]:
    """Return counts and times only. Never returns any payload content.

    An open gap row means the stream is unknown for that interval. It never
    means there were no orders or no positions.
    """

    from sqlalchemy import func, select

    hour_ago_ms = int(now.timestamp() * 1000) - 3_600_000
    counts_by_channel: dict[str, int] = {}
    last_event_at: str | None = None
    with session_factory() as session:
        for channel, count in session.execute(
            select(DeepcoinWsEvent.channel, func.count(DeepcoinWsEvent.id)).group_by(
                DeepcoinWsEvent.channel
            )
        ).all():
            counts_by_channel[str(channel)] = int(count)
        latest = session.execute(
            select(DeepcoinWsEvent.received_at)
            .order_by(DeepcoinWsEvent.id.desc())
            .limit(1)
        ).first()
        if latest is not None and latest[0] is not None:
            last_event_at = latest[0].isoformat()
        events_last_hour = int(
            session.execute(
                select(func.count(DeepcoinWsEvent.id)).where(
                    DeepcoinWsEvent.received_ms >= hour_ago_ms
                )
            ).scalar()
            or 0
        )
        open_gaps = int(
            session.execute(
                select(func.count(DeepcoinWsConnectionGap.id)).where(
                    DeepcoinWsConnectionGap.reconnected_at.is_(None)
                )
            ).scalar()
            or 0
        )
        total_gaps = int(
            session.execute(select(func.count(DeepcoinWsConnectionGap.id))).scalar()
            or 0
        )
    for table in DEEPCOIN_WS_TABLES:
        counts_by_channel.setdefault(table, 0)
    return {
        "connected": bool(getattr(inbox, "connected", False)),
        "last_event_at": last_event_at,
        "events_last_hour": events_last_hour,
        "counts_by_channel": counts_by_channel,
        "unparsed_count": counts_by_channel.get(UNPARSED_CHANNEL, 0),
        "open_gap_count": open_gaps,
        "gap_count": total_gaps,
        "now": now.isoformat(),
    }


class DeepcoinPrivateWsInbox:
    """Long-lived private-stream reader owned by the ``worker`` role only."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        deepcoin_client_factory: Callable[[], Any],
        connect_factory: Callable[..., Any] | None = None,
        reconnect_interval_seconds: float = DEEPCOIN_WS_RECONNECT_INTERVAL_SECONDS,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_ms_provider: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._deepcoin_client_factory = deepcoin_client_factory
        self._connect_factory = connect_factory
        self._reconnect_interval_seconds = reconnect_interval_seconds
        self._now = now_provider
        self._now_ms = monotonic_ms_provider
        self._sleep = sleep
        self.connected = False
        self.events_persisted = 0
        self.last_event_id: int | None = None
        self.last_event_received_ms: int | None = None
        self.open_gap_id: int | None = None

    def snapshot(self) -> dict[str, Any]:
        """Process-local liveness view. Carries no payload content."""

        return {
            "connected": self.connected,
            "events_persisted": self.events_persisted,
            "last_event_id": self.last_event_id,
            "last_event_received_ms": self.last_event_received_ms,
            "open_gap_id": self.open_gap_id,
        }

    def _resolve_connect(self) -> Callable[..., Any]:
        if self._connect_factory is not None:
            return self._connect_factory
        from websockets.asyncio.client import connect

        return connect

    def _acquire_listen_key(self) -> str:
        client = self._deepcoin_client_factory()
        try:
            return client.acquire_listen_key()
        finally:
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    logger.warning("Deepcoin private WS client cleanup failed")

    def _record_gap_start(self, *, reason: str, detail: str | None) -> None:
        if self.open_gap_id is not None:
            return
        try:
            self.open_gap_id = open_connection_gap(
                self._session_factory,
                reason=reason,
                detail=detail,
                disconnected_at=self._now(),
                disconnected_ms=self._now_ms(),
                last_event_id=self.last_event_id,
                last_event_received_ms=self.last_event_received_ms,
                events_persisted_before_gap=self.events_persisted,
            )
        except Exception:
            logger.exception("Failed to record Deepcoin private WS gap start")

    def _record_gap_end(self) -> None:
        if self.open_gap_id is None:
            return
        try:
            close_connection_gap(
                self._session_factory,
                self.open_gap_id,
                reconnected_at=self._now(),
                reconnected_ms=self._now_ms(),
            )
        except Exception:
            logger.exception("Failed to close Deepcoin private WS gap")
        finally:
            self.open_gap_id = None

    async def run_forever(self) -> None:
        """Connect, subscribe, persist. Reconnect at a fixed interval forever.

        Exponential backoff and listen-key renewal are phase 2. A cancelled task
        closes the current gap record as still open: the interval genuinely was
        not covered, and phase 2's resync reads it.
        """

        # A process restart loses frames exactly like a network drop does, so
        # startup opens a gap that only closes once the stream is live again.
        try:
            self.last_event_id, self.last_event_received_ms = latest_event_watermark(
                self._session_factory
            )
        except Exception:
            logger.exception("Failed to read Deepcoin private WS watermark")
        self._record_gap_start(reason="process_start", detail=None)

        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:
                self.connected = False
                # Only the exception type: its text can embed the stream URL,
                # which carries the listen key.
                self._record_gap_start(
                    reason="connection_error",
                    detail=type(exc).__name__,
                )
                logger.warning(
                    "Deepcoin private WS connection error (%s); retrying",
                    type(exc).__name__,
                )
            else:
                self.connected = False
                self._record_gap_start(reason="connection_closed", detail=None)
            await self._sleep(self._reconnect_interval_seconds)

    async def _run_once(self) -> None:
        connect = self._resolve_connect()
        listen_key = await asyncio.to_thread(self._acquire_listen_key)
        # Never log, persist or re-raise this URL: it embeds the listen key.
        url = f"{DEEPCOIN_PRIVATE_WS_URL}?listenKey={listen_key}"
        del listen_key
        try:
            connection = connect(
                url,
                open_timeout=DEEPCOIN_WS_OPEN_TIMEOUT_SECONDS,
                close_timeout=DEEPCOIN_WS_CLOSE_TIMEOUT_SECONDS,
                ping_interval=DEEPCOIN_WS_PING_INTERVAL_SECONDS,
                ping_timeout=DEEPCOIN_WS_PING_TIMEOUT_SECONDS,
                max_size=DEEPCOIN_WS_MAX_FRAME_BYTES,
            )
        finally:
            del url
        async with connection as websocket:
            await websocket.send(build_subscribe_frame())
            self.connected = True
            self._record_gap_end()
            logger.info("Deepcoin private WS subscribed to %s", list(DEEPCOIN_WS_TABLES))
            async for raw in websocket:
                received_at = self._now()
                received_ms = self._now_ms()
                raw_payload = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                )
                await asyncio.to_thread(
                    self._persist_frame,
                    raw_payload,
                    received_at,
                    received_ms,
                )

    def _persist_frame(
        self,
        raw_payload: str,
        received_at: datetime,
        received_ms: int,
    ) -> None:
        try:
            row_ids = persist_ws_frame(
                self._session_factory,
                raw_payload,
                received_at=received_at,
                received_ms=received_ms,
            )
        except Exception:
            logger.exception("Failed to persist Deepcoin private WS frame")
            return
        if row_ids:
            self.events_persisted += len(row_ids)
            self.last_event_id = row_ids[-1]
            self.last_event_received_ms = received_ms


async def run_deepcoin_private_ws_loop(
    *,
    session_factory: Callable[[], Any],
    deepcoin_client_factory: Callable[[], Any],
    inbox_sink: Callable[[DeepcoinPrivateWsInbox], None] | None = None,
    connect_factory: Callable[..., Any] | None = None,
    reconnect_interval_seconds: float = DEEPCOIN_WS_RECONNECT_INTERVAL_SECONDS,
    now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Entry point registered as the ``deepcoin_private_ws`` worker singleton."""

    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=deepcoin_client_factory,
        connect_factory=connect_factory,
        reconnect_interval_seconds=reconnect_interval_seconds,
        now_provider=now_provider,
    )
    if inbox_sink is not None:
        inbox_sink(inbox)
    await inbox.run_forever()
