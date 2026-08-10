import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class _ReadOnlyClient:
    def __init__(
        self,
        *,
        positions=None,
        open_orders=None,
        order_history=None,
        trade_fills=None,
        trigger_history=None,
        pending_triggers=None,
        fail_source=None,
    ):
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.order_history = order_history or []
        self.trade_fills = trade_fills or []
        self.trigger_history = trigger_history or []
        self.pending_triggers = pending_triggers or []
        self.fail_source = fail_source
        self.instruments = []
        self.mutation_calls = 0

    def _read(self, source, rows, inst_id=None):
        if source == self.fail_source:
            raise RuntimeError("read unavailable")
        if inst_id:
            self.instruments.append(inst_id)
        return list(rows)

    def list_positions(self):
        return self._read("positions", self.positions)

    def list_open_orders(self):
        return self._read("open_orders", self.open_orders)

    def list_order_history(self, *, inst_id):
        return self._read("order_history", self.order_history, inst_id)

    def list_trade_fills(self, *, inst_id):
        return self._read("trade_fills", self.trade_fills, inst_id)

    def list_trigger_order_history(self, *, inst_id):
        return self._read("trigger_history", self.trigger_history, inst_id)

    def read_trigger_orders_pending(self, *, inst_id):
        rows = self._read("pending_trigger_orders", self.pending_triggers, inst_id)
        return {"code": "0", "data": rows}

    def place_order(self, *args, **kwargs):
        self.mutation_calls += 1
        raise AssertionError("reconciler must remain read-only")

    def cancel_order(self, *args, **kwargs):
        self.mutation_calls += 1
        raise AssertionError("reconciler must remain read-only")

    def close_position(self, *args, **kwargs):
        self.mutation_calls += 1
        raise AssertionError("reconciler must remain read-only")


def _persist_case(
    session_factory,
    *,
    state="submit_unknown",
    attempted_write=True,
    with_binding=True,
    pos_id=None,
    attribution_status="unassigned",
    lifecycle_entered=False,
):
    strategy_id = "deepcoin:100:9974:BTC:long"
    draft = {
        "instrument_id": "BTC-USDT-SWAP",
        "strategy_instance_id": strategy_id,
        "selected_entry_leg_indices": [1],
        "order_legs": [{"client_order_id": "LEG-1"}],
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
            strategy_instance_id=strategy_id,
            idempotency_key="r" * 64,
        )
        session.add(item)
        session.flush()
        binding = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=strategy_id,
                kol_id="chen",
                chat_id=100,
                message_id=9974,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="active" if pos_id else "open",
                payload_json=json.dumps({"draft": draft}),
            )
            session.add(binding)
            session.flush()
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=strategy_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="ORDER-1",
                    client_order_id="LEG-1",
                    pos_id=pos_id,
                    venue="deepcoin",
                    attribution_status=attribution_status,
                    status="active" if pos_id else "open",
                )
            )
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=9974,
            symbol="BTC",
            side="long",
            lifecycle_status="entered" if lifecycle_entered else "pending_entry",
            signal_at=NOW - timedelta(hours=1),
            execution_binding_id=binding.id if binding else None,
        )
        session.add(lifecycle)
        contract = InstructionExecutionContract(
            message_instruction_item_id=item.id,
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=strategy_id,
            intent_kind="entry",
            state=state,
            state_version=1,
            attempted_exchange_write=attempted_write,
            execution_binding_id=binding.id if binding else None,
            deadline_at=NOW + timedelta(hours=1),
            last_progress_at=NOW - timedelta(minutes=10),
            created_at=NOW - timedelta(minutes=10),
            updated_at=NOW - timedelta(minutes=10),
        )
        session.add(contract)
        session.commit()
        return contract.id


def _contract(session_factory, contract_id):
    with session_factory() as session:
        return session.get(InstructionExecutionContract, contract_id).state


def _reconcile(session_factory, client, **kwargs):
    from telegram_kol_research.instruction_execution_reconciliation import (
        reconcile_instruction_execution_contracts,
    )

    return reconcile_instruction_execution_contracts(
        session_factory,
        client=client,
        reconciled_at=NOW,
        mode="shadow",
        **kwargs,
    )


def test_unknown_outcome_becomes_verified_from_exact_client_order_history(tmp_path):
    session_factory = create_session_factory(tmp_path / "exact-client.db")
    contract_id = _persist_case(session_factory)
    client = _ReadOnlyClient(
        order_history=[{"ordId": "ORDER-1", "clOrdId": "LEG-1"}]
    )

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "verified"
    assert result.transitioned == 1
    assert client.mutation_calls == 0
    assert set(client.instruments) == {"BTC-USDT-SWAP"}


def test_exact_verified_position_can_verify_unknown_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "exact-position.db")
    contract_id = _persist_case(
        session_factory,
        pos_id="POS-1",
        attribution_status="verified",
    )
    client = _ReadOnlyClient(positions=[{"posId": "POS-1", "instId": "BTC-USDT-SWAP"}])

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "verified"
    assert result.transitioned == 1
    assert client.mutation_calls == 0


@pytest.mark.parametrize("initial_state", ["submitting", "submit_unknown"])
def test_complete_absence_after_no_write_becomes_failed(tmp_path, initial_state):
    session_factory = create_session_factory(tmp_path / f"absent-{initial_state}.db")
    contract_id = _persist_case(
        session_factory,
        state=initial_state,
        attempted_write=False,
    )
    client = _ReadOnlyClient()

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "failed"
    assert result.transitioned == 1
    assert client.mutation_calls == 0


def test_unknown_outcome_later_confirmed_absent_becomes_failed(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-absent.db")
    contract_id = _persist_case(session_factory, attempted_write=True)

    result = _reconcile(session_factory, _ReadOnlyClient())

    assert _contract(session_factory, contract_id) == "failed"
    assert result.transitioned == 1


def test_duplicate_exact_rows_remain_submit_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "duplicate.db")
    contract_id = _persist_case(session_factory)
    client = _ReadOnlyClient(
        order_history=[
            {"ordId": "ORDER-1", "clOrdId": "LEG-1"},
            {"ordId": "OTHER-ORDER", "clOrdId": "LEG-1"},
        ]
    )

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert "exchange_evidence_duplicate" in {fact.code for fact in result.facts}
    assert client.mutation_calls == 0


def test_incomplete_exchange_snapshot_remains_submit_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "incomplete.db")
    contract_id = _persist_case(session_factory)
    client = _ReadOnlyClient(fail_source="order_history")

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert "exchange_snapshot_incomplete" in {fact.code for fact in result.facts}
    assert client.mutation_calls == 0


def test_stale_submitting_contract_emits_fact_and_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-submitting.db")
    contract_id = _persist_case(session_factory, state="submitting")
    client = _ReadOnlyClient()

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert "submitting_stale" in {fact.code for fact in result.facts}
    assert client.mutation_calls == 0


def test_local_contradiction_facts_are_bounded_and_read_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "contradictions.db")
    contract_id = _persist_case(
        session_factory,
        state="verified",
        with_binding=False,
        lifecycle_entered=True,
    )

    result = _reconcile(session_factory, _ReadOnlyClient())

    facts = {fact.code for fact in result.facts}
    assert "verified_without_binding" in facts
    assert "lifecycle_entered_without_binding" in facts
    assert all(fact.contract_id in {None, contract_id} for fact in result.facts)
    assert len(result.facts) <= 20
