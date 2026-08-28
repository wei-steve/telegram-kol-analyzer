from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryRevisionReplacement,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageProcessingJob,
    PositionMutationIntent,
    PositionProtectionLeg,
    RepairConfirmationToken,
    RawMessage,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.trading_settings import save_trading_settings


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
        self.after_cancel_callback = None

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
        if self.after_cancel_callback is not None:
            self.after_cancel_callback()
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
            PositionMutationIntent,
            RepairConfirmationToken,
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
                PositionMutationIntent,
                RepairConfirmationToken,
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


def test_plan_blocks_all_actions_when_exchange_write_authority_is_active(
    tmp_path,
):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    with session_factory() as session:
        session.get(ExecutionOrderLeg, leg_ids[0]).status = "submitting"
        session.commit()
    client = _Client()

    plan = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(binding_id, lifecycle_id, leg_ids),
        now=NOW,
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_plan_blocks_claimed_non_shadow_message_job_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    with session_factory() as session:
        raw = RawMessage(chat_id=999, message_id=888, text="new signal")
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=999,
                status="claimed",
                claim_token=None,
                claimed_at=None,
                shadow=False,
                enqueued_at=NOW,
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_plan_blocks_cancel_submitting_mutation_authority(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    with session_factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="other-cancel-submitting",
                venue="deepcoin",
                operation="cancel_other_order",
                strategy_instance_id="other-strategy",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_ids[0],
                pos_id="other-position",
                order_id="other-order",
                authority_fingerprint="a" * 64,
                request_fingerprint="b" * 64,
                status="cancel_submitting",
                request_json="{}",
                reserved_at=NOW,
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def _seed_revision_batch(
    session_factory,
    *,
    binding_id: int,
    lifecycle_id: int,
    status: str,
    claim_token: str | None = None,
    claim_timestamp_present: bool = False,
) -> int:
    with session_factory() as session:
        raw = RawMessage(chat_id=303, message_id=404, text="old revision")
        thread = StrategyThread(
            chat_id=303,
            root_message_id=404,
            symbol="BTC",
            side="long",
            status="active",
        )
        session.add_all([raw, thread])
        session.flush()
        session.add(
            StrategyRevisionBatch(
                idempotency_fingerprint=_fingerprint(
                    {"status": status, "raw_message_id": raw.id}
                ),
                raw_message_id=raw.id,
                strategy_thread_id=thread.id,
                target_lifecycle_id=lifecycle_id,
                execution_binding_id=binding_id,
                revision_kind="replacement",
                status=status,
                replacement_json="{}",
                reason_code=(
                    "revision_cancel_outcome_unknown"
                    if status == "recovery_required"
                    else None
                ),
                advance_claim_token=claim_token,
                advance_claimed_at=(
                    NOW if claim_timestamp_present else None
                ),
                planned_at=NOW,
            )
        )
        session.commit()
        return int(
            session.query(StrategyRevisionBatch.id)
            .filter_by(raw_message_id=raw.id)
            .one()[0]
        )


def _seed_unrelated_revision_owner(session_factory):
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:historical-revision",
            chat_id=303,
            message_id=404,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="open",
            strategy_instance_id="deepcoin:303:404:BTC:long",
            margin_mode="cross",
            position_mode="split",
            last_exchange_status="entry_order_pending",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=303,
            message_id=404,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            entry_range_low=61000,
            entry_range_high=62000,
            stop_loss=60000,
            take_profit="63000",
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
            order_id="historical-unrelated-order",
            client_order_id="historical-unrelated-client",
            venue="deepcoin",
            attribution_status="unassigned",
            status="pending",
            request_json="{}",
        )
        session.add(leg)
        session.commit()
        return int(binding.id), int(lifecycle.id), int(leg.id)


def test_exact_legacy_bridge_sentinel_is_the_only_revision_claim_exemption(
    tmp_path,
):
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeIdentity,
        build_legacy_runtime_drain_bridge_plan,
        fence_legacy_runtime_revisions,
        freeze_legacy_runtime_drain_bridge,
    )
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        REVIEWED_PENDING_ENTRY_TARGETS,
        _active_exchange_authority_present,
    )

    session_factory = create_session_factory(tmp_path / "bridge-gate.db")
    binding_id, lifecycle_id, _leg_ids = _seed(session_factory)
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="planned",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "entry_revision_v2_mode": "live",
            "message_pipeline_mode": "queue",
        },
        updated_at=NOW,
    )
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=51,
        worker_start_ticks=73,
    )
    reviewed_ids = tuple(
        target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS
    )
    bridge_plan = build_legacy_runtime_drain_bridge_plan(
        session_factory,
        runtime_identity=identity,
        expected_production_sha=identity.production_sha,
        reviewed_order_ids=reviewed_ids,
        planned_at=NOW,
    )
    frozen = freeze_legacy_runtime_drain_bridge(
        session_factory,
        plan=bridge_plan,
        runtime_identity=identity,
        reviewed_order_ids=reviewed_ids,
        expected_fingerprint=bridge_plan.fingerprint,
        confirmation_token="reviewed-bridge-freeze-token",
        frozen_at=NOW,
    )
    fenced = fence_legacy_runtime_revisions(
        session_factory,
        bridge_token=str(frozen.bridge_token),
        runtime_identity=identity,
        confirmation_token="reviewed-bridge-fence-token",
        fenced_at=NOW,
    )
    assert fenced.status == "fenced"

    with session_factory() as session:
        assert not _active_exchange_authority_present(
            session,
            targets=REVIEWED_PENDING_ENTRY_TARGETS,
            legacy_runtime_identity=identity,
        )
        batch = session.get(StrategyRevisionBatch, batch_id)
        batch.advance_claimed_at = NOW
        session.commit()
    with session_factory() as session:
        assert _active_exchange_authority_present(
            session,
            targets=REVIEWED_PENDING_ENTRY_TARGETS,
            legacy_runtime_identity=identity,
        )


