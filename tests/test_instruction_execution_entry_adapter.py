import hashlib
import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.instruction_execution_contracts import (
    load_or_create_instruction_execution_contract,
)
from telegram_kol_research.instruction_execution_entry_adapter import (
    EntryExecutionContractBlocked,
    prepare_entry_submission_contract,
    project_entry_deferred_contract,
    project_entry_submission_result,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    TradeSignal,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _persist_chain(session_factory, *, leg_count=1):
    draft = {
        "strategy_instance_id": "deepcoin:100:9974:BTC:long",
        "order_legs": [
            {
                "order_type": "limit",
                "client_order_id": f"LEG-{index}",
                "risk_budget_usdt": 10,
            }
            for index in range(1, leg_count + 1)
        ],
        "selected_entry_leg_indices": list(range(1, leg_count + 1)),
    }
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=9974, text="BTC long")
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
            strategy_instance_id=draft["strategy_instance_id"],
            idempotency_key="a" * 64,
        )
        session.add(item)
        session.flush()
        signal = TradeSignal(
            signal_uid="adapter-signal",
            strategy_instance_id=draft["strategy_instance_id"],
            source_type="recovery",
            venue="deepcoin",
            kol_id="chen",
            chat_id=100,
            message_id=9974,
            symbol="BTC",
            side="long",
            action="open_position",
            status="processing",
            payload_json=json.dumps({"deepcoin_order_draft": draft}),
        )
        session.add(signal)
        session.commit()
        ids = item.id, signal.id
    load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=ids[0],
        projected_at=NOW,
    )
    return ids[0], ids[1], draft


def _persist_verified_legs(session_factory, *, signal_id, draft, leg_indices):
    with session_factory() as session:
        signal = session.get(TradeSignal, signal_id)
        binding = ExecutionBinding(
            strategy_instance_id=signal.strategy_instance_id,
            kol_id=signal.kol_id,
            chat_id=signal.chat_id,
            message_id=signal.message_id,
            symbol=signal.symbol,
            side=signal.side,
            venue="deepcoin",
            order_id=":".join(f"ORDER-{index}" for index in leg_indices),
            client_order_id=":".join(f"LEG-{index}" for index in leg_indices),
            status="open",
            payload_json=json.dumps({"draft": draft}),
        )
        session.add(binding)
        session.flush()
        for index in leg_indices:
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=signal.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=f"ORDER-{index}",
                    client_order_id=f"LEG-{index}",
                    venue="deepcoin",
                    attribution_status="verified",
                    status="open",
                )
            )
        signal.status = "submitted"
        signal.result_json = json.dumps({"submitted": True})
        session.commit()
        return binding.id


def _contract(session_factory):
    with session_factory() as session:
        row = session.query(InstructionExecutionContract).one()
        session.expunge(row)
        return row


def test_pending_entry_can_be_projected_as_deferred(tmp_path):
    session_factory = create_session_factory(tmp_path / "deferred.db")
    item_id, _, _ = _persist_chain(session_factory)

    result = project_entry_deferred_contract(
        session_factory,
        message_instruction_item_id=item_id,
        reason_code="adjacent_entry_context_pending",
        blocker_ids=(17,),
        deadline_at=NOW,
        recheck_fingerprint="f" * 64,
        projected_at=NOW,
        mode="shadow",
    )

    assert result.state == "deferred"
    assert _contract(session_factory).attempted_exchange_write is False
    with session_factory() as session:
        evidence = json.loads(
            session.query(InstructionExecutionContract).one().evidence_refs_json
        )[0]
        assert evidence["raw_message_ids"] == [17]
        assert evidence["deadline_at"] == NOW.isoformat()
        assert evidence["recheck_fingerprint"] == "f" * 64


def test_successful_one_leg_submission_becomes_verified_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "verified.db")
    item_id, signal_id, draft = _persist_chain(session_factory)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    binding_id = _persist_verified_legs(
        session_factory, signal_id=signal_id, draft=draft, leg_indices=(1,)
    )

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=1,
        confirmed_legs=1,
        projected_at=NOW,
        mode="shadow",
    )

    contract = _contract(session_factory)
    assert projection.state == "verified"
    assert contract.terminal_kind == "verified_entry"
    assert contract.completion_scope == "full"
    assert contract.execution_binding_id == binding_id


