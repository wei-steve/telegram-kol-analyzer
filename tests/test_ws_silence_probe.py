"""Phase 6-pre-4: silence is not a disconnect, but only one answer proves it.

6-pre-1 measured what the ten-minute silence timer costs: 145 gaps, 1060
seconds, 1.23% of the day, and 134 of those were silence rather than a real
disconnect. Each one holds back a new entry. These tests pin the replacement
and, more importantly, pin what it refuses to claim.
"""

import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_ws_silence_probe import (
    PROBE_CHANGED,
    PROBE_NO_BASELINE,
    PROBE_PASS,
    PROBE_UNREADABLE,
    instruments_to_probe,
    position_facts,
    probe_silence,
    snapshot_fingerprint,
    take_silence_snapshot,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

INST = "ETH-USDT-SWAP"


def _position(pos_id, *, size="2", side="short", sl="2530"):
    # slTriggerPx is present on purpose: the fingerprint must ignore it.
    return {
        "posId": pos_id, "instId": INST, "posSide": side, "pos": size,
        "avgPx": "2500", "slTriggerPx": sl,
    }


def _pending(ord_id, *, size="0", side="short"):
    return {"ordId": ord_id, "instId": INST, "posSide": side, "sz": size}


class Client:
    def __init__(self, *, positions=None, pending=None, open_orders=None,
                 fail=None):
        self._positions = positions if positions is not None else []
        self._pending = pending if pending is not None else {}
        self._open_orders = open_orders
        self._fail = fail
        self.calls = []

    def list_positions(self, *, inst_id=None):
        self.calls.append("positions")
        if self._fail == "positions":
            raise RuntimeError("unreachable")
        return self._positions

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(f"pending:{inst_id}")
        if self._fail == "pending":
            raise RuntimeError("unreachable")
        return self._pending.get(inst_id, [])

    def list_open_orders(self):
        self.calls.append("open_orders")
        if self._fail == "open_orders":
            raise RuntimeError("unreachable")
        return self._open_orders or []


# --- the fingerprint: what counts as a change ----------------------------


def test_the_fingerprint_ignores_the_field_that_lies_about_stops():
    """slTriggerPx reflects only the latest TPSL pair (ARCHITECTURE section 6).

    It has twice been read as "this position has no stop" while two stop
    orders were resting on it. A field that lies must not be able to make the
    probe reconnect, nor to hide a real change.
    """

    a = snapshot_fingerprint(
        positions=[_position("p1", sl="2530")], pending_by_instrument={}
    )
    b = snapshot_fingerprint(
        positions=[_position("p1", sl="9999")], pending_by_instrument={}
    )

    assert a == b


def test_size_side_and_id_all_count_as_changes():
    base = snapshot_fingerprint(
        positions=[_position("p1", size="2")], pending_by_instrument={}
    )
    for changed in (
        _position("p1", size="3"),
        _position("p1", side="long"),
        _position("p2", size="2"),
    ):
        assert snapshot_fingerprint(
            positions=[changed], pending_by_instrument={}
        ) != base


def test_ordering_does_not_count_as_a_change():
    """The venue may return rows in any order; that is not an event."""

    one = snapshot_fingerprint(
        positions=[_position("p1"), _position("p2")], pending_by_instrument={}
    )
    two = snapshot_fingerprint(
        positions=[_position("p2"), _position("p1")], pending_by_instrument={}
    )
    assert one == two


def test_a_partial_snapshot_is_never_a_fingerprint():
    """Comparing a partial read against a whole one would read as a change."""

    assert snapshot_fingerprint(positions=None, pending_by_instrument={}) is None
    assert snapshot_fingerprint(
        positions=[], pending_by_instrument={INST: None}
    ) is None
    assert position_facts("not a list") is None


# --- the decision --------------------------------------------------------


def _factory(tmp_path, name="probe.db", *, legs=()):
    session_factory = create_session_factory(tmp_path / name)
    if legs:
        with session_factory() as session:
            binding = ExecutionBinding(
                strategy_instance_id="s", kol_id="k", chat_id=-1, message_id=1,
                symbol="ETH", side="short", venue="deepcoin",
                margin_mode="cross", position_mode="split", status="open",
            )
            session.add(binding)
            session.flush()
            for index, (kind, status) in enumerate(legs, start=1):
                session.add(
                    ExecutionOrderLeg(
                        execution_binding_id=binding.id,
                        strategy_instance_id="s", leg_index=index,
                        purpose="entry", order_kind=kind, venue="deepcoin",
                        status=status,
                        request_json=json.dumps({"instId": INST}),
                    )
                )
            session.commit()
    return session_factory


def test_nothing_changed_keeps_the_connection(tmp_path):
    session_factory = _factory(tmp_path, legs=[("trigger_limit", "pending")])
    client = Client(positions=[_position("p1")], pending={INST: [_pending("o1")]})
    baseline, _ = take_silence_snapshot(
        client, instruments=[INST], include_open_orders=False
    )

    result = probe_silence(client, session_factory, baseline_fingerprint=baseline)

    assert result.status == PROBE_PASS
    assert result.missed_nothing is True


def test_a_change_during_silence_reconnects(tmp_path):
    session_factory = _factory(tmp_path, legs=[("trigger_limit", "pending")])
    client = Client(positions=[_position("p1", size="2")], pending={INST: []})
    baseline, _ = take_silence_snapshot(
        client, instruments=[INST], include_open_orders=False
    )
    # The position was partially closed and no frame told us.
    client._positions = [_position("p1", size="1")]

    result = probe_silence(client, session_factory, baseline_fingerprint=baseline)

    assert result.status == PROBE_CHANGED
    assert result.missed_nothing is False


@pytest.mark.parametrize("failure", ["positions", "pending"])
def test_an_unreadable_exchange_reconnects(tmp_path, failure):
    """Rule 4: unreadable is unknown, never "nothing happened"."""

    session_factory = _factory(tmp_path, legs=[("trigger_limit", "pending")])
    client = Client(fail=failure, pending={INST: []})

    result = probe_silence(
        client, session_factory, baseline_fingerprint="whatever"
    )

    assert result.status == PROBE_UNREADABLE
    assert result.missed_nothing is False


def test_no_baseline_reconnects(tmp_path):
    """Without a known-good picture there is nothing to compare against."""

    session_factory = _factory(tmp_path)
    client = Client(positions=[], pending={})

    result = probe_silence(client, session_factory, baseline_fingerprint=None)

    assert result.status == PROBE_NO_BASELINE
    assert result.missed_nothing is False


# --- the read budget -----------------------------------------------------


def test_a_ledger_without_ordinary_limit_legs_spends_no_v2_read(tmp_path):
    """Phase 5a: V2 orders-pending only matters for migrated ordinary legs."""

    session_factory = _factory(tmp_path, legs=[("trigger_limit", "pending")])

    instruments, needs_open_orders = instruments_to_probe(session_factory)

    assert instruments == [INST]
    assert needs_open_orders is False


def test_a_live_ordinary_limit_leg_adds_exactly_one_read(tmp_path):
    session_factory = _factory(
        tmp_path, name="limit.db", legs=[("limit", "submitted")]
    )

    instruments, needs_open_orders = instruments_to_probe(session_factory)
    client = Client(positions=[], pending={INST: []}, open_orders=[])
    take_silence_snapshot(
        client, instruments=instruments, include_open_orders=needs_open_orders
    )

    assert needs_open_orders is True
    assert client.calls.count("open_orders") == 1
    assert len(client.calls) <= 3


def test_terminal_legs_do_not_widen_the_probe(tmp_path):
    """A cancelled leg is not a reason to keep reading its instrument."""

    session_factory = _factory(
        tmp_path, name="terminal.db",
        legs=[("trigger_limit", "cancelled"), ("limit", "cancelled")],
    )

    instruments, needs_open_orders = instruments_to_probe(session_factory)

    assert instruments == []
    assert needs_open_orders is False


# --- the read loop: what the probe's answer actually does -----------------


def _inbox(tmp_path, *, probe_result, name="loop.db"):
    """An inbox whose probe is stubbed, so the loop's branch is what is tested."""

    import asyncio

    from telegram_kol_research.deepcoin_private_ws import DeepcoinPrivateWsInbox

    session_factory = create_session_factory(tmp_path / name)
    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=lambda: Client(),
        silence_timeout_seconds=0.01,
    )

    async def _stub():
        return probe_result

    inbox._probe_silence = _stub
    return inbox