def test_plan_allows_recovery_required_revision_batch_without_claim(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="recovery_required",
    )

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert len(plan.actions) == 2
    assert plan.conflicts == ()


def test_plan_blocks_submitting_replacements_revision_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="submitting_replacements",
    )

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


@pytest.mark.parametrize(
    ("claim_token", "claim_timestamp_present"),
    (("revision-claim", False), (None, True), ("revision-claim", True)),
)
def test_plan_blocks_revision_batch_with_claim_evidence(
    tmp_path,
    claim_token,
    claim_timestamp_present,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="recovery_required",
        claim_token=claim_token,
        claim_timestamp_present=claim_timestamp_present,
    )

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


@pytest.mark.parametrize("child_status", ("cancel_submitting", "submit_unknown"))
def test_plan_blocks_recovery_required_revision_with_ambiguous_cancel_child(
    tmp_path,
    child_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="recovery_required",
    )
    with session_factory() as session:
        session.add(
            StrategyRevisionLeg(
                revision_batch_id=batch_id,
                execution_order_leg_id=leg_ids[0],
                action="cancel_pending",
                prior_status="pending",
                status=child_status,
                order_id="reviewed-1",
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


@pytest.mark.parametrize("child_status", ("submit_reserved", "submitted"))
def test_plan_blocks_recovery_required_revision_with_ambiguous_replacement(
    tmp_path,
    child_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=binding_id,
        lifecycle_id=lifecycle_id,
        status="recovery_required",
    )
    with session_factory() as session:
        session.add(
            EntryRevisionReplacement(
                revision_batch_id=batch_id,
                execution_order_leg_id=leg_ids[0],
                leg_index=0,
                desired_json="{}",
                status=child_status,
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


@pytest.mark.parametrize("child_status", ("cancel_submitting", "submit_unknown"))
def test_plan_allows_unrelated_terminal_ambiguous_cancel_child(
    tmp_path,
    child_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    old_binding_id, old_lifecycle_id, old_leg_id = (
        _seed_unrelated_revision_owner(session_factory)
    )
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=old_binding_id,
        lifecycle_id=old_lifecycle_id,
        status="recovery_required",
    )
    with session_factory() as session:
        session.add(
            StrategyRevisionLeg(
                revision_batch_id=batch_id,
                execution_order_leg_id=old_leg_id,
                action="cancel_pending",
                prior_status="pending",
                status=child_status,
                order_id="historical-unrelated-order",
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert len(plan.actions) == 2
    assert plan.conflicts == ()


@pytest.mark.parametrize("child_status", ("submit_reserved", "submitted"))
def test_plan_allows_unrelated_terminal_ambiguous_replacement(
    tmp_path,
    child_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    old_binding_id, old_lifecycle_id, old_leg_id = (
        _seed_unrelated_revision_owner(session_factory)
    )
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=old_binding_id,
        lifecycle_id=old_lifecycle_id,
        status="recovery_required",
    )
    with session_factory() as session:
        session.add(
            EntryRevisionReplacement(
                revision_batch_id=batch_id,
                execution_order_leg_id=old_leg_id,
                leg_index=0,
                desired_json="{}",
                status=child_status,
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert len(plan.actions) == 2
    assert plan.conflicts == ()


def test_plan_blocks_terminal_ambiguous_replacement_with_reviewed_order_only(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    old_binding_id, old_lifecycle_id, old_leg_id = (
        _seed_unrelated_revision_owner(session_factory)
    )
    batch_id = _seed_revision_batch(
        session_factory,
        binding_id=old_binding_id,
        lifecycle_id=old_lifecycle_id,
        status="recovery_required",
    )
    with session_factory() as session:
        session.add(
            EntryRevisionReplacement(
                revision_batch_id=batch_id,
                execution_order_leg_id=old_leg_id,
                leg_index=0,
                desired_json="{}",
                status="submitted",
                order_id="reviewed-1",
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_plan_blocks_orphan_ambiguous_revision_child(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    with session_factory() as session:
        session.add(
            StrategyRevisionLeg(
                revision_batch_id=999_999,
                execution_order_leg_id=leg_ids[0],
                action="cancel_pending",
                prior_status="pending",
                status="submit_unknown",
                order_id="reviewed-1",
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_plan_blocks_orphan_ambiguous_replacement(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    with session_factory() as session:
        session.add(
            EntryRevisionReplacement(
                revision_batch_id=999_999,
                execution_order_leg_id=leg_ids[0],
                leg_index=0,
                desired_json="{}",
                status="submitted",
                order_id="reviewed-1",
            )
        )
        session.commit()

    plan = _build_plan(
        session_factory,
        _Client(),
        _targets(binding_id, lifecycle_id, leg_ids),
    )

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "active_exchange_authority_present"},
    )


def test_plan_rejects_empty_reviewed_target_set(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    plan = _build_plan(session_factory, _Client(), ())

    assert plan.actions == ()
    assert plan.conflicts == (
        {"order_id": "*", "reason": "empty_reviewed_target_set"},
    )


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


def _build_plan(session_factory, client, targets):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        build_reviewed_pending_entry_cancel_plan,
    )

    return build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=targets,
        now=NOW,
    )


def _apply_one(
    session_factory,
    client,
    targets,
    plan,
    *,
    order_id="reviewed-1",
    confirmation_token="cancel-token-one",
    **apply_kwargs,
):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        apply_reviewed_pending_entry_cancel_plan,
    )

    action = next(item for item in plan.actions if item.order_id == order_id)
    return apply_reviewed_pending_entry_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=targets,
        order_id=order_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token=confirmation_token,
        now=NOW,
        **apply_kwargs,
    )


def _authority_document(session_factory):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )

    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        return json.loads(row.value_json)


@pytest.mark.parametrize(
    ("settings", "reason_code"),
    (
        (
            {"auto_trade_enabled": True, "entry_revision_v2_mode": "disabled"},
            "pending_entry_cancel_auto_trade_not_frozen",
        ),
        (
            {"auto_trade_enabled": False, "entry_revision_v2_mode": "shadow"},
            "pending_entry_cancel_revision_not_disabled",
        ),
        (
            {"auto_trade_enabled": False, "entry_revision_v2_mode": "live"},
            "pending_entry_cancel_revision_not_disabled",
        ),
    ),
)
def test_apply_quiescence_requires_frozen_settings(
    tmp_path,
    settings,
    reason_code,
):
    from telegram_kol_research.trading_settings import save_trading_settings

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    save_trading_settings(session_factory, settings, updated_at=NOW)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "blocked"
    assert result.reason_code == reason_code
    assert client.cancel_payloads == []
    with session_factory() as session:
        assert session.query(RepairConfirmationToken).count() == 0
        assert session.query(PositionMutationIntent).count() == 0


def test_worker_exchange_authority_blocks_cancel_before_confirmation(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        acquire_entry_revision_exchange_authority,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    worker = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:91",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )
    assert worker.acquired is True

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "blocked"
    assert result.reason_code == "entry_revision_exchange_authority_busy"
    assert client.cancel_payloads == []
    assert _authority_document(session_factory)["token"] == worker.token
    with session_factory() as session:
        assert session.query(RepairConfirmationToken).count() == 0
        assert session.query(PositionMutationIntent).count() == 0


def test_cancel_holds_authority_during_exchange_and_releases_on_success(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        acquire_entry_revision_exchange_authority,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)

    class AuthorityInspectingClient(_Client):
        worker_attempt = None

        def cancel_trigger_order(self, payload):
            document = _authority_document(session_factory)
            assert document["state"] == "held"
            assert document["owner_kind"] == "reviewed_pending_entry_cancel"
            assert document["owner_id"] == "order:reviewed-1"
            self.worker_attempt = acquire_entry_revision_exchange_authority(
                session_factory,
                owner_kind="entry_revision_worker",
                owner_id="batch:92",
                acquired_at=NOW,
                require_cancel_quiescence=False,
            )
            return super().cancel_trigger_order(payload)

    client = AuthorityInspectingClient()
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancelled"
    assert client.worker_attempt.acquired is False
    assert (
        client.worker_attempt.reason_code
        == "entry_revision_exchange_authority_busy"
    )
    assert _authority_document(session_factory)["state"] == "idle"


def test_cancel_unknown_retains_authority_and_blocks_new_writer(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        acquire_entry_revision_exchange_authority,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.cancel_exception = RuntimeError("unknown")
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancel_outcome_unknown"
    document = _authority_document(session_factory)
    assert document["state"] == "held"
    assert document["owner_kind"] == "reviewed_pending_entry_cancel"
    worker = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:93",
        acquired_at=NOW,
        require_cancel_quiescence=False,
    )
    assert worker.acquired is False
    assert worker.reason_code == "entry_revision_exchange_authority_busy"


@pytest.mark.parametrize(
    "failure_mode",
    ("unconfirmed_response", "changed_readback", "terminalization_drift"),
)
def test_post_write_incomplete_outcome_retains_exchange_authority(
    tmp_path,
    failure_mode,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    if failure_mode == "unconfirmed_response":
        client.cancel_response = {
            "code": "0",
            "data": [{"ordId": "different-order", "sCode": "0"}],
        }
    elif failure_mode == "changed_readback":
        client.mutate_sibling_after_cancel = True
    else:
        def mutate_local_identity():
            with session_factory() as session:
                session.get(ExecutionOrderLeg, leg_ids[0]).purpose = "exit"
                session.commit()

        client.after_cancel_callback = mutate_local_identity
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status != "cancelled"
    document = _authority_document(session_factory)
    assert document["state"] == "held"
    assert document["owner_kind"] == "reviewed_pending_entry_cancel"


def test_under_authority_plan_drift_releases_before_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)

    class ThirdPlanDriftClient(_Client):
        eth_pending_reads = 0

        def list_trigger_orders_pending(self, *, inst_id):
            if inst_id == "ETH-USDT-SWAP":
                self.eth_pending_reads += 1
                if self.eth_pending_reads == 3:
                    self.pending[0]["sz"] = "99"
            return super().list_trigger_orders_pending(inst_id=inst_id)

    client = ThirdPlanDriftClient()
    plan = _build_plan(session_factory, client, targets)

    with pytest.raises(ValueError, match="plan fingerprint changed"):
        _apply_one(session_factory, client, targets, plan)

    assert client.cancel_payloads == []
    assert _authority_document(session_factory)["state"] == "idle"


@pytest.mark.parametrize("failure_point", ("write_gate", "intent_transition"))
def test_prewrite_refusal_releases_exchange_authority(
    tmp_path,
    monkeypatch,
    failure_point,
):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    if failure_point == "write_gate":
        monkeypatch.setattr(
            cancel_module,
            "_single_pending_cancel_write_gate",
            lambda *_args, **_kwargs: False,
        )
    else:
        original_transition = cancel_module.transition_position_mutation_intent

        def fail_submitting_transition(*args, **kwargs):
            if kwargs.get("new_status") == "submitting":
                return False
            return original_transition(*args, **kwargs)

        monkeypatch.setattr(
            cancel_module,
            "transition_position_mutation_intent",
            fail_submitting_transition,
        )

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status in {"blocked", "intent_changed"}
    assert client.cancel_payloads == []
    assert _authority_document(session_factory)["state"] == "idle"


@pytest.mark.parametrize(
    "failure_point",
    ("under_authority_plan", "intent_reserve", "token_consume"),
)
def test_unhandled_prewrite_exception_retains_exchange_authority(
    tmp_path,
    monkeypatch,
    failure_point,
):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    if failure_point == "under_authority_plan":
        original_builder = cancel_module.build_reviewed_pending_entry_cancel_plan
        calls = 0

        def fail_third_plan(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("unhandled under-authority plan failure")
            return original_builder(*args, **kwargs)

        monkeypatch.setattr(
            cancel_module,
            "build_reviewed_pending_entry_cancel_plan",
            fail_third_plan,
        )
    elif failure_point == "intent_reserve":
        monkeypatch.setattr(
            cancel_module,
            "reserve_position_mutation_intent",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("unhandled intent reserve failure")
            ),
        )
    else:
        monkeypatch.setattr(
            cancel_module,
            "consume_repair_confirmation_token",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("unhandled token consume failure")
            ),
        )

    with pytest.raises(RuntimeError, match="unhandled"):
        _apply_one(session_factory, client, targets, plan)

    assert client.cancel_payloads == []
    authority = _authority_document(session_factory)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "reviewed_pending_entry_cancel"


def test_apply_cancels_exactly_one_and_terminalizes_only_selected_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancelled"
    assert result.order_id == "reviewed-1"
    assert client.cancel_payloads == [
        {"instId": "ETH-USDT-SWAP", "ordId": "reviewed-1"}
    ]
    assert {row["ordId"] for row in client.pending} == {"reviewed-2"}

    with session_factory() as session:
        selected = session.get(ExecutionOrderLeg, leg_ids[0])
        sibling = session.get(ExecutionOrderLeg, leg_ids[1])
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        intent = (
            session.query(TriggerProtectionIntent)
            .filter_by(execution_order_leg_id=leg_ids[0])
            .one()
        )
        protection = (
            session.query(PositionProtectionLeg)
            .filter_by(execution_order_leg_id=leg_ids[0])
            .all()
        )
        convergence = (
            session.query(TriggerTakeProfitConvergence)
            .filter_by(execution_order_leg_id=leg_ids[0])
            .one()
        )
        event = (
            session.query(ExecutionEvent)
            .filter_by(
                action="cancel_reviewed_pending_entry",
                order_id="reviewed-1",
            )
            .one()
        )

        assert selected.status == "cancelled"
        assert selected.terminal_reason == "operator_cancelled_unfilled_entry_leg"
        assert sibling.status == "pending"
        assert intent.recovery_state == "resolved"
        assert intent.recovery_disposition == "terminal"
        assert intent.last_reason_code == "parent_trigger_cancelled_before_entry"
        assert intent.next_attempt_at is None
        assert {row.status for row in protection} == {"cancelled"}
        assert convergence.status == "completed"
        assert convergence.reason_code == "parent_trigger_cancelled_before_entry"
        assert convergence.completed_at == NOW.replace(tzinfo=None)
        assert binding.status == "open"
        assert lifecycle.lifecycle_status == "pending_entry"
        assert event.status == "confirmed"
        mutation_intent = (
            session.query(PositionMutationIntent)
            .filter_by(
                operation="cancel_reviewed_pending_entry",
                order_id="reviewed-1",
            )
            .one()
        )
        assert mutation_intent.status == "confirmed"
        assert mutation_intent.confirmed_at == NOW.replace(tzinfo=None)
        assert json.loads(event.request_json) == {
            "instId": "ETH-USDT-SWAP",
            "ordId": "reviewed-1",
        }
        assert json.loads(event.response_json) == {
            "code": "0",
            "order_id": "reviewed-1",
        }

    post_plan = _build_plan(session_factory, client, targets)
    assert post_plan.completed_order_ids == ("reviewed-1",)
    assert [action.order_id for action in post_plan.actions] == ["reviewed-2"]


def test_bridge_hooks_wrap_the_exact_single_order_exchange_boundary(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeDrainBridgeResult,
        LegacyRuntimeIdentity,
    )

    session_factory = create_session_factory(tmp_path / "bridge-apply.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    events = []
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=55,
        worker_start_ticks=89,
    )
    monkeypatch.setattr(
        cancel_module,
        "validate_legacy_runtime_bridge_cancellation_ready",
        lambda *_args, **_kwargs: LegacyRuntimeDrainBridgeResult(
            status="ready"
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "begin_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: (
            events.append(("begin", _kwargs["order_id"]))
            or LegacyRuntimeDrainBridgeResult(status="cancelling")
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "complete_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: (
            events.append(("complete", _kwargs["order_id"]))
            or LegacyRuntimeDrainBridgeResult(status="fenced")
        ),
    )
    client.after_cancel_callback = lambda: events.append(
        ("exchange", "reviewed-1")
    )

    result = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        legacy_bridge_token="bridge-token",
        legacy_runtime_identity=identity,
        legacy_runtime_identity_reader=lambda: identity,
    )

    assert result.status == "cancelled"
    assert events == [
        ("begin", "reviewed-1"),
        ("exchange", "reviewed-1"),
        ("complete", "reviewed-1"),
    ]


def test_bridge_unknown_hook_precedes_any_future_retry(tmp_path, monkeypatch):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeDrainBridgeResult,
        LegacyRuntimeIdentity,
    )

    session_factory = create_session_factory(tmp_path / "bridge-unknown-apply.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=55,
        worker_start_ticks=89,
    )
    ready = True
    unknown_orders = []

    def validate(*_args, **_kwargs):
        return LegacyRuntimeDrainBridgeResult(
            status="ready" if ready else "blocked",
            reason_code=None if ready else "legacy_bridge_state_mismatch",
        )

    monkeypatch.setattr(
        cancel_module,
        "validate_legacy_runtime_bridge_cancellation_ready",
        validate,
    )
    monkeypatch.setattr(
        cancel_module,
        "begin_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: LegacyRuntimeDrainBridgeResult(
            status="cancelling"
        ),
    )

    def mark_unknown(*_args, **kwargs):
        nonlocal ready
        ready = False
        unknown_orders.append(kwargs["order_id"])
        return LegacyRuntimeDrainBridgeResult(status="unknown_locked")

    monkeypatch.setattr(
        cancel_module,
        "mark_legacy_runtime_bridge_unknown",
        mark_unknown,
    )
    client.cancel_exception = RuntimeError("transport unknown")

    first = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        legacy_bridge_token="bridge-token",
        legacy_runtime_identity=identity,
        legacy_runtime_identity_reader=lambda: identity,
    )
    cancel_count = len(client.cancel_payloads)
    second = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        confirmation_token="unused-second-token",
        legacy_bridge_token="bridge-token",
        legacy_runtime_identity=identity,
        legacy_runtime_identity_reader=lambda: identity,
    )

    assert first.status == "cancel_outcome_unknown"
    assert unknown_orders == ["reviewed-1"]
    assert second.status == "blocked"
    assert second.reason_code == "legacy_bridge_state_mismatch"
    assert len(client.cancel_payloads) == cancel_count == 1


def test_bridge_apply_rechecks_live_worker_identity_before_exchange_write(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeDrainBridgeResult,
        LegacyRuntimeIdentity,
    )

    session_factory = create_session_factory(tmp_path / "worker-drift.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=55,
        worker_start_ticks=89,
    )
    drifted = LegacyRuntimeIdentity(
        production_sha=identity.production_sha,
        worker_pid=55,
        worker_start_ticks=90,
    )
    identities = iter((identity, drifted))
    monkeypatch.setattr(
        cancel_module,
        "validate_legacy_runtime_bridge_cancellation_ready",
        lambda *_args, **_kwargs: LegacyRuntimeDrainBridgeResult(
            status="ready"
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "begin_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: pytest.fail(
            "drift must block before bridge write boundary"
        ),
    )

    result = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        legacy_bridge_token="bridge-token",
        legacy_runtime_identity=identity,
        legacy_runtime_identity_reader=lambda: next(identities),
    )

    assert result.status == "blocked"
    assert result.reason_code == "legacy_bridge_worker_identity_drift"
    assert client.cancel_payloads == []
    assert _authority_document(session_factory)["state"] == "idle"


def test_bridge_apply_locks_unknown_when_worker_drifts_after_exchange_write(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.reviewed_pending_entry_cancel as cancel_module
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeDrainBridgeResult,
        LegacyRuntimeIdentity,
    )

    session_factory = create_session_factory(tmp_path / "postwrite-drift.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=55,
        worker_start_ticks=89,
    )
    drifted = LegacyRuntimeIdentity(
        production_sha=identity.production_sha,
        worker_pid=55,
        worker_start_ticks=90,
    )
    identities = iter((identity, identity, identity, drifted))
    unknown_reasons = []
    monkeypatch.setattr(
        cancel_module,
        "validate_legacy_runtime_bridge_cancellation_ready",
        lambda *_args, **_kwargs: LegacyRuntimeDrainBridgeResult(
            status="ready"
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "begin_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: LegacyRuntimeDrainBridgeResult(
            status="cancelling"
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "complete_legacy_runtime_bridge_cancellation",
        lambda *_args, **_kwargs: pytest.fail(
            "drift must block bridge completion"
        ),
    )
    monkeypatch.setattr(
        cancel_module,
        "mark_legacy_runtime_bridge_unknown",
        lambda *_args, **kwargs: (
            unknown_reasons.append(kwargs["reason_code"])
            or LegacyRuntimeDrainBridgeResult(status="unknown_locked")
        ),
    )

    result = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        legacy_bridge_token="bridge-token",
        legacy_runtime_identity=identity,
        legacy_runtime_identity_reader=lambda: next(identities),
    )

    assert result.status == "cancelled_authority_retained"
    assert result.reason_code == "legacy_bridge_worker_identity_drift"
    assert unknown_reasons == ["worker_identity_drift"]
    assert len(client.cancel_payloads) == 1
    authority = _authority_document(session_factory)
    assert authority["state"] == "held"


def test_last_cancel_terminalizes_binding_and_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()

    first_plan = _build_plan(session_factory, client, targets)
    first = _apply_one(session_factory, client, targets, first_plan)
    assert first.status == "cancelled"

    second_plan = _build_plan(session_factory, client, targets)
    second = _apply_one(
        session_factory,
        client,
        targets,
        second_plan,
        order_id="reviewed-2",
        confirmation_token="cancel-token-two",
    )

    assert second.status == "cancelled"
    assert len(client.cancel_payloads) == 2
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert binding.status == "cancelled"
        assert binding.last_exchange_status == "reviewed_pending_entries_cancelled"
        assert lifecycle.lifecycle_status == "expired"
        assert lifecycle.exit_reason == "expired"
        assert lifecycle.exited_at == NOW.replace(tzinfo=None)
        assert lifecycle.management_action == "reviewed_pending_entries_cancelled"
        assert lifecycle.expiry_review_next_at is None

    completed = _build_plan(session_factory, client, targets)
    assert completed.actions == ()
    assert completed.conflicts == ()
    assert completed.completed_order_ids == ("reviewed-1", "reviewed-2")


def test_cancel_exception_is_recorded_unknown_once_and_cannot_be_retried(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.cancel_exception = RuntimeError("credential=do-not-leak")
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        confirmation_token="unknown-token-one",
    )

    assert result.status == "cancel_outcome_unknown"
    assert result.reason_code == "cancel_outcome_unknown"
    assert len(client.cancel_payloads) == 1
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_ids[0]).status == "pending"
        event = (
            session.query(ExecutionEvent)
            .filter_by(
                action="cancel_reviewed_pending_entry",
                order_id="reviewed-1",
            )
            .one()
        )
        assert event.status == "unknown"
        assert event.reason == "cancel_outcome_unknown"
        serialized = " ".join(
            str(value or "")
            for value in (event.reason, event.request_json, event.response_json)
        )
        assert "credential" not in serialized
        assert "do-not-leak" not in serialized
        assert session.query(RepairConfirmationToken).count() == 1

    with pytest.raises(ValueError, match="already consumed"):
        _apply_one(
            session_factory,
            client,
            targets,
            plan,
            confirmation_token="unknown-token-one",
        )
    assert len(client.cancel_payloads) == 1

    blocked_plan = _build_plan(session_factory, client, targets)
    assert blocked_plan.actions == ()
    assert blocked_plan.conflicts == (
        {"order_id": "reviewed-1", "reason": "prior_cancel_outcome_unknown"},
    )


def test_cancel_response_without_exact_order_identity_is_unknown_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.cancel_response = {
        "code": "0",
        "data": [{"ordId": "different-order", "raw": "secret-response"}],
    }
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(
        session_factory,
        client,
        targets,
        plan,
        confirmation_token="invalid-response-token",
    )

    assert result.status == "cancel_outcome_unknown"
    assert result.reason_code == "cancel_response_unconfirmed"
    assert len(client.cancel_payloads) == 1
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_ids[0]).status == "pending"
        mutation = session.query(PositionMutationIntent).one()
        assert mutation.status == "recovery_required"
        event = session.query(ExecutionEvent).one()
        assert event.status == "unknown"
        serialized = " ".join(
            str(value or "")
            for value in (event.reason, event.request_json, event.response_json)
        )
        assert "secret-response" not in serialized
        assert "different-order" not in serialized


def test_apply_rejects_stale_fingerprint_and_action_before_write(tmp_path):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        apply_reviewed_pending_entry_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    action = plan.actions[0]

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        apply_reviewed_pending_entry_cancel_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            targets=targets,
            order_id=action.order_id,
            action_id=action.action_id,
            expected_fingerprint="0" * 64,
            confirmation_token="stale-token-one",
            now=NOW,
        )
    with pytest.raises(ValueError, match="exactly one"):
        apply_reviewed_pending_entry_cancel_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            targets=targets,
            order_id=action.order_id,
            action_id="0" * 64,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="stale-token-two",
            now=NOW,
        )
    assert client.cancel_payloads == []


