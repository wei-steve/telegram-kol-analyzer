import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.instruction_execution_contracts import (
    InstructionExecutionConflictError,
    InstructionExecutionTransitionError,
    load_or_create_instruction_execution_contract,
    transition_instruction_execution_contract,
)
from telegram_kol_research.models import (
    InstructionExecutionContract,
    InstructionExecutionTransition,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
LEGAL_EDGES = (
    ("pending", "deferred"),
    ("pending", "submitting"),
    ("pending", "verified"),
    ("pending", "failed"),
    ("pending", "expired"),
    ("deferred", "pending"),
    ("deferred", "failed"),
    ("deferred", "expired"),
    ("submitting", "verified"),
    ("submitting", "failed"),
    ("submitting", "submit_unknown"),
    ("submit_unknown", "verified"),
    ("submit_unknown", "failed"),
)
ILLEGAL_EDGES = (
    ("pending", "submit_unknown"),
    ("deferred", "submitting"),
    ("submitting", "pending"),
    ("submit_unknown", "pending"),
    ("submit_unknown", "submitting"),
    ("verified", "failed"),
    ("failed", "pending"),
    ("expired", "pending"),
)


def _persist_item(session_factory, *, instruction_kind="entry") -> int:
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
            instruction_kind=instruction_kind,
            strategy_instance_id="deepcoin:100:9974:BTC:long",
            idempotency_key="a" * 64,
        )
        session.add(item)
        session.commit()
        return item.id


def _persist_contract(session_factory, *, state="pending", version=0) -> int:
    item_id = _persist_item(session_factory)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        contract = InstructionExecutionContract(
            message_instruction_item_id=item.id,
            raw_message_id=item.raw_message_id,
            signal_candidate_id=item.signal_candidate_id,
            strategy_instance_id=item.strategy_instance_id,
            intent_kind=item.instruction_kind,
            state=state,
            state_version=version,
        )
        session.add(contract)
        session.commit()
        return contract.id


def _transition_kwargs(before, after):
    values = {
        "expected_state": before,
        "expected_version": 0,
        "new_state": after,
        "reason_code": f"test_{before}_to_{after}",
        "evidence_refs": [{"kind": "test", "id": 17}],
        "transitioned_at": NOW,
    }
    if after == "verified":
        values.update(terminal_kind="verified_refusal", completion_scope="full")
    return values


def test_load_or_create_derives_authoritative_references_and_is_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "create-contract.db")
    item_id = _persist_item(session_factory)

    first = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=NOW,
        deadline_at=NOW + timedelta(minutes=5),
    )
    repeated = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=NOW + timedelta(seconds=1),
        deadline_at=NOW + timedelta(minutes=10),
    )

    assert repeated.id == first.id
    assert first.state == "pending"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        stored = session.get(InstructionExecutionContract, first.id)
        assert stored.raw_message_id == item.raw_message_id
        assert stored.signal_candidate_id == item.signal_candidate_id
        assert stored.strategy_instance_id == item.strategy_instance_id
        assert stored.intent_kind == item.instruction_kind
        assert stored.deadline_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=5)
        assert session.query(InstructionExecutionContract).count() == 1
        assert session.query(InstructionExecutionTransition).count() == 0


@pytest.mark.parametrize(("before", "after"), LEGAL_EDGES)
def test_transition_accepts_legal_edge(tmp_path, before, after):
    session_factory = create_session_factory(
        tmp_path / f"legal-{before}-{after}.db"
    )
    contract_id = _persist_contract(session_factory, state=before)

    result = transition_instruction_execution_contract(
        session_factory,
        contract_id=contract_id,
        **_transition_kwargs(before, after),
    )

    assert result.state == after
    assert result.state_version == 1
    with session_factory() as session:
        transition = session.query(InstructionExecutionTransition).one()
        assert transition.contract_id == contract_id
        assert transition.state_version == 1
        assert transition.previous_state == before
        assert transition.next_state == after
        assert json.loads(transition.evidence_refs_json) == [
            {"id": 17, "kind": "test"}
        ]


@pytest.mark.parametrize(("before", "after"), ILLEGAL_EDGES)
def test_transition_rejects_illegal_edge_without_writes(tmp_path, before, after):
    session_factory = create_session_factory(
        tmp_path / f"illegal-{before}-{after}.db"
    )
    contract_id = _persist_contract(session_factory, state=before)

    with pytest.raises(InstructionExecutionTransitionError):
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            **_transition_kwargs(before, after),
        )

    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        assert contract.state == before
        assert contract.state_version == 0
        assert session.query(InstructionExecutionTransition).count() == 0


def test_stale_version_fails_without_changing_contract_or_ledger(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-version.db")
    contract_id = _persist_contract(session_factory)
    transition_instruction_execution_contract(
        session_factory,
        contract_id=contract_id,
        **_transition_kwargs("pending", "deferred"),
    )

    with pytest.raises(InstructionExecutionConflictError):
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            **_transition_kwargs("pending", "failed"),
        )

    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        assert (contract.state, contract.state_version) == ("deferred", 1)
        assert session.query(InstructionExecutionTransition).count() == 1


def test_submitting_marks_exchange_write_only_at_explicit_writer_boundary(tmp_path):
    planning_factory = create_session_factory(tmp_path / "planning-submit.db")
    planning_id = _persist_contract(planning_factory)
    planned = transition_instruction_execution_contract(
        planning_factory,
        contract_id=planning_id,
        **_transition_kwargs("pending", "submitting"),
    )
    assert planned.attempted_exchange_write is False

    writer_factory = create_session_factory(tmp_path / "writer-submit.db")
    writer_id = _persist_contract(writer_factory)
    written = transition_instruction_execution_contract(
        writer_factory,
        contract_id=writer_id,
        attempted_exchange_write=True,
        **_transition_kwargs("pending", "submitting"),
    )
    assert written.attempted_exchange_write is True


def test_exchange_write_flag_cannot_be_set_outside_submitting_transition(tmp_path):
    session_factory = create_session_factory(tmp_path / "invalid-write-flag.db")
    contract_id = _persist_contract(session_factory)

    with pytest.raises(InstructionExecutionTransitionError):
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            attempted_exchange_write=True,
            **_transition_kwargs("pending", "deferred"),
        )


@pytest.mark.parametrize(
    "evidence_refs",
    [
        {"kind": "not-a-list"},
        ["not-a-mapping"],
        [{"value": object()}],
        [{"value": "x" * 4096}],
    ],
)
def test_evidence_references_must_be_structured_and_bounded(
    tmp_path, evidence_refs
):
    session_factory = create_session_factory(tmp_path / "invalid-evidence.db")
    contract_id = _persist_contract(session_factory)
    kwargs = _transition_kwargs("pending", "deferred")
    kwargs["evidence_refs"] = evidence_refs

    with pytest.raises(ValueError):
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            **kwargs,
        )

    with session_factory() as session:
        assert session.get(InstructionExecutionContract, contract_id).state == (
            "pending"
        )
        assert session.query(InstructionExecutionTransition).count() == 0
