from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    StrategyLifecycle,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)


NOW = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _Client:
    def __init__(self) -> None:
        self.pending = [
            {
                "instId": "ETH-USDT-SWAP",
                "ordId": "reviewed-1",
                "triggerOrderType": "Conditional",
                "side": "buy",
                "posSide": "long",
                "sz": "3",
                "triggerPx": "1827",
                "ordPx": "1827",
                "closeSLTriggerPrice": "1795",
                "closeTPTriggerPrice": "0",
                "cTime": "1786381201000",
                "uTime": "1786381201000",
            },
            {
                "instId": "ETH-USDT-SWAP",
                "ordId": "reviewed-2",
                "triggerOrderType": "Conditional",
                "side": "buy",
                "posSide": "long",
                "sz": "3",
                "triggerPx": "1812",
                "ordPx": "1812",
                "closeSLTriggerPrice": "1795",
                "closeTPTriggerPrice": "0",
                "cTime": "1786381203000",
                "uTime": "1786381203000",
            },
        ]
        self.trigger_history: list[dict[str, str]] = []
        self.fills: list[dict[str, str]] = []
        self.positions: list[dict[str, str]] = []
        self.regular: list[dict[str, str]] = []
        self.cancel_payloads: list[dict[str, str]] = []
        self.cancel_exception: Exception | None = None
        self.cancel_response: object | None = None
        self.mutate_sibling_after_cancel = False

    def list_positions(self, *, inst_id=None):
        return [row for row in self.positions if not inst_id or row.get("instId") == inst_id]

    def list_open_orders(self, *, inst_id=None):
        return [row for row in self.regular if not inst_id or row.get("instId") == inst_id]

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending if row.get("instId") == inst_id]

    def list_trigger_order_history(self, *, inst_id):
        return [row for row in self.trigger_history if row.get("instId") == inst_id]

    def list_trade_fills(self, *, inst_id=None):
        return [row for row in self.fills if not inst_id or row.get("instId") == inst_id]

    def cancel_trigger_order(self, payload):
        self.cancel_payloads.append(dict(payload))
        if self.cancel_exception is not None:
            raise self.cancel_exception
        order_id = str(payload["ordId"])
        self.pending = [row for row in self.pending if row.get("ordId") != order_id]
        self.trigger_history.append(
            {
                "instId": str(payload["instId"]),
                "ordId": order_id,
                "state": "cancelled",
                "triggerOrderType": "Conditional",
            }
        )
        if self.mutate_sibling_after_cancel and self.pending:
            self.pending[0]["sz"] = "99"
        return (
            self.cancel_response
            if self.cancel_response is not None
            else {"code": "0", "data": [{"ordId": order_id, "sCode": "0"}]}
        )


def _request(*, price: str, order_id: str) -> dict[str, str]:
    return {
        "clOrdId": f"client-{order_id}",
        "instId": "ETH-USDT-SWAP",
        "isCrossMargin": "1",
        "mrgPosition": "split",
        "orderType": "limit",
        "posSide": "long",
        "price": price,
        "productGroup": "Swap",
        "side": "buy",
        "slOrdPx": "-1",
        "slTriggerPx": "1795.0",
        "slTriggerPxType": "last",
        "sz": "3.0",
        "tdMode": "cross",
        "triggerPrice": price,
        "triggerPxType": "last",
    }


def _seed(session_factory):
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:test",
            chat_id=101,
            message_id=202,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="open",
            strategy_instance_id="deepcoin:101:202:ETH:long",
            margin_mode="cross",
            position_mode="split",
            last_exchange_status="entry_order_pending",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=101,
            message_id=202,
            symbol="ETH",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            entry_range_low=1810,
            entry_range_high=1825,
            stop_loss=1795,
            take_profit="1860/1885/1925",
            filled_tp_index=-1,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        leg_ids: list[int] = []
        for index, (order_id, price) in enumerate(
            (("reviewed-1", "1827.0"), ("reviewed-2", "1812.0")),
            start=1,
        ):
            request = _request(price=price, order_id=order_id)
            leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="trigger_limit",
                order_id=order_id,
                client_order_id=f"client-{order_id}",
                venue="deepcoin",
                attribution_status="unassigned",
                status="pending",
                request_json=json.dumps(request, sort_keys=True),
            )
            session.add(leg)
            session.flush()
            leg_ids.append(int(leg.id))
            session.add(
                TriggerProtectionIntent(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    request_fingerprint=_fingerprint(request),
                    pre_submit_tpsl_baseline_json="[]",
                    correlation_id=f"trigger-protection:{leg.id}",
                    parent_trigger_order_id=order_id,
                    recovery_state="pending",
                    retry_attempts=0,
                )
            )
            session.add_all(
                [
                    PositionProtectionLeg(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=leg.id,
                        role="primary_stop",
                        leg_index=1,
                        planned_trigger_price="1795.0",
                        planned_size="3.0",
                        parent_entry_order_id=order_id,
                        status="planned",
                    ),
                    PositionProtectionLeg(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=leg.id,
                        role="backup_stop",
                        leg_index=1,
                        parent_entry_order_id=order_id,
                        status="planned",
                    ),
                ]
            )
            session.add(
                TriggerTakeProfitConvergence(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    desired_take_profits_json='[{"price":"1860","allocation_pct":100}]',
                    status="waiting_backup_stop",
                    reason_code="convergence_waiting_backup_stop",
                )
            )
        session.commit()
        return int(binding.id), int(lifecycle.id), tuple(leg_ids)


