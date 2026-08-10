import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
    RawMessage,
    RecoveryOrderConfirmation,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TelegramSourceMessageEvent,
    TradeSignal,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
)
from telegram_kol_research.source_message_deletion_worker import (
    _transition_claimed,
    finalize_source_message_deletion_exit,
    run_source_message_deletion_worker_loop,
    run_source_message_deletion_worker_if_enabled,
    run_source_message_deletion_worker_tick,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 2, 7, 0, tzinfo=UTC)


def test_outcome_transition_supports_production_partial_notification_index(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_pending_strategy(
        session_factory,
        chat_id=91,
        message_id=901,
        order_id="order-partial-index",
    )
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=91,
        message_id=901,
        deleted_at=NOW,
    )
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX uq_execution_events_cleanup_notification_fingerprint"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_execution_events_cleanup_notification_fingerprint "
            "ON execution_events (notification_fingerprint) "
            "WHERE notification_fingerprint IS NOT NULL"
        )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.claim_token = "partial-index-claim"
        deletion_exit.claimed_at = NOW
        session.commit()

    assert _transition_claimed(
        session_factory,
        exit_id=deletion.exit_id,
        claim_token="partial-index-claim",
        new_state="reconciling",
        reason="entry_cleanup_complete",
        updated_at=NOW,
    )
    with session_factory() as session:
        assert session.get(SourceMessageDeletionExit, deletion.exit_id).state == "reconciling"
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "source_message_deletion_outcome")
            .count()
            == 1
        )


