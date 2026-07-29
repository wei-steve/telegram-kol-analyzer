from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyLifecycle,
)
from telegram_kol_research.terminal_entry_cleanup import (
    cleanup_terminal_entry_legs,
)


NOW = datetime(2026, 7, 30, 1, tzinfo=UTC)


def _seed_cleanup_target(session_factory, *, binding_status="unknown"):
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status=binding_status,
            order_id="position-order,entry-order-2",
            pos_id="position-order",
            strategy_instance_id="deepcoin:88:4106:BTC:short",
        )
        session.add(binding)
        session.flush()
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="position-order",
                    pos_id="position-order",
                    status="filled",
                    attribution_status="verified",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="entry-order-2",
                    client_order_id="entry-client-2",
                    status="pending",
                    attribution_status="unassigned",
                ),
            ]
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 27, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        return lifecycle.id, binding.id


class TriggerCancelClient:
    def __init__(self, *, remains_after_cancel=False, absent_initially=False):
        self.remains_after_cancel = remains_after_cancel
        self.trigger_orders = (
            []
            if absent_initially
            else [
                {
                    "ordId": "entry-order-2",
                    "clOrdId": "entry-client-2",
                    "instId": "BTC-USDT-SWAP",
                }
            ]
        )
        self.calls = []

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(("list_trigger_orders_pending", inst_id))
        return list(self.trigger_orders)

    def list_open_orders(self, *, inst_id):
        self.calls.append(("list_open_orders", inst_id))
        return []

    def cancel_trigger_order(self, payload):
        self.calls.append(("cancel_trigger_order", payload["ordId"]))
        if not self.remains_after_cancel:
            self.trigger_orders = []
        return {"code": "0"}


class UnknownTriggerCancelClient(TriggerCancelClient):
    def __init__(self, *, disappears):
        super().__init__()
        self.disappears = disappears

    def cancel_trigger_order(self, payload):
        self.calls.append(("cancel_trigger_order", payload["ordId"]))
        if self.disappears:
            self.trigger_orders = []
        raise RuntimeError("transport outcome unknown")


def test_terminal_entry_cleanup_cancels_exact_trigger_and_confirms_readback(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, binding_id = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient()

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "resolved"
    assert result.binding_id == binding_id
    assert result.order_ids == ("entry-order-2",)
    assert client.calls == [
        ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
        ("list_open_orders", "BTC-USDT-SWAP"),
        ("cancel_trigger_order", "entry-order-2"),
        ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
        ("list_open_orders", "BTC-USDT-SWAP"),
    ]
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        binding = session.get(ExecutionBinding, binding_id)
        assert leg.status == "cancelled"
        assert leg.terminal_reason == "terminal_entry_cleanup_confirmed"
        assert binding.order_id == "position-order"


def test_terminal_entry_cleanup_marks_exact_already_absent_order_terminal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient(absent_initially=True)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="exchange_position_missing",
        cleaned_at=NOW,
    )

    assert result.status == "already_absent"
    assert not any(call[0].startswith("cancel_") for call in client.calls)
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        assert leg.status == "cancelled"
        assert leg.terminal_reason == "terminal_entry_cleanup_absent"


def test_terminal_entry_cleanup_blocks_when_cancelled_order_remains_visible(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient(remains_after_cancel=True)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        assert leg.status == "pending"
        assert leg.terminal_reason is None


def test_terminal_entry_cleanup_resolves_unknown_response_only_after_absent_readback(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = UnknownTriggerCancelClient(disappears=True)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "already_absent"
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        assert leg.status == "cancelled"
        assert leg.terminal_reason == "terminal_entry_cleanup_absent"


def test_terminal_entry_cleanup_does_not_retry_unknown_cancel_while_order_remains(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = UnknownTriggerCancelClient(disappears=False)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "unknown"
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        assert leg.status == "pending"


def test_terminal_entry_cleanup_is_idempotent_after_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient()

    first = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )
    calls_after_first = list(client.calls)
    second = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert first.status == "resolved"
    assert second.status == "already_absent"
    assert client.calls == calls_after_first
