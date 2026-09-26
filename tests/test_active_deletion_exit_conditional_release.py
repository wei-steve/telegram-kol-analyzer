"""L3: an active deletion exit that holds nothing must stop sealing its lane.

``docs/plans/2026-09-26-active-deletion-exit-selfheal-design.md`` section 4, L3.

The already-released self-heal only looked at ``recovery_required``. The other
branch of the same shape is an exit standing still in one of the worker's four
active states: ``source_execution_barrier`` shuts the lane on
``state != 'succeeded'``, so such a row seals its chat+symbol+side lane just as
completely, and until now no automation could ever end it. 陈哥's
``recovery_required`` pair sealed a lane for eleven days and ate eleven
messages, four of them entry strategies; this is the same cost on the other
branch.

Five conditions, and every unknown is a refusal. Three are reused verbatim from
the released path (no credentials, the exchange read succeeded, every live
position and resting order in the lane belongs to somebody else's binding). Two
are new because an active row still has an owner:

* nobody may be working on it -- and the release is a CAS naming the claim the
  pass saw, so a row a worker holds is never pulled out of its hands;
* the age bar is six hours, D6a's bar, not the stuck state's 120 minutes.

Nothing here writes to the exchange. L3 decides one thing only: whether to keep
the lane shut.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deferred_instruction_recovery import (
    DEFERRED_EXPIRED_REASON,
    DEFERRED_HOLD_REASON,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageProcessingJob,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.source_deletion_exit_timeout import (
    ACTIVE_EXIT_CLAIMED_REASON,
    ACTIVE_EXIT_CREDENTIALS_REASON,
    ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON,
    ACTIVE_RELEASE_LOST_THE_RACE_REASON,
    ACTIVE_STATE_STUCK_AFTER,
    ACTIVE_STATES,
    NO_EXCHANGE_FOOTPRINT_REASON,
    POSITION_GONE_REASON,
    STUCK_EXIT_CAPTURE_MIN_INTERVAL,
    SourceDeletionExitTimeoutResult,
    build_exchange_absence_reader,
    expire_stuck_source_deletion_exits,
)


NOW = datetime(2026, 9, 26, 9, 46, tzinfo=UTC)
CHAT_ID = -1002337721508
#: Old enough for the six-hour bar with room to spare.
SEALED_SINCE = NOW - timedelta(days=11)


# --------------------------------------------------------------------------
# The numbers that must not drift: one active-state list, one bar, one lease
# --------------------------------------------------------------------------


def test_the_active_state_list_is_the_worker_s_own():
    """Four words, now spelled in four places. This is the join between two."""

    from telegram_kol_research.oncall_detector import SEALED_LANE_ACTIVE_STATES
    from telegram_kol_research.source_message_deletion_worker import _ACTIVE_STATES

    assert ACTIVE_STATES == _ACTIVE_STATES
    assert ACTIVE_STATES == SEALED_LANE_ACTIVE_STATES


def test_the_age_bar_is_d6a_s_one_bar():
    """Not a third threshold: what D6a files a case about is what L3 may free."""

    from telegram_kol_research.oncall_detector import SEALED_LANE_STUCK_AFTER

    assert ACTIVE_STATE_STUCK_AFTER == SEALED_LANE_STUCK_AFTER
    assert ACTIVE_STATE_STUCK_AFTER == timedelta(hours=6)


def test_the_claim_lease_is_the_worker_s_own():
    """L3's fourth condition uses the very lease ``_claim_next_job`` uses."""

    from telegram_kol_research.source_deletion_exit_timeout import _claim_lease
    from telegram_kol_research.source_message_deletion_worker import CLAIM_LEASE

    assert _claim_lease() == CLAIM_LEASE == timedelta(minutes=5)


# --------------------------------------------------------------------------
# Fixtures: one active exit over a deleted BTC-long message
# --------------------------------------------------------------------------


