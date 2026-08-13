import json
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecLookup
from telegram_kol_research.deepcoin_execution_operations import (
    DeepcoinOperationConflict,
    record_request_attempt,
    reserve_execution_operation,
)
from telegram_kol_research.deepcoin_request_policy import OutcomeCertainty
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.entry_strategy_assembly import (
    finalize_adjacent_entry_assembly_draft,
)
from telegram_kol_research.models import (
    DeepcoinAccountWriteGeneration,
    DeepcoinExecutionOperation,
    DeepcoinSnapshotEvidence,
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    PositionMutationIntent,
    RawMessage,
    SignalCandidate,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
)
from telegram_kol_research.models import SourceMessageDeletionExit, StrategyLifecycle, TradeSignal, TriggerTakeProfitConvergence
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import EntrySubmissionProgressError
from telegram_kol_research.recovery_live_submit import build_deepcoin_market_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_place_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payloads
from telegram_kol_research.recovery_live_submit import build_deepcoin_trigger_order_payload
from telegram_kol_research.recovery_live_submit import enqueue_recovery_trade_signal
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_live_submit import submit_entry_draft_revision_live
from telegram_kol_research.recovery_live_submit import load_entry_draft_revision_authority
from telegram_kol_research.recovery_live_submit import submit_strategy_revision_replacement_live
from telegram_kol_research.recovery_live_submit import _load_matching_position_ids
from telegram_kol_research.recovery_live_submit import _protected_entry_operation_key
from telegram_kol_research.recovery_live_submit import _submit_recovery_signal_direct
from telegram_kol_research.deepcoin_execution_actions import _exact_exchange_order_id
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import canonical_management_batch_id
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.source_message_deletion import record_source_message_deleted
from telegram_kol_research.strategy_threads import create_strategy_thread_for_lifecycle


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _StaleCapabilityProvider:
    snapshot = SimpleNamespace(
        source_digest_sha256="c" * 64,
        fetched_at=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
    )

    def lookup_contract_spec(self, instrument_id):
        return DeepcoinContractSpecLookup(
            instrument_id=instrument_id,
            reason="contract_spec_stale",
        )

    def get_contract_spec(self, instrument_id):
        return None


def test_pending_entry_update_requires_an_exact_exchange_order_id():
    assert _exact_exchange_order_id({"ordId": "trigger-123", "clOrdId": "client-123"}) == "trigger-123"
    assert _exact_exchange_order_id({"clOrdId": "client-123"}) is None
    assert _exact_exchange_order_id({"id": "internal-123"}) is None


def test_entry_order_id_extractors_accept_only_their_endpoint_fields():
    from telegram_kol_research.recovery_live_submit import (
        _extract_exact_market_order_id,
        _extract_exact_trigger_order_id,
    )

    assert _extract_exact_market_order_id({"data": {"ordId": "market-1"}}) == "market-1"
    assert _extract_exact_market_order_id({"data": {"algoId": "algo-1"}}) is None
    assert _extract_exact_market_order_id({"data": {"orderId": "alias-1"}}) is None
    assert _extract_exact_trigger_order_id({"data": {"ordId": "trigger-1"}}) == "trigger-1"
    assert _extract_exact_trigger_order_id({"data": {"algoId": "algo-1"}}) is None
    assert _extract_exact_trigger_order_id(
        {"data": {"triggerOrderId": "trigger-1"}}
    ) is None


@pytest.mark.parametrize("payload", [None, [], "scalar", 42, 1.5, True])
def test_canonical_management_batch_id_rejects_non_mapping_payload(payload):
    assert canonical_management_batch_id(payload) is None


class _FakeDeepcoinClient:
    def __init__(self):
        self.payloads = []
        self.trigger_payloads = []
        self.protection_payloads = []
        self.position_protection_payloads = []
        self.cancel_payloads = []
        self.positions = []
        self.pending_tpsl = []

    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"order-{len(self.payloads)}"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"}}

    def set_position_sltp(self, protection_payload):
        self.position_protection_payloads.append(protection_payload)
        self.pending_tpsl.append(
            {
                "ordId": "sltp-1",
                "instId": protection_payload["instId"],
                "posId": protection_payload["posId"],
                "posSide": protection_payload["posSide"],
                **(
                    {"slTriggerPx": protection_payload["slTriggerPx"]}
                    if protection_payload.get("slTriggerPx") not in (None, "")
                    else {"tpTriggerPx": protection_payload["tpTriggerPx"]}
                ),
                "sz": protection_payload.get("sz", "0"),
            }
        )
        return {"code": "0", "data": {"ordId": "sltp-1"}}

    def replace_order_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

    def cancel_order(self, cancel_payload):
        self.cancel_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def list_positions(self, *, inst_id=None):
        return [
            {
                "avgPx": "64000",
                "mgnMode": "cross",
                "mrgPosition": "split",
                **row,
            }
            for row in self.positions
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            row for row in self.pending_tpsl
            if row["instId"] == inst_id
        ]

    def get_ticker_price(self, *, inst_id):
        return 68100.0


class _RecordingAllDeepcoinCalls(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def place_order(self, order_payload):
        self.calls.append("place_order")
        return super().place_order(order_payload)

    def trigger_order(self, order_payload):
        self.calls.append("trigger_order")
        return super().trigger_order(order_payload)

    def set_position_sltp(self, protection_payload):
        self.calls.append("set_position_sltp")
        return super().set_position_sltp(protection_payload)

    def replace_order_sltp(self, protection_payload):
        self.calls.append("replace_order_sltp")
        return super().replace_order_sltp(protection_payload)

    def cancel_order(self, cancel_payload):
        self.calls.append("cancel_order")
        return super().cancel_order(cancel_payload)

    def list_positions(self, *, inst_id=None):
        self.calls.append("list_positions")
        return super().list_positions(inst_id=inst_id)

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append("list_trigger_orders_pending")
        return super().list_trigger_orders_pending(inst_id=inst_id)


class _ProtectionFailingDeepcoinClient(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.positions = [
            {
                "posId": "pos-market-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "9",
            }
        ]

    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "order-market-1", "posId": "pos-market-1"}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        raise RuntimeError("missing_take_profit_for_protection")


class _InsufficientMoneyDeepcoinClient(_FakeDeepcoinClient):
    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        raise DeepcoinDefiniteRejection("Deepcoin API error 36: InsufficientMoney")


def test_market_entry_fails_before_submit_when_position_baseline_is_unavailable():
    class _UnavailableBaselineClient(_FakeDeepcoinClient):
        def list_positions(self, *, inst_id=None):
            raise TimeoutError("positions unavailable")

    client = _UnavailableBaselineClient()
    with pytest.raises(
        RecoveryLiveSubmitError,
        match="pre_submit_position_snapshot_unavailable",
    ):
        _load_matching_position_ids(
            client,
            draft={
                "instrument_id": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "position_mode": "split",
            },
            side="short",
        )
    assert client.payloads == []


class _OrderProtectionFailingDeepcoinClient(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.positions = [
            {
                "posId": "unrelated-pos",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "7",
                "avgPx": "64000",
                "mrgPosition": "split",
                "mgnMode": "cross",
            }
        ]

    def replace_order_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        raise DeepcoinClientError("order_not_open")


class _DelayedFilledPositionDeepcoinClient(_OrderProtectionFailingDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.position_calls = 0

    def list_positions(self, *, inst_id=None):
        self.position_calls += 1
        if self.position_calls == 1:
            return self.positions
        return [
            *self.positions,
            {
                "posId": "pos-filled-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "7",
                "avgPx": "64000",
                "mrgPosition": "split",
                "mgnMode": "cross",
            },
        ]


def _persist_ready_item(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_name="alice",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="BTC long 68000-68200 SL 67500 TP 69000 / 70000",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000 / 70000",
                parse_source="text",
                confidence=0.9,
            )
        )
        session.commit()
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )
    confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 0, tzinfo=UTC),
    )


def test_live_submit_revalidates_capability_before_exchange_or_binding_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-before-submit.db")
    _persist_ready_item(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "allowed_symbols": ["BTC", "ETH"]},
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(RecoveryLiveSubmitError, match="contract_spec_stale"):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaleCapabilityProvider(),
            processed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []
    assert client.protection_payloads == []
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 0


def test_shadow_entry_contract_observes_writer_without_changing_calls(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-contract-shadow.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(raw_message_id=raw.id).one()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            strategy_instance_id=signal.strategy_instance_id,
            idempotency_key="e" * 64,
        )
        session.add(item)
        session.commit()
        item_id = item.id
    client = _FakeDeepcoinClient()
    expected_calls = len(signal.payload["deepcoin_order_draft"]["order_legs"])

    result = process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        message_instruction_item_id=item_id,
        execution_contract_mode="shadow",
        processed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )

    assert result["order_count"] == expected_calls
    assert len(client.trigger_payloads) == expected_calls
    with session_factory() as session:
        contract = session.query(InstructionExecutionContract).one()
        assert contract.state == "verified"
        assert contract.trade_signal_id == signal.id
        assert contract.execution_binding_id is not None


def test_entry_draft_revision_uses_existing_audited_writer(tmp_path, monkeypatch):
    import telegram_kol_research.recovery_live_submit as submit_module
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-wrapper.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
    )
    calls = []
    monkeypatch.setattr(
        submit_module,
        "submit_strategy_revision_replacement_live",
        lambda *args, **kwargs: calls.append(kwargs) or {"status": "submitted"},
    )

    result = submit_entry_draft_revision_live(
        session_factory,
        batch_id=batch_id,
        original_draft=original,
        operation="market_first_leg",
        market_price=Decimal("68100"),
        authorized_leg_indices=(1,),
        expected_parent_fingerprint=deepcoin_order_draft_fingerprint(original),
        deepcoin_client=object(),
    )

    assert result == {"status": "submitted"}
    assert calls[0]["batch_id"] == batch_id
    assert len(calls[0]["draft"]["order_legs"]) == 2
    assert calls[0]["draft"]["order_legs"][1] == original["order_legs"][1]
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        assert batch.status == "reconciling"
        assert batch.advance_claim_token is None
        assert json.loads(batch.replacement_response_json) == result


def test_entry_draft_revision_rejects_file_that_differs_from_binding_authority(tmp_path):
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-authority.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
    )
    tampered = json.loads(json.dumps(original))
    tampered["risk_budget_usdt"] = 999

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="entry_draft_parent_fingerprint_changed",
    ):
        submit_entry_draft_revision_live(
            session_factory,
            batch_id=batch_id,
            original_draft=tampered,
            operation="market_first_leg",
            market_price=Decimal("68100"),
            authorized_leg_indices=(1,),
            expected_parent_fingerprint=deepcoin_order_draft_fingerprint(tampered),
            deepcoin_client=object(),
        )


def test_entry_draft_revision_rejects_unknown_unmodified_leg(tmp_path):
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-unknown-leg.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
        unknown_leg_index=2,
    )

    with pytest.raises(RecoveryLiveSubmitError, match="revision_leg_outcome_unknown"):
        submit_entry_draft_revision_live(
            session_factory,
            batch_id=batch_id,
            original_draft=original,
            operation="market_first_leg",
            market_price=Decimal("68100"),
            authorized_leg_indices=(1,),
            expected_parent_fingerprint=deepcoin_order_draft_fingerprint(original),
            deepcoin_client=object(),
        )


def test_entry_draft_revision_uses_original_client_ids_after_prior_replacement(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submit_module
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-lineage.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
    )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=batch.execution_binding_id,
                strategy_instance_id=original["strategy_instance_id"],
                leg_index=3,
                purpose="entry",
                order_kind="market",
                order_id="replacement-order-1",
                client_order_id="replacement-client-1",
                venue="deepcoin",
                attribution_status="unassigned",
                status="open",
            )
        )
        session.commit()
    monkeypatch.setattr(
        submit_module,
        "submit_strategy_revision_replacement_live",
        lambda *args, **kwargs: {"status": "submitted"},
    )

    result = submit_entry_draft_revision_live(
        session_factory,
        batch_id=batch_id,
        original_draft=original,
        operation="market_first_leg",
        market_price=Decimal("68100"),
        authorized_leg_indices=(1,),
        expected_parent_fingerprint=deepcoin_order_draft_fingerprint(original),
        deepcoin_client=object(),
    )

    assert result["status"] == "submitted"


