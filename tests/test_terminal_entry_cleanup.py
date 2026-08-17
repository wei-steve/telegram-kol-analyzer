from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    StrategyLifecycle,
    TradeSignal,
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
    def __init__(
        self,
        *,
        remains_after_cancel=False,
        absent_initially=False,
        history_state=None,
        history_row_extra=None,
        session_factory=None,
    ):
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
        self.history_state = (
            history_state
            if history_state is not None
            else "canceled" if absent_initially else None
        )
        self.history_row_extra = dict(history_row_extra or {})
        self.session_factory = session_factory
        self.signal_statuses_at_cancel = []
        self.calls = []

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(("list_trigger_orders_pending", inst_id))
        return list(self.trigger_orders)

    def list_open_orders(self, *, inst_id):
        self.calls.append(("list_open_orders", inst_id))
        return []

    def _record_signal_status_at_cancel(self):
        if self.session_factory is not None:
            with self.session_factory() as session:
                signal = (
                    session.query(TradeSignal)
                    .filter(TradeSignal.source_type == "terminal_entry_cleanup")
                    .one()
                )
                self.signal_statuses_at_cancel.append(signal.status)

    def cancel_trigger_order(self, payload):
        self._record_signal_status_at_cancel()
        self.calls.append(("cancel_trigger_order", payload["ordId"]))
        if not self.remains_after_cancel:
            self.trigger_orders = []
            self.history_state = "canceled"
        return {"code": "0"}

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        if self.history_state is None:
            return []
        return [
            {
                "ordId": "entry-order-2",
                "clOrdId": "entry-client-2",
                "state": self.history_state,
                **self.history_row_extra,
            }
        ]

    def list_trade_fills(self, *, inst_id=None):
        if self.history_state in {"filled", "partially_filled"}:
            return [{"ordId": "entry-order-2", "clOrdId": "entry-client-2"}]
        return []


class UnknownTriggerCancelClient(TriggerCancelClient):
    def __init__(self, *, disappears, session_factory=None):
        super().__init__(session_factory=session_factory)
        self.disappears = disappears

    def cancel_trigger_order(self, payload):
        self._record_signal_status_at_cancel()
        self.calls.append(("cancel_trigger_order", payload["ordId"]))
        if self.disappears:
            self.trigger_orders = []
            self.history_state = "canceled"
        raise RuntimeError("transport outcome unknown")


class RegularCancelClient(TriggerCancelClient):
    def __init__(self, *, session_factory):
        super().__init__(session_factory=session_factory)
        self.regular_orders = list(self.trigger_orders)
        self.trigger_orders = []

    def list_open_orders(self, *, inst_id):
        self.calls.append(("list_open_orders", inst_id))
        return list(self.regular_orders)

    def cancel_order(self, payload):
        self._record_signal_status_at_cancel()
        self.calls.append(("cancel_order", payload["ordId"]))
        self.regular_orders = []
        self.history_state = "canceled"
        return {"code": "0"}

    def list_order_history(self, *, inst_id=None):
        if self.history_state is None:
            return []
        return [
            {
                "ordId": "entry-order-2",
                "clOrdId": "entry-client-2",
                "state": self.history_state,
            }
        ]

    def list_trigger_order_history(self, *, inst_id):
        return []


class UnknownRegularCancelClient(RegularCancelClient):
    def cancel_order(self, payload):
        self._record_signal_status_at_cancel()
        self.calls.append(("cancel_order", payload["ordId"]))
        raise RuntimeError("transport outcome unknown")


def test_terminal_entry_cleanup_cancels_exact_trigger_and_confirms_readback(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, binding_id = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient(session_factory=session_factory)

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
    assert client.signal_statuses_at_cancel == ["processing"]
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        binding = session.get(ExecutionBinding, binding_id)
        notification = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "terminal_entry_cleanup_outcome")
            .one()
        )
        assert leg.status == "cancelled"
        assert leg.terminal_reason == "terminal_entry_cleanup_confirmed"
        assert binding.order_id == "position-order"
        assert notification.status == "resolved"
        assert notification.notification_status == "pending"


def test_terminal_entry_cleanup_claims_before_regular_cancel_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = RegularCancelClient(session_factory=session_factory)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "resolved"
    assert client.signal_statuses_at_cancel == ["processing"]
    assert client.calls.count(("cancel_order", "entry-order-2")) == 1


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


