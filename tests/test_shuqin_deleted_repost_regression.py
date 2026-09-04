from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.auto_trade_execution import (
    auto_process_message_trade_signal,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
    source_execution_barrier,
)
from telegram_kol_research.source_message_deletion_worker import (
    run_source_message_deletion_worker_tick,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)


class _ContractSpecs:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.01,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.01,
        )


class _ShuqinExchange:
    def __init__(self):
        self.pending = {
            "old-entry-1828": {
                "ordId": "old-entry-1828",
                "clOrdId": "old-client-1828",
                "instId": "ETH-USDT-SWAP",
            },
            "old-entry-1808": {
                "ordId": "old-entry-1808",
                "clOrdId": "old-client-1808",
                "instId": "ETH-USDT-SWAP",
            },
        }
        self.cancelled = []
        self.position_live = True
        self.orders = []
        self.protections = []

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending.values())

    def list_open_orders(self, *, inst_id=None):
        return []

    def cancel_trigger_order(self, payload):
        order_id = payload["ordId"]
        self.cancelled.append(order_id)
        self.pending.pop(order_id, None)
        return {"code": "0"}

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return [
            {"ordId": order_id, "state": "canceled"}
            for order_id in self.cancelled
        ]

    def list_trade_fills(self, *, inst_id=None):
        return [
            {
                "ordId": "old-entry-1808",
                "posId": "old-pos-3428",
                "fillSz": "3",
            }
        ]

    def list_positions(self, *, inst_id=None):
        if self.orders:
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "new-pos-3429",
                    "posSide": "long",
                    "pos": "4",
                    "avgPx": "1800",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                }
            ]
        if not self.position_live:
            return []
        return [
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "old-pos-3428",
                "posSide": "long",
                "pos": "3",
                "avgPx": "1808",
                "mgnMode": "cross",
                "posMode": "split",
                "cTime": "1721000000000",
            }
        ]

    def get_ticker_price(self, *, inst_id):
        assert inst_id == "ETH-USDT-SWAP"
        return 1800.0

    def place_order(self, payload):
        self.orders.append(payload)
        return {
            "code": "0",
            "data": {"ordId": "new-entry-3429", "posId": "new-pos-3429"},
        }

    def set_position_sltp(self, payload):
        self.protections.append(payload)
        self.pending["new-stop-1795"] = {
            "instId": payload.get("instId"),
            "ordId": "new-stop-1795",
            "posId": payload.get("posId"),
            "posSide": payload.get("posSide"),
            "triggerOrderType": "TPSL",
            "slTriggerPrice": payload.get("slTriggerPx"),
            "sz": payload.get("sz", "0"),
        }
        return {"code": "0", "data": {"ordId": "new-stop-1795"}}


def _seed_old_strategy(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-100777,
            message_id=3428,
            text="ETH long 1828/1808 SL1695 TP1853/1885/1930",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            recognition_generation="shuqin-3428",
            parse_source="mimo_authoritative",
            entry_text="1828/1808",
            stop_loss_text="1695",
            take_profit_text="1853/1885/1930",
            confidence=1.0,
        )
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="是策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:-100777:3428:ETH:long",
            kol_id="group:-100777",
            chat_id=-100777,
            message_id=3428,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            order_id="old-entry-1828,old-entry-1808",
            client_order_id="old-client-1828,old-client-1808",
            pos_id="old-pos-3428",
            status="active",
        )
        session.add_all([candidate, decision, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=-100777,
            message_id=3428,
            symbol="ETH",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entry_range_low=1808,
            entry_range_high=1828,
            stop_loss=1695,
            take_profit="[1853,1885,1930]",
            execution_binding_id=binding.id,
        )
        unfilled = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="old-entry-1828",
            client_order_id="old-client-1828",
            status="pending",
            attribution_status="unassigned",
        )
        partial = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="old-entry-1808",
            client_order_id="old-client-1808",
            pos_id="old-pos-3428",
            status="partially_filled",
            attribution_status="verified",
            attribution_evidence_json=(
                '{"policy_version":2,'
                '"evidence_type":"verified_by_current_policy"}'
            ),
        )
        session.add_all([lifecycle, unfilled, partial])
        session.flush()
        session.add(
            PositionProtectionLedger(
                execution_binding_id=binding.id,
                execution_order_leg_id=partial.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="old-pos-3428",
                instrument_id="ETH-USDT-SWAP",
                side="long",
                order_id="old-stop-1695",
                purpose="stop_loss",
                trigger_price="1695",
                status="verified",
                evidence_source="test_fixture",
            )
        )
        session.commit()
        return raw.id, lifecycle.id, binding.id


