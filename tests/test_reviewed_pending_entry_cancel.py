from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLeg,
    RepairConfirmationToken,
    StrategyLifecycle,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _request(order_id: str) -> dict[str, str]:
    return {
        "clOrdId": f"client-{order_id}",
        "instId": "ETH-USDT-SWAP",
        "isCrossMargin": "1",
        "mrgPosition": "split",
        "orderType": "limit",
        "posSide": "long",
        "price": "1827.0",
        "productGroup": "Swap",
        "side": "buy",
        "slOrdPx": "-1",
        "slTriggerPx": "1795.0",
        "slTriggerPxType": "last",
        "sz": "3.0",
        "tdMode": "cross",
        "triggerPrice": "1827.0",
        "triggerPxType": "last",
    }


def _pending_row(order_id: str) -> dict[str, str]:
    return {
        "instId": "ETH-USDT-SWAP",
        "ordId": order_id,
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
    }


class Client:
    def __init__(self, order_id: str = "reviewed-1") -> None:
        self.pending = [_pending_row(order_id)]
        self.history: list[dict[str, str]] = []
        self.positions: list[dict[str, str]] = []
        self.regular: list[dict[str, str]] = []
        self.fills: list[dict[str, str]] = []
        self.cancel_calls = 0
        self.cancel_exception: Exception | None = None
        self.guard: Guard | None = None
        self.reads_under_guard = 0

    def _rows(self, rows, inst_id):
        if self.guard is not None and self.guard.quiescent:
            self.reads_under_guard += 1
        return [row for row in rows if row.get("instId") == inst_id]

    def list_positions(self, *, inst_id):
        return self._rows(self.positions, inst_id)

    def list_open_orders(self, *, inst_id):
        return self._rows(self.regular, inst_id)

    def list_trigger_orders_pending(self, *, inst_id):
        return self._rows(self.pending, inst_id)

    def list_trigger_order_history(self, *, inst_id):
        return self._rows(self.history, inst_id)

    def list_trade_fills(self, *, inst_id):
        return self._rows(self.fills, inst_id)

    def cancel_trigger_order(self, payload):
        self.cancel_calls += 1
        assert self.guard is None or self.guard.quiescent
        if self.cancel_exception is not None:
            raise self.cancel_exception
        order_id = str(payload["ordId"])
        self.pending = [row for row in self.pending if row["ordId"] != order_id]
        self.history.append(
            {
                "instId": str(payload["instId"]),
                "ordId": order_id,
                "state": "cancelled",
                "triggerOrderType": "Conditional",
            }
        )
        return {"code": "0", "data": [{"ordId": order_id, "sCode": "0"}]}


@dataclass(frozen=True, slots=True)
class Receipt:
    fingerprint: str


class Guard:
    def __init__(self) -> None:
        self.quiescent = False
        self.block_reason: str | None = None
        self.restored = False

    def enter(self, *, action_id: str) -> Receipt:
        assert action_id
        self.quiescent = True
        return Receipt(fingerprint="a" * 64)

    def prove_quiescent(self) -> None:
        assert self.quiescent

    def mark_safe_to_restore(self, *, expected_fingerprint: str) -> Receipt:
        assert expected_fingerprint == "a" * 64
        assert self.block_reason is None
        return Receipt(fingerprint="b" * 64)

    def restore(self, *, expected_fingerprint: str) -> None:
        assert expected_fingerprint == "b" * 64
        self.quiescent = False
        self.restored = True

    def block(self, *, reason_code: str) -> Receipt:
        self.block_reason = reason_code
        return Receipt(fingerprint="c" * 64)


def _seed(session_factory, *, order_id: str = "reviewed-1"):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        ReviewedPendingEntryTarget,
    )

    request = _request(order_id)
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
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
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
                TriggerTakeProfitConvergence(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    desired_take_profits_json=(
                        '[{"price":"1860","allocation_pct":100}]'
                    ),
                    status="waiting_backup_stop",
                    reason_code="convergence_waiting_backup_stop",
                ),
            ]
        )
        session.commit()
        target = ReviewedPendingEntryTarget(
            order_id=order_id,
            instrument_id="ETH-USDT-SWAP",
            lifecycle_id=int(lifecycle.id),
            execution_binding_id=int(binding.id),
            execution_order_leg_id=int(leg.id),
            trigger_price="1827",
            size="3",
            embedded_stop_price="1795",
            request_fingerprint=_fingerprint(request),
        )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": False, "entry_revision_v2_mode": "disabled"},
        updated_at=NOW,
    )
    seeded = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
        initial_generation=7,
    )
    assert seeded.seeded
    return target


def _plan(session_factory, client, target):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    return build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=(target,),
        order_id=target.order_id,
        now=NOW,
    )