def _active_lane_fixture(
    tmp_path,
    *,
    exit_id=410,
    state="cancelling_entries",
    execution_binding_id=None,
    created_at=SEALED_SINCE,
    updated_at=None,
    claim_token=None,
    claimed_at=None,
    with_candidate=True,
):
    """A credential-less active exit sealing 陈哥's BTC-long lane."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                id=15900,
                chat_id=CHAT_ID,
                message_id=15,
                text="BTC 多单 76000-67300",
                source_status="deleted",
                posted_at=created_at.replace(tzinfo=None),
            )
        )
        if with_candidate:
            session.add(
                SignalCandidate(
                    raw_message_id=15900,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                )
            )
        session.add(
            TelegramSourceMessageEvent(
                id=9001,
                chat_id=CHAT_ID,
                message_id=15,
                event_type="message_deleted",
                raw_message_id=15900,
                event_fingerprint="a" * 64,
                binding_state="bound",
                occurred_at=created_at.replace(tzinfo=None),
            )
        )
        session.add(
            SourceMessageDeletionExit(
                id=exit_id,
                source_event_id=9001,
                raw_message_id=15900,
                execution_binding_id=execution_binding_id,
                state=state,
                last_reason="entry_cancellation_pending",
                attempt_count=1,
                claim_token=claim_token,
                claimed_at=(
                    claimed_at.replace(tzinfo=None) if claimed_at is not None else None
                ),
                created_at=created_at.replace(tzinfo=None),
                updated_at=(
                    (updated_at or created_at).replace(tzinfo=None)
                ),
            )
        )
        session.commit()
    return session_factory


def _other_group_binding(session_factory, *, pos_id="pos-383", order_id="ord-383"):
    """米娅's BTC long: the position that really was on the account."""

    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=383,
                venue="deepcoin",
                strategy_instance_id="strategy-383",
                kol_id="9",
                chat_id=-1001111111111,
                message_id=77,
                symbol="BTC",
                side="long",
                status="open",
                pos_id=pos_id,
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=901,
                execution_binding_id=383,
                venue="deepcoin",
                purpose="entry",
                leg_index=1,
                status="active",
                order_id=order_id,
                pos_id=pos_id,
                attribution_status="verified",
                strategy_instance_id="strategy-383",
            )
        )
        session.commit()


def _attributed_lane_rows():
    """The exchange shape that makes a release correct: everything is 米娅's."""

    return {
        "positions": [
            {
                "posId": "pos-383",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "3",
            }
        ],
        "orders": [{"ordId": "ord-383", "instId": "BTC-USDT-SWAP", "posSide": "long"}],
    }


def _reader(*, positions=(), orders=(), broken=False, reads=None):
    def positions_loader():
        if reads is not None:
            reads.append("positions")
        if broken:
            raise RuntimeError("deepcoin down")
        return list(positions)

    return build_exchange_absence_reader(
        positions_loader=positions_loader,
        resting_orders_loader=lambda: list(orders),
    )


def _sweep(session_factory, *, now=NOW, captured=None, reader=None):
    return expire_stuck_source_deletion_exits(
        session_factory,
        now=now,
        # The stuck state's bar, passed on every call, so every case below also
        # asserts that it is not what the active branch is judged on.
        timeout_minutes=120,
        exchange_reader=reader if reader is not None else _reader(),
        capture=(
            (lambda **kwargs: captured.append(kwargs))
            if captured is not None
            else (lambda **kwargs: None)
        ),
    )


def _exit_row(session_factory, exit_id=410):
    with session_factory() as session:
        return session.get(SourceMessageDeletionExit, exit_id)


def _exit_state(session_factory, exit_id=410):
    row = _exit_row(session_factory, exit_id)
    return row.state, row.last_reason


