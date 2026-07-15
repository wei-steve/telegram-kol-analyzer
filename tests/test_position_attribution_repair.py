from dataclasses import replace
from datetime import UTC, datetime
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionOrderLegRecord,
    list_execution_order_legs,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    StrategyLifecycle,
)
from telegram_kol_research.position_attribution_repair import (
    PositionAttributionRepairError,
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)
import telegram_kol_research.position_attribution_repair as repair_module


class _HistoricalCleanupClient:
    def __init__(self, *, live_position_ids=()):
        self.positions = [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": pos_id,
                "posSide": "long",
                "pos": "1",
            }
            for pos_id in live_position_ids
        ]
        self.position_history = {}
        self.position_history_calls = []

    def list_positions(self):
        return self.positions

    def list_open_orders(self, *, inst_id=None):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        return []

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trade_fills(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_position_history(self, *, inst_id, pos_id):
        self.position_history_calls.append((inst_id, pos_id))
        return self.position_history.get((inst_id, pos_id), [])


def _seed_historical_duplicate_fixture(tmp_path):
    database_path = tmp_path / "historical-duplicates.db"
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER NOT NULL,
            strategy_instance_id VARCHAR(255),
            leg_index INTEGER NOT NULL,
            purpose VARCHAR(64) NOT NULL,
            order_kind VARCHAR(64) NOT NULL,
            order_id VARCHAR(255),
            client_order_id VARCHAR(255),
            pos_id VARCHAR(255),
            status VARCHAR(32) NOT NULL,
            request_json TEXT,
            response_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO execution_order_legs VALUES
          (1, 10, 'deepcoin:10:10:BTC:long', 1, 'entry', 'market', 'p1', 'client-1', 'p1', 'manually_closed', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
          (2, 10, 'deepcoin:10:10:BTC:long', 2, 'entry', 'market', 'child-2', 'client-2', 'p1', 'manually_closed', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """
    )
    conn.commit()
    conn.close()
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=10,
                strategy_instance_id="deepcoin:10:10:BTC:long",
                kol_id="historical",
                chat_id=10,
                message_id=10,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="unknown",
            )
        )
        session.add(
            StrategyLifecycle(
                id=100,
                chat_id=10,
                message_id=10,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="manual",
                signal_at=datetime(2026, 7, 1),
                entered_at=datetime(2026, 7, 1),
                exited_at=datetime(2026, 7, 2),
                execution_binding_id=10,
            )
        )
        for leg in session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id):
            leg.venue = "deepcoin"
            leg.attribution_status = "attribution_conflict"
            leg.terminal_reason = "manual_lifecycle_terminal"
        session.commit()
    return session_factory


def _historical_fixture_state(session_factory):
    with session_factory() as session:
        return [
            (
                row.id,
                row.pos_id,
                row.status,
                row.attribution_status,
                row.terminal_reason,
            )
            for row in session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id)
        ]


def _seed_exact_history_candidates(session_factory):
    with session_factory() as session:
        session.add_all(
            [
                ExecutionBinding(
                    id=201,
                    strategy_instance_id="deepcoin:20:201:BTC:long",
                    kol_id="history",
                    chat_id=20,
                    message_id=201,
                    symbol="BTC",
                    side="long",
                    venue="deepcoin",
                    status="open",
                ),
                ExecutionBinding(
                    id=202,
                    strategy_instance_id="other:20:202:ETH:long",
                    kol_id="history",
                    chat_id=20,
                    message_id=202,
                    symbol="ETH",
                    side="long",
                    venue="other",
                    status="open",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=201,
                    strategy_instance_id="deepcoin:20:201:BTC:long",
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="actual-order-pos",
                    pos_id=None,
                    venue="deepcoin",
                    status="submitted",
                    attribution_status="unassigned",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=201,
                    strategy_instance_id="deepcoin:20:201:BTC:long",
                    leg_index=2,
                    purpose="entry",
                    order_kind="market",
                    order_id="actual-order-pos",
                    pos_id="stale-pos",
                    venue="deepcoin",
                    status="open",
                    attribution_status="unassigned",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=201,
                    strategy_instance_id="deepcoin:20:201:BTC:long",
                    leg_index=3,
                    purpose="entry",
                    order_kind="market",
                    order_id="terminal-order",
                    pos_id="terminal-pos",
                    venue="deepcoin",
                    status="cancelled",
                    attribution_status="unassigned",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=201,
                    strategy_instance_id="deepcoin:20:201:BTC:long",
                    leg_index=4,
                    purpose="exit",
                    order_kind="market",
                    order_id="exit-order",
                    pos_id="exit-pos",
                    venue="deepcoin",
                    status="submitted",
                    attribution_status="unassigned",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=202,
                    strategy_instance_id="other:20:202:ETH:long",
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="other-order",
                    pos_id="other-pos",
                    venue="other",
                    status="submitted",
                    attribution_status="unassigned",
                ),
            ]
        )
        session.commit()


class _RepairClient:
    def __init__(self):
        self.positions = [
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "1001124083099498",
                "posSide": "short",
                "pos": "1.5",
                "avgPx": "1840",
                "cTime": "1782788876000",
            }
        ]
        self.position_history = {}
        self.position_history_calls = []

    def list_positions(self):
        return self.positions

    def list_open_orders(self, *, inst_id=None):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            {
                "instId": inst_id,
                "ordId": "shuqin-live-trigger",
                "clOrdId": "shuqin-live-client",
                "triggerOrderType": "NORMAL",
                "state": "live",
                "posSide": "short",
            }
        ]

    def list_order_history(self, *, inst_id=None):
        return []

    def list_trade_fills(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return [
            {
                "instId": inst_id,
                "ordId": "sanma-cancelled",
                "clOrdId": "sanma-client",
                "state": "cancelled",
                "posSide": "short",
            },
            {
                "instId": inst_id,
                "ordId": "zhige-filled",
                "clOrdId": "zhige-client",
                "state": "filled",
                "posId": "1001124083099498",
                "posSide": "short",
                "sz": "1.5",
                "px": "1840",
                "triggerTime": "1782788876000",
                "errorCode": "0",
            },
        ]

    def list_position_history(self, *, inst_id, pos_id):
        self.position_history_calls.append((inst_id, pos_id))
        return self.position_history.get((inst_id, pos_id), [])


class _MiyaRepairClient(_RepairClient):
    def __init__(self):
        self.positions = [
            {
                "instId": "ETH-USDT-SWAP",
                "posId": pos_id,
                "posSide": "short",
                "pos": "1.5",
                "avgPx": "1770",
                "cTime": "10000",
                "slTriggerPx": "1820",
                "tpTriggerPx": "1700",
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
            for pos_id in ("1001124099803509", "1001124099803507")
        ]
        self.position_history = {}
        self.position_history_calls = []

    def list_trigger_orders_pending(self, *, inst_id):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return [
            {
                "instId": inst_id,
                "ordId": f"miya-order-{leg_id}",
                "clOrdId": f"miya-client-{leg_id}",
                "state": "filled",
                "posSide": "short",
                "sz": "1.5",
                "px": "1770",
                "triggerTime": "10000",
                "errorCode": "0",
            }
            for leg_id in (245, 244)
        ]


def test_repair_plan_loads_unique_exact_history_for_nonterminal_entry_ids(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_history_candidates(session_factory)
    client = _HistoricalCleanupClient()
    expected_rows = [
        {"instId": "BTC-USDT-SWAP", "posId": "actual-order-pos", "state": "closed"},
        {"instId": "BTC-USDT-SWAP", "posId": "stale-pos", "state": "closed"},
    ]
    client.position_history = {
        ("BTC-USDT-SWAP", "actual-order-pos"): [expected_rows[0]],
        ("BTC-USDT-SWAP", "stale-pos"): [expected_rows[1]],
    }
    captured = {}
    original = repair_module.plan_historical_attribution_cleanup

    def capture_snapshot(**kwargs):
        captured["snapshot"] = kwargs["snapshot"]
        return original(**kwargs)

    monkeypatch.setattr(
        repair_module, "plan_historical_attribution_cleanup", capture_snapshot
    )

    build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert client.position_history_calls == [
        ("BTC-USDT-SWAP", "actual-order-pos"),
        ("BTC-USDT-SWAP", "stale-pos"),
    ]
    assert captured["snapshot"].position_history == expected_rows


def test_repair_plan_exact_history_failure_is_source_error_and_blocks_actions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_history_candidates(session_factory)
    client = _HistoricalCleanupClient()

    def fail_one_request(*, inst_id, pos_id):
        client.position_history_calls.append((inst_id, pos_id))
        if pos_id == "stale-pos":
            raise RuntimeError("history unavailable")
        return []

    client.list_position_history = fail_one_request

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.historical_actions == ()
    assert plan.unresolved_conflicts == [
        {
            "evidence_source_errors": {
                "position_history:BTC-USDT-SWAP:stale-pos": "history unavailable"
            }
        }
    ]


def test_exact_history_changes_exchange_and_plan_fingerprints(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_history_candidates(session_factory)
    client = _HistoricalCleanupClient()
    key = ("BTC-USDT-SWAP", "stale-pos")
    client.position_history[key] = [
        {"instId": key[0], "posId": key[1], "closePx": "100"}
    ]
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    first = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client, now=now
    )
    client.position_history[key] = [
        {"instId": key[0], "posId": key[1], "closePx": "101"}
    ]
    second = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client, now=now
    )

    assert second.exchange_evidence_fingerprint != first.exchange_evidence_fingerprint
    assert second.fingerprint != first.fingerprint


def _seed_miya_equivalent_component(session_factory):
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=123,
                strategy_instance_id="deepcoin:miya:ETH:short",
                kol_id="Miya",
                chat_id=5,
                message_id=123,
                symbol="ETH",
                side="short",
                venue="deepcoin",
                margin_mode="cross",
                position_mode="split",
                status="open",
            )
        )
        session.add_all(
            [
                ExecutionOrderLeg(
                    id=leg_id,
                    execution_binding_id=123,
                    strategy_instance_id="deepcoin:miya:ETH:short",
                    leg_index=index,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=f"miya-order-{leg_id}",
                    client_order_id=f"miya-client-{leg_id}",
                    venue="deepcoin",
                    status="filled",
                    attribution_status="attribution_conflict",
                    request_json=(
                        '{"instId":"ETH-USDT-SWAP","posSide":"short",'
                        '"sz":"1.5","px":"1770","slTriggerPx":"1820",'
                        '"tpTriggerPx":"1700"}'
                    ),
                )
                for index, leg_id in enumerate((244, 245), start=1)
            ]
        )
        session.commit()


def _seed_incident(session_factory):
    with session_factory() as session:
        session.add_all(
            [
                ExecutionBinding(
                    id=112,
                    strategy_instance_id="deepcoin:1:112:ETH:short",
                    kol_id="三马哥",
                    chat_id=1,
                    message_id=112,
                    symbol="ETH",
                    side="short",
                    venue="deepcoin",
                    pos_id="1001124083099498",
                    status="unknown",
                ),
                ExecutionBinding(
                    id=120,
                    strategy_instance_id="deepcoin:2:120:ETH:short",
                    kol_id="智哥",
                    chat_id=2,
                    message_id=120,
                    symbol="ETH",
                    side="short",
                    venue="deepcoin",
                    pos_id=None,
                    status="open",
                ),
                ExecutionBinding(
                    id=121,
                    strategy_instance_id="deepcoin:3:121:ETH:short",
                    kol_id="舒琴",
                    chat_id=3,
                    message_id=121,
                    symbol="ETH",
                    side="short",
                    venue="deepcoin",
                    pos_id=None,
                    status="open",
                ),
            ]
        )
        session.commit()
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=112,
            leg_index=1,
            order_kind="trigger_limit",
            order_id="sanma-cancelled",
            client_order_id="sanma-client",
            pos_id="1001124083099498",
            status="active",
            attribution_status="attribution_conflict",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=120,
            leg_index=2,
            order_kind="trigger_limit",
            order_id="zhige-filled",
            client_order_id="zhige-client",
            status="filled",
            request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "1.5"},
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=121,
            leg_index=1,
            order_kind="trigger_limit",
            order_id="shuqin-live-trigger",
            client_order_id="shuqin-live-client",
            status="open",
            request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "2"},
        ),
    )


def test_repair_plan_is_read_only_and_preserves_live_trigger(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=_RepairClient(),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert [(action.action, action.binding_id, action.leg_index) for action in plan.actions] == [
        ("clear_stale_position", 112, 1),
        ("terminal_cancelled_leg", 112, 1),
        ("assign_verified_position", 120, 2),
    ]
    assert plan.unresolved_conflicts == []
    wrong = list_execution_order_legs(session_factory, execution_binding_id=112)[0]
    target = list_execution_order_legs(session_factory, execution_binding_id=120)[0]
    shuqin = list_execution_order_legs(session_factory, execution_binding_id=121)[0]
    assert wrong.pos_id == "1001124083099498"
    assert wrong.status == "active"
    assert target.pos_id is None
    assert shuqin.status == "open"


def test_repair_plan_canonicalizes_only_miya_equivalent_component(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=_MiyaRepairClient(),
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert [
        (action.action, action.leg_id, action.new_pos_id) for action in plan.actions
    ] == [
        ("assign_verified_position", 244, "1001124099803507"),
        ("assign_verified_position", 245, "1001124099803509"),
    ]
    assert plan.unresolved_conflicts == []
    assert all(
        action.evidence["evidence_type"] == "equivalent_permutation_assignment"
        for action in plan.actions
    )
    with session_factory() as session:
        assert {
            int(leg.id): (leg.pos_id, leg.attribution_status)
            for leg in session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id)
        } == {
            244: (None, "attribution_conflict"),
            245: (None, "attribution_conflict"),
        }


def test_repair_plan_preserves_other_unresolved_component(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=124,
                strategy_instance_id="deepcoin:other:BTC:long",
                kol_id="other",
                chat_id=6,
                message_id=124,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                margin_mode="cross",
                position_mode="split",
                status="open",
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=246,
                execution_binding_id=124,
                strategy_instance_id="deepcoin:other:BTC:long",
                leg_index=1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="other-order-246",
                client_order_id="other-client-246",
                venue="deepcoin",
                status="filled",
                attribution_status="attribution_conflict",
                request_json=(
                    '{"instId":"BTC-USDT-SWAP","posSide":"long",'
                    '"sz":"1","px":"60000"}'
                ),
            )
        )
        session.commit()
    client = _MiyaRepairClient()
    client.positions.extend(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": pos_id,
                "posSide": "long",
                "pos": "1",
                "avgPx": "60000",
                "cTime": "20000",
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
            for pos_id in ("btc-position-1", "btc-position-2")
        ]
    )
    original_history = client.list_trigger_order_history

    def history(*, inst_id):
        if inst_id == "BTC-USDT-SWAP":
            return [
                {
                    "instId": inst_id,
                    "ordId": "other-order-246",
                    "clOrdId": "other-client-246",
                    "state": "filled",
                    "posSide": "long",
                    "sz": "1",
                    "px": "60000",
                    "triggerTime": "20000",
                    "errorCode": "0",
                }
            ]
        return original_history(inst_id=inst_id)

    client.list_trigger_order_history = history

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert [(action.leg_id, action.new_pos_id) for action in plan.actions] == [
        (244, "1001124099803507"),
        (245, "1001124099803509"),
    ]
    assert plan.unresolved_conflicts == [
        {
            "leg_ids": [246],
            "position_ids": ["btc-position-1", "btc-position-2"],
        }
    ]
    with pytest.raises(PositionAttributionRepairError, match="unresolved"):
        apply_position_attribution_repair_plan(
            session_factory, plan, deepcoin_client=client
        )


def test_miya_repair_apply_rejects_exchange_position_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    client.positions.pop()

    with pytest.raises(PositionAttributionRepairError, match="live positions changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_nonempty_repair_apply_requires_reviewed_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    with pytest.raises(PositionAttributionRepairError, match="reviewed fingerprint"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
        )


def test_nonempty_repair_apply_rejects_mismatched_reviewed_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    with pytest.raises(PositionAttributionRepairError, match="reviewed fingerprint"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint="0" * 64,
        )


def test_nonempty_repair_apply_without_deepcoin_client_is_rejected(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=_MiyaRepairClient()
    )

    with pytest.raises(PositionAttributionRepairError, match="Deepcoin client"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            expected_fingerprint=plan.fingerprint,
        )


def test_zero_action_repair_apply_preserves_no_client_behavior(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=_RepairClient()
    )
    assert plan.actions == ()

    result = apply_position_attribution_repair_plan(session_factory, plan)

    assert result.applied == 0
    assert result.already_applied is False


def test_zero_action_apply_cannot_bypass_supplied_reviewed_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=_RepairClient()
    )
    assert plan.actions == ()

    with pytest.raises(PositionAttributionRepairError, match="reviewed fingerprint"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            expected_fingerprint="previous-reviewed-nonzero-plan",
        )


def test_repair_apply_accepts_matching_reviewed_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    result = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == 2


def test_repair_apply_rejects_drift_to_different_coherent_plan(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    reviewed_client = _MiyaRepairClient()
    reviewed_plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=reviewed_client
    )
    current_client = _MiyaRepairClient()
    current_client.positions[1]["posId"] = "1001124099803511"
    current_plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=current_client
    )
    assert current_plan.actions
    assert current_plan.fingerprint != reviewed_plan.fingerprint

    with pytest.raises(PositionAttributionRepairError, match="reviewed fingerprint"):
        apply_position_attribution_repair_plan(
            session_factory,
            current_plan,
            deepcoin_client=current_client,
            expected_fingerprint=reviewed_plan.fingerprint,
        )


def test_miya_repair_apply_rejects_database_fingerprint_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    with session_factory() as session:
        session.query(ExecutionOrderLeg).filter_by(id=244).one().request_json = (
            '{"instId":"ETH-USDT-SWAP","posSide":"short","sz":"9"}'
        )
        session.commit()

    with pytest.raises(PositionAttributionRepairError, match="database evidence changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_miya_repair_apply_is_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    first = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    repeated = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    assert first.applied == 2
    assert first.already_applied is False
    assert repeated.applied == 0
    assert repeated.already_applied is True


def test_repeated_miya_repair_rejects_exchange_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    client.positions.pop()

    with pytest.raises(PositionAttributionRepairError, match="live positions changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repeated_miya_repair_rejects_api_error(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    def fail_positions():
        raise RuntimeError("positions unavailable after apply")

    client.list_positions = fail_positions
    with pytest.raises(PositionAttributionRepairError, match="evidence unavailable"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repeated_miya_repair_rejects_database_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    with session_factory() as session:
        session.query(ExecutionOrderLeg).filter_by(id=244).one().request_json = (
            '{"instId":"ETH-USDT-SWAP","posSide":"short","sz":"9"}'
        )
        session.commit()

    with pytest.raises(PositionAttributionRepairError, match="database evidence changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repeated_miya_repair_rejects_binding_evidence_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    with session_factory() as session:
        session.query(ExecutionBinding).filter_by(id=123).one().margin_mode = (
            "isolated"
        )
        session.commit()

    with pytest.raises(PositionAttributionRepairError, match="database evidence changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_miya_repair_api_error_remains_unresolved_and_unapplied(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    client = _MiyaRepairClient()

    def fail_positions():
        raise RuntimeError("positions unavailable")

    client.list_positions = fail_positions
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    assert plan.actions == ()
    assert plan.unresolved_conflicts == [
        {"evidence_source_errors": {"positions": "positions unavailable"}}
    ]
    with pytest.raises(PositionAttributionRepairError, match="unresolved"):
        apply_position_attribution_repair_plan(
            session_factory, plan, deepcoin_client=client
        )


def test_repair_plan_clears_legacy_weak_verified_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:9:9:ETH:short",
            kol_id="legacy",
            chat_id=9,
            message_id=9,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            pos_id="later-position",
            status="active",
        )
        session.add(binding)
        session.commit()
        binding_id = int(binding.id)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="old-order",
            pos_id="later-position",
            status="active",
            attribution_status="verified",
            attribution_evidence={"evidence_type": "exact_regular_order_id"},
            request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "1.5"},
        ),
    )
    client = _RepairClient()
    client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "later-position",
            "posSide": "short",
            "pos": "1.5",
            "avgPx": "1840",
            "cTime": "1782788876000",
        }
    ]

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert [(action.action, action.leg_id) for action in plan.actions] == [
        ("clear_untrusted_verified_position", 1)
    ]
    assert plan.actions[0].new_pos_id is None
    assert plan.actions[0].new_attribution_status == "unassigned"


def test_repair_plan_includes_historical_cleanup_without_mutating_database(tmp_path):
    session_factory = _seed_historical_duplicate_fixture(tmp_path)
    before = _historical_fixture_state(session_factory)

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=_HistoricalCleanupClient(),
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )

    assert plan.historical_actions
    assert plan.historical_actions[0].action == "clear_redundant_historical_position"
    assert _historical_fixture_state(session_factory) == before


def test_repair_plan_excludes_current_live_position_from_historical_actions(tmp_path):
    session_factory = _seed_historical_duplicate_fixture(tmp_path)

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=_HistoricalCleanupClient(live_position_ids=["p1"]),
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )

    assert all(action.old_pos_id != "p1" for action in plan.historical_actions)
    assert all(
        conflict.get("reason") != "historical_position_still_exchange_active"
        for conflict in plan.unresolved_conflicts
    )


@pytest.mark.parametrize("mutation", ["lifecycle", "execution_event", "close_reservation"])
def test_historical_repair_fingerprint_changes_when_terminal_evidence_changes(
    tmp_path, mutation
):
    session_factory = _seed_historical_duplicate_fixture(tmp_path)
    client = _HistoricalCleanupClient()
    first = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )
    with session_factory() as session:
        if mutation == "lifecycle":
            row = session.get(StrategyLifecycle, 100)
            row.exit_reason = "stop_loss"
        elif mutation == "execution_event":
            session.add(
                ExecutionEvent(
                    execution_binding_id=10,
                    strategy_instance_id="deepcoin:10:10:BTC:long",
                    venue="deepcoin",
                    action="close_position_market",
                    status="completed",
                    pos_id="p1",
                    created_at=datetime(2026, 7, 15, 4, 1),
                )
            )
        else:
            session.add(
                BoundPositionCloseReservation(
                    pos_id="p1",
                    execution_binding_id=10,
                    status="completed",
                    created_at=datetime(2026, 7, 15, 4, 1),
                    updated_at=datetime(2026, 7, 15, 4, 1),
                )
            )
        session.commit()
    second = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )

    assert second.database_fingerprint != first.database_fingerprint
    assert second.fingerprint != first.fingerprint


def test_historical_cleanup_apply_is_atomic_audited_indexed_and_idempotent(tmp_path):
    session_factory = _seed_historical_duplicate_fixture(tmp_path)
    client = _HistoricalCleanupClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )

    assert [action.action for action in plan.historical_actions] == [
        "clear_redundant_historical_position",
        "close_historical_binding",
        "install_position_ownership_unique_index",
    ]
    result = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == 3
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        audits = (
            session.query(PositionAttributionAudit)
            .filter_by(event_type="historical_cleanup")
            .order_by(PositionAttributionAudit.id)
            .all()
        )
        indexes = {
            row[1]
            for row in session.connection()
            .exec_driver_sql("PRAGMA index_list(execution_order_legs)")
            .fetchall()
        }
    assert [leg.pos_id for leg in legs] == ["p1", None]
    assert len(audits) == 3
    assert "uq_execution_order_legs_venue_pos" in indexes

    repeated = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    assert repeated.applied == 0
    assert repeated.already_applied is True

    with session_factory() as session:
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=10,
                strategy_instance_id="deepcoin:10:10:BTC:long",
                leg_index=3,
                purpose="entry",
                order_kind="market",
                order_id="duplicate-after-cleanup",
                pos_id="p1",
                venue="deepcoin",
                status="active",
                attribution_status="verified",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_historical_cleanup_updates_exact_execution_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "lifecycle-cleanup.db")
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=96,
                strategy_instance_id="deepcoin:96:96:BTC:long",
                kol_id="historical",
                chat_id=96,
                message_id=96,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                pos_id="old-position",
                status="unknown",
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=188,
                execution_binding_id=96,
                strategy_instance_id="deepcoin:96:96:BTC:long",
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id="old-position",
                pos_id="old-position",
                venue="deepcoin",
                status="active",
                attribution_status="attribution_conflict",
            )
        )
        session.add(
            StrategyLifecycle(
                id=420,
                chat_id=96,
                message_id=96,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 1),
                entered_at=datetime(2026, 7, 1),
                execution_binding_id=96,
            )
        )
        session.add(
            BoundPositionCloseReservation(
                pos_id="old-position",
                execution_binding_id=96,
                status="completed",
                created_at=datetime(2026, 7, 2),
                updated_at=datetime(2026, 7, 2),
            )
        )
        session.commit()
    client = _HistoricalCleanupClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )

    assert [action.action for action in plan.historical_actions] == [
        "terminalize_historical_entry_leg",
        "close_historical_binding",
        "exit_historical_lifecycle",
    ]
    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, 188)
        binding = session.get(ExecutionBinding, 96)
        lifecycle = session.get(StrategyLifecycle, 420)
    assert leg.status == "manually_closed"
    assert leg.terminal_reason == "historical_close_reservation"
    assert binding.status == "closed"
    assert binding.pos_id is None
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"
    assert lifecycle.exited_at == datetime(2026, 7, 15, 4, 0)


def test_unique_index_failure_rolls_back_cleanup_and_audits(tmp_path, monkeypatch):
    session_factory = _seed_historical_duplicate_fixture(tmp_path)
    client = _HistoricalCleanupClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    )
    before = _historical_fixture_state(session_factory)

    def fail_index(_connection):
        raise RuntimeError("index failure")

    monkeypatch.setattr(
        repair_module,
        "ensure_position_ownership_unique_index",
        fail_index,
        raising=False,
    )
    with pytest.raises(PositionAttributionRepairError, match="repair transaction failed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )

    assert _historical_fixture_state(session_factory) == before
    with session_factory() as session:
        assert (
            session.query(PositionAttributionAudit)
            .filter_by(event_type="historical_cleanup")
            .count()
            == 0
        )


def test_repair_plan_preserves_explicit_legacy_manual_bind(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:9:10:ETH:short",
            kol_id="manual",
            chat_id=9,
            message_id=10,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            pos_id="manual-position",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=int(binding.id),
                leg_index=1,
                purpose="entry",
                order_kind="manual_bind",
                pos_id="manual-position",
                venue="deepcoin",
                status="active",
                attribution_status="verified",
                attribution_evidence_json='{"source":"manual_operator_bind"}',
            )
        )
        session.commit()
    client = _RepairClient()
    client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "manual-position",
            "posSide": "short",
            "pos": "1.5",
            "avgPx": "1840",
            "cTime": "1782788876000",
        }
    ]

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.unresolved_conflicts == []


def test_repair_apply_is_atomic_audited_and_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    result = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == 3
    assert result.already_applied is False
    wrong = list_execution_order_legs(session_factory, execution_binding_id=112)[0]
    target = list_execution_order_legs(session_factory, execution_binding_id=120)[0]
    shuqin = list_execution_order_legs(session_factory, execution_binding_id=121)[0]
    assert wrong.pos_id is None
    assert wrong.status == "manually_cancelled"
    assert target.pos_id == "1001124083099498"
    assert target.attribution_status == "verified"
    assert shuqin.status == "open"
    with session_factory() as session:
        audits = session.query(PositionAttributionAudit).filter_by(
            event_type="historical_repair"
        ).all()
    assert len(audits) == 3

    repeated = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    assert repeated.applied == 0
    assert repeated.already_applied is True


def test_unrelated_manually_closed_binding_is_not_resurrected_by_repair(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_miya_equivalent_component(session_factory)
    with session_factory() as session:
        binding = ExecutionBinding(
            id=999,
            strategy_instance_id="deepcoin:manual:ETH:long",
            kol_id="manual",
            chat_id=999,
            message_id=999,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            pos_id=None,
            status="closed",
            last_exchange_status="manual_closed_or_not_found_on_exchange",
        )
        session.add(binding)
        session.add(
            ExecutionOrderLeg(
                id=999,
                execution_binding_id=999,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id="stale-manual-pos",
                pos_id="stale-manual-pos",
                venue="deepcoin",
                status="manually_closed",
                terminal_reason="manual_position_missing",
                attribution_status="verified",
                attribution_evidence_json='{"policy_version":2}',
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=999,
                message_id=999,
                symbol="ETH",
                side="long",
                lifecycle_status="exited",
                exit_reason="manual",
                signal_at=datetime(2026, 7, 1),
                execution_binding_id=999,
            )
        )
        session.commit()
    client = _MiyaRepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )

    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, 999)
        lifecycle = session.query(StrategyLifecycle).filter_by(chat_id=999).one()
        assert (binding.status, binding.pos_id) == ("closed", None)
        assert binding.last_exchange_status == "manual_closed_or_not_found_on_exchange"
        assert (lifecycle.lifecycle_status, lifecycle.exit_reason) == (
            "exited",
            "manual",
        )


def test_absent_verified_pos_id_cannot_activate_affected_binding_or_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            id=130,
            strategy_instance_id="deepcoin:absent:ETH:short",
            kol_id="absent",
            chat_id=130,
            message_id=130,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            pos_id=None,
            status="closed",
            last_exchange_status="manual_closed_or_not_found_on_exchange",
        )
        session.add(binding)
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=130,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="absent-pos",
                    pos_id="absent-pos",
                    venue="deepcoin",
                    status="active",
                    attribution_status="verified",
                    attribution_evidence_json='{"policy_version":2}',
                ),
                ExecutionOrderLeg(
                    execution_binding_id=130,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="cancelled-entry",
                    client_order_id="cancelled-client",
                    venue="deepcoin",
                    status="open",
                    attribution_status="unassigned",
                ),
            ]
        )
        session.add(
            StrategyLifecycle(
                chat_id=130,
                message_id=130,
                symbol="ETH",
                side="short",
                lifecycle_status="exited",
                exit_reason="manual",
                signal_at=datetime(2026, 7, 1),
                execution_binding_id=130,
            )
        )
        session.commit()
    client = _RepairClient()
    client.positions = []
    client.list_trigger_order_history = lambda *, inst_id: [
        {
            "instId": inst_id,
            "ordId": "cancelled-entry",
            "clOrdId": "cancelled-client",
            "state": "cancelled",
            "posSide": "short",
        }
    ]
    plan = build_position_attribution_repair_plan(
        session_factory, deepcoin_client=client
    )
    assert any(action.binding_id == 130 for action in plan.actions)

    apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, 130)
        lifecycle = session.query(StrategyLifecycle).filter_by(chat_id=130).one()
        assert binding.status != "active"
        assert binding.pos_id is None
        assert (lifecycle.lifecycle_status, lifecycle.exit_reason) == (
            "exited",
            "manual",
        )


def test_repair_apply_rejects_stale_database_without_partial_changes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    with session_factory() as session:
        target = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=120).one()
        target.order_id = "changed-after-review"
        session.commit()

    with pytest.raises(PositionAttributionRepairError, match="stale repair plan"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )

    wrong = list_execution_order_legs(session_factory, execution_binding_id=112)[0]
    target = list_execution_order_legs(session_factory, execution_binding_id=120)[0]
    assert wrong.pos_id == "1001124083099498"
    assert target.pos_id is None


def test_repair_apply_rejects_changed_request_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    with session_factory() as session:
        target = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=120).one()
        target.request_json = '{"instId":"ETH-USDT-SWAP","posSide":"short","sz":"9"}'
        session.commit()

    with pytest.raises(PositionAttributionRepairError, match="stale repair plan"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repair_apply_rejects_changed_live_position_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    client.positions = []

    with pytest.raises(PositionAttributionRepairError, match="live positions changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repair_apply_rejects_changed_exchange_fill_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    original = client.list_trigger_order_history

    def changed_history(*, inst_id):
        rows = original(inst_id=inst_id)
        rows[1] = {**rows[1], "posId": "different-position"}
        return rows

    client.list_trigger_order_history = changed_history

    with pytest.raises(PositionAttributionRepairError, match="evidence changed"):
        apply_position_attribution_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            expected_fingerprint=plan.fingerprint,
        )


def test_repair_apply_rejects_unresolved_conflicts_at_core_boundary(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    client = _RepairClient()
    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    unsafe_plan = replace(plan, unresolved_conflicts=[{"leg_ids": [1, 2]}])

    with pytest.raises(PositionAttributionRepairError, match="unresolved"):
        apply_position_attribution_repair_plan(
            session_factory,
            unsafe_plan,
            deepcoin_client=client,
        )


def test_repair_plan_never_assigns_ambiguous_candidate_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_incident(session_factory)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=122,
                strategy_instance_id="deepcoin:4:122:ETH:short",
                kol_id="另一个候选",
                chat_id=4,
                message_id=122,
                symbol="ETH",
                side="short",
                venue="deepcoin",
                status="open",
            )
        )
        session.commit()
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=122,
            leg_index=1,
            order_kind="trigger_limit",
            order_id="zhige-filled",
            client_order_id="zhige-client",
            status="filled",
            request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "1.5"},
        ),
    )

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=_RepairClient(),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert plan.unresolved_conflicts
    assert not any(
        action.action in {"clear_stale_position", "assign_verified_position"}
        for action in plan.actions
    )