def test_entry_draft_revision_rejects_original_leg_client_id_mismatch(tmp_path):
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-lineage-mismatch.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
    )
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all()
        legs[0].client_order_id = "wrong-parent-client"
        session.commit()

    with pytest.raises(RecoveryLiveSubmitError, match="revision_leg_authority_incomplete"):
        submit_entry_draft_revision_live(
            session_factory,
            batch_id=batch_id,
            original_draft=original,
            operation="market_first_leg",
            market_price=Decimal("68100"),
            authorized_leg_indices=(1,),
            expected_parent_fingerprint=deepcoin_order_draft_fingerprint(original),
            deepcoin_client=object(),
        )


@pytest.mark.parametrize(
    ("execution_status", "attribution_status"),
    [
        ("submitting", "verified"),
        ("open", "ambiguous"),
    ],
)
def test_entry_draft_revision_rejects_unverified_unmodified_leg(
    tmp_path,
    execution_status,
    attribution_status,
):
    from decimal import Decimal
    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    session_factory = create_session_factory(tmp_path / "revision-unverified-leg.db")
    batch_id, original = _persist_reserved_revision_batch(session_factory)
    _persist_revision_leg_authority(
        session_factory,
        batch_id=batch_id,
        draft=original,
        unmodified_leg_status=execution_status,
        unmodified_leg_attribution_status=attribution_status,
    )

    with pytest.raises(RecoveryLiveSubmitError, match="revision_leg_outcome_unknown"):
        submit_entry_draft_revision_live(
            session_factory,
            batch_id=batch_id,
            original_draft=original,
            operation="market_first_leg",
            market_price=Decimal("68100"),
            authorized_leg_indices=(1,),
            expected_parent_fingerprint=deepcoin_order_draft_fingerprint(original),
            deepcoin_client=object(),
        )


def test_entry_draft_revision_loads_missing_deadline_from_exact_contract(tmp_path):
    from telegram_kol_research.instruction_execution_contracts import (
        load_or_create_instruction_execution_contract,
    )

    session_factory = create_session_factory(tmp_path / "revision-deadline.db")
    batch_id, original = _persist_reserved_revision_batch(
        session_factory,
        include_deadline=False,
    )
    deadline = datetime(2099, 8, 10, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(raw_message_id=raw.id).one()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            strategy_instance_id=binding.strategy_instance_id,
            idempotency_key="deadline" * 8,
            execution_deadline_at=deadline,
        )
        session.add(item)
        session.commit()
        item_id = item.id
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        deadline_at=deadline,
    )
    with session_factory() as session:
        session.get(InstructionExecutionContract, contract.id).execution_binding_id = (
            session.get(StrategyRevisionBatch, batch_id).execution_binding_id
        )
        session.commit()

    authoritative, persisted_fingerprint = load_entry_draft_revision_authority(
        session_factory,
        batch_id=batch_id,
        supplied_draft=original,
    )

    assert authoritative["execution_deadline_at"] == deadline.isoformat()
    assert "execution_deadline_at" not in original
    assert persisted_fingerprint


def _finalize_v2_assembly_for_signal(session_factory, signal):
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        draft = payload["deepcoin_order_draft"]
        leg_count = len(draft["order_legs"])
        draft.setdefault(
            "selected_entry_leg_indices",
            list(range(1, leg_count + 1)),
        )
        draft.setdefault("selected_entry_leg_count", leg_count)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
        signal.payload = payload
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(
            chat_id=signal.chat_id,
            message_id=signal.message_id,
        ).one()
        candidate = session.query(SignalCandidate).filter_by(
            raw_message_id=raw.id
        ).one()
        evidence_json = json.dumps(
            {"mode": "live"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=str(signal.strategy_instance_id),
            risk_multiplier="1",
            evidence_json=evidence_json,
            fingerprint=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
        )
        session.add(assembly)
        session.commit()
        assembly_id = assembly.id
    draft = signal.payload["deepcoin_order_draft"]
    return finalize_adjacent_entry_assembly_draft(
        session_factory,
        assembly_id=assembly_id,
        order_draft=draft,
    )


def _persist_finalized_signal_evidence(session_factory, signal, finalized):
    evidence = {
        "assembly_id": finalized.assembly_id,
        "strategy_instance_id": finalized.strategy_instance_id,
        "assembly_fingerprint": finalized.final_fingerprint,
    }
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload["entry_preamble_assembly"] = dict(evidence)
        payload["deepcoin_order_draft"][
            "entry_preamble_assembly"
        ] = dict(evidence)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()


def _persist_lifecycle(
    session_factory,
    *,
    chat_id=100,
    message_id=55,
    symbol="BTC",
    side="long",
):
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side=side,
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 12, 8, 0),
                entry_range_low=68000.0,
                entry_range_high=68200.0,
                stop_loss=67500.0,
                take_profit="69000 / 70000",
            )
        )
        session.commit()


def _persist_ready_market_item(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=200,
            message_id=66,
            sender_name="bob",
            posted_at=datetime(2026, 6, 30, 8, 0),
            text="BTC 现价做空 59800 止损 61800 止盈 59000",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text="现价 59800",
                stop_loss_text="61800",
                take_profit_text="59000",
                parse_source="text_ai",
                confidence=0.95,
            )
        )
        session.commit()
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="bob",
                    chat_id=200,
                    message_id=66,
                    posted_at=datetime(2026, 6, 30, 8, 0),
                    symbol="BTC",
                    side="short",
                    entry_range=(59800.0, 59800.0),
                    stop_loss_text="61800",
                    take_profit_text="59000",
                    trading_mode="auto_trade",
                    max_loss_usdt=20.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["live_signal_auto_trade_market"],
                    entry_range=(59800.0, 59800.0),
                    max_loss_usdt=20.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 30, 8, 1, tzinfo=UTC),
    )
    confirm_recovery_order_dry_run(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 30, 8, 2, tzinfo=UTC),
    )


def _persist_reserved_revision_batch(session_factory, *, include_deadline=True):
    _persist_ready_item(session_factory)
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    draft = {**signal.payload["deepcoin_order_draft"]}
    if include_deadline:
        draft["execution_deadline_at"] = "2099-08-10T12:00:00+00:00"
    with session_factory() as session:
        session.delete(session.get(TradeSignal, signal.id))
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        binding = ExecutionBinding(
            strategy_instance_id=draft["strategy_instance_id"],
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="open",
            payload_json=json.dumps({"draft": draft}),
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 8, 8, 8, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
        raw_id = raw.id
        binding_id = binding.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    with session_factory() as session:
        batch = StrategyRevisionBatch(
            idempotency_fingerprint="revision" * 8,
            raw_message_id=raw_id,
            strategy_thread_id=thread.id,
            target_lifecycle_id=lifecycle_id,
            execution_binding_id=binding_id,
            status="submitting_replacements",
            replacement_json="{}",
            advance_claim_token="reserved",
            planned_at=datetime(2026, 8, 8, 8, tzinfo=UTC),
        )
        session.add(batch)
        session.commit()
        return batch.id, draft


def _persist_revision_leg_authority(
    session_factory,
    *,
    batch_id,
    draft,
    authorized_leg_indices=(1,),
    unknown_leg_index=None,
    unmodified_leg_status="open",
    unmodified_leg_attribution_status="unassigned",
):
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        for index, draft_leg in enumerate(draft["order_legs"], start=1):
            authorized = index in set(authorized_leg_indices)
            execution_leg = ExecutionOrderLeg(
                execution_binding_id=batch.execution_binding_id,
                strategy_instance_id=draft["strategy_instance_id"],
                leg_index=index,
                purpose="entry",
                order_kind=str(draft_leg.get("order_type") or "limit"),
                order_id=f"old-order-{index}",
                client_order_id=str(draft_leg["client_order_id"]),
                venue="deepcoin",
                status=(
                    "submit_unknown"
                    if index == unknown_leg_index
                    else "cancelled"
                    if authorized
                    else unmodified_leg_status
                ),
                attribution_status=(
                    "unassigned" if authorized else unmodified_leg_attribution_status
                ),
                request_json=json.dumps({"sz": draft_leg.get("quantity") or 1}),
            )
            session.add(execution_leg)
            session.flush()
            if authorized:
                session.add(StrategyRevisionLeg(
                    revision_batch_id=batch.id,
                    execution_order_leg_id=execution_leg.id,
                    action="cancel_pending",
                    prior_status="open",
                    status="cancelled",
                    order_id=execution_leg.order_id,
                    client_order_id=execution_leg.client_order_id,
                ))
        session.commit()


def _replace_queued_order_legs(session_factory, signal_id, order_legs):
    with session_factory() as session:
        signal = session.get(TradeSignal, signal_id)
        payload = json.loads(signal.payload_json)
        payload["deepcoin_order_draft"]["order_legs"] = order_legs
        signal.payload_json = json.dumps(payload)
        session.commit()
    return payload["deepcoin_order_draft"]


def test_build_deepcoin_place_order_payload_maps_limit_leg():
    payload = build_deepcoin_place_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 68100.0,
            "quantity": 83.0,
            "client_order_id": "client-1",
        },
    )

    assert payload == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "posSide": "long",
        "ordType": "limit",
        "px": "68100.0",
        "sz": "83.0",
        "clOrdId": "client-1",
        "mrgPosition": "split",
    }


def test_build_deepcoin_position_sltp_payload_allows_stop_loss_without_take_profit():
    payload = build_deepcoin_position_sltp_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 61800.0,
            "take_profit_legs": [],
            "order_legs": [{"position_side": "short"}],
        },
        pos_id="pos-btc-short",
    )

    assert payload == {
        "instType": "SWAP",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "mrgPosition": "split",
        "tdMode": "cross",
        "slTriggerPx": "61800.0",
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
        "posId": "pos-btc-short",
    }


def test_build_deepcoin_trigger_order_payload_is_stop_only_for_staged_take_profit():
    payload = build_deepcoin_trigger_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 67500.0,
            "take_profit_legs": [
                {"price": 69000.0, "allocation_pct": 50.0},
                {"price": 70000.0, "allocation_pct": 50.0},
            ],
            "order_legs": [{"position_side": "long"}],
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 68100.0,
            "quantity": 83.0,
            "client_order_id": "TKFG8248E1",
        },
    )

    assert payload["orderType"] == "limit"
    assert payload["triggerPrice"] == "68100.0"
    assert not any(key.startswith("tp") for key in payload)
    assert payload["slTriggerPx"] == "67500.0"
    assert payload["mrgPosition"] == "split"
    assert payload["clOrdId"] == "TKFG8248E1"


def test_build_deepcoin_market_order_and_position_sltp_payloads():
    draft = {
        "instrument_id": "BTC-USDT-SWAP",
        "margin_mode": "cross",
        "position_mode": "split",
        "stop_loss": 67500.0,
        "take_profit_legs": [{"price": 69000.0, "allocation_pct": 100.0}],
        "order_legs": [{"position_side": "long"}],
    }

    order_payload = build_deepcoin_market_order_payload(
        draft,
        {
            "side": "buy",
            "position_side": "long",
            "quantity": 83.0,
            "client_order_id": "client-1",
        },
    )
    protection_payload = build_deepcoin_position_sltp_payload(draft, pos_id="pos-1")

    assert order_payload["ordType"] == "market"
    assert "px" not in order_payload
    assert protection_payload["posId"] == "pos-1"
    assert protection_payload["tpOrdPx"] == "-1"
    assert protection_payload["slOrdPx"] == "-1"


def test_build_deepcoin_position_sltp_payloads_split_multi_take_profit_by_size():
    payloads = build_deepcoin_position_sltp_payloads(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 67500.0,
            "take_profit_legs": [
                {"price": 69000.0, "allocation_pct": 40.0},
                {"price": 70000.0, "allocation_pct": 30.0},
                {"price": 71000.0, "allocation_pct": 30.0},
            ],
            "order_legs": [{"position_side": "long"}],
            "contract_spec": {
                "instrument_id": "BTC-USDT-SWAP",
                "contract_value": 0.001,
                "quantity_step": 1.0,
                "min_quantity": 1.0,
                "price_tick": 0.1,
            },
        },
        pos_id="pos-1",
        position_size=83.0,
    )

    assert payloads == [
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "slTriggerPx": "67500.0",
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "69000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "33",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "70000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "24",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "71000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "26",
            "posId": "pos-1",
        },
    ]