# --------------------------------------------------------------------------
# Condition 1-3, reused: the release, and each refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ACTIVE_STATES)
def test_an_unclaimed_active_exit_over_an_unowned_lane_is_released(tmp_path, state):
    """All four active states, because the barrier treats all four alike."""

    session_factory = _active_lane_fixture(tmp_path, state=state)
    _other_group_binding(session_factory)
    captured: list[dict] = []

    result = _sweep(
        session_factory, captured=captured, reader=_reader(**_attributed_lane_rows())
    )

    assert (result.released, result.held) == ((410,), ())
    assert _exit_state(session_factory) == (
        "succeeded",
        ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON,
    )
    row = _exit_row(session_factory)
    assert (row.claim_token, row.claimed_at) == (None, None)
    assert row.completed_at is not None
    # A release is news, and the throttle never sits on news.
    assert captured[0]["lane_released"] is True
    assert captured[0]["release_reason"] == ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON
    # The alert quotes the bar this row actually crossed, not the stuck one's.
    assert captured[0]["timeout_minutes"] == 360


def test_an_unattributed_position_keeps_an_active_lane_sealed(tmp_path):
    """The safety boundary: an unclaimed BTC long could be our own orphan."""

    session_factory = _active_lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    captured: list[dict] = []

    result = _sweep(
        session_factory,
        captured=captured,
        reader=_reader(
            positions=[
                {
                    "posId": "pos-orphan",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "5",
                }
            ]
        ),
    )

    assert (result.released, result.held) == ((), (410,))
    assert _exit_state(session_factory) == (
        "cancelling_entries",
        "entry_cancellation_pending",
    )
    assert captured[0]["release_reason"] == "lane_footprint_unattributed"


def test_an_unattributed_resting_order_keeps_an_active_lane_sealed(tmp_path):
    """Same boundary for a resting order nobody claims."""

    session_factory = _active_lane_fixture(tmp_path)

    result = _sweep(
        session_factory,
        reader=_reader(
            orders=[
                {"ordId": "ord-orphan", "instId": "BTC-USDT-SWAP", "posSide": "long"}
            ]
        ),
    )

    assert (result.released, result.held) == ((), (410,))
    assert _exit_state(session_factory)[0] == "cancelling_entries"


def test_a_failed_exchange_read_never_releases_an_active_lane(tmp_path):
    """"Unknown" must never be spent as proof, on this branch either."""

    session_factory = _active_lane_fixture(tmp_path)
    captured: list[dict] = []

    result = _sweep(session_factory, captured=captured, reader=_reader(broken=True))

    assert (result.released, result.held) == ((), (410,))
    assert captured[0]["release_reason"] == "lane_read_failed"
    assert _exit_state(session_factory)[0] == "cancelling_entries"


# --------------------------------------------------------------------------
# Condition 4: somebody may be working on it right now
# --------------------------------------------------------------------------


def test_a_live_claim_never_loses_its_row(tmp_path):
    """A worker holding this row must not have it taken away mid-flight.

    The lease is checked before the lane read, so a held row also costs no
    exchange call: it could not be released whatever the snapshot says.
    """

    session_factory = _active_lane_fixture(
        tmp_path,
        claim_token="worker-token",
        # One second short of the lease: the boundary from the held side. The
        # released side is the next case, in its own test because the capture
        # throttle would otherwise silence a second sweep of the same exit.
        claimed_at=NOW - timedelta(minutes=4, seconds=59),
    )
    _other_group_binding(session_factory)
    captured: list[dict] = []
    reads: list[str] = []

    result = _sweep(
        session_factory,
        captured=captured,
        reader=_reader(reads=reads, **_attributed_lane_rows()),
    )

    assert (result.released, result.held) == ((), (410,))
    assert captured[0]["release_reason"] == ACTIVE_EXIT_CLAIMED_REASON
    assert reads == []
    row = _exit_row(session_factory)
    # Nothing at all was written: state, reason and the claim are as they were.
    assert (row.state, row.last_reason) == (
        "cancelling_entries",
        "entry_cancellation_pending",
    )
    assert row.claim_token == "worker-token"
    assert row.completed_at is None