def _targets(binding_id: int, lifecycle_id: int, leg_ids: tuple[int, int]):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        ReviewedPendingEntryTarget,
    )

    values = (
        ("reviewed-1", leg_ids[0], "1827", "7f9f86c10c30936a062984b6a5839b5db293f9dcbd0222d45a85b90c37f06130"),
        ("reviewed-2", leg_ids[1], "1812", "a05cae373185d2b221b47297b23c25cd854affc402310588ed4a19e3f8ffb3e6"),
    )
    targets = []
    for order_id, leg_id, price, _production_fingerprint in values:
        targets.append(
            ReviewedPendingEntryTarget(
                order_id=order_id,
                instrument_id="ETH-USDT-SWAP",
                lifecycle_id=lifecycle_id,
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                trigger_price=price,
                size="3",
                embedded_stop_price="1795",
                request_fingerprint=_fingerprint(
                    _request(price=f"{price}.0", order_id=order_id)
                ),
            )
        )
    return tuple(targets)


def test_plan_requires_exact_reviewed_exchange_and_local_ownership(tmp_path):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    client = _Client()

    before_counts = {}
    with session_factory() as session:
        for model in (
            ExecutionBinding,
            StrategyLifecycle,
            ExecutionOrderLeg,
            TriggerProtectionIntent,
            PositionProtectionLeg,
            TriggerTakeProfitConvergence,
            ExecutionEvent,
        ):
            before_counts[model.__tablename__] = session.query(model).count()

    plan = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(binding_id, lifecycle_id, leg_ids),
        now=NOW,
    )

    assert len(plan.actions) == 2
    assert plan.conflicts == ()
    assert plan.completed_order_ids == ()
    assert len(plan.fingerprint) == 64
    assert {action.order_id for action in plan.actions} == {
        "reviewed-1",
        "reviewed-2",
    }
    assert {
        (
            action.execution_binding_id,
            action.execution_order_leg_id,
            action.lifecycle_id,
        )
        for action in plan.actions
    } == {
        (binding_id, leg_ids[0], lifecycle_id),
        (binding_id, leg_ids[1], lifecycle_id),
    }
    assert client.cancel_payloads == []

    with session_factory() as session:
        after_counts = {
            model.__tablename__: session.query(model).count()
            for model in (
                ExecutionBinding,
                StrategyLifecycle,
                ExecutionOrderLeg,
                TriggerProtectionIntent,
                PositionProtectionLeg,
                TriggerTakeProfitConvergence,
                ExecutionEvent,
            )
        }
    assert after_counts == before_counts


def test_plan_fails_closed_for_pending_trigger_without_order_identity(tmp_path):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    client = _Client()
    client.pending.append(
        {
            "instId": "ETH-USDT-SWAP",
            "triggerOrderType": "Conditional",
            "side": "buy",
            "posSide": "long",
            "sz": "1",
            "triggerPx": "1700",
            "closeSLTriggerPrice": "1600",
        }
    )

    plan = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(binding_id, lifecycle_id, leg_ids),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "unidentified_pending_trigger"},
    )


def test_plan_blocks_all_actions_when_account_has_position(tmp_path):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    client = _Client()
    client.positions.append(
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "unexpected-position",
            "posSide": "long",
            "pos": "1",
        }
    )

    plan = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(binding_id, lifecycle_id, leg_ids),
        now=NOW,
    )

    assert plan.actions == ()
    assert {item["reason"] for item in plan.conflicts} == {
        "live_position_present"
    }


def test_plan_rejects_changed_embedded_stop_and_fill_evidence(tmp_path):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    client = _Client()
    client.pending[0]["closeSLTriggerPrice"] = "1700"
    client.fills.append(
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": "reviewed-2",
            "fillSz": "1",
        }
    )

    plan = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(binding_id, lifecycle_id, leg_ids),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "reviewed-1", "reason": "reviewed_exchange_row_changed"},
        {"order_id": "reviewed-2", "reason": "reviewed_order_has_fill_evidence"},
    )


def test_fixed_reviewed_target_set_contains_exact_seven_orders():
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        REVIEWED_PENDING_ENTRY_TARGETS,
    )

    assert {target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS} == {
        "1001124718697641",
        "1001124718698413",
        "1001124760022605",
        "1001124760022650",
        "1001124898942178",
        "1001124905627977",
        "1001124905628046",
    }
