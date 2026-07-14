from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionOrderLegRecord,
    list_execution_order_legs,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg, PositionAttributionAudit
from telegram_kol_research.position_attribution_repair import (
    PositionAttributionRepairError,
    apply_position_attribution_repair_plan,
    build_position_attribution_repair_plan,
)


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
    )
    assert repeated.applied == 0
    assert repeated.already_applied is True


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
