from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    INSTRUCTION_EXECUTION_STATES,
    INSTRUCTION_EXECUTION_TERMINAL_KINDS,
    InstructionExecutionContract,
    InstructionExecutionTransition,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _persist_instruction_item(session_factory) -> int:
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=9974,
            posted_at=NOW,
            text="anonymized strategy",
        )
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
            idempotency_key="a" * 64,
        )
        session.add(item)
        session.commit()
        return item.id


def _new_contract(item_id: int, **overrides) -> InstructionExecutionContract:
    values = {
        "message_instruction_item_id": item_id,
        "raw_message_id": 1,
        "signal_candidate_id": 1,
        "intent_kind": "entry",
    }
    values.update(overrides)
    return InstructionExecutionContract(**values)


def test_instruction_execution_vocabularies_are_bounded():
    assert INSTRUCTION_EXECUTION_STATES == frozenset(
        {
            "pending",
            "deferred",
            "submitting",
            "submit_unknown",
            "verified",
            "failed",
            "expired",
        }
    )
    assert INSTRUCTION_EXECUTION_TERMINAL_KINDS == frozenset(
        {
            "verified_entry",
            "verified_management",
            "verified_cancel",
            "verified_exit",
            "verified_refusal",
        }
    )


def test_instruction_item_has_one_execution_contract(tmp_path):
    session_factory = create_session_factory(tmp_path / "one-contract.db")
    item_id = _persist_instruction_item(session_factory)

    with session_factory() as session:
        session.add(_new_contract(item_id))
        session.commit()
        session.add(_new_contract(item_id))
        with pytest.raises(IntegrityError):
            session.commit()


def test_contract_defaults_are_non_terminal_and_exchange_safe(tmp_path):
    session_factory = create_session_factory(tmp_path / "contract-defaults.db")
    item_id = _persist_instruction_item(session_factory)

    with session_factory() as session:
        contract = _new_contract(item_id)
        session.add(contract)
        session.commit()
        session.refresh(contract)

        assert contract.state == "pending"
        assert contract.state_version == 0
        assert contract.attempted_exchange_write is False
        assert contract.evidence_refs_json == "[]"
        assert contract.terminal_kind is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "succeeded"},
        {"terminal_kind": "entered"},
        {"completion_scope": "mostly"},
        {"reason_code": "r" * 129},
        {"evidence_refs_json": "e" * 4097},
        {"state_version": -1},
    ],
)
def test_contract_rejects_unbounded_or_unknown_values(tmp_path, overrides):
    session_factory = create_session_factory(tmp_path / "contract-checks.db")
    item_id = _persist_instruction_item(session_factory)

    with session_factory() as session:
        session.add(_new_contract(item_id, **overrides))
        with pytest.raises(IntegrityError):
            session.commit()


def test_transitions_are_uniquely_ordered_by_contract_version(tmp_path):
    session_factory = create_session_factory(tmp_path / "transition-order.db")
    item_id = _persist_instruction_item(session_factory)

    with session_factory() as session:
        contract = _new_contract(item_id, state="deferred", state_version=2)
        session.add(contract)
        session.flush()
        session.add_all(
            [
                InstructionExecutionTransition(
                    contract_id=contract.id,
                    state_version=2,
                    previous_state="pending",
                    next_state="deferred",
                    reason_code="context_pending",
                ),
                InstructionExecutionTransition(
                    contract_id=contract.id,
                    state_version=1,
                    previous_state=None,
                    next_state="pending",
                    reason_code="projected",
                ),
            ]
        )
        session.commit()
        assert [
            row.state_version
            for row in session.query(InstructionExecutionTransition)
            .order_by(InstructionExecutionTransition.state_version)
            .all()
        ] == [1, 2]

        session.add(
            InstructionExecutionTransition(
                contract_id=contract.id,
                state_version=2,
                previous_state="pending",
                next_state="failed",
                reason_code="duplicate_version",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "values",
    [
        {"state_version": 0},
        {"previous_state": "succeeded"},
        {"next_state": "succeeded"},
        {"reason_code": "r" * 129},
        {"evidence_refs_json": "e" * 4097},
    ],
)
def test_transition_rejects_invalid_audit_values(tmp_path, values):
    session_factory = create_session_factory(tmp_path / "transition-checks.db")
    item_id = _persist_instruction_item(session_factory)

    with session_factory() as session:
        contract = _new_contract(item_id)
        session.add(contract)
        session.flush()
        transition_values = {
            "contract_id": contract.id,
            "state_version": 1,
            "previous_state": None,
            "next_state": "pending",
            "reason_code": "projected",
        }
        transition_values.update(values)
        session.add(InstructionExecutionTransition(**transition_values))
        with pytest.raises(IntegrityError):
            session.commit()
