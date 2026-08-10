from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    InstructionExecutionContract,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)
from telegram_kol_research.runtime_incident_snapshot import (
    build_instruction_execution_contradiction_snapshot,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _add_contract(session, *, state, terminal_kind=None, deadline_at=None):
    sequence = session.query(MessageInstructionItem).count() + 1
    raw = RawMessage(chat_id=10, message_id=100 + sequence, text="sensitive raw text")
    session.add(raw)
    session.flush()
    candidate = SignalCandidate(
        raw_message_id=raw.id,
        symbol="BTC",
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
        idempotency_key=str(sequence) * 64,
    )
    session.add(item)
    session.flush()
    contract = InstructionExecutionContract(
        message_instruction_item_id=item.id,
        raw_message_id=raw.id,
        signal_candidate_id=candidate.id,
        intent_kind="entry",
        state=state,
        state_version=1,
        terminal_kind=terminal_kind,
        deadline_at=deadline_at,
        last_progress_at=NOW - timedelta(minutes=10),
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=10),
    )
    session.add(contract)
    session.flush()
    return contract.id, item.id, raw.id


def test_instruction_execution_snapshot_is_bounded_redacted_and_future_aware(tmp_path):
    sf = create_session_factory(tmp_path / "snapshot.db")
    with sf() as session:
        exact_ids = _add_contract(
            session,
            state="verified",
            terminal_kind="verified_entry",
        )
        _add_contract(session, state="pending")
        future_ids = _add_contract(
            session,
            state="deferred",
            deadline_at=NOW - timedelta(seconds=1),
        )
        session.commit()
    save_trading_settings(
        sf,
        {
            "instruction_execution_contract_mode": "shadow",
            "instruction_execution_entry_after_item_id": 2,
        },
    )

    snapshot = build_instruction_execution_contradiction_snapshot(
        sf,
        observed_at=NOW,
        limit=20,
    )

    assert snapshot["scan_truncated"] is False
    assert snapshot["contradictions_total"] == 2
    facts = snapshot["facts"]
    assert {(row["reason_code"], row["contract_id"]) for row in facts} == {
        ("verified_without_binding", exact_ids[0]),
        ("deferred_overdue", future_ids[0]),
    }
    assert next(row for row in facts if row["contract_id"] == exact_ids[0])["exact_historical"] is True
    assert next(row for row in facts if row["contract_id"] == future_ids[0])["future_contract"] is True
    assert set(facts[0]) == {
        "reason_code",
        "contract_id",
        "message_instruction_item_id",
        "raw_message_id",
        "future_contract",
        "exact_historical",
    }
    assert "sensitive raw text" not in str(snapshot)


def test_disabled_execution_contract_mode_suppresses_nonexact_legacy_noise(tmp_path):
    sf = create_session_factory(tmp_path / "disabled.db")
    with sf() as session:
        _add_contract(
            session,
            state="deferred",
            deadline_at=NOW - timedelta(hours=1),
        )
        session.commit()

    snapshot = build_instruction_execution_contradiction_snapshot(
        sf,
        observed_at=NOW,
        limit=20,
    )

    assert snapshot["facts"] == ()
    assert snapshot["contradictions_total"] == 0
