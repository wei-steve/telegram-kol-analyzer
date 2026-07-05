import sqlite3
from datetime import datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    build_client_order_id,
    build_deepcoin_account_state,
    build_strategy_instance_id,
    list_execution_order_legs,
    load_deepcoin_order_bindings,
    reconcile_deepcoin_execution_bindings,
    repair_execution_order_legs_from_binding_payloads,
    sync_manual_closed_deepcoin_positions,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg, StrategyLifecycle


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


def test_upsert_execution_order_leg_tracks_deepcoin_ids_per_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1,order-2", client_order_id="client-1,client-2"),
    )

    first_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="order-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
            request={"instId": "BTC-USDT-SWAP", "sz": "5"},
            response={"data": {"ordId": "order-1"}},
        ),
    )
    second_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="order-1b",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
        ),
    )

    assert first_id == second_id
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert len(legs) == 1
    assert legs[0].order_id == "order-1b"
    assert legs[0].client_order_id == "client-1"
    assert legs[0].pos_id == "pos-1"
    with session_factory() as session:
        stored = session.query(ExecutionOrderLeg).one()
    assert stored.request_json == '{"instId":"BTC-USDT-SWAP","sz":"5"}'


def test_repair_execution_order_legs_from_binding_payloads_backfills_legacy_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-1,trigger-2",
            client_order_id="client-1,client-2",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-1",
                        "client_order_id": "client-1",
                        "request": {"instId": "BTC-USDT-SWAP", "sz": "5"},
                        "response": {"data": {"ordId": "trigger-1"}},
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-2",
                        "client_order_id": "client-2",
                        "pos_id": "pos-2",
                    },
                ]
            },
        ),
    )

    repaired = repair_execution_order_legs_from_binding_payloads(session_factory)

    assert repaired == 2
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.pos_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "client-1", None, "open"),
        (2, "trigger-2", "client-2", "pos-2", "active"),
    ]


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


def test_build_client_order_id_can_include_kol_code_and_message_id():
    strategy_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=-1002409877375,
        message_id=8248,
        symbol="btc",
        side="short",
    )

    client_order_id = build_client_order_id(
        strategy_instance_id=strategy_id,
        leg_index=1,
        kol_code="FG",
        message_id=8248,
    )

    assert client_order_id == "TKFG8248E1"
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
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=101,
                message_id=66,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
            )
        )
        session.commit()

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
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).filter_by(chat_id=101).one()
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None


def test_reconcile_deepcoin_execution_bindings_keeps_trigger_pending_order_open(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="trigger-open", client_order_id="client-trigger", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "trigger-open",
                    "clOrdId": "client-trigger",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.open == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.last_exchange_status == "trigger_order_open"
    assert lifecycle.lifecycle_status == "pending_entry"


def test_reconcile_recovers_filled_order_position_id_when_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-filled",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-filled"
    assert binding.last_exchange_status == "position_active_recovered_without_pos_id"
    assert lifecycle.execution_binding_id == binding.id
    assert lifecycle.lifecycle_status == "entered"


def test_reconcile_keeps_bound_live_position_active_even_when_signal_is_old(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-late", status="active"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 2, 51),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-late",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 3, 44),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.last_exchange_status == "position_active"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.execution_binding_id == binding_id


def test_reconcile_revives_exited_lifecycle_when_bound_position_is_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-live",
            status="active",
            payload={
                "draft": {
                    "stop_loss": 62440.0,
                    "take_profit_legs": [{"price": 59588.0}],
                }
            },
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                exited_at=datetime(2026, 6, 30, 10, 0),
                stop_loss=2,
                take_profit=None,
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-live",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.stop_loss == 62440
    assert lifecycle.take_profit == "59588"


def test_reconcile_uses_order_history_to_pick_position_when_symbol_side_ambiguous(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-correct",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "68100",
                    "cTime": "100000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-other",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "69000",
                    "cTime": "100100",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-filled",
                    "clOrdId": "",
                    "state": "filled",
                    "avgPx": "68100",
                    "fillSz": "9",
                    "fillTime": "100000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-correct"
    assert binding.last_exchange_status == "position_active_recovered_from_filled_order"


def test_reconcile_updates_matching_order_leg_with_recovered_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="order-filled",
            client_order_id="client-filled",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-correct",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "68100",
                    "cTime": "100000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-filled",
                    "clOrdId": "client-filled",
                    "state": "filled",
                    "avgPx": "68100",
                    "fillSz": "9",
                    "fillTime": "100000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.order_id, leg.client_order_id, leg.pos_id, leg.status) for leg in legs] == [
        ("order-filled", "client-filled", "pos-correct", "active")
    ]


def test_reconcile_maps_multiple_bound_positions_back_to_matching_order_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-1,trigger-2",
            client_order_id="client-1,client-2",
            pos_id="pos-1,pos-2",
            status="stale",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-1",
                        "client_order_id": "client-1",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "sz": "7",
                            "triggerPrice": "62900",
                        },
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-2",
                        "client_order_id": "client-2",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "sz": "8",
                            "triggerPrice": "63050",
                        },
                    },
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "7",
                    "avgPx": "62900",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-2",
                    "posSide": "long",
                    "pos": "8",
                    "avgPx": "63050",
                },
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 5, 12, 0),
    )

    assert result.active == 1
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.pos_id, leg.status) for leg in legs] == [
        (1, "pos-1", "active"),
        (2, "pos-2", "active"),
    ]


