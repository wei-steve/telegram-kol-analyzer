from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    StrategyLifecycle,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReadOnlyClient:
    def __init__(self) -> None:
        self.positions = []
        self.regular = []
        self.pending = []
        self.history = []
        self.fills = []
        self.write_calls = 0

    def _for(self, rows, inst_id):
        return [row for row in rows if row.get("instId") == inst_id]

    def list_positions(self, *, inst_id):
        return self._for(self.positions, inst_id)

    def list_open_orders(self, *, inst_id):
        return self._for(self.regular, inst_id)

    def list_trigger_orders_pending(self, *, inst_id):
        return self._for(self.pending, inst_id)

    def list_trigger_order_history(self, *, inst_id):
        return self._for(self.history, inst_id)

    def list_trade_fills(self, *, inst_id):
        return self._for(self.fills, inst_id)

    def cancel_trigger_order(self, payload):
        self.write_calls += 1
        raise AssertionError("manual reconciliation must not write to Deepcoin")


class RuntimeGuard:
    def __init__(self, *, fail_on_call=None, events=None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.events = events

    def __call__(self) -> None:
        self.calls += 1
        if self.events is not None:
            self.events.append(f"guard:{self.calls}")
        if self.calls == self.fail_on_call:
            raise ValueError("maintenance_runtime_not_stopped")


def _record_cancelled_history(client: ReadOnlyClient, target) -> None:
    client.history.append(
        {
            "instId": target.instrument_id,
            "ordId": target.order_id,
            "state": "cancelled",
        }
    )


def _seed_pending_target(session_factory):
    from telegram_kol_research.reviewed_pending_entry_targets import (
        ReviewedPendingEntryTarget,
    )

    order_id = "manual-cancel-1"
    request = {
        "instId": "ETH-USDT-SWAP",
        "posSide": "long",
        "side": "buy",
        "triggerPrice": "1827",
        "sz": "3",
        "slTriggerPx": "1795",
    }
    request_fingerprint = _sha(request)
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:test",
            chat_id=1,
            message_id=2,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="open",
            strategy_instance_id="deepcoin:1:2:ETH:long",
            margin_mode="cross",
            position_mode="split",
            last_exchange_status="entry_order_pending",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=1,
            message_id=2,
            symbol="ETH",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id=order_id,
            venue="deepcoin",
            status="pending",
            request_json=json.dumps(request, sort_keys=True),
        )
        session.add(leg)
        session.flush()
        session.add(
            TriggerProtectionIntent(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                request_fingerprint=request_fingerprint,
                pre_submit_tpsl_baseline_json="[]",
                correlation_id=f"manual:{leg.id}",
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
                    role=role,
                    leg_index=1,
                    planned_trigger_price=(
                        "1795" if role == "primary_stop" else None
                    ),
                    parent_entry_order_id=order_id,
                    status="planned",
                )
                for role in ("primary_stop", "backup_stop")
            ]
        )
        session.add(
            TriggerTakeProfitConvergence(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                desired_take_profits_json="[]",
                status="waiting_position",
                reason_code="waiting_position",
            )
        )
        session.commit()
        return ReviewedPendingEntryTarget(
            order_id=order_id,
            instrument_id="ETH-USDT-SWAP",
            lifecycle_id=lifecycle.id,
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            chat_id=1,
            message_id=2,
            strategy_instance_id="deepcoin:1:2:ETH:long",
            trigger_price="1827",
            size="3",
            embedded_stop_price="1795",
            request_fingerprint=request_fingerprint,
        )


def _seed_foreign_binding_and_leg(session):
    binding = ExecutionBinding(
        kol_id="group:foreign",
        chat_id=99,
        message_id=100,
        symbol="ETH",
        side="long",
        venue="deepcoin",
        status="open",
        strategy_instance_id="deepcoin:99:100:ETH:long",
    )
    session.add(binding)
    session.flush()
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=1,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="foreign-order",
        venue="deepcoin",
        status="pending",
        request_json="{}",
    )
    session.add(leg)
    session.flush()
    return binding, leg