def test_position_sltp_payloads_support_four_stage_btc_take_profits():
    payloads = build_deepcoin_position_sltp_payloads(
        {
            "instrument_id": "BTC-USDT-SWAP", "margin_mode": "cross",
            "position_mode": "split", "stop_loss": 61800.0,
            "take_profit_legs": [
                {"price": 67100.0, "allocation_pct": 40.0},
                {"price": 68500.0, "allocation_pct": 20.0},
                {"price": 70300.0, "allocation_pct": 20.0},
                {"price": 72000.0, "allocation_pct": 20.0},
            ],
            "order_legs": [{"position_side": "long"}],
            "contract_spec": {"quantity_step": 1.0, "min_quantity": 1.0},
        },
        pos_id="pos-4", position_size=25.0,
    )

    assert [payload["sz"] for payload in payloads[1:]] == ["10", "5", "5", "5"]
    assert [payload["tpTriggerPx"] for payload in payloads[1:]] == [
        "67100.0", "68500.0", "70300.0", "72000.0",
    ]


def test_position_sltp_payloads_reject_undersized_five_stage_position():
    with pytest.raises(RecoveryLiveSubmitError, match="minimum"):
        build_deepcoin_position_sltp_payloads(
            {
                "instrument_id": "ETH-USDT-SWAP", "margin_mode": "cross",
                "position_mode": "split", "stop_loss": 1800.0,
                "take_profit_legs": [
                    {"price": 1900.0, "allocation_pct": 40.0},
                    {"price": 1920.0, "allocation_pct": 15.0},
                    {"price": 1940.0, "allocation_pct": 15.0},
                    {"price": 1960.0, "allocation_pct": 15.0},
                    {"price": 1980.0, "allocation_pct": 15.0},
                ],
                "order_legs": [{"position_side": "long"}],
                "contract_spec": {"quantity_step": 0.1, "min_quantity": 0.1},
            },
            pos_id="pos-too-small", position_size=0.3,
        )


def test_submit_recovery_order_live_blocks_when_auto_trade_is_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)

    try:
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=_FakeDeepcoinClient(),
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    except RecoveryLiveSubmitError as exc:
        assert str(exc) == "auto_trade_disabled"
    else:
        raise AssertionError("expected disabled auto-trade to block live submit")


def test_submit_recovery_order_live_places_orders_and_persists_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert result["order_count"] == 2
    assert fake_client.payloads == []
    assert fake_client.protection_payloads == []
    assert fake_client.trigger_payloads[0]["tdMode"] == "cross"
    assert fake_client.trigger_payloads[0]["mrgPosition"] == "split"
    assert fake_client.trigger_payloads[0]["orderType"] == "limit"
    assert [payload["triggerPrice"] for payload in fake_client.trigger_payloads] == [
        "68290.0",
        "68090.0",
    ]
    assert all(not any(key.startswith("tp") for key in payload) for payload in fake_client.trigger_payloads)
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == "67500.0"
    assert fake_client.trigger_payloads[0]["slTriggerPxType"] == "last"
    assert fake_client.trigger_payloads[0]["slOrdPx"] == "-1"
    assert "posId" not in fake_client.trigger_payloads[0]
    assert fake_client.position_protection_payloads == []
    assert result["warnings"] == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index.asc()).all()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.order_id == "trigger-1,trigger-2"
    assert binding.client_order_id == "TK649760E806ACF61,TK729D11F4739D2A2"
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert lifecycle.execution_binding_id == binding.id
    assert [event.action for event in events] == [
        "create_trigger_entry",
        "create_trigger_entry",
    ]
    assert events[0].execution_binding_id == binding.id
    assert events[0].trade_signal_id == result["signal_id"]
    assert events[0].order_id == "trigger-1"
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "TK649760E806ACF61", "open"),
        (2, "trigger-2", "TK729D11F4739D2A2", "open"),
    ]
    assert {leg.execution_binding_id for leg in legs} == {binding.id}
    assert {leg.strategy_instance_id for leg in legs} == {"deepcoin:100:55:BTC:long"}


def test_entry_submit_rechecks_source_after_planning_before_exchange_write(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "delete-race.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()
    original_builder = build_deepcoin_trigger_order_payload
    deleted = False

    def delete_after_planning(draft, leg):
        nonlocal deleted
        payload = original_builder(draft, leg)
        if not deleted:
            deleted = True
            record_source_message_deleted(
                session_factory,
                chat_id=100,
                message_id=55,
            )
        return payload

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit.build_deepcoin_trigger_order_payload",
        delete_after_planning,
    )

    with pytest.raises(RecoveryLiveSubmitError, match="source_message_deleted"):
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert fake_client.trigger_payloads == []
    assert fake_client.payloads == []


def test_source_deletion_waits_until_exchange_identity_is_durably_ledgered(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "ledger-window.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    deletion_started = Event()
    deletion_finished = Event()
    deletion_thread = None

    class Client(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            nonlocal deletion_thread
            self.trigger_payloads.append(order_payload)
            if deletion_thread is None:
                def delete_source():
                    deletion_started.set()
                    record_source_message_deleted(
                        session_factory,
                        chat_id=100,
                        message_id=55,
                    )
                    deletion_finished.set()

                deletion_thread = Thread(target=delete_source)
                deletion_thread.start()
                assert deletion_started.wait(timeout=1)
            return {
                "code": "0",
                "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"},
            }

    original_normalize = __import__(
        "telegram_kol_research.recovery_live_submit",
        fromlist=["_normalized_trigger_order_id"],
    )._normalized_trigger_order_id

    def assert_deletion_is_still_serialized(response):
        assert not deletion_finished.wait(timeout=0.05)
        return original_normalize(response)

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit._normalized_trigger_order_id",
        assert_deletion_is_still_serialized,
    )

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=Client(),
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    deletion_thread.join(timeout=1)

    assert result["submitted"] is True
    assert deletion_finished.is_set()
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert deletion_exit.execution_binding_id == binding.id
        assert deletion_exit.strategy_instance_id == binding.strategy_instance_id


def test_process_next_trade_signal_live_consumes_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["signal_id"] == signal.id
    assert result["order_count"] == 2
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 1
        assert session.query(TradeSignal).filter_by(id=signal.id).one().status == "submitted"


def test_two_workers_atomically_claim_one_finalized_entry_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    client = _RecordingAllDeepcoinCalls()
    start = Barrier(2)
    results = []
    errors = []

    def worker():
        start.wait(timeout=2)
        try:
            results.append(
                process_trade_signal_live(
                    session_factory,
                    signal_id=signal.id,
                    deepcoin_client=client,
                    contract_spec_provider=_StaticContractSpecProvider(),
                )
            )
        except Exception as exc:
            errors.append(exc)

    workers = [Thread(target=worker), Thread(target=worker)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in workers)
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RecoveryLiveSubmitError)
    assert str(errors[0]).startswith("trade_signal_claim_failed:")
    assert len(client.trigger_payloads) == 2
    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "submitted"
        assert session.query(ExecutionBinding).count() == 1


def test_processing_signal_is_not_auto_reset_or_reexecuted_after_crash(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        session.get(TradeSignal, signal.id).status = "processing"
        session.commit()
    client = _RecordingAllDeepcoinCalls()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:processing$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.calls == []
    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "processing"


def test_v2_pre_submit_gate_failure_is_ordinary_failed_with_zero_exchange_calls(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(tmp_path / "pre-submit-failure.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    monkeypatch.setattr(
        submitter,
        "validate_recovery_live_submit_gate",
        lambda *args, **kwargs: {
            "would_submit": False,
            "reason_codes": ["pre_submit_blocked"],
        },
    )
    client = _RecordingAllDeepcoinCalls()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^live_submit_blocked:pre_submit_blocked$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.calls == []
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.last_error == "live_submit_blocked:pre_submit_blocked"


def test_v2_unknown_first_exchange_write_is_quarantined_without_retry(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-first-write.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)

    class _UnknownFirstWriteClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            raise DeepcoinRequestOutcomeUnknown("first leg outcome unknown")

    client = _UnknownFirstWriteClient()
    with pytest.raises(DeepcoinRequestOutcomeUnknown):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.trigger_payloads) == 1
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "unknown_exchange_outcome"
        assert row.last_error == "first leg outcome unknown"

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:unknown_exchange_outcome$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    assert len(client.trigger_payloads) == 1


def test_v2_generic_post_call_error_is_unknown_and_not_retried(tmp_path):
    session_factory = create_session_factory(tmp_path / "generic-post-call.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)

    class _GenericPostCallErrorClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            raise DeepcoinClientError("transport failed after trigger call")

    client = _GenericPostCallErrorClient()
    with pytest.raises(DeepcoinClientError, match="transport failed after trigger call"):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "unknown_exchange_outcome"
    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:unknown_exchange_outcome$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    assert len(client.trigger_payloads) == 1


def test_v2_embedded_trigger_missing_parent_identity_is_unknown_and_not_retried(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "embedded-missing-parent.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)

    class _MissingParentIdentityClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            return {"code": "0", "data": {"id": "generic-parent-id"}}

    client = _MissingParentIdentityClient()
    with pytest.raises(
        DeepcoinClientError,
        match="Deepcoin trigger order response missing order id",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "unknown_exchange_outcome"
    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:unknown_exchange_outcome$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    assert len(client.trigger_payloads) == 1


@pytest.mark.parametrize("ambiguous_id_field", ["id", "algoId", "triggerOrderId"])
def test_v2_market_ambiguous_id_is_not_a_confirmed_exchange_order(
    tmp_path,
    ambiguous_id_field,
):
    session_factory = create_session_factory(tmp_path / "market-generic-id.db")
    _persist_ready_market_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)

    class _GenericMarketIdClient(_FakeDeepcoinClient):
        def place_order(self, order_payload):
            self.payloads.append(order_payload)
            return {
                "code": "0",
                "data": {ambiguous_id_field: "ambiguous-market-id"},
            }

    client = _GenericMarketIdClient()
    with pytest.raises(
        DeepcoinRequestOutcomeUnknown,
        match="market order response missing exact order id",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.payloads) == 1
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "unknown_exchange_outcome"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(ExecutionOrderLeg).count() == 0


def test_fallback_trigger_generic_id_is_not_persisted_as_order_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "fallback-generic-id.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload["deepcoin_order_draft"]["order_legs"][0][
            "order_type"
        ] = "fallback_trigger"
        payload["deepcoin_order_draft"]["order_legs"] = payload[
            "deepcoin_order_draft"
        ]["order_legs"][:1]
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()

    class _GenericFallbackIdClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            return {"code": "0", "data": {"id": "generic-trigger-id"}}

    client = _GenericFallbackIdClient()
    with pytest.raises(
        DeepcoinRequestOutcomeUnknown,
        match="trigger order response missing exact order id",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            max_order_legs=1,
        )

    assert len(client.trigger_payloads) == 1
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "unknown_exchange_outcome"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(ExecutionOrderLeg).count() == 0


def test_revision_first_generic_write_error_is_unknown_and_never_retried(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-unknown.db")
    batch_id, draft = _persist_reserved_revision_batch(session_factory)

    class _GenericRevisionErrorClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            raise DeepcoinClientError("revision write transport failed")

    client = _GenericRevisionErrorClient()
    with pytest.raises(DeepcoinClientError, match="revision write transport failed"):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft=draft,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        original_payload_json = signal.payload_json
        assert signal.status == "unknown_exchange_outcome"

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:unknown_exchange_outcome$",
    ):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft={**draft, "risk_budget_usdt": 999},
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.trigger_payloads) == 1
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        assert signal.payload_json == original_payload_json


def test_revision_writer_submits_only_authorized_original_leg_index(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-selected-leg.db")
    batch_id, draft = _persist_reserved_revision_batch(session_factory)
    revised = {**draft, "authorized_leg_indices": [2]}
    client = _FakeDeepcoinClient()

    result = submit_strategy_revision_replacement_live(
        session_factory,
        batch_id=batch_id,
        draft=revised,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["order_count"] == 1
    assert len(client.trigger_payloads) == 1
    assert client.trigger_payloads[0]["clOrdId"] == draft["order_legs"][1][
        "client_order_id"
    ]
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        assert leg.leg_index == 2


def test_revision_confirmed_first_leg_then_error_is_partial_and_never_retried(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "revision-partial.db")
    batch_id, draft = _persist_reserved_revision_batch(session_factory)

    class _PartialRevisionClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            if len(self.trigger_payloads) == 1:
                return {"code": "0", "data": {"ordId": "revision-leg-1"}}
            raise DeepcoinClientError("revision second write failed")

    client = _PartialRevisionClient()
    with pytest.raises(DeepcoinClientError, match="revision second write failed"):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft=draft,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        original_payload_json = signal.payload_json
        assert signal.status == "partial_submission_failed"

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:partial_submission_failed$",
    ):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft=draft,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.trigger_payloads) == 2
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        assert signal.payload_json == original_payload_json


def test_revision_definite_first_rejection_is_failed_but_never_auto_revived(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-rejected.db")
    batch_id, draft = _persist_reserved_revision_batch(session_factory)

    class _RejectedRevisionClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            raise DeepcoinDefiniteRejection("revision explicitly rejected")

    client = _RejectedRevisionClient()
    with pytest.raises(DeepcoinDefiniteRejection, match="revision explicitly rejected"):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft=draft,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        original_payload_json = signal.payload_json
        assert signal.status == "failed"

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:failed$",
    ):
        submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=batch_id,
            draft=draft,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.trigger_payloads) == 1
    with session_factory() as session:
        signal = session.query(TradeSignal).filter_by(source_type="strategy_revision").one()
        assert signal.payload_json == original_payload_json