def test_worker_loop_logs_worker_loop_exception(tmp_path, caplog):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {"telegram_source_deletion_exit_enabled": True},
    )
    attempts = 0

    def failing_worker(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("partial-index-transition-failed")

    async def run_until_first_failure():
        task = asyncio.create_task(
            run_source_message_deletion_worker_loop(
                session_factory=session_factory,
                deepcoin_client_factory=lambda: object(),
                interval_seconds=0.1,
                worker_runner=failing_worker,
            )
        )
        while attempts == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(
        logging.ERROR,
        logger="telegram_kol_research.source_message_deletion_worker",
    ):
        asyncio.run(run_until_first_failure())

    assert attempts >= 1
    assert "source message deletion worker tick failed" in caplog.text
    assert "partial-index-transition-failed" in caplog.text


def test_rollout_flag_keeps_exchange_worker_dormant_until_enabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    calls = []

    def runner(*_args, **_kwargs):
        calls.append("worker")
        return SimpleNamespace(
            discovered=1,
            cancelled=0,
            planned_exits=0,
            finalized=0,
            waiting=1,
            recovery_required=0,
        )

    dormant = run_source_message_deletion_worker_if_enabled(
        session_factory,
        deepcoin_client_factory=lambda: calls.append("exchange"),
        worker_runner=runner,
        processed_at=NOW,
    )
    assert dormant.discovered == 0
    assert calls == []

    save_trading_settings(
        session_factory,
        {"telegram_source_deletion_exit_enabled": True},
    )
    enabled = run_source_message_deletion_worker_if_enabled(
        session_factory,
        deepcoin_client_factory=lambda: calls.append("exchange"),
        worker_runner=runner,
        processed_at=NOW,
    )
    assert enabled.discovered == 1
    assert calls == ["worker"]


def _seed_pending_strategy(
    session_factory,
    *,
    chat_id: int,
    message_id: int,
    order_id: str,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="BTC long",
            archived_target_group=True,
        )
        binding = ExecutionBinding(
            strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:BTC:long",
            kol_id=f"group:{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id=order_id,
            status="open",
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id=order_id,
            client_order_id=f"client-{order_id}",
            status="pending",
            attribution_status="unassigned",
        )
        session.add_all([lifecycle, leg])
        session.commit()
        return raw.id, lifecycle.id, binding.id, leg.id


def _seed_never_executed_strategy(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=30,
            message_id=300,
            text="BTC long",
            archived_target_group=True,
        )
        lifecycle = StrategyLifecycle(
            chat_id=30,
            message_id=300,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
        )
        session.add_all([raw, lifecycle])
        session.commit()
        return lifecycle.id


def _seed_filled_strategy(session_factory, *, frozen_spec=False):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=20,
            message_id=200,
            text="BTC short",
            archived_target_group=True,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:20:200:BTC:short",
            kol_id="group:20",
            chat_id=20,
            message_id=200,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            order_id="entry-filled",
            pos_id="pos-filled",
            status="active",
            payload_json=(
                json.dumps(
                    {
                        "draft": {
                            "strategy_instance_id": "deepcoin:20:200:BTC:short",
                            "instrument_id": "BTC-USDT-SWAP",
                            "symbol": "BTC",
                            "source": {"chat_id": 20, "message_id": 200},
                            "order_legs": [
                                {
                                    "position_side": "short",
                                    "client_order_id": "entry-filled-client",
                                }
                            ],
                            "contract_spec": {
                                "instrument_id": "BTC-USDT-SWAP",
                                "contract_value": 0.001,
                                "quantity_step": 1,
                                "min_quantity": 1,
                                "price_tick": 0.1,
                            },
                            "contract_spec_snapshot": {
                                "source_digest_sha256": "b" * 64,
                                "fetched_at": "2026-08-01T00:00:00+00:00",
                                "expires_at": "2026-08-02T00:00:00+00:00",
                            },
                        }
                    },
                    sort_keys=True,
                )
                if frozen_spec
                else None
            ),
        )
        session.add_all([raw, binding])
        session.flush()
        if frozen_spec:
            session.add(
                RecoveryOrderConfirmation(
                    kol_id=binding.kol_id,
                    chat_id=binding.chat_id,
                    message_id=binding.message_id,
                    symbol=binding.symbol,
                    side=binding.side,
                    venue="deepcoin",
                    status="ready_confirmed",
                    confirmation_payload_json=json.dumps(
                        {
                            "source": {
                                "chat_id": binding.chat_id,
                                "message_id": binding.message_id,
                                "symbol": binding.symbol,
                                "side": binding.side,
                            },
                            "deepcoin_order_draft": json.loads(
                                binding.payload_json
                            )["draft"],
                        },
                        sort_keys=True,
                    ),
                    confirmed_at=NOW,
                )
            )
        lifecycle = StrategyLifecycle(
            chat_id=20,
            message_id=200,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add_all(
            [
                lifecycle,
                RecognitionDecision(
                    raw_message_id=raw.id,
                    input_kind="text",
                    authoritative_model="mimo",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status="authoritative_only",
                    differences_json="[]",
                ),
                SignalCandidate(
                    raw_message_id=raw.id,
                    symbol="BTC",
                    side="short",
                    event_type="entry_signal",
                    recognition_generation="original",
                    parse_source="mimo_authoritative",
                    confidence=1.0,
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-filled",
                    pos_id="pos-filled",
                    status="active",
                    attribution_status="verified",
                    attribution_evidence_json='{"policy_version": 2}',
                ),
            ]
        )
        session.commit()


class _ContractSpecs:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _ExactCancelClient:
    def __init__(self, order_ids, *, unknown=False, partial_fill=False):
        self.pending = {
            order_id: {
                "ordId": order_id,
                "clOrdId": f"client-{order_id}",
                "instId": "BTC-USDT-SWAP",
            }
            for order_id in order_ids
        }
        self.cancelled = []
        self.unknown = unknown
        self.partial_fill = partial_fill

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending.values())

    def list_open_orders(self, *, inst_id=None):
        return []

    def cancel_trigger_order(self, payload):
        self.cancelled.append(payload["ordId"])
        if not self.unknown:
            self.pending.pop(payload["ordId"], None)
        if self.unknown:
            raise RuntimeError("transport outcome unknown")
        return {"code": "0"}

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return [
            {
                "ordId": order_id,
                "clOrdId": f"client-{order_id}",
                "state": "partially_filled" if self.partial_fill else "canceled",
            }
            for order_id in self.cancelled
            if order_id not in self.pending
        ]

    def list_trade_fills(self, *, inst_id=None):
        if not self.partial_fill:
            return []
        return [{"ordId": order_id} for order_id in self.cancelled]


class _PositionClient(_ExactCancelClient):
    def __init__(self):
        super().__init__([])

    def list_positions(self, *, inst_id=None):
        rows = [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-filled",
                "posSide": "short",
                "pos": "3",
                "avgPx": "62000",
                "mgnMode": "cross",
                "posMode": "split",
                "cTime": "1721000000000",
            }
        ]
        return rows


class _LatePositionClient(_ExactCancelClient):
    def __init__(self):
        super().__init__([])

    def list_positions(self, *, inst_id=None):
        return [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-late",
                "posSide": "short",
                "pos": "2",
                "avgPx": "62100",
                "mgnMode": "cross",
                "posMode": "split",
                "cTime": "1721000001000",
            }
        ]


