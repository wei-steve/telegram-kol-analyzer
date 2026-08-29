from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import telegram_kol_research.auto_trade_execution as auto_trade_execution_module

from telegram_kol_research.auto_trade_execution import (
    execute_message_instruction_items,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.entry_assembly_admission import (
    claim_ready_entry_assembly_wakeups,
    finish_entry_assembly_wakeup,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.instruction_execution_contracts import (
    load_or_create_instruction_execution_contract,
)
from telegram_kol_research.instruction_execution_entry_adapter import (
    EntryExecutionContractBlocked,
    prepare_entry_submission_contract,
    project_entry_deferred_contract,
)
from telegram_kol_research.message_instruction_items import (
    claim_next_message_instruction_item,
)
from telegram_kol_research.instruction_execution_reconciliation import (
    reconcile_instruction_execution_contracts,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    ExecutionBinding,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    PositionProtectionLedger,
    RawMessage,
    SignalCandidate,
    TradeSignal,
)
from telegram_kol_research.recovery_live_submit import (
    RecoveryLiveSubmitError,
    enqueue_recovery_trade_signal,
    process_trade_signal_live,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _seed_entry_exchange_authority(session_factory) -> None:
    result = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
    )
    assert result.seeded is True


class _CrashInjected(RuntimeError):
    pass


class _FaultExchange:
    def __init__(self, *, positions=(), readback_rows=(), incomplete_readback=False):
        self.write_client_order_ids: list[str] = []
        self.positions = list(positions)
        self.readback_rows = list(readback_rows)
        self.incomplete_readback = incomplete_readback
        self.mutation_calls = 0

    def place_order(self, *, client_order_id: str, lose_response: bool = False):
        self.mutation_calls += 1
        self.write_client_order_ids.append(client_order_id)
        if lose_response:
            raise _CrashInjected("response lost after HTTP send")
        return {"code": "0", "data": {"clOrdId": client_order_id}}

    def list_positions(self):
        if self.incomplete_readback:
            raise _CrashInjected("readback interrupted")
        return list(self.positions)

    def list_open_orders(self):
        return []

    def list_order_history(self, *, inst_id):
        return list(self.readback_rows)

    def list_trade_fills(self, *, inst_id):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return []

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": []}


def _persist_entry_chain(session_factory, *, leg_count=1):
    strategy_id = "deepcoin:100:9974:BTC:long"
    draft = {
        "instrument_id": "BTC-USDT-SWAP",
        "strategy_instance_id": strategy_id,
        "selected_entry_leg_indices": list(range(1, leg_count + 1)),
        "order_legs": [
            {
                "order_type": "market" if index == 1 else "limit",
                "client_order_id": f"CRASH-LEG-{index}",
                "risk_budget_usdt": 10,
            }
            for index in range(1, leg_count + 1)
        ],
    }
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=9974,
            text="redacted entry",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            strategy_instance_id=strategy_id,
            idempotency_key="f" * 64,
            execution_deadline_at=NOW + timedelta(minutes=5),
        )
        session.add(item)
        session.flush()
        signal = TradeSignal(
            signal_uid="fault-injection-entry",
            strategy_instance_id=strategy_id,
            source_type="message_instruction",
            venue="deepcoin",
            kol_id="redacted",
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
        item_id, signal_id = int(item.id), int(signal.id)
    load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=NOW,
        deadline_at=NOW + timedelta(minutes=5),
    )
    return item_id, signal_id, draft


def _prepare(session_factory, *, item_id, signal_id, draft):
    return prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="live",
    )


def _contract(session_factory):
    with session_factory() as session:
        row = session.query(InstructionExecutionContract).one()
        return row.state, row.attempted_exchange_write


def _persist_real_writer_case(session_factory, *, market=False):
    from test_recovery_live_submit import (
        _StaticContractSpecProvider,
        _finalize_v2_assembly_for_signal,
        _persist_finalized_signal_evidence,
        _persist_ready_item,
        _persist_ready_market_item,
    )

    if market:
        _persist_ready_market_item(session_factory)
        identity = {"chat_id": 200, "message_id": 66, "symbol": "BTC", "side": "short"}
    else:
        _persist_ready_item(session_factory)
        identity = {"chat_id": 100, "message_id": 55, "symbol": "BTC", "side": "long"}
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        contract_spec_provider=_StaticContractSpecProvider(),
        **identity,
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(
            chat_id=identity["chat_id"], message_id=identity["message_id"]
        ).one()
        candidate = session.query(SignalCandidate).filter_by(raw_message_id=raw.id).one()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            strategy_instance_id=signal.strategy_instance_id,
            idempotency_key=("m" if market else "l") * 64,
            execution_deadline_at=NOW + timedelta(minutes=5),
        )
        session.add(item)
        session.commit()
        item_id = int(item.id)
    load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=NOW,
        deadline_at=NOW + timedelta(minutes=5),
    )
    return signal, item_id, _StaticContractSpecProvider()