def _seed_repost(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-100777,
            message_id=3429,
            text="ETH long 市价进场 SL1795",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
                recognition_generation="shuqin-3429",
                parse_source="mimo_authoritative",
                entry_text="1800",
                stop_loss_text="1795",
                confidence=1.0,
            )
        )
        session.commit()
        return raw.id


def _confirm_exit_batch(session_factory, batch_id):
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.status = "succeeded"
        for management_leg in (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch_id)
            .all()
        ):
            management_leg.status = "confirmed"
            entry_leg = session.get(
                ExecutionOrderLeg, management_leg.execution_order_leg_id
            )
            entry_leg.status = "closed"
            entry_leg.terminal_reason = "management_full_close_confirmed"
            session.add(
                PositionMutationIntent(
                    idempotency_key=(
                        f"management:{batch_id}:{management_leg.id}:close:test"
                    ),
                    venue="deepcoin",
                    operation="close_position",
                    strategy_instance_id=batch.strategy_instance_id,
                    execution_binding_id=batch.execution_binding_id,
                    execution_order_leg_id=management_leg.execution_order_leg_id,
                    pos_id=management_leg.pos_id,
                    authority_fingerprint="a" * 64,
                    request_fingerprint="b" * 64,
                    status="confirmed",
                    reserved_at=NOW,
                    confirmed_at=NOW,
                )
            )
        for protection in (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id
                == batch.execution_binding_id
            )
            .all()
        ):
            protection.status = "cancelled"
        session.commit()


def test_shuqin_deleted_strategy_exits_before_repost_can_own_new_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "shuqin-regression.db")
    seeded = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
    )
    assert seeded.seeded is True
    old_raw_id, old_lifecycle_id, old_binding_id = _seed_old_strategy(
        session_factory
    )
    new_raw_id = _seed_repost(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=-100777,
        message_id=3428,
        deleted_at=NOW,
    )
    exchange = _ShuqinExchange()

    cancelled = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: exchange,
        processed_at=NOW,
    )
    assert cancelled.cancelled == 1
    assert set(exchange.cancelled) == {"old-entry-1828", "old-entry-1808"}
    held = source_execution_barrier(
        session_factory, raw_message_id=new_raw_id
    )
    assert held.status == "hold"
    assert held.blocking_exit_id == deletion.exit_id

    planned = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: exchange,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    assert planned.planned_exits == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        batch_id = deletion_exit.management_batch_id
        deletion_exit.state = "reconciling"
        session.commit()
    _confirm_exit_batch(session_factory, batch_id)
    exchange.position_live = False

    finalized = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: exchange,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        binding_reconciler=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )
    assert finalized.finalized == 1
    assert source_execution_barrier(
        session_factory, raw_message_id=new_raw_id
    ).status == "allow"

    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["ETH"],
        },
    )
    submitted = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=new_raw_id,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="舒琴",
                    chat_id=-100777,
                    enabled=True,
                    trading_mode="auto_trade",
                    max_loss_usdt=20,
                    symbol_whitelist=["ETH"],
                )
            ]
        ),
        deepcoin_client=exchange,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    assert submitted["status"] == "submitted"
    assert submitted["entry_execution_type"] == "market"

    with session_factory() as session:
        new_binding = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.message_id == 3429)
            .one()
        )
        new_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == new_binding.id)
            .all()
        )

        old_lifecycle = session.get(StrategyLifecycle, old_lifecycle_id)
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        old_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == old_binding_id)
            .all()
        )
        old_protection_ids = {
            row.order_id
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_binding_id == old_binding_id)
            .all()
        }
        old_protection_states = {
            row.status
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_binding_id == old_binding_id)
            .all()
        }
        new_protection_ids = {
            row.order_id
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_binding_id == new_binding.id)
            .all()
        }

        assert old_lifecycle.lifecycle_status == "exited"
        assert old_lifecycle.exit_reason == "source_message_deleted"
        assert deletion_exit.state == "succeeded"
        assert all(leg.status in {"cancelled", "closed"} for leg in old_legs)
        assert all(leg.terminal_reason for leg in old_legs)
        assert new_protection_ids == {"new-stop-1795"}
        assert {leg.order_id for leg in new_legs} == {"new-entry-3429"}
        assert {leg.pos_id for leg in new_legs} == {"new-pos-3429"}
        assert all(leg.attribution_status == "verified" for leg in new_legs)
        assert old_protection_ids == {"old-stop-1695"}
        assert old_protection_states == {"cancelled"}
        assert {leg.order_id for leg in old_legs}.isdisjoint({"new-entry-3429"})
        assert {leg.pos_id for leg in old_legs if leg.pos_id}.isdisjoint(
            {"new-pos-3429"}
        )
        assert old_raw_id != new_raw_id