def test_confirmed_cancel_with_changed_sibling_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.mutate_sibling_after_cancel = True
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancel_confirmed_readback_changed"
    assert result.reason_code == "post_cancel_state_changed"
    assert len(client.cancel_payloads) == 1
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_ids[0]).status == "pending"
        event = (
            session.query(ExecutionEvent)
            .filter_by(
                action="cancel_reviewed_pending_entry",
                order_id="reviewed-1",
            )
            .one()
        )
        assert event.status == "confirmed_readback_changed"
        assert event.reason == "post_cancel_state_changed"


def test_rejected_history_is_not_confirmed_as_cancelled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()

    def replace_history_with_rejection():
        client.trigger_history[-1]["state"] = "rejected"

    client.after_cancel_callback = replace_history_with_rejection
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancel_confirmed_readback_changed"
    assert result.reason_code == "post_cancel_state_changed"
    assert len(client.cancel_payloads) == 1
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_ids[0]).status == "pending"


def test_terminalization_rechecks_full_local_identity_after_exchange_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()

    def mutate_local_identity():
        with session_factory() as session:
            leg = session.get(ExecutionOrderLeg, leg_ids[0])
            leg.purpose = "exit"
            leg.venue = "other"
            session.commit()

    client.after_cancel_callback = mutate_local_identity
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancelled_audit_state_changed"
    assert result.reason_code == "confirmed_cancel_database_state_changed"
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_ids[0])
        assert leg.status == "pending"
        assert leg.terminal_reason is None
        mutation = session.query(PositionMutationIntent).one()
        assert mutation.status == "recovery_required"


