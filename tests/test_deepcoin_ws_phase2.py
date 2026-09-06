"""Phase 2 guards: de-duplication, ordering, the state machine and the REST resync.

The frames in ``tests/fixtures/deepcoin_ws_recorded_frames.jsonl`` are the real
ones captured on 2026-09-05 by
``scripts/deepcoin_rest_ws_tpsl_experiment.py`` (evidence directory
``eth-rest-ws-tpsl-short-no-clordid-test-20260905/live-ab734b3900f6``). They are
used verbatim: the ``TriggerOrder.TU`` flip from ``default`` to the real split
``posId`` is the single behaviour phase 2 is most likely to get wrong, and a
hand-written frame would not prove anything about it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_private_ws import (
    PROCESSED_STATE_DUPLICATE,
    PROCESSED_STATE_PROCESSED,
    PROCESSED_STATE_UNPROCESSED,
    DeepcoinPrivateWsInbox,
    DeepcoinWsResyncNotConverged,
    build_deepcoin_ws_health,
    decode_ws_frame,
    load_unprocessed_events,
    persist_ws_frame_rows,
)
from telegram_kol_research.deepcoin_ws_resync import (
    DeepcoinInstrumentIdMap,
    DeepcoinWsResyncCoordinator,
    RestSnapshot,
)
from telegram_kol_research.deepcoin_ws_stream_state import (
    WS_STATE_CONNECTING,
    WS_STATE_DISCONNECTED,
    WS_STATE_HEALTHY,
    WS_STATE_RESYNCING,
    WS_STATE_TRANSITIONS,
    WS_STATES,
    DeepcoinWsStateTransitionError,
    DeepcoinWsStreamStateMachine,
    WsEntityKey,
    WsEntityStateTracker,
    compute_backoff_delay,
    entity_key_for_row,
    ws_observation_permits_new_entry,
)
from telegram_kol_research.models import DeepcoinWsEvent


NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
NOW_MS = 1788696000000

FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "deepcoin_ws_recorded_frames.jsonl"
)

TRIGGER_ORDER_SYS_ID = "1001125145471183"
ENTRY_ORDER_SYS_ID = "1001125145471184"
SPLIT_POSITION_ID = "1001125145471184"


def recorded_frames() -> list[dict]:
    """The seven real frames, in the order the exchange delivered them."""

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
                "action": record["payload"]["action"],
            }
        )
    return frames


def _decoded(frame: dict) -> list[dict]:
    return decode_ws_frame(
        frame["raw"],
        received_at=NOW,
        received_ms=frame["received_ms"],
    )


def _rows(session_factory):
    with session_factory() as session:
        return list(
            session.execute(select(DeepcoinWsEvent).order_by(DeepcoinWsEvent.id))
            .scalars()
            .all()
        )


def _machine() -> DeepcoinWsStreamStateMachine:
    return DeepcoinWsStreamStateMachine(
        now_provider=lambda: NOW, monotonic_ms_provider=lambda: NOW_MS
    )


# --------------------------------------------------------------------------
# The recorded sample itself
# --------------------------------------------------------------------------


def test_recorded_sample_contains_the_one_way_trade_unit_flip():
    """Guard the fixture: without the flip the ordering tests prove nothing."""

    frames = recorded_frames()
    assert len(frames) == 7
    trigger_units = [
        row["trade_unit_id"]
        for frame in frames
        for row in _decoded(frame)
        if row["channel"] == "TriggerOrder"
    ]
    assert trigger_units == ["default", SPLIT_POSITION_ID]


# --------------------------------------------------------------------------
# Task 3 -- de-duplication
# --------------------------------------------------------------------------


def test_replayed_frame_is_marked_duplicate_and_never_deleted(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frame = recorded_frames()[1]

    first = persist_ws_frame_rows(
        session_factory, frame["raw"], received_at=NOW, received_ms=NOW_MS
    )
    second = persist_ws_frame_rows(
        session_factory, frame["raw"], received_at=NOW, received_ms=NOW_MS + 12
    )

    assert [row["processed_state"] for row in first] == [PROCESSED_STATE_UNPROCESSED]
    assert [row["processed_state"] for row in second] == [PROCESSED_STATE_DUPLICATE]
    rows = _rows(session_factory)
    assert len(rows) == 2, "a repeat is marked, never removed"
    assert rows[0].payload_hash == rows[1].payload_hash
    assert rows[1].raw_payload == frame["raw"]


def test_de_duplication_is_idempotent_across_repeated_delivery(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frame = recorded_frames()[1]

    states = []
    for offset in range(4):
        rows = persist_ws_frame_rows(
            session_factory,
            frame["raw"],
            received_at=NOW,
            received_ms=NOW_MS + offset,
        )
        states.append(rows[0]["processed_state"])

    assert states == [
        PROCESSED_STATE_UNPROCESSED,
        PROCESSED_STATE_DUPLICATE,
        PROCESSED_STATE_DUPLICATE,
        PROCESSED_STATE_DUPLICATE,
    ]
    assert len(_rows(session_factory)) == 4


def test_distinct_frames_are_never_confused_for_repeats(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")

    for frame in recorded_frames():
        persist_ws_frame_rows(
            session_factory,
            frame["raw"],
            received_at=NOW,
            received_ms=frame["received_ms"],
        )

    rows = _rows(session_factory)
    assert len(rows) == 7
    assert {row.processed_state for row in rows} == {PROCESSED_STATE_UNPROCESSED}


def test_duplicate_rate_is_reported_on_the_health_endpoint(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frame = recorded_frames()[1]
    now_ms = int(NOW.timestamp() * 1000)
    for offset in range(4):
        persist_ws_frame_rows(
            session_factory,
            frame["raw"],
            received_at=NOW,
            received_ms=now_ms - 1000 + offset,
        )

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=None, now=NOW
    )

    assert health["events_last_hour"] == 4
    assert health["duplicates_last_hour"] == 3
    assert health["duplicate_rate_1h"] == pytest.approx(0.75)
    assert health["counts_by_processed_state"][PROCESSED_STATE_DUPLICATE] == 3
    assert "ETHUSDT" not in json.dumps(health)


# --------------------------------------------------------------------------
# Task 4 -- ordering
# --------------------------------------------------------------------------


def test_entity_identity_per_channel_matches_the_documented_short_keys():
    frames = recorded_frames()
    by_channel = {}
    for frame in frames:
        for row in _decoded(frame):
            by_channel.setdefault(row["channel"], []).append(row)

    position_key = entity_key_for_row(by_channel["Position"][0])
    order_key = entity_key_for_row(by_channel["Order"][0])
    trigger_key = entity_key_for_row(by_channel["TriggerOrder"][0])
    trade_key = entity_key_for_row(by_channel["Trade"][0])

    assert position_key == WsEntityKey("Position", SPLIT_POSITION_ID)
    assert order_key == WsEntityKey("Order", ENTRY_ORDER_SYS_ID)
    assert trigger_key == WsEntityKey("TriggerOrder", TRIGGER_ORDER_SYS_ID)
    # A fill is append-only, so its identity carries a per-frame component and
    # one fill can never overwrite another.
    assert trade_key.channel == "Trade"
    assert trade_key.identity.startswith(f"{ENTRY_ORDER_SYS_ID}:")


def test_reversed_delivery_never_rolls_state_backwards():
    """Deliver the recorded frames back to front. Newest state must survive."""

    tracker = WsEntityStateTracker()
    frames = list(reversed(recorded_frames()))
    for frame in frames:
        for row in _decoded(frame):
            tracker.apply(row)

    trigger = tracker.state_for(WsEntityKey("TriggerOrder", TRIGGER_ORDER_SYS_ID))
    order = tracker.state_for(WsEntityKey("Order", ENTRY_ORDER_SYS_ID))
    position = tracker.state_for(WsEntityKey("Position", SPLIT_POSITION_ID))

    assert trigger.trade_unit_id == SPLIT_POSITION_ID
    assert trigger.exchange_time_ms == 1788636239
    assert order.exchange_time_ms == 1788635962122
    assert position.exchange_time_ms == 1788636239
    assert tracker.out_of_order_count > 0


def test_three_frames_delivered_out_of_order_are_counted_and_ignored():
    tracker = WsEntityStateTracker()
    frames = recorded_frames()
    trigger_frames = [f for f in frames if f["action"] == "PushTriggerOrder"]
    position_frames = [f for f in frames if f["action"] == "PushPosition"]

    ordered = [
        _decoded(position_frames[1])[0],
        _decoded(trigger_frames[1])[0],
    ]
    for row in ordered:
        tracker.apply(row)
    assert tracker.out_of_order_count == 0

    stale = [
        _decoded(position_frames[0])[0],
        _decoded(trigger_frames[0])[0],
        _decoded(position_frames[0])[0],
    ]
    results = [tracker.apply(row) for row in stale]

    assert [result.out_of_order for result in results] == [True, True, True]
    assert [result.applied for result in results] == [False, False, False]
    assert tracker.out_of_order_count == 3
    assert (
        tracker.state_for(WsEntityKey("Position", SPLIT_POSITION_ID)).exchange_time_ms
        == 1788636239
    )


def test_a_late_default_trade_unit_never_restores_default():
    """The single easiest thing in phase 2 to get wrong.

    ``TU`` moves ``default -> <posId>`` once. A late frame still carrying
    ``default`` -- including one whose timestamps look newer, since the exchange
    does not write the fields atomically -- must not put ``default`` back.
    """

    tracker = WsEntityStateTracker()
    frames = recorded_frames()
    trigger_frames = [f for f in frames if f["action"] == "PushTriggerOrder"]
    key = WsEntityKey("TriggerOrder", TRIGGER_ORDER_SYS_ID)

    tracker.apply(_decoded(trigger_frames[0])[0])
    assert tracker.state_for(key).trade_unit_id == "default"

    tracker.apply(_decoded(trigger_frames[1])[0])
    assert tracker.state_for(key).trade_unit_id == SPLIT_POSITION_ID

    # 1. the genuine stale frame arriving late
    tracker.apply(_decoded(trigger_frames[0])[0])
    assert tracker.state_for(key).trade_unit_id == SPLIT_POSITION_ID

    # 2. a stale ``default`` wearing a newer timestamp
    forged = dict(_decoded(trigger_frames[0])[0])
    forged["exchange_time_ms"] = 9_999_999_999
    forged["received_ms"] = NOW_MS + 5_000
    tracker.apply(forged)
    assert tracker.state_for(key).trade_unit_id == SPLIT_POSITION_ID


def test_cross_channel_arrival_order_is_never_assumed():
    """Trade before Order, Position before either: all must be accepted."""

    tracker = WsEntityStateTracker()
    frames = recorded_frames()
    trade = _decoded([f for f in frames if f["action"] == "PushTrade"][0])[0]
    order = _decoded([f for f in frames if f["action"] == "PushOrder"][0])[0]

    trade_result = tracker.apply(trade)
    order_result = tracker.apply(order)

    assert trade_result.applied is True
    assert order_result.applied is True
    assert tracker.entity_count() == 2


def test_repeated_fill_frames_accumulate_rather_than_overwrite():
    tracker = WsEntityStateTracker()
    frames = recorded_frames()
    trade_row = _decoded([f for f in frames if f["action"] == "PushTrade"][0])[0]
    other = dict(trade_row)
    other["payload_hash"] = "b" * 64
    other["received_ms"] = trade_row["received_ms"] + 1

    tracker.apply(trade_row)
    tracker.apply(other)

    assert tracker.entity_count() == 2
    assert tracker.out_of_order_count == 0


# --------------------------------------------------------------------------
# Task 1 -- the state machine
# --------------------------------------------------------------------------


def test_the_state_set_is_exactly_the_four_chain_states():
    assert set(WS_STATES) == {
        WS_STATE_CONNECTING,
        WS_STATE_HEALTHY,
        WS_STATE_DISCONNECTED,
        WS_STATE_RESYNCING,
    }
    assert set(WS_STATE_TRANSITIONS) == set(WS_STATES)


def test_every_legal_transition_of_the_chain_is_walkable():
    machine = _machine()
    assert machine.state == WS_STATE_CONNECTING

    machine.transition(WS_STATE_RESYNCING, reason="resync")
    machine.transition(WS_STATE_HEALTHY, reason="converged")
    machine.transition(WS_STATE_DISCONNECTED, reason="dropped")
    machine.transition(WS_STATE_CONNECTING, reason="retry")
    machine.transition(WS_STATE_DISCONNECTED, reason="dial_failed")
    machine.transition(WS_STATE_CONNECTING, reason="retry")
    machine.transition(WS_STATE_RESYNCING, reason="resync")
    machine.transition(WS_STATE_DISCONNECTED, reason="resync_failed")

    assert machine.reconnect_count == 2
    assert machine.state == WS_STATE_DISCONNECTED


def test_healthy_is_reachable_only_through_resyncing():
    """A restart loses frames like a drop does: rule 12 forbids the shortcut."""

    machine = _machine()
    with pytest.raises(DeepcoinWsStateTransitionError):
        machine.transition(WS_STATE_HEALTHY, reason="skip_resync")

    machine.transition(WS_STATE_RESYNCING, reason="resync")
    machine.transition(WS_STATE_HEALTHY, reason="converged")
    machine.transition(WS_STATE_DISCONNECTED, reason="dropped")
    with pytest.raises(DeepcoinWsStateTransitionError):
        machine.transition(WS_STATE_HEALTHY, reason="skip_resync")


@pytest.mark.parametrize("state", list(WS_STATES))
def test_no_state_may_be_invented(state):
    machine = _machine()
    machine.state = state
    with pytest.raises(DeepcoinWsStateTransitionError):
        machine.transition("degraded", reason="invented")


# --------------------------------------------------------------------------
# Task 6 -- the entry permission hook
# --------------------------------------------------------------------------


def test_permits_new_entry_defaults_to_false_everywhere_but_converged_healthy():
    machine = _machine()

    assert ws_observation_permits_new_entry(None, open_gap_count=0) == (
        False,
        "unavailable",
    )
    assert machine.permits_new_entry(open_gap_count=0) == (False, WS_STATE_CONNECTING)
    assert machine.permits_new_entry(open_gap_count=None) == (
        False,
        "gap_state_unknown",
    )

    machine.transition(WS_STATE_RESYNCING, reason="resync")
    assert machine.permits_new_entry(open_gap_count=0) == (False, WS_STATE_RESYNCING)

    machine.transition(WS_STATE_HEALTHY, reason="converged")
    # Healthy but the resync was never recorded as converged.
    assert machine.permits_new_entry(open_gap_count=0) == (False, "no_converged_resync")

    machine.last_resync_outcome = "converged"
    assert machine.permits_new_entry(open_gap_count=1) == (False, "open_gap")
    assert machine.permits_new_entry(open_gap_count=0) == (True, "")


def test_permission_hook_is_not_wired_into_any_entry_path():
    """Phase 2 only exposes it. Wiring it in is phase 5."""

    package = pathlib.Path(
        __import__("telegram_kol_research").__file__
    ).parent
    callers = sorted(
        path.name
        for path in package.rglob("*.py")
        if "permits_new_entry" in path.read_text(encoding="utf-8")
    )
    assert callers == [
        "deepcoin_private_ws.py",
        "deepcoin_ws_stream_state.py",
    ]


# --------------------------------------------------------------------------
# Task 2 -- backoff
# --------------------------------------------------------------------------


def test_backoff_is_exponential_capped_and_jittered():
    no_jitter = [
        compute_backoff_delay(attempt, rng=lambda: 0.5) for attempt in range(9)
    ]
    assert no_jitter[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert no_jitter[8] == 60.0

    low = compute_backoff_delay(3, rng=lambda: 0.0)
    high = compute_backoff_delay(3, rng=lambda: 0.999)
    assert low < 8.0 < high, "jitter must spread the delay in both directions"
    assert compute_backoff_delay(100, rng=lambda: 0.999) <= 60.0
    assert compute_backoff_delay(-5, rng=lambda: 0.0) >= 0.0


# --------------------------------------------------------------------------
# Task 5 -- contract-name mapping
# --------------------------------------------------------------------------


def test_instrument_map_resolves_both_directions_from_the_product_list():
    instrument_map = DeepcoinInstrumentIdMap()
    instrument_map.build(
        [{"instId": "ETH-USDT-SWAP"}, {"instId": "BTC-USDT-SWAP"}, {"junk": 1}]
    )

    assert instrument_map.rest_id_for_stream_name("ETHUSDT") == "ETH-USDT-SWAP"
    assert instrument_map.stream_name_for_rest_id("BTC-USDT-SWAP") == "BTCUSDT"
    assert instrument_map.size == 2


def test_unknown_contract_name_fails_closed_rather_than_being_pieced_together():
    instrument_map = DeepcoinInstrumentIdMap()
    instrument_map.build([{"instId": "ETH-USDT-SWAP"}])

    assert instrument_map.rest_id_for_stream_name("SOLUSDT") is None
    assert instrument_map.rest_id_for_stream_name("") is None
    assert instrument_map.rest_id_for_stream_name(None) is None


def test_colliding_contract_names_are_dropped_from_both_directions():
    instrument_map = DeepcoinInstrumentIdMap()
    instrument_map.build([{"instId": "ETH-USDT-SWAP"}, {"instId": "ETH-USDT"}])

    assert instrument_map.rest_id_for_stream_name("ETHUSDT") is None
    assert instrument_map.collision_count == 1


def test_an_unresolvable_instrument_blocks_convergence():
    coordinator = _coordinator(_RestStub())
    coordinator.instrument_map.build([{"instId": "BTC-USDT-SWAP"}])
    coordinator.instrument_map.mark_built(now_ms=NOW_MS)

    snapshot = coordinator.rest_snapshot(stream_instruments=["ETHUSDT"])

    assert snapshot.complete is False
    assert snapshot.unresolved_instruments == ("ETHUSDT",)


# --------------------------------------------------------------------------
# Task 5 -- the five-step resync
# --------------------------------------------------------------------------


class _RestStub:
    """Read-only Deepcoin stand-in that records the exact call order.

    It exposes no write method at all, so a resync that tried to write would
    raise ``AttributeError`` rather than reaching an exchange.
    """

    def __init__(self, *, positions=None, fail=(), instruments=None):
        self.calls: list[str] = []
        self._positions = list(positions or [])
        self._fail = set(fail)
        self._instruments = (
            [{"instId": "ETH-USDT-SWAP"}] if instruments is None else list(instruments)
        )
        self.snapshot_round = 0

    def _maybe_fail(self, label):
        if label in self._fail:
            raise RuntimeError(f"{label} unavailable")

    def list_swap_instruments(self):
        self.calls.append("instruments")
        self._maybe_fail("instruments")
        return list(self._instruments)

    def list_positions(self, **kwargs):
        self.calls.append("positions")
        self._maybe_fail("positions")
        return list(self._positions)

    def list_open_orders(self, **kwargs):
        self.calls.append("open_orders")
        self._maybe_fail("open_orders")
        return []

    def list_trade_fills(self, **kwargs):
        self.calls.append("fills")
        self._maybe_fail("fills")
        return []

    def list_trigger_orders_pending(self, **kwargs):
        self.calls.append("trigger_orders")
        self._maybe_fail("trigger_orders")
        return []

    def close(self):
        self.calls.append("close")


def _coordinator(stub):
    return DeepcoinWsResyncCoordinator(
        client_factory=lambda: stub,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
    )


def test_resync_runs_all_five_steps_with_the_second_snapshot_after_subscribe():
    stub = _RestStub()
    coordinator = _coordinator(stub)
    tracker = WsEntityStateTracker()
    order: list[str] = []

    def _replay(_tracker, _limit):
        order.append("replay")
        return 3

    def _subscribe():
        order.append("subscribe")

    def _snapshot_marker():
        order.append("snapshot")

    original = coordinator.rest_snapshot

    def _traced(**kwargs):
        _snapshot_marker()
        return original(**kwargs)

    coordinator.rest_snapshot = _traced

    outcome = coordinator.run(
        tracker=tracker, replay_unprocessed=_replay, subscribe=_subscribe
    )

    assert order == ["snapshot", "replay", "subscribe", "snapshot"], (
        "step 4 must run after the subscription, or the race window is only hidden"
    )
    assert outcome.converged is True
    assert outcome.reason == "converged"
    assert outcome.replayed_events == 3
    assert set(outcome.step_durations_ms) == {
        "step1_rest_snapshot",
        "step2_replay_events",
        "step3_subscribe",
        "step4_rest_snapshot",
        "step5_compare",
    }


def test_resync_uses_only_read_methods():
    stub = _RestStub()
    coordinator = _coordinator(stub)

    coordinator.run(
        tracker=WsEntityStateTracker(),
        replay_unprocessed=lambda _t, _l: 0,
        subscribe=lambda: None,
    )

    assert {call for call in stub.calls} <= {
        "instruments",
        "positions",
        "open_orders",
        "fills",
        "trigger_orders",
        "close",
    }
    for forbidden in (
        "place_order",
        "trigger_order",
        "cancel_order",
        "set_position_sltp",
        "cancel_position_sltp",
        "replace_order_sltp",
        "cancel_trigger_order",
    ):
        assert not hasattr(stub, forbidden)


def test_a_failed_rest_read_is_unknown_and_blocks_convergence():
    """Hard rule 4: an incomplete read never becomes "there are no positions"."""

    stub = _RestStub(fail={"positions"})
    coordinator = _coordinator(stub)

    outcome = coordinator.run(
        tracker=WsEntityStateTracker(),
        replay_unprocessed=lambda _t, _l: 0,
        subscribe=lambda: None,
    )

    assert outcome.converged is False
    assert outcome.reason == "incomplete_rest_read"
    assert outcome.incomplete_reads
    assert "positions:RuntimeError" in outcome.incomplete_reads[0]


def test_an_incomplete_snapshot_is_never_read_as_an_empty_account():
    snapshot = RestSnapshot(
        complete=False,
        positions=[],
        open_orders=[],
        trigger_orders=[],
        fills=[],
        incomplete_reads=("positions:RuntimeError",),
    )

    assert snapshot.complete is False
    assert snapshot.object_count() == 0
    # The count being zero is exactly why callers must branch on ``complete``
    # first; the resync does, and returns "not converged".
    assert bool(snapshot.incomplete_reads) is True


def test_a_failed_subscribe_stops_the_sequence_before_the_second_snapshot():
    stub = _RestStub()
    coordinator = _coordinator(stub)

    def _subscribe():
        raise TimeoutError("subscribe timed out")

    outcome = coordinator.run(
        tracker=WsEntityStateTracker(),
        replay_unprocessed=lambda _t, _l: 0,
        subscribe=_subscribe,
    )

    assert outcome.converged is False
    assert outcome.reason == "subscribe_failed:TimeoutError"
    assert "step4_rest_snapshot" not in outcome.step_durations_ms


def test_forward_only_comparison_flags_a_regression_and_accepts_completion():
    before = RestSnapshot(
        complete=True,
        positions=[{"posId": "P1", "uTime": "200"}],
        open_orders=[{"ordId": "O1", "uTime": "200"}],
        trigger_orders=[],
        fills=[],
    )
    forward = RestSnapshot(
        complete=True,
        positions=[{"posId": "P1", "uTime": "300"}],
        open_orders=[],  # filled or cancelled between the snapshots
        trigger_orders=[],
        fills=[],
    )
    backward = RestSnapshot(
        complete=True,
        positions=[{"posId": "P1", "uTime": "100"}],
        open_orders=[{"ordId": "O1", "uTime": "200"}],
        trigger_orders=[],
        fills=[],
    )
    tracker = WsEntityStateTracker()

    advanced, reasons = DeepcoinWsResyncCoordinator.compare_forward_only(
        before, forward, tracker
    )
    assert reasons == []
    assert advanced == 2

    _, regression_reasons = DeepcoinWsResyncCoordinator.compare_forward_only(
        before, backward, tracker
    )
    assert regression_reasons == ["rest_time_regression:position"]


# --------------------------------------------------------------------------
# Replay of persisted events (resync step 2)
# --------------------------------------------------------------------------


def test_replay_folds_unprocessed_rows_in_and_skips_duplicates(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frames = recorded_frames()
    for frame in frames:
        persist_ws_frame_rows(
            session_factory,
            frame["raw"],
            received_at=NOW,
            received_ms=frame["received_ms"],
        )
    # Re-deliver the whole capture: every row is now a marked duplicate.
    for frame in frames:
        persist_ws_frame_rows(
            session_factory,
            frame["raw"],
            received_at=NOW,
            received_ms=frame["received_ms"] + 1,
        )

    pending = load_unprocessed_events(session_factory, limit=100)
    assert len(pending) == 7, "duplicates must not be replayed a second time"

    inbox = _offline_inbox(session_factory)
    tracker = WsEntityStateTracker()
    replayed = inbox._replay_unprocessed(tracker, 100)

    assert replayed == 7
    assert load_unprocessed_events(session_factory, limit=100) == []
    with session_factory() as session:
        states = [
            row.processed_state
            for row in session.execute(
                select(DeepcoinWsEvent).order_by(DeepcoinWsEvent.id)
            ).scalars()
        ]
    assert states.count(PROCESSED_STATE_PROCESSED) == 7
    assert states.count(PROCESSED_STATE_DUPLICATE) == 7
    assert (
        tracker.state_for(
            WsEntityKey("TriggerOrder", TRIGGER_ORDER_SYS_ID)
        ).trade_unit_id
        == SPLIT_POSITION_ID
    )


# --------------------------------------------------------------------------
# Supplementary check 4 -- disconnect at three points in a fill
# --------------------------------------------------------------------------


class _ScriptedConnection:
    def __init__(self, frames, *, drop_after=None):
        self._frames = list(frames)
        self._drop_after = drop_after
        self.sent: list[str] = []
        self.delivered = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self._drop_after is not None and self.delivered >= self._drop_after:
            raise ConnectionResetError("link dropped")
        if not self._frames:
            raise ConnectionResetError("link dropped")
        self.delivered += 1
        return self._frames.pop(0)


def _offline_inbox(session_factory, *, connections=None, client=None, **kwargs):
    stub = client or _RestStub()
    queue = list(connections or [])

    def _connect(url, **connect_kwargs):
        return queue.pop(0)

    kwargs.setdefault("rng", lambda: 0.5)
    return DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: _ListenKeyClient(stub),
        connect_factory=_connect,
        now_provider=lambda: NOW,
        monotonic_ms_provider=lambda: NOW_MS,
        **kwargs,
    )


class _ListenKeyClient:
    """The REST stub plus the listen-key method the inbox needs."""

    def __init__(self, stub):
        self._stub = stub

    def __getattr__(self, name):
        return getattr(self._stub, name)

    def acquire_listen_key(self):
        return "SECRET-LISTEN-KEY"


@pytest.mark.parametrize(
    ("label", "drop_after"),
    [("before_fill", 0), ("during_fill", 4), ("after_fill", 7)],
)
def test_a_drop_at_any_point_walks_the_chain_and_never_permits_entry(
    tmp_path, label, drop_after
):
    """Supplementary check 4, offline: drop before, during and after the fill.

    The recorded capture is seven frames; the fill lands on frames 5-7. Dropping
    at 0, 4 and 7 covers the three moments the handoff document names.
    """

    session_factory = create_session_factory(tmp_path / f"ws-{label}.db")
    frames = [frame["raw"] for frame in recorded_frames()]
    first = _ScriptedConnection(frames, drop_after=drop_after)
    second = _ScriptedConnection([], drop_after=0)
    observed: list[tuple[str, bool]] = []

    inbox = _offline_inbox(session_factory, connections=[first, second])

    async def _sleep(_seconds):
        observed.append(
            (inbox.state_machine.state, inbox.permits_new_entry()[0])
        )
        if len(observed) >= 2:
            raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    walked = [transition[1] for transition in inbox.state_machine.transitions]
    assert walked[:3] == [
        WS_STATE_RESYNCING,
        WS_STATE_HEALTHY,
        WS_STATE_DISCONNECTED,
    ]
    assert WS_STATE_CONNECTING in walked[3:]
    assert inbox.state_machine.reconnect_count >= 1
    # Every moment the loop paused, entry stayed forbidden.
    assert [permitted for _state, permitted in observed] == [False, False]
    assert len(_rows(session_factory)) == drop_after


def test_resyncing_never_permits_entry_and_never_closes_the_gap(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    stub = _RestStub(fail={"positions"})
    connection = _ScriptedConnection([])
    inbox = _offline_inbox(session_factory, connections=[connection], client=stub)

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    assert inbox.state_machine.state == WS_STATE_DISCONNECTED
    assert inbox.state_machine.last_resync_outcome == (
        "not_converged:incomplete_rest_read"
    )
    assert inbox.permits_new_entry() == (False, WS_STATE_DISCONNECTED)
    # The startup gap stays open: coverage was never re-established.
    assert inbox.open_gap_id is not None
    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=inbox, now=NOW
    )
    assert health["open_gap_count"] == 1
    assert health["permits_new_entry"] is False
    assert health["state"] == WS_STATE_DISCONNECTED


def test_a_converged_resync_closes_the_gap_and_reports_health(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    frames = [frame["raw"] for frame in recorded_frames()]
    connection = _ScriptedConnection(frames)
    inbox = _offline_inbox(session_factory, connections=[connection, _ScriptedConnection([])])

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    health = build_deepcoin_ws_health(
        session_factory=session_factory, inbox=inbox, now=NOW
    )
    assert inbox.state_machine.last_resync_outcome == "converged"
    assert health["counts_by_processed_state"][PROCESSED_STATE_UNPROCESSED] == 0
    assert health["counts_by_processed_state"][PROCESSED_STATE_PROCESSED] == 7
    assert health["instrument_map_size"] == 1
    assert health["last_resync_step_durations_ms"]
    assert "listenKey" not in json.dumps(health)
    assert "SECRET-LISTEN-KEY" not in json.dumps(health)


# --------------------------------------------------------------------------
# Task 2 -- silence timer and listen-key rotation
# --------------------------------------------------------------------------


class _SilentConnection:
    """Open, subscribable, and permanently quiet. A live pong proves nothing."""

    def __init__(self):
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        await asyncio.Event().wait()


def test_an_open_but_silent_socket_is_treated_as_a_gap(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    inbox = _offline_inbox(
        session_factory,
        connections=[_SilentConnection(), _SilentConnection()],
        silence_timeout_seconds=0.05,
    )

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    from telegram_kol_research.models import DeepcoinWsConnectionGap

    with session_factory() as session:
        gaps = list(
            session.execute(
                select(DeepcoinWsConnectionGap).order_by(DeepcoinWsConnectionGap.id)
            ).scalars()
        )
    assert [gap.reason for gap in gaps] == ["process_start", "silence_timeout"]
    assert gaps[0].reconnected_at is not None, "the resync did converge"
    assert gaps[1].reconnected_at is None, "the silence is still an open gap"
    assert inbox.state_machine.state == WS_STATE_DISCONNECTED
    assert inbox.permits_new_entry()[0] is False


def test_listen_key_rotation_is_a_planned_reconnect_without_backoff_escalation(
    tmp_path,
):
    """Deepcoin publishes no renewal endpoint, so renewal is a fresh connection.

    A planned rotation must not be counted as a failure, or a busy account would
    walk itself up to the sixty-second backoff cap for no reason.
    """

    session_factory = create_session_factory(tmp_path / "ws.db")
    inbox = _offline_inbox(
        session_factory,
        connections=[_SilentConnection(), _SilentConnection()],
        silence_timeout_seconds=30.0,
        listen_key_ttl_seconds=0.05,
    )
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    from telegram_kol_research.models import DeepcoinWsConnectionGap

    with session_factory() as session:
        reasons = [
            gap.reason
            for gap in session.execute(
                select(DeepcoinWsConnectionGap).order_by(DeepcoinWsConnectionGap.id)
            ).scalars()
        ]
    assert reasons == ["process_start", "listen_key_renewal"]
    assert inbox.state_machine.consecutive_failures == 0
    assert slept == [1.0]


def test_unparsed_frames_never_corrupt_the_entity_view(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    tracker = WsEntityStateTracker()

    rows = persist_ws_frame_rows(
        session_factory, "not json at all", received_at=NOW, received_ms=NOW_MS
    )
    for row in rows:
        result = tracker.apply(row)
        assert result.applied is False
        assert result.reason == "unidentified"

    assert tracker.entity_count() == 0
    assert tracker.unidentified_count == 1
    assert len(_rows(session_factory)) == 1, "an unusable frame is still kept"


# --------------------------------------------------------------------------
# The listen-key expiry notice, as the exchange actually sends it
# --------------------------------------------------------------------------

# Captured verbatim from production on 2026-09-06 at 14:56:31Z, exactly sixty
# minutes after that stream subscribed. The key is a hard hour, not a sliding
# window, and the exchange says so before closing the socket.
LISTEN_KEY_EXPIRED_FRAME = (
    '{"code":"50118","event":"error","msg":"listen key expired, connection closing"}'
)


def test_a_control_frame_is_classified_apart_from_an_undecodable_one():
    from telegram_kol_research.deepcoin_private_ws import CONTROL_CHANNEL

    control = decode_ws_frame(
        LISTEN_KEY_EXPIRED_FRAME, received_at=NOW, received_ms=NOW_MS
    )
    garbage = decode_ws_frame("not json", received_at=NOW, received_ms=NOW_MS)

    assert len(control) == 1
    assert control[0]["channel"] == CONTROL_CHANNEL
    assert control[0]["action"] == "error"
    assert control[0]["raw_payload"] == LISTEN_KEY_EXPIRED_FRAME
    assert garbage[0]["channel"] == "unparsed"


def test_the_expiry_notice_is_recognised_by_code_not_by_wording():
    from telegram_kol_research.deepcoin_private_ws import (
        is_listen_key_expiry_notice,
    )

    assert is_listen_key_expiry_notice(LISTEN_KEY_EXPIRED_FRAME) is True
    assert is_listen_key_expiry_notice('{"code":"50118","event":"error"}') is True
    assert (
        is_listen_key_expiry_notice(
            '{"event":"error","msg":"Listen Key Expired, closing"}'
        )
        is True
    )
    assert is_listen_key_expiry_notice('{"event":"error","msg":"rate limited"}') is False
    assert is_listen_key_expiry_notice(recorded_frames()[0]["raw"]) is False
    assert is_listen_key_expiry_notice("not json") is False


def test_the_expiry_notice_is_persisted_before_it_triggers_the_reconnect(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws.db")
    first = _ScriptedConnection([LISTEN_KEY_EXPIRED_FRAME])
    inbox = _offline_inbox(
        session_factory, connections=[first, _ScriptedConnection([])]
    )

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    inbox._sleep = _sleep

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox.run_forever())

    rows = _rows(session_factory)
    assert [row.raw_payload for row in rows] == [LISTEN_KEY_EXPIRED_FRAME], (
        "the notice is evidence and must be kept, not consumed"
    )

    from telegram_kol_research.models import DeepcoinWsConnectionGap

    with session_factory() as session:
        reasons = [
            gap.reason
            for gap in session.execute(
                select(DeepcoinWsConnectionGap).order_by(DeepcoinWsConnectionGap.id)
            ).scalars()
        ]
    assert reasons == ["process_start", "listen_key_renewal"]
    # A planned rotation, so no failure escalation.
    assert inbox.state_machine.consecutive_failures == 0
    assert inbox.state_machine.state == WS_STATE_DISCONNECTED
    assert inbox.permits_new_entry()[0] is False