def _confirm_management_batch(session_factory, batch_id):
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.status = "succeeded"
        management_legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch_id)
            .all()
        )
        for management_leg in management_legs:
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
        session.commit()


def test_worker_cancels_only_exact_deleted_strategy_entry_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, _, _, deleted_leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    _, _, _, other_leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=101,
        order_id="order-other",
    )
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    client = _ExactCancelClient(["order-deleted", "order-other"])

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    assert result.cancelled == 1
    assert client.cancelled == ["order-deleted"]
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, deleted_leg_id).status == "cancelled"
        assert session.get(ExecutionOrderLeg, other_leg_id).status == "pending"
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert deletion_exit.state == "reconciling"
        alert = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "source_message_deletion_outcome"
            )
            .one()
        )
        assert alert.notification_status == "pending"
        assert alert.execution_binding_id == deletion_exit.execution_binding_id


def test_worker_routes_already_terminal_target_without_exchange_mutation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, lifecycle_id, binding_id, leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=92,
        message_id=902,
        order_id="order-already-terminal",
    )
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        lifecycle.lifecycle_status = "exited"
        binding = session.get(ExecutionBinding, binding_id)
        binding.status = "closed"
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.status = "exchange_cancelled"
        session.commit()
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=92,
        message_id=902,
        deleted_at=NOW,
    )

    def forbidden_exchange_client():
        raise AssertionError("terminal routing must not create an exchange client")

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=forbidden_exchange_client,
        processed_at=NOW,
    )

    assert result.waiting == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "reconciling"
        assert deletion_exit.last_reason == "strategy_already_terminal"


def test_worker_unknown_cancel_enters_recovery_without_resubmit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    client = _ExactCancelClient(["order-deleted"], unknown=True)

    first = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )
    second = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    assert first.recovery_required == 1
    assert second.discovered == 0
    assert client.cancelled == ["order-deleted"]
    with session_factory() as session:
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert deletion_exit.state == "recovery_required"
        assert "unknown" in deletion_exit.last_error


def test_worker_partial_fill_is_fail_closed_without_cancelling(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, _, _, leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    with session_factory() as session:
        session.get(ExecutionOrderLeg, leg_id).status = "partially_filled"
        session.commit()
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    client = _ExactCancelClient(["order-deleted"])

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    assert result.recovery_required == 1
    assert client.cancelled == []
    with session_factory() as session:
        assert session.query(SourceMessageDeletionExit).one().state == "recovery_required"


def test_worker_cancels_partial_fill_remainder_then_preserves_exact_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, _, binding_id, leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-partial"
        binding.status = "active"
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.status = "partially_filled"
        leg.pos_id = "pos-partial"
        leg.attribution_status = "verified"
        session.commit()
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    client = _ExactCancelClient(["order-deleted"], partial_fill=True)

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    assert result.cancelled == 1
    assert client.cancelled == ["order-deleted"]
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert leg.status == "active"
        assert leg.pos_id == "pos-partial"
        assert leg.terminal_reason is None
        assert deletion_exit.state == "closing_positions"


def test_worker_refuses_cleanup_when_frozen_lifecycle_binding_has_drifted(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, lifecycle_id, _, _ = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    with session_factory() as session:
        session.get(StrategyLifecycle, lifecycle_id).execution_binding_id = None
        session.commit()
    client = _ExactCancelClient(["order-deleted"])

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    assert result.recovery_required == 1
    assert client.cancelled == []


def test_worker_plans_exact_full_exit_after_entry_cancellation_stage(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        session.commit()

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )

    assert result.planned_exits == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "closing_positions"
        assert deletion_exit.management_batch_id is not None


def test_worker_plans_delisted_full_exit_from_proven_frozen_spec(tmp_path):
    session_factory = create_session_factory(tmp_path / "frozen-spec-exit.db")
    _seed_filled_strategy(session_factory, frozen_spec=True)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        session.commit()

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=None,
        processed_at=NOW,
    )

    assert result.planned_exits == 1
    assert result.recovery_required == 0
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        batch = session.get(
            StrategyManagementBatch, deletion_exit.management_batch_id
        )
        assert batch.target_snapshot_json is not None
        assert json.loads(batch.target_snapshot_json)["contract_spec_source"] == (
            "frozen_binding_draft"
        )


def test_worker_blocks_full_exit_planning_when_frozen_binding_has_drifted(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        lifecycle = session.get(
            StrategyLifecycle, deletion_exit.target_lifecycle_id
        )
        lifecycle.execution_binding_id = None
        session.commit()

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )

    assert result.recovery_required == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "recovery_required"
        assert deletion_exit.management_batch_id is None


def test_pending_only_deletion_finishes_cancelled_after_flat_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, lifecycle_id, _, _ = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    client = _ExactCancelClient(["order-deleted"])
    first = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )
    assert first.cancelled == 1

    second = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={},
            positions=[],
            open_orders=[],
            pending_trigger_orders=[],
        ),
        binding_reconciler=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert second.finalized == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert deletion_exit.state == "succeeded"
        assert deletion_exit.flat_proof_json is not None
        assert lifecycle.lifecycle_status == "cancelled"
        assert lifecycle.exit_reason == "source_message_deleted"