def test_v2_submission_uses_durable_selected_legs_not_external_maximum(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    retried = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        selected_entry_leg_indices=(1,),
    )
    assert retried.payload["deepcoin_order_draft"][
        "selected_entry_leg_indices"
    ] == [1, 2]
    client = _FakeDeepcoinClient()

    result = process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        max_order_legs=1,
    )

    assert result["order_count"] == 2
    assert len(client.trigger_payloads) == 2


def test_legacy_shadow_metadata_does_not_override_maximum_order_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-shadow.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload["entry_preamble_assembly"] = {"mode": "shadow"}
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _FakeDeepcoinClient()

    result = process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        max_order_legs=1,
    )

    assert result["order_count"] == 1
    assert len(client.trigger_payloads) == 1


def test_process_next_rejects_declared_v2_evidence_without_matching_assembly(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload["entry_preamble_assembly"] = {
            "assembly_id": 999,
            "strategy_instance_id": signal.strategy_instance_id,
            "assembly_fingerprint": "a" * 64,
        }
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []


@pytest.mark.parametrize(
    ("drift_field", "drift_value"),
    [
        ("price", 68001.0),
        ("quantity", 99.0),
        ("risk_budget_usdt", 999.0),
        ("stop_loss", 67499.0),
        ("malformed_leg", "invalid"),
        ("margin_mode", "isolated"),
        ("position_mode", "merge"),
        ("side", "sell"),
        ("position_side", "short"),
        ("take_profit_leg", {"price": 99999.0, "allocation_pct": 100}),
    ],
)
def test_process_next_rejects_finalized_v2_order_economics_drift(
    tmp_path,
    drift_field,
    drift_value,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        draft = payload["deepcoin_order_draft"]
        if drift_field in {"stop_loss", "margin_mode", "position_mode"}:
            draft[drift_field] = drift_value
        elif drift_field == "malformed_leg":
            draft["order_legs"][0] = drift_value
        else:
            draft["order_legs"][0][drift_field] = drift_value
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _RecordingAllDeepcoinCalls()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.calls == []


def test_process_next_validates_malformed_second_leg_before_first_exchange_call(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    assert len(signal.payload["deepcoin_order_draft"]["order_legs"]) == 2
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload["deepcoin_order_draft"]["order_legs"][1] = "invalid"
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _RecordingAllDeepcoinCalls()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.calls == []


@pytest.mark.parametrize("evidence_state", ["missing", "malformed"])
def test_process_next_rejects_v2_row_without_two_valid_evidence_copies(
    tmp_path,
    evidence_state,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    _finalize_v2_assembly_for_signal(session_factory, signal)
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        payload.pop("entry_preamble_assembly", None)
        payload["deepcoin_order_draft"].pop("entry_preamble_assembly", None)
        if evidence_state == "malformed":
            payload["entry_preamble_assembly"] = "invalid"
            payload["deepcoin_order_draft"]["entry_preamble_assembly"] = []
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _RecordingAllDeepcoinCalls()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.calls == []


def test_process_next_rejects_noncanonical_current_assembly_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, finalized.assembly_id)
        evidence = json.loads(assembly.evidence_json)
        evidence["unexpected_drift"] = True
        assembly.evidence_json = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "processing",
        "submitted",
        "failed",
        "partial_submission_failed",
        "unknown_exchange_outcome",
    ],
)
def test_enqueue_recovery_reuses_any_finalized_signal_without_rewriting_payload(
    tmp_path,
    status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    finalized = _finalize_v2_assembly_for_signal(session_factory, signal)
    _persist_finalized_signal_evidence(session_factory, signal, finalized)
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        row.status = status
        session.commit()
        original_payload_json = row.payload_json
        original_updated_at = row.updated_at

    reused = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert reused.id == signal.id
    assert reused.status == status
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.payload_json == original_payload_json
        assert row.updated_at == original_updated_at


@pytest.mark.parametrize("assembly_is_finalized", [True, False])
def test_process_next_rejects_unsynchronized_or_unfinalized_v2_entry_signal(
    tmp_path,
    assembly_is_finalized,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    assembly_fingerprint = ("b" if assembly_is_finalized else "a") * 64
    assembly_evidence = (
        {
            "order_draft_snapshot": {"order_legs": [{"price": 68000}]},
            "final_entry_leg_count": 1,
        }
        if assembly_is_finalized
        else {}
    )
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(
            raw_message_id=raw.id
        ).one()
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=str(signal.strategy_instance_id),
            risk_multiplier="1",
            evidence_json=json.dumps(assembly_evidence, sort_keys=True),
            fingerprint=assembly_fingerprint,
        )
        session.add(assembly)
        session.flush()
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        stale_evidence = {
            "assembly_id": assembly.id,
            "strategy_instance_id": signal.strategy_instance_id,
            "assembly_fingerprint": "a" * 64,
        }
        payload["deepcoin_order_draft"][
            "entry_preamble_assembly"
        ] = stale_evidence
        if not assembly_is_finalized:
            payload["entry_preamble_assembly"] = dict(stale_evidence)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []
    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "failed"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(TriggerProtectionIntent).count() == 0


def test_process_next_reloads_v2_assembly_after_loading_pending_signal(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as live_submit_module

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(
            raw_message_id=raw.id
        ).one()
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=str(signal.strategy_instance_id),
            risk_multiplier="1",
            evidence_json=json.dumps(
                {
                    "order_draft_snapshot": {
                        "order_legs": [{"price": 68000}]
                    },
                    "final_entry_leg_count": 1,
                },
                sort_keys=True,
            ),
            fingerprint="a" * 64,
        )
        session.add(assembly)
        session.flush()
        assembly_id = assembly.id
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        evidence = {
            "assembly_id": assembly_id,
            "strategy_instance_id": signal.strategy_instance_id,
            "assembly_fingerprint": "a" * 64,
        }
        payload["entry_preamble_assembly"] = dict(evidence)
        payload["deepcoin_order_draft"][
            "entry_preamble_assembly"
        ] = dict(evidence)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()

    real_load = live_submit_module.load_trade_signal

    def load_then_finalize_concurrently(factory, signal_id):
        loaded = real_load(factory, signal_id)
        with factory() as session:
            assembly = session.get(EntryStrategyAssembly, assembly_id)
            assembly.fingerprint = "b" * 64
            session.commit()
        return loaded

    monkeypatch.setattr(
        live_submit_module,
        "load_trade_signal",
        load_then_finalize_concurrently,
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []


def test_legacy_management_audit_is_bounded_redacted_and_read_only(tmp_path):
    from telegram_kol_research.trade_signals import audit_pending_legacy_management_signals

    session_factory = create_session_factory(tmp_path / "research.db")
    legacy = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=700,
        symbol="BTC",
        side="short",
        action="adjust_stop_loss",
        payload={"binding_id": 12, "api_secret": "must-not-leak", "stop_loss": 62000},
    )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:200",
        chat_id=200,
        message_id=701,
        symbol="ETH",
        side="long",
        action="close_position",
        payload={"binding_id": 13, "management_batch_id": 55},
    )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="group:100",
        chat_id=100,
        message_id=702,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"api_secret": "entry-secret"},
    )
    for offset, invalid_reference in enumerate([" 1", "01", True, 1.0], start=1):
        enqueue_trade_signal(
            session_factory,
            venue="deepcoin",
            source_type="kol_management",
            kol_id="group:100",
            chat_id=100,
            message_id=710 + offset,
            symbol="BTC",
            side="short",
            action="close_position",
            payload={"binding_id": 12, "management_batch_id": invalid_reference},
        )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=720,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12, "batch_id": 1},
    )

    before = [(row.id, row.status, row.payload_json) for row in _all_trade_signals(session_factory)]
    report = audit_pending_legacy_management_signals(session_factory, limit=10)
    after = [(row.id, row.status, row.payload_json) for row in _all_trade_signals(session_factory)]

    assert report == {
        "total": 6,
        "returned": 6,
        "truncated": False,
        "scan_truncated": False,
        "by_action": {"adjust_stop_loss": 1, "close_position": 5},
        "by_status": {"pending": 6},
        "items": [
            {
                "id": legacy.id,
                "action": "adjust_stop_loss",
                "status": "pending",
                "source_type": "kol_management",
                "chat_id": 100,
                "message_id": 700,
            },
            *[
                {
                    "id": legacy_id,
                    "action": "close_position",
                    "status": "pending",
                    "source_type": "kol_management",
                    "chat_id": 100,
                    "message_id": message_id,
                }
                for legacy_id, message_id in _legacy_signal_ids_and_messages(
                    session_factory
                )
            ],
        ],
    }
    assert "secret" not in json.dumps(report).lower()
    assert before == after


def test_recovery_dispatch_rejects_automatic_legacy_management_before_generic_action(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=703,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    import telegram_kol_research.recovery_live_submit as live_submit

    monkeypatch.setattr(
        live_submit,
        "execute_deepcoin_management_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy management must not reach generic dispatcher")
        ),
    )

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=_FakeDeepcoinClient(),
        )

    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.attempts == 1


def test_process_next_rejects_legacy_management_before_client_factory(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=704,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    factory_calls = []

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client_factory=lambda: factory_calls.append("called")
            or (_ for _ in ()).throw(AssertionError("factory must not run")),
        )

    assert factory_calls == []
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.attempts == 1


