from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    InstructionExecutionContract,
    ExecutionOrderLeg,
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    MessageInstructionItem,
    PositionMutationIntent,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
)
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.strategy_management_planner import _protection_incident_requires_recovery
from telegram_kol_research.strategy_management_batches import (
    create_management_batch,
    load_management_batch,
    transition_batch,
)
from telegram_kol_research.strategy_management_executor import (
    ManagementBatchExecutionError,
)
from telegram_kol_research.strategy_management_worker import (
    StrategyManagementWorkerCursor,
    _resolve_capability_deferred_successor,
    _resolve_deferred_entry_cancel_race_successor,
    _advance_temporary_visibility_retry,
    run_strategy_management_worker_tick,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _batch(*, batch_id: int, strategy: str, status: str, action="partial_close", legs=(), management_contract_json=None):
    return SimpleNamespace(
        id=batch_id,
        strategy_instance_id=strategy,
        status=status,
        effective_action=action,
        reason_code=None,
        legs=tuple(legs),
        management_contract_json=management_contract_json,
    )


def test_management_preflight_requires_operator_recovery_for_exact_protection_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "incident.db")
    with session_factory() as session:
        session.add(PositionProtectionIncident(
            venue="deepcoin", execution_binding_id=1, execution_order_leg_id=9,
            pos_id="pos-1", incident_type="stop_trigger_failed", fingerprint="f" * 64,
            evidence_json="{}", delivery_status="pending",
        ))
        session.commit()
        assert _protection_incident_requires_recovery(
            session, entry_legs=(SimpleNamespace(id=9, pos_id="pos-1"),)
        ) is True
        assert _protection_incident_requires_recovery(
            session, entry_legs=(SimpleNamespace(id=10, pos_id="pos-1"),)
        ) is False


def test_visibility_retry_expiry_becomes_actionable_terminal_block(tmp_path):
    session_factory = create_session_factory(tmp_path / "visibility-expiry.db")
    with session_factory() as session:
        batch = StrategyManagementBatch(
            idempotency_fingerprint="v" * 64,
            raw_message_id=1,
            recognition_decision_id=1,
            recognition_generation="g1",
            target_lifecycle_id=1,
            strategy_instance_id="deepcoin:100:10:BTC:short",
            execution_binding_id=1,
            intent="adjust_stop_loss",
            effective_action="adjust_stop_loss",
            partial_round_before=0,
            status="blocked",
            reason_code="target_protection_snapshot_incomplete",
            target_fingerprint="b" * 64,
            target_snapshot_json="{}",
            planned_at=NOW,
            visibility_first_failed_at=NOW,
            visibility_retry_attempts=5,
            visibility_next_attempt_at=NOW + timedelta(seconds=120),
            notification_state="sent",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    _advance_temporary_visibility_retry(
        session_factory, batch_id=batch_id, now=NOW + timedelta(minutes=5)
    )

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        assert batch.reason_code == "protection_visibility_retry_expired"
        assert batch.visibility_next_attempt_at is None
        assert batch.notification_state == "pending"


def test_worker_retries_due_visibility_item_and_finishes_same_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "item-visibility.db")
    save_trading_settings(
        session_factory,
        {"instruction_execution_contract_mode": "live"},
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=30, text="BTC exit")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="close_signal",
            management_action="full_exit",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            strategy_instance_id="deepcoin:100:20:BTC:short",
            idempotency_key="v" * 64,
            status="pending",
            result_json=json.dumps(
                {
                    "status": "deferred",
                    "reason": "target_strategy_binding_not_visible_yet",
                    "execution_mode": "live",
                }
            ),
            visibility_first_failed_at=NOW - timedelta(seconds=5),
            visibility_retry_attempts=1,
            visibility_next_attempt_at=NOW,
        )
        session.add(item)
        session.commit()
        item_id = item.id

    planned = SimpleNamespace(
        status="ready",
        reason_code=None,
        batch=SimpleNamespace(id=77),
        batch_id=77,
    )
    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [],
        binding_reconciler=lambda *_args, **_kwargs: None,
        take_profit_convergence_runner=lambda *_args, **_kwargs: 0,
        instruction_planner=lambda *_args, **_kwargs: planned,
        executor=lambda *_args, **_kwargs: {
            "status": "reconciling",
            "submitted": True,
            "batch_id": 77,
            "legs": [],
        },
        contract_spec_provider=object(),
        processed_at=NOW,
    )

    assert result.executed == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "submitted"
        assert item.visibility_retry_attempts == 1
        assert session.query(InstructionExecutionContract).count() == 0


