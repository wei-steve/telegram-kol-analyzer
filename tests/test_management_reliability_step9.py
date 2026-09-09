"""A-9: the operator can finally answer the question A-7 asks.

A-7 parks a management instruction whose target nobody can settle and sends a
notification listing the candidates. Until now that was half a conversation --
the bot had no command to answer with, so every parked instruction stayed
parked. These tests cover the three ways out and the two ways in that must be
refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_events import (
    NON_EXCHANGE_WRITING_EXECUTION_ACTIONS,
)
from telegram_kol_research.management_target_confirmation import (
    CONFIRMATION_TIMEOUT,
    OPERATOR_DISMISSED,
    choose_management_target,
    dismiss_management_target,
    expire_stale_management_confirmations,
)
from telegram_kol_research.management_target_verification import (
    AWAITING_CONFIRMATION,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
NAIVE = NOW.replace(tzinfo=None)
CHAT = -1002337721508
OPERATOR_CHAT = -4999000111
RAW_ID = 15155


def _fixture(tmp_path, *, live=True, candidates=True, awaiting=True):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                id=RAW_ID,
                chat_id=CHAT,
                message_id=RAW_ID,
                text="全部平掉",
                posted_at=NAIVE,
                created_at=NAIVE,
            )
        )
        session.add(
            ExecutionBinding(
                id=345,
                venue="deepcoin",
                strategy_instance_id="strategy-345",
                kol_id=1,
                chat_id=CHAT,
                message_id=345,
                symbol="BTC",
                side="long",
                status="active" if live else "closed",
                last_exchange_status=(
                    "position_ownership_verified" if live else "entry_legs_terminal"
                ),
                pos_id="pos-open" if live else None,
                recovered_at=NAIVE - timedelta(seconds=20),
            )
        )
        session.add(
            StrategyLifecycle(
                id=1097,
                chat_id=CHAT,
                message_id=1097,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                execution_binding_id=345,
                signal_at=NAIVE - timedelta(hours=2),
                entered_at=NAIVE - timedelta(hours=1),
            )
        )
        candidate = SignalCandidate(
            raw_message_id=RAW_ID,
            symbol="BTC",
            side="long",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        result = {}
        if candidates:
            result["confirmation_candidates"] = [
                {
                    "number": 1,
                    "lifecycle_id": 1097,
                    "symbol": "BTC",
                    "side": "long",
                    "entry_range_low": "80000",
                    "entry_range_high": "80500",
                    "entered_at": "2026-09-09 11:00:00",
                }
            ]
        session.add(
            MessageInstructionItem(
                id=1500,
                raw_message_id=RAW_ID,
                signal_candidate_id=int(candidate.id),
                sequence=0,
                instruction_kind="management",
                idempotency_key="item-1500",
                status=AWAITING_CONFIRMATION if awaiting else "pending",
                result_json=json.dumps(result) if result else None,
                last_progress_at=NAIVE,
                created_at=NAIVE,
                updated_at=NAIVE,
            )
        )
        session.commit()
    return session_factory


def _events(session_factory, action):
    with session_factory() as session:
        return (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == action)
            .all()
        )


# --------------------------------------------------------------------------
# The three ways out
# --------------------------------------------------------------------------


def test_choosing_a_live_candidate_sends_the_instruction_back_to_the_worker(tmp_path):
    session_factory = _fixture(tmp_path)

    outcome = choose_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        choice_number=1,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW,
    )

    assert outcome.status == "chosen"
    assert outcome.lifecycle_id == 1097
    with session_factory() as session:
        item = session.get(MessageInstructionItem, 1500)
        assert item.status == "pending"
        choice = json.loads(item.result_json)["operator_choice"]
        assert choice["lifecycle_id"] == 1097
        assert choice["operator_chat_id"] == OPERATOR_CHAT
        # The worker resolves its target from the candidate, so the operator's
        # pick has to land there too or the item would go back to pending
        # pointing at nothing.
        candidate = session.get(SignalCandidate, int(item.signal_candidate_id))
        assert candidate.target_lifecycle_id == 1097

    audit = _events(session_factory, "management_target_chosen")
    assert len(audit) == 1
    after = json.loads(audit[0].after_json)
    assert after["accepted"] is True
    assert after["operator_chat_id"] == OPERATOR_CHAT
    assert json.loads(audit[0].before_json)["offered_candidates"][0]["number"] == 1


def test_dismissing_records_a_decision_rather_than_neglect(tmp_path):
    session_factory = _fixture(tmp_path)

    outcome = dismiss_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW,
    )

    assert outcome.status == "dismissed"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, 1500)
        assert item.status == "failed"
        assert json.loads(item.error_json)["reason"] == OPERATOR_DISMISSED
    assert len(_events(session_factory, "management_target_dismissed")) == 1


def test_an_unanswered_question_expires_after_a_reminder(tmp_path):
    """Never execute on a question nobody answered."""

    session_factory = _fixture(tmp_path)
    notified: list[dict] = []

    # 100 minutes in: inside the last half hour, so one reminder.
    first = expire_stale_management_confirmations(
        session_factory,
        now=NOW + timedelta(minutes=100),
        timeout_minutes=120,
        notify=lambda **kwargs: notified.append(kwargs),
    )
    assert first["reminded"] == (1500,)
    assert first["expired"] == ()

    # A minute later, still inside the window: no second reminder.
    second = expire_stale_management_confirmations(
        session_factory,
        now=NOW + timedelta(minutes=101),
        timeout_minutes=120,
        notify=lambda **kwargs: notified.append(kwargs),
    )
    assert second["reminded"] == ()

    third = expire_stale_management_confirmations(
        session_factory,
        now=NOW + timedelta(minutes=121),
        timeout_minutes=120,
        notify=lambda **kwargs: notified.append(kwargs),
    )
    assert third["expired"] == (1500,)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, 1500)
        assert item.status == "failed"
        assert json.loads(item.error_json)["reason"] == CONFIRMATION_TIMEOUT
    assert [entry["kind"] for entry in notified] == [
        "confirmation_reminder",
        CONFIRMATION_TIMEOUT,
    ]
    assert len(_events(session_factory, "management_target_confirmation_timeout")) == 1


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_a_candidate_that_has_since_closed_is_refused(tmp_path):
    """Minutes passed between the question and the answer.

    Accepting a choice we can no longer stand behind is the exact failure A-7
    exists to prevent, so the same verification runs again here.
    """

    session_factory = _fixture(tmp_path, live=False)

    outcome = choose_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        choice_number=1,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW,
    )

    assert outcome.status == "candidate_not_verifiable"
    with session_factory() as session:
        # Still parked: a refused choice must not quietly release the item.
        assert session.get(MessageInstructionItem, 1500).status == AWAITING_CONFIRMATION
    refusal = _events(session_factory, "management_target_chosen")
    assert json.loads(refusal[0].after_json)["accepted"] is False


def test_a_command_from_another_chat_is_refused(tmp_path):
    """Only the SYSTEM bot's own conversation may answer."""

    from telegram_kol_research.telegram_bot_commands import (
        process_system_operator_command,
    )

    session_factory = _fixture(tmp_path)
    response = process_system_operator_command(
        session_factory,
        f"/choose {RAW_ID} 1",
        operator_chat_id=-1000000000,
        alert_chat_id=OPERATOR_CHAT,
        now=NOW,
    )

    assert "只接受来自 SYSTEM bot" in str(response)
    with session_factory() as session:
        assert session.get(MessageInstructionItem, 1500).status == AWAITING_CONFIRMATION
    assert _events(session_factory, "management_target_chosen") == []


