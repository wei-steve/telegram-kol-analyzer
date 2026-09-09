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


def _observation(session, *, pos_id, size, observed_at, complete=True, fingerprint=None):
    session.add(
        PositionReconciliationObservation(
            venue="deepcoin",
            execution_binding_id=1,
            execution_order_leg_id=1,
            strategy_instance_id="strategy-1",
            avg_entry_price="80000",
            snapshot_fingerprint=(
                fingerprint
                or f"{pos_id}-{observed_at.isoformat()}".ljust(64, "0")[:64]
            ),
            pos_id=pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            size_text=size,
            snapshot_complete=complete,
            observed_at=observed_at.replace(tzinfo=None),
            pending_tpsl_json="[]",
        )
    )


def _lifecycle(session, *, lifecycle_id, binding_id, pos_id, chat_id=-100):
    if binding_id is not None:
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
                status="active",
                pos_id=pos_id,
            )
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


def test_a_fresh_complete_snapshot_lists_the_open_positions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _observation(session, pos_id="pos-open", size="3", observed_at=NOW - timedelta(minutes=1))
        _observation(session, pos_id="pos-closed", size="0", observed_at=NOW - timedelta(minutes=1))
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) == frozenset({"pos-open"})


def test_a_snapshot_older_than_five_minutes_is_unknown_not_empty(tmp_path):
    """The distinction the whole design turns on."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _observation(session, pos_id="pos-open", size="3", observed_at=NOW - timedelta(minutes=6))
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) is None


def test_an_incomplete_snapshot_is_not_used(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _observation(
            session,
            pos_id="pos-open",
            size="3",
            observed_at=NOW - timedelta(minutes=1),
            complete=False,
        )
        session.commit()

    with session_factory() as session:
        assert load_verified_position_ids(session, now=NOW) is None


def test_the_newest_observation_per_position_wins(tmp_path):
    """A position that closed a minute ago is not open because it once was."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _observation(session, pos_id="pos-a", size="5", observed_at=NOW - timedelta(minutes=4))
        _observation(session, pos_id="pos-a", size="0", observed_at=NOW - timedelta(minutes=1))
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
        _observation(
            session,
            pos_id="pos-open",
            size="3",
            observed_at=NOW - timedelta(minutes=1),
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