def _seed_all_canonical_targets(session_factory):
    from telegram_kol_research.reviewed_pending_entry_targets import (
        REVIEWED_PENDING_ENTRY_TARGETS,
    )

    grouped = {}
    for target in REVIEWED_PENDING_ENTRY_TARGETS:
        grouped.setdefault(target.execution_binding_id, []).append(target)
    with session_factory() as session:
        for binding_id, targets in grouped.items():
            first = targets[0]
            symbol = first.instrument_id.split("-", 1)[0]
            chat_id = first.chat_id
            message_id = first.message_id
            strategy_id = first.strategy_instance_id
            session.add(
                ExecutionBinding(
                    id=binding_id,
                    kol_id=f"group:{binding_id}",
                    chat_id=chat_id,
                    message_id=message_id,
                    symbol=symbol,
                    side="long",
                    venue="deepcoin",
                    status="open",
                    strategy_instance_id=strategy_id,
                    margin_mode="cross",
                    position_mode="split",
                    last_exchange_status="entry_order_pending",
                )
            )
            session.add(
                StrategyLifecycle(
                    id=first.lifecycle_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    symbol=symbol,
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    execution_binding_id=binding_id,
                )
            )
            session.flush()
            for leg_index, target in enumerate(targets, start=1):
                request = {
                    "instId": target.instrument_id,
                    "posSide": "long",
                    "side": "buy",
                    "slTriggerPx": target.embedded_stop_price,
                    "sz": target.size,
                    "triggerPrice": target.trigger_price,
                }
                leg = ExecutionOrderLeg(
                    id=target.execution_order_leg_id,
                    execution_binding_id=binding_id,
                    strategy_instance_id=strategy_id,
                    leg_index=leg_index,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=target.order_id,
                    venue="deepcoin",
                    status="pending",
                    request_json=json.dumps(request, sort_keys=True),
                )
                session.add(leg)
                session.flush()
                session.add(
                    TriggerProtectionIntent(
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg.id,
                        request_fingerprint=target.request_fingerprint,
                        pre_submit_tpsl_baseline_json="[]",
                        correlation_id=f"canonical:{leg.id}",
                        parent_trigger_order_id=target.order_id,
                        recovery_state="pending",
                        retry_attempts=0,
                    )
                )
                session.add_all(
                    [
                        PositionProtectionLeg(
                            venue="deepcoin",
                            execution_binding_id=binding_id,
                            execution_order_leg_id=leg.id,
                            role=role,
                            leg_index=1,
                            planned_trigger_price=(
                                target.embedded_stop_price
                                if role == "primary_stop"
                                else None
                            ),
                            parent_entry_order_id=target.order_id,
                            status="planned",
                        )
                        for role in ("primary_stop", "backup_stop")
                    ]
                )
                session.add(
                    TriggerTakeProfitConvergence(
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg.id,
                        desired_take_profits_json="[]",
                        status="waiting_position",
                        reason_code="waiting_position",
                    )
                )
        session.commit()
    return REVIEWED_PENDING_ENTRY_TARGETS


def test_manual_reconciliation_terminalizes_locally_and_seeds_authority(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    backup_path = tmp_path / "research.before-manual-cancel.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    assert plan.status == "ready"

    result = apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=backup_path,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        expected_fingerprint=plan.fingerprint,
        now=NOW,
    )

    assert result.status == "completed"
    assert backup_path.is_file()
    assert client.write_calls == 0
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "cancelled"
        assert session.get(ExecutionBinding, target.execution_binding_id).status == "cancelled"
        assert session.get(StrategyLifecycle, target.lifecycle_id).lifecycle_status == "expired"
        intent = session.query(TriggerProtectionIntent).one()
        assert intent.recovery_state == "resolved"
        assert intent.recovery_disposition == "terminal"
        assert {row.status for row in session.query(PositionProtectionLeg)} == {"cancelled"}
        assert session.query(TriggerTakeProfitConvergence).one().status == "completed"
        assert session.query(ExecutionEvent).filter_by(
            action="reconcile_manual_pending_entry_cancel"
        ).count() == 1
        authority = session.query(TradingSetting).filter_by(
            key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
        ).one()
        assert json.loads(authority.value_json)["state"] == "idle"