def _run_real_writer(session_factory, *, signal, item_id, client, provider):
    return process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=provider,
        message_instruction_item_id=item_id,
        execution_contract_mode="live",
        processed_at=NOW,
        writer_boundary_at=NOW,
    )


def _assert_real_writer_restart_blocked(
    session_factory, *, signal, item_id, client, provider, call_count
):
    with pytest.raises(RecoveryLiveSubmitError, match="trade_signal_claim_failed"):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )
    assert len(client.trigger_payloads) + len(client.payloads) == call_count


def test_worker_start_before_deadline_cannot_write_after_boundary_expiry(tmp_path):
    from test_recovery_live_submit import _FakeDeepcoinClient

    session_factory = create_session_factory(tmp_path / "boundary-expired.db")
    _seed_entry_exchange_authority(session_factory)
    signal, item_id, provider = _persist_real_writer_case(session_factory)
    client = _FakeDeepcoinClient()

    with pytest.raises(EntryExecutionContractBlocked, match="deadline_expired"):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=provider,
            message_instruction_item_id=item_id,
            execution_contract_mode="live",
            processed_at=NOW,
            writer_boundary_at=NOW + timedelta(minutes=6),
        )

    assert client.trigger_payloads == client.payloads == []
    assert _contract(session_factory) == ("expired", False)


def test_expired_claimed_item_finishes_terminal_and_cannot_be_reclaimed(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "expired-item-mirror.db")
    item_id, signal_id, draft = _persist_entry_chain(session_factory)
    project_entry_deferred_contract(
        session_factory,
        message_instruction_item_id=item_id,
        reason_code="adjacent_entry_context_pending",
        blocker_ids=(17,),
        deadline_at=NOW + timedelta(minutes=5),
        projected_at=NOW,
        mode="live",
    )
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_entry_after_item_id": 0,
        },
    )
    writer_calls = []

    def blocked_before_writer(*args, **kwargs):
        prepare_entry_submission_contract(
            session_factory,
            message_instruction_item_id=item_id,
            trade_signal_id=signal_id,
            draft=draft,
            prepared_at=NOW + timedelta(minutes=6),
            mode="live",
        )
        writer_calls.append("called")

    monkeypatch.setattr(
        auto_trade_execution_module,
        "_auto_process_single_message_trade_signal",
        blocked_before_writer,
    )
    with session_factory() as session:
        raw_message_id = session.get(MessageInstructionItem, item_id).raw_message_id

    execute_message_instruction_items(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=object(),
        deepcoin_client=None,
        processed_at=NOW,
    )

    assert writer_calls == []
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "failed"
        assert session.query(InstructionExecutionContract).one().state == "expired"
    assert claim_next_message_instruction_item(
        session_factory,
        raw_message_id=raw_message_id,
        now=NOW + timedelta(minutes=7),
    ) is None


def test_crash_before_contract_transition_never_reaches_real_writer(
    tmp_path, monkeypatch
):
    import telegram_kol_research.instruction_execution_entry_adapter as adapter
    from test_recovery_live_submit import _FakeDeepcoinClient

    session_factory = create_session_factory(tmp_path / "before-transition.db")
    signal, item_id, provider = _persist_real_writer_case(session_factory)
    client = _FakeDeepcoinClient()
    monkeypatch.setattr(
        adapter,
        "transition_instruction_execution_contract",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _CrashInjected("before contract transition")
        ),
    )

    with pytest.raises(EntryExecutionContractBlocked, match="entry_contract_pending"):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )

    assert client.trigger_payloads == client.payloads == []
    assert _contract(session_factory) == ("pending", False)
    _assert_real_writer_restart_blocked(
        session_factory,
        signal=signal,
        item_id=item_id,
        client=client,
        provider=provider,
        call_count=0,
    )


