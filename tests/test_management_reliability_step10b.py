"""A-10b: a position is not gone because one snapshot did not list it.

pos 1001125178552543 was a live BTC short in an auto_trade group, with both
its stops armed. On 2026-09-08 the manual-close sweep did not see it in one
positions snapshot and wrote it off: binding closed, leg manually_closed,
lifecycle exited. Nothing managed it for the next thirty-four hours and no
alert was ever raised.

The criterion was also inverted. Proof was demanded only when the leg was
*poorly* attributed; a leg that was verified and carried an authoritative
position record went down the branch that needed no proof at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ABSENCE_OBSERVED_ACTION,
    MARKED_CLOSED_ACTION,
    sync_manual_closed_deepcoin_positions,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    StrategyLifecycle,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
NAIVE = NOW.replace(tzinfo=None)
POS = "1001125178552543"
OTHER_POS = "1001125179691393"


class _Client:
    """A venue that lists one unrelated position and answers history as told."""

    def __init__(self, *, positions=None, history=None, history_raises=False):
        self._positions = (
            positions
            if positions is not None
            else [
                {
                    "posId": OTHER_POS,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "pos": "3",
                    "avgPx": "79000",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                }
            ]
        )
        self._history = history or []
        self._history_raises = history_raises

    def list_positions(self, *, inst_id=None):
        return list(self._positions)

    def list_position_history(self, *, inst_id, pos_id=None):
        if self._history_raises:
            raise RuntimeError("history unavailable")
        return list(self._history)

    def list_trigger_orders_pending(self, *, inst_id=None):
        return []

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trade_fills(self, *, inst_id=None):
        return []


def _fixture(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=343,
                venue="deepcoin",
                strategy_instance_id="deepcoin:-1002805019371:1552:BTC:short",
                kol_id=1,
                chat_id=-1002805019371,
                message_id=1552,
                symbol="BTC",
                side="short",
                status="active",
                last_exchange_status="position_ownership_verified",
                pos_id=POS,
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=589,
                execution_binding_id=343,
                leg_index=0,
                purpose="entry",
                order_kind="trigger_limit",
                venue="deepcoin",
                order_id="1001125172997005",
                pos_id=POS,
                # leg 589's exact shape: verified *and* carrying an
                # authoritative position record, which is precisely what used
                # to route it past every proof.
                status="active",
                attribution_status="verified",
                response_json='{"pos_id": "' + POS + '"}',
            )
        )
        session.add(
            StrategyLifecycle(
                id=1109,
                chat_id=-1002805019371,
                message_id=1552,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                execution_binding_id=343,
                signal_at=NAIVE - timedelta(days=2),
                entered_at=NAIVE - timedelta(days=2),
            )
        )
        session.commit()
    return session_factory


def _state(session_factory):
    with session_factory() as session:
        return (
            str(session.get(ExecutionBinding, 343).status),
            str(session.get(ExecutionOrderLeg, 589).status),
            str(session.get(StrategyLifecycle, 1109).lifecycle_status),
        )


def _events(session_factory, action):
    with session_factory() as session:
        return session.query(ExecutionEvent).filter_by(action=action).all()


def test_one_absent_snapshot_changes_nothing_and_is_only_recorded(tmp_path):
    """The exact failure of 2026-09-08, now a no-op plus a note."""

    session_factory = _fixture(tmp_path)

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )

    assert result.pending_absence == 1
    assert result.manually_closed == 0
    assert _state(session_factory) == ("active", "active", "entered")
    observed = _events(session_factory, ABSENCE_OBSERVED_ACTION)
    assert [row.pos_id for row in observed] == [POS]
    assert _events(session_factory, MARKED_CLOSED_ACTION) == []


def test_a_second_absence_a_minute_later_settles_it(tmp_path):
    """Two independent looks, at least a minute apart, are the proof."""

    session_factory = _fixture(tmp_path)

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=NOW + timedelta(seconds=61),
        allow_exchange_mutations=False,
    )

    assert result.manually_closed == 1
    assert _state(session_factory) == ("closed", "manually_closed", "exited")
    marked = _events(session_factory, MARKED_CLOSED_ACTION)
    assert [row.reason for row in marked] == ["two_absent_snapshots"]
    assert [row.pos_id for row in marked] == [POS]


def test_a_second_absence_too_soon_does_not_settle_it(tmp_path):
    """Two looks inside one bad minute at the venue are one look."""

    session_factory = _fixture(tmp_path)

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=NOW + timedelta(seconds=30),
        allow_exchange_mutations=False,
    )

    assert result.manually_closed == 0
    assert _state(session_factory) == ("active", "active", "entered")


def test_venue_history_settles_it_on_the_first_pass(tmp_path):
    """When the venue itself says it closed, one pass is enough.

    The row shape is the one ``position_history_row_proves_full_close``
    actually accepts: the same pos id, the same instrument and side, and an
    opened size that equals the closed size.
    """

    session_factory = _fixture(tmp_path)
    history = [
        {
            "posId": POS,
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "3",
            "closePos": "3",
            "uTime": "1788830587000",
            "cTime": "1788830587000",
        }
    ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(history=history),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )

    assert result.manually_closed == 1
    assert result.pending_absence == 0
    assert _state(session_factory) == ("closed", "manually_closed", "exited")
    marked = _events(session_factory, MARKED_CLOSED_ACTION)
    assert [row.reason for row in marked] == ["position_history_full_close"]
    # One pass, so no absence observation was needed at all.
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []


def test_a_history_row_for_a_partial_close_is_not_proof(tmp_path):
    """Closed less than was opened is not "gone"."""

    session_factory = _fixture(tmp_path)
    history = [
        {
            "posId": POS,
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "3",
            "closePos": "1",
            "uTime": "1788830587000",
        }
    ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(history=history),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )

    assert result.manually_closed == 0
    assert result.pending_absence == 1
    assert _state(session_factory) == ("active", "active", "entered")


def test_an_empty_snapshot_judges_nothing(tmp_path):
    """An empty positions list is a read that told us nothing.

    An account with no positions and a venue answering 200 with an empty page
    are indistinguishable from here, and one of them would close every bound
    position at once.
    """

    session_factory = _fixture(tmp_path)

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )

    assert result.skipped_empty_snapshot is True
    assert result.manually_closed == 0
    assert result.pending_absence == 0
    assert _state(session_factory) == ("active", "active", "entered")
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []


def test_a_read_error_aborts_instead_of_judging(tmp_path):
    """A failed read must never look like an empty account."""

    class Exploding(_Client):
        def list_positions(self, *, inst_id=None):
            raise RuntimeError("deepcoin unreachable")

    session_factory = _fixture(tmp_path)

    try:
        sync_manual_closed_deepcoin_positions(
            session_factory,
            client=Exploding(),
            synced_at=NOW,
            allow_exchange_mutations=False,
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover - the read must not be swallowed
        raise AssertionError("a failed positions read must not be swallowed")

    assert _state(session_factory) == ("active", "active", "entered")


def test_history_unavailable_falls_back_to_needing_two_absences(tmp_path):
    """An unreadable history proves nothing; it must not shortcut the pair."""

    session_factory = _fixture(tmp_path)

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(history_raises=True),
        synced_at=NOW,
        allow_exchange_mutations=False,
    )

    assert result.manually_closed == 0
    assert result.pending_absence == 1
    assert _state(session_factory) == ("active", "active", "entered")


def test_the_alarm_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "position_marked_manually_closed" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_the_new_actions_write_no_exchange_order():
    from telegram_kol_research.execution_events import (
        NON_EXCHANGE_WRITING_EXECUTION_ACTIONS,
    )

    assert ABSENCE_OBSERVED_ACTION in NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
    assert MARKED_CLOSED_ACTION in NON_EXCHANGE_WRITING_EXECUTION_ACTIONS


# --------------------------------------------------------------------------
# The prepared reclaim (A-10a option (a)) -- guards only; it is not run here
# --------------------------------------------------------------------------


def test_the_reclaim_refuses_an_empty_positions_read():
    from telegram_kol_research.one_off.reclaim_binding_343_2026_09_09 import (
        ReclaimRefused,
        verify_exchange_preconditions,
    )

    class Empty:
        def list_positions(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

    try:
        verify_exchange_preconditions(Empty())
    except ReclaimRefused as exc:
        assert "empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an empty read must not authorise a reclaim")


def test_the_reclaim_refuses_when_a_stop_has_gone():
    """Reclaiming an unprotected position would be worse than leaving it."""

    from telegram_kol_research.one_off.reclaim_binding_343_2026_09_09 import (
        EXPECTED_STOP_ORDER_IDS,
        POS_ID,
        ReclaimRefused,
        verify_exchange_preconditions,
    )

    class OneStopGone:
        def list_positions(self):
            return [{"posId": POS_ID, "instId": "BTC-USDT-SWAP", "pos": "3"}]

        def list_trigger_orders_pending(self, *, inst_id):
            return [{"ordId": EXPECTED_STOP_ORDER_IDS[0]}]

    try:
        verify_exchange_preconditions(OneStopGone())
    except ReclaimRefused as exc:
        assert EXPECTED_STOP_ORDER_IDS[1] in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a missing stop must not authorise a reclaim")


def test_the_reclaim_accepts_the_state_a10a_found():
    from telegram_kol_research.one_off.reclaim_binding_343_2026_09_09 import (
        EXPECTED_STOP_ORDER_IDS,
        POS_ID,
        verify_exchange_preconditions,
    )

    class AsFound:
        def list_positions(self):
            return [
                {"posId": POS_ID, "instId": "BTC-USDT-SWAP", "pos": "3",
                 "avgPx": "79412.8"}
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            # No posId on these rows -- matched by order id, as the venue
            # actually returns them.
            return [{"ordId": order_id} for order_id in EXPECTED_STOP_ORDER_IDS]

    evidence = verify_exchange_preconditions(AsFound())
    assert evidence["live_size"] == "3"
    assert evidence["stops_present"] == list(EXPECTED_STOP_ORDER_IDS)


# ---------------------------------------------------------------------------
# A-10c: the snapshot is older than what the ledger already knows
#
# What actually happened on 2026-09-08 was not one unlucky read. Two things
# wrote to binding 343 inside the same minute: one claimed leg 589 for pos
# 1001125178552543 from trigger-fill evidence, set the binding active and put a
# stop on the position (stamp 01:23:10.548695); the other was this sweep,
# holding a snapshot read at 01:23:03.102990 -- four seconds before that
# position existed -- and it committed last. The row still carries
# ``recovered_at`` 01:23:10.548695 next to a ``closed`` written at
# 01:23:03.102990: the fingerprint of the claim it overwrote.


def _claim(session_factory, *, recovered_at=None, last_verified_at=None):
    """Record that somebody claimed this binding at a given moment."""

    with session_factory() as session:
        binding = session.get(ExecutionBinding, 343)
        binding.recovered_at = recovered_at
        leg = session.get(ExecutionOrderLeg, 589)
        leg.last_verified_at = last_verified_at
        session.commit()


def test_a_snapshot_older_than_the_claim_does_not_get_to_judge(tmp_path):
    """The 2026-09-08 race: claimed at 01:23:10, judged on a 01:23:03 read."""

    session_factory = _fixture(tmp_path)
    snapshot_at = NAIVE
    _claim(session_factory, recovered_at=snapshot_at + timedelta(seconds=7.4))

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at,
        snapshot_clock=lambda: snapshot_at,
    )

    assert result.skipped_claimed_after_snapshot == 1
    assert result.manually_closed == 0
    # Not even an absence observation: this snapshot has nothing to say about
    # this binding, so it does not get to contribute half of a proof either.
    assert result.pending_absence == 0
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []
    assert _state(session_factory) == ("active", "active", "entered")


def test_a_stale_look_does_not_count_towards_the_two(tmp_path):
    """The skipped look contributes nothing, so the next one is still the first.

    This is the part that matters for 2026-09-08. Even after the guard lets a
    later, honest snapshot through, that snapshot is only ever *one* absence,
    and one absence still closes nothing.
    """

    session_factory = _fixture(tmp_path)
    snapshot_at = NAIVE
    _claim(session_factory, recovered_at=snapshot_at + timedelta(seconds=7.4))

    stale = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at,
        snapshot_clock=lambda: snapshot_at,
    )
    assert stale.skipped_claimed_after_snapshot == 1
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []

    later = snapshot_at + timedelta(seconds=61)
    fresh = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=later,
        snapshot_clock=lambda: later,
    )

    assert fresh.skipped_claimed_after_snapshot == 0
    assert fresh.pending_absence == 1
    assert fresh.manually_closed == 0
    assert len(_events(session_factory, ABSENCE_OBSERVED_ACTION)) == 1
    assert _state(session_factory) == ("active", "active", "entered")


def test_a_pos_id_claimed_inside_this_round_is_not_absent_from_it(tmp_path):
    """The leg-level half: last_verified_at is a claim too.

    ``recovered_at`` is written when the binding is re-derived, which does not
    happen on every claim. The leg's own ``last_verified_at`` is what moves the
    moment attribution lands, and on 2026-09-08 it moved to 01:23:10.548695 --
    later than the snapshot that then called the position missing.
    """

    session_factory = _fixture(tmp_path)
    snapshot_at = NAIVE
    _claim(
        session_factory,
        recovered_at=snapshot_at - timedelta(minutes=5),
        last_verified_at=snapshot_at + timedelta(seconds=7.4),
    )

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at,
        snapshot_clock=lambda: snapshot_at,
    )

    assert result.skipped_claimed_after_snapshot == 1
    assert result.manually_closed == 0
    assert _state(session_factory) == ("active", "active", "entered")


def test_a_claim_older_than_the_snapshot_is_not_an_excuse(tmp_path):
    """The guard is about age, not about being claimed at all.

    Without this, "was verified once" would become a permanent exemption and
    the sweep would stop closing anything -- the opposite failure.
    """

    session_factory = _fixture(tmp_path)
    snapshot_at = NAIVE
    _claim(
        session_factory,
        recovered_at=snapshot_at - timedelta(hours=2),
        last_verified_at=snapshot_at - timedelta(hours=2),
    )

    first = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at,
        snapshot_clock=lambda: snapshot_at,
    )
    assert first.skipped_claimed_after_snapshot == 0
    assert first.pending_absence == 1

    second_at = snapshot_at + timedelta(seconds=61)
    second = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=second_at,
        snapshot_clock=lambda: second_at,
    )
    assert second.skipped_claimed_after_snapshot == 0
    assert second.manually_closed == 1
    assert _state(session_factory) == ("closed", "manually_closed", "exited")


# ---------------------------------------------------------------------------
# A-10d: the guard was keyed to the wrong moment, and nothing said so
#
# A-10c compared the claim against ``synced_at`` -- the stamp taken when the
# reconcile round opened. The sweep does not run until reconcile finishes, and
# on 2026-09-10 that was twenty-four seconds later; the management planner
# reconciles on its own schedule and refreshes every live binding's
# recovered_at in between. So the guard fired on every binding of every round
# for twenty-five rounds, the sweep judged nothing at all, and the only reason
# anyone found out is that a person read the journal.


def test_the_round_stamp_is_not_the_read_and_a_fresh_read_may_judge(tmp_path):
    """The production regression, as a test.

    Round opens, reconcile runs, a concurrent claim lands, and only then does
    the sweep read positions. The claim is newer than the round stamp and older
    than the read, and the sweep is entitled to judge.
    """

    session_factory = _fixture(tmp_path)
    round_opened_at = NAIVE
    claimed_at = round_opened_at + timedelta(seconds=7)
    read_at = round_opened_at + timedelta(seconds=24)
    _claim(session_factory, recovered_at=claimed_at, last_verified_at=claimed_at)

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=round_opened_at,
        snapshot_clock=lambda: read_at,
    )

    assert result.skipped_claimed_after_snapshot == 0
    assert result.pending_absence == 1
    assert _state(session_factory) == ("active", "active", "entered")


def test_a_claim_after_the_read_still_stops_the_judgement(tmp_path):
    """The guard itself still works, now against the moment that means it."""

    session_factory = _fixture(tmp_path)
    read_at = NAIVE
    _claim(session_factory, recovered_at=read_at + timedelta(seconds=1))

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=read_at - timedelta(seconds=24),
        snapshot_clock=lambda: read_at,
    )

    assert result.skipped_claimed_after_snapshot == 1
    assert result.pending_absence == 0


def _degenerate_round(session_factory, *, at, captured):
    """One round in which the guard refuses the only live binding."""

    import telegram_kol_research.runtime_incident_adapters as adapters

    original = adapters.capture_manual_close_guard_degenerate

    def _record(session_factory_arg, **kwargs):
        captured.append(kwargs)
        return None

    adapters.capture_manual_close_guard_degenerate = _record
    try:
        _claim(session_factory, recovered_at=at + timedelta(seconds=1))
        return sync_manual_closed_deepcoin_positions(
            session_factory,
            client=_Client(),
            synced_at=at,
            snapshot_clock=lambda: at,
        )
    finally:
        adapters.capture_manual_close_guard_degenerate = original


def test_two_degenerate_rounds_say_nothing_three_raise_the_alarm(tmp_path):
    """One refused round is ordinary; three in a row is a broken guard.

    A claim really can land inside the read, so refusing once proves nothing.
    Refusing everything three rounds running means the reference moment is
    wrong again -- and from outside, a guard that refuses everything is
    indistinguishable from a quiet system.
    """

    session_factory = _fixture(tmp_path)
    captured: list[dict] = []

    first = _degenerate_round(session_factory, at=NAIVE, captured=captured)
    assert first.skipped_claimed_after_snapshot == 1
    assert captured == []

    _degenerate_round(session_factory, at=NAIVE + timedelta(minutes=1), captured=captured)
    assert captured == [], "two rounds must not alert"

    _degenerate_round(session_factory, at=NAIVE + timedelta(minutes=2), captured=captured)
    assert len(captured) == 1
    assert captured[0]["streak"] == 3
    assert captured[0]["live_bindings"] == 1


def test_one_judging_round_clears_the_streak(tmp_path):
    """The streak counts consecutive rounds, so a good round resets it."""

    session_factory = _fixture(tmp_path)
    captured: list[dict] = []

    _degenerate_round(session_factory, at=NAIVE, captured=captured)
    _degenerate_round(session_factory, at=NAIVE + timedelta(minutes=1), captured=captured)

    # A round the guard lets through: the claim is older than the read.
    healthy_at = NAIVE + timedelta(minutes=2)
    _claim(session_factory, recovered_at=healthy_at - timedelta(seconds=30))
    healthy = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=healthy_at,
        snapshot_clock=lambda: healthy_at,
    )
    assert healthy.skipped_claimed_after_snapshot == 0

    _degenerate_round(session_factory, at=NAIVE + timedelta(minutes=3), captured=captured)
    assert captured == [], "the streak restarted, so three-in-a-row has not happened"


def test_the_two_clocks_are_recorded_beside_the_row_stamp(tmp_path):
    """A row's own stamp answers neither "when was this read" nor "when written"."""

    session_factory = _fixture(tmp_path)
    round_opened_at = NAIVE
    read_at = round_opened_at + timedelta(seconds=24)

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=round_opened_at,
        snapshot_clock=lambda: read_at,
    )

    events = _events(session_factory, ABSENCE_OBSERVED_ACTION)
    assert len(events) == 1
    import json

    after = json.loads(events[0].after_json)
    assert after["snapshot_read_at"] == read_at.isoformat()
    assert after["wall_clock_at"] == read_at.isoformat()
    # The row's own stamp is still the round's, deliberately unchanged.
    assert str(events[0].created_at) == str(round_opened_at)