def test_worker_fails_closed_on_unregistered_executor_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-worker-outcome.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=32, text="BTC exit")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="close_signal",
            management_action="full_exit",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            idempotency_key="u" * 64,
            status="pending",
            result_json=json.dumps(
                {
                    "status": "deferred",
                    "reason": "target_strategy_binding_not_visible_yet",
                    "execution_mode": "live",
                }
            ),
            visibility_first_failed_at=NOW - timedelta(seconds=5),
            visibility_retry_attempts=1,
            visibility_next_attempt_at=NOW,
        )
        session.add(item)
        session.commit()
        item_id = item.id
    planned = SimpleNamespace(
        status="ready",
        reason_code=None,
        batch=SimpleNamespace(id=78),
        batch_id=78,
    )

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [],
        binding_reconciler=lambda *_args, **_kwargs: None,
        take_profit_convergence_runner=lambda *_args, **_kwargs: 0,
        instruction_planner=lambda *_args, **_kwargs: planned,
        executor=lambda *_args, **_kwargs: {"status": "mystery"},
        contract_spec_provider=object(),
        processed_at=NOW,
    )

    assert result.failed == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert json.loads(item.error_json)["reason"] == (
            "instruction_outcome_contract_invalid"
        )


def test_worker_captures_target_orchestration_failure_after_item_commit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES",
        "management_target_orchestration_failed",
    )
    session_factory = create_session_factory(tmp_path / "target-incident.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=31, text="BTC exit")
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=30,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        session.add_all((raw, lifecycle))
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="close_signal",
            target_lifecycle_id=lifecycle.id,
            management_action="full_exit",
        )
        envelope = ManagementMessageEnvelope(
            raw_message_id=raw.id,
            decision_fingerprint="d" * 64,
            normalized_action="exit_full",
            shared_parameters_json="{}",
            projection_mode="live",
        )
        session.add_all((candidate, envelope))
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            idempotency_key="o" * 64,
            status="pending",
            visibility_first_failed_at=NOW - timedelta(seconds=5),
            visibility_retry_attempts=1,
            visibility_next_attempt_at=NOW,
        )
        session.add(item)
        session.flush()
        target = ManagementMessageTarget(
            envelope_id=envelope.id,
            raw_message_id=raw.id,
            target_lifecycle_id=lifecycle.id,
            target_ordinal=0,
            symbol="BTC",
            side="short",
            normalized_action="exit_full",
            parameters_json="{}",
            parameter_fingerprint="p" * 64,
            collision_group_fingerprint="c" * 64,
            admission_state="admitted",
            execution_state="pending",
            signal_candidate_id=candidate.id,
            message_instruction_item_id=item.id,
        )
        session.add(target)
        session.commit()
        item_id, target_id = item.id, target.id

    blocked = SimpleNamespace(
        status="blocked",
        reason_code="target_position_snapshot_unavailable",
        batch=None,
        batch_id=None,
    )
    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [],
        binding_reconciler=lambda *_args, **_kwargs: None,
        take_profit_convergence_runner=lambda *_args, **_kwargs: 0,
        instruction_planner=lambda *_args, **_kwargs: blocked,
        contract_spec_provider=object(),
        processed_at=NOW,
    )

    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "failed"
        target = session.get(ManagementMessageTarget, target_id)
        assert target.execution_state == "failed"
        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "management_target_orchestration_failed"
        assert incident.source_record_id == str(target_id)
        assert target.latest_runtime_incident_id == incident.id


def test_worker_runs_ready_take_profit_convergence_only_when_execution_enabled():
    calls = []

    def convergence_runner(*_args, **kwargs):
        calls.append(kwargs["processed_at"])
        return 1

    enabled = run_strategy_management_worker_tick(
        object(), deepcoin_client_factory=lambda: object(), max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [], processed_at=NOW,
        take_profit_convergence_runner=convergence_runner,
    )
    disabled = run_strategy_management_worker_tick(
        object(), deepcoin_client_factory=lambda: object(), max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [], processed_at=NOW,
        allow_execution=False, take_profit_convergence_runner=convergence_runner,
    )

    assert calls == [NOW]
    assert enabled.executed == 1
    assert disabled.executed == 0


def test_worker_keeps_composite_recovery_active_when_new_execution_is_disabled():
    calls = []

    def composite_reconciler(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(reconciled=1, executed=0, awaiting=0)

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [],
        allow_execution=False,
        processed_at=NOW,
        composite_reconciler=composite_reconciler,
    )

    assert len(calls) == 1
    assert calls[0]["allow_new_writes"] is False


def test_worker_routes_contract_batch_only_to_composite_executor(monkeypatch):
    import telegram_kol_research.strategy_management_worker as worker_module

    calls = []
    monkeypatch.setattr(
        worker_module,
        "load_trading_settings",
        lambda *_args: SimpleNamespace(
            effective_composite_management_v2_mode="live",
            trigger_backup_stop_buffer_bps=20,
        ),
    )
    batch = _batch(
        batch_id=41,
        strategy="composite",
        status="ready",
        management_contract_json='{"version":2}',
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [batch],
        claimer=lambda *_args, **_kwargs: True,
        composite_executor=lambda *_args, **kwargs: calls.append(kwargs),
        executor=lambda *_args, **_kwargs: pytest.fail("legacy executor called"),
        take_profit_convergence_runner=lambda *_args, **_kwargs: 0,
        processed_at=NOW,
    )

    assert result.executed == 1
    assert calls[0]["batch_id"] == 41


