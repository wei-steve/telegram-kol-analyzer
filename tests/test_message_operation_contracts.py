from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_operation_contracts import (
    MessageOperationContractBoundsError,
    append_message_operation_item,
    create_message_operation_contract,
    get_message_operation_contract,
    transition_message_operation_contract,
)
from telegram_kol_research.models import MessageOperationContract, RawMessage


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)


def _raw_message(session_factory, *, message_id: int = 42) -> RawMessage:
    with session_factory() as session:
        row = RawMessage(
            chat_id=7,
            message_id=message_id,
            posted_at=NOW,
            text="bounded fixture",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _create_contract(session_factory, *, raw_message_id: int, **overrides):
    values = {
        "raw_message_id": raw_message_id,
        "intent_kind": "manage",
        "expected_terminal_kind": "verified_management",
        "deadline_at": NOW + timedelta(minutes=2),
        "policy_version": "message-operation-contract-v1",
    }
    values.update(overrides)
    return create_message_operation_contract(session_factory, **values)


def test_message_operation_contract_schema_is_additive(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)

    contract = _create_contract(
        session_factory,
        raw_message_id=raw_message.id,
    )

    assert contract.status == "observing"
    assert contract.agent_requested is False
    assert contract.evidence_refs_json == "[]"
    assert contract.runtime_incident_id is None

    inspector = inspect(session_factory.kw["bind"])
    assert {
        "ix_message_operation_contracts_status_deadline",
        "ix_message_operation_contracts_runtime_incident",
        "uq_message_operation_contracts_message_policy",
    } <= {
        index["name"]
        for index in inspector.get_indexes("message_operation_contracts")
    }


def test_one_contract_per_raw_message_and_policy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)

    first = _create_contract(session_factory, raw_message_id=raw_message.id)
    second = _create_contract(session_factory, raw_message_id=raw_message.id)

    assert second.id == first.id
    with session_factory() as session:
        assert session.query(MessageOperationContract).count() == 1


def test_items_are_idempotent_bounded_and_ordered(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)
    contract = _create_contract(session_factory, raw_message_id=raw_message.id)

    first = append_message_operation_item(
        session_factory,
        contract_id=contract.id,
        sequence=1,
        instruction_key="candidate:91",
        instruction_kind="take_profit",
        authoritative_instruction_id="signal_candidate:91",
        expected_descendant_kind="management_item",
        expected_terminal_kind="verified_execution",
        evidence_refs=["signal_candidate:91"],
    )
    second = append_message_operation_item(
        session_factory,
        contract_id=contract.id,
        sequence=1,
        instruction_key="candidate:91",
        instruction_kind="take_profit",
        authoritative_instruction_id="signal_candidate:91",
        expected_descendant_kind="management_item",
        expected_terminal_kind="verified_execution",
        evidence_refs=["signal_candidate:91"],
    )

    assert second.id == first.id
    assert first.status == "observing"
    assert first.observed_terminal_kind is None
    assert first.evidence_refs_json == '["signal_candidate:91"]'

    with pytest.raises(MessageOperationContractBoundsError, match="sequence"):
        append_message_operation_item(
            session_factory,
            contract_id=contract.id,
            sequence=0,
            instruction_key="candidate:92",
            instruction_kind="take_profit",
            authoritative_instruction_id="signal_candidate:92",
            expected_descendant_kind="management_item",
            expected_terminal_kind="verified_execution",
        )
    with pytest.raises(MessageOperationContractBoundsError, match="evidence"):
        append_message_operation_item(
            session_factory,
            contract_id=contract.id,
            sequence=2,
            instruction_key="candidate:92",
            instruction_kind="take_profit",
            authoritative_instruction_id="signal_candidate:92",
            expected_descendant_kind="management_item",
            expected_terminal_kind="verified_execution",
            evidence_refs=["not a stable reference"],
        )


def test_contract_transition_is_compare_and_set_and_closed_enum(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)
    contract = _create_contract(session_factory, raw_message_id=raw_message.id)

    assert transition_message_operation_contract(
        session_factory,
        contract_id=contract.id,
        expected_status="observing",
        new_status="violated",
        violation_code="missing_verified_descendant",
        evidence_refs=["raw_message:42"],
        now=NOW,
    )
    assert not transition_message_operation_contract(
        session_factory,
        contract_id=contract.id,
        expected_status="observing",
        new_status="verified",
        now=NOW,
    )
    updated = get_message_operation_contract(session_factory, contract.id)
    assert updated is not None
    assert updated.status == "violated"
    assert updated.violation_code == "missing_verified_descendant"

    with pytest.raises(MessageOperationContractBoundsError, match="status"):
        transition_message_operation_contract(
            session_factory,
            contract_id=contract.id,
            expected_status="violated",
            new_status="invented",
            now=NOW,
        )


def test_schema_rejects_unbounded_fields_and_non_positive_item_sequence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)
    contract = _create_contract(session_factory, raw_message_id=raw_message.id)

    with session_factory.kw["bind"].begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO message_operation_items ("
                    "contract_id, sequence, instruction_key, instruction_kind, "
                    "authoritative_instruction_id, expected_descendant_kind, "
                    "expected_terminal_kind, status, evidence_refs_json, "
                    "created_at, updated_at) VALUES ("
                    ":contract_id, 0, 'candidate:92', 'take_profit', "
                    "'signal_candidate:92', 'management_item', "
                    "'verified_execution', 'observing', '[]', :now, :now)"
                ),
                {"contract_id": contract.id, "now": NOW},
            )


def test_contract_helper_rejects_unbounded_or_unknown_values(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message = _raw_message(session_factory)

    with pytest.raises(MessageOperationContractBoundsError, match="intent_kind"):
        _create_contract(
            session_factory,
            raw_message_id=raw_message.id,
            intent_kind="invented",
        )
    with pytest.raises(MessageOperationContractBoundsError, match="policy_version"):
        _create_contract(
            session_factory,
            raw_message_id=raw_message.id,
            policy_version="x" * 65,
        )