def test_terminalization_rejects_changed_mutation_intent_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()

    def mutate_intent_evidence():
        with session_factory() as session:
            mutation = session.query(PositionMutationIntent).one()
            mutation.venue = "other"
            mutation.error_json = '{"reason":"corrupt"}'
            session.commit()

    client.after_cancel_callback = mutate_intent_evidence
    plan = _build_plan(session_factory, client, targets)

    result = _apply_one(session_factory, client, targets, plan)

    assert result.status == "cancelled_audit_state_changed"
    assert result.reason_code == "confirmed_cancel_database_state_changed"
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_ids[0]).status == "pending"
        mutation = session.query(PositionMutationIntent).one()
        assert mutation.status == "recovery_required"


def test_completed_plan_rejects_tampered_durable_cancellation_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    plan = _build_plan(session_factory, client, targets)
    assert _apply_one(session_factory, client, targets, plan).status == "cancelled"

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_ids[0])
        leg.terminal_reason = "wrong_reason"
        event = session.query(ExecutionEvent).one()
        event.before_json = "{}"
        event.response_json = "{}"
        mutation = session.query(PositionMutationIntent).one()
        mutation.venue = "other"
        mutation.error_json = '{"reason":"corrupt"}'
        mutation.confirmed_at = None
        mutation.response_json = "{}"
        session.commit()

    repeated = _build_plan(session_factory, client, targets)

    assert repeated.actions == ()
    assert repeated.completed_order_ids == ()
    assert repeated.conflicts == (
        {"order_id": "reviewed-1", "reason": "reviewed_order_not_pending"},
    )