def test_worker_reconciles_backup_stops_before_running_take_profit_lane():
    calls = []

    provider = object()

    def binding_reconciler(*_args, **kwargs):
        calls.append(("backup_reconcile", kwargs))

    def convergence_runner(*_args, **_kwargs):
        calls.append("take_profit")
        return 0

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        max_batches=1,
        batch_lister=lambda *_args, **_kwargs: [],
        snapshot_loader=lambda *_args, **_kwargs: object(),
        binding_reconciler=binding_reconciler,
        contract_spec_provider=provider,
        processed_at=NOW,
        take_profit_convergence_runner=convergence_runner,
    )

    assert calls[0][0] == "backup_reconcile"
    assert calls[0][1]["contract_spec_provider"] is provider
    assert calls[1] == "take_profit"


def test_worker_claims_ready_batch_once_across_racing_ticks(tmp_path):
    session_factory = create_session_factory(tmp_path / "worker.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="a" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="b" * 64,
        target_snapshot_json="{}",
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        # This test exercises the database claim only; FK enforcement is disabled
        # in the SQLite test database just as it is for existing batch unit tests.
        session.add(row)
        session.commit()
        batch_id = row.id
        strategy_instance_id = row.strategy_instance_id

    executed = []
    lister = lambda *_args, **_kwargs: [
        _batch(batch_id=batch_id, strategy=strategy_instance_id, status="ready")
    ]

    def execute(*_args, **kwargs):
        executed.append(kwargs["batch_id"])

    def tick():
        return run_strategy_management_worker_tick(
            session_factory,
            deepcoin_client_factory=lambda: object(),
                batch_lister=lister,
                executor=execute,
                restart_validator=lambda *_args, **_kwargs: None,
            max_batches=1,
            processed_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [
            future.result() for future in (pool.submit(tick), pool.submit(tick))
        ]

    assert executed == [batch_id]
    assert sorted((first.executed, second.executed)) == [0, 1]


def test_restart_queries_exchange_before_reconciling_and_never_resubmits_unknown():
    events = []
    unknown = _batch(
        batch_id=7,
        strategy="deepcoin:100:10:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submit_unknown"),),
    )

    def snapshot_loader(*_args, **_kwargs):
        events.append("exchange-read")
        return object()

    def reconciler(*_args, **kwargs):
        events.append(("reconcile", tuple(kwargs["batch_ids"])))

    def executor(*_args, **_kwargs):
        raise AssertionError("submit_unknown must never be submitted again")

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [unknown],
        snapshot_loader=snapshot_loader,
        reconciler=reconciler,
        executor=executor,
        processed_at=NOW,
    )

    assert events == ["exchange-read", ("reconcile", (7,))]
    assert result.recovered == 1
    assert result.executed == 0


@pytest.mark.parametrize(
    "status", ["reserved", "submitted", "submit_unknown", "reconciling"]
)
def test_restart_batch_states_are_reconciled_only(status):
    events = []
    batch = _batch(
        batch_id=8,
        strategy="deepcoin:100:10:BTC:short",
        status=status,
        legs=(SimpleNamespace(status=status),),
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [batch],
        snapshot_loader=lambda *_args, **_kwargs: events.append("read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("reconcile"),
        executor=lambda *_args, **_kwargs: events.append("execute"),
        processed_at=NOW,
    )

    assert events == ["read", "reconcile"]
    assert result.recovered == 1


def test_recovery_required_is_paused_and_other_strategies_are_independent():
    executed = []
    batches = [
        _batch(batch_id=1, strategy="deepcoin:100:10:BTC:short", status="recovery_required"),
        _batch(batch_id=2, strategy="deepcoin:200:10:BTC:short", status="ready"),
    ]

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: batches,
        claimer=lambda *_args, **kwargs: kwargs["batch_id"] == 2,
        executor=lambda *_args, **kwargs: executed.append(kwargs["batch_id"]),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        processed_at=NOW,
    )

    assert executed == [2]
    assert result.executed == 1
    assert result.paused == 1


def test_cancel_race_parent_is_reconciled_and_resolved_into_successor():
    events = []
    race_parent = _batch(
        batch_id=71,
        strategy="deepcoin:100:10:BTC:short",
        status="recovery_required",
    )
    race_parent.reason_code = "deferred_entry_cancel_race_detected"

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: events.append("client") or object(),
        batch_lister=lambda *_args, **_kwargs: [race_parent],
        snapshot_loader=lambda *_args, **_kwargs: events.append("snapshot") or object(),
        binding_reconciler=lambda *_args, **_kwargs: events.append("binding-reconcile"),
        race_successor_resolver=lambda *_args, **kwargs: (
            events.append(("resolve", kwargs["batch_id"])), True
        )[1],
        processed_at=NOW,
    )

    assert events == [
        "client",
        "snapshot",
        "binding-reconcile",
        ("resolve", 71),
    ]
    assert result.recovered == 1
    assert result.paused == 0


