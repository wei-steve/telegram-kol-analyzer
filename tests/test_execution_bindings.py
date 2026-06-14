import sqlite3

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    build_deepcoin_account_state,
    load_deepcoin_order_bindings,
    upsert_execution_binding,
)
from telegram_kol_research.models import ExecutionBinding


def _binding(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "venue": "deepcoin",
        "order_id": "order-1",
        "pos_id": None,
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
    assert "pos_id" in columns
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