def test_process_next_entry_creates_deferred_client(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    client = _FakeDeepcoinClient()
    factory_calls = []

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client_factory=lambda: factory_calls.append("called") or client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert factory_calls == ["called"]
    assert result["submitted"] is True


def test_non_mapping_legacy_payload_fails_without_factory_then_queue_continues(
    tmp_path
):
    session_factory = create_session_factory(tmp_path / "research.db")
    legacy = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=705,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    with session_factory() as session:
        row = session.get(TradeSignal, legacy.id)
        row.payload_json = "[]"
        session.commit()
    _persist_ready_item(session_factory)
    entry = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    client = _FakeDeepcoinClient()
    factory_calls = []

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client_factory=lambda: factory_calls.append("called") or client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert factory_calls == []
    with session_factory() as session:
        legacy_row = session.get(TradeSignal, legacy.id)
        assert legacy_row.status == "failed"
        assert legacy_row.attempts == 1
        assert session.get(TradeSignal, entry.id).status == "pending"

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client_factory=lambda: factory_calls.append("called") or client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["submitted"] is True
    assert factory_calls == ["called"]


def test_audit_lists_non_mapping_pending_management_payloads_read_only(tmp_path):
    from telegram_kol_research.trade_signals import audit_pending_legacy_management_signals

    session_factory = create_session_factory(tmp_path / "research.db")
    payload_json_values = ["[]", '"scalar"', "null", "123"]
    signal_ids = []
    for index, payload_json in enumerate(payload_json_values, start=1):
        signal = enqueue_trade_signal(
            session_factory,
            venue="deepcoin",
            source_type="kol_management",
            kol_id="group:100",
            chat_id=100,
            message_id=730 + index,
            symbol="BTC",
            side="short",
            action="close_position",
            payload={},
        )
        signal_ids.append(signal.id)
        with session_factory() as session:
            row = session.get(TradeSignal, signal.id)
            row.payload_json = payload_json
            session.commit()

    report = audit_pending_legacy_management_signals(session_factory)

    assert report["total"] == 4
    assert [item["id"] for item in report["items"]] == signal_ids
    with session_factory() as session:
        rows = session.query(TradeSignal).order_by(TradeSignal.id).all()
        assert [row.status for row in rows] == ["pending"] * 4
        assert [row.payload_json for row in rows] == payload_json_values


def _all_trade_signals(session_factory):
    with session_factory() as session:
        return session.query(TradeSignal).order_by(TradeSignal.id).all()


def _legacy_signal_ids_and_messages(session_factory):
    with session_factory() as session:
        rows = (
            session.query(TradeSignal)
            .filter(TradeSignal.message_id >= 711)
            .filter(TradeSignal.message_id <= 720)
            .order_by(TradeSignal.id)
            .all()
        )
        return [(row.id, row.message_id) for row in rows]


def test_process_live_coalesces_equivalent_legacy_trigger_legs_before_submission(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg, second_leg = signal.payload["deepcoin_order_draft"]["order_legs"]
    legacy_legs = [
        dict(first_leg),
        {
            **first_leg,
            "client_order_id": second_leg["client_order_id"],
        },
    ]
    queued_draft = _replace_queued_order_legs(
        session_factory,
        signal.id,
        legacy_legs,
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["order_count"] == 1
    assert len(fake_client.trigger_payloads) == 1
    assert fake_client.trigger_payloads[0]["sz"] == str(
        first_leg["quantity"] + first_leg["quantity"]
    )
    assert "merged_from_leg_indices" not in fake_client.trigger_payloads[0]
    assert result["deepcoin_order_draft"] == queued_draft
    assert len(result["deepcoin_order_draft"]["order_legs"]) == 2
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).all()
    assert len(legs) == 1
    assert json.loads(legs[0].request_json)["merged_from_leg_indices"] == [1, 2]


def test_process_live_preserves_distinct_price_legacy_trigger_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg, second_leg = signal.payload["deepcoin_order_draft"]["order_legs"]
    assert first_leg["price"] != second_leg["price"]
    legacy_legs = [
        {
            **first_leg,
            "allocation_pct": 50.0,
            "risk_budget_usdt": 50.0,
            "quantity": 63.0,
            "base_asset_estimate": 0.063,
        },
        {
            **second_leg,
            "allocation_pct": 50.0,
            "risk_budget_usdt": 50.0,
            "quantity": 84.0,
            "base_asset_estimate": 0.084,
        },
    ]
    queued_draft = _replace_queued_order_legs(
        session_factory,
        signal.id,
        legacy_legs,
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["order_count"] == 2
    assert [payload["triggerPrice"] for payload in fake_client.trigger_payloads] == [
        str(first_leg["price"]),
        str(second_leg["price"]),
    ]
    assert [payload["sz"] for payload in fake_client.trigger_payloads] == [
        "63.0",
        "84.0",
    ]
    assert result["deepcoin_order_draft"] == queued_draft
    with session_factory() as session:
        assert session.query(ExecutionOrderLeg).count() == 2


def test_market_submit_persists_binding_when_position_protection_fails(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _ProtectionFailingDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
    )

    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert result["submitted"] is True
    assert "position_protection_failed_after_entry_submitted" in result["warnings"]
    assert binding.status == "active"
    assert binding.last_exchange_status == "position_active_protection_failed"
    assert binding.order_id == "order-market-1"
    assert binding.pos_id == "pos-market-1"


@pytest.mark.parametrize(
    (
        "protection_failure",
        "failure_index",
        "expected_exception",
        "expected_child_states",
        "expected_message",
    ),
    [
        (
            "unknown",
            1,
            DeepcoinRequestOutcomeUnknown,
            ["protected", "protection_unknown"],
            "writer_outcome_unknown",
        ),
        (
            "rejected",
            1,
            DeepcoinDefiniteRejection,
            ["protected", "recovery_required"],
            "business_rejected",
        ),
        (
            "unknown",
            0,
            DeepcoinRequestOutcomeUnknown,
            ["protection_unknown", "protection_prepared"],
            "writer_outcome_unknown",
        ),
        (
            "readback_missing",
            0,
            DeepcoinRequestOutcomeUnknown,
            ["protection_pending_readback", "protection_prepared"],
            "position_sltp_pending_readback",
        ),
        (
            "rejected_secret",
            0,
            DeepcoinDefiniteRejection,
            ["recovery_required", "protection_prepared"],
            "business_rejected",
        ),
    ],
)
def test_protected_entry_market_persists_operations_and_blocks_later_leg_on_protection_failure(
    tmp_path,
    monkeypatch,
    protection_failure,
    failure_index,
    expected_exception,
    expected_child_states,
    expected_message,
):
    session_factory = create_session_factory(tmp_path / "protected-market.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg = signal.payload["deepcoin_order_draft"]["order_legs"][0]
    _replace_queued_order_legs(
        session_factory,
        signal.id,
        [
            first_leg,
            {
                **first_leg,
                "order_type": "limit",
                "price": 59000.0,
                "client_order_id": "protected-later-leg",
            },
        ],
    )

    class ProtectedClient(_FakeDeepcoinClient):
        uid_scope_hash = "d" * 64

        def __init__(self):
            super().__init__()
            self.active_scope = None
            self.protection_child_count_at_first_post = None
            self.now = 100.0
            self._monotonic_factory = lambda: self.now
            self._sleep_fn = lambda seconds: setattr(
                self, "now", self.now + seconds
            )

        @contextmanager
        def request_scope(self, scope):
            previous = self.active_scope
            self.active_scope = scope
            try:
                yield self
            finally:
                self.active_scope = previous

        def place_order(self, payload):
            with session_factory() as session:
                operation = session.query(DeepcoinExecutionOperation).one()
                assert operation.state == "entry_submitting"
                assert operation.writer_attempted_at is not None
            assert self.active_scope is not None
            assert self.active_scope.phase == "entry_submit"
            self.payloads.append(dict(payload))
            return {
                "code": "0",
                "data": {
                    "ordId": "protected-entry-1",
                    "posId": "pos-market-1",
                },
                "message": "Authorization: Bearer TOPSECRET",
            }

        def list_positions(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "posId": "pos-market-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "10",
                "avgPx": "59800",
                "mrgPosition": "split",
                "mgnMode": "cross",
            }]

        def list_order_history(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "ordId": "protected-entry-1",
                "clOrdId": self.payloads[0]["clOrdId"],
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-market-1",
                "posSide": "short",
                "state": "filled",
            }]

        def set_position_sltp(self, payload):
            self.position_protection_payloads.append(dict(payload))
            current_index = len(self.position_protection_payloads) - 1
            if current_index == 0:
                with session_factory() as session:
                    self.protection_child_count_at_first_post = (
                        session.query(DeepcoinExecutionOperation)
                        .filter(
                            DeepcoinExecutionOperation.parent_operation_id
                            .is_not(None)
                        )
                        .count()
                    )
            if current_index == failure_index:
                if protection_failure == "readback_missing":
                    return {
                        "code": "0",
                        "data": {"ordId": "unread-protection"},
                    }
                if protection_failure == "rejected":
                    raise DeepcoinDefiniteRejection(
                        "protection_rejected"
                    )
                if protection_failure == "rejected_secret":
                    raise DeepcoinDefiniteRejection(
                        "DC-ACCESS-KEY=TOPSECRET "
                        "Authorization: Bearer TOPSECRET"
                    )
                raise DeepcoinRequestOutcomeUnknown(
                    "protection_unknown"
                )
            if current_index == 0:
                self.pending_tpsl.append({
                    "ordId": "protected-stop-1",
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-market-1",
                    "posSide": "short",
                    "slTriggerPx": payload["slTriggerPx"],
                    "sz": payload.get("sz", "0"),
                })
                return {
                    "code": "0",
                    "data": {"ordId": "protected-stop-1"},
                }
            raise AssertionError("unexpected protection write")

    client = ProtectedClient()
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "build_deepcoin_position_sltp_payloads",
        lambda *args, **kwargs: [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-market-1",
                "slTriggerPx": "61800",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-market-1",
                "slTriggerPx": "61900",
            },
        ],
    )

    @contextmanager
    def assert_parent_is_not_writer_attempted_before_final_source_gate(
        *args, **kwargs
    ):
        with session_factory() as session:
            parent = (
                session.query(DeepcoinExecutionOperation)
                .filter(
                    DeepcoinExecutionOperation.parent_operation_id.is_(None)
                )
                .one()
            )
            assert parent.state == "entry_prepared"
            assert parent.writer_attempted_at is None
        yield

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_entry_source_exchange_write_gate",
        assert_parent_is_not_writer_attempted_before_final_source_gate,
    )

    with pytest.raises(expected_exception, match=expected_message):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    assert len(client.payloads) == 1
    assert client.trigger_payloads == []
    assert client.protection_child_count_at_first_post == 2
    with session_factory() as session:
        operations = (
            session.query(DeepcoinExecutionOperation)
            .order_by(DeepcoinExecutionOperation.id)
            .all()
        )
        lifecycle = session.query(StrategyLifecycle).one()
        persisted_signal = session.get(TradeSignal, signal.id)
        mutation_intent_ids = {
            intent.id for intent in session.query(PositionMutationIntent).all()
        }
        write_generation = session.query(
            DeepcoinAccountWriteGeneration
        ).one()
    assert operations[0].contract_version == "1"
    assert operations[0].state == "recovery_required"
    assert [operation.state for operation in operations[1:]] == (
        expected_child_states
    )
    assert all(operation.execution_order_leg_id for operation in operations[1:])
    assert {
        json.loads(operation.evidence_json)["position_mutation_intent_id"]
        for operation in operations[1:]
    } == mutation_intent_ids
    assert lifecycle.lifecycle_status != "invalidated"
    assert persisted_signal.status == "recovery_required"
    assert "TOPSECRET" not in (persisted_signal.last_error or "")
    with session_factory() as session:
        assert all(
            "TOPSECRET" not in (intent.error_json or "")
            for intent in session.query(PositionMutationIntent).all()
        )
        assert all(
            "TOPSECRET" not in (leg.response_json or "")
            for leg in session.query(ExecutionOrderLeg).all()
        )
    assert write_generation.uid_scope_hash == "d" * 64
    assert write_generation.generation == 2 + (failure_index + 1) * 2
    retry_signal = load_trade_signal(session_factory, signal.id)
    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_operation_state_conflict",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=retry_signal,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
            validated_draft=retry_signal.payload[
                "deepcoin_order_draft"
            ],
        )
    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == failure_index + 1


def test_protected_entry_market_request_identity_conflict_sends_zero_posts(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "protected-conflict.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    reserve_execution_operation(
        session_factory,
        operation_key=_protected_entry_operation_key(signal.id, 1),
        trade_signal_id=signal.id,
        contract_version="1",
        phase="entry_preflight",
        state="planned",
        outcome_certainty="not_sent",
        request_fingerprint="a" * 64,
        economics_fingerprint="b" * 64,
        deadline_at=datetime(2026, 8, 13, 8, 0, 10, tzinfo=UTC),
        evidence={"forged": True},
        created_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
    )

    class ConflictClient(_FakeDeepcoinClient):
        uid_scope_hash = "e" * 64

        @contextmanager
        def request_scope(self, scope):
            yield self

        def list_order_history(self, *, inst_id=None):
            return []

    client = ConflictClient()

    with pytest.raises(
        DeepcoinOperationConflict,
        match="operation_identity_conflict",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []
    assert client.position_protection_payloads == []
    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).count() == 1