def test_cancel_race_snapshot_read_failure_leaves_parent_paused():
    race_parent = _batch(
        batch_id=72, strategy="deepcoin:100:10:BTC:short", status="recovery_required"
    )
    race_parent.reason_code = "deferred_entry_cancel_race_detected"
    resolver_called = []

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [race_parent],
        snapshot_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("read failed")),
        race_successor_resolver=lambda *_args, **_kwargs: resolver_called.append(True),
        processed_at=NOW,
    )

    assert result.failed == 1
    assert resolver_called == []


def test_subset_parent_reconciles_then_schedules_capability_successor(
    monkeypatch,
):
    parent = _batch(
        batch_id=73,
        strategy="deepcoin:100:10:BTC:short",
        status="reconciling",
        action="full_exit",
    )
    parent.reason_code = "management_subset_close_exchange_confirmed"
    events = []
    monkeypatch.setattr(
        "telegram_kol_research.strategy_management_worker.load_management_batch",
        lambda *_args, **_kwargs: parent,
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [parent],
        snapshot_loader=lambda *_args, **_kwargs: events.append("snapshot")
        or object(),
        reconciler=lambda *_args, **kwargs: events.append(
            ("reconcile", tuple(kwargs["batch_ids"]))
        ),
        capability_successor_resolver=lambda *_args, **kwargs: events.append(
            ("successor", kwargs["batch_id"])
        )
        or True,
        processed_at=NOW,
    )

    assert events == ["snapshot", ("reconcile", (73,)), ("successor", 73)]
    assert result.recovered == 1


def test_cancel_race_successor_requires_exact_verified_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "race-successor.db")
    strategy = "deepcoin:100:10:BTC:short"
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="mia", chat_id=100, message_id=10, symbol="BTC", side="short",
            strategy_instance_id=strategy, status="active",
        ),
    )
    entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy, leg_index=0,
            order_id="trigger-1", pos_id="pos-race", order_kind="trigger_limit",
            attribution_status="verified", attribution_evidence={"policy_version": 2},
            status="active",
        ),
    )
    second_entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy, leg_index=1,
            order_id="trigger-2", pos_id="pos-alpha", order_kind="trigger_limit",
            attribution_status="verified", attribution_evidence={"policy_version": 2},
            status="active",
        ),
    )
    parent = create_management_batch(
        session_factory,
        idempotency_fingerprint="parent-race-worker",
        raw_message_id=10, recognition_decision_id=1, recognition_generation="g1",
        target_lifecycle_id=1, strategy_instance_id=strategy,
        execution_binding_id=binding_id, intent="full_exit", effective_action="full_exit",
        execution_mode="live", requested_fraction=None, effective_fraction=1.0,
        partial_round_before=0, target_fingerprint="parent-target",
        target_snapshot={
            "identity": {"deferred_entry_leg_ids": [entry_id, second_entry_id]},
            "contract_spec": {"quantity_step": 1, "min_quantity": 1},
        },
        legs=[], planned_at=NOW,
    )
    transition_batch(
        session_factory, parent.id, expected_statuses={"ready"},
        new_status="recovery_required", reason_code="deferred_entry_cancel_race_detected",
        transitioned_at=NOW,
    )
    snapshot = SimpleNamespace(positions=[{
        "instId": "BTC-USDT-SWAP", "posId": "pos-race", "posSide": "short",
        "pos": "7", "avgPx": "66400", "mgnMode": "cross", "mrgPosition": "split",
    }, {
        "instId": "BTC-USDT-SWAP", "posId": "pos-alpha", "posSide": "short",
        "pos": "3", "avgPx": "66500", "mgnMode": "cross", "mrgPosition": "split",
    }])

    assert _resolve_deferred_entry_cancel_race_successor(
        session_factory, batch_id=parent.id, snapshot=snapshot, resolved_at=NOW
    )
    successor = load_management_batch(session_factory, parent.id + 1)
    assert successor.status == "ready"
    close_sizes = {leg.pos_id: leg.planned_close_size for leg in successor.legs}
    assert close_sizes == {"pos-race": "7", "pos-alpha": "3"}
    assert load_management_batch(session_factory, parent.id).status == "resolved"


