from datetime import UTC, datetime
from threading import Event, Thread

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_actions import DeepcoinExecutionActionError
from telegram_kol_research.deepcoin_execution_actions import adjust_position_tpsl
from telegram_kol_research.deepcoin_execution_actions import close_bound_position_market
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord
from telegram_kol_research.execution_bindings import list_execution_order_legs
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_bindings import upsert_execution_order_leg
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings


class _FakeDeepcoinClient:
    def __init__(self):
        self.positions = [
            {
                "posId": "pos-1",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "pos": "0.1",
                "cTime": "1000",
            }
        ]
        self.trigger_pending = [
            {
                "triggerOrderType": "TPSL",
                "ordId": "tp-old",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-1",
                "tpTriggerPx": "1605.6",
                "sz": "0.1",
                "cTime": "1000",
            },
            {
                "triggerOrderType": "TPSL",
                "ordId": "sl-old",
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-1",
                "slTriggerPx": "1567.52",
                "sz": "0.1",
                "cTime": "1000",
            },
        ]
        self.open_orders = []
        self.cancel_trigger_payloads = []
        self.cancel_order_payloads = []
        self.protection_payloads = []
        self.order_payloads = []
        self.trigger_payloads = []

    def list_positions(self, *, inst_id=None):
        return self.positions

    def list_trigger_orders_pending(self, *, inst_id):
        return self.trigger_pending

    def list_open_orders(self, *, inst_id=None):
        return self.open_orders

    def cancel_trigger_order(self, cancel_payload):
        self.cancel_trigger_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def cancel_order(self, cancel_payload):
        self.cancel_order_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"ordId": "tpsl-new"}}

    def place_order(self, order_payload):
        self.order_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "close-1"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "trigger-new"}}


def _binding(session_factory, **overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "ETH",
        "side": "long",
        "order_id": "entry-1",
        "client_order_id": "client-1",
        "pos_id": "pos-1",
        "status": "active",
        "strategy_instance_id": "deepcoin:100:55:ETH:long",
    }
    values.update(overrides)
    return upsert_execution_binding(session_factory, ExecutionBindingRecord(**values))


def _signal(session_factory, *, action, payload=None, message_id=88, kol_id="alice", symbol="ETH", side="long"):
    return enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id=kol_id,
        chat_id=100,
        message_id=message_id,
        symbol=symbol,
        side=side,
        action=action,
        payload=payload or {},
        strategy_instance_id="deepcoin:100:55:ETH:long",
    )


def test_adjust_stop_loss_cancels_existing_position_tpsl_before_resetting(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()

    result = adjust_position_tpsl(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
    )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == ["tp-old", "sl-old"]
    assert client.protection_payloads == [
        {
            "instType": "SWAP",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "posId": "pos-1",
            "tpTriggerPx": "1605.6",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "slTriggerPx": "1577.04",
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
        }
    ]
    assert result["cancelled_tpsl_order_ids"] == ["tp-old", "sl-old"]
    assert result["before"] == {"take_profit": 1605.6, "stop_loss": 1567.52}
    assert result["after"] == {"take_profit": 1605.6, "stop_loss": 1577.04}

    events = list_execution_events(session_factory, execution_binding_id=binding_id)
    assert [event.action for event in events] == [
        "adjust_position_tpsl",
        "cancel_position_tpsl",
        "cancel_position_tpsl",
    ]
    assert events[0].related_order_id == "tp-old,sl-old"


def test_adjust_position_tpsl_refuses_to_append_when_existing_tpsl_is_missing(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = []

    with pytest.raises(DeepcoinExecutionActionError, match="no_existing_position_tpsl_to_adjust"):
        adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )
    assert client.protection_payloads == []


def test_adjust_position_tpsl_refuses_unattributed_pending_tpsl_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="adjust_stop_loss",
        payload={"binding_id": binding_id, "stop_loss": 1577.04},
    )
    client = _FakeDeepcoinClient()
    for order in client.trigger_pending:
        order.pop("posId")

    with pytest.raises(DeepcoinExecutionActionError, match="ambiguous_pending_position_tpsl"):
        adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=client,
        )

    assert client.cancel_trigger_payloads == []
    assert client.protection_payloads == []


def test_process_trade_signal_live_closes_bound_position_with_close_pos_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory)
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
        processed_at=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )

    assert client.order_payloads == [
        {
            "instId": "ETH-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": "0.1",
            "mrgPosition": "split",
            "closePosId": "pos-1",
        }
    ]
    assert result["action"] == "close_position"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "closed"
        assert binding.last_exchange_status == "close_position_submitted"


