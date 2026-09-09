"""A-7: a management instruction may only aim at a position that exists.

Two production messages set the bar. 峰哥 raw 15155 was offered two candidates,
one of them lifecycle 1081 -- an entry that had failed and that
``lifecycle_monitor`` had simulated into ``entered`` anyway -- and resolved to
nothing, silently. 大镖客 raw 15201 was matched to lifecycle 1074, whose
position had closed four days earlier, "by price description and strategy
activity" rather than by a reply.

The user's decision (``ambiguous_target_notifies_user``) is to ask, never to
re-point. So the candidate set is narrowed to positions the exchange actually
shows open, and anything other than exactly one of them goes to a person.
"""

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.management_target_verification import (
    AWAITING_CONFIRMATION,
    NO_BINDING,
    POSITION_ABSENT,
    SNAPSHOT_STALE,
    VERIFIED,
    load_verified_position_ids,
    request_management_target_confirmation,
    verify_lifecycle_targets,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    PositionReconciliationObservation,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


NOW = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)


def _reconciled_binding(
    session,
    *,
    binding_id,
    pos_id,
    live=True,
    recovered_at,
    chat_id=-100,
):
    """A binding as one reconcile round leaves it.

    ``recovered_at`` is stamped every round whatever the outcome; ``active`` +
    ``position_ownership_verified`` is the round saying it found this pos_id in
    the live positions list. When the position closes the same round rewrites
    the row -- which is why this, and not the append-on-change observation
    table, is what "still open" is read from.
    """

    session.add(
        ExecutionBinding(
            id=binding_id,
            venue="deepcoin",
            strategy_instance_id=f"strategy-{binding_id}",
            kol_id=1,
            chat_id=chat_id,
            message_id=binding_id,
            symbol="BTC",
            side="long",
            status="active" if live else "closed",
            last_exchange_status=(
                "position_ownership_verified" if live else "entry_legs_terminal"
            ),
            pos_id=pos_id if live else None,
            recovered_at=recovered_at.replace(tzinfo=None),
        )
    )


def _observation(session, *, pos_id, size, observed_at, complete=True):
    """A row of the append-on-change table, for the regression that needs one."""

    session.add(
        PositionReconciliationObservation(
            venue="deepcoin",
            execution_binding_id=1,
            execution_order_leg_id=1,
            strategy_instance_id="strategy-1",
            avg_entry_price="80000",
            snapshot_fingerprint=f"{pos_id}-{observed_at.isoformat()}".ljust(64, "0")[:64],
            pos_id=pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            size_text=size,
            snapshot_complete=complete,
            observed_at=observed_at.replace(tzinfo=None),
            pending_tpsl_json="[]",
        )
    )


def _lifecycle(
    session,
    *,
    lifecycle_id,
    binding_id,
    pos_id,
    chat_id=-100,
    live=True,
    recovered_at=None,
):
    if binding_id is not None:
        _reconciled_binding(
            session,
            binding_id=binding_id,
            pos_id=pos_id,
            live=live,
            recovered_at=recovered_at or (NOW - timedelta(seconds=30)),
            chat_id=chat_id,
        )
    session.add(
        StrategyLifecycle(
            id=lifecycle_id,
            chat_id=chat_id,
            message_id=lifecycle_id,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            execution_binding_id=binding_id,
            signal_at=NOW.replace(tzinfo=None) - timedelta(hours=2),
            entered_at=NOW.replace(tzinfo=None) - timedelta(hours=1),
        )
    )


# --------------------------------------------------------------------------
# The snapshot: stale is not empty
# --------------------------------------------------------------------------


def test_a_fresh_round_lists_what_it_found_open(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _reconciled_binding(
            session, binding_id=345, pos_id="pos-open", live=True,
            recovered_at=NOW - timedelta(seconds=20),
        )
        _reconciled_binding(
            session, binding_id=337, pos_id="pos-closed", live=False,
            recovered_at=NOW - timedelta(seconds=20),
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) == frozenset({"pos-open"})


def test_one_binding_may_own_several_positions(tmp_path):
    """Production binding 345 holds two pos_ids, comma-joined."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _reconciled_binding(
            session, binding_id=345, pos_id="pos-a,pos-b", live=True,
            recovered_at=NOW - timedelta(seconds=20),
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) == frozenset(
            {"pos-a", "pos-b"}
        )


def test_a_stopped_reconcile_loop_is_unknown_not_empty(tmp_path):
    """The distinction the whole design turns on."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _reconciled_binding(
            session, binding_id=345, pos_id="pos-open", live=True,
            recovered_at=NOW - timedelta(minutes=6),
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) is None


def test_a_reconcile_loop_that_never_ran_is_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=345, venue="deepcoin", strategy_instance_id="strategy-345",
                kol_id=1, chat_id=-100, message_id=345, symbol="BTC", side="long",
                status="active", last_exchange_status="position_ownership_verified",
                pos_id="pos-open",
            )
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) is None


