"""Phase 1 guards for the Deepcoin private WebSocket inbox.

The inbox must persist every frame verbatim, must never drop one, must never
turn a disconnect into a zero, and must never leak the listen key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import (
    DEEPCOIN_LISTENKEY_ACQUIRE_PATH,
    DeepcoinClientError,
    DeepcoinCredentials,
    DeepcoinRestClient,
)
from websockets.exceptions import ConnectionClosedOK

from telegram_kol_research.deepcoin_private_ws import (
    DEEPCOIN_PRIVATE_WS_URL,
    DEEPCOIN_WS_BACKOFF_BASE_SECONDS,
    DeepcoinPrivateWsInbox,
    build_deepcoin_ws_health,
    build_subscribe_frame,
    decode_ws_frame,
    persist_ws_frame,
)
from telegram_kol_research.models import DeepcoinWsConnectionGap, DeepcoinWsEvent


NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
NOW_MS = 1788696000000


ORDER_FRAME = json.dumps(
    {
        "action": "PushOrder",
        "result": [
            {
                "table": "Order",
                "data": {
                    "OS": "1001125145471100",
                    "I": "ETHUSDT",
                    "UM": 1788695999123,
                    "U": "1788695999",
                    "Or": "2",
                },
            }
        ],
    }
)


def _rows(session_factory):
    with session_factory() as session:
        return list(
            session.execute(select(DeepcoinWsEvent).order_by(DeepcoinWsEvent.id))
            .scalars()
            .all()
        )


def _gaps(session_factory):
    with session_factory() as session:
        return list(
            session.execute(
                select(DeepcoinWsConnectionGap).order_by(DeepcoinWsConnectionGap.id)
            )
            .scalars()
            .all()
        )


def test_subscribe_frame_covers_exactly_the_four_phase_one_tables():
    frame = json.loads(build_subscribe_frame())

    assert frame == {
        "action": "subscribe",
        "tables": ["Order", "Trade", "Position", "TriggerOrder"],
    }


def test_decode_reads_only_the_documented_short_keys():
    rows = decode_ws_frame(ORDER_FRAME, received_at=NOW, received_ms=NOW_MS)

    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "Order"
    assert row["action"] == "PushOrder"
    assert row["order_sys_id"] == "1001125145471100"
    assert row["trade_unit_id"] is None
    assert row["position_id"] is None
    # Raw exchange spelling is preserved; normalisation is not phase 1's job.
    assert row["instrument_raw"] == "ETHUSDT"
    assert row["exchange_time_ms"] == 1788695999123
    assert row["exchange_time_source"] == "UM"
    assert row["processed_state"] == "unprocessed"
    assert row["raw_payload"] == ORDER_FRAME


def test_decode_never_falls_back_to_long_key_spellings():
    frame = json.dumps(
        {
            "action": "PushOrder",
            "result": [
                {
                    "table": "Order",
                    "data": {"ordId": "999", "posId": "888", "instId": "ETH-USDT-SWAP"},
                }
            ],
        }
    )

    row = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)[0]

    assert row["order_sys_id"] is None
    assert row["position_id"] is None
    assert row["instrument_raw"] is None


def test_position_and_trigger_short_keys_are_read_from_their_own_fields():
    frame = json.dumps(
        {
            "action": "PushPosition",
            "result": [
                {"table": "Position", "data": {"PI": "1001125145471184", "I": "ETHUSDT"}},
                {
                    "table": "TriggerOrder",
                    "data": {"TU": "default", "OS": "1001125145471183"},
                },
            ],
        }
    )

    rows = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)

    assert [row["channel"] for row in rows] == ["Position", "TriggerOrder"]
    assert rows[0]["position_id"] == "1001125145471184"
    assert rows[0]["trade_unit_id"] is None
    # The literal string "default" is a real observed value, not a missing one.
    assert rows[1]["trade_unit_id"] == "default"
    assert rows[1]["order_sys_id"] == "1001125145471183"


def test_multi_row_frame_shares_one_raw_payload_and_hash():
    frame = json.dumps(
        {
            "action": "PushTrade",
            "result": [
                {"table": "Trade", "data": {"OS": "1"}},
                {"table": "Trade", "data": {"OS": "2"}},
            ],
        }
    )

    rows = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)

    assert len(rows) == 2
    assert rows[0]["raw_payload"] == rows[1]["raw_payload"] == frame
    assert rows[0]["payload_hash"] == rows[1]["payload_hash"]
    assert rows[0]["payload_hash"] == hashlib.sha256(frame.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"action": "PushOrder", "result": "not-a-list"}),
        json.dumps({"action": "PushOrder", "result": []}),
        json.dumps({"action": "PushOrder", "result": [{"table": "Order", "data": []}]}),
        json.dumps({"action": "PushOrder", "result": [{"data": {"OS": "1"}}]}),
    ],
)
def test_unusable_frames_are_still_persisted_verbatim_as_unparsed(raw):
    rows = decode_ws_frame(raw, received_at=NOW, received_ms=NOW_MS)

    assert len(rows) >= 1
    assert all(row["channel"] == "unparsed" for row in rows)
    assert all(row["raw_payload"] == raw for row in rows)


def test_single_object_result_is_a_shape_fix_not_a_dropped_frame():
    frame = json.dumps(
        {"action": "PushOrder", "result": {"table": "Order", "data": {"OS": "7"}}}
    )

    rows = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)

    assert len(rows) == 1
    assert rows[0]["channel"] == "Order"
    assert rows[0]["order_sys_id"] == "7"


def test_exchange_time_falls_back_in_key_order_and_records_its_source():
    frame = json.dumps(
        {"action": "PushTrade", "result": [{"table": "Trade", "data": {"IT": "1788"}}]}
    )

    row = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)[0]

    assert row["exchange_time_ms"] == 1788
    assert row["exchange_time_source"] == "IT"


def test_non_numeric_exchange_time_is_null_rather_than_guessed():
    frame = json.dumps(
        {
            "action": "PushTrade",
            "result": [{"table": "Trade", "data": {"U": "2026-09-06 12:00:00"}}],
        }
    )

    row = decode_ws_frame(frame, received_at=NOW, received_ms=NOW_MS)[0]

    assert row["exchange_time_ms"] is None
    assert row["exchange_time_source"] is None


def test_persist_writes_every_duplicate_frame_without_deduplication(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")

    first = persist_ws_frame(
        session_factory, ORDER_FRAME, received_at=NOW, received_ms=NOW_MS
    )
    second = persist_ws_frame(
        session_factory, ORDER_FRAME, received_at=NOW, received_ms=NOW_MS + 5
    )

    assert len(first) == 1 and len(second) == 1
    rows = _rows(session_factory)
    assert len(rows) == 2
    assert rows[0].payload_hash == rows[1].payload_hash
    assert rows[0].venue == rows[1].venue == "deepcoin"


def test_health_reports_counts_only_and_never_payload_content(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    persist_ws_frame(session_factory, ORDER_FRAME, received_at=NOW, received_ms=NOW_MS)
    persist_ws_frame(session_factory, "garbage", received_at=NOW, received_ms=NOW_MS)

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=None, now=NOW
    )

    assert health["connected"] is False
    assert health["counts_by_channel"]["Order"] == 1
    assert health["counts_by_channel"]["Trade"] == 0
    assert health["counts_by_channel"]["Position"] == 0
    assert health["counts_by_channel"]["TriggerOrder"] == 0
    assert health["unparsed_count"] == 1
    assert health["events_last_hour"] == 2
    serialised = json.dumps(health)
    assert "ETHUSDT" not in serialised
    assert "1001125145471100" not in serialised


class _FakeConnection:
    """Stands in for a ``websockets`` client connection.

    ``recv`` is what phase 2 reads: the read loop wraps it in the
    application-level silence timer, so an iterator would not exercise the code
    under test. Exhausting the frames closes the socket the way the real library
    does, which is what makes the caller record a gap rather than a zero.
    """

    def __init__(self, frames, *, raise_on_exit=None, hang_after=False):
        self._frames = list(frames)
        self._raise_on_exit = raise_on_exit
        self._hang_after = hang_after
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        if self._raise_on_exit is not None:
            raise self._raise_on_exit
        if self._hang_after:
            await asyncio.Event().wait()
        raise ConnectionClosedOK(None, None)


class _FakeClient:
    """Read-only Deepcoin client. Every method here is a GET equivalent."""

    def __init__(self, listen_key="SECRET-LISTEN-KEY", *, instruments=None):
        self.listen_key = listen_key
        self.closed = False
        self.reads: list[str] = []
        self._instruments = (
            [{"instId": "ETH-USDT-SWAP"}, {"instId": "BTC-USDT-SWAP"}]
            if instruments is None
            else list(instruments)
        )

    def acquire_listen_key(self):
        return self.listen_key

    def list_swap_instruments(self):
        self.reads.append("instruments")
        return list(self._instruments)

    def list_positions(self, **kwargs):
        self.reads.append("positions")
        return []

    def list_open_orders(self, **kwargs):
        self.reads.append("open_orders")
        return []

    def list_trade_fills(self, **kwargs):
        self.reads.append("fills")
        return []

    def list_trigger_orders_pending(self, **kwargs):
        self.reads.append("trigger_orders")
        return []

    def close(self):
        self.closed = True


def _inbox(session_factory, connections, *, client=None, **kwargs):
    calls = []

    def _connect(url, **connect_kwargs):
        calls.append(url)
        return connections.pop(0)

    kwargs.setdefault("rng", lambda: 0.5)  # jitter factor 0: deterministic delay
    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=(lambda: _FakeClient()) if client is None else (lambda: client),
        connect_factory=_connect,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
        **kwargs,
    )
    return inbox, calls


def test_run_once_subscribes_then_persists_each_frame(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    connection = _FakeConnection([ORDER_FRAME, ORDER_FRAME])
    inbox, urls = _inbox(session_factory, [connection])

    asyncio.run(inbox._run_once())

    assert connection.sent == [build_subscribe_frame()]
    assert urls[0].startswith(DEEPCOIN_PRIVATE_WS_URL + "?listenKey=")
    assert len(_rows(session_factory)) == 2
    assert inbox.events_persisted == 2


def test_process_start_opens_a_gap_that_closes_only_once_subscribed(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    persist_ws_frame(session_factory, ORDER_FRAME, received_at=NOW, received_ms=NOW_MS)
    connection = _FakeConnection([])
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    inbox, _ = _inbox(session_factory, [connection])
    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    gaps = _gaps(session_factory)
    # One closed startup gap, then one still-open gap for the closed connection.
    assert [gap.reason for gap in gaps] == ["process_start", "connection_closed"]
    assert gaps[0].reconnected_at is not None
    assert gaps[0].last_event_id == 1
    assert gaps[0].last_event_received_ms == NOW_MS
    assert gaps[1].reconnected_at is None
    assert slept == [DEEPCOIN_WS_BACKOFF_BASE_SECONDS]


def test_connection_error_records_a_gap_carrying_no_exception_text(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")

    def _connect(url, **kwargs):
        raise RuntimeError(f"boom while dialing {url}")

    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: _FakeClient(),
        connect_factory=_connect,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
        rng=lambda: 0.5,
    )
    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    gaps = _gaps(session_factory)
    # The startup gap is still open, so the failure does not open a second one:
    # the interval was already unknown and stays one auditable interval.
    assert [gap.reason for gap in gaps] == ["process_start"]
    assert gaps[0].reconnected_at is None
    for gap in gaps:
        assert "SECRET-LISTEN-KEY" not in json.dumps(
            {"reason": gap.reason, "detail": gap.detail}
        )
    assert slept == [DEEPCOIN_WS_BACKOFF_BASE_SECONDS]


def test_reconnect_closes_the_open_gap_and_keeps_the_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    first = _FakeConnection([ORDER_FRAME], raise_on_exit=RuntimeError("dropped"))
    second = _FakeConnection([ORDER_FRAME])
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    inbox, _ = _inbox(session_factory, [first, second])
    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    gaps = _gaps(session_factory)
    assert [gap.reason for gap in gaps] == [
        "process_start",
        "connection_error",
        "connection_closed",
    ]
    assert gaps[0].reconnected_at is not None
    # The gap opened by the drop is closed by the successful resubscribe, and it
    # carries the watermark of the last row persisted before the drop.
    assert gaps[1].reconnected_at is not None
    assert gaps[1].last_event_id == 1
    assert gaps[1].events_persisted_before_gap == 1
    assert len(_rows(session_factory)) == 2


def test_a_gap_never_reports_zero_orders_or_zero_positions(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    persist_ws_frame(session_factory, ORDER_FRAME, received_at=NOW, received_ms=NOW_MS)

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=None, now=NOW
    )

    # Disconnected health is expressed as an open gap plus retained counts, never
    # as a fresh zero that a caller could read as "the exchange is empty".
    assert health["connected"] is False
    assert health["counts_by_channel"]["Order"] == 1


def test_acquire_listen_key_accepts_object_and_single_element_list_shapes():
    payloads = [
        {"code": "0", "data": {"listenkey": "abc"}},
        {"code": "0", "data": [{"listenkey": "def"}]},
    ]
    seen: list[tuple[str, str]] = []

    class _Client(DeepcoinRestClient):
        def _request(self, method, request_path, body_payload=None):
            seen.append((method, request_path))
            return payloads.pop(0)

    client = _Client(DeepcoinCredentials(api_key="k", api_secret="s", passphrase="p"))

    assert client.acquire_listen_key() == "abc"
    assert client.acquire_listen_key() == "def"
    assert seen == [
        ("GET", DEEPCOIN_LISTENKEY_ACQUIRE_PATH),
        ("GET", DEEPCOIN_LISTENKEY_ACQUIRE_PATH),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "0", "data": {}},
        {"code": "0", "data": {"listenkey": ""}},
        {"code": "0", "data": []},
        {"code": "0", "data": [{"listenkey": "SENTINEL-A"}, {"listenkey": "SENTINEL-B"}]},
        {"code": "0", "data": {"listenKey": "SENTINEL-A"}},
        {"code": "0"},
    ],
)
def test_acquire_listen_key_fails_closed_without_echoing_the_body(payload):
    class _Client(DeepcoinRestClient):
        def _request(self, method, request_path, body_payload=None):
            return payload

    client = _Client(DeepcoinCredentials(api_key="k", api_secret="s", passphrase="p"))

    with pytest.raises(DeepcoinClientError) as excinfo:
        client.acquire_listen_key()
    # The failure names the endpoint, never any value carried in the response.
    assert "SENTINEL" not in str(excinfo.value)


def _worker_app(tmp_path, runner):
    from telegram_kol_research.web_app import create_web_app

    return create_web_app(
        database_path=tmp_path / "worker.db",
        runtime_role="worker",
        deepcoin_private_ws_runner=runner,
        deepcoin_client_factory=_FakeClient,
    )


def test_worker_lifespan_starts_and_stops_the_inbox_task(tmp_path):
    from fastapi.testclient import TestClient

    started = asyncio.Event()
    cancelled: list[bool] = []
    seen_kwargs: dict = {}

    async def _runner(**kwargs):
        seen_kwargs.update(kwargs)
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    app = _worker_app(tmp_path, _runner)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert app.state.deepcoin_private_ws_task is not None
        identity = client.get("/api/runtime/deployment-identity").json()
        assert identity["health"]["deepcoin_private_ws"] is True
        # The inbox is observation only: it must not grant any authority.
        assert identity["capabilities"]["global_exchange_authority"] is False
        assert set(seen_kwargs) >= {
            "session_factory",
            "deepcoin_client_factory",
            "inbox_sink",
            "now_provider",
        }

    assert app.state.deepcoin_private_ws_task is None
    assert app.state.deepcoin_private_ws_inbox is None
    assert cancelled == [True]


def test_health_endpoint_is_localhost_only_and_returns_no_payload(tmp_path):
    from fastapi.testclient import TestClient

    async def _runner(**kwargs):
        await asyncio.sleep(3600)

    app = _worker_app(tmp_path, _runner)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        ok = client.get("/api/runtime/deepcoin-ws-health")
        assert ok.status_code == 200
        body = ok.json()
        assert body["connected"] is False
        assert body["counts_by_channel"] == {
            "Order": 0,
            "Trade": 0,
            "Position": 0,
            "TriggerOrder": 0,
        }
        assert body["unparsed_count"] == 0
        assert "raw_payload" not in json.dumps(body)

        forwarded = client.get(
            "/api/runtime/deepcoin-ws-health",
            headers={"x-forwarded-for": "203.0.113.9"},
        )
        assert forwarded.status_code == 404


def test_no_production_module_reads_the_phase_one_inbox_table():
    """Phase 1 keeps ``deepcoin_ws_events`` write-only.

    The only readers allowed are the inbox module itself (its own watermark and
    health projection) and the tests. Any other module reading it would make an
    exchange decision depend on unverified push data.
    """

    import pathlib

    package = pathlib.Path(
        __import__("telegram_kol_research").__file__
    ).parent
    readers = sorted(
        path.name
        for path in package.rglob("*.py")
        if "DeepcoinWsEvent" in path.read_text(encoding="utf-8")
        and path.name not in {"deepcoin_private_ws.py", "models.py"}
    )

    assert readers == []