def test_a_claim_whose_lease_has_expired_is_releasable(tmp_path):
    """The other half of condition 4, at exactly the worker's five minutes.

    A dead process's claim is already up for grabs -- ``_claim_next_job`` steals
    it on the same predicate, and ``_transition_claimed`` filters on
    ``claim_token``, so the old holder cannot write over the release either. The
    CAS still names that token: a *re-claim* in between produces a fresh uuid4
    and loses the row nothing.
    """

    session_factory = _active_lane_fixture(
        tmp_path,
        claim_token="dead-worker",
        claimed_at=NOW - timedelta(minutes=5),
    )
    _other_group_binding(session_factory)

    result = _sweep(session_factory, reader=_reader(**_attributed_lane_rows()))

    assert result.released == (410,)
    assert _exit_state(session_factory) == (
        "succeeded",
        ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON,
    )


# --------------------------------------------------------------------------
# Condition 5: the six-hour bar, and only it
# --------------------------------------------------------------------------


def test_an_active_exit_is_judged_on_six_hours_not_on_the_stuck_timeout(tmp_path):
    """Three hours is past ``timeout_minutes`` and nowhere near this bar."""

    session_factory = _active_lane_fixture(
        tmp_path, created_at=NOW - ACTIVE_STATE_STUCK_AFTER + timedelta(minutes=1)
    )
    _other_group_binding(session_factory)

    early = _sweep(session_factory, reader=_reader(**_attributed_lane_rows()))
    # Not a candidate at all: the sweep did not even judge it.
    assert early == SourceDeletionExitTimeoutResult()
    assert _exit_state(session_factory)[0] == "cancelling_entries"

    late = _sweep(
        session_factory,
        now=NOW + timedelta(minutes=1),
        reader=_reader(**_attributed_lane_rows()),
    )
    assert late.released == (410,)


def test_an_active_exit_well_inside_the_stuck_timeout_is_also_left_alone(tmp_path):
    """Two hours old: past 120 minutes, and the active branch does not care."""

    session_factory = _active_lane_fixture(
        tmp_path, created_at=NOW - timedelta(hours=2, minutes=30)
    )
    _other_group_binding(session_factory)

    assert _sweep(
        session_factory, reader=_reader(**_attributed_lane_rows())
    ) == SourceDeletionExitTimeoutResult()


# --------------------------------------------------------------------------
# Never release an exit that has something at the venue to cancel
# --------------------------------------------------------------------------


def test_an_active_exit_with_execution_credentials_is_never_released(tmp_path):
    """It may be halfway through cancelling; that call is the worker's.

    The empty snapshot below would have satisfied the old ``position_gone``
    proof. The active branch never asks for it -- which is also why the venue is
    not read at all here.
    """

    session_factory = _active_lane_fixture(tmp_path, execution_binding_id=383)
    _other_group_binding(session_factory, pos_id="pos-410", order_id="ord-410")
    captured: list[dict] = []
    reads: list[str] = []

    result = _sweep(
        session_factory, captured=captured, reader=_reader(reads=reads)
    )

    assert (result.released, result.held) == ((), (410,))
    assert captured[0]["release_reason"] == ACTIVE_EXIT_CREDENTIALS_REASON
    assert reads == []
    assert _exit_state(session_factory)[0] == "cancelling_entries"


def test_an_active_exit_whose_lane_cannot_be_named_is_left_sealed(tmp_path):
    """No candidate means no symbol/side, so the footprint cannot be judged."""

    session_factory = _active_lane_fixture(tmp_path, with_candidate=False)
    captured: list[dict] = []

    result = _sweep(session_factory, captured=captured)

    assert (result.released, result.held) == ((), (410,))
    assert captured[0]["release_reason"] == "lane_identity_unknown"