def test_a_binding_the_round_skipped_does_not_count_as_open(tmp_path):
    """Reconcile skips manual-terminal and conflicted bindings.

    Their rows keep whatever the last round that did touch them wrote, so the
    same freshness window that proves the loop is running has to be applied to
    each row, not only to the newest one.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _reconciled_binding(
            session, binding_id=345, pos_id="pos-open", live=True,
            recovered_at=NOW - timedelta(seconds=20),
        )
        _reconciled_binding(
            session, binding_id=200, pos_id="pos-abandoned", live=True,
            recovered_at=NOW - timedelta(days=40),
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) == frozenset({"pos-open"})


def test_a_position_that_closed_is_not_open_because_it_once_was(tmp_path):
    """The regression that took production down, from the other side.

    ``position_reconciliation_observations`` only ever records *non-zero*
    positions and never gains a closing row, so reading its newest row per
    position would report this one open forever -- lifecycle 1074's bug. The
    binding row is rewritten by the round that finds the position gone.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _observation(
            session, pos_id="pos-gone", size="5",
            observed_at=NOW - timedelta(days=5),
        )
        _reconciled_binding(
            session, binding_id=337, pos_id="pos-gone", live=False,
            recovered_at=NOW - timedelta(seconds=20),
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) == frozenset()


# --------------------------------------------------------------------------
# The verdicts: a ghost is never a target
# --------------------------------------------------------------------------


def test_a_lifecycle_without_a_binding_is_never_a_target(tmp_path):
    """Lifecycle 1081's shape: entered on paper, no order behind it."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _lifecycle(session, lifecycle_id=1081, binding_id=None, pos_id=None)
        session.commit()

    with session_factory() as session:
        verdict = verify_lifecycle_targets(
            session, [1081], verified_position_ids=frozenset({"pos-open"})
        )[1081]
    assert (verdict.verified, verdict.reason) == (False, NO_BINDING)


def test_a_lifecycle_whose_position_has_closed_is_never_a_target(tmp_path):
    """Lifecycle 1074's shape: a real binding over a position that is gone."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _lifecycle(session, lifecycle_id=1074, binding_id=337, pos_id="pos-closed")
        session.commit()

    with session_factory() as session:
        verdict = verify_lifecycle_targets(
            session, [1074], verified_position_ids=frozenset({"pos-open"})
        )[1074]
    assert (verdict.verified, verdict.reason) == (False, POSITION_ABSENT)


def test_a_lifecycle_whose_position_is_open_is_a_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _lifecycle(session, lifecycle_id=1097, binding_id=345, pos_id="pos-open")
        session.commit()

    with session_factory() as session:
        verdict = verify_lifecycle_targets(
            session, [1097], verified_position_ids=frozenset({"pos-open"})
        )[1097]
    assert (verdict.verified, verdict.reason) == (True, VERIFIED)


