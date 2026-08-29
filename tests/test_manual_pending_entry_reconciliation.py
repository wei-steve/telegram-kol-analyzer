from datetime import UTC, datetime
import hashlib
import json

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


def _seed_pending_target(session_factory):
    from telegram_kol_research.reviewed_pending_entry_targets import (
        ReviewedPendingEntryTarget,
    )

    order_id = "manual-cancel-1"
    request = {
        "instId": "ETH-USDT-SWAP",
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
            trigger_price="1827",
            size="3",
            embedded_stop_price="1795",
            request_fingerprint=request_fingerprint,
        )


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

    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        now=NOW,
    )
    assert plan.status == "ready"

    result = apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=backup_path,
        deepcoin_client=client,
        targets=(target,),
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
        now=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "live_position_present"


def test_manual_reconciliation_rerun_is_read_only_completed(tmp_path):
    from telegram_kol_research.manual_pending_entry_reconciliation import (
        apply_manual_pending_entry_reconciliation,
        build_manual_pending_entry_reconciliation_plan,
    )

    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    target = _seed_pending_target(session_factory)
    client = ReadOnlyClient()
    plan = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        now=NOW,
    )
    apply_manual_pending_entry_reconciliation(
        session_factory,
        database_path=database_path,
        backup_path=tmp_path / "first.db",
        deepcoin_client=client,
        targets=(target,),
        expected_fingerprint=plan.fingerprint,
        now=NOW,
    )

    repeated = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        now=NOW,
    )

    assert repeated.status == "completed"
    assert repeated.reason_code is None