def test_choosing_twice_does_not_move_the_instruction_twice(tmp_path):
    session_factory = _fixture(tmp_path)

    first = choose_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        choice_number=1,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW,
    )
    second = choose_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        choice_number=1,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW + timedelta(minutes=1),
    )

    assert first.status == "chosen"
    assert second.status == "already_chosen"
    assert second.lifecycle_id == 1097
    # One command, one audit row.
    assert len(_events(session_factory, "management_target_chosen")) == 1


def test_a_number_nobody_was_offered_is_refused(tmp_path):
    session_factory = _fixture(tmp_path)

    outcome = choose_management_target(
        session_factory,
        raw_message_id=RAW_ID,
        choice_number=7,
        operator_chat_id=OPERATOR_CHAT,
        now=NOW,
    )

    assert outcome.status == "unknown_candidate"
    with session_factory() as session:
        assert session.get(MessageInstructionItem, 1500).status == AWAITING_CONFIRMATION


# --------------------------------------------------------------------------
# The notification the operator reads
# --------------------------------------------------------------------------


def test_the_notification_numbers_the_candidates_and_says_how_to_answer():
    from telegram_kol_research.management_target_verification import (
        describe_candidates,
        reply_instructions,
    )

    class Candidate:
        symbol = "BTC"
        side = "long"
        lifecycle_summary = {
            "id": 1097,
            "entry_range_low": "80000",
            "entry_range_high": "80500",
            "entered_at": "2026-09-09 11:00:00",
        }

    digest = describe_candidates([Candidate(), Candidate()])
    assert digest.startswith("[1] lifecycle 1097")
    assert "[2] lifecycle 1097" in digest
    assert reply_instructions(15155, 2) == "/choose 15155 <1-2> or /dismiss 15155"
    # Nothing to choose between: the only answer offered is to dismiss.
    assert reply_instructions(15155, 0) == "/dismiss 15155"


def test_the_audit_actions_write_no_exchange_order():
    """A-5 task 8 reads this set; an audit row must not look hazardous."""

    for action in (
        "management_target_chosen",
        "management_target_dismissed",
        "management_target_confirmation_timeout",
        "management_target_confirmation_reminder",
    ):
        assert action in NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