def test_protected_entry_market_rechecks_live_mode_at_writer_boundary(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "protected-writer-gate.db"
    )
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    class BoundaryClient(_FakeDeepcoinClient):
        uid_scope_hash = "4" * 64
        _monotonic_factory = staticmethod(lambda: 100.0)
        _sleep_fn = staticmethod(lambda seconds: None)

        @contextmanager
        def request_scope(self, scope):
            yield self

        def list_order_history(self, *, inst_id=None):
            return []

    client = BoundaryClient()

    @contextmanager
    def disable_after_source_gate(*args, **kwargs):
        save_trading_settings(
            session_factory,
            {"protected_entry_execution_mode": "disabled"},
        )
        yield

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_entry_source_exchange_write_gate",
        disable_after_source_gate,
    )
    loaded_signal = load_trade_signal(session_factory, signal.id)

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_submit_not_authorized",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=loaded_signal,
            deepcoin_client=client,
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
            validated_draft=loaded_signal.payload[
                "deepcoin_order_draft"
            ],
        )

    assert client.payloads == []
    with session_factory() as session:
        operation = session.query(DeepcoinExecutionOperation).one()
    assert operation.state == "entry_prepared"
    assert operation.writer_attempted_at is None


def test_protected_entry_market_accepted_without_exact_readback_never_protects_or_advances(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "protected-pending.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    class PendingClient(_FakeDeepcoinClient):
        uid_scope_hash = "f" * 64

        def __init__(self):
            super().__init__()
            self.now = 100.0
            self._monotonic_factory = lambda: self.now
            self._sleep_fn = self._sleep

        def _sleep(self, seconds):
            self.now += seconds

        @contextmanager
        def request_scope(self, scope):
            yield self

        def place_order(self, payload):
            self.payloads.append(dict(payload))
            return {"code": "0", "data": {"ordId": "pending-order-1"}}

        def list_positions(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    client = PendingClient()

    with pytest.raises(
        DeepcoinRequestOutcomeUnknown,
        match="protected_entry_readback_pending",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    assert len(client.payloads) == 1
    assert client.position_protection_payloads == []
    assert client.trigger_payloads == []
    with session_factory() as session:
        operation = session.query(DeepcoinExecutionOperation).one()
        lifecycle = session.query(StrategyLifecycle).one()
        persisted_signal = session.get(TradeSignal, signal.id)
    assert operation.state == "entry_pending_readback"
    assert operation.writer_attempted_at is not None
    assert persisted_signal.status == "recovery_required"
    assert lifecycle.lifecycle_status != "invalidated"


def test_protected_entry_market_crash_after_post_resumes_readback_without_second_post(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(tmp_path / "protected-resume.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    class ResumeClient(_FakeDeepcoinClient):
        uid_scope_hash = "1" * 64

        def __init__(self):
            super().__init__()
            self.now = 100.0
            self._monotonic_factory = lambda: self.now
            self._sleep_fn = lambda seconds: setattr(
                self, "now", self.now + seconds
            )

        @contextmanager
        def request_scope(self, scope):
            yield self

        def place_order(self, payload):
            self.payloads.append(dict(payload))
            return {
                "code": "0",
                "data": {"ordId": "resume-order-1", "posId": "resume-pos-1"},
            }

        def list_positions(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "posId": "resume-pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "10",
                "avgPx": "59800",
                "mrgPosition": "split",
                "mgnMode": "cross",
            }]

        def list_order_history(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "ordId": "resume-order-1",
                "clOrdId": self.payloads[0]["clOrdId"],
                "instId": "BTC-USDT-SWAP",
                "posId": "resume-pos-1",
                "posSide": "short",
                "state": "filled",
            }]

    client = ResumeClient()
    original_transition = submitter.transition_execution_operation
    crashed = False

    def crash_after_post(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("state") == "entry_pending_readback" and not crashed:
            crashed = True
            raise RuntimeError("crash_after_market_post")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        submitter,
        "transition_execution_operation",
        crash_after_post,
    )
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="crash_after_market_post"):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=submitted_at,
        )
    monkeypatch.setattr(
        submitter,
        "transition_execution_operation",
        original_transition,
    )
    with session_factory() as session:
        legacy_parent = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one()
        legacy_parent_id = legacy_parent.id
        legacy_parent_key = legacy_parent.operation_key
        legacy_parent_request_fingerprint = legacy_parent.request_fingerprint
        legacy_evidence = json.loads(legacy_parent.evidence_json)
        legacy_evidence.pop("expected_entry_leg_indices", None)
        legacy_evidence.pop("uid_scope_hash", None)
        legacy_evidence.pop("leg_index", None)
        legacy_parent.evidence_json = json.dumps(
            legacy_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()
    record_request_attempt(
        session_factory,
        operation_id=legacy_parent_id,
        expected_operation_key=legacy_parent_key,
        expected_request_fingerprint=legacy_parent_request_fingerprint,
        uid_scope_hash="1" * 64,
        fact=RequestAttemptFact(
            ordinal=1,
            method="POST",
            normalized_path="/deepcoin/trade/order",
            phase="entry_submit",
            priority=RequestPriority.CRITICAL,
            correlation_id="legacy-v1-entry",
            outcome_certainty=OutcomeCertainty.ACCEPTED,
            error_category=None,
            safe_code="request_accepted",
            http_status=200,
            business_code="0",
            governor_wait_ms=0,
            retry_delay_ms=0,
            latency_ms=1,
        ),
        started_at=submitted_at,
        completed_at=submitted_at + timedelta(milliseconds=1),
    )
    save_trading_settings(
        session_factory,
        {"protected_entry_execution_mode": "disabled"},
    )

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_readback_only",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )

    assert len(client.payloads) == 1
    with session_factory() as session:
        parent = (
            session.query(DeepcoinExecutionOperation)
            .filter(DeepcoinExecutionOperation.parent_operation_id.is_(None))
            .one()
        )
    assert parent.state == "recovery_required"
    parent_evidence = json.loads(parent.evidence_json)
    assert parent_evidence["expected_entry_leg_indices"] == [1]
    assert parent_evidence["uid_scope_hash"] == "1" * 64


def test_protected_entry_market_crash_after_exact_readback_rebuilds_binding_and_protects(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    class SimulatedCrash(BaseException):
        pass

    session_factory = create_session_factory(
        tmp_path / "protected-binding-resume.db"
    )
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    class ResumeClient(_FakeDeepcoinClient):
        uid_scope_hash = "5" * 64
        _monotonic_factory = staticmethod(lambda: 100.0)
        _sleep_fn = staticmethod(lambda seconds: None)

        @contextmanager
        def request_scope(self, scope):
            yield self

        def place_order(self, payload):
            self.payloads.append(dict(payload))
            return {
                "code": "0",
                "data": {"ordId": "resume-order", "posId": "resume-pos"},
            }

        def list_positions(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "posId": "resume-pos", "instId": "BTC-USDT-SWAP",
                "posSide": "short", "pos": "10", "avgPx": "59800",
                "mrgPosition": "split", "mgnMode": "cross",
            }]

        def list_order_history(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "ordId": "resume-order",
                "clOrdId": self.payloads[0]["clOrdId"],
                "instId": "BTC-USDT-SWAP", "posId": "resume-pos",
                "posSide": "short", "state": "filled",
            }]

    client = ResumeClient()
    monkeypatch.setattr(
        submitter,
        "build_deepcoin_position_sltp_payloads",
        lambda *args, **kwargs: [{
            "instId": "BTC-USDT-SWAP",
            "posId": "resume-pos",
            "slTriggerPx": "61800",
        }],
    )
    original_upsert = submitter._upsert_protection_failed_binding
    monkeypatch.setattr(
        submitter,
        "_upsert_protection_failed_binding",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SimulatedCrash()
        ),
    )

    with pytest.raises(SimulatedCrash):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(ExecutionOrderLeg).count() == 0
        assert session.query(DeepcoinExecutionOperation).one().state == (
            "entry_confirmed"
        )

    monkeypatch.setattr(
        submitter,
        "_upsert_protection_failed_binding",
        original_upsert,
    )
    loaded_signal = load_trade_signal(session_factory, signal.id)
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=loaded_signal,
        deepcoin_client=client,
        submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        validated_draft=loaded_signal.payload["deepcoin_order_draft"],
    )

    assert result["submitted"] is True
    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == 1
    assert client.trigger_payloads == []
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 1
        assert session.query(ExecutionOrderLeg).count() == 1
        assert session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one().state == "protected"


def test_protected_entry_protection_post_crash_resumes_get_only(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.position_mutation_gateway as gateway_module
    import telegram_kol_research.recovery_live_submit as submitter

    class SimulatedCrash(BaseException):
        pass

    session_factory = create_session_factory(
        tmp_path / "protected-protection-resume.db"
    )
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory, chat_id=200, message_id=66,
        symbol="BTC", side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory, chat_id=200, message_id=66,
        symbol="BTC", side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg = signal.payload["deepcoin_order_draft"]["order_legs"][0]
    _replace_queued_order_legs(
        session_factory,
        signal.id,
        [
            first_leg,
            {
                **first_leg,
                "order_type": "limit",
                "price": 59000.0,
                "client_order_id": "readback-only-later-leg",
            },
        ],
    )

    class ResumeClient(_FakeDeepcoinClient):
        uid_scope_hash = "6" * 64
        _monotonic_factory = staticmethod(lambda: 100.0)
        _sleep_fn = staticmethod(lambda seconds: None)

        @contextmanager
        def request_scope(self, scope):
            yield self

        def place_order(self, payload):
            self.payloads.append(dict(payload))
            return {"code": "0", "data": {
                "ordId": "entry-order", "posId": "entry-pos",
            }}

        def list_positions(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "posId": "entry-pos", "instId": "BTC-USDT-SWAP",
                "posSide": "short", "pos": "10", "avgPx": "59800",
                "mrgPosition": "split", "mgnMode": "cross",
            }]

        def list_order_history(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "ordId": "entry-order",
                "clOrdId": self.payloads[0]["clOrdId"],
                "instId": "BTC-USDT-SWAP", "posId": "entry-pos",
                "posSide": "short", "state": "filled",
            }]

    client = ResumeClient()
    monkeypatch.setattr(
        submitter,
        "build_deepcoin_position_sltp_payloads",
        lambda *args, **kwargs: [{
            "instId": "BTC-USDT-SWAP", "posId": "entry-pos",
            "slTriggerPx": "61800",
        }],
    )
    original_transition = gateway_module.transition_position_mutation_intent

    def crash_before_submitted(*args, **kwargs):
        if kwargs.get("new_status") == "submitted":
            raise SimulatedCrash()
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        gateway_module,
        "transition_position_mutation_intent",
        crash_before_submitted,
    )
    with pytest.raises(SimulatedCrash):
        process_trade_signal_live(
            session_factory, signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )
    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == 1
    assert client.trigger_payloads == []
    with session_factory() as session:
        assert session.query(PositionMutationIntent).one().status == (
            "submitting"
        )

    def fail_if_later_leg_is_written(payload):
        raise AssertionError("readback-only submitted a later leg")

    client.trigger_order = fail_if_later_leg_is_written

    monkeypatch.setattr(
        gateway_module,
        "transition_position_mutation_intent",
        original_transition,
    )
    save_trading_settings(
        session_factory,
        {"protected_entry_execution_mode": "disabled"},
    )
    loaded_signal = load_trade_signal(session_factory, signal.id)
    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_readback_only",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=loaded_signal,
            deepcoin_client=client,
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
            validated_draft=loaded_signal.payload["deepcoin_order_draft"],
        )

    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == 1
    with session_factory() as session:
        assert session.query(PositionMutationIntent).one().status == (
            "confirmed"
        )
        assert session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one().state == "protected"


def _task10_two_leg_signal(session_factory):
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg = signal.payload["deepcoin_order_draft"]["order_legs"][0]
    _replace_queued_order_legs(
        session_factory,
        signal.id,
        [
            first_leg,
            {
                **first_leg,
                "order_type": "limit",
                "price": 59000.0,
                "client_order_id": "task10-later-leg",
            },
        ],
    )
    return signal