def test_cancel_race_ambiguous_live_position_keeps_parent_for_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "race-ambiguous.db")
    strategy = "deepcoin:100:10:BTC:short"
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="mia", chat_id=100, message_id=10, symbol="BTC", side="short",
            strategy_instance_id=strategy, status="active",
        ),
    )
    entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy, leg_index=0,
            order_id="trigger-1", pos_id="pos-race", order_kind="trigger_limit",
            attribution_status="verified", attribution_evidence={"policy_version": 2},
            status="active",
        ),
    )
    parent = create_management_batch(
        session_factory, idempotency_fingerprint="ambiguous-race", raw_message_id=10,
        recognition_decision_id=1, recognition_generation="g1", target_lifecycle_id=1,
        strategy_instance_id=strategy, execution_binding_id=binding_id, intent="full_exit",
        effective_action="full_exit", execution_mode="live", requested_fraction=None,
        effective_fraction=1.0, partial_round_before=0, target_fingerprint="parent-target",
        target_snapshot={
            "identity": {"deferred_entry_leg_ids": [entry_id]},
            "contract_spec": {"quantity_step": 1, "min_quantity": 1},
        }, legs=[], planned_at=NOW,
    )
    transition_batch(
        session_factory, parent.id, expected_statuses={"ready"},
        new_status="recovery_required", reason_code="deferred_entry_cancel_race_detected",
        transitioned_at=NOW,
    )
    position = {
        "instId": "BTC-USDT-SWAP", "posId": "pos-race", "posSide": "short",
        "pos": "7", "avgPx": "66400", "mgnMode": "cross", "mrgPosition": "split",
    }

    assert not _resolve_deferred_entry_cancel_race_successor(
        session_factory, batch_id=parent.id,
        snapshot=SimpleNamespace(positions=[position, dict(position)]), resolved_at=NOW,
    )
    assert load_management_batch(session_factory, parent.id).status == "recovery_required"


def test_cancel_race_successor_creation_is_idempotent_on_repeat_tick(tmp_path):
    session_factory = create_session_factory(tmp_path / "race-repeat.db")
    strategy = "deepcoin:100:10:BTC:short"
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="mia", chat_id=100, message_id=10, symbol="BTC", side="short",
            strategy_instance_id=strategy, status="active",
        ),
    )
    entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy, leg_index=0,
            order_id="trigger-1", pos_id="pos-race", order_kind="trigger_limit",
            attribution_status="verified", attribution_evidence={"policy_version": 2},
            status="active",
        ),
    )
    parent = create_management_batch(
        session_factory, idempotency_fingerprint="repeat-race", raw_message_id=10,
        recognition_decision_id=1, recognition_generation="g1", target_lifecycle_id=1,
        strategy_instance_id=strategy, execution_binding_id=binding_id, intent="full_exit",
        effective_action="full_exit", execution_mode="live", requested_fraction=None,
        effective_fraction=1.0, partial_round_before=0, target_fingerprint="parent-target",
        target_snapshot={
            "identity": {"deferred_entry_leg_ids": [entry_id]},
            "contract_spec": {"quantity_step": 1, "min_quantity": 1},
        }, legs=[], planned_at=NOW,
    )
    transition_batch(
        session_factory, parent.id, expected_statuses={"ready"},
        new_status="recovery_required", reason_code="deferred_entry_cancel_race_detected",
        transitioned_at=NOW,
    )
    snapshot = SimpleNamespace(positions=[{
        "instId": "BTC-USDT-SWAP", "posId": "pos-race", "posSide": "short",
        "pos": "7", "avgPx": "66400", "mgnMode": "cross", "mrgPosition": "split",
    }])

    assert _resolve_deferred_entry_cancel_race_successor(
        session_factory, batch_id=parent.id, snapshot=snapshot, resolved_at=NOW
    )
    assert not _resolve_deferred_entry_cancel_race_successor(
        session_factory, batch_id=parent.id, snapshot=snapshot, resolved_at=NOW
    )
    with session_factory() as session:
        assert session.query(StrategyManagementBatch).count() == 2