def test_process_trade_signal_live_closes_all_bound_position_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        status="active",
    )
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "cTime": "1000",
        },
        {
            "posId": "pos-2",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.2",
            "cTime": "1001",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert [payload["closePosId"] for payload in client.order_payloads] == ["pos-1", "pos-2"]
    assert [payload["sz"] for payload in client.order_payloads] == ["0.1", "0.2"]
    assert result["pos_id"] == "pos-1,pos-2"
    assert result["close_size"] == 0.30000000000000004
    assert result["full_close"] is True
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "closed"


def test_composite_breakeven_management_reduces_each_target_then_uses_its_own_average_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_binding_id = _binding(
        session_factory,
        kol_id="andy",
        symbol="BTC",
        side="short",
        pos_id="pos-1",
        strategy_instance_id="deepcoin:100:55:BTC:short",
    )
    second_binding_id = _binding(
        session_factory,
        kol_id="andy",
        message_id=56,
        symbol="BTC",
        side="short",
        pos_id="pos-2",
        strategy_instance_id="deepcoin:100:56:BTC:short",
    )
    signal = _signal(
        session_factory,
        action="partial_close_and_move_stop_to_entry",
        payload={
            "targets": [
                {"binding_id": first_binding_id, "fraction": 0.5},
                {"binding_id": second_binding_id, "fraction": 0.5},
            ]
        },
        kol_id="andy",
        symbol="BTC",
        side="short",
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "2", "avgPx": "64000", "cTime": "1000"},
        {"posId": "pos-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "4", "avgPx": "64500", "cTime": "1001"},
    ]
    client.trigger_pending = [
        {"triggerOrderType": "TPSL", "ordId": "tp-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-1", "tpTriggerPx": "63200", "sz": "2", "cTime": "1000"},
        {"triggerOrderType": "TPSL", "ordId": "sl-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-1", "slTriggerPx": "65200", "sz": "2", "cTime": "1000"},
        {"triggerOrderType": "TPSL", "ordId": "tp-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-2", "tpTriggerPx": "62500", "sz": "4", "cTime": "1001"},
        {"triggerOrderType": "TPSL", "ordId": "sl-2", "instId": "BTC-USDT-SWAP", "posSide": "short", "posId": "pos-2", "slTriggerPx": "65400", "sz": "4", "cTime": "1001"},
    ]

    result = execute_deepcoin_management_signal(
        session_factory,
        trade_signal=signal,
        deepcoin_client=client,
        executed_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
    )

    assert [order["closePosId"] for order in client.order_payloads] == ["pos-1", "pos-2"]
    assert [order["sz"] for order in client.order_payloads] == ["1", "2"]
    assert [target["status"] for target in result["targets"]] == ["submitted", "submitted"], result["targets"]
    assert [payload["slTriggerPx"] for payload in client.protection_payloads] == ["64000.0", "64500.0"]
    assert [payload["tpTriggerPx"] for payload in client.protection_payloads] == ["63200.0", "62500.0"]
    assert [target["status"] for target in result["targets"]] == ["submitted", "submitted"]


def test_process_trade_signal_live_closes_only_requested_bound_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        pos_id="pos-1,pos-2",
        status="active",
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="entry-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="entry-2",
            client_order_id="client-2",
            pos_id="pos-2",
            status="active",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="close_position",
        payload={"binding_id": binding_id, "pos_id": "pos-2", "fraction": 0.5},
    )
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.1",
            "cTime": "1000",
        },
        {
            "posId": "pos-2",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "pos": "0.2",
            "cTime": "1001",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.order_payloads == [
        {
            "instId": "ETH-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": "0.1",
            "mrgPosition": "split",
            "closePosId": "pos-2",
        }
    ]
    assert result["pos_id"] == "pos-2"
    assert result["close_size"] == 0.1
    assert result["full_close"] is False
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "active"
        assert binding.last_exchange_status == "partial_close_submitted"
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.pos_id, leg.status) for leg in legs] == [
        (1, "pos-1", "active"),
        (2, "pos-2", "partial_closed"),
    ]


