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
    TradeSignal,
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
    message_id=9974,
    execution_status=None,
    leg_count=1,
):
    strategy_id = f"deepcoin:100:{message_id}:BTC:long"
    draft = {
        "instrument_id": "BTC-USDT-SWAP",
        "strategy_instance_id": strategy_id,
        "selected_entry_leg_indices": list(range(1, leg_count + 1)),
        "order_legs": [
            {
                "client_order_id": f"LEG-{index}",
                "order_type": "market" if index == 1 else "limit",
            }
            for index in range(1, leg_count + 1)
        ],
    }
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=message_id, text="BTC long")
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
            idempotency_key=f"{message_id:064d}"[-64:],
        )
        session.add(item)
        session.flush()
        signal = TradeSignal(
            signal_uid=f"reconcile:{message_id}",
            strategy_instance_id=strategy_id,
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="chen",
            chat_id=100,
            message_id=message_id,
            symbol="BTC",
            side="long",
            action="open_position",
            status="submit_unknown" if state == "submit_unknown" else state,
            payload_json=json.dumps({"deepcoin_order_draft": draft}),
        )
        session.add(signal)
        session.flush()
        binding = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=strategy_id,
                kol_id="chen",
                chat_id=100,
                message_id=message_id,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="active" if pos_id else "open",
                payload_json=json.dumps({"draft": draft}),
            )
            session.add(binding)
            session.flush()
            for index in range(1, leg_count + 1):
                session.add(ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=strategy_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="market" if index == 1 else "trigger_limit",
                    order_id=f"ORDER-{index}",
                    client_order_id=f"LEG-{index}",
                    pos_id=pos_id if index == 1 else None,
                    venue="deepcoin",
                    attribution_status=(
                        attribution_status if index == 1 else "unassigned"
                    ),
                    status=execution_status or ("active" if pos_id else "open"),
                ))
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=message_id,
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
            terminal_kind="verified_entry" if state == "verified" else None,
            completion_scope="full" if state == "verified" else None,
            attempted_exchange_write=attempted_write,
            trade_signal_id=signal.id,
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
        order_history=[{"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "filled"}]
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

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert result.transitioned == 0


def test_durable_terminal_leg_absence_can_fail_unknown_contract(tmp_path):
    session_factory = create_session_factory(tmp_path / "durable-absent.db")
    contract_id = _persist_case(
        session_factory,
        attempted_write=True,
        execution_status="cancelled",
    )

    result = _reconcile(session_factory, _ReadOnlyClient())

    assert _contract(session_factory, contract_id) == "failed"
    assert result.transitioned == 1


def test_partial_multi_leg_visibility_remains_submit_unknown(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial-visible.db")
    contract_id = _persist_case(session_factory, leg_count=2)
    client = _ReadOnlyClient(
        order_history=[
            {"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "filled"}
        ]
    )

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert "multi_leg_partial" in {fact.code for fact in result.facts}


def test_bindingless_unknown_uses_trade_signal_draft_and_recovers_exact_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "bindingless.db")
    contract_id = _persist_case(session_factory, with_binding=False)
    client = _ReadOnlyClient(
        order_history=[
            {"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "open"}
        ]
    )

    result = _reconcile(session_factory, client)

    assert _contract(session_factory, contract_id) == "verified"
    assert result.transitioned == 1
    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        binding = session.get(ExecutionBinding, contract.execution_binding_id)
        legs = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding.id
        ).all()
        lifecycle = session.query(StrategyLifecycle).filter_by(message_id=9974).one()
        assert binding.client_order_id == "LEG-1"
        assert [(leg.leg_index, leg.client_order_id) for leg in legs] == [(1, "LEG-1")]
        assert lifecycle.execution_binding_id == binding.id


def test_existing_pre_submit_binding_is_completed_before_contract_verification(tmp_path):
    session_factory = create_session_factory(tmp_path / "existing-incomplete.db")
    contract_id = _persist_case(session_factory, with_binding=True)
    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=contract.execution_binding_id
        ).one()
        lifecycle = session.query(StrategyLifecycle).filter_by(message_id=9974).one()
        leg.order_id = None
        leg.status = "submitting"
        lifecycle.execution_binding_id = None
        session.commit()

    _reconcile(
        session_factory,
        _ReadOnlyClient(
            order_history=[
                {"ordId": "ORDER-READBACK", "clOrdId": "LEG-1", "state": "filled"}
            ]
        ),
    )

    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        binding = session.get(ExecutionBinding, contract.execution_binding_id)
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding.id
        ).one()
        lifecycle = session.query(StrategyLifecycle).filter_by(message_id=9974).one()
        assert contract.state == "verified"
        assert leg.order_id == "ORDER-READBACK"
        assert leg.status == "filled"
        assert lifecycle.execution_binding_id == binding.id


def test_binding_persistence_collision_cannot_verify_incomplete_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "binding-collision.db")
    contract_id = _persist_case(session_factory, with_binding=True)
    with session_factory() as session:
        other = ExecutionBinding(
            strategy_instance_id="deepcoin:200:300:ETH:long",
            kol_id="other",
            chat_id=200,
            message_id=300,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="active",
        )
        session.add(other)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=other.id,
                strategy_instance_id=other.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                client_order_id="OTHER-LEG",
                pos_id="POS-COLLISION",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
        )
        session.commit()

    result = _reconcile(
        session_factory,
        _ReadOnlyClient(
            order_history=[
                {
                    "ordId": "ORDER-1",
                    "clOrdId": "LEG-1",
                    "posId": "POS-COLLISION",
                    "state": "filled",
                }
            ]
        ),
    )

    assert _contract(session_factory, contract_id) == "submit_unknown"
    assert result.transitioned == 0
    assert "exchange_snapshot_incomplete" in {fact.code for fact in result.facts}