@pytest.mark.parametrize("deferred_position_still_live", [True, False])
def test_capability_deferred_subset_finishes_after_mutation_resolves(
    tmp_path, deferred_position_still_live,
):
    session_factory = create_session_factory(tmp_path / "capability-successor.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "position_management_liveness_v2_mode": "live",
        },
    )
    strategy = "deepcoin:100:10:BTC:short"
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="mia", chat_id=100, message_id=10, symbol="BTC",
            side="short", strategy_instance_id=strategy, status="active",
            pos_id="pos-safe,pos-deferred",
        ),
    )
    safe_entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy,
            leg_index=0, order_id="entry-safe", pos_id="pos-safe",
            order_kind="market", attribution_status="verified",
            attribution_evidence={"policy_version": 2}, status="closed",
        ),
    )
    deferred_entry_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, strategy_instance_id=strategy,
            leg_index=1, order_id="entry-deferred", pos_id="pos-deferred",
            order_kind="market", attribution_status="verified",
            attribution_evidence={"policy_version": 2},
            status="partial_closed",
        ),
    )
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=100, message_id=10, symbol="BTC", side="short",
            lifecycle_status="entered", signal_at=NOW, entered_at=NOW,
            execution_binding_id=binding_id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = int(lifecycle.id)
    parent = create_management_batch(
        session_factory, idempotency_fingerprint="capability-parent",
        raw_message_id=10, recognition_decision_id=1,
        recognition_generation="g1", target_lifecycle_id=lifecycle_id,
        strategy_instance_id=strategy, execution_binding_id=binding_id,
        intent="full_exit", effective_action="full_exit",
        execution_mode="live", requested_fraction=None,
        effective_fraction=1.0, partial_round_before=0,
        target_fingerprint="capability-parent-target",
        target_snapshot={
            "identity": {
                "manageable_entry_leg_ids": [safe_entry_id],
                "capability_deferred_entry_leg_ids": [deferred_entry_id],
                "capability_deferred_pos_ids": ["pos-deferred"],
            },
            "contract_spec": {"quantity_step": 1, "min_quantity": 1},
        },
        legs=[], planned_at=NOW,
    )
    transition_batch(
        session_factory, parent.id, expected_statuses={"ready"},
        new_status="reconciling",
        reason_code="management_subset_close_exchange_confirmed",
        transitioned_at=NOW,
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-deferred"
        mutation = PositionMutationIntent(
            idempotency_key="capability-deferred-unknown",
            venue="deepcoin", operation="close_position",
            strategy_instance_id=strategy, execution_binding_id=binding_id,
            execution_order_leg_id=deferred_entry_id,
            pos_id="pos-deferred", authority_fingerprint="a" * 64,
            request_fingerprint="b" * 64, status="submit_unknown",
            request_json="{}", reserved_at=NOW,
        )
        session.add(mutation)
        session.commit()
        mutation_id = int(mutation.id)
    snapshot = SimpleNamespace(
        errors={},
        positions=([{
            "instId": "BTC-USDT-SWAP", "posId": "pos-deferred",
            "posSide": "short", "pos": "3", "avgPx": "66500",
            "mgnMode": "cross", "mrgPosition": "split",
        }] if deferred_position_still_live else []),
    )

    assert not _resolve_capability_deferred_successor(
        session_factory, batch_id=parent.id, snapshot=snapshot,
        resolved_at=NOW,
    )
    with session_factory() as session:
        mutation = session.get(PositionMutationIntent, mutation_id)
        mutation.status = "confirmed"
        session.commit()

    worker_result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [
            load_management_batch(session_factory, parent.id)
        ],
        snapshot_loader=lambda *_args, **_kwargs: snapshot,
        processed_at=NOW,
    )
    assert worker_result.recovered == 1
    final_parent = load_management_batch(session_factory, parent.id)
    if deferred_position_still_live:
        successor = load_management_batch(session_factory, parent.id + 1)
        assert successor.status == "ready"
        assert successor.target_snapshot[
            "capability_deferred_successor_of"
        ] == parent.id
        assert [
            (leg.pos_id, leg.planned_close_size) for leg in successor.legs
        ] == [("pos-deferred", "3")]
        assert final_parent.status == "resolved"
        assert final_parent.reason_code == (
            "management_subset_continued_by_successor"
        )
    else:
        assert final_parent.status == "succeeded"
        assert final_parent.reason_code == (
            "management_full_close_exchange_confirmed"
        )
        with session_factory() as session:
            binding = session.get(ExecutionBinding, binding_id)
            deferred = session.get(ExecutionOrderLeg, deferred_entry_id)
            lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert (binding.status, binding.pos_id) == ("closed", None)
        assert deferred.status == "closed"
        assert deferred.terminal_reason == "position_mutation_close_confirmed"
        assert lifecycle.lifecycle_status == "exited"


def test_disabled_or_shadow_mode_never_claims_or_executes_ready_batches():
    events = []
    ready = _batch(
        batch_id=2,
        strategy="deepcoin:200:10:BTC:short",
        status="ready",
    )

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: events.append("client") or object(),
        batch_lister=lambda *_args, **_kwargs: [ready],
        claimer=lambda *_args, **_kwargs: events.append("claim") or True,
        executor=lambda *_args, **_kwargs: events.append("execute"),
        allow_execution=False,
        processed_at=NOW,
    )

    assert events == []
    assert result.skipped == 1


def test_batch_failure_does_not_stop_later_bounded_work():
    batches = [
        _batch(batch_id=1, strategy="deepcoin:100:10:BTC:short", status="ready"),
        _batch(batch_id=2, strategy="deepcoin:200:10:BTC:short", status="ready"),
        _batch(batch_id=3, strategy="deepcoin:300:10:BTC:short", status="ready"),
    ]
    executed = []

    def execute(*_args, **kwargs):
        executed.append(kwargs["batch_id"])
        if kwargs["batch_id"] == 1:
            raise RuntimeError("boom")

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: batches,
        claimer=lambda *_args, **_kwargs: True,
        executor=execute,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=2,
        processed_at=NOW,
    )

    assert executed == [1, 2]
    assert result.failed == 1
    assert result.executed == 1
    assert result.discovered == 2


def test_composite_protection_phase_is_not_sent_to_close_reconciliation():
    composite = _batch(
        batch_id=9,
        strategy="deepcoin:100:10:BTC:short",
        status="executing",
        action="partial_then_break_even",
        legs=(SimpleNamespace(status="reserved"),),
    )
    composite.reason_code = "protection_phase_executing"
    events = []

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [composite],
        snapshot_loader=lambda *_args, **_kwargs: events.append("exchange-read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("close-reconcile"),
        executor=lambda *_args, **_kwargs: events.append("protection-executor"),
        processed_at=NOW,
    )

    assert events == ["exchange-read", "protection-executor"]