def test_crash_after_submitting_before_http_never_reaches_real_writer(
    tmp_path, monkeypatch
):
    import telegram_kol_research.recovery_live_submit as submitter
    from test_recovery_live_submit import _FakeDeepcoinClient

    session_factory = create_session_factory(tmp_path / "before-http.db")
    _seed_entry_exchange_authority(session_factory)
    signal, item_id, provider = _persist_real_writer_case(session_factory)
    client = _FakeDeepcoinClient()
    monkeypatch.setattr(
        submitter,
        "_submit_recovery_signal_direct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _CrashInjected("after submitting before HTTP")
        ),
    )

    with pytest.raises(_CrashInjected, match="before HTTP"):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )

    assert client.trigger_payloads == client.payloads == []
    assert _contract(session_factory) == ("failed", True)
    _assert_real_writer_restart_blocked(
        session_factory,
        signal=signal,
        item_id=item_id,
        client=client,
        provider=provider,
        call_count=0,
    )


def test_crash_after_http_send_before_response_is_quarantined_by_real_writer(
    tmp_path,
):
    from test_recovery_live_submit import _FakeDeepcoinClient

    class _LostResponseClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            raise DeepcoinRequestOutcomeUnknown("response lost after HTTP send")

    session_factory = create_session_factory(tmp_path / "lost-response.db")
    _seed_entry_exchange_authority(session_factory)
    signal, item_id, provider = _persist_real_writer_case(session_factory)
    client = _LostResponseClient()

    with pytest.raises(DeepcoinRequestOutcomeUnknown, match="response lost"):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )

    assert _contract(session_factory) == ("submit_unknown", True)
    _assert_real_writer_restart_blocked(
        session_factory,
        signal=signal,
        item_id=item_id,
        client=client,
        provider=provider,
        call_count=1,
    )
    assert len({row["clOrdId"] for row in client.trigger_payloads}) == 1


@pytest.mark.parametrize(
    ("boundary", "fault_target"),
    [
        ("after_accepted_response_before_local_commit", "record_parent"),
        ("between_first_and_second_entry_legs", "first_leg"),
    ],
)
def test_post_accept_crashes_do_not_resubmit_real_entry_writer(
    tmp_path, monkeypatch, boundary, fault_target
):
    import telegram_kol_research.recovery_live_submit as submitter
    from test_recovery_live_submit import _FakeDeepcoinClient

    session_factory = create_session_factory(tmp_path / f"{boundary}.db")
    _seed_entry_exchange_authority(session_factory)
    signal, item_id, provider = _persist_real_writer_case(session_factory)
    client = _FakeDeepcoinClient()
    if fault_target == "record_parent":
        monkeypatch.setattr(
            submitter,
            "record_trigger_protection_parent",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                _CrashInjected("accepted response before local commit")
            ),
        )
        expected_calls = 1
    else:
        original = submitter._submit_trigger_with_protection_intent

        def crash_after_first(*args, **kwargs):
            original(*args, **kwargs)
            raise _CrashInjected("between first and second entry legs")

        monkeypatch.setattr(
            submitter, "_submit_trigger_with_protection_intent", crash_after_first
        )
        expected_calls = 1

    with pytest.raises(Exception, match=(
        "accepted response before local commit"
        if fault_target == "record_parent"
        else "between first and second entry legs"
    )):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )

    assert _contract(session_factory) == ("submit_unknown", True)
    _assert_real_writer_restart_blocked(
        session_factory,
        signal=signal,
        item_id=item_id,
        client=client,
        provider=provider,
        call_count=expected_calls,
    )
    client_ids = [row["clOrdId"] for row in client.trigger_payloads]
    assert len(client_ids) == len(set(client_ids)) == expected_calls