def _apply(
    session_factory,
    client,
    target,
    plan,
    guard,
    *,
    token="fresh-token",
    authorization_expires_at=NOW + timedelta(minutes=10),
    clock=None,
    apply_now=NOW,
):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        apply_reviewed_pending_entry_cancel_plan,
    )

    action = plan.actions[0]
    return apply_reviewed_pending_entry_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=(target,),
        order_id=target.order_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        expected_evidence_fingerprint=plan.evidence_fingerprint,
        confirmation_token=token,
        guard=guard,
        authorization_expires_at=authorization_expires_at,
        clock=clock,
        now=apply_now,
    )


def _authority_document(session_factory):
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one()
        )
        return json.loads(row.value_json)


def test_canonical_targets_are_the_only_order_source_and_bridge_is_absent():
    import telegram_kol_research.reviewed_pending_entry_cancel as module

    assert len(module.REVIEWED_PENDING_ENTRY_TARGETS) == 7
    assert len({row.order_id for row in module.REVIEWED_PENDING_ENTRY_TARGETS}) == 7
    assert "legacy_runtime_drain_bridge" not in inspect.getsource(module)


def test_rejected_bridge_and_internal_freeze_key_are_absent_from_production():
    root = Path(__file__).resolve().parents[1]
    production = []
    for directory in ("src", "scripts", "deploy"):
        production.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".sh", ".ps1", ".service"}
        )

    sources = "\n".join(path.read_text(encoding="utf-8") for path in production)
    assert "legacy_runtime_drain_bridge" not in sources
    assert "legacy_entry_submission_frozen" not in sources


def test_plan_is_exactly_one_canonical_order_with_fresh_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "plan.db")
    target = _seed(session_factory)
    client = Client()

    plan = _plan(session_factory, client, target)

    assert plan.conflicts == ()
    assert [row.order_id for row in plan.actions] == [target.order_id]
    assert plan.expected_generation == 7
    assert len(plan.evidence_fingerprint) == 64
    assert plan.pending_fingerprints[0][0] == target.order_id


def test_noncanonical_or_extra_pending_order_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "extra.db")
    target = _seed(session_factory)
    client = Client()
    client.pending.append(_pending_row("not-reviewed"))

    plan = _plan(session_factory, client, target)

    assert plan.actions == ()
    assert {row["reason"] for row in plan.conflicts} == {
        "unreviewed_pending_trigger"
    }


def test_missing_or_active_global_authority_blocks_planning(tmp_path):
    session_factory = create_session_factory(tmp_path / "authority.db")
    target = _seed(session_factory)
    client = Client()
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one()
        )
        session.delete(row)
        session.commit()

    plan = _plan(session_factory, client, target)

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_apply_replans_after_quiescence_and_holds_authority_through_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "success.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard

    result = _apply(session_factory, client, target, plan, guard)

    assert result.status == "cancelled"
    assert client.cancel_calls == 1
    assert client.reads_under_guard > 0
    assert guard.restored is True
    assert _authority_document(session_factory)["state"] == "idle"