def test_bound_position_market_close_reserves_exact_position_before_concurrent_submission(tmp_path):
    class BlockingClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.listing_started = Event()
            self.release_listing = Event()

        def list_positions(self, *, inst_id=None):
            self.listing_started.set()
            assert self.release_listing.wait(timeout=5)
            return super().list_positions(inst_id=inst_id)

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(session_factory, pos_id="pos-1", status="active")
    client = BlockingClient()
    first_attempt_errors = []

    def first_attempt():
        try:
            close_bound_position_market(
                session_factory,
                pos_id="pos-1",
                deepcoin_client=client,
                executed_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
            )
        except Exception as exc:  # pragma: no cover - assertion below documents failures
            first_attempt_errors.append(exc)

    thread = Thread(target=first_attempt)
    thread.start()
    assert client.listing_started.wait(timeout=5)

    with pytest.raises(DeepcoinExecutionActionError, match="close_already_reserved"):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
        )

    client.release_listing.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_attempt_errors == []
    assert [payload["closePosId"] for payload in client.order_payloads] == ["pos-1"]
    events = list_execution_events(session_factory, pos_id="pos-1")
    assert [(event.action, event.status) for event in events] == [
        ("close_bound_position_market", "submitted"),
        ("close_bound_position_reservation", "submitted"),
        ("close_bound_position_reservation", "reserved"),
    ]


def test_bound_position_market_close_keeps_reservation_after_exchange_error(tmp_path):
    class FailingClient(_FakeDeepcoinClient):
        def place_order(self, order_payload):
            self.order_payloads.append(order_payload)
            raise RuntimeError("connection lost after request may have reached exchange")

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding(session_factory, pos_id="pos-1", status="active")
    client = FailingClient()

    with pytest.raises(RuntimeError, match="connection lost"):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
        )

    with pytest.raises(DeepcoinExecutionActionError, match="close_already_reserved"):
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
        )

    assert len(client.order_payloads) == 1
    events = list_execution_events(session_factory, pos_id="pos-1")
    assert [(event.action, event.status) for event in events] == [
        ("close_bound_position_reservation", "unknown_exchange_outcome"),
        ("close_bound_position_reservation", "reserved"),
    ]


def test_process_trade_signal_live_cancels_bound_trigger_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert result["cancel_type"] == "trigger"
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status == "cancelled"


def test_process_trade_signal_live_cancels_trigger_entry_identified_by_algo_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "algoId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert result["cancel_type"] == "trigger"
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status == "cancelled"


def test_process_trade_signal_live_cancels_all_bound_trigger_entry_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(
        session_factory,
        order_id="trigger-1,trigger-2",
        client_order_id="client-1,client-2",
        pos_id=None,
        status="open",
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-1",
            client_order_id="client-1",
            status="open",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-2",
            client_order_id="client-2",
            status="open",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="cancel_entry",
        payload={"binding_id": binding_id},
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-1",
            "clOrdId": "client-1",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        },
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-2",
            "clOrdId": "client-2",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
        },
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert [item["ordId"] for item in client.cancel_trigger_payloads] == [
        "trigger-1",
        "trigger-2",
    ]
    assert result["order_id"] == "trigger-1,trigger-2"
    assert len(result["cancelled_orders"]) == 2
    events = list_execution_events(session_factory, execution_binding_id=binding_id)
    assert [event.order_id for event in events] == ["trigger-2", "trigger-1"]
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "cancelled"),
        (2, "trigger-2", "cancelled"),
    ]


def test_process_trade_signal_live_recreates_trigger_entry_to_adjust_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    binding_id = _binding(session_factory, order_id="trigger-old", pos_id=None, status="open")
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="trigger-old",
            client_order_id="client-old",
            status="open",
        ),
    )
    trade_signal = _signal(
        session_factory,
        action="adjust_trigger_entry_tpsl",
        payload={
            "binding_id": binding_id,
            "client_order_id": "client-new",
            "take_profit": 1615.12,
            "stop_loss": 1577.04,
        },
    )
    client = _FakeDeepcoinClient()
    client.trigger_pending = [
        {
            "triggerOrderType": "NORMAL",
            "ordId": "trigger-old",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "long",
            "price": "1000",
            "triggerPrice": "1000",
            "sz": "0.1",
            "tpTriggerPx": "1605.6",
            "slTriggerPx": "1567.52",
        }
    ]

    result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=client,
    )

    assert client.cancel_trigger_payloads == [{"instId": "ETH-USDT-SWAP", "ordId": "trigger-old"}]
    assert client.trigger_payloads[0]["price"] == "1000"
    assert client.trigger_payloads[0]["tpTriggerPx"] == 1615.12
    assert client.trigger_payloads[0]["slTriggerPx"] == 1577.04
    assert result["old_order_id"] == "trigger-old"
    assert result["new_order_id"] == "trigger-new"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.order_id == "trigger-new"
        assert binding.last_exchange_status == "trigger_entry_recreated"
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.status) for leg in legs] == [
        (1, "trigger-new", "client-new", "open")
    ]
