"""Phase 3 guards: WebSocket frames wake the existing REST reconciliation.

The one thing phase 3 changes is *when* ``deepcoin_reconcile`` runs. Everything
below is written to hold that line from both sides: the wake path must fire when
it should, stay quiet when it should not, and never survive to change what a
reconciliation concludes.

The frames used here are the same seven real ones phase 2 used
(``tests/fixtures/deepcoin_ws_recorded_frames.jsonl``, captured 2026-09-05).
They carry every transition the wake rules key on -- ``Or`` "4" -> "1",
``TS`` "0" -> "1", ``TU`` ``default`` -> the split ``posId``, ``Po`` 0 -> 0.1,
and one real fill -- so the channel rules are tested against what the exchange
actually sent rather than against a hand-written idea of it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_private_ws import (
    DEEPCOIN_WS_EVENT_COLUMNS,
    DeepcoinPrivateWsInbox,
    build_deepcoin_ws_health,
    decode_ws_frame,
    persist_ws_frame_rows,
)
from telegram_kol_research.deepcoin_reconcile_wake import (
    TRIGGER_TIMER,
    TRIGGER_WAKE,
    WAKE_MAX_PER_MINUTE,
    WAKE_MIN_INTERVAL_SECONDS,
    DeepcoinReconcileWakeSignal,
    wake_channel_for_result,
)
from telegram_kol_research.deepcoin_ws_resync import (
    DeepcoinWsResyncCoordinator,
    RestSnapshot,
    http_status_from_exception,
)
from telegram_kol_research.deepcoin_ws_stream_state import (
    DeepcoinWsStreamStateMachine,
    WsEntityKey,
    WsEntityStateTracker,
)
from telegram_kol_research.models import DeepcoinWsEvent

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
NOW_MS = 1788696000000

FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "deepcoin_ws_recorded_frames.jsonl"
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"


def recorded_frames() -> list[dict]:
    frames = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        frames.append(
            {
                "raw": json.dumps(
                    record["payload"], ensure_ascii=False, separators=(",", ":")
                ),
                "received_ms": int(record["received_ms"]),
            }
        )
    return frames


def _decoded(frame: dict) -> list[dict]:
    return decode_ws_frame(
        frame["raw"], received_at=NOW, received_ms=frame["received_ms"]
    )


def _signal(**kwargs) -> DeepcoinReconcileWakeSignal:
    kwargs.setdefault("min_interval_seconds", 0.02)
    kwargs.setdefault("max_wakes_per_minute", 100)
    return DeepcoinReconcileWakeSignal(**kwargs)


# --------------------------------------------------------------------------
# Task 3: only the right frames wake anything
# --------------------------------------------------------------------------


def test_the_recorded_capture_carries_every_field_the_wake_rules_key_on():
    """Guard the fixture: without these transitions the channel rules prove nothing."""

    seen: dict[str, set[str]] = {}
    for frame in recorded_frames():
        for row in _decoded(frame):
            values = seen.setdefault(row["channel"], set())
            for name in ("order_status", "trigger_status", "position_qty"):
                if row[name] is not None:
                    values.add(f"{name}={row[name]}")
            if row["trade_unit_id"] is not None:
                values.add(f"trade_unit_id={row['trade_unit_id']}")

    assert seen["Order"] == {"order_status=4", "order_status=1"}
    assert seen["Position"] == {"position_qty=0", "position_qty=0.1"}
    assert "trigger_status=0" in seen["TriggerOrder"]
    assert "trigger_status=1" in seen["TriggerOrder"]
    assert "trade_unit_id=default" in seen["TriggerOrder"]
    assert "trade_unit_id=1001125145471184" in seen["TriggerOrder"]


def test_the_recorded_capture_wakes_exactly_the_frames_that_moved_something():
    tracker = WsEntityStateTracker()
    woken: list[str | None] = []
    for frame in recorded_frames():
        for row in _decoded(frame):
            woken.append(wake_channel_for_result(tracker.apply(row)))

    # Every one of the seven real frames moved something the first time it was
    # seen: the position opened, the order filled, the trigger armed, the fill
    # landed. That is what makes the next assertion meaningful.
    assert woken == [
        "Position",
        "Order",
        "TriggerOrder",
        "Position",
        "Order",
        "TriggerOrder",
        "Trade",
    ]


def test_replaying_the_same_capture_wakes_nothing_the_second_time():
    """A repeat carries no news, so it costs no REST round trip."""

    tracker = WsEntityStateTracker()
    for frame in recorded_frames():
        for row in _decoded(frame):
            tracker.apply(row)

    woken = [
        wake_channel_for_result(tracker.apply(row))
        for frame in recorded_frames()
        for row in _decoded(frame)
    ]

    # The Trade frame is the one exception, and deliberately so: a fill is
    # identified per fill, so a re-sent fill frame is a new identity rather than
    # a restatement of an old one. De-duplication upstream marks a literal
    # re-delivery ``duplicate`` before it ever reaches the tracker.
    assert [channel for channel in woken if channel is not None] == ["Trade"]


@pytest.mark.parametrize(
    ("channel", "first", "second", "expected"),
    [
        # Order: only ``Or`` matters.
        ("Order", {"order_status": "4"}, {"order_status": "1"}, "Order"),
        ("Order", {"order_status": "4"}, {"order_status": "4"}, None),
        ("Order", {"order_status": "4"}, {"instrument_raw": "BTCUSDT"}, None),
        # TriggerOrder: ``TS`` or ``TU``.
        (
            "TriggerOrder",
            {"trigger_status": "0"},
            {"trigger_status": "1"},
            "TriggerOrder",
        ),
        (
            "TriggerOrder",
            {"trade_unit_id": "default"},
            {"trade_unit_id": "pos-1"},
            "TriggerOrder",
        ),
        ("TriggerOrder", {"trigger_status": "0"}, {"trigger_status": "0"}, None),
        # Position: only ``Po``.
        ("Position", {"position_qty": "0"}, {"position_qty": "0.1"}, "Position"),
        ("Position", {"position_qty": "0.1"}, {"position_qty": "0.1"}, None),
    ],
)
def test_only_the_documented_field_changes_wake_their_channel(
    channel, first, second, expected
):
    tracker = WsEntityStateTracker()
    identity = (
        {"position_id": "pos-1"} if channel == "Position" else {"order_sys_id": "ord-1"}
    )

    tracker.apply({"channel": channel, "received_ms": 1, **identity, **first})
    result = tracker.apply(
        {"channel": channel, "received_ms": 2, **identity, **second}
    )

    assert wake_channel_for_result(result) == expected


def test_a_trade_frame_always_wakes_because_a_fill_is_never_a_repeat():
    tracker = WsEntityStateTracker()
    first = tracker.apply(
        {
            "channel": "Trade",
            "order_sys_id": "ord-1",
            "payload_hash": "hash-a",
            "received_ms": 1,
        }
    )
    second = tracker.apply(
        {
            "channel": "Trade",
            "order_sys_id": "ord-1",
            "payload_hash": "hash-b",
            "received_ms": 2,
        }
    )

    assert wake_channel_for_result(first) == "Trade"
    assert wake_channel_for_result(second) == "Trade"


def test_an_out_of_order_frame_never_wakes_anything():
    """Rule 6 decides this: an older frame is not news, whatever it carries."""

    tracker = WsEntityStateTracker()
    tracker.apply(
        {
            "channel": "Order",
            "order_sys_id": "ord-1",
            "exchange_time_ms": 200,
            "received_ms": 2,
            "order_status": "1",
        }
    )
    stale = tracker.apply(
        {
            "channel": "Order",
            "order_sys_id": "ord-1",
            "exchange_time_ms": 100,
            "received_ms": 3,
            "order_status": "4",
        }
    )

    assert stale.out_of_order is True
    assert wake_channel_for_result(stale) is None
    assert tracker.state_for(WsEntityKey("Order", "ord-1")).order_status == "1"


def test_an_unidentifiable_or_control_frame_wakes_nothing():
    tracker = WsEntityStateTracker()

    control = tracker.apply({"channel": "control", "received_ms": 1})
    unparsed = tracker.apply({"channel": "unparsed", "received_ms": 2})

    assert wake_channel_for_result(control) is None
    assert wake_channel_for_result(unparsed) is None


def test_a_duplicate_frame_never_reaches_the_tracker_and_never_wakes(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frame = recorded_frames()[1]
    requests: list[str] = []

    class _Recorder:
        def request(self, *, channel):
            requests.append(channel)

    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: None,
        wake_signal=_Recorder(),
    )

    inbox._persist_frame(frame["raw"], NOW, frame["received_ms"])
    inbox._persist_frame(frame["raw"], NOW, frame["received_ms"])

    assert requests == ["Order"]


# --------------------------------------------------------------------------
# Tasks 1 and 2: wake or timeout, debounce, merge, per-minute cap
# --------------------------------------------------------------------------


def test_the_timer_still_fires_when_nothing_ever_wakes():
    async def _run():
        signal = _signal()

        assert await signal.wait_for_next_run(timeout=0.05) == TRIGGER_TIMER

    asyncio.run(_run())


def test_a_request_wakes_the_loop_before_the_timer_would_have():
    async def _run():
        signal = _signal()
        loop = asyncio.get_running_loop()
        started = loop.time()
        loop.call_later(0.02, lambda: signal.request(channel="Trade"))

        trigger = await signal.wait_for_next_run(timeout=30.0)

        assert trigger == TRIGGER_WAKE
        assert loop.time() - started < 1.0
        assert signal.last_wake_channel == "Trade"

    asyncio.run(_run())


def test_requests_inside_the_minimum_interval_merge_into_one_wake():
    async def _run():
        signal = _signal(min_interval_seconds=0.2)

        signal.request(channel="Trade")
        assert await signal.wait_for_next_run(timeout=30.0) == TRIGGER_WAKE

        loop = asyncio.get_running_loop()
        for delay in (0.0, 0.01, 0.02, 0.03):
            loop.call_later(delay, lambda: signal.request(channel="Order"))
        started = loop.time()

        assert await signal.wait_for_next_run(timeout=30.0) == TRIGGER_WAKE

        elapsed = loop.time() - started
        assert elapsed >= 0.15, "four events inside the interval must not be four wakes"
        snapshot = signal.health_snapshot()
        assert snapshot["wakes_last_hour"] == 2
        assert snapshot["wake_requests_seen"] == 5

    asyncio.run(_run())


def test_the_debounce_never_pushes_the_timer_out():
    async def _run():
        """A wake arriving just before the deadline must not delay the timer."""

        signal = _signal(min_interval_seconds=5.0)
        signal.request(channel="Trade")
        assert await signal.wait_for_next_run(timeout=30.0) == TRIGGER_WAKE

        loop = asyncio.get_running_loop()
        signal.request(channel="Trade")
        started = loop.time()

        trigger = await signal.wait_for_next_run(timeout=0.1)

        assert trigger == TRIGGER_TIMER
        assert loop.time() - started < 1.0

    asyncio.run(_run())


def test_the_per_minute_cap_falls_back_to_pure_polling():
    async def _run():
        signal = _signal(min_interval_seconds=0.0, max_wakes_per_minute=3)

        for _ in range(3):
            signal.request(channel="Trade")
            assert await signal.wait_for_next_run(timeout=30.0) == TRIGGER_WAKE

        assert signal.wake_throttled is True
        signal.request(channel="Trade")
        trigger = await signal.wait_for_next_run(timeout=0.1)

        assert trigger == TRIGGER_TIMER, "past the cap the loop must go back to polling"
        snapshot = signal.health_snapshot()
        assert snapshot["wakes_last_hour"] == 3
        assert snapshot["wakes_throttled_last_hour"] >= 1
        assert snapshot["wake_throttled"] is True

    asyncio.run(_run())


def test_a_frame_storm_cannot_spin_the_wait_loop():
    async def _run():
        """Throttled requests advance the gate too, so refusals stay rate limited."""

        signal = _signal(min_interval_seconds=0.05, max_wakes_per_minute=1)
        signal.request(channel="Trade")
        assert await signal.wait_for_next_run(timeout=30.0) == TRIGGER_WAKE

        stop = False

        async def _storm():
            while not stop:
                signal.request(channel="Trade")
                await asyncio.sleep(0)

        task = asyncio.create_task(_storm())
        assert await signal.wait_for_next_run(timeout=0.3) == TRIGGER_TIMER
        stop = True
        await task

        # 0.3s of wall time at a 0.05s gate can refuse at most a handful of wakes,
        # however many million requests were made in the meantime.
        assert signal.health_snapshot()["wakes_throttled_last_hour"] <= 8

    asyncio.run(_run())


def test_a_request_from_a_worker_thread_reaches_the_loop():
    async def _run():
        """The stream persists frames off-loop, so this is the real calling pattern."""

        signal = _signal()
        # Bind the loop the way the reconcile loop does before any frame arrives.
        assert await signal.wait_for_next_run(timeout=0.01) == TRIGGER_TIMER

        thread = threading.Thread(target=lambda: signal.request(channel="Position"))
        thread.start()
        trigger = await signal.wait_for_next_run(timeout=30.0)
        thread.join()

        assert trigger == TRIGGER_WAKE
        assert signal.last_wake_channel == "Position"

    asyncio.run(_run())


def test_the_defaults_are_the_documented_ones():
    async def _run():
        signal = DeepcoinReconcileWakeSignal()
        snapshot = signal.health_snapshot()

        assert WAKE_MIN_INTERVAL_SECONDS == 2.0
        assert WAKE_MAX_PER_MINUTE == 20
        assert snapshot["wake_min_interval_seconds"] == 2.0
        assert snapshot["wake_max_per_minute"] == 20

    asyncio.run(_run())


# --------------------------------------------------------------------------
# Task 6: seeding the ordering tracker from the REST snapshot
# --------------------------------------------------------------------------


def _snapshot(*, complete=True, **kwargs) -> RestSnapshot:
    return RestSnapshot(
        complete=complete,
        positions=kwargs.get("positions", []),
        open_orders=kwargs.get("open_orders", []),
        trigger_orders=kwargs.get("trigger_orders", []),
        fills=kwargs.get("fills", []),
    )


def test_a_complete_snapshot_seeds_the_ordering_tracker():
    tracker = WsEntityStateTracker()
    snapshot = _snapshot(
        positions=[{"posId": "pos-1", "uTime": "500"}],
        open_orders=[{"ordId": "ord-1", "uTime": "500"}],
        trigger_orders=[{"ordId": "trig-1", "cTime": "500"}],
        fills=[{"tradeId": "fill-1", "fillTime": "500"}],
    )

    seeded = DeepcoinWsResyncCoordinator.seed_tracker_from_snapshot(
        tracker, snapshot, snapshot_ms=NOW_MS
    )

    assert seeded == 3
    assert tracker.entity_count() == 3
    assert tracker.seeded_count == 3
    # Fills are append-only facts identified per fill, and REST does not publish
    # the stream's own fill identity, so seeding one could only invent an
    # identity that no frame will ever match.
    assert tracker.state_for(WsEntityKey("Position", "pos-1")).exchange_time_ms == 500


def test_an_incomplete_snapshot_seeds_nothing_at_all():
    tracker = WsEntityStateTracker()
    snapshot = _snapshot(
        complete=False,
        positions=[{"posId": "pos-1", "uTime": "500"}],
        open_orders=[{"ordId": "ord-1", "uTime": "500"}],
    )

    seeded = DeepcoinWsResyncCoordinator.seed_tracker_from_snapshot(
        tracker, snapshot, snapshot_ms=NOW_MS
    )

    assert seeded == 0
    assert tracker.entity_count() == 0, "incomplete is unknown, not a partial truth"


def test_a_stale_frame_arriving_after_seeding_is_rejected_as_out_of_order():
    """The whole point of task 6: a reconnect must be able to refuse old news."""

    tracker = WsEntityStateTracker()
    DeepcoinWsResyncCoordinator.seed_tracker_from_snapshot(
        tracker,
        _snapshot(open_orders=[{"ordId": "1001125145471184", "uTime": "1788636239000"}]),
        snapshot_ms=NOW_MS,
    )

    # The frame the exchange sent before the disconnect, delivered afterwards.
    stale = tracker.apply(
        {
            "channel": "Order",
            "order_sys_id": "1001125145471184",
            "exchange_time_ms": 1788635962122,
            "received_ms": NOW_MS + 10,
            "order_status": "4",
        }
    )

    assert stale.applied is False
    assert stale.out_of_order is True
    assert wake_channel_for_result(stale) is None
    state = tracker.state_for(WsEntityKey("Order", "1001125145471184"))
    assert state.exchange_time_ms == 1788636239000, "REST state must not roll back"


def test_seeding_never_rolls_back_what_the_stream_already_knows():
    tracker = WsEntityStateTracker()
    tracker.apply(
        {
            "channel": "Order",
            "order_sys_id": "ord-1",
            "exchange_time_ms": 900,
            "received_ms": 5,
            "order_status": "1",
        }
    )

    seeded = DeepcoinWsResyncCoordinator.seed_tracker_from_snapshot(
        tracker, _snapshot(open_orders=[{"ordId": "ord-1", "uTime": "100"}]), snapshot_ms=NOW_MS
    )

    state = tracker.state_for(WsEntityKey("Order", "ord-1"))
    assert seeded == 0
    assert state.exchange_time_ms == 900
    assert state.order_status == "1"
    assert tracker.out_of_order_count == 0, "a stale REST row is not a reordered frame"


def test_the_resync_seeds_from_the_second_snapshot_and_reports_how_many():
    class _Client:
        def list_swap_instruments(self):
            return [{"instId": "ETH-USDT-SWAP"}]

        def list_positions(self):
            return [{"instId": "ETH-USDT-SWAP", "posId": "pos-1", "uTime": "500"}]

        def list_open_orders(self):
            return [{"instId": "ETH-USDT-SWAP", "ordId": "ord-1", "uTime": "500"}]

        def list_trade_fills(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return [{"instId": inst_id, "ordId": "trig-1", "cTime": "500"}]

    tracker = WsEntityStateTracker()
    coordinator = DeepcoinWsResyncCoordinator(
        client_factory=_Client,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
    )

    outcome = coordinator.run(
        tracker=tracker,
        replay_unprocessed=lambda _t, _l: 0,
        subscribe=lambda: None,
    )

    assert outcome.converged is True
    assert outcome.seeded_entities == 3
    assert tracker.entity_count() == 3
    # Phase 2 shipped with the five step names as its contract; seeding is
    # bookkeeping inside step 4, not a sixth thing the exchange is asked about.
    assert set(outcome.step_durations_ms) == {
        "step1_rest_snapshot",
        "step2_replay_events",
        "step3_subscribe",
        "step4_rest_snapshot",
        "step5_compare",
    }


# --------------------------------------------------------------------------
# Task 7: new REST reads must branch on ``complete`` first
# --------------------------------------------------------------------------

_SNAPSHOT_COLLECTIONS = {"positions", "open_orders", "trigger_orders", "fills"}
_GUARDED_MODULES = (
    "deepcoin_private_ws.py",
    "deepcoin_ws_resync.py",
    "deepcoin_ws_stream_state.py",
    "deepcoin_reconcile_wake.py",
)


@pytest.mark.architecture
@pytest.mark.parametrize("filename", _GUARDED_MODULES)
def test_every_snapshot_collection_read_is_preceded_by_a_completeness_check(filename):
    """Turn the discipline into a test rather than a convention.

    ``resyncing -> healthy`` is decided entirely by ``RestSnapshot.complete``,
    so "read the collection, then check whether it meant anything" is the one
    shape that silently turns unknown into zero. Accesses through ``self``
    inside ``RestSnapshot`` itself are exempt: that class *is* the flag's owner,
    and the rule is about its consumers.
    """

    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # A nested helper is analysed as part of its enclosing function: a guard at
    # the top of the outer function does protect the inner one, and splitting
    # them would report a false offender for every closure.
    nested = {
        id(child)
        for node in functions
        for child in ast.walk(node)
        if child is not node
        and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    offenders: list[str] = []

    for node in functions:
        if id(node) in nested:
            continue
        collection_lines = [
            child.lineno
            for child in ast.walk(node)
            if isinstance(child, ast.Attribute)
            and child.attr in _SNAPSHOT_COLLECTIONS
            and not (isinstance(child.value, ast.Name) and child.value.id == "self")
        ]
        if not collection_lines:
            continue
        complete_lines = [
            child.lineno
            for child in ast.walk(node)
            if (isinstance(child, ast.Attribute) and child.attr == "complete")
            or (isinstance(child, ast.Name) and child.id == "complete")
        ]
        if not complete_lines or min(complete_lines) > min(collection_lines):
            offenders.append(f"{filename}:{node.name}:{min(collection_lines)}")

    assert offenders == []


def test_the_comparison_refuses_to_count_anything_from_an_incomplete_snapshot():
    complete = _snapshot(positions=[{"posId": "pos-1", "uTime": "200"}])
    incomplete = RestSnapshot(
        complete=False,
        positions=[],
        open_orders=[],
        trigger_orders=[],
        fills=[],
        incomplete_reads=("positions:DeepcoinClientError:401",),
    )

    advanced, reasons = DeepcoinWsResyncCoordinator.compare_forward_only(
        complete, incomplete, WsEntityStateTracker()
    )

    assert advanced == 0, "a disappearance you could not read is not progress"
    assert reasons == ["incomplete_snapshot"]


def test_a_401_is_recorded_with_its_call_and_status_and_blocks_convergence():
    """The 2026-09-06 production 401, reproduced offline so it stays attributable."""

    class _Response:
        status_code = 401

    class _HttpError(Exception):
        def __init__(self):
            super().__init__("401 Unauthorized")
            self.response = _Response()

    class _DeepcoinClientError(RuntimeError):
        pass

    class _Client:
        def list_swap_instruments(self):
            return [{"instId": "ETH-USDT-SWAP"}]

        def list_positions(self):
            return [{"instId": "ETH-USDT-SWAP", "posId": "pos-1", "uTime": "1"}]

        def list_open_orders(self):
            return []

        def list_trade_fills(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            try:
                raise _HttpError()
            except _HttpError as exc:
                raise _DeepcoinClientError("Deepcoin request failed") from exc

    machine = DeepcoinWsStreamStateMachine(
        now_provider=lambda: NOW, monotonic_ms_provider=lambda: NOW_MS
    )
    coordinator = DeepcoinWsResyncCoordinator(
        client_factory=_Client,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
    )

    outcome = coordinator.run(
        tracker=WsEntityStateTracker(),
        replay_unprocessed=lambda _t, _l: 0,
        subscribe=lambda: None,
    )
    machine.record_resync(outcome)

    assert outcome.converged is False
    assert outcome.reason == "incomplete_rest_read"
    failures = {
        (failure.call, failure.exception_type, failure.http_status)
        for failure in outcome.read_failures
    }
    assert failures == {
        (
            "trigger_orders[ETH-USDT-SWAP]",
            "_DeepcoinClientError",
            401,
        )
    }
    assert outcome.seeded_entities == 0, "an unreadable account seeds nothing"
    # A REST 401 says nothing about whether the socket is delivering frames.
    assert machine.state != "disconnected"


def test_a_read_failure_never_looks_like_an_empty_account():
    class _Client:
        def list_swap_instruments(self):
            return [{"instId": "ETH-USDT-SWAP"}]

        def list_positions(self):
            raise RuntimeError("boom")

        def list_open_orders(self):
            return []

        def list_trade_fills(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    coordinator = DeepcoinWsResyncCoordinator(
        client_factory=_Client,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
    )

    snapshot = coordinator.rest_snapshot()

    assert snapshot.complete is False
    assert snapshot.positions == []
    assert snapshot.read_failures[0].call == "positions"
    assert snapshot.read_failures[0].http_status is None
    assert snapshot.incomplete_reads == ("positions:RuntimeError:-",)


def test_the_http_status_is_dug_out_of_a_wrapped_exception_without_looping():
    class _Response:
        status_code = 429

    inner = RuntimeError("rate limited")
    inner.response = _Response()
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    cyclic = RuntimeError("cyclic")
    cyclic.__cause__ = cyclic

    assert http_status_from_exception(outer) == 429
    assert http_status_from_exception(cyclic) is None
    assert http_status_from_exception(None) is None


# --------------------------------------------------------------------------
# Persistence and health surface
# --------------------------------------------------------------------------


def test_the_persisted_column_list_matches_the_table_exactly():
    """Phase 3 decodes more than phase 1 stores; the allowlist is what separates them."""

    columns = {column.name for column in DeepcoinWsEvent.__table__.columns}

    # ``id`` and ``created_at`` are assigned by the database, never by the
    # decoder; everything else must be written explicitly or it would silently
    # stop being persisted the day a decoded key is renamed.
    assert set(DEEPCOIN_WS_EVENT_COLUMNS) | {"id", "created_at"} == columns
    assert "id" not in DEEPCOIN_WS_EVENT_COLUMNS


def test_the_new_short_keys_are_decoded_but_never_persisted(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frame = recorded_frames()[1]

    rows = persist_ws_frame_rows(
        session_factory, frame["raw"], received_at=NOW, received_ms=frame["received_ms"]
    )

    assert rows[0]["order_status"] == "4"
    with session_factory() as session:
        stored = session.query(DeepcoinWsEvent).one()
    assert not hasattr(stored, "order_status")


def test_the_health_endpoint_exposes_every_phase_three_counter(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    signal = _signal()
    signal.record_reconcile_run(TRIGGER_TIMER)
    signal.record_reconcile_run(TRIGGER_WAKE)
    signal.record_reconcile_failure(
        RuntimeError("nope"), call="deepcoin_execution_reconcile", http_status=401
    )

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=None, now=NOW, wake_signal=signal
    )

    assert health["reconcile_runs_last_hour"] == {
        "by_timer": 1,
        "by_wake": 1,
        "total": 2,
    }
    for field in (
        "wakes_last_hour",
        "wakes_throttled_last_hour",
        "last_wake_at",
        "last_wake_channel",
        "wake_throttled",
        "seeded_entity_count",
        "last_resync_read_failures",
    ):
        assert field in health
    assert health["last_reconcile_failure"]["http_status"] == 401
    assert health["last_reconcile_failure"]["call"] == "deepcoin_execution_reconcile"


def test_the_health_endpoint_reports_zeros_rather_than_omitting_the_counters(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=None, now=NOW, wake_signal=None
    )

    assert health["wake_signal_installed"] is False
    assert health["wakes_last_hour"] == 0
    assert health["reconcile_runs_last_hour"] == {
        "by_timer": 0,
        "by_wake": 0,
        "total": 0,
    }


def test_no_credential_or_payload_reaches_the_failure_record():
    signal = _signal()
    signal.record_reconcile_failure(
        RuntimeError("secret-passphrase and body {\"ordId\": \"1\"}"),
        call="deepcoin_execution_reconcile",
        http_status=401,
    )

    recorded = json.dumps(signal.health_snapshot()["last_reconcile_failure"])

    assert "secret-passphrase" not in recorded
    assert "ordId" not in recorded
    assert "RuntimeError" in recorded


def test_the_inbox_keeps_working_with_no_wake_signal_at_all(tmp_path):
    """WS-only deployments and every non-worker role must be unaffected."""

    session_factory = create_session_factory(tmp_path / "ws.db")
    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory, deepcoin_client_factory=lambda: None
    )

    for frame in recorded_frames():
        inbox._persist_frame(frame["raw"], NOW, frame["received_ms"])

    assert inbox.events_persisted == 7
    assert inbox.wake_signal is None


# --------------------------------------------------------------------------
# The core assertion: a woken pass IS a timed pass
# --------------------------------------------------------------------------


# One live binding, plus exchange objects that belong to no binding at all: the
# pending conditional entry order and the position the user might have opened by
# hand. This is supplementary check 7 of the handoff document, offline. Nothing
# here may be cancelled, amended or claimed, and the two trigger paths must
# reach the same conclusion about all of it.
_MANUAL_TRIGGER_ORDER = {
    "instId": "BTC-USDT-SWAP",
    "ordId": "manual-conditional-1",
    "posSide": "short",
    "side": "sell",
    "sz": "24",
    "triggerPx": "76410",
    "closeSLTriggerPrice": "76000",
    "triggerOrderType": "Conditional",
    "cTime": "1788503485000",
}
_MANUAL_POSITION = {
    "instId": "BTC-USDT-SWAP",
    "posId": "manual-position-1",
    "posSide": "short",
    "pos": "0.5",
    "avgPx": "76500",
    "mgnMode": "cross",
    "posMode": "split",
    "uTime": "1788503485000",
}
_BOUND_POSITION = {
    "instId": "ETH-USDT-SWAP",
    "posId": "bound-position-1",
    "posSide": "long",
    "pos": "0.1",
    "avgPx": "2478.78",
    "mgnMode": "cross",
    "posMode": "split",
    "uTime": "1788636239000",
}

_WRITE_METHODS = (
    "place_order",
    "trigger_order",
    "cancel_order",
    "cancel_trigger_order",
    "set_position_sltp",
    "cancel_position_sltp",
    "replace_order_sltp",
)


class _RecordingReconcileClient:
    """Read-only Deepcoin stand-in that records the exact call sequence.

    It defines none of the write methods. ``hasattr`` is how several production
    paths decide whether a client can mutate, so their absence is the assertion:
    a wake-driven pass that tried to cancel the manual order would raise here
    rather than quietly succeed.
    """

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def list_positions(self, *, inst_id=None):
        self.calls.append(f"list_positions:{inst_id}")
        rows = [_BOUND_POSITION, _MANUAL_POSITION]
        if inst_id is None:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows if row["instId"] == inst_id]

    def list_open_orders(self, *, inst_id=None):
        self.calls.append(f"list_open_orders:{inst_id}")
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(f"list_trigger_orders_pending:{inst_id}")
        if inst_id == "BTC-USDT-SWAP":
            return [dict(_MANUAL_TRIGGER_ORDER)]
        return []

    def list_trade_fills(self, *, inst_id=None):
        self.calls.append(f"list_trade_fills:{inst_id}")
        return []

    def list_order_history(self, *, inst_id=None):
        self.calls.append(f"list_order_history:{inst_id}")
        return []

    def list_trigger_order_history(self, *, inst_id):
        self.calls.append(f"list_trigger_order_history:{inst_id}")
        return []

    def list_position_history(self, *, inst_id, pos_id=None):
        self.calls.append(f"list_position_history:{inst_id}:{pos_id}")
        return []


def _seed_reconcile_scenario(session_factory) -> None:
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        upsert_execution_binding,
    )

    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol-1",
            chat_id=-100123,
            message_id=7,
            symbol="ETH-USDT-SWAP",
            side="long",
            order_id="1001125145471184",
            pos_id="bound-position-1",
            status="open",
        ),
    )


def _clock_defaulted_columns() -> dict[str, set[str]]:
    """Columns whose value is stamped by a callable default at insert time.

    ``created_at`` and its relatives read the real clock, not the loop's
    injected ``now_provider``, so two runs at two different wall-clock instants
    differ there by construction. Deriving the set from the table definitions
    rather than listing names keeps it correct as the schema grows -- and keeps
    the comparison total over every other column of every other table.
    """

    from telegram_kol_research.models import Base

    return {
        table.name: {
            column.name
            for column in table.columns
            if column.default is not None
            and getattr(column.default, "is_callable", False)
        }
        for table in Base.metadata.tables.values()
    }


def _database_fingerprint(database_path: pathlib.Path) -> dict[str, list[tuple]]:
    """Every row of every table, so nothing can differ unnoticed."""

    stamped = _clock_defaulted_columns()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        fingerprint: dict[str, list[tuple]] = {}
        for table in tables:
            skip = stamped.get(table, set())
            cursor = connection.execute(f"SELECT * FROM {table}")  # noqa: S608
            keep = [
                index
                for index, description in enumerate(cursor.description)
                if description[0] not in skip
            ]
            fingerprint[table] = sorted(
                (tuple(row[index] for index in keep) for row in cursor), key=repr
            )
        return fingerprint
    finally:
        connection.close()


def _run_two_reconcile_passes(tmp_path, *, name, wake_the_second_pass):
    """Run the real loop for exactly two passes and report the second one.

    The second pass is timer-driven or wake-driven depending on the flag, and
    everything else -- database, exchange responses, clock -- is identical
    between the two runs. That is what makes the comparison an equivalence
    check rather than two unrelated observations.
    """

    from telegram_kol_research.web_app import run_deepcoin_execution_reconcile_loop

    database_path = tmp_path / f"{name}.db"
    session_factory = create_session_factory(database_path)
    _seed_reconcile_scenario(session_factory)

    calls: list[str] = []
    signal = (
        _signal(min_interval_seconds=0.0) if wake_the_second_pass else None
    )
    completed: list[datetime] = []
    done = asyncio.Event()

    def _observer(*, observed_at):
        calls.append("pass_end")
        completed.append(observed_at)
        if len(completed) == 1 and signal is not None:
            signal.request(channel="Trade")
        if len(completed) >= 2:
            done.set()

    async def _run():
        task = asyncio.create_task(
            run_deepcoin_execution_reconcile_loop(
                session_factory=session_factory,
                deepcoin_client_factory=lambda: _RecordingReconcileClient(calls),
                # Long enough that the second pass can only be wake-driven when
                # a wake is what we are testing.
                interval_seconds=0 if signal is None else 60,
                now_provider=lambda: NOW,
                authority_observer=_observer,
                wake_signal=signal,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    second_pass = calls[calls.index("pass_end") + 1 :]
    return second_pass, _database_fingerprint(database_path), signal


def test_a_woken_pass_makes_exactly_the_same_calls_as_a_timed_pass(tmp_path):
    """The core phase 3 assertion. If this ever fails, waking changed behaviour."""

    timed_calls, timed_db, _ = _run_two_reconcile_passes(
        tmp_path, name="timed", wake_the_second_pass=False
    )
    woken_calls, woken_db, signal = _run_two_reconcile_passes(
        tmp_path, name="woken", wake_the_second_pass=True
    )

    assert woken_calls == timed_calls
    assert woken_calls.count("list_positions:None") >= 1, "the pass must have run"
    assert woken_db == timed_db
    assert signal.health_snapshot()["reconcile_runs_last_hour"] == {
        "by_timer": 1,
        "by_wake": 1,
        "total": 2,
    }


def test_neither_trigger_writes_anything_to_an_order_that_belongs_to_no_binding(
    tmp_path,
):
    """Supplementary check 7, offline: manual objects survive both paths untouched."""

    _, timed_db, _ = _run_two_reconcile_passes(
        tmp_path, name="manual-timed", wake_the_second_pass=False
    )
    _, woken_db, _ = _run_two_reconcile_passes(
        tmp_path, name="manual-woken", wake_the_second_pass=True
    )

    assert woken_db == timed_db
    for method in _WRITE_METHODS:
        assert not hasattr(_RecordingReconcileClient([]), method)
    # Nothing anywhere in the database may claim the manual objects.
    for fingerprint in (timed_db, woken_db):
        rendered = repr(fingerprint)
        assert "manual-conditional-1" not in rendered
        assert "manual-position-1" not in rendered


def test_a_wake_arriving_mid_pass_does_not_re_enter_the_loop(tmp_path):
    """One task, one pass at a time. A wake during a pass is merged, not stacked."""

    from telegram_kol_research.web_app import run_deepcoin_execution_reconcile_loop

    session_factory = create_session_factory(tmp_path / "reentrancy.db")
    _seed_reconcile_scenario(session_factory)
    signal = _signal(min_interval_seconds=0.0)
    in_flight = 0
    max_in_flight = 0
    completed = 0
    done = asyncio.Event()

    class _SlowClient(_RecordingReconcileClient):
        def list_positions(self, *, inst_id=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                # Fire a storm of wakes while a pass is demonstrably running.
                for _ in range(50):
                    signal.request(channel="Trade")
                return super().list_positions(inst_id=inst_id)
            finally:
                in_flight -= 1

    def _observer(*, observed_at):
        nonlocal completed
        completed += 1
        if completed >= 3:
            done.set()

    async def _run():
        task = asyncio.create_task(
            run_deepcoin_execution_reconcile_loop(
                session_factory=session_factory,
                deepcoin_client_factory=lambda: _SlowClient([]),
                interval_seconds=60,
                now_provider=lambda: NOW,
                authority_observer=_observer,
                wake_signal=signal,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert max_in_flight == 1, "a wake during a pass must not start a second pass"
    assert completed >= 3
    snapshot = signal.health_snapshot()
    # Pass 1 is the timer's; everything after it was woken by the storm.
    assert snapshot["reconcile_runs_last_hour"]["by_timer"] == 1
    assert snapshot["reconcile_runs_last_hour"]["by_wake"] >= 2
    assert snapshot["wake_requests_seen"] > 50, "the storm really did arrive"


def test_the_loop_without_a_wake_signal_still_polls_on_its_timer(tmp_path):
    """WS down, no signal installed: exactly the pre-phase-3 loop."""

    from telegram_kol_research.web_app import run_deepcoin_execution_reconcile_loop

    session_factory = create_session_factory(tmp_path / "polling.db")
    _seed_reconcile_scenario(session_factory)
    completed = 0
    done = asyncio.Event()

    def _observer(*, observed_at):
        nonlocal completed
        completed += 1
        if completed >= 3:
            done.set()

    async def _run():
        task = asyncio.create_task(
            run_deepcoin_execution_reconcile_loop(
                session_factory=session_factory,
                deepcoin_client_factory=lambda: _RecordingReconcileClient([]),
                interval_seconds=0,
                now_provider=lambda: NOW,
                authority_observer=_observer,
                wake_signal=None,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert completed == 3


def test_a_reconcile_failure_is_attributed_and_the_loop_keeps_its_cadence(tmp_path):
    """An intermittent 401 must be recorded, retried next pass, and nothing more.

    The failure is raised while the pass is acquiring its client, which is where
    a credential-shaped failure actually surfaces to this loop: a failed read
    *inside* reconciliation is already absorbed there and recorded as an error
    on the snapshot, and phase 3 does not touch that.
    """

    from telegram_kol_research.deepcoin_client import DeepcoinClientError
    from telegram_kol_research.web_app import run_deepcoin_execution_reconcile_loop

    session_factory = create_session_factory(tmp_path / "failure.db")
    _seed_reconcile_scenario(session_factory)
    signal = _signal(min_interval_seconds=0.0)
    attempts = 0
    done = asyncio.Event()

    class _Response:
        status_code = 401

    def _factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            inner = RuntimeError("401 Unauthorized")
            inner.response = _Response()
            try:
                raise inner
            except RuntimeError as exc:
                raise DeepcoinClientError("Deepcoin request failed") from exc
        if attempts >= 2:
            done.set()
        return _RecordingReconcileClient([])

    async def _run():
        task = asyncio.create_task(
            run_deepcoin_execution_reconcile_loop(
                session_factory=session_factory,
                deepcoin_client_factory=_factory,
                interval_seconds=0,
                now_provider=lambda: NOW,
                wake_signal=signal,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    failure = signal.health_snapshot()["last_reconcile_failure"]
    assert failure["exception_type"] == "DeepcoinClientError"
    assert failure["http_status"] == 401
    assert failure["call"] == "deepcoin_execution_reconcile"
    assert attempts >= 2, "the next pass must retry rather than accept 'nothing there'"
    with session_factory() as session:
        from telegram_kol_research.models import ExecutionBinding

        binding = session.query(ExecutionBinding).one()
    assert binding.status == "open", "a failed read must never close a binding"


def test_the_worker_hands_the_same_wake_signal_to_both_of_its_tasks(tmp_path):
    """Two instances would each wake nobody, and nothing would say so.

    The wake path is invisible in production until a real business frame
    arrives, so "the stream reader and the reconcile loop hold the same object"
    has to be a property of the wiring rather than something an idle production
    window could confirm.
    """

    from fastapi.testclient import TestClient

    from telegram_kol_research.web_app import create_web_app

    ws_kwargs: dict = {}
    reconcile_kwargs: dict = {}

    async def _ws_runner(**kwargs):
        ws_kwargs.update(kwargs)
        await asyncio.sleep(3600)

    async def _reconcile_runner(**kwargs):
        reconcile_kwargs.update(kwargs)
        await asyncio.sleep(3600)

    class _FakeClient:
        def list_open_orders(self):
            return []

    app = create_web_app(
        database_path=tmp_path / "worker.db",
        runtime_role="worker",
        deepcoin_private_ws_runner=_ws_runner,
        deepcoin_reconcile_runner=_reconcile_runner,
        deepcoin_reconcile_startup_delay_seconds=0,
        deepcoin_client_factory=_FakeClient,
    )

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        health = client.get("/api/runtime/deepcoin-ws-health").json()

    signal = app.state.deepcoin_reconcile_wake_signal
    assert isinstance(signal, DeepcoinReconcileWakeSignal)
    assert ws_kwargs["wake_signal"] is signal
    assert reconcile_kwargs["wake_signal"] is signal
    assert health["wake_signal_installed"] is True