class _Task10ProtectedClient(_FakeDeepcoinClient):
    uid_scope_hash = "8" * 64

    def __init__(
        self,
        session_factory,
        *,
        make_protection_capture_incomplete=False,
        later_baseline_failures=0,
        later_writer_unknown=False,
        raw_pending_pagination=False,
        list_only_page_limit=False,
        first_preflight_delay=0.0,
        malformed_later_baseline=False,
    ):
        super().__init__()
        self.session_factory = session_factory
        self.now = 100.0
        self._monotonic_factory = lambda: self.now
        self._sleep_fn = self._sleep
        self.pending_reads = 0
        self.sleep_times = []
        self.make_protection_capture_incomplete = (
            make_protection_capture_incomplete
        )
        self.later_baseline_failures = later_baseline_failures
        self.later_writer_unknown = later_writer_unknown
        self.raw_pending_pagination = raw_pending_pagination
        self.list_only_page_limit = list_only_page_limit
        self.first_preflight_delay = first_preflight_delay
        self.malformed_later_baseline = malformed_later_baseline
        self.later_trigger_visible = False
        self.later_trigger_state = "live"
        self.crash_later_before_post = False
        self.scopes = []
        self.crash_on_retry_sleep = False

    def _sleep(self, seconds):
        if self.crash_on_retry_sleep and seconds > 0:
            self.crash_on_retry_sleep = False
            raise _Task10CrashBeforePost()
        self.sleep_times.append(seconds)
        self.now += seconds

    @contextmanager
    def request_scope(self, scope):
        self.scopes.append((scope.phase, scope.deadline_monotonic, self.now))
        if (
            scope.phase == "entry_preflight"
            and self.first_preflight_delay
        ):
            self.now += self.first_preflight_delay
            self.first_preflight_delay = 0.0
        if (
            self.crash_later_before_post
            and scope.phase == "entry_submit"
            and ":leg:2:entry" in scope.correlation_id
        ):
            self.crash_later_before_post = False
            raise _Task10CrashBeforePost()
        yield self

    def place_order(self, payload):
        self.payloads.append(dict(payload))
        return {
            "code": "0",
            "data": {"ordId": "task10-entry", "posId": "task10-pos"},
        }

    def list_positions(self, *, inst_id=None):
        if not self.payloads:
            return []
        return [{
            "posId": "task10-pos",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "10",
            "avgPx": "59800",
            "mrgPosition": "split",
            "mgnMode": "cross",
        }]

    def list_order_history(self, *, inst_id=None):
        if not self.payloads:
            return []
        return [{
            "ordId": "task10-entry",
            "clOrdId": self.payloads[0]["clOrdId"],
            "instId": "BTC-USDT-SWAP",
            "posId": "task10-pos",
            "posSide": "short",
            "state": "filled",
        }]

    def set_position_sltp(self, payload):
        self.position_protection_payloads.append(dict(payload))
        self.pending_tpsl.append({
            "ordId": "task10-stop",
            "instId": "BTC-USDT-SWAP",
            "posId": "task10-pos",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "slTriggerPx": payload["slTriggerPx"],
            "sz": payload.get("sz", "0"),
        })
        return {"code": "0", "data": {"ordId": "task10-stop"}}

    def list_trigger_orders_pending(self, *, inst_id):
        self.pending_reads += 1
        if self.pending_reads >= 3:
            with self.session_factory() as session:
                later = session.query(DeepcoinExecutionOperation).filter(
                    DeepcoinExecutionOperation.operation_key.like(
                        "%:leg:2:entry"
                    )
                ).one_or_none()
                assert later is not None
                assert later.state in {
                    "next_leg_preflight",
                    "entry_submitting",
                    "entry_pending_readback",
                    "entry_unknown",
                    "completed",
                }
                if later.state == "next_leg_preflight":
                    assert later.writer_attempted_at is None
                assert session.query(ExecutionOrderLeg).filter(
                    ExecutionOrderLeg.leg_index == 2
                ).count() == 1
            if self.later_baseline_failures > 0:
                self.later_baseline_failures -= 1
                raise TimeoutError("transient pending TPSL read")
        rows = [dict(row) for row in self.pending_tpsl]
        if self.list_only_page_limit:
            rows.extend(
                {
                    "ordId": f"list-only-filler-{index}",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "triggerOrderType": "TPSL",
                }
                for index in range(100)
            )
        if self.pending_reads == 2 and self.make_protection_capture_incomplete:
            rows.extend(
                {
                    "ordId": f"filler-{index}",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "triggerOrderType": "TPSL",
                }
                for index in range(99)
            )
        if self.later_trigger_visible:
            rows.append(self._later_trigger_row())
        if (
            self.malformed_later_baseline
            and self.pending_reads >= 3
            and rows
        ):
            rows[0]["posSide"] = "corrupt"
        return rows

    def read_trigger_orders_pending(self, *, inst_id):
        rows = self.list_trigger_orders_pending(inst_id=inst_id)
        response = {"data": rows}
        if self.raw_pending_pagination:
            response.update({"hasMore": True, "nextCursor": "page-2"})
        return response

    def list_trigger_order_history(self, *, inst_id):
        return [self._later_trigger_row()] if self.later_trigger_visible else []

    def trigger_order(self, payload):
        with self.session_factory() as session:
            later = session.query(DeepcoinExecutionOperation).filter(
                DeepcoinExecutionOperation.operation_key.like(
                    "%:leg:2:entry"
                )
            ).one()
            assert later.state == "entry_submitting"
            assert later.writer_attempted_at is not None
        self.trigger_payloads.append(dict(payload))
        if self.later_writer_unknown:
            raise DeepcoinRequestOutcomeUnknown("later trigger timeout")
        self.later_trigger_visible = True
        return {"code": "0", "data": {"ordId": "task10-trigger"}}

    def _later_trigger_row(self):
        payload = self.trigger_payloads[0] if self.trigger_payloads else {}
        return {
            "ordId": "task10-trigger",
            "clOrdId": payload.get("clOrdId", "task10-later-leg"),
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "triggerOrderType": "Trigger",
            "orderType": "limit",
            "price": payload.get("price", "59000.0"),
            "triggerPrice": payload.get("triggerPrice", "59000.0"),
            "sz": payload.get("sz", "10.0"),
            "state": self.later_trigger_state,
        }


def _task10_one_stop(monkeypatch):
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "build_deepcoin_position_sltp_payloads",
        lambda *args, **kwargs: [{
            "instId": "BTC-USDT-SWAP",
            "posId": "task10-pos",
            "slTriggerPx": "61800",
        }],
    )


class _Task10CrashBeforePost(BaseException):
    pass


def test_next_leg_reuses_post_protection_snapshot_without_redundant_get(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "task10-reuse.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)

    result = process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert len(client.trigger_payloads) == 1
    assert client.pending_reads == 2
    with session_factory() as session:
        parent = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one()
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        snapshots = session.query(DeepcoinSnapshotEvidence).order_by(
            DeepcoinSnapshotEvidence.id
        ).all()
    assert later.parent_operation_id == parent.id
    assert later.deadline_at == parent.deadline_at
    assert later.state == "completed"
    assert any(item.deepcoin_execution_operation_id == parent.id for item in snapshots)
    assert any(item.deepcoin_execution_operation_id == later.id for item in snapshots)


def test_slow_first_preflight_does_not_extend_original_next_leg_deadline(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-original-deadline.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        first_preflight_delay=4.0,
    )
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )

    result = process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    later_scopes = [
        item for item in client.scopes if item[0] == "next_leg_preflight"
    ]
    assert later_scopes
    assert all(item[1] == 110.0 for item in later_scopes)
    assert all(item[2] < 110.0 for item in later_scopes)


def test_malformed_later_baseline_exhausts_global_attempt_budget_and_defers(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-malformed-baseline.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        malformed_later_baseline=True,
    )
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_pre_submit_deferred",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        snapshot_count = session.query(DeepcoinSnapshotEvidence).filter(
            DeepcoinSnapshotEvidence.deepcoin_execution_operation_id
            == later.id
        ).count()
    assert later.state == "pre_submit_deferred"
    assert snapshot_count == 4
    assert client.trigger_payloads == []


def test_active_protected_deferred_finalization_preserves_live_lifecycle(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task11-protected-deferred.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        malformed_later_baseline=True,
    )
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="protected_entry_pre_submit_deferred",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        persisted_signal = session.get(TradeSignal, signal.id)
        lifecycle = session.query(StrategyLifecycle).one()
        parent = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one()
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert parent.state == "protected"
    assert later.state == "pre_submit_deferred"
    assert persisted_signal.status == "active_protected_deferred"
    assert lifecycle.lifecycle_status != "invalidated"
    assert client.trigger_payloads == []


def test_missing_planned_later_leg_never_projects_submitted(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task11-missing-later-reservation.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)

    def fail_before_later_reservation(*args, **kwargs):
        raise RecoveryLiveSubmitError("later_reservation_interrupted")

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_submit_trigger_with_protection_intent",
        fail_before_later_reservation,
    )

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="later_reservation_interrupted",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        persisted_signal = session.get(TradeSignal, signal.id)
        lifecycle = session.query(StrategyLifecycle).one()
        operations = session.query(DeepcoinExecutionOperation).all()
    assert persisted_signal.status == "recovery_required"
    assert persisted_signal.processed_at is None
    assert lifecycle.lifecycle_status != "invalidated"
    assert not any(operation.state == "completed" for operation in operations)
    assert client.trigger_payloads == []


def test_v1_failure_before_parent_reservation_freezes_without_lifecycle_close(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task11-pre-parent-failure.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)

    def reject_before_parent(*args, **kwargs):
        raise RecoveryLiveSubmitError("pre_parent_failure")

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_require_current_contract_spec_matches_queued",
        reject_before_parent,
    )

    with pytest.raises(RecoveryLiveSubmitError, match="pre_parent_failure"):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        persisted_signal = session.get(TradeSignal, signal.id)
        lifecycle = session.query(StrategyLifecycle).one()
        operation_count = session.query(DeepcoinExecutionOperation).count()
    assert persisted_signal.status == "recovery_required"
    assert lifecycle.lifecycle_status != "invalidated"
    assert operation_count == 0
    assert client.payloads == []
    assert client.trigger_payloads == []
    assert client.position_protection_payloads == []


def test_later_baseline_attempt_budget_survives_crash_and_restart(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-baseline-budget-restart.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        malformed_later_baseline=True,
    )
    client.crash_on_retry_sleep = True
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    with pytest.raises(_Task10CrashBeforePost):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        first_snapshot_count = session.query(DeepcoinSnapshotEvidence).filter(
            DeepcoinSnapshotEvidence.deepcoin_execution_operation_id
            == later.id
        ).count()
    assert later.state == "next_leg_preflight"
    assert first_snapshot_count == 1

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_pre_submit_deferred",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at + timedelta(seconds=1),
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        final_snapshot_count = session.query(DeepcoinSnapshotEvidence).filter(
            DeepcoinSnapshotEvidence.deepcoin_execution_operation_id
            == later.id
        ).count()
    assert later.state == "pre_submit_deferred"
    assert final_snapshot_count == 4
    assert client.trigger_payloads == []


def test_next_leg_transient_baseline_retries_inside_original_deadline(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "task10-retry.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        later_baseline_failures=1,
    )
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )

    process_trade_signal_live(
        session_factory,
        signal_id=signal.id,
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
    )

    assert len(client.trigger_payloads) == 1
    assert client.sleep_times == [0.5]
    assert client.now < 110.0
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        snapshots = session.query(DeepcoinSnapshotEvidence).filter(
            DeepcoinSnapshotEvidence.deepcoin_execution_operation_id
            == later.id
        ).all()
    assert later.state == "completed"
    assert [item.complete for item in snapshots][-2:] == [False, True]