# ---------------------------------------------------------------------------
# A-10d, second finding: the alarm could never fire
#
# A-10b's whole point was that writing off a bound position stops being
# silent. The alert carried pos_id, which was not in the closed summary
# vocabulary, so runtime_incidents refused both the detailed and the minimal
# summary and produced no row -- and the refusal is logged, not raised, so the
# sweep looked entirely healthy. Three real write-offs on 2026-09-10 went
# unannounced. Every test written for A-10b asserted the event row, which is
# the thing being built, and none asserted the incident row, which is the
# thing being relied on.


def _permissive_incident_config():
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )

    return RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES)
    )


def test_writing_off_a_position_actually_produces_an_incident_row(tmp_path):
    """Not "the adapter was called" -- the row exists and names the position."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_position_marked_manually_closed,
    )

    session_factory = create_session_factory(tmp_path / "incidents.db")
    capture_position_marked_manually_closed(
        session_factory,
        config=_permissive_incident_config(),
        execution_binding_id=345,
        pos_id=POS,
        basis="position_history_full_close",
        occurred_at=NOW,
    )

    with session_factory() as session:
        rows = (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "position_marked_manually_closed"
            )
            .all()
        )
    assert len(rows) == 1, "the summary was refused and nobody was told"
    assert POS in rows[0].redacted_summary
    assert "position_history_full_close" in rows[0].redacted_summary


def test_a_degenerate_guard_actually_produces_an_incident_row(tmp_path):
    """The same assertion for the guard's own alarm, so it cannot rot quietly."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_manual_close_guard_degenerate,
    )

    session_factory = create_session_factory(tmp_path / "incidents.db")
    capture_manual_close_guard_degenerate(
        session_factory,
        config=_permissive_incident_config(),
        streak=3,
        live_bindings=2,
        occurred_at=NOW,
    )

    with session_factory() as session:
        rows = (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "manual_close_guard_degenerate"
            )
            .all()
        )
    assert len(rows) == 1
    assert "guard_refused_every_binding" in rows[0].redacted_summary