def test_cli_defaults_to_closed_schema_dry_run_without_write(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.pending[0]["rawSecret"] = "must-not-render"
    monkeypatch.setattr(
        cli_module,
        "REVIEWED_PENDING_ENTRY_TARGETS",
        targets,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: client,
    )
    monkeypatch.setattr(
        cli_module,
        "create_session_factory",
        lambda *_args, **_kwargs: pytest.fail(
            "dry-run must not invoke schema-creating session setup"
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda _path: session_factory,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "cancel-reviewed-pending-entries",
            "--database-path",
            str(tmp_path / "research.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert len(payload["plan"]["actions"]) == 2
    assert client.cancel_payloads == []
    assert "must-not-render" not in result.output
    assert "rawSecret" not in result.output


def test_cli_apply_requires_all_exact_single_order_guards(tmp_path, monkeypatch):
    import telegram_kol_research.cli as cli_module

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    monkeypatch.setattr(
        cli_module,
        "REVIEWED_PENDING_ENTRY_TARGETS",
        targets,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "cancel-reviewed-pending-entries",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "--order-id" in result.output
    assert "--action-id" in result.output
    assert "--expected-fingerprint" in result.output
    assert "--confirmation-token" in result.output
    assert client.cancel_payloads == []


def test_cli_conflict_dry_run_exits_nonzero(tmp_path, monkeypatch):
    import telegram_kol_research.cli as cli_module

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    client.positions.append(
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "unexpected-position",
            "posSide": "long",
            "pos": "1",
        }
    )
    monkeypatch.setattr(
        cli_module,
        "REVIEWED_PENDING_ENTRY_TARGETS",
        targets,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: client,
    )
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda _path: session_factory,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "cancel-reviewed-pending-entries",
            "--database-path",
            str(tmp_path / "research.db"),
        ],
    )

    assert result.exit_code == 2
    assert '"reason": "live_position_present"' in result.output
    assert client.cancel_payloads == []


def test_cli_help_exposes_reviewed_pending_entry_guards():
    from telegram_kol_research.cli import app

    result = CliRunner().invoke(
        app,
        ["cancel-reviewed-pending-entries", "--help"],
    )

    assert result.exit_code == 0
    assert "--order-id" in result.output
    assert "--action-id" in result.output
    assert "--expected-fingerprint" in result.output
    assert "--confirmation-token" in result.output
    assert "--bridge-token" in result.output
    assert "--expected-production-sha" in result.output


def test_cli_bridge_mode_passes_exact_runtime_identity_to_plan_and_apply(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.legacy_runtime_drain_bridge import (
        LegacyRuntimeIdentity,
    )
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        ReviewedPendingEntryCancelResult,
    )

    session_factory = create_session_factory(tmp_path / "bridge-cli.db")
    binding_id, lifecycle_id, leg_ids = _seed(session_factory)
    targets = _targets(binding_id, lifecycle_id, leg_ids)
    client = _Client()
    identity = LegacyRuntimeIdentity(
        production_sha="0a6a9a18d1d62ff3c7d0c4c27cdab5961d94339f",
        worker_pid=303,
        worker_start_ticks=404,
    )
    captured = []
    monkeypatch.setattr(cli_module, "REVIEWED_PENDING_ENTRY_TARGETS", targets)
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env", lambda: client
    )
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda _path: session_factory,
    )
    monkeypatch.setattr(
        cli_module,
        "read_local_legacy_worker_identity",
        lambda **_kwargs: identity,
    )
    original_builder = cli_module.build_reviewed_pending_entry_cancel_plan

    def build_plan(*args, **kwargs):
        captured.append(("plan", kwargs.get("legacy_runtime_identity")))
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        cli_module,
        "build_reviewed_pending_entry_cancel_plan",
        build_plan,
    )
    monkeypatch.setattr(
        cli_module,
        "apply_reviewed_pending_entry_cancel_plan",
        lambda *_args, **kwargs: (
            captured.append(
                (
                    "apply",
                    kwargs.get("legacy_bridge_token"),
                    kwargs.get("legacy_runtime_identity"),
                )
            )
            or ReviewedPendingEntryCancelResult(
                status="cancelled",
                order_id=kwargs["order_id"],
            )
        ),
    )
    base_args = [
        "--database-path",
        str(tmp_path / "bridge-cli.db"),
        "--bridge-token",
        "exact-bridge-token",
        "--checkout-path",
        str(tmp_path),
        "--expected-production-sha",
        identity.production_sha,
    ]
    dry = CliRunner().invoke(
        cli_module.app,
        ["cancel-reviewed-pending-entries", *base_args],
    )
    plan = json.loads(dry.output)["plan"]
    action = plan["actions"][0]

    applied = CliRunner().invoke(
        cli_module.app,
        [
            "cancel-reviewed-pending-entries",
            *base_args,
            "--apply",
            "--order-id",
            action["order_id"],
            "--action-id",
            action["action_id"],
            "--expected-fingerprint",
            plan["fingerprint"],
            "--confirmation-token",
            "bridge-cli-confirmation",
        ],
    )

    assert dry.exit_code == 0, dry.output
    assert applied.exit_code == 0, applied.output
    assert captured == [
        ("plan", identity),
        ("plan", identity),
        ("apply", "exact-bridge-token", identity),
    ]