# --------------------------------------------------------------------------
# The CAS: the sweep and the worker cannot both win
# --------------------------------------------------------------------------


def test_the_release_loses_the_cas_to_a_worker_claiming_the_same_row(tmp_path):
    """A claim landing between the judgement and the write wins, and we write nothing.

    The worker's claim is the real one (``_claim_next_job``), interleaved the way
    ``test_a_claim_lost_to_another_worker_is_not_returned_twice`` does it: a
    wrapper counts sessions and lets the other worker in just before the CAS
    opens its own.
    """

    from telegram_kol_research.source_message_deletion_worker import _claim_next_job

    session_factory = _active_lane_fixture(tmp_path, state="reconciling")
    _other_group_binding(session_factory)
    captured: list[dict] = []
    opened = {"count": 0}
    worker_claim: list[tuple] = []

    def racing_session_factory():
        opened["count"] += 1
        # Sessions in this pass, in order: the candidate read, the lane identity
        # read, the attribution read, then the release CAS.
        if opened["count"] == 4:
            claim = _claim_next_job(session_factory, claimed_at=NOW)
            if claim is not None:
                worker_claim.append(claim)
        return session_factory()

    result = _sweep(
        racing_session_factory,
        captured=captured,
        reader=_reader(**_attributed_lane_rows()),
    )

    assert len(worker_claim) == 1, "the other worker must have got in"
    assert (result.released, result.held) == ((), (410,))
    assert captured[0]["release_reason"] == ACTIVE_RELEASE_LOST_THE_RACE_REASON
    row = _exit_row(session_factory)
    # The worker owns the row, and the sweep left no trace on it.
    assert row.state == "reconciling"
    assert row.claim_token == worker_claim[0][2]
    assert row.completed_at is None


def test_a_released_row_is_no_longer_claimable_by_the_worker(tmp_path):
    """The other order of the same race: exactly one of the two succeeds."""

    from telegram_kol_research.source_message_deletion_worker import _claim_next_job

    session_factory = _active_lane_fixture(tmp_path, state="reconciling")
    _other_group_binding(session_factory)

    result = _sweep(session_factory, reader=_reader(**_attributed_lane_rows()))

    assert result.released == (410,)
    assert _claim_next_job(session_factory, claimed_at=NOW + timedelta(seconds=5)) is None


# --------------------------------------------------------------------------
# The stuck state keeps its own bar, its own reason, and its own behaviour
# --------------------------------------------------------------------------


def test_the_stuck_state_still_releases_on_its_own_120_minute_bar(tmp_path):
    """L3 must not have moved ``recovery_required`` onto the six-hour bar."""

    session_factory = _active_lane_fixture(
        tmp_path,
        state="recovery_required",
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=3),
    )
    _other_group_binding(session_factory)

    result = _sweep(session_factory, reader=_reader(**_attributed_lane_rows()))

    assert result.released == (410,)
    # Its own reason, not L3's: three hours is under the active bar anyway, so a
    # release here can only have come from the stuck path.
    assert _exit_state(session_factory) == ("succeeded", NO_EXCHANGE_FOOTPRINT_REASON)


