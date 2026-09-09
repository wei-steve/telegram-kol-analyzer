"""Phase 6-pre-1: a WebSocket gap holds a new entry instead of killing it.

Every ``tg-deploy`` restart opens a gap of a few seconds. Phase 5 refused any
entry that arrived inside one and the refusal was terminal, so the entry was
gone. These tests pin the replacement: the same refusal, recorded as a
deferral the reconciler retries until the stream converges or the entry
deadline passes -- and never, at any point, a submission the stream cannot
vouch for.
"""

import json
from datetime import UTC, datetime, timedelta

import telegram_kol_research.deepcoin_entry_admission as admission_module

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_entry_admission import (
    WS_OBSERVATION_DEFER_REASON,
    ws_observation_entry_defer_result,
)
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.entry_assembly_admission import (
    ENTRY_ADMISSION_EXECUTION_DEADLINE,
)
from telegram_kol_research.instruction_execution_outcomes import (
    VISIBILITY_DEFER_REASONS,
    interpret_instruction_outcome,
)
from telegram_kol_research.message_instruction_items import (
    should_defer_instruction_result,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    InstructionExecutionContract,
    InstructionExecutionTransition,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _persist_ws_deferred_entry(
    session_factory,
    *,
    seed: int = 0,
    deferred_at: datetime | None = None,
    deadline_at: datetime | None = None,
    contract_state: str = "deferred",
    result_reason: str = WS_OBSERVATION_DEFER_REASON,
):
    """One entry item exactly as the defer path leaves it: pending and delayed."""

    deferred_at = deferred_at or NOW
    with session_factory() as session:
        strategy = RawMessage(
            chat_id=500 + seed,
            message_id=7000 + (seed * 10),
            posted_at=deferred_at,
            text="ETH long strategy",
        )
        session.add(strategy)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            recognition_generation=f"ws-generation-{seed}",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key=f"{(seed + 1) * 7:064x}",
            status="pending",
            result_json=json.dumps(
                {
                    "status": "deferred",
                    "reason": result_reason,
                    "ws_observation_reason": "open_gap",
                },
                sort_keys=True,
            ),
            visibility_next_attempt_at=deferred_at + timedelta(seconds=5),
            execution_deadline_at=(
                deadline_at
                if deadline_at is not None
                else deferred_at + ENTRY_ADMISSION_EXECUTION_DEADLINE
            ),
            updated_at=deferred_at,
        )
        session.add(item)
        session.flush()
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=strategy.id,
                signal_candidate_id=candidate.id,
                intent_kind="entry",
                state=contract_state,
                state_version=1,
                attempted_exchange_write=False,
                deadline_at=item.execution_deadline_at,
            )
        )
        session.commit()
        return item.id


def _admits(permitted: bool, reason: str = "open_gap"):
    return lambda: (permitted, "" if permitted else reason)


def _blocked_stream(monkeypatch, reason: str = "open_gap"):
    monkeypatch.setattr(
        admission_module,
        "ws_observation_admits_new_entry",
        lambda: (False, reason),
    )


def _healthy_stream(monkeypatch):
    monkeypatch.setattr(
        admission_module,
        "ws_observation_admits_new_entry",
        lambda: (True, ""),
    )


# --- the defer decision itself -------------------------------------------


def test_a_healthy_stream_produces_no_deferral_at_all(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "healthy.db")
    _healthy_stream(monkeypatch)

    assert (
        ws_observation_entry_defer_result(
            session_factory,
            message_instruction_item_id=1,
            now=NOW,
        )
        is None
    )


def test_a_gap_defers_the_entry_and_names_the_stream_reason(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "gap.db")
    item_id = _persist_ws_deferred_entry(session_factory)
    _blocked_stream(monkeypatch, "no_converged_resync")

    result = ws_observation_entry_defer_result(
        session_factory,
        message_instruction_item_id=item_id,
        now=NOW,
    )

    assert result == {
        "status": "deferred",
        "reason": WS_OBSERVATION_DEFER_REASON,
        "ws_observation_reason": "no_converged_resync",
    }
    # The existing item defer machinery must accept it without a second path.
    assert should_defer_instruction_result(result) is True
    outcome = interpret_instruction_outcome(result, intent_kind="entry")
    assert outcome.state == "deferred"
    assert outcome.attempted_exchange_write is False