def test_manual_reconciliation_refuses_any_live_exchange_object(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.positions = [{"instId": "ETH-USDT-SWAP", "posId": "live"}]

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "live_position_present"


def test_manual_reconciliation_default_clock_is_taken_after_exchange_reads(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)

    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
    )

    assert plan.status == "ready"
    assert plan.reason_code is None


def test_manual_reconciliation_proves_stopped_runtime_before_exchange_reads(
    tmp_path,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    events = []
    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    original = client.list_positions

    def list_positions(*, inst_id):
        events.append("exchange-read")
        return original(inst_id=inst_id)

    client.list_positions = list_positions
    guard = RuntimeGuard(events=events)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=guard,
        now=NOW,
    )

    assert plan.status == "ready"
    assert events[0] == "guard:1"
    assert guard.calls == 1


def test_manual_reconciliation_without_runtime_proof_blocks_before_reads(
    tmp_path,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "maintenance_runtime_not_stopped"


def test_manual_reconciliation_reproves_runtime_before_backup(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    backup_path = tmp_path / "backup.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    guard = RuntimeGuard(fail_on_call=2)

    with pytest.raises(ValueError, match="maintenance_runtime_not_stopped"):
        apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=database_path,
            backup_path=backup_path,
            deepcoin_client=client,
            targets=(target,),
            expected_fingerprint=plan.fingerprint,
            runtime_guard=guard,
            now=NOW,
        )

    assert guard.calls == 2
    assert not backup_path.exists()
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"


@pytest.mark.parametrize(
    ("source", "row", "reason"),
    [
        ("fills", {"ordId": "manual-cancel-1", "state": "filled"}, "target_fill_present"),
        (
            "history",
            {"ordId": "manual-cancel-1", "state": "filled"},
            "target_history_not_cancelled",
        ),
        (
            "history",
            {"ordId": "manual-cancel-1"},
            "target_history_not_cancelled",
        ),
    ],
)
def test_manual_reconciliation_refuses_target_fill_or_ambiguous_history(
    tmp_path,
    source,
    row,
    reason,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    if source == "fills":
        _record_cancelled_history(client, target)
    row["instId"] = target.instrument_id
    getattr(client, source).append(row)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == reason


@pytest.mark.parametrize(
    "alias",
    (
        "ordId",
        "orderId",
        "order_id",
        "orderSysID",
        "OrderSysID",
        "id",
        "algoId",
        "triggerOrderId",
        "trigger_order_id",
    ),
)
def test_manual_reconciliation_refuses_target_fill_for_every_supported_alias(
    tmp_path,
    alias,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    client.fills.append(
        {
            "instId": target.instrument_id,
            alias: target.order_id,
            "state": "filled",
        }
    )

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "target_fill_present"


def test_manual_reconciliation_accepts_explicit_cancelled_target_history(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.history.append(
        {"instId": target.instrument_id, "ordId": target.order_id, "state": "canceled"}
    )

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "ready"


@pytest.mark.parametrize(
    "alias",
    (
        "ordId",
        "orderId",
        "order_id",
        "orderSysID",
        "OrderSysID",
        "id",
        "algoId",
        "triggerOrderId",
        "trigger_order_id",
    ),
)
def test_manual_reconciliation_accepts_cancelled_history_supported_alias(
    tmp_path,
    alias,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.history.append(
        {
            "instId": target.instrument_id,
            alias: target.order_id,
            "state": "cancelled",
        }
    )

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "ready"


def test_manual_reconciliation_requires_one_cancelled_history_row_per_target(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()

    missing = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    assert missing.status == "blocked"
    assert missing.reason_code == "target_cancelled_history_missing"

    client.history = [
        {"instId": target.instrument_id, "ordId": target.order_id, "state": "cancelled"},
        {"instId": target.instrument_id, "orderId": target.order_id, "state": "cancelled"},
    ]
    duplicate = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    assert duplicate.status == "blocked"
    assert duplicate.reason_code == "target_cancelled_history_not_unique"


def test_manual_reconciliation_refuses_conflicting_order_id_aliases(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.history = [
        {
            "instId": target.instrument_id,
            "ordId": "different-child",
            "triggerOrderId": target.order_id,
            "state": "cancelled",
        }
    ]

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "target_history_identity_conflict"


@pytest.mark.parametrize(
    ("target_alias", "conflict_alias"),
    (
        ("ordId", "orderId"),
        ("orderId", "order_id"),
        ("order_id", "orderSysID"),
        ("orderSysID", "OrderSysID"),
        ("OrderSysID", "id"),
        ("id", "algoId"),
        ("algoId", "triggerOrderId"),
        ("triggerOrderId", "trigger_order_id"),
        ("trigger_order_id", "ordId"),
    ),
)
def test_manual_reconciliation_refuses_every_history_alias_conflict(
    tmp_path,
    target_alias,
    conflict_alias,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.history.append(
        {
            "instId": target.instrument_id,
            target_alias: target.order_id,
            conflict_alias: "different-order",
            "state": "cancelled",
        }
    )

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "target_history_identity_conflict"


@pytest.mark.parametrize("instrument_id", (None, "BTC-USDT-SWAP", "eth-usdt-swap"))
def test_manual_reconciliation_refuses_history_without_exact_instrument(
    tmp_path,
    instrument_id,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    row = {"ordId": target.order_id, "state": "cancelled"}
    if instrument_id is not None:
        row["instId"] = instrument_id
    client.list_trigger_order_history = (
        lambda *, inst_id: [row] if inst_id == target.instrument_id else []
    )

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "target_history_instrument_mismatch"


@pytest.mark.parametrize(
    "drift",
    (
        "binding_venue",
        "binding_symbol",
        "binding_side",
        "lifecycle_symbol",
        "lifecycle_side",
        "duplicate_lifecycle",
        "leg_venue",
        "leg_strategy",
        "request_fingerprint",
        "request_instrument",
        "request_trigger_price",
        "request_size",
        "request_stop",
        "intent_binding",
        "intent_leg",
        "protection_parent",
        "protection_stop",
        "protection_binding",
        "protection_leg",
        "convergence_binding",
        "convergence_leg",
    ),
)
def test_manual_reconciliation_refuses_canonical_local_ownership_drift(
    tmp_path,
    drift,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, target.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        intent = session.query(TriggerProtectionIntent).one()
        protection = session.query(PositionProtectionLeg).first()
        convergence = session.query(TriggerTakeProfitConvergence).one()
        foreign_binding, foreign_leg = _seed_foreign_binding_and_leg(session)
        if drift == "binding_venue":
            binding.venue = "other"
        elif drift == "binding_symbol":
            binding.symbol = "BTC"
        elif drift == "binding_side":
            binding.side = "short"
        elif drift == "lifecycle_symbol":
            lifecycle.symbol = "BTC"
        elif drift == "lifecycle_side":
            lifecycle.side = "short"
        elif drift == "duplicate_lifecycle":
            session.add(
                StrategyLifecycle(
                    chat_id=88,
                    message_id=89,
                    symbol="ETH",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    execution_binding_id=target.execution_binding_id,
                )
            )
        elif drift == "leg_venue":
            leg.venue = "other"
        elif drift == "leg_strategy":
            leg.strategy_instance_id = "different-strategy"
        elif drift == "request_fingerprint":
            intent.request_fingerprint = "f" * 64
        elif drift.startswith("request_"):
            request = json.loads(leg.request_json)
            key = {
                "request_instrument": "instId",
                "request_trigger_price": "triggerPrice",
                "request_size": "sz",
                "request_stop": "slTriggerPx",
            }[drift]
            request[key] = "different"
            leg.request_json = json.dumps(request, sort_keys=True)
        elif drift == "intent_binding":
            intent.execution_binding_id = foreign_binding.id
        elif drift == "intent_leg":
            intent.execution_order_leg_id = foreign_leg.id
        elif drift == "protection_parent":
            protection.parent_entry_order_id = "different-order"
        elif drift == "protection_stop":
            primary = session.query(PositionProtectionLeg).filter_by(
                role="primary_stop"
            ).one()
            primary.planned_trigger_price = "different"
        elif drift == "protection_binding":
            protection.execution_binding_id = foreign_binding.id
        elif drift == "protection_leg":
            protection.execution_order_leg_id = foreign_leg.id
        elif drift == "convergence_binding":
            convergence.execution_binding_id = foreign_binding.id
        elif drift == "convergence_leg":
            convergence.execution_order_leg_id = foreign_leg.id
        else:  # pragma: no cover - parameter list is closed above
            raise AssertionError(drift)
        session.commit()

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "reviewed_local_state_changed"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("instId", "BTC-USDT-SWAP"),
        ("side", "sell"),
        ("posSide", "short"),
        ("triggerPrice", "1828"),
        ("sz", "4"),
        ("slTriggerPx", "1794"),
    ),
)
def test_manual_reconciliation_compares_request_economics_beyond_fingerprint(
    tmp_path,
    field,
    value,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        request = json.loads(leg.request_json)
        request[field] = value
        fingerprint = _sha(request)
        leg.request_json = json.dumps(request, sort_keys=True)
        session.query(TriggerProtectionIntent).one().request_fingerprint = fingerprint
        session.commit()
    drifted_target = replace(target, request_fingerprint=fingerprint)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(drifted_target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "reviewed_local_state_changed"


def test_manual_reconciliation_accepts_documented_request_aliases(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    request = {
        "instrument_id": target.instrument_id,
        "posSide": "long",
        "side": "buy",
        "triggerPx": target.trigger_price,
        "size": target.size,
        "slTriggerPrice": target.embedded_stop_price,
    }
    fingerprint = _sha(request)
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        leg.request_json = json.dumps(request, sort_keys=True)
        session.query(TriggerProtectionIntent).one().request_fingerprint = fingerprint
        session.commit()
    aliased_target = replace(target, request_fingerprint=fingerprint)

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(aliased_target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "ready"


def test_manual_reconciliation_refuses_coordinated_strategy_identity_drift(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, target.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        binding.chat_id = 91
        binding.message_id = 92
        lifecycle.chat_id = 91
        lifecycle.message_id = 92
        binding.strategy_instance_id = "deepcoin:91:92:ETH:long"
        leg.strategy_instance_id = binding.strategy_instance_id
        session.commit()

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "reviewed_local_state_changed"


def test_manual_reconciliation_refuses_unreviewed_active_entry_sibling(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=target.execution_binding_id,
                strategy_instance_id=target.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="not-reviewed",
                venue="deepcoin",
                status="pending",
                request_json=leg.request_json,
            )
        )
        session.commit()

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "reviewed_local_state_changed"


def test_manual_reconciliation_allows_terminal_unreviewed_entry_sibling(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        build_manual_pending_entry_reconciliation_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=target.execution_binding_id,
                strategy_instance_id=target.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="already-terminal",
                venue="deepcoin",
                status="cancelled",
                request_json=leg.request_json,
            )
        )
        session.commit()

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert plan.status == "ready"


def test_manual_reconciliation_apply_rechecks_unreviewed_active_sibling(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = reconciliation.build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    def add_sibling(*args):
        with session_factory() as session:
            leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=target.execution_binding_id,
                    strategy_instance_id=target.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="late-unreviewed",
                    venue="deepcoin",
                    status="pending",
                    request_json=leg.request_json,
                )
            )
            session.commit()

    monkeypatch.setattr(reconciliation, "_create_verified_backup", add_sibling)

    with pytest.raises(ValueError, match="reviewed_local_state_changed"):
        reconciliation.apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=database_path,
            backup_path=tmp_path / "backup.db",
            deepcoin_client=client,
            targets=(target,),
            runtime_guard=RuntimeGuard(),
            expected_fingerprint=plan.fingerprint,
            now=NOW,
        )

    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"
        assert session.get(ExecutionBinding, target.execution_binding_id).status == "open"
        assert session.get(StrategyLifecycle, target.lifecycle_id).lifecycle_status == "pending_entry"


def test_manual_reconciliation_apply_rechecks_freshness_after_backup(tmp_path, monkeypatch):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    client.history = [
        {"instId": target.instrument_id, "ordId": target.order_id, "state": "cancelled"}
    ]
    moments = iter(
        (
            NOW,
            NOW,
            NOW,
            NOW,
            NOW,
            NOW + timedelta(seconds=31),
        )
    )
    clock = lambda: next(moments)
    plan = reconciliation.build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        clock=clock,
    )
    monkeypatch.setattr(reconciliation, "_create_verified_backup", lambda *_: None)

    with pytest.raises(ValueError, match="exchange_snapshot_stale_at_write_boundary"):
        reconciliation.apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=database_path,
            backup_path=tmp_path / "backup.db",
            deepcoin_client=client,
            targets=(target,),
            runtime_guard=RuntimeGuard(),
            expected_fingerprint=plan.fingerprint,
            clock=clock,
        )

    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"


def test_manual_reconciliation_rerun_is_read_only_completed(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=tmp_path / "first.db",
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        expected_fingerprint=plan.fingerprint,
        now=NOW,
    )

    repeated = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert repeated.status == "completed"
    assert repeated.reason_code is None


@pytest.mark.parametrize(
    "drift",
    (
        "event_binding",
        "intent_binding",
        "protection_binding",
        "convergence_binding",
        "request_trigger_price",
        "leg_reason",
        "intent_reason",
        "binding_exchange",
        "lifecycle_action",
        "convergence_reason",
        "event_after",
    ),
)
def test_completed_reconciliation_still_requires_canonical_ownership(
    tmp_path,
    drift,
):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=tmp_path / "first.db",
        deepcoin_client=client,
        targets=(target,),
        expected_fingerprint=plan.fingerprint,
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    with session_factory() as session:
        foreign_binding, _ = _seed_foreign_binding_and_leg(session)
        if drift == "event_binding":
            session.query(ExecutionEvent).one().execution_binding_id = foreign_binding.id
        elif drift == "intent_binding":
            session.query(TriggerProtectionIntent).one().execution_binding_id = foreign_binding.id
        elif drift == "protection_binding":
            session.query(PositionProtectionLeg).first().execution_binding_id = foreign_binding.id
        elif drift == "convergence_binding":
            session.query(TriggerTakeProfitConvergence).one().execution_binding_id = foreign_binding.id
        elif drift == "request_trigger_price":
            leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
            request = json.loads(leg.request_json)
            request["triggerPrice"] = "different"
            leg.request_json = json.dumps(request, sort_keys=True)
        elif drift == "leg_reason":
            session.get(
                ExecutionOrderLeg,
                target.execution_order_leg_id,
            ).terminal_reason = "different"
        elif drift == "intent_reason":
            session.query(TriggerProtectionIntent).one().last_reason_code = "different"
        elif drift == "binding_exchange":
            session.get(
                ExecutionBinding,
                target.execution_binding_id,
            ).last_exchange_status = "different"
        elif drift == "lifecycle_action":
            session.get(
                StrategyLifecycle,
                target.lifecycle_id,
            ).management_action = "different"
        elif drift == "convergence_reason":
            session.query(TriggerTakeProfitConvergence).one().reason_code = "different"
        elif drift == "event_after":
            session.query(ExecutionEvent).one().after_json = json.dumps(
                {"pending": True, "terminalized": False}
            )
        session.commit()

    repeated = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    assert repeated.status == "blocked"


def test_verified_backup_refuses_dangling_symlink(tmp_path):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    sqlite3.connect(source).close()
    backup = tmp_path / "backup.db"
    backup.symlink_to(tmp_path / "missing.db")

    with pytest.raises(ValueError, match="backup_path_invalid"):
        reconciliation._create_verified_backup(source, backup)


def test_verified_backup_refuses_unsafe_parent_mode(tmp_path):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    sqlite3.connect(source).close()
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)

    with pytest.raises(ValueError, match="backup_parent_unsafe"):
        reconciliation._create_verified_backup(source, parent / "backup.db")


def test_verified_backup_is_created_with_exact_0600_mode(tmp_path):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    sqlite3.connect(source).close()
    backup = tmp_path / "backup.db"

    reconciliation._create_verified_backup(source, backup)

    assert backup.stat().st_mode & 0o777 == 0o600


def test_verified_backup_writes_the_exclusive_inode_without_path_reopen(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence(value) VALUES ('reviewed')")
    connection.commit()
    connection.close()
    backup = tmp_path / "backup.db"
    real_connect = reconciliation.sqlite3.connect

    def connect(database, *args, **kwargs):
        assert Path(str(database)) != backup
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reconciliation.sqlite3, "connect", connect)

    reconciliation._create_verified_backup(source, backup)

    with real_connect(backup) as copied:
        assert copied.execute("SELECT value FROM evidence").fetchone() == (
            "reviewed",
        )


def test_verified_backup_refuses_source_path_aba(tmp_path, monkeypatch):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    original = sqlite3.connect(source)
    original.execute("CREATE TABLE original(value INTEGER)")
    original.commit()
    original.close()
    replacement = tmp_path / "replacement.db"
    other = sqlite3.connect(replacement)
    other.execute("CREATE TABLE replacement(value INTEGER)")
    other.commit()
    other.close()
    held = tmp_path / "held.db"
    real_connect = reconciliation.sqlite3.connect

    def connect(database, *args, **kwargs):
        if str(database).startswith(f"file:{source}"):
            source.rename(held)
            replacement.rename(source)
            opened = real_connect(database, *args, **kwargs)
            source.rename(replacement)
            held.rename(source)
            return opened
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reconciliation.sqlite3, "connect", connect)

    with pytest.raises(ValueError, match="backup_source_invalid"):
        reconciliation._create_verified_backup(source, tmp_path / "backup.db")


def test_verified_backup_includes_uncheckpointed_wal_commit(tmp_path):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
    writer.commit()
    writer.execute("INSERT INTO evidence(value) VALUES ('wal-only')")
    writer.commit()
    backup = tmp_path / "backup.db"

    reconciliation._create_verified_backup(source, backup)

    with sqlite3.connect(backup) as copied:
        assert copied.execute("SELECT value FROM evidence").fetchone() == (
            "wal-only",
        )
    writer.close()


def test_verified_backup_removes_output_when_mode_cannot_be_enforced(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    sqlite3.connect(source).close()
    monkeypatch.setattr(
        reconciliation.os,
        "fchmod",
        lambda *args: (_ for _ in ()).throw(OSError("mode refused")),
    )

    with pytest.raises(ValueError, match="backup_metadata_invalid"):
        reconciliation._create_verified_backup(source, backup)

    assert not backup.exists()


def test_verified_backup_refuses_foreign_key_violations(tmp_path):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    connection.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
        INSERT INTO child(parent_id) VALUES (99);
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="backup_foreign_key_check_failed"):
        reconciliation._create_verified_backup(source, tmp_path / "backup.db")


def test_verified_backup_removes_failed_quick_check_output(tmp_path, monkeypatch):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    sqlite3.connect(source).close()
    real_connect = reconciliation.sqlite3.connect

    class FailedQuickCheckConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA quick_check":
                return SimpleNamespace(fetchone=lambda: ("corrupt",))
            return super().execute(sql, parameters)

    def connect(database, *args, **kwargs):
        if database == ":memory:":
            kwargs["factory"] = FailedQuickCheckConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reconciliation.sqlite3, "connect", connect)

    with pytest.raises(ValueError, match="backup_quick_check_failed"):
        reconciliation._create_verified_backup(source, backup)

    assert not backup.exists()


def test_manual_reconciliation_terminalization_failure_rolls_back_and_keeps_backup(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    database_path = tmp_path / "research.db"
    backup_path = tmp_path / "backup.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = reconciliation.build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    original = reconciliation._terminalize_target

    def fail_after_terminalization(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected terminalization failure")

    monkeypatch.setattr(
        reconciliation,
        "_terminalize_target",
        fail_after_terminalization,
    )

    with pytest.raises(RuntimeError, match="injected terminalization failure"):
        reconciliation.apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=database_path,
            backup_path=backup_path,
            deepcoin_client=client,
            targets=(target,),
            expected_fingerprint=plan.fingerprint,
            runtime_guard=RuntimeGuard(),
            now=NOW,
        )

    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"
        assert session.query(ExecutionEvent).count() == 0
        assert session.query(TradingSetting).filter_by(
            key="entry_revision_exchange_authority"
        ).one_or_none() is None
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == backup_sha256
    with sqlite3.connect(backup_path) as backup_connection:
        assert backup_connection.execute("PRAGMA quick_check").fetchone() == (
            "ok",
        )


def test_manual_reconciliation_terminalizes_all_canonical_targets_once(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    backup_path = tmp_path / "backup.db"
    session_factory = create_session_factory(database_path)
    targets = _seed_all_canonical_targets(session_factory)
    client = ReadOnlyClient()
    for target in targets:
        _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=targets,
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    guard = RuntimeGuard()

    result = apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=backup_path,
        deepcoin_client=client,
        targets=targets,
        expected_fingerprint=plan.fingerprint,
        runtime_guard=guard,
        now=NOW,
    )

    assert result.status == "completed"
    assert result.terminalized_count == 7
    assert result.authority_seeded is True
    assert guard.calls == 3
    with session_factory() as session:
        assert {
            session.get(ExecutionOrderLeg, target.execution_order_leg_id).status
            for target in targets
        } == {"cancelled"}
        assert session.query(ExecutionEvent).filter_by(
            action="reconcile_manual_pending_entry_cancel",
            status="confirmed",
        ).count() == 7
        authority = session.query(TradingSetting).filter_by(
            key="entry_revision_exchange_authority"
        ).one()
        assert json.loads(authority.value_json)["state"] == "idle"
    with sqlite3.connect(backup_path) as backup_connection:
        assert backup_connection.execute("PRAGMA quick_check").fetchone() == (
            "ok",
        )


def test_all_canonical_terminalization_failure_is_atomic(tmp_path, monkeypatch):
    import telegram_kol_research.manual_pending_entry_reconciliation as reconciliation

    database_path = tmp_path / "research.db"
    backup_path = tmp_path / "backup.db"
    session_factory = create_session_factory(database_path)
    targets = _seed_all_canonical_targets(session_factory)
    client = ReadOnlyClient()
    for target in targets:
        _record_cancelled_history(client, target)
    plan = reconciliation.build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=targets,
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )
    original = reconciliation._terminalize_target
    calls = 0

    def fail_on_fourth(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 4:
            raise RuntimeError("injected canonical terminalization failure")

    monkeypatch.setattr(reconciliation, "_terminalize_target", fail_on_fourth)

    with pytest.raises(
        RuntimeError,
        match="injected canonical terminalization failure",
    ):
        reconciliation.apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=database_path,
            backup_path=backup_path,
            deepcoin_client=client,
            targets=targets,
            expected_fingerprint=plan.fingerprint,
            runtime_guard=RuntimeGuard(),
            now=NOW,
        )

    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    with session_factory() as session:
        assert {
            session.get(ExecutionOrderLeg, target.execution_order_leg_id).status
            for target in targets
        } == {"pending"}
        assert session.query(ExecutionEvent).count() == 0
        assert session.query(TradingSetting).filter_by(
            key="entry_revision_exchange_authority"
        ).one_or_none() is None
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == backup_sha256


def test_manual_reconciliation_refuses_database_path_mismatch(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    other_database_path = tmp_path / "other.db"
    session_factory = create_session_factory(database_path)
    create_session_factory(other_database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    _record_cancelled_history(client, target)
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        runtime_guard=RuntimeGuard(),
        now=NOW,
    )

    with pytest.raises(ValueError, match="database_path_mismatch"):
        apply_manual_pending_entry_reconciliation(
            session_factory,
            database_path=other_database_path,
            backup_path=tmp_path / "backup.db",
            deepcoin_client=client,
            targets=(target,),
            runtime_guard=RuntimeGuard(),
            expected_fingerprint=plan.fingerprint,
            now=NOW,
        )

    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"