def test_break_even_post_write_restart_is_sent_to_state_aware_executor():
    batch = _batch(
        batch_id=11,
        strategy="deepcoin:100:10:BTC:short",
        status="executing",
        action="break_even_by_market",
        legs=(
            SimpleNamespace(status="submitted"),
            SimpleNamespace(status="planned"),
        ),
    )
    events = []

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [batch],
        snapshot_loader=lambda *_args, **_kwargs: (
            events.append("exchange-read") or object()
        ),
        reconciler=lambda *_args, **_kwargs: events.append("close-reconcile"),
        executor=lambda *_args, **_kwargs: events.append("market-executor"),
        processed_at=NOW,
    )

    assert events == ["exchange-read", "market-executor"]


def test_composite_protection_leg_state_survives_missing_reason_after_restart():
    composite = _batch(
        batch_id=10,
        strategy="deepcoin:100:10:BTC:short",
        status="executing",
        action="partial_then_break_even",
        legs=(SimpleNamespace(status="succeeded"),),
    )
    events = []

    run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **_kwargs: [composite],
        snapshot_loader=lambda *_args, **_kwargs: events.append("read") or object(),
        reconciler=lambda *_args, **_kwargs: events.append("close-reconcile"),
        executor=lambda *_args, **_kwargs: events.append("protection-executor"),
        processed_at=NOW,
    )

    assert events == ["read", "protection-executor"]


