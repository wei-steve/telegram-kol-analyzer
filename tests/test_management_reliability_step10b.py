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
        session_factory, client=_Client(), synced_at=snapshot_at
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
        session_factory, client=_Client(), synced_at=snapshot_at
    )
    assert stale.skipped_claimed_after_snapshot == 1
    assert _events(session_factory, ABSENCE_OBSERVED_ACTION) == []

    fresh = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at + timedelta(seconds=61),
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
        session_factory, client=_Client(), synced_at=snapshot_at
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
        session_factory, client=_Client(), synced_at=snapshot_at
    )
    assert first.skipped_claimed_after_snapshot == 0
    assert first.pending_absence == 1

    second = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=_Client(),
        synced_at=snapshot_at + timedelta(seconds=61),
    )
    assert second.skipped_claimed_after_snapshot == 0
    assert second.manually_closed == 1
    assert _state(session_factory) == ("closed", "manually_closed", "exited")
