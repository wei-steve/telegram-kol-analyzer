import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.entry_assembly_admission import (
    assess_entry_assembly_admission,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    InstructionExecutionContract,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _persist_deferred_entry(session_factory):
    with session_factory() as session:
        strategy = RawMessage(
            chat_id=100,
            message_id=1000,
            posted_at=NOW,
            text="BTC long strategy",
        )
        blocker = RawMessage(
            chat_id=100,
            message_id=1001,
            posted_at=NOW + timedelta(seconds=1),
            text="later structured context",
        )
        session.add_all([strategy, blocker])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            recognition_generation="generation-1",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="a" * 64,
            status="pending",
        )
        session.add(item)
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=blocker.id,
                input_fingerprint="blocker-input",
                claim_token="blocker-claim",
                claimed_at=NOW,
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        session.commit()
        ids = strategy.id, candidate.id, blocker.id, item.id

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=ids[0],
        signal_candidate_id=ids[1],
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )
    assert decision.status == "deferred"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, ids[3])
        item.result_json = json.dumps(
            {
                "status": "deferred",
                "reason": "adjacent_entry_context_pending",
            }
        )
        item.visibility_next_attempt_at = NOW + timedelta(minutes=1)
        session.commit()
    return ids


def _complete_blocker(session_factory, blocker_id):
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=blocker_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=blocker_id,
                version=1,
                input_fingerprint="blocker-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":null,'
                    '"lifecycle_event":{"event_type":"none"}}'
                ),
            )
        )
        session.commit()


def test_lost_wakeup_releases_only_exact_item_without_exchange_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "lost-wakeup.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    with session_factory() as session:
        unrelated_raw = RawMessage(chat_id=200, message_id=80, text="unrelated")
        session.add(unrelated_raw)
        session.flush()
        unrelated_candidate = SignalCandidate(
            raw_message_id=unrelated_raw.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
        )
        session.add(unrelated_candidate)
        session.flush()
        unrelated = MessageInstructionItem(
            raw_message_id=unrelated_raw.id,
            signal_candidate_id=unrelated_candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="b" * 64,
            status="pending",
            visibility_next_attempt_at=NOW + timedelta(minutes=2),
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        limit=10,
    )

    assert result.released == 1
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at is None
        assert session.query(EntryAssemblyAttempt).one().status == "woken"
        assert session.get(MessageInstructionItem, unrelated_id).visibility_next_attempt_at == (
            NOW + timedelta(minutes=2)
        ).replace(tzinfo=None)


def test_not_yet_due_attempt_is_untouched(tmp_path):
    session_factory = create_session_factory(tmp_path / "not-due.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=4),
    )

    assert result.released == 0
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at is not None


def test_expired_attempt_fails_closed_without_exchange_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.execution_deadline_at = NOW + timedelta(seconds=5)
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
    )

    assert result.expired == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert json.loads(item.error_json)["reason"] == "entry_admission_deadline_expired"
        assert session.query(EntryAssemblyAttempt).one().status == "expired"


def test_submit_unknown_contract_is_excluded(tmp_path):
    session_factory = create_session_factory(tmp_path / "submit-unknown.db")
    strategy_id, candidate_id, blocker_id, item_id = _persist_deferred_entry(
        session_factory
    )
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item_id,
                raw_message_id=strategy_id,
                signal_candidate_id=candidate_id,
                intent_kind="entry",
                state="submit_unknown",
                state_version=1,
                attempted_exchange_write=True,
            )
        )
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
    )

    assert result.skipped == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).status == "pending"


def test_repeated_ticks_are_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "repeated.db")
    _, _, blocker_id, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    first = reconcile_due_entry_admissions(
        session_factory, now=NOW + timedelta(seconds=10)
    )
    repeated = reconcile_due_entry_admissions(
        session_factory, now=NOW + timedelta(seconds=20)
    )

    assert first.released == 1
    assert repeated.released == 0
    assert repeated.expired == 0


def test_historical_succeeded_defer_creates_incident_but_no_order(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-stale.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.status = "succeeded"
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
    )

    assert result.incidents == 1
    assert result.released == 0
    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "unclassified_operation_failure"
        assert session.query(EntryAssemblyAttempt).one().status == "expired"