def test_the_three_release_reasons_stay_distinguishable(tmp_path):
    """Afterwards ``last_reason`` alone must say which path let the lane go.

    Three databases, three exit ids: the capture throttle is process memory
    keyed by exit id, so reusing one id here would silence the later sweeps.
    """

    proven = _active_lane_fixture(
        tmp_path / "proven",
        exit_id=410,
        state="recovery_required",
        execution_binding_id=383,
        updated_at=NOW - timedelta(hours=3),
    )
    _other_group_binding(proven, pos_id="pos-410", order_id="ord-410")
    assert _sweep(proven, reader=_reader()).released == (410,)
    assert _exit_state(proven, 410) == ("succeeded", POSITION_GONE_REASON)

    stuck = _active_lane_fixture(
        tmp_path / "stuck",
        exit_id=411,
        state="recovery_required",
        updated_at=NOW - timedelta(hours=3),
    )
    _other_group_binding(stuck)
    assert _sweep(stuck, reader=_reader(**_attributed_lane_rows())).released == (411,)
    assert _exit_state(stuck, 411) == ("succeeded", NO_EXCHANGE_FOOTPRINT_REASON)

    active = _active_lane_fixture(tmp_path / "active", exit_id=412)
    _other_group_binding(active)
    assert _sweep(active, reader=_reader(**_attributed_lane_rows())).released == (412,)
    assert _exit_state(active, 412) == (
        "succeeded",
        ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON,
    )

    assert len(
        {
            POSITION_GONE_REASON,
            NO_EXCHANGE_FOOTPRINT_REASON,
            ACTIVE_NO_EXCHANGE_FOOTPRINT_REASON,
        }
    ) == 3


# --------------------------------------------------------------------------
# What a release costs, and what it must never do
# --------------------------------------------------------------------------


def _held_message(session_factory, *, raw_message_id, message_id, reason):
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_message_id,
                chat_id=CHAT_ID,
                message_id=message_id,
                text="BTC 多单 83000-83300",
                source_status="active",
                posted_at=NOW.replace(tzinfo=None),
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=raw_message_id,
                symbol="BTC",
                side="long",
                review_status="approved",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="test",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="agreement",
                automation_status="deferred",
                automation_reason=reason,
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()


def test_releasing_an_active_lane_resumes_the_waiting_but_never_the_expired(tmp_path):
    """The account owner's own rule: too much time has passed, do not order."""

    session_factory = _active_lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    _held_message(
        session_factory,
        raw_message_id=16010,
        message_id=101,
        reason=DEFERRED_HOLD_REASON,
    )
    _held_message(
        session_factory,
        raw_message_id=16011,
        message_id=102,
        reason=DEFERRED_EXPIRED_REASON,
    )

    result = _sweep(session_factory, reader=_reader(**_attributed_lane_rows()))

    assert result.released == (410,)
    with session_factory() as session:
        requeued = {
            int(row.raw_message_id) for row in session.query(MessageProcessingJob).all()
        }
    assert requeued == {16010}


def test_the_active_lane_read_only_happens_on_a_speaking_pass(tmp_path):
    """Five-second ticks must not become 24 REST calls a minute."""

    session_factory = _active_lane_fixture(tmp_path)
    _other_group_binding(session_factory)
    reads: list[str] = []

    for tick in range(4):
        _sweep(
            session_factory,
            now=NOW + timedelta(seconds=5 * tick),
            # One reader per tick, like the worker builds: only the throttle
            # survives between passes, not the memoised snapshot.
            reader=_reader(
                reads=reads,
                positions=[
                    {
                        "posId": "pos-orphan",
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "5",
                    }
                ],
            ),
        )

    assert len(reads) == 1
    # Held throughout -- the orphan position is the refusal, not the throttle.
    assert _exit_state(session_factory)[0] == "cancelling_entries"


def test_a_held_active_exit_is_captured_once_per_interval(tmp_path):
    """The 30-minute throttle governs the new candidates too."""

    session_factory = _active_lane_fixture(tmp_path)
    captured: list[dict] = []

    for tick in range(6):
        _sweep(
            session_factory,
            now=NOW + timedelta(seconds=5 * tick),
            captured=captured,
            reader=_reader(
                orders=[
                    {
                        "ordId": "ord-orphan",
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                    }
                ]
            ),
        )
    assert len(captured) == 1

    _sweep(
        session_factory,
        now=NOW + STUCK_EXIT_CAPTURE_MIN_INTERVAL + timedelta(seconds=1),
        captured=captured,
        reader=_reader(
            orders=[
                {"ordId": "ord-orphan", "instId": "BTC-USDT-SWAP", "posSide": "long"}
            ]
        ),
    )
    assert len(captured) == 2
