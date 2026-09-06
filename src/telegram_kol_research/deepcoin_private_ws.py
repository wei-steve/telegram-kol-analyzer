"""Deepcoin private WebSocket inbox: connect, subscribe, persist, de-duplicate, resync.

Phase 1 of the REST+WebSocket program made this a write-only inbox. Phase 2
turns it into a *trustworthy* stream: it now knows which frames are repeats,
which arrived out of order, what state the connection is in, what it missed
while disconnected, and how to rebuild the missing part over REST.

What it still deliberately does **not** do, and must not start doing here:

* no exchange write of any kind -- the resync issues GET reads only,
* no ledger write, no protection decision, no position attribution,
* no deletion of any inbox row: a repeat is *marked* ``duplicate``, never
  removed, because the historical frames are the only evidence of how the
  exchange actually behaves,
* no interpretation of a disconnect as "no orders" or "no positions" -- a gap
  is unknown coverage and nothing else,
* no wiring of ``permits_new_entry`` into the entry path (that is phase 5),
* no mode switch (``inline`` / ``shadow``) of any kind.

The listen key is a credential. It is never logged, never persisted, never
placed in exception text, and the stream URL built from it is treated the same
way because it embeds the key.

**No sequence numbers exist on this stream.** Deepcoin publishes no continuous
sequence for the private stream and promises no replay after a disconnect; the
public market stream's ``ResumeNo`` does not apply here. Gap detection is
therefore "time watermark + REST resync", never sequence continuity. Do not
invent a sequence number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from telegram_kol_research.deepcoin_ws_resync import (
    DeepcoinWsResyncCoordinator,
)
from telegram_kol_research.deepcoin_ws_stream_state import (
    WS_STATE_CONNECTING,
    WS_STATE_DISCONNECTED,
    WS_STATE_HEALTHY,
    WS_STATE_RESYNCING,
    DeepcoinWsStreamStateMachine,
    WsEntityStateTracker,
    compute_backoff_delay,
    ws_observation_permits_new_entry,
)
from telegram_kol_research.models import DeepcoinWsConnectionGap, DeepcoinWsEvent

logger = logging.getLogger(__name__)


DEEPCOIN_PRIVATE_WS_URL = "wss://stream.deepcoin.com/v1/private"
DEEPCOIN_WS_TABLES = ("Order", "Trade", "Position", "TriggerOrder")
DEEPCOIN_WS_RECONNECT_INTERVAL_SECONDS = 5.0
DEEPCOIN_WS_OPEN_TIMEOUT_SECONDS = 15.0
DEEPCOIN_WS_CLOSE_TIMEOUT_SECONDS = 5.0
# Verified in the recorded experiment. Protocol-level keepalive only.
DEEPCOIN_WS_PING_INTERVAL_SECONDS = 10.0
DEEPCOIN_WS_PING_TIMEOUT_SECONDS = 10.0
DEEPCOIN_WS_MAX_FRAME_BYTES = 2_000_000
DEEPCOIN_WS_SUBSCRIBE_TIMEOUT_SECONDS = 15.0

# Application-level silence timer. A live pong proves the socket is open; it
# proves nothing about the business stream still being routed to us. After this
# long with no frame at all we treat the stream as unknown and rebuild it.
#
# The cost is real and deliberate: an idle account produces no frames, so this
# forces a reconnect plus a full REST resync roughly every ten minutes even when
# nothing is wrong. That is the cheap direction to be wrong in -- a redundant
# resync costs a handful of GETs, while a silently dead subscription would make
# every later phase read "no orders" when it means "no idea".
DEEPCOIN_WS_SILENCE_TIMEOUT_SECONDS = 600.0

# Deepcoin publishes no listen-key renewal endpoint, and this program does not
# invent request paths. "Renewal" is therefore a planned reconnect with a
# freshly acquired key. A failed acquisition is handled exactly like a
# disconnect: gap recorded, backoff, acquire again.
#
# Measured in production on 2026-09-06: the key is a hard sixty minutes, not a
# sliding window. A stream subscribed at 13:56:25Z received
# ``{"code":"50118","event":"error","msg":"listen key expired, connection
# closing"}`` at 14:56:31Z and the socket closed. Forty-five minutes leaves a
# fifteen-minute margin for a slow acquisition or a slow resync.
DEEPCOIN_WS_LISTEN_KEY_TTL_SECONDS = 2700.0

# The control-frame code the exchange sends just before it closes an expired
# stream. Observed in production; matched on the code rather than the message
# text so that a wording change does not silently disable the early reconnect.
DEEPCOIN_WS_LISTEN_KEY_EXPIRED_CODE = "50118"

DEEPCOIN_WS_BACKOFF_BASE_SECONDS = 1.0
DEEPCOIN_WS_BACKOFF_CAP_SECONDS = 60.0

UNPARSED_CHANNEL = "unparsed"
# Frames the exchange sends about the connection itself rather than about an
# order, trade or position. They are decoded as their own channel so that
# ``unparsed`` keeps its one meaning -- "a frame shape the decoder does not
# recognise" -- and stays usable as a decoder-defect signal.
CONTROL_CHANNEL = "control"
UNKNOWN_ACTION = "unknown"

PROCESSED_STATE_UNPROCESSED = "unprocessed"
PROCESSED_STATE_DUPLICATE = "duplicate"
PROCESSED_STATE_PROCESSED = "processed"

# Documented short keys. Phase 1 read these and nothing else: guessing at long
# key spellings is exactly the inference this program exists to remove.
_ORDER_SYS_ID_KEY = "OS"
_TRADE_UNIT_ID_KEY = "TU"
_POSITION_ID_KEY = "PI"
_INSTRUMENT_KEY = "I"
# UpdateMillTime is the only key documented in milliseconds; UpdateTime and
# InsertTime are stored as received with their source recorded, never rescaled.
_EXCHANGE_TIME_KEYS = ("UM", "U", "IT")

_HOUR_MS = 3_600_000


class DeepcoinWsSilenceTimeout(RuntimeError):
    """No frame arrived within the application-level silence window."""


class DeepcoinWsListenKeyExpiring(RuntimeError):
    """Planned reconnect so a fresh listen key can be acquired."""


class DeepcoinWsResyncNotConverged(RuntimeError):
    """The five-step REST resync did not settle; coverage stays unknown."""


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
    the raw frame stays authoritative.
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
        "processed_state": PROCESSED_STATE_UNPROCESSED,
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
    if result is None and "event" in payload:
        # e.g. {"code":"50118","event":"error","msg":"listen key expired, ..."}
        # Stored whole like every other frame; only its classification differs.
        return [
            {
                **base,
                "channel": CONTROL_CHANNEL,
                "action": (_short_text(payload, "event", limit=64) or UNKNOWN_ACTION),
                "order_sys_id": None,
                "trade_unit_id": None,
                "position_id": None,
                "instrument_raw": None,
                "exchange_time_ms": None,
                "exchange_time_source": None,
            }
        ]
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


def is_listen_key_expiry_notice(raw_payload: str) -> bool:
    """Does this frame say the exchange is about to close an expired stream?

    The exchange announces the expiry before dropping the socket. Acting on the
    announcement reconnects immediately instead of waiting for a read to fail,
    which is the difference between a gap of milliseconds and a gap of seconds.
    """

    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("code") or "").strip() == DEEPCOIN_WS_LISTEN_KEY_EXPIRED_CODE:
        return True
    if str(payload.get("event") or "").strip().lower() != "error":
        return False
    return "listen key expired" in str(payload.get("msg") or "").lower()


def _dedup_key(row: dict[str, Any]) -> tuple[str, str, int | None]:
    """The de-duplication key: ``(channel, payload_hash)`` plus exchange time.

    Exchange time is part of the key on purpose. Two frames with identical
    bytes but different exchange timestamps are two real observations; only a
    genuine re-delivery repeats all three.
    """

    exchange_time_ms = row.get("exchange_time_ms")
    return (
        str(row.get("channel") or ""),
        str(row.get("payload_hash") or ""),
        None if exchange_time_ms is None else int(exchange_time_ms),
    )


def _existing_dedup_keys(
    session: Any, candidates: set[tuple[str, str, int | None]]
) -> set[tuple[str, str, int | None]]:
    from sqlalchemy import select

    hashes = {candidate[1] for candidate in candidates if candidate[1]}
    if not hashes:
        return set()
    rows = session.execute(
        select(
            DeepcoinWsEvent.channel,
            DeepcoinWsEvent.payload_hash,
            DeepcoinWsEvent.exchange_time_ms,
        ).where(DeepcoinWsEvent.payload_hash.in_(sorted(hashes)))
    ).all()
    return {
        (
            str(channel),
            str(payload_hash),
            None if exchange_time_ms is None else int(exchange_time_ms),
        )
        for channel, payload_hash, exchange_time_ms in rows
    }


def persist_ws_frame_rows(
    session_factory: Callable[[], Any],
    raw_payload: str,
    *,
    received_at: datetime,
    received_ms: int,
) -> list[dict[str, Any]]:
    """Persist one frame and return the rows written, each with its id and state.

    Rows whose de-duplication key already exists in the inbox are written with
    ``processed_state='duplicate'``. They are still written: nothing in this
    program deletes an inbox row.

    De-duplication compares against rows persisted *earlier*. Two rows inside
    the same frame that share a key are both kept unmarked, because they are one
    delivery carrying two data rows rather than a re-delivery. Re-delivering
    that same frame later marks both, which is what makes the operation
    idempotent: processing a frame twice leaves exactly one unmarked copy.
    """

    rows = decode_ws_frame(
        raw_payload,
        received_at=received_at,
        received_ms=received_ms,
    )
    with session_factory() as session:
        existing = _existing_dedup_keys(session, {_dedup_key(row) for row in rows})
        prepared: list[dict[str, Any]] = []
        for row in rows:
            if _dedup_key(row) in existing:
                prepared.append({**row, "processed_state": PROCESSED_STATE_DUPLICATE})
            else:
                prepared.append(dict(row))
        models = [DeepcoinWsEvent(**row) for row in prepared]
        session.add_all(models)
        session.commit()
        return [
            {**row, "event_id": int(model.id)}
            for row, model in zip(prepared, models, strict=True)
        ]


def persist_ws_frame(
    session_factory: Callable[[], Any],
    raw_payload: str,
    *,
    received_at: datetime,
    received_ms: int,
) -> list[int]:
    """Persist one frame and return the inbox row ids written, in order."""

    return [
        int(row["event_id"])
        for row in persist_ws_frame_rows(
            session_factory,
            raw_payload,
            received_at=received_at,
            received_ms=received_ms,
        )
    ]


def load_unprocessed_events(
    session_factory: Callable[[], Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return inbox rows still waiting to be folded into the stream state.

    Ordered by exchange time, then receive time, then insertion order, so that a
    replay reproduces the exchange's own ordering as closely as the recorded
    data allows. Rows already marked ``duplicate`` are skipped -- their content
    is by definition already represented.
    """

    from sqlalchemy import func, select

    with session_factory() as session:
        rows = session.execute(
            select(
                DeepcoinWsEvent.id,
                DeepcoinWsEvent.channel,
                DeepcoinWsEvent.order_sys_id,
                DeepcoinWsEvent.trade_unit_id,
                DeepcoinWsEvent.position_id,
                DeepcoinWsEvent.instrument_raw,
                DeepcoinWsEvent.exchange_time_ms,
                DeepcoinWsEvent.received_ms,
                DeepcoinWsEvent.payload_hash,
            )
            .where(DeepcoinWsEvent.processed_state == PROCESSED_STATE_UNPROCESSED)
            .order_by(
                func.coalesce(DeepcoinWsEvent.exchange_time_ms, -1),
                DeepcoinWsEvent.received_ms,
                DeepcoinWsEvent.id,
            )
            .limit(limit)
        ).all()
    return [
        {
            "event_id": int(row[0]),
            "channel": row[1],
            "order_sys_id": row[2],
            "trade_unit_id": row[3],
            "position_id": row[4],
            "instrument_raw": row[5],
            "exchange_time_ms": row[6],
            "received_ms": row[7],
            "payload_hash": row[8],
        }
        for row in rows
    ]