def test_next_leg_preflight_exhaustion_is_durable_and_never_timer_submitted(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "task10-defer.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        later_baseline_failures=4,
    )
    _task10_one_stop(monkeypatch)
    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit."
        "_complete_reusable_protection_capture",
        lambda *args, **kwargs: None,
    )
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_pre_submit_deferred",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        parent = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one()
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        original_deadline = later.deadline_at
    assert parent.state == "protected"
    assert later.state == "pre_submit_deferred"
    assert later.writer_attempted_at is None
    assert client.trigger_payloads == []

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_pre_submit_deferred",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at + timedelta(seconds=30),
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert later.deadline_at == original_deadline
    assert later.state == "pre_submit_deferred"
    assert client.trigger_payloads == []


def test_next_leg_unknown_writer_restarts_with_get_only_stable_identity(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "task10-unknown.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        later_writer_unknown=True,
    )
    _task10_one_stop(monkeypatch)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_later_leg_readback_pending",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        original_deadline = later.deadline_at
    assert later.state == "entry_unknown"
    assert len(client.trigger_payloads) == 1

    client.later_trigger_visible = True
    client.later_writer_unknown = False
    client.later_trigger_state = "cancelled"
    save_trading_settings(
        session_factory,
        {"protected_entry_execution_mode": "disabled"},
    )
    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_later_leg_readback_pending",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at + timedelta(seconds=4),
        )
    assert len(client.trigger_payloads) == 1

    client.later_trigger_state = "live"
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=load_trade_signal(session_factory, signal.id),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=submitted_at + timedelta(seconds=5),
    )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert result["submitted"] is True
    assert later.state == "completed"
    assert later.deadline_at == original_deadline
    assert len(client.trigger_payloads) == 1


def test_next_leg_unknown_readback_rejects_tampered_baseline_without_new_post(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "task10-tamper.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        later_writer_unknown=True,
    )
    _task10_one_stop(monkeypatch)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_later_leg_readback_pending",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        intent.pre_submit_tpsl_baseline_json = "[]"
        session.commit()

    client.later_trigger_visible = True
    with pytest.raises(
        EntrySubmissionProgressError,
        match="trigger_protection_intent_identity_conflict",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at + timedelta(seconds=5),
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert later.state == "entry_unknown"
    assert len(client.trigger_payloads) == 1


def test_next_leg_crash_after_writer_boundary_restarts_get_only_without_post(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-writer-boundary-crash.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    client.crash_later_before_post = True
    _task10_one_stop(monkeypatch)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    with pytest.raises(_Task10CrashBeforePost):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert later.state == "entry_submitting"
    assert later.writer_attempted_at is not None
    assert client.trigger_payloads == []

    with pytest.raises(
        EntrySubmissionProgressError,
        match="protected_entry_later_leg_readback_pending",
    ):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at + timedelta(seconds=2),
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert later.state == "entry_unknown"
    assert client.trigger_payloads == []


def test_paginated_protection_baseline_fails_before_protection_post(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-paginated-protection-baseline.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        raw_pending_pagination=True,
    )
    _task10_one_stop(monkeypatch)

    with pytest.raises(EntrySubmissionProgressError):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    assert len(client.payloads) == 1
    assert client.position_protection_payloads == []
    assert client.trigger_payloads == []


def test_list_only_page_limit_protection_baseline_fails_before_post(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(
        tmp_path / "task10-list-limit-protection-baseline.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(
        session_factory,
        list_only_page_limit=True,
    )
    client.read_trigger_orders_pending = None
    _task10_one_stop(monkeypatch)

    with pytest.raises(EntrySubmissionProgressError):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )

    assert len(client.payloads) == 1
    assert client.position_protection_payloads == []
    assert client.trigger_payloads == []


def test_completed_later_child_replays_after_rollout_disable_without_post(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(
        tmp_path / "task10-completed-child-replay.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    original = submitter._submit_trigger_with_protection_intent
    crashed = {"value": False}

    def crash_after_completed(*args, **kwargs):
        response = original(*args, **kwargs)
        if not crashed["value"]:
            crashed["value"] = True
            raise _Task10CrashBeforePost()
        return response

    monkeypatch.setattr(
        submitter,
        "_submit_trigger_with_protection_intent",
        crash_after_completed,
    )
    with pytest.raises(_Task10CrashBeforePost):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
    assert later.state == "completed"
    assert len(client.trigger_payloads) == 1

    save_trading_settings(
        session_factory,
        {"protected_entry_execution_mode": "disabled"},
    )
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=load_trade_signal(session_factory, signal.id),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=submitted_at + timedelta(seconds=5),
    )

    assert result["submitted"] is True
    assert len(client.trigger_payloads) == 1


def test_restart_reuses_durable_post_protection_snapshot_without_third_get(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(
        tmp_path / "task10-durable-protection-snapshot.db"
    )
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    original = submitter._submit_protected_entry_protections
    crashed = {"value": False}

    def crash_after_protection_snapshot(*args, **kwargs):
        responses = original(*args, **kwargs)
        if not crashed["value"]:
            crashed["value"] = True
            raise _Task10CrashBeforePost()
        return responses

    monkeypatch.setattr(
        submitter,
        "_submit_protected_entry_protections",
        crash_after_protection_snapshot,
    )
    with pytest.raises(_Task10CrashBeforePost):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )
    with session_factory() as session:
        parent = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one()
        parent_snapshots = session.query(DeepcoinSnapshotEvidence).filter(
            DeepcoinSnapshotEvidence.deepcoin_execution_operation_id
            == parent.id
        ).count()
    assert parent.state == "protected"
    assert parent_snapshots == 1
    assert client.pending_reads == 2

    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=load_trade_signal(session_factory, signal.id),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=submitted_at + timedelta(seconds=1),
    )

    assert result["submitted"] is True
    assert client.pending_reads == 2
    assert len(client.trigger_payloads) == 1


@pytest.mark.parametrize(
    "crash_after_parent",
    [False, True],
    ids=["child_confirmed", "parent_confirmed"],
)
def test_protected_entry_crash_after_confirmed_operation_resumes_without_repeating_writer(
    tmp_path,
    monkeypatch,
    crash_after_parent,
):
    import telegram_kol_research.recovery_live_submit as submitter

    class SimulatedCrash(BaseException):
        pass

    session_factory = create_session_factory(
        tmp_path / "protected-parent-finish-resume.db"
    )
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory, chat_id=200, message_id=66,
        symbol="BTC", side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "protected_entry_execution_mode": "live",
            "protected_entry_execution_after_trade_signal_id": 0,
        },
    )
    signal = enqueue_recovery_trade_signal(
        session_factory, chat_id=200, message_id=66,
        symbol="BTC", side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    class ResumeClient(_FakeDeepcoinClient):
        uid_scope_hash = "7" * 64
        _monotonic_factory = staticmethod(lambda: 100.0)
        _sleep_fn = staticmethod(lambda seconds: None)

        @contextmanager
        def request_scope(self, scope):
            yield self

        def place_order(self, payload):
            self.payloads.append(dict(payload))
            return {"code": "0", "data": {
                "ordId": "entry-order", "posId": "entry-pos",
            }}

        def list_positions(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "posId": "entry-pos", "instId": "BTC-USDT-SWAP",
                "posSide": "short", "pos": "10", "avgPx": "59800",
                "mrgPosition": "split", "mgnMode": "cross",
            }]

        def list_order_history(self, *, inst_id=None):
            if not self.payloads:
                return []
            return [{
                "ordId": "entry-order",
                "clOrdId": self.payloads[0]["clOrdId"],
                "instId": "BTC-USDT-SWAP", "posId": "entry-pos",
                "posSide": "short", "state": "filled",
            }]

    client = ResumeClient()
    monkeypatch.setattr(
        submitter,
        "build_deepcoin_position_sltp_payloads",
        lambda *args, **kwargs: [{
            "instId": "BTC-USDT-SWAP", "posId": "entry-pos",
            "slTriggerPx": "61800",
        }],
    )
    original_transition = submitter._transition_protected_operation
    crashed = False

    def crash_after_child_confirmed(*args, **kwargs):
        nonlocal crashed
        result = original_transition(*args, **kwargs)
        if (
            not crashed
            and kwargs.get("state") == "protected"
            and (
                (result.parent_operation_id is None)
                == crash_after_parent
            )
        ):
            crashed = True
            raise SimulatedCrash()
        return result

    monkeypatch.setattr(
        submitter,
        "_transition_protected_operation",
        crash_after_child_confirmed,
    )
    with pytest.raises(SimulatedCrash):
        process_trade_signal_live(
            session_factory, signal_id=signal.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        )
    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == 1

    monkeypatch.setattr(
        submitter,
        "_transition_protected_operation",
        original_transition,
    )
    loaded_signal = load_trade_signal(session_factory, signal.id)
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=loaded_signal,
        deepcoin_client=client,
        submitted_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        validated_draft=loaded_signal.payload["deepcoin_order_draft"],
    )

    assert result["submitted"] is True
    assert len(client.payloads) == 1
    assert len(client.position_protection_payloads) == 1
    with session_factory() as session:
        assert session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.parent_operation_id.is_(None)
        ).one().state == "protected"


def test_market_submit_defers_take_profit_until_verified_backup_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(session_factory, chat_id=200, message_id=66, symbol="BTC", side="short")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [{
        "posId": "pos-market-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "9",
    }]
    fake_client.place_order = lambda payload: {"code": "0", "data": {"ordId": "order-market-1", "posId": "pos-market-1"}}

    result = submit_recovery_order_live(
        session_factory, chat_id=200, message_id=66, symbol="BTC", side="short",
        deepcoin_client=fake_client, contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert len(fake_client.position_protection_payloads) == 1
    payload = fake_client.position_protection_payloads[0]
    assert "slTriggerPx" in payload
    assert "tpTriggerPx" not in payload
    with session_factory() as session:
        convergence = session.query(TriggerTakeProfitConvergence).one()
    assert convergence.status == "waiting_position"


def test_market_submit_failure_invalidates_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _InsufficientMoneyDeepcoinClient()

    try:
        submit_recovery_order_live(
            session_factory,
            chat_id=200,
            message_id=66,
            symbol="BTC",
            side="short",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
        )
    except DeepcoinClientError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected DeepcoinClientError")

    with session_factory() as session:
        signal = session.query(TradeSignal).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert signal.status == "failed"
    assert signal.last_error == "Deepcoin API error 36: InsufficientMoney"
    assert lifecycle.lifecycle_status == "invalidated"
    assert lifecycle.exit_reason == "auto_trade_failed"
    assert lifecycle.exited_at is not None


def test_limit_submit_uses_stop_only_trigger_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _OrderProtectionFailingDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert "order_protection_failed_after_entry_submitted" not in result["warnings"]
    assert fake_client.payloads == []
    assert fake_client.protection_payloads == []
    assert fake_client.trigger_payloads[0]["orderType"] == "limit"
    assert all(not any(key.startswith("tp") for key in payload) for payload in fake_client.trigger_payloads)
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == "67500.0"
    assert fake_client.position_protection_payloads == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.order_id == "trigger-1,trigger-2"
    assert binding.last_exchange_status == "submitted"
    assert lifecycle.execution_binding_id == binding.id


def test_trigger_parent_event_is_durable_before_later_submission_bookkeeping_crashes(
    tmp_path, monkeypatch
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _OrderProtectionFailingDeepcoinClient()
    monkeypatch.setattr(
        submitter,
        "_record_submitted_order_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after parent submit")),
    )

    with pytest.raises(RuntimeError, match="crash after parent submit"):
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).all()
        parent_events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "create_trigger_entry")
            .order_by(ExecutionEvent.order_id.asc())
            .all()
        )
    assert [intent.parent_trigger_order_id for intent in intents] == ["trigger-1", "trigger-2"]
    assert [event.order_id for event in parent_events] == ["trigger-1", "trigger-2"]
    assert all(event.request_json for event in parent_events)


def test_market_submit_uses_filled_position_id_even_when_different_from_order_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _DelayedFilledPositionDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
        max_order_legs=1,
    )

    assert result["submitted"] is True
    assert fake_client.position_protection_payloads[0]["posId"] == "pos-filled-1"
    assert fake_client.position_calls == 4
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.pos_id == "pos-filled-1"


def test_process_next_trade_signal_live_returns_none_without_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})

    assert process_next_trade_signal_live(
        session_factory,
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
    ) is None