def test_a_stale_snapshot_disqualifies_even_a_real_position(tmp_path):
    """Not knowing is not the same as knowing it is there."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _lifecycle(session, lifecycle_id=1097, binding_id=345, pos_id="pos-open")
        session.commit()

    with session_factory() as session:
        verdict = verify_lifecycle_targets(
            session, [1097], verified_position_ids=None
        )[1097]
    assert (verdict.verified, verdict.reason) == (False, SNAPSHOT_STALE)


# --------------------------------------------------------------------------
# The confirmation request
# --------------------------------------------------------------------------


def _message_with_items(session_factory, *, raw_id=15155, kinds=("management",)):
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_id,
                chat_id=-1002409877375,
                message_id=raw_id,
                text="全部平掉",
                created_at=NOW.replace(tzinfo=None),
            )
        )
        for index, kind in enumerate(kinds):
            session.add(
                SignalCandidate(
                    id=raw_id * 10 + index,
                    raw_message_id=raw_id,
                    event_type="close_signal" if kind != "entry" else "entry_signal",
                    symbol="BTC",
                    side="long",
                )
            )
            session.add(
                MessageInstructionItem(
                    id=raw_id * 10 + index,
                    raw_message_id=raw_id,
                    signal_candidate_id=raw_id * 10 + index,
                    sequence=index,
                    instruction_kind=kind,
                    idempotency_key=f"key-{raw_id}-{index}",
                    status="pending",
                )
            )
        session.commit()
    return session_factory


class _Candidate:
    def __init__(self, lifecycle_id, symbol="BTC", side="long"):
        self.symbol = symbol
        self.side = side
        self.lifecycle_summary = {
            "id": lifecycle_id,
            "entry_range_low": 80000,
            "entry_range_high": 81000,
            "entered_at": "2026-09-09T04:00:00",
        }


def test_two_candidates_park_the_item_and_raise_one_alert(tmp_path):
    session_factory = _message_with_items(
        create_session_factory(tmp_path / "research.db")
    )
    captured: list[dict] = []

    moved = request_management_target_confirmation(
        session_factory,
        raw_message_id=15155,
        candidates=[_Candidate(1081), _Candidate(1097)],
        snapshot_stale=False,
        now=NOW,
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert moved == (151550,)
    assert len(captured) == 1
    assert captured[0]["reason_code"] == "target_ambiguous"
    assert captured[0]["candidate_count"] == 2
    assert "lifecycle 1081" in captured[0]["candidate_digest"]
    assert "lifecycle 1097" in captured[0]["candidate_digest"]
    with session_factory() as session:
        item = session.get(MessageInstructionItem, 151550)
        # Not failed: nothing went wrong, it just needs a person to choose.
        assert item.status == AWAITING_CONFIRMATION
        assert item.last_progress_at is not None


def test_no_verifiable_candidate_is_reported_as_such(tmp_path):
    session_factory = _message_with_items(
        create_session_factory(tmp_path / "research.db")
    )
    captured: list[dict] = []

    request_management_target_confirmation(
        session_factory,
        raw_message_id=15155,
        candidates=[],
        snapshot_stale=False,
        now=NOW,
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert captured[0]["reason_code"] == "no_verifiable_target"
    assert captured[0]["candidate_digest"] == "(no verifiable candidate)"


def test_a_stale_snapshot_is_reported_as_staleness_not_as_absence(tmp_path):
    session_factory = _message_with_items(
        create_session_factory(tmp_path / "research.db")
    )
    captured: list[dict] = []

    request_management_target_confirmation(
        session_factory,
        raw_message_id=15155,
        candidates=[],
        snapshot_stale=True,
        now=NOW,
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert captured[0]["reason_code"] == SNAPSHOT_STALE


def test_an_entry_item_is_not_parked_by_a_management_confirmation(tmp_path):
    """Only the management instruction is waiting on a choice."""

    session_factory = _message_with_items(
        create_session_factory(tmp_path / "research.db"),
        kinds=("management", "entry"),
    )

    moved = request_management_target_confirmation(
        session_factory,
        raw_message_id=15155,
        candidates=[_Candidate(1081), _Candidate(1097)],
        snapshot_stale=False,
        now=NOW,
        capture=lambda **kwargs: None,
    )

    assert moved == (151550,)
    with session_factory() as session:
        assert session.get(MessageInstructionItem, 151551).status == "pending"


def test_the_incident_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "management_target_needs_confirmation" in ALWAYS_NOTIFIED_INCIDENT_TYPES


# --------------------------------------------------------------------------
# Task 1 end to end: the candidate generator drops what it cannot verify
# --------------------------------------------------------------------------


def _thread_with_lifecycle(session, *, thread_id, lifecycle_id, binding_id, pos_id):
    from telegram_kol_research.models import StrategyThread

    session.add(
        StrategyThread(
            id=thread_id,
            chat_id=-1002409877375,
            root_message_id=thread_id,
            symbol="BTC",
            side="long",
            status="active",
            current_lifecycle_id=lifecycle_id,
        )
    )
    _lifecycle(
        session,
        lifecycle_id=lifecycle_id,
        binding_id=binding_id,
        pos_id=pos_id,
        chat_id=-1002409877375,
    )


def _candidate_fixture(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                id=15155,
                chat_id=-1002409877375,
                message_id=15155,
                text="全部平掉",
                posted_at=NOW.replace(tzinfo=None),
                created_at=NOW.replace(tzinfo=None),
            )
        )
        # One ghost (no binding) and one real position, exactly raw 15155's shape.
        _thread_with_lifecycle(
            session, thread_id=81, lifecycle_id=1081, binding_id=None, pos_id=None
        )
        _thread_with_lifecycle(
            session, thread_id=97, lifecycle_id=1097, binding_id=345, pos_id="pos-open"
        )
        session.commit()
    return session_factory


def test_the_ghost_is_dropped_and_the_real_position_survives(tmp_path):
    from telegram_kol_research.strategy_thread_candidates import (
        generate_strategy_thread_candidates,
    )

    session_factory = _candidate_fixture(tmp_path)
    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=15155,
            symbol="BTC",
            side="long",
            require_verified_position=True,
            verified_position_ids=load_verified_position_ids(session, now=NOW),
        )

    assert [c.lifecycle_id for c in candidates] == [1097]
    assert candidates[0].position_verification == VERIFIED


def test_without_the_gate_both_candidates_are_offered_as_before(tmp_path):
    """The unchanged path: an entry, or a notify_only group, sees what it saw."""

    from telegram_kol_research.strategy_thread_candidates import (
        generate_strategy_thread_candidates,
    )

    session_factory = _candidate_fixture(tmp_path)
    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session, raw_message_id=15155, symbol="BTC", side="long"
        )

    assert sorted(c.lifecycle_id for c in candidates) == [1081, 1097]
    assert {c.position_verification for c in candidates} == {"not_required"}


def test_a_stale_snapshot_leaves_no_candidate_at_all(tmp_path):
    """Which is how the caller learns it must ask instead of act."""

    from telegram_kol_research.strategy_thread_candidates import (
        generate_strategy_thread_candidates,
    )

    session_factory = _candidate_fixture(tmp_path)
    with session_factory() as session:
        candidates = generate_strategy_thread_candidates(
            session,
            raw_message_id=15155,
            symbol="BTC",
            side="long",
            require_verified_position=True,
            verified_position_ids=None,
        )

    assert candidates == ()