def test_never_executed_deletion_finishes_cancelled_without_a_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _seed_never_executed_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=30,
        message_id=300,
        deleted_at=NOW,
    )

    first = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=object,
        processed_at=NOW,
    )
    second = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=object,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        binding_reconciler=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert first.waiting == 1
    assert second.finalized == 1
    with session_factory() as session:
        assert session.get(SourceMessageDeletionExit, deletion.exit_id).state == "succeeded"
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "cancelled"
        assert lifecycle.exit_reason == "source_message_deleted"


def test_no_binding_deletion_fails_closed_when_submit_evidence_exists(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_never_executed_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=30,
        message_id=300,
        deleted_at=NOW,
    )
    with session_factory() as session:
        session.add(
            TradeSignal(
                signal_uid="historical-submit-without-binding",
                source_type="recovery",
                venue="deepcoin",
                kol_id="group:30",
                chat_id=30,
                message_id=300,
                symbol="BTC",
                side="long",
                action="open_position",
                status="submitted",
                payload_json="{}",
                attempts=1,
            )
        )
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"


def test_snapshot_loader_exception_remains_retryable_until_flat_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_never_executed_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=30,
        message_id=300,
        deleted_at=NOW,
    )
    run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=object,
        processed_at=NOW,
    )

    retry = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=object,
        snapshot_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("temporary snapshot timeout")
        ),
        processed_at=NOW,
    )

    assert retry.waiting == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "reconciling"
        assert "TimeoutError" in deletion_exit.last_error

    completed = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=object,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        binding_reconciler=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert completed.finalized == 1


def test_flat_finalization_fails_closed_when_exchange_snapshot_has_errors(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={"positions": "timeout"},
            positions=[],
            open_orders=[],
            pending_trigger_orders=[],
        ),
        finalized_at=NOW,
    )

    assert status == "waiting"
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "reconciling"
        assert deletion_exit.flat_proof_json is None
        alert = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "source_message_deletion_outcome")
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        assert (alert.status, alert.reason) == (
            "reconciling",
            "flat_snapshot_retry",
        )


def test_flat_finalization_waits_while_exact_order_or_position_is_live(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={},
            positions=[{"posId": "pos-filled", "pos": "3"}],
            open_orders=[],
            pending_trigger_orders=[],
        ),
        finalized_at=NOW,
    )

    assert status == "waiting"
    with session_factory() as session:
        assert session.get(SourceMessageDeletionExit, deletion.exit_id).state == "reconciling"


def test_filled_deletion_requires_a_durable_exit_batch_before_flat_completion(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"
    with session_factory() as session:
        alert = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "source_message_deletion_outcome")
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        assert (alert.status, alert.reason) == (
            "recovery_required",
            "position_exit_batch_not_planned",
        )


def test_flat_finalization_alert_matches_missing_exit_batch_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        deletion_exit.management_batch_id = 999_999
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"
    with session_factory() as session:
        alert = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "source_message_deletion_outcome")
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        assert (alert.status, alert.reason) == (
            "recovery_required",
            "position_exit_batch_missing",
        )


def test_flat_finalization_alert_matches_failed_exit_batch_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        session.commit()
    planned = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    assert planned.planned_exits == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        batch = session.get(
            StrategyManagementBatch, deletion_exit.management_batch_id
        )
        batch.status = "blocked"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"
    with session_factory() as session:
        alert = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "source_message_deletion_outcome")
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        assert (alert.status, alert.reason) == (
            "recovery_required",
            "position_exit_batch_requires_recovery",
        )