def test_pre_submit_failure_with_zero_writes_is_failed(tmp_path):
    session_factory = create_session_factory(tmp_path / "failed.db")
    item_id, signal_id, draft = _persist_chain(session_factory)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    with session_factory() as session:
        signal = session.get(TradeSignal, signal_id)
        signal.status = "failed"
        signal.last_error = "validation rejected"
        session.commit()

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=0,
        confirmed_legs=0,
        error=ValueError("validation rejected"),
        projected_at=NOW,
        mode="shadow",
    )

    assert projection.state == "failed"
    assert _contract(session_factory).attempted_exchange_write is True


def test_ambiguous_exchange_outcome_becomes_submit_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown.db")
    item_id, signal_id, draft = _persist_chain(session_factory)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    with session_factory() as session:
        session.get(TradeSignal, signal_id).status = "unknown_exchange_outcome"
        session.commit()

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=1,
        confirmed_legs=0,
        error=RuntimeError("response lost"),
        projected_at=NOW,
        mode="shadow",
    )

    assert projection.state == "submit_unknown"
    with pytest.raises(EntryExecutionContractBlocked, match="submit_unknown"):
        prepare_entry_submission_contract(
            session_factory,
            message_instruction_item_id=item_id,
            trade_signal_id=signal_id,
            draft=draft,
            prepared_at=NOW,
            mode="shadow",
        )


def test_existing_submitting_contract_never_reenters_writer(tmp_path):
    session_factory = create_session_factory(tmp_path / "submitting-retry.db")
    item_id, signal_id, draft = _persist_chain(session_factory)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )

    with pytest.raises(EntryExecutionContractBlocked, match="reconciliation"):
        prepare_entry_submission_contract(
            session_factory,
            message_instruction_item_id=item_id,
            trade_signal_id=signal_id,
            draft=draft,
            prepared_at=NOW,
            mode="shadow",
        )


def test_stale_same_index_binding_with_different_client_id_stays_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-binding.db")
    item_id, signal_id, draft = _persist_chain(session_factory)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    _persist_verified_legs(
        session_factory, signal_id=signal_id, draft=draft, leg_indices=(1,)
    )
    with session_factory() as session:
        session.query(ExecutionOrderLeg).one().client_order_id = "OLD-LEG-1"
        session.get(TradeSignal, signal_id).status = "unknown_exchange_outcome"
        session.commit()

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=1,
        confirmed_legs=0,
        error=RuntimeError("response lost"),
        projected_at=NOW,
        mode="shadow",
    )

    assert projection.state == "submit_unknown"


def test_two_verified_legs_require_exact_durable_leg_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "two-legs.db")
    item_id, signal_id, draft = _persist_chain(session_factory, leg_count=2)
    prepared = prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    assert prepared.draft_fingerprint == hashlib.sha256(
        json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _persist_verified_legs(
        session_factory, signal_id=signal_id, draft=draft, leg_indices=(1, 2)
    )

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=2,
        confirmed_legs=2,
        projected_at=NOW,
        mode="shadow",
    )

    assert projection.state == "verified"
    assert projection.verified_leg_indices == (1, 2)
    assert projection.incident_facts == ()


def test_confirmed_absent_second_leg_allows_verified_partial_with_fact(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial.db")
    item_id, signal_id, draft = _persist_chain(session_factory, leg_count=2)
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="shadow",
    )
    _persist_verified_legs(
        session_factory, signal_id=signal_id, draft=draft, leg_indices=(1,)
    )

    projection = project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=1,
        confirmed_legs=1,
        confirmed_absent_leg_indices=(2,),
        projected_at=NOW,
        mode="shadow",
    )

    assert projection.state == "verified"
    assert projection.completion_scope == "partial"
    assert projection.incident_facts == ("multi_leg_partial",)
    assert _contract(session_factory).completion_scope == "partial"