# ---------------------------------------------------------------------------
# A-10e: an empty read is confirmed the same way absence is
#
# A-10b stood the whole sweep down on any empty positions read, because an
# empty page and an empty account are indistinguishable and one of them would
# close every bound position at once. On 2026-09-10 the last position closed
# and the account went genuinely flat -- and the sweep then did nothing at all,
# six rounds running, and would have kept doing nothing forever. So the empty
# read gets the same treatment a missing position gets: seen twice, at least a
# minute apart, before it is allowed to mean anything.


def test_one_empty_read_still_decides_nothing(tmp_path):
    session_factory = _fixture(tmp_path)
    at = NAIVE

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=at,
        snapshot_clock=lambda: at,
    )

    assert result.skipped_empty_snapshot is True
    assert result.proceeded_on_confirmed_empty is False
    assert _state(session_factory) == ("active", "active", "entered")
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []


def test_two_empty_reads_too_close_together_still_decide_nothing(tmp_path):
    session_factory = _fixture(tmp_path)
    at = NAIVE

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=at,
        snapshot_clock=lambda: at,
    )
    second_at = at + timedelta(seconds=59)
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=second_at,
        snapshot_clock=lambda: second_at,
    )

    assert result.skipped_empty_snapshot is True
    assert _state(session_factory) == ("active", "active", "entered")