class _SilentSocket:
    """Never yields a frame, so the read loop always hits its silence timeout."""

    def __init__(self):
        self.recv_calls = 0

    async def recv(self):
        import asyncio

        self.recv_calls += 1
        await asyncio.sleep(3600)


def test_a_passing_probe_keeps_reading_instead_of_reconnecting(tmp_path):
    """The whole point: no gap row, no resync, the socket stays up."""

    import asyncio

    from telegram_kol_research.deepcoin_ws_silence_probe import SilenceProbeResult

    inbox = _inbox(
        tmp_path,
        probe_result=SilenceProbeResult(PROBE_PASS, fingerprint="f1"),
    )
    socket = _SilentSocket()

    async def scenario():
        task = asyncio.create_task(inbox._read_loop(socket))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    # It went round the loop more than once, which only happens when the probe
    # said the silence cost us nothing.
    assert socket.recv_calls > 1
    assert inbox.silence_probe_passes > 0
    assert inbox.silence_probe_reconnects == 0
    # A passing probe becomes the new baseline.
    assert inbox.silence_baseline_fingerprint == "f1"


@pytest.mark.parametrize(
    "status", [PROBE_CHANGED, PROBE_UNREADABLE, PROBE_NO_BASELINE]
)
def test_every_answer_but_pass_reconnects(tmp_path, status):
    """Changed, unreadable and no-baseline all fall back to the old behaviour."""

    import asyncio

    from telegram_kol_research.deepcoin_private_ws import DeepcoinWsSilenceTimeout
    from telegram_kol_research.deepcoin_ws_silence_probe import SilenceProbeResult

    inbox = _inbox(
        tmp_path,
        probe_result=SilenceProbeResult(status, reason=status),
        name=f"loop-{status}.db",
    )

    async def scenario():
        with pytest.raises(DeepcoinWsSilenceTimeout):
            await inbox._read_loop(_SilentSocket())

    asyncio.run(scenario())

    assert inbox.silence_probe_reconnects == 1
    assert inbox.silence_probe_passes == 0