def test_terminal_entry_cleanup_never_treats_absent_filled_order_as_cancelled(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient(
        absent_initially=True,
        history_state="filled",
    )

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="exchange_position_missing",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    assert not any(call[0].startswith("cancel_") for call in client.calls)
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        assert leg.status == "pending"
        assert leg.terminal_reason is None


def test_terminal_entry_cleanup_detects_fill_quantity_on_cancelled_history(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = TriggerCancelClient(
        absent_initially=True,
        history_state="canceled",
        history_row_extra={"accFillSz": "1"},
    )

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="exchange_position_missing",
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


def test_terminal_entry_cleanup_rejects_crossed_order_identity_before_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, binding_id = _seed_cleanup_target(session_factory)
    with session_factory() as session:
        first = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        first.client_order_id = "client-cross-b"
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding_id,
                strategy_instance_id="deepcoin:88:4106:BTC:short",
                leg_index=3,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-order-3",
                client_order_id="entry-client-2",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()

    class CrossedClient(TriggerCancelClient):
        def __init__(self):
            super().__init__()
            self.trigger_orders = [
                {
                    "ordId": "entry-order-2",
                    "clOrdId": "entry-client-2",
                },
                {
                    "ordId": "entry-order-3",
                    "clOrdId": "client-cross-b",
                },
            ]

    client = CrossedClient()
    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="exchange_position_missing",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    assert not any(call[0].startswith("cancel_") for call in client.calls)


def test_terminal_entry_cleanup_rejects_duplicate_absent_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, binding_id = _seed_cleanup_target(session_factory)
    with session_factory() as session:
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding_id,
                strategy_instance_id="deepcoin:88:4106:BTC:short",
                leg_index=3,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-order-2",
                client_order_id="another-client",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()
    client = TriggerCancelClient(absent_initially=True)

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="exchange_position_missing",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    with session_factory() as session:
        assert (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.status == "pending")
            .count()
            == 2
        )


def test_terminal_entry_cleanup_never_cancels_partially_filled_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        leg.status = "partially_filled"
        session.commit()
    client = TriggerCancelClient()

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    assert not any(call[0].startswith("cancel_") for call in client.calls)


def test_terminal_entry_cleanup_requires_terminal_evidence_before_cancel_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)

    class MissingHistoryClient:
        def __init__(self):
            self.cancel_calls = 0

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "ordId": "entry-order-2",
                    "clOrdId": "entry-client-2",
                }
            ]

        def list_open_orders(self, *, inst_id):
            return []

        def cancel_trigger_order(self, payload):
            self.cancel_calls += 1
            return {"code": "0"}

    client = MissingHistoryClient()
    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "blocked"
    assert client.cancel_calls == 0
    with session_factory() as session:
        signal = (
            session.query(TradeSignal)
            .filter(TradeSignal.source_type == "terminal_entry_cleanup")
            .one()
        )
        assert signal.status == "failed"


def test_terminal_entry_cleanup_persists_unknown_when_post_cancel_history_fails(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)

    class FailingHistoryClient(TriggerCancelClient):
        def __init__(self):
            super().__init__()
            self.history_reads = 0

        def list_trigger_order_history(self, *, inst_id):
            self.history_reads += 1
            if self.history_reads >= 2:
                raise RuntimeError("history unavailable")
            return []

    client = FailingHistoryClient()
    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "unknown"
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1


def test_terminal_entry_cleanup_handles_absent_and_visible_legs_without_reintroducing_ids(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, binding_id = _seed_cleanup_target(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.order_id = "position-order,entry-order-2,entry-order-3"
        binding.client_order_id = "entry-client-2,entry-client-3"
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding_id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=3,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-order-3",
                client_order_id="entry-client-3",
                status="pending",
                attribution_status="unassigned",
            )
        )
        session.commit()

    class MixedClient:
        def __init__(self):
            self.pending = [
                {
                    "ordId": "entry-order-3",
                    "clOrdId": "entry-client-3",
                }
            ]
            self.history = [
                {
                    "ordId": "entry-order-2",
                    "clOrdId": "entry-client-2",
                    "state": "canceled",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def list_open_orders(self, *, inst_id):
            return []

        def cancel_trigger_order(self, payload):
            self.pending = []
            self.history.append(
                {
                    "ordId": payload["ordId"],
                    "clOrdId": payload["clOrdId"],
                    "state": "canceled",
                }
            )
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.history)

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=MixedClient(),
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "resolved"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.order_id == "position-order"
        assert binding.client_order_id is None


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
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        signal = (
            session.query(TradeSignal)
            .filter(TradeSignal.source_type == "terminal_entry_cleanup")
            .one()
        )
        assert leg.status == "pending"
        assert leg.terminal_reason is None
        assert signal.status == "unknown_exchange_outcome"

    repeated = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert repeated.status == "unknown"
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1


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
    client = UnknownTriggerCancelClient(
        disappears=False,
        session_factory=session_factory,
    )

    result = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert result.status == "unknown"
    assert client.signal_statuses_at_cancel == ["processing"]
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-order-2")
            .one()
        )
        signal = (
            session.query(TradeSignal)
            .filter(TradeSignal.source_type == "terminal_entry_cleanup")
            .one()
        )
        assert leg.status == "pending"
        assert signal.status == "unknown_exchange_outcome"

    repeated = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert repeated.status == "unknown"
    assert client.calls.count(("cancel_trigger_order", "entry-order-2")) == 1


def test_terminal_entry_cleanup_does_not_retry_unknown_regular_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id, _ = _seed_cleanup_target(session_factory)
    client = UnknownRegularCancelClient(session_factory=session_factory)

    first = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )
    second = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=client,
        reason="manual_full_close",
        cleaned_at=NOW,
    )

    assert first.status == "unknown"
    assert second.status == "unknown"
    assert client.signal_statuses_at_cancel == ["processing"]
    assert client.calls.count(("cancel_order", "entry-order-2")) == 1
    with session_factory() as session:
        signal = (
            session.query(TradeSignal)
            .filter(TradeSignal.source_type == "terminal_entry_cleanup")
            .one()
        )
        assert signal.status == "unknown_exchange_outcome"


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