def test_exact_rejected_order_is_verified_refusal_not_verified_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "rejected.db")
    contract_id = _persist_case(session_factory, with_binding=False)
    client = _ReadOnlyClient(
        order_history=[
            {"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "rejected"}
        ]
    )

    result = _reconcile(session_factory, client)

    assert result.transitioned == 1
    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        assert contract.state == "verified"
        assert contract.terminal_kind == "verified_refusal"
        assert contract.execution_binding_id is None


def test_exact_rejected_order_terminalizes_existing_binding_and_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "rejected-existing.db")
    contract_id = _persist_case(session_factory, with_binding=True)

    _reconcile(
        session_factory,
        _ReadOnlyClient(
            order_history=[
                {"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "rejected"}
            ]
        ),
    )

    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        binding = session.get(ExecutionBinding, contract.execution_binding_id)
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding.id
        ).one()
        assert contract.terminal_kind == "verified_refusal"
        assert binding.status == "rejected"
        assert leg.status == "rejected"


def test_exact_positive_and_refused_legs_converge_as_verified_partial(tmp_path):
    session_factory = create_session_factory(tmp_path / "mixed-exact.db")
    contract_id = _persist_case(session_factory, with_binding=True, leg_count=2)
    result = _reconcile(
        session_factory,
        _ReadOnlyClient(
            order_history=[
                {"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "filled"},
                {"ordId": "ORDER-2", "clOrdId": "LEG-2", "state": "rejected"},
            ]
        ),
    )

    assert result.transitioned == 1
    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        legs = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=contract.execution_binding_id
        ).order_by(ExecutionOrderLeg.leg_index).all()
        assert contract.state == "verified"
        assert contract.terminal_kind == "verified_entry"
        assert contract.completion_scope == "partial"
        assert [leg.status for leg in legs] == ["filled", "rejected"]


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


def test_readback_contracts_are_not_starved_by_old_pending_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "priority.db")
    for offset in range(3):
        _persist_case(
            session_factory,
            state="pending",
            attempted_write=False,
            with_binding=False,
            message_id=9_800 + offset,
        )
    contract_id = _persist_case(
        session_factory,
        state="submit_unknown",
        message_id=9_900,
    )

    result = _reconcile(
        session_factory,
        _ReadOnlyClient(order_history=[{"ordId": "ORDER-1", "clOrdId": "LEG-1", "state": "filled"}]),
        limit=1,
    )

    assert result.checked == 1
    assert _contract(session_factory, contract_id) == "verified"


def test_incomplete_unknown_contracts_rotate_within_actionable_queue(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-fairness.db")
    first_id = _persist_case(session_factory, message_id=9_910)
    second_id = _persist_case(session_factory, message_id=9_911)
    client = _ReadOnlyClient(fail_source="order_history")

    first = _reconcile(session_factory, client, limit=1)
    second = _reconcile(session_factory, client, limit=1)

    assert [first.facts[0].contract_id, second.facts[0].contract_id] == [
        first_id,
        second_id,
    ]


def test_unknown_contracts_do_not_starve_stale_submitting_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "cross-state-fairness.db")
    unknown_id = _persist_case(session_factory, message_id=9_920)
    submitting_id = _persist_case(
        session_factory,
        message_id=9_921,
        state="submitting",
    )
    client = _ReadOnlyClient(fail_source="order_history")

    first = _reconcile(session_factory, client, limit=1)
    second = _reconcile(session_factory, client, limit=1)

    assert first.facts[0].contract_id == unknown_id
    assert {fact.contract_id for fact in second.facts} == {submitting_id}