def test_a_probe_that_raises_is_an_unreadable_answer_not_a_crash(tmp_path):
    """The probe must not become a new way for the worker to die."""

    import asyncio

    from telegram_kol_research.deepcoin_private_ws import DeepcoinPrivateWsInbox

    session_factory = create_session_factory(tmp_path / "probe-raises.db")

    def _exploding_client():
        raise RuntimeError("client construction failed")

    inbox = DeepcoinPrivateWsInbox(
        session_factory=session_factory,
        deepcoin_client_factory=_exploding_client,
        silence_timeout_seconds=0.01,
    )

    result = asyncio.run(inbox._probe_silence())

    assert result.status == PROBE_UNREADABLE
    assert result.missed_nothing is False


# --- the stale baseline: a difference we were told about is not a miss ----


def test_a_frame_since_the_baseline_refreshes_instead_of_reconnecting(tmp_path):
    """The common case, and the one that would have cancelled the phase's gain.

    Activity, then silence. The frames told us what changed, so comparing the
    post-silence snapshot against a pre-activity baseline reports a difference
    we did not miss -- and would reconnect every single time there had been
    trading, which is precisely when silence follows.
    """

    from telegram_kol_research.deepcoin_ws_silence_probe import PROBE_REFRESHED

    session_factory = _factory(tmp_path, name="stale.db",
                               legs=[("trigger_limit", "pending")])
    client = Client(positions=[_position("p1", size="2")], pending={INST: []})
    baseline, _ = take_silence_snapshot(
        client, instruments=[INST], include_open_orders=False
    )
    # A frame arrived and told us the position grew.
    client._positions = [_position("p1", size="5")]

    result = probe_silence(
        client, session_factory,
        baseline_fingerprint=baseline, baseline_stale=True,
    )

    assert result.status == PROBE_REFRESHED
    assert result.missed_nothing is True
    # The refreshed fingerprint describes the world as it is now.
    assert result.fingerprint != baseline


def test_a_stale_baseline_does_not_excuse_an_unreadable_exchange(tmp_path):
    """Refreshing is still a read. If the read fails, rule 4 wins."""

    session_factory = _factory(tmp_path, name="stale-unreadable.db",
                               legs=[("trigger_limit", "pending")])
    client = Client(fail="positions")

    result = probe_silence(
        client, session_factory,
        baseline_fingerprint="old", baseline_stale=True,
    )

    assert result.status == PROBE_UNREADABLE
    assert result.missed_nothing is False


def test_the_frame_marks_the_baseline_stale_and_a_pass_clears_it(tmp_path):
    """The flag has to be set by frames and cleared by an affirmative probe."""

    import asyncio

    from telegram_kol_research.deepcoin_ws_silence_probe import (
        PROBE_REFRESHED,
        SilenceProbeResult,
    )

    inbox = _inbox(
        tmp_path,
        probe_result=SilenceProbeResult(PROBE_REFRESHED, fingerprint="f2"),
        name="stale-flag.db",
    )
    inbox.silence_baseline_stale = True

    inbox._record_probe_outcome(
        SilenceProbeResult(PROBE_REFRESHED, fingerprint="f2")
    )

    assert inbox.silence_baseline_stale is False
    assert inbox.silence_baseline_fingerprint == "f2"
    assert inbox.silence_probe_refreshes == 1
    assert inbox.silence_probe_reconnects == 0