def test_unverified_position_identity_can_never_be_declared_flat(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.pos_id == "pos-filled")
            .one()
        )
        leg.attribution_status = "unassigned"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={},
            positions=[{"posId": "pos-filled", "pos": "3"}],
            open_orders=[],
            pending_trigger_orders=[],
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"


def test_binding_position_identity_missing_from_legs_can_never_be_declared_flat(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        binding = session.get(ExecutionBinding, deletion_exit.execution_binding_id)
        binding.pos_id = "pos-filled,pos-unledgered"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        finalized_at=NOW,
    )

    assert status == "recovery_required"


def test_filled_deletion_finishes_exited_only_after_terminal_management_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        session.commit()
    planned = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    assert planned.planned_exits == 1
    flat_snapshot = SimpleNamespace(
        errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        batch_id = deletion_exit.management_batch_id
        lifecycle_id = deletion_exit.target_lifecycle_id
        source_event_id = deletion_exit.source_event_id
        session.commit()

    assert finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=flat_snapshot,
        finalized_at=NOW,
    ) == "waiting"

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.status = "succeeded"
        session.commit()

    assert finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=flat_snapshot,
        finalized_at=NOW,
    ) == "waiting"

    _confirm_management_batch(session_factory, batch_id)

    assert finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=flat_snapshot,
        finalized_at=NOW,
    ) == "succeeded"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        event = session.get(TelegramSourceMessageEvent, source_event_id)
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "source_message_deleted"
        assert event.processing_status == "completed"
        assert event.reason_code == "source_message_deleted"
        assert deletion_exit.state == "succeeded"


def test_final_reconcile_reopens_exit_scope_for_a_late_verified_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=20,
        message_id=200,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "closing_positions"
        session.commit()
    planned = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    assert planned.planned_exits == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        batch_id = deletion_exit.management_batch_id
        binding_id = deletion_exit.execution_binding_id
        session.commit()
    _confirm_management_batch(session_factory, batch_id)

    def reconcile_late_fill(factory, **_kwargs):
        with factory() as session:
            binding = session.get(ExecutionBinding, binding_id)
            binding.pos_id = "pos-filled,pos-late"
            binding.order_id = "entry-filled,entry-late"
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-late",
                    pos_id="pos-late",
                    status="active",
                    attribution_status="verified",
                    attribution_evidence_json=(
                        '{"policy_version":2,'
                        '"evidence_type":"verified_by_current_policy"}'
                    ),
                )
            )
            session.commit()

    result = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_PositionClient,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={},
            positions=[{"posId": "pos-late", "pos": "2"}],
            open_orders=[],
            pending_trigger_orders=[],
        ),
        binding_reconciler=reconcile_late_fill,
        processed_at=NOW,
    )

    assert result.waiting == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        assert deletion_exit.state == "closing_positions"
        assert deletion_exit.management_batch_id is None
        assert deletion_exit.last_reason == "position_exit_scope_expanded"

    replanned = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_LatePositionClient,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )

    assert replanned.planned_exits == 1
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        replacement_batch_id = deletion_exit.management_batch_id
        assert replacement_batch_id != batch_id
        replacement_pos_ids = {
            pos_id
            for (pos_id,) in session.query(StrategyManagementLeg.pos_id)
            .filter(
                StrategyManagementLeg.management_batch_id
                == replacement_batch_id
            )
            .all()
        }
        assert replacement_pos_ids == {"pos-late"}
        deletion_exit.state = "reconciling"
        session.commit()
    _confirm_management_batch(session_factory, replacement_batch_id)

    finalized = run_source_message_deletion_worker_tick(
        session_factory,
        deepcoin_client_factory=_LatePositionClient,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            errors={}, positions=[], open_orders=[], pending_trigger_orders=[]
        ),
        binding_reconciler=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert finalized.finalized == 1


def test_flat_finalization_waits_while_exact_entry_order_is_live(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, _, _, leg_id = _seed_pending_strategy(
        session_factory,
        chat_id=10,
        message_id=100,
        order_id="order-deleted",
    )
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=100,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.status = "cancelled"
        leg.terminal_reason = "source_message_deleted_entry_cancelled"
        session.commit()

    status = finalize_source_message_deletion_exit(
        session_factory,
        deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(
            errors={},
            positions=[],
            open_orders=[{"ordId": "order-deleted"}],
            pending_trigger_orders=[],
        ),
        finalized_at=NOW,
    )

    assert status == "waiting"