def test_cancel_timeout_is_permanent_unknown_and_never_restores(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard
    client.cancel_exception = TimeoutError("response lost")

    result = _apply(session_factory, client, target, plan, guard)

    assert result.status == "cancel_outcome_unknown"
    assert client.cancel_calls == 1
    assert guard.restored is False
    assert guard.block_reason == "cancel_outcome_unknown"
    assert _authority_document(session_factory)["state"] == "blocked"
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "recovery_required"


def test_prewrite_refusal_cas_failure_never_releases_inner_authority(
    tmp_path, monkeypatch
):
    import telegram_kol_research.reviewed_pending_entry_cancel as module

    session_factory = create_session_factory(tmp_path / "cas.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard
    monkeypatch.setattr(module, "_single_pending_cancel_write_gate", lambda *a, **k: False)
    monkeypatch.setattr(
        module,
        "_record_pending_cancel_prewrite_refusal",
        lambda *a, **k: False,
    )

    result = _apply(session_factory, client, target, plan, guard)

    assert result.status == "blocked"
    assert result.reason_code == "prewrite_refusal_persistence_unknown"
    assert client.cancel_calls == 0
    assert guard.restored is False
    assert _authority_document(session_factory)["state"] == "blocked"


def test_authorization_expiring_immediately_before_transport_refuses_zero_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "expired.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard
    base = datetime.now(UTC)
    times = iter(
        (
            base,
            base,
            base,
            base,
            base + timedelta(seconds=6),
            base + timedelta(seconds=6),
        )
    )

    result = _apply(
        session_factory,
        client,
        target,
        plan,
        guard,
        authorization_expires_at=base + timedelta(seconds=5),
        clock=lambda: next(times),
        apply_now=None,
    )

    assert result.status == "blocked"
    assert result.reason_code == "maintenance_authorization_expired"
    assert client.cancel_calls == 0
    assert guard.restored is True
    assert _authority_document(session_factory)["state"] == "idle"
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "prewrite_refused"


def test_expired_authority_lease_blocks_before_transport(tmp_path):
    session_factory = create_session_factory(tmp_path / "lease-expired.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard
    base = datetime.now(UTC)
    times = iter(
        (
            base,
            base,
            base,
            base + timedelta(minutes=11),
        )
    )

    result = _apply(
        session_factory,
        client,
        target,
        plan,
        guard,
        authorization_expires_at=base + timedelta(minutes=15),
        clock=lambda: next(times),
        apply_now=None,
    )

    assert result.status == "blocked"
    assert result.reason_code == (
        "entry_revision_exchange_authority_expired_blocked"
    )
    assert client.cancel_calls == 0
    assert guard.restored is False
    assert guard.block_reason == (
        "entry_revision_exchange_authority_expired_blocked"
    )
    assert _authority_document(session_factory)["state"] == "blocked"


def test_stale_under_authority_evidence_refuses_before_transport(tmp_path):
    session_factory = create_session_factory(tmp_path / "evidence-stale.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard
    base = datetime.now(UTC)
    times = iter(
        (
            base,
            base,
            base,
            base,
            base + timedelta(seconds=31),
            base + timedelta(seconds=31),
        )
    )

    result = _apply(
        session_factory,
        client,
        target,
        plan,
        guard,
        authorization_expires_at=base + timedelta(minutes=15),
        clock=lambda: next(times),
        apply_now=None,
    )

    assert result.status == "blocked"
    assert result.reason_code == "maintenance_evidence_stale"
    assert client.cancel_calls == 0
    assert guard.restored is True
    assert _authority_document(session_factory)["state"] == "idle"
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "prewrite_refused"
        assert json.loads(intent.error_json) == {
            "reason": "maintenance_evidence_stale"
        }


def test_confirmed_cancel_terminalizes_every_local_surface_and_token(tmp_path):
    session_factory = create_session_factory(tmp_path / "terminal.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard

    assert _apply(session_factory, client, target, plan, guard).status == "cancelled"

    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "cancelled"
        assert session.get(ExecutionBinding, target.execution_binding_id).status == "cancelled"
        assert session.get(StrategyLifecycle, target.lifecycle_id).lifecycle_status == "expired"
        assert session.query(PositionMutationIntent).one().status == "confirmed"
        assert session.query(TriggerProtectionIntent).one().recovery_state == "resolved"
        assert {row.status for row in session.query(PositionProtectionLeg).all()} == {"cancelled"}
        assert session.query(TriggerTakeProfitConvergence).one().status == "completed"
        event = session.query(ExecutionEvent).one()
        assert event.status == "confirmed"
        assert session.query(RepairConfirmationToken).one().action_kind == "drain_one_pending_entry"


@pytest.mark.parametrize(
    "stage",
    (
        "leg",
        "protection_intent",
        "protection",
        "convergence",
        "intent",
        "binding",
        "lifecycle",
        "event",
    ),
)
def test_terminalization_failure_rolls_back_all_surfaces_and_blocks(
    tmp_path, monkeypatch, stage
):
    import telegram_kol_research.reviewed_pending_entry_cancel as module

    session_factory = create_session_factory(tmp_path / f"rollback-{stage}.db")
    target = _seed(session_factory)
    client = Client()
    plan = _plan(session_factory, client, target)
    guard = Guard()
    client.guard = guard

    def fail_at(current):
        if current == stage:
            raise RuntimeError(stage)

    monkeypatch.setattr(module, "_terminalization_checkpoint", fail_at)
    result = _apply(session_factory, client, target, plan, guard)

    assert result.status == "cancelled_audit_state_changed"
    assert guard.restored is False
    assert _authority_document(session_factory)["state"] == "blocked"
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, target.execution_order_leg_id).status == "pending"
        assert session.get(ExecutionBinding, target.execution_binding_id).status == "open"
        assert session.get(StrategyLifecycle, target.lifecycle_id).lifecycle_status == "pending_entry"
        assert {row.status for row in session.query(PositionProtectionLeg).all()} == {"planned"}
        assert session.query(TriggerProtectionIntent).one().recovery_state == "pending"
        assert session.query(TriggerTakeProfitConvergence).one().status == "waiting_backup_stop"
        assert session.query(PositionMutationIntent).one().status == "recovery_required"
        assert session.query(ExecutionEvent).filter(ExecutionEvent.status == "confirmed").count() == 0