def test_reconcile_uses_trigger_history_to_pick_position_after_trigger_entry_fills(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-entry",
            client_order_id="client-trigger",
            side="short",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 7, 3, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "filled-entry-order",
                    "posSide": "short",
                    "pos": "10",
                    "avgPx": "61351",
                    "cTime": "1782995766000",
                    "uTime": "1782995766000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "other-short",
                    "posSide": "short",
                    "pos": "10",
                    "avgPx": "61688",
                    "cTime": "1782995900000",
                    "uTime": "1782995900000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "filled-entry-order",
                    "state": "filled",
                    "side": "sell",
                    "posSide": "short",
                    "avgPx": "61351",
                    "fillSz": "10",
                    "fillTime": "1782995766000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "trigger-entry",
                    "side": "sell",
                    "posSide": "short",
                    "sz": "10",
                    "px": "61351",
                    "triggerTime": "1782995766000",
                    "uTime": "1782995766000",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 9, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "filled-entry-order"
    assert binding.last_exchange_status == "position_active_recovered_from_filled_order"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.execution_binding_id == binding.id


def test_reconcile_appends_filled_second_leg_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="order-market,order-limit",
            client_order_id="client-market,client-limit",
            pos_id="pos-market",
            status="active",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 10, 0),
                entered_at=datetime(2026, 7, 2, 10, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-market",
                    "posSide": "short",
                    "pos": "4.3",
                    "avgPx": "1616.8",
                    "cTime": "100000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-limit",
                    "posSide": "short",
                    "pos": "6.4",
                    "avgPx": "1624.5",
                    "cTime": "160000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "order-limit",
                    "clOrdId": "client-limit",
                    "state": "filled",
                    "avgPx": "1624.5",
                    "fillSz": "6.4",
                    "fillTime": "160000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.pos_id == "pos-market,pos-limit"
    assert binding.last_exchange_status == "position_active_recovered_additional_pos_id"


def test_reconcile_recovers_second_leg_when_first_leg_is_no_longer_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="order-market,order-limit",
            client_order_id="client-market,client-limit",
            pos_id="pos-market",
            status="active",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 10, 0),
                entered_at=datetime(2026, 7, 2, 10, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-limit",
                    "posSide": "short",
                    "pos": "6.4",
                    "avgPx": "1624.5",
                    "cTime": "160000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "order-limit",
                    "clOrdId": "client-limit",
                    "state": "filled",
                    "avgPx": "1624.5",
                    "fillSz": "6.4",
                    "fillTime": "160000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 5),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-market,pos-limit"
    assert lifecycle.lifecycle_status == "entered"


def test_reconcile_revives_cancelled_stale_binding_when_positions_fill_later(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            symbol="BTC",
            side="short",
            order_id="order-a,order-b",
            client_order_id="client-a,client-b",
            pos_id=None,
            status="stale",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="cancelled",
                signal_at=datetime(2026, 7, 2, 10, 0),
                exited_at=datetime(2026, 7, 2, 10, 5),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-a",
                    "clOrdId": "client-a",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-b",
                    "clOrdId": "client-b",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                },
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 10),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-a,pos-b"
    assert binding.last_exchange_status == "position_active_recovered_additional_pos_id"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None