def test_two_empty_reads_a_minute_apart_let_the_sweep_work_again(tmp_path):
    """The frozen-forever case. The account really is flat; act like it."""

    session_factory = _fixture(tmp_path)
    at = NAIVE

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=at,
        snapshot_clock=lambda: at,
    )
    second_at = at + timedelta(seconds=61)
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=second_at,
        snapshot_clock=lambda: second_at,
    )

    assert result.skipped_empty_snapshot is False
    assert result.proceeded_on_confirmed_empty is True
    # It works again, and A-10b still applies: absence is recorded, not acted on.
    assert result.pending_absence == 1
    assert _state(session_factory) == ("active", "active", "entered")


def test_a_non_empty_read_in_between_restarts_the_confirmation(tmp_path):
    """One position reappearing means the account was never flat."""

    session_factory = _fixture(tmp_path)
    at = NAIVE

    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=at,
        snapshot_clock=lambda: at,
    )
    middle_at = at + timedelta(seconds=30)
    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=middle_at,
        snapshot_clock=lambda: middle_at,
    )
    later_at = at + timedelta(seconds=120)
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(positions=[]),
        synced_at=later_at,
        snapshot_clock=lambda: later_at,
    )

    assert result.skipped_empty_snapshot is True, (
        "the clock restarted at the later empty read, so it is the first again"
    )


def test_a_summary_key_outside_the_vocabulary_fails_the_suite(tmp_path):
    """The guard for every guard here (A-10e).

    Capture fails open in production on purpose. That is also how A-8c and
    A-10b each shipped an alarm that could never fire: the only symptom was a
    log line. Under the suite the same condition raises, so the next one is a
    red test instead of a silence somebody notices weeks later.
    """

    import pytest

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.runtime_incident_adapters import _capture
    from telegram_kol_research.runtime_incidents import RuntimeIncidentBoundsError

    session_factory = create_session_factory(tmp_path / "strict.db")
    with pytest.raises(RuntimeIncidentBoundsError):
        _capture(
            session_factory,
            config=_permissive_incident_config(),
            source_kind="execution_binding",
            source_record_id="343",
            incident_type="position_marked_manually_closed",
            severity="high",
            redacted_summary='{"component":"x","not_a_known_field":"y"}',
            occurred_at=NOW,
            recorder=None,
        )