def test_the_defer_reason_is_registered_as_a_visibility_defer():
    assert WS_OBSERVATION_DEFER_REASON in VISIBILITY_DEFER_REASONS


def test_a_deferral_stamps_the_entry_deadline_exactly_once(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "deadline.db")
    _blocked_stream(monkeypatch)
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, posted_at=NOW, text="x")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="c" * 64,
            status="executing",
        )
        session.add(item)
        session.commit()
        item_id = item.id
        assert item.execution_deadline_at is None

    ws_observation_entry_defer_result(
        session_factory,
        message_instruction_item_id=item_id,
        now=NOW,
    )
    with session_factory() as session:
        first = session.get(MessageInstructionItem, item_id).execution_deadline_at
    assert first == (NOW + ENTRY_ADMISSION_EXECUTION_DEADLINE).replace(tzinfo=None)

    # A second gap an hour later must not slide the deadline forward, or an
    # entry could be held indefinitely by a stream that never converges.
    ws_observation_entry_defer_result(
        session_factory,
        message_instruction_item_id=item_id,
        now=NOW + timedelta(hours=1),
    )
    with session_factory() as session:
        assert (
            session.get(MessageInstructionItem, item_id).execution_deadline_at
            == first
        )


def test_without_an_instruction_item_the_writer_gate_stays_the_only_refusal(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "no-item.db")
    _blocked_stream(monkeypatch)

    # A CLI recovery submit has nothing durable to defer onto. It proceeds here
    # and is refused by the two unchanged checkpoints in the writer.
    assert (
        ws_observation_entry_defer_result(
            session_factory,
            message_instruction_item_id=None,
            now=NOW,
        )
        is None
    )


# --- the reconciler half --------------------------------------------------


def test_a_recovered_stream_releases_the_held_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "release.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )

    assert (result.released, result.expired, result.incidents) == (1, 0, 0)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "pending"
        assert item.visibility_next_attempt_at is None
        contract = session.query(InstructionExecutionContract).one()
        assert contract.state == "deferred"


def test_a_still_open_gap_holds_the_entry_without_touching_it(tmp_path):
    session_factory = create_session_factory(tmp_path / "still-open.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=_admits(False),
    )

    assert (result.released, result.expired, result.skipped) == (0, 0, 0)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "pending"
        assert item.visibility_next_attempt_at is not None


def test_a_gap_that_outlives_the_deadline_expires_loudly(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    deadline = NOW + timedelta(hours=6)
    item_id = _persist_ws_deferred_entry(session_factory, deadline_at=deadline)
    reported = []

    def reporter(**kwargs):
        reported.append(kwargs)
        return object()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=deadline + timedelta(seconds=1),
        execution_contract_mode="shadow",
        incident_reporter=reporter,
        ws_admission=_admits(False),
    )

    assert (result.expired, result.incidents, result.released) == (1, 1, 0)
    assert reported[0]["message_instruction_item_id"] == item_id
    # The alert has to say *why* it was held, or the operator cannot tell a
    # stream gap from an unresolved adjacent context.
    assert reported[0]["defer_reason_code"] == WS_OBSERVATION_DEFER_REASON
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert item.visibility_next_attempt_at is None
        assert json.loads(item.error_json)["reason"] == (
            "entry_admission_deadline_expired"
        )
        contract = session.query(InstructionExecutionContract).one()
        assert contract.state == "expired"
        assert contract.state_version == 2
        transition = session.query(InstructionExecutionTransition).one()
        assert (transition.previous_state, transition.next_state) == (
            "deferred",
            "expired",
        )
        # Nothing invented an attempt row for an entry that never had one.
        assert session.query(EntryAssemblyAttempt).count() == 0