def test_reconcile_revives_expired_keep_order_when_position_fills_later(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            symbol="BTC",
            side="short",
            order_id="order-keep",
            client_order_id="client-keep",
            pos_id=None,
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="expired",
                exit_reason="expired",
                signal_at=datetime(2026, 7, 2, 10, 0),
                exited_at=datetime(2026, 7, 2, 16, 0),
                management_action="expiry_expired_keep_order",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-keep",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-keep",
                    "clOrdId": "client-keep",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 10, 10),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-keep"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.execution_binding_id == binding.id


def test_reconcile_does_not_guess_position_id_when_ambiguous(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1", client_order_id="client-1", status="open"),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=101,
            message_id=56,
            order_id="order-2",
            client_order_id="client-2",
            status="open",
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "long",
                    "pos": "9",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "long",
                    "pos": "9",
                },
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.stale == 2
    with session_factory() as session:
        rows = session.query(ExecutionBinding).order_by(ExecutionBinding.chat_id).all()

    assert [row.pos_id for row in rows] == [None, None]


def test_reconcile_recovers_trigger_limit_position_from_submitted_order_payload(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="group:-1002370796392",
            chat_id=-1002370796392,
            message_id=3240,
            symbol="BTC",
            side="short",
            order_id="1001123853022859,1001123853022867",
            client_order_id="TKSQ3240E1,TKSQ3240E2",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "client_order_id": "TKSQ3240E1",
                        "execution_type": "trigger_limit",
                        "leg_index": 1,
                        "order_id": "1001123853022859",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62300.0",
                            "triggerPrice": "62300.0",
                            "sz": "12.0",
                        },
                    },
                    {
                        "client_order_id": "TKSQ3240E2",
                        "execution_type": "trigger_limit",
                        "leg_index": 2,
                        "order_id": "1001123853022867",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62500.0",
                            "triggerPrice": "62500.0",
                            "sz": "16.0",
                        },
                    },
                ]
            },
        ),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=-1003825498321,
            message_id=442,
            symbol="BTC",
            side="short",
            order_id="other-order",
            client_order_id="other-client",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=-1002370796392,
                message_id=3240,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 13, 20, 5),
                entered_at=datetime(2026, 7, 3, 15, 51, 47),
                entry_range_low=62300,
                entry_range_high=62700,
                stop_loss=63100,
                take_profit="61500/60800/60000",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "1001123877920316",
                    "posSide": "short",
                    "pos": "12",
                    "avgPx": "62300.0",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 16, 0),
    )

    assert result.active == 1
    with session_factory() as session:
        recovered = (
            session.query(ExecutionBinding)
            .filter_by(chat_id=-1002370796392, message_id=3240)
            .one()
        )
        other = session.query(ExecutionBinding).filter_by(message_id=442).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert recovered.status == "active"
    assert recovered.pos_id == "1001123877920316"
    assert recovered.last_exchange_status == "position_active_recovered_from_submitted_order_payload"
    assert lifecycle.execution_binding_id == recovered.id
    assert other.pos_id is None


def test_reconcile_recovers_filled_trigger_leg_even_when_another_trigger_order_is_open(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=-1002370796392,
            message_id=3240,
            symbol="BTC",
            side="short",
            order_id="filled-trigger,open-trigger",
            client_order_id="filled-client,open-client",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "order_id": "filled-trigger",
                        "client_order_id": "filled-client",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62300",
                            "triggerPrice": "62300",
                            "sz": "12",
                        },
                    },
                    {
                        "order_id": "open-trigger",
                        "client_order_id": "open-client",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62500",
                            "triggerPrice": "62500",
                            "sz": "16",
                        },
                    },
                ]
            },
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "filled-pos",
                    "posSide": "short",
                    "pos": "12",
                    "avgPx": "62300",
                }
            ]

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "open-trigger",
                    "clOrdId": "open-client",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 16, 5),
    )

    assert result.active == 1
    assert result.open == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.status == "active"
    assert binding.pos_id == "filled-pos"
    assert binding.last_exchange_status == "position_active_recovered_from_submitted_order_payload"


@pytest.mark.parametrize("binding_status", ["active", "stale"])
def test_sync_manual_closed_positions_closes_missing_bound_position(tmp_path, binding_status):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-closed", status=binding_status),
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