def test_ready_claim_with_deterministic_pre_submit_failure_is_persistently_blocked(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "blocked.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="z" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="y" * 64,
        target_snapshot_json="{}",
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_binding_not_active_or_exact")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.failed == 1
    assert stored.status == "blocked"
    assert stored.reason_code == "management_pre_submit_validation_failed"


def test_full_exit_deferred_set_validation_failure_uses_cancellation_recovery_path(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "deferred-recovery.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="d" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="ready",
        target_fingerprint="e" * 64,
        target_snapshot_json='{"identity":{"deferred_entry_leg_ids":[]}}',
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        restart_validator=lambda *_args, **_kwargs: None,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_entry_set_not_exact")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.failed == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "deferred_entry_cancel_preflight_failed"


def test_full_exit_restart_validator_deferred_set_failure_uses_cancellation_recovery_path(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "deferred-restart-recovery.db")
    row = StrategyManagementBatch(
        idempotency_fingerprint="f" * 64,
        raw_message_id=1,
        recognition_decision_id=1,
        recognition_generation="g1",
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=1,
        intent="full_take_profit",
        effective_action="full_exit",
        partial_round_before=0,
        status="executing",
        target_fingerprint="g" * 64,
        target_snapshot_json='{"identity":{"deferred_entry_leg_ids":[2]}}',
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with session_factory() as session:
        session.add(row)
        session.commit()
        batch_id = row.id

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        restart_validator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagementBatchExecutionError("batch_entry_set_not_exact")
        ),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe restart must stay frozen")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch_id)
    assert result.recovered == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "deferred_entry_cancel_preflight_failed"


def test_old_reconciling_backlog_does_not_starve_later_ready_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "fair.db")
    with session_factory() as session:
        for index in range(5):
            session.add(
                StrategyManagementBatch(
                    idempotency_fingerprint=f"recovery-{index}",
                    raw_message_id=index + 1,
                    recognition_decision_id=index + 1,
                    recognition_generation="g1",
                    target_lifecycle_id=index + 1,
                    strategy_instance_id=f"deepcoin:{index}:10:BTC:short",
                    execution_binding_id=index + 1,
                    intent="full_take_profit",
                    effective_action="full_exit",
                    partial_round_before=0,
                    status="reconciling",
                    target_fingerprint=f"target-{index}",
                    target_snapshot_json="{}",
                    planned_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        ready = StrategyManagementBatch(
            idempotency_fingerprint="later-ready",
            raw_message_id=99,
            recognition_decision_id=99,
            recognition_generation="g1",
            target_lifecycle_id=99,
            strategy_instance_id="deepcoin:999:10:BTC:short",
            execution_binding_id=99,
            intent="full_take_profit",
            effective_action="full_exit",
            partial_round_before=0,
            status="ready",
            target_fingerprint="later-target",
            target_snapshot_json="{}",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(ready)
        session.commit()
        ready_id = ready.id
    executed = []

    run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: object(),
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: None,
        executor=lambda *_args, **kwargs: executed.append(kwargs["batch_id"]),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=2,
        processed_at=NOW,
    )

    assert ready_id in executed


def test_max_one_cursor_gives_recovery_a_bounded_live_tick():
    cursor = StrategyManagementWorkerCursor()
    ready = _batch(batch_id=1, strategy="deepcoin:1:1:BTC:short", status="ready")
    recovery = _batch(
        batch_id=2,
        strategy="deepcoin:2:2:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submitted"),),
    )
    events = []

    def lister(*_args, **kwargs):
        return [recovery] if kwargs["prefer_recovery"] else [ready]

    common = dict(
        deepcoin_client_factory=lambda: object(),
        batch_lister=lister,
        claimer=lambda *_args, **_kwargs: True,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: events.append("recovery"),
        executor=lambda *_args, **_kwargs: events.append("ready"),
        restart_validator=lambda *_args, **_kwargs: None,
        max_batches=1,
        cursor=cursor,
        processed_at=NOW,
    )
    run_strategy_management_worker_tick(object(), **common)
    run_strategy_management_worker_tick(object(), **common)

    assert events == ["ready", "recovery"]


def test_disabled_shadow_max_one_prioritizes_recovery_and_never_claims_ready():
    cursor = StrategyManagementWorkerCursor()
    recovery = _batch(
        batch_id=2,
        strategy="deepcoin:2:2:BTC:short",
        status="reconciling",
        legs=(SimpleNamespace(status="submitted"),),
    )
    events = []

    result = run_strategy_management_worker_tick(
        object(),
        deepcoin_client_factory=lambda: object(),
        batch_lister=lambda *_args, **kwargs: (
            [recovery]
            if kwargs["prefer_recovery"]
            else (_ for _ in ()).throw(AssertionError("ready lane selected"))
        ),
        claimer=lambda *_args, **_kwargs: events.append("claim") or True,
        snapshot_loader=lambda *_args, **_kwargs: object(),
        reconciler=lambda *_args, **_kwargs: events.append("recovery"),
        executor=lambda *_args, **_kwargs: events.append("write"),
        max_batches=1,
        cursor=cursor,
        allow_execution=False,
        processed_at=NOW,
    )

    assert result.recovered == 1
    assert events == ["recovery"]


def _drive_strategy_loop(monkeypatch, *, tick_wrapper, ticks_wanted):
    """Run the real loop until ``ticks_wanted`` ticks have run, then cancel."""

    import asyncio

    from telegram_kol_research import strategy_management_worker as worker
    from telegram_kol_research.runtime_worker_executor import (
        shutdown_management_worker_executor,
    )

    real_tick = worker.run_strategy_management_worker_tick
    done = threading.Event()
    calls = {"count": 0}

    def patched(session_factory, **kwargs):
        calls["count"] += 1
        try:
            return tick_wrapper(real_tick, session_factory, **kwargs)
        finally:
            if calls["count"] >= ticks_wanted:
                done.set()

    monkeypatch.setattr(
        worker, "run_strategy_management_worker_tick", patched
    )
    monkeypatch.setattr(
        worker,
        "load_trading_settings",
        lambda _session_factory: SimpleNamespace(
            live_management_execution_enabled=True
        ),
    )

    async def scenario():
        task = asyncio.create_task(
            worker.run_strategy_management_worker_loop(
                session_factory=object(),
                deepcoin_client_factory=lambda: object(),
                interval_seconds=0.01,
                max_batches=1,
                now_provider=lambda: NOW,
            )
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(done.wait, 10.0), 15.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(scenario())
    finally:
        shutdown_management_worker_executor(wait=True)

    assert done.is_set()


def test_worker_loop_cursor_lane_alternation_survives_the_thread_offload():
    """The loop still owns one cursor, and the lanes still alternate."""

    monkeypatch = pytest.MonkeyPatch()
    lanes: list[str] = []
    cursors: list[int] = []

    def wrapper(real_tick, session_factory, **kwargs):
        cursor = kwargs["cursor"]
        lanes.append(cursor.next_lane)
        cursors.append(id(cursor))
        return real_tick(
            session_factory,
            batch_lister=lambda *_args, **_kwargs: [],
            **kwargs,
        )

    try:
        _drive_strategy_loop(monkeypatch, tick_wrapper=wrapper, ticks_wanted=4)
    finally:
        monkeypatch.undo()

    assert lanes[:4] == ["executable", "recovery", "executable", "recovery"]
    assert len(set(cursors)) == 1


def test_worker_loop_runs_the_tick_off_the_event_loop_thread():
    monkeypatch = pytest.MonkeyPatch()
    main_thread = threading.current_thread().name
    tick_threads: list[str] = []

    def wrapper(real_tick, session_factory, **kwargs):
        tick_threads.append(threading.current_thread().name)
        return real_tick(
            session_factory,
            batch_lister=lambda *_args, **_kwargs: [],
            **kwargs,
        )

    try:
        _drive_strategy_loop(monkeypatch, tick_wrapper=wrapper, ticks_wanted=2)
    finally:
        monkeypatch.undo()

    assert tick_threads
    assert all(name != main_thread for name in tick_threads)
    assert all(name.startswith("mgmt-worker") for name in tick_threads)


def test_worker_loop_keeps_running_after_a_tick_raises():
    monkeypatch = pytest.MonkeyPatch()
    attempts: list[int] = []

    def wrapper(real_tick, session_factory, **kwargs):
        attempts.append(len(attempts))
        raise RuntimeError("tick exploded")

    try:
        _drive_strategy_loop(monkeypatch, tick_wrapper=wrapper, ticks_wanted=3)
    finally:
        monkeypatch.undo()

    assert len(attempts) >= 3
