from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    SourceMessageDeletionExit,
    StrategyLifecycle,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
)
from telegram_kol_research.source_message_deletion_worker import (
    run_source_message_deletion_worker_tick,
)


NOW = datetime(2026, 8, 2, 7, 0, tzinfo=UTC)


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
