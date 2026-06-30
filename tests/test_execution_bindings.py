import sqlite3
from datetime import datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    build_client_order_id,
    build_deepcoin_account_state,
    build_strategy_instance_id,
    load_deepcoin_order_bindings,
    reconcile_deepcoin_execution_bindings,
    sync_manual_closed_deepcoin_positions,
    upsert_execution_binding,
)
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle


def _binding(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "venue": "deepcoin",
        "order_id": "order-1",
        "client_order_id": "client-1",
        "pos_id": None,
        "margin_mode": "cross",
        "position_mode": "split",
        "status": "open",
    }
    values.update(overrides)
    return ExecutionBindingRecord(**values)


def test_database_bootstrap_creates_execution_bindings_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_bindings)").fetchall()
    }
    conn.close()

    assert "kol_id" in columns
    assert "message_id" in columns
    assert "order_id" in columns
    assert "client_order_id" in columns
    assert "pos_id" in columns
    assert "strategy_instance_id" in columns
    assert "margin_mode" in columns
    assert "position_mode" in columns
    assert "payload_json" in columns
    assert "recovered_at" in columns
    assert "status" in columns


def test_upsert_execution_binding_updates_existing_strategy_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    first = upsert_execution_binding(session_factory, _binding(order_id="order-1"))
    second = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-2", pos_id="pos-1", status="active"),
    )

    assert first == second
    with session_factory() as session:
        stored = session.query(ExecutionBinding).one()

    assert stored.order_id == "order-2"
    assert stored.pos_id == "pos-1"
    assert stored.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert stored.client_order_id == "client-1"
    assert stored.margin_mode == "cross"
    assert stored.position_mode == "split"
    assert stored.status == "active"


def test_load_deepcoin_order_bindings_returns_open_and_active_records(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(session_factory, _binding(order_id="order-1", status="open"))
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="bob",
            chat_id=101,
            message_id=66,
            symbol="ETH",
            side="short",
            order_id="order-2",
            pos_id="pos-2",
            status="closed",
        ),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="carol",
            chat_id=102,
            message_id=77,
            symbol="BTC",
            side="short",
            order_id=None,
            pos_id="pos-3",
            status="active",
        ),
    )

    bindings = load_deepcoin_order_bindings(session_factory)

    assert [(binding.kol_id, binding.order_id, binding.pos_id) for binding in bindings] == [
        ("alice", "order-1", None),
        ("carol", None, "pos-3"),
    ]
    assert bindings[0].client_order_id == "client-1"


def test_build_deepcoin_account_state_uses_persisted_bindings(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1", pos_id="pos-1", status="active"),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "1",
                }
            ]

        def list_open_orders(self):
            return []

    account_state = build_deepcoin_account_state(
        session_factory,
        client=FakeClient(),
    )

    positions = account_state.load_active_positions()
    assert len(positions) == 1
    assert positions[0].kol_id == "alice"


def test_build_stable_strategy_and_client_order_ids():
    strategy_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=100,
        message_id=55,
        symbol="btc",
        side="LONG",
    )

    assert strategy_id == "deepcoin:100:55:BTC:long"
    client_order_id = build_client_order_id(strategy_instance_id=strategy_id, leg_index=2)
    assert client_order_id == "TK729D11F4739D2A2"
    assert client_order_id.isalnum()
    assert len(client_order_id) <= 20


def test_reconcile_deepcoin_execution_bindings_marks_restart_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id=None, client_order_id="client-open", status="unknown"),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="bob",
            chat_id=101,
            message_id=66,
            symbol="ETH",
            side="short",
            order_id="order-stale",
            client_order_id="client-stale",
            status="open",
        ),
    )

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "clOrdId": "client-open",
                    "posSide": "long",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.open == 1
    assert result.stale == 1
    with session_factory() as session:
        rows = session.query(ExecutionBinding).order_by(ExecutionBinding.chat_id.asc()).all()
    assert rows[0].status == "open"
    assert rows[0].last_exchange_status == "order_open"
    assert rows[0].strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert rows[1].status == "stale"
    assert rows[1].last_exchange_status == "not_found_on_exchange"


def test_sync_manual_closed_positions_closes_missing_bound_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-closed", status="active"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "closed"
    assert binding.last_exchange_status == "manual_closed_or_not_found_on_exchange"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"