def test_position_created_before_protection_ledger_commit_is_not_reentered(
    tmp_path, monkeypatch
):
    import telegram_kol_research.recovery_live_submit as submitter
    from test_recovery_live_submit import _FakeDeepcoinClient

    class _MarketPositionClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.position_reads = 0

        def place_order(self, order_payload):
            self.payloads.append(order_payload)
            return {
                "code": "0",
                "data": {"ordId": "market-order-1", "posId": "position-1"},
            }

        def list_positions(self, *, inst_id=None):
            self.position_reads += 1
            if self.position_reads == 1:
                return []
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "position-1",
                    "posSide": "short",
                    "pos": "1",
                    "avgPx": "59800",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                }
            ]

    session_factory = create_session_factory(tmp_path / "protection-ledger.db")
    _seed_entry_exchange_authority(session_factory)
    signal, item_id, provider = _persist_real_writer_case(
        session_factory, market=True
    )
    client = _MarketPositionClient()
    monkeypatch.setattr(
        submitter,
        "submit_exact_position_sltp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _CrashInjected("protection exchange response not committed")
        ),
    )
    monkeypatch.setattr(
        submitter,
        "_record_entry_protection_ledger_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _CrashInjected("after position before protection ledger commit")
        ),
    )

    with pytest.raises(_CrashInjected, match="protection ledger"):
        _run_real_writer(
            session_factory,
            signal=signal,
            item_id=item_id,
            client=client,
            provider=provider,
        )

    assert len(client.payloads) == 1
    assert _contract(session_factory) == ("verified", True)
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 1
        assert session.query(ExecutionOrderLeg).count() == 1
        assert session.query(PositionProtectionLedger).count() == 0
    _assert_real_writer_restart_blocked(
        session_factory,
        signal=signal,
        item_id=item_id,
        client=client,
        provider=provider,
        call_count=1,
    )


def test_restart_reconciliation_is_read_only_and_replayable(tmp_path):
    session_factory = create_session_factory(tmp_path / "restart-reconcile.db")
    item_id, signal_id, draft = _persist_entry_chain(session_factory)
    writer = _FaultExchange()
    _prepare(
        session_factory, item_id=item_id, signal_id=signal_id, draft=draft
    )
    with pytest.raises(_CrashInjected):
        writer.place_order(client_order_id="CRASH-LEG-1", lose_response=True)

    interrupted = _FaultExchange(incomplete_readback=True)
    first = reconcile_instruction_execution_contracts(
        session_factory,
        client=interrupted,
        reconciled_at=NOW + timedelta(seconds=1),
        mode="live",
    )
    completed = _FaultExchange(
        readback_rows=[
            {
                "clOrdId": "CRASH-LEG-1",
                "ordId": "ORDER-1",
                "state": "filled",
            }
        ]
    )
    second = reconcile_instruction_execution_contracts(
        session_factory,
        client=completed,
        reconciled_at=NOW + timedelta(seconds=2),
        mode="live",
    )

    assert first.transitioned == 0
    assert second.transitioned == 1
    assert interrupted.mutation_calls == completed.mutation_calls == 0
    assert writer.write_client_order_ids == ["CRASH-LEG-1"]
    assert _contract(session_factory) == ("verified", True)


def test_lost_wakeup_is_reclaimed_without_exchange_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "lost-wakeup.db")
    item_id, _, _ = _persist_entry_chain(session_factory)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.result_json = json.dumps(
            {"status": "deferred", "reason": "adjacent_entry_context_pending"}
        )
        item.visibility_next_attempt_at = NOW + timedelta(seconds=5)
        attempt = EntryAssemblyAttempt(
            strategy_raw_message_id=item.raw_message_id,
            signal_candidate_id=item.signal_candidate_id,
            candidate_generation="fault-fixture",
            cutoff_posted_at=NOW,
            cutoff_message_id=9975,
            cutoff_raw_message_id=item.raw_message_id,
            blocking_raw_message_ids_json="[999]",
            status="pending",
            fingerprint="a" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(attempt)
        session.commit()
        attempt_id = int(attempt.id)

    first_claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=999,
        now=NOW + timedelta(seconds=1),
    )[0]
    second_claim = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=123456,
        now=NOW + timedelta(minutes=6),
    )[0]
    finish_entry_assembly_wakeup(
        session_factory,
        attempt_id=second_claim.attempt_id,
        claim_token=second_claim.claim_token,
        succeeded=True,
        now=NOW + timedelta(minutes=6, seconds=1),
    )

    assert first_claim.attempt_id == second_claim.attempt_id == attempt_id
    assert first_claim.claim_token != second_claim.claim_token
    with session_factory() as session:
        attempt = session.get(EntryAssemblyAttempt, attempt_id)
        item = session.get(MessageInstructionItem, item_id)
        assert attempt.status == "woken"
        assert item.visibility_next_attempt_at is None
        assert session.query(ExecutionBinding).count() == 0