def test_an_expiry_at_the_deadline_beats_a_healthy_stream(tmp_path):
    """A recovered stream does not resurrect an entry whose deadline passed."""

    session_factory = create_session_factory(tmp_path / "deadline-wins.db")
    deadline = NOW + timedelta(hours=6)
    item_id = _persist_ws_deferred_entry(session_factory, deadline_at=deadline)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=deadline,
        execution_contract_mode="shadow",
        incident_reporter=lambda **kwargs: object(),
        ws_admission=_admits(True),
    )

    assert (result.expired, result.released) == (1, 0)
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "failed"


def test_repeated_ticks_release_the_same_entry_only_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "idempotent.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    first = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )
    second = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=20),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )

    assert first.released == 1
    assert second.released == 0
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "pending"


def test_disabled_mode_never_touches_a_ws_deferred_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "ws-disabled.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        ws_admission=_admits(True),
    )

    assert result == type(result)()
    with session_factory() as session:
        assert (
            session.get(MessageInstructionItem, item_id).visibility_next_attempt_at
            is not None
        )


def test_a_just_deferred_entry_is_not_rechecked_in_the_same_breath(tmp_path):
    session_factory = create_session_factory(tmp_path / "not-due.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=1),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )

    assert result.released == 0
    with session_factory() as session:
        assert (
            session.get(MessageInstructionItem, item_id).visibility_next_attempt_at
            is not None
        )


def test_an_entry_below_the_watermark_is_left_to_history(tmp_path):
    session_factory = create_session_factory(tmp_path / "watermark.db")
    item_id = _persist_ws_deferred_entry(session_factory)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        entry_after_item_id=item_id,
        ws_admission=_admits(True),
    )

    assert result.released == 0


def test_a_non_deferred_contract_is_not_released_by_the_ws_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "contract-state.db")
    item_id = _persist_ws_deferred_entry(
        session_factory, contract_state="submit_unknown"
    )

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )

    assert result.released == 0
    with session_factory() as session:
        assert (
            session.get(MessageInstructionItem, item_id).visibility_next_attempt_at
            is not None
        )


def test_the_ws_pass_ignores_an_adjacent_context_deferral(tmp_path):
    """The two defer kinds must not recheck each other's condition."""

    session_factory = create_session_factory(tmp_path / "adjacent.db")
    item_id = _persist_ws_deferred_entry(
        session_factory, result_reason="adjacent_entry_context_pending"
    )

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=_admits(True),
    )

    assert result.released == 0
    with session_factory() as session:
        assert (
            session.get(MessageInstructionItem, item_id).visibility_next_attempt_at
            is not None
        )


def test_one_stream_reading_decides_the_whole_tick(tmp_path):
    """Two entries in one pass must not get two different answers."""

    session_factory = create_session_factory(tmp_path / "one-reading.db")
    _persist_ws_deferred_entry(session_factory, seed=0)
    _persist_ws_deferred_entry(session_factory, seed=1)
    readings = []

    def admits():
        readings.append(len(readings))
        return (True, "")

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
        ws_admission=admits,
    )

    assert result.released == 2
    assert len(readings) == 1


def test_the_contract_carries_the_same_deadline_as_the_item(tmp_path, monkeypatch):
    """A held entry must be refusable on the submit path, not only by a timer."""

    session_factory = create_session_factory(tmp_path / "contract-deadline.db")
    _blocked_stream(monkeypatch)
    with session_factory() as session:
        raw = RawMessage(chat_id=9, message_id=9, posted_at=NOW, text="x")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="d" * 64,
            status="executing",
        )
        session.add(item)
        session.flush()
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                intent_kind="entry",
                state="pending",
                state_version=0,
            )
        )
        session.commit()
        item_id = item.id

    ws_observation_entry_defer_result(
        session_factory,
        message_instruction_item_id=item_id,
        now=NOW,
    )

    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        contract = session.query(InstructionExecutionContract).one()
        assert contract.deadline_at == item.execution_deadline_at
        assert contract.deadline_at == (
            NOW + ENTRY_ADMISSION_EXECUTION_DEADLINE
        ).replace(tzinfo=None)