def mark_events_processed(
    session_factory: Callable[[], Any],
    event_ids: list[int],
) -> int:
    """Advance rows to ``processed``. Never touches ``duplicate`` rows."""

    if not event_ids:
        return 0
    from sqlalchemy import update

    with session_factory() as session:
        result = session.execute(
            update(DeepcoinWsEvent)
            .where(
                DeepcoinWsEvent.id.in_(event_ids),
                DeepcoinWsEvent.processed_state == PROCESSED_STATE_UNPROCESSED,
            )
            .values(processed_state=PROCESSED_STATE_PROCESSED)
        )
        session.commit()
        return int(result.rowcount or 0)


def recent_stream_instruments(
    session_factory: Callable[[], Any],
    *,
    since_ms: int,
    limit: int = 32,
) -> list[str]:
    """Distinct stream-format contract names seen recently, newest windows first."""

    from sqlalchemy import select

    with session_factory() as session:
        rows = session.execute(
            select(DeepcoinWsEvent.instrument_raw)
            .where(
                DeepcoinWsEvent.instrument_raw.is_not(None),
                DeepcoinWsEvent.received_ms >= since_ms,
            )
            .distinct()
            .limit(limit)
        ).all()
    return [str(row[0]) for row in rows if row[0]]


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
    """Close one gap row once delivery has demonstrably resumed.

    "Demonstrably" means the resync converged, not merely that a socket opened.
    """

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
    """Return counts, states and times only. Never returns any payload content.

    An open gap row means the stream is unknown for that interval. It never
    means there were no orders or no positions, and neither does a
    ``disconnected`` or ``resyncing`` state.
    """

    from sqlalchemy import func, select

    now_ms = int(now.timestamp() * 1000)
    hour_ago_ms = now_ms - _HOUR_MS
    counts_by_channel: dict[str, int] = {}
    counts_by_processed_state: dict[str, int] = {}
    last_event_at: str | None = None
    with session_factory() as session:
        for channel, count in session.execute(
            select(DeepcoinWsEvent.channel, func.count(DeepcoinWsEvent.id)).group_by(
                DeepcoinWsEvent.channel
            )
        ).all():
            counts_by_channel[str(channel)] = int(count)
        for state, count in session.execute(
            select(
                DeepcoinWsEvent.processed_state, func.count(DeepcoinWsEvent.id)
            ).group_by(DeepcoinWsEvent.processed_state)
        ).all():
            counts_by_processed_state[str(state)] = int(count)
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
        duplicates_last_hour = int(
            session.execute(
                select(func.count(DeepcoinWsEvent.id)).where(
                    DeepcoinWsEvent.received_ms >= hour_ago_ms,
                    DeepcoinWsEvent.processed_state == PROCESSED_STATE_DUPLICATE,
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
    for state in (
        PROCESSED_STATE_UNPROCESSED,
        PROCESSED_STATE_DUPLICATE,
        PROCESSED_STATE_PROCESSED,
    ):
        counts_by_processed_state.setdefault(state, 0)

    machine = getattr(inbox, "state_machine", None)
    tracker = getattr(inbox, "entity_tracker", None)
    permits, permit_reason = ws_observation_permits_new_entry(
        machine, open_gap_count=open_gaps
    )
    duplicate_rate_1h = (
        0.0 if events_last_hour == 0 else duplicates_last_hour / events_last_hour
    )
    return {
        "connected": bool(getattr(inbox, "connected", False)),
        "state": getattr(machine, "state", None) or WS_STATE_DISCONNECTED,
        "state_since": (
            machine.state_since.isoformat() if machine is not None else None
        ),
        "last_event_at": last_event_at,
        "last_frame_at": (
            machine.last_frame_at.isoformat()
            if machine is not None and machine.last_frame_at is not None
            else None
        ),
        "reconnect_count": int(getattr(machine, "reconnect_count", 0) or 0),
        "events_last_hour": events_last_hour,
        "duplicates_last_hour": duplicates_last_hour,
        "duplicate_rate_1h": round(duplicate_rate_1h, 6),
        "out_of_order_count_1h": (
            0
            if tracker is None
            else int(tracker.out_of_order_count_since(hour_ago_ms))
        ),
        "out_of_order_count_total": int(
            getattr(tracker, "out_of_order_count", 0) or 0
        ),
        "tracked_entity_count": (0 if tracker is None else tracker.entity_count()),
        "last_resync_at": (
            machine.last_resync_at.isoformat()
            if machine is not None and machine.last_resync_at is not None
            else None
        ),
        "last_resync_outcome": getattr(machine, "last_resync_outcome", None),
        "last_resync_step_durations_ms": dict(
            getattr(machine, "last_resync_step_durations_ms", {}) or {}
        ),
        "permits_new_entry": permits,
        "permits_new_entry_reason": permit_reason,
        "counts_by_channel": counts_by_channel,
        "counts_by_processed_state": counts_by_processed_state,
        "unparsed_count": counts_by_channel.get(UNPARSED_CHANNEL, 0),
        "control_count": counts_by_channel.get(CONTROL_CHANNEL, 0),
        "open_gap_count": open_gaps,
        "gap_count": total_gaps,
        "instrument_map_size": int(getattr(inbox, "instrument_map_size", 0) or 0),
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
        silence_timeout_seconds: float = DEEPCOIN_WS_SILENCE_TIMEOUT_SECONDS,
        listen_key_ttl_seconds: float = DEEPCOIN_WS_LISTEN_KEY_TTL_SECONDS,
        backoff_base_seconds: float = DEEPCOIN_WS_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = DEEPCOIN_WS_BACKOFF_CAP_SECONDS,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_ms_provider: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        resync_coordinator: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._deepcoin_client_factory = deepcoin_client_factory
        self._connect_factory = connect_factory
        self._reconnect_interval_seconds = reconnect_interval_seconds
        self._silence_timeout_seconds = silence_timeout_seconds
        self._listen_key_ttl_seconds = listen_key_ttl_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_cap_seconds = backoff_cap_seconds
        self._now = now_provider
        self._now_ms = monotonic_ms_provider
        self._sleep = sleep
        self._rng = rng
        self.connected = False
        self.events_persisted = 0
        self.duplicates_persisted = 0
        self.last_event_id: int | None = None
        self.last_event_received_ms: int | None = None
        self.open_gap_id: int | None = None
        self.state_machine = DeepcoinWsStreamStateMachine(
            now_provider=now_provider,
            monotonic_ms_provider=monotonic_ms_provider,
        )
        self.entity_tracker = WsEntityStateTracker()
        self.resync_coordinator = resync_coordinator or DeepcoinWsResyncCoordinator(
            client_factory=deepcoin_client_factory,
            now_provider=now_provider,
            monotonic_ms_provider=monotonic_ms_provider,
        )
        self._connection_started_ms: int | None = None

    @property
    def instrument_map_size(self) -> int:
        instrument_map = getattr(self.resync_coordinator, "instrument_map", None)
        return 0 if instrument_map is None else int(instrument_map.size)

    def snapshot(self) -> dict[str, Any]:
        """Process-local liveness view. Carries no payload content."""

        return {
            "connected": self.connected,
            "state": self.state_machine.state,
            "events_persisted": self.events_persisted,
            "duplicates_persisted": self.duplicates_persisted,
            "last_event_id": self.last_event_id,
            "last_event_received_ms": self.last_event_received_ms,
            "open_gap_id": self.open_gap_id,
            "reconnect_count": self.state_machine.reconnect_count,
            "last_resync_outcome": self.state_machine.last_resync_outcome,
        }

    def permits_new_entry(self) -> tuple[bool, str]:
        """Phase 2 exposure only. Nothing calls this from the entry path yet."""

        return ws_observation_permits_new_entry(
            self.state_machine, open_gap_count=self._open_gap_count()
        )

    def _open_gap_count(self) -> int | None:
        from sqlalchemy import func, select

        try:
            with self._session_factory() as session:
                return int(
                    session.execute(
                        select(func.count(DeepcoinWsConnectionGap.id)).where(
                            DeepcoinWsConnectionGap.reconnected_at.is_(None)
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            # Unknown, never "no gaps". Fail closed.
            logger.exception("Failed to count open Deepcoin private WS gaps")
            return None

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

    def _to_disconnected(self, reason: str) -> None:
        if self.state_machine.state != WS_STATE_DISCONNECTED:
            self.state_machine.transition(WS_STATE_DISCONNECTED, reason=reason)

    async def run_forever(self) -> None:
        """Connect, resync, subscribe, persist. Reconnect with backoff forever.

        A process restart loses frames exactly like a network drop does, so
        startup opens a gap that only closes once the stream is live *and* the
        REST resync has converged. Until then coverage is unknown, which is what
        hard rule 12 requires.
        """

        try:
            self.last_event_id, self.last_event_received_ms = latest_event_watermark(
                self._session_factory
            )
        except Exception:
            logger.exception("Failed to read Deepcoin private WS watermark")
        self._record_gap_start(reason="process_start", detail=None)

        while True:
            planned = False
            try:
                await self._run_once()
            except asyncio.CancelledError:
                self.connected = False
                raise
            except DeepcoinWsListenKeyExpiring:
                planned = True
                self.connected = False
                self._record_gap_start(reason="listen_key_renewal", detail=None)
                self._to_disconnected("listen_key_renewal")
            except DeepcoinWsSilenceTimeout:
                self.connected = False
                self._record_gap_start(reason="silence_timeout", detail=None)
                self._to_disconnected("silence_timeout")
                self.state_machine.mark_connection_attempt_failed()
                logger.warning(
                    "Deepcoin private WS silent for %.0fs; reconnecting",
                    self._silence_timeout_seconds,
                )
            except DeepcoinWsResyncNotConverged as exc:
                self.connected = False
                self._record_gap_start(
                    reason="resync_not_converged", detail=str(exc)[:255]
                )
                self._to_disconnected("resync_not_converged")
                self.state_machine.mark_connection_attempt_failed()
                logger.warning("Deepcoin private WS resync did not converge: %s", exc)
            except Exception as exc:
                self.connected = False
                # Only the exception type: its text can embed the stream URL,
                # which carries the listen key.
                self._record_gap_start(
                    reason="connection_error",
                    detail=type(exc).__name__,
                )
                self._to_disconnected(f"connection_error:{type(exc).__name__}")
                self.state_machine.mark_connection_attempt_failed()
                logger.warning(
                    "Deepcoin private WS connection error (%s); retrying",
                    type(exc).__name__,
                )
            else:
                self.connected = False
                self._record_gap_start(reason="connection_closed", detail=None)
                self._to_disconnected("connection_closed")
                self.state_machine.mark_connection_attempt_failed()

            attempt = 0 if planned else max(
                0, self.state_machine.consecutive_failures - 1
            )
            await self._sleep(
                compute_backoff_delay(
                    attempt,
                    base_seconds=self._backoff_base_seconds,
                    cap_seconds=self._backoff_cap_seconds,
                    rng=self._rng,
                )
            )

    async def _run_once(self) -> None:
        connect = self._resolve_connect()
        self.state_machine.transition(WS_STATE_CONNECTING, reason="attempt")
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
            self._connection_started_ms = self._now_ms()
            self.state_machine.transition(WS_STATE_RESYNCING, reason="resync_start")
            outcome = await self._run_resync(websocket)
            self.state_machine.record_resync(outcome)
            if not outcome.converged:
                raise DeepcoinWsResyncNotConverged(outcome.reason)
            self.connected = True
            self._record_gap_end()
            self.state_machine.transition(WS_STATE_HEALTHY, reason="resync_converged")
            logger.info(
                "Deepcoin private WS healthy on %s (resync %s)",
                list(DEEPCOIN_WS_TABLES),
                outcome.reason,
            )
            await self._read_loop(websocket)

    async def _run_resync(self, websocket: Any) -> Any:
        """Run the five-step resync, with step 3 sending the subscribe frame.

        The coordinator is synchronous because the REST client is; it runs in a
        worker thread so the event loop stays free to keep the socket's
        protocol-level keepalive going while the snapshots are taken. Step 3
        hops back onto the loop to actually send the subscribe frame.
        """

        loop = asyncio.get_running_loop()

        def _subscribe() -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._send_subscribe(websocket), loop
            )
            future.result(timeout=DEEPCOIN_WS_SUBSCRIBE_TIMEOUT_SECONDS)

        since_ms = self._now_ms() - 24 * _HOUR_MS
        try:
            stream_instruments = recent_stream_instruments(
                self._session_factory, since_ms=since_ms
            )
        except Exception:
            logger.exception("Failed to read recent Deepcoin stream instruments")
            stream_instruments = []

        return await asyncio.to_thread(
            self.resync_coordinator.run,
            tracker=self.entity_tracker,
            replay_unprocessed=self._replay_unprocessed,
            subscribe=_subscribe,
            stream_instruments=stream_instruments,
        )

    async def _send_subscribe(self, websocket: Any) -> None:
        await websocket.send(build_subscribe_frame())

    def _replay_unprocessed(self, tracker: WsEntityStateTracker, limit: int) -> int:
        """Step 2: fold persisted-but-unprocessed rows into the stream state."""

        try:
            rows = load_unprocessed_events(self._session_factory, limit=limit)
        except Exception:
            logger.exception("Failed to load unprocessed Deepcoin WS events")
            return 0
        replayed: list[int] = []
        for row in rows:
            tracker.apply(row)
            replayed.append(int(row["event_id"]))
        try:
            mark_events_processed(self._session_factory, replayed)
        except Exception:
            logger.exception("Failed to mark Deepcoin WS events processed")
        return len(replayed)

    def _listen_key_deadline_seconds(self) -> float:
        if self._connection_started_ms is None:
            return self._listen_key_ttl_seconds
        elapsed = (self._now_ms() - self._connection_started_ms) / 1000.0
        return self._listen_key_ttl_seconds - elapsed

    @staticmethod
    def _connection_closed_types() -> tuple[type[BaseException], ...]:
        """Lazily resolve the library's close exceptions.

        Kept lazy for the same reason ``_resolve_connect`` is: the module must
        stay importable (and its pure decode/de-duplication logic testable)
        without the ``websockets`` package present.
        """

        try:
            from websockets.exceptions import ConnectionClosed
        except Exception:  # pragma: no cover - dependency always present in prod
            return ()
        return (ConnectionClosed,)

    async def _read_loop(self, websocket: Any) -> None:
        closed_types = self._connection_closed_types()
        while True:
            remaining_key_seconds = self._listen_key_deadline_seconds()
            if remaining_key_seconds <= 0:
                raise DeepcoinWsListenKeyExpiring()
            # Whichever deadline is nearer decides both the wait and, if it
            # expires, which kind of reconnect this is. Deciding that after the
            # fact would misreport a key rotation as a silent stream whenever
            # the clock lands on the boundary.
            key_limited = remaining_key_seconds <= self._silence_timeout_seconds
            timeout = min(self._silence_timeout_seconds, remaining_key_seconds)
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                if key_limited:
                    raise DeepcoinWsListenKeyExpiring() from None
                raise DeepcoinWsSilenceTimeout() from None
            except closed_types:
                # Clean or unclean, a closed socket is a gap. The caller records
                # it and reconnects; it never becomes "there were no events".
                return
            received_at = self._now()
            received_ms = self._now_ms()
            raw_payload = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
            self.state_machine.mark_frame_received()
            await asyncio.to_thread(
                self._persist_frame,
                raw_payload,
                received_at,
                received_ms,
            )
            if is_listen_key_expiry_notice(raw_payload):
                # Persist first, then act: the notice is evidence too.
                logger.info(
                    "Deepcoin private WS listen key expired; reconnecting with a "
                    "fresh key"
                )
                raise DeepcoinWsListenKeyExpiring()

    def _persist_frame(
        self,
        raw_payload: str,
        received_at: datetime,
        received_ms: int,
    ) -> None:
        try:
            rows = persist_ws_frame_rows(
                self._session_factory,
                raw_payload,
                received_at=received_at,
                received_ms=received_ms,
            )
        except Exception:
            logger.exception("Failed to persist Deepcoin private WS frame")
            return
        if not rows:
            return
        self.events_persisted += len(rows)
        self.last_event_id = int(rows[-1]["event_id"])
        self.last_event_received_ms = received_ms
        fresh_ids: list[int] = []
        for row in rows:
            if row.get("processed_state") == PROCESSED_STATE_DUPLICATE:
                self.duplicates_persisted += 1
                continue
            self.entity_tracker.apply(row)
            fresh_ids.append(int(row["event_id"]))
        try:
            mark_events_processed(self._session_factory, fresh_ids)
        except Exception:
            logger.exception("Failed to mark Deepcoin WS events processed")


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
