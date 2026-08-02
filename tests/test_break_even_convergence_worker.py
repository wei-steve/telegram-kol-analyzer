from datetime import UTC, datetime, timedelta

from telegram_kol_research.break_even_convergence_worker import (
    run_break_even_convergence_worker_tick,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _seed(session_factory, *, mode="live", status="planned"):
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="strategy-1",
            kol_id="group:1",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
            pos_id="pos-1",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="pos-1",
            pos_id="pos-1",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        session.add(entry)
        session.flush()
        convergence = StrategyBreakEvenConvergence(
            strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id,
            target_lifecycle_id=lifecycle.id,
            trigger_type="tp1_fill",
            trigger_identity=f"tp-{mode}-{status}",
            trigger_evidence_json='{"order_id":"tp-1"}',
            target_snapshot_json="{}",
            execution_mode=mode,
            status=status,
            planned_at=NOW,
            updated_at=NOW,
        )
        session.add(convergence)
        session.flush()
        session.add(StrategyBreakEvenConvergenceLeg(
            convergence_id=convergence.id,
            execution_order_leg_id=entry.id,
            pos_id="pos-1",
            preflight_size="5",
            avg_entry_price="63000",
            old_protection_json="[]",
            decision_json="{}",
            status="planned",
            updated_at=NOW,
        ))
        session.commit()
        return int(convergence.id)


def test_shadow_tick_executes_plan_with_read_client(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    convergence_id = _seed(sf, mode="shadow")
    calls = []

    result = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: object(),
        executor=lambda _sf, **kwargs: calls.append(kwargs) or type(
            "Result", (), {"status": "shadow_planned", "reason_code": None}
        )(),
        processed_at=NOW,
    )

    assert result.shadowed == 1
    assert calls[0]["convergence_id"] == convergence_id


def test_live_tick_claims_and_executes_each_task_once(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    convergence_id = _seed(sf)
    calls = []

    def executor(_sf, **kwargs):
        calls.append(kwargs)
        with sf() as session:
            row = session.get(StrategyBreakEvenConvergence, convergence_id)
            row.status = "completed"
            session.commit()
        return type("Result", (), {"status": "completed", "reason_code": None})()

    first = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: object(),
        executor=executor,
        processed_at=NOW,
    )
    second = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: object(),
        executor=executor,
        processed_at=NOW,
    )

    assert first.executed == 1
    assert second.discovered == 0
    assert len(calls) == 1


def test_recovery_task_only_enqueues_one_alert_and_never_calls_exchange(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    convergence_id = _seed(sf, status="recovery_required")

    first = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("recovery alert must not access exchange")
        ),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery task must not execute")
        ),
        processed_at=NOW,
    )
    second = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: object(),
        processed_at=NOW,
    )

    assert first.alerted == 1
    assert second.alerted == 0
    with sf() as session:
        incident = session.query(PositionProtectionIncident).one()
        assert incident.incident_type == "automatic_break_even_recovery_required"
        assert str(convergence_id) in incident.evidence_json


def test_disabled_task_is_blocked_without_exchange_access(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    convergence_id = _seed(sf, mode="disabled")

    result = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("disabled task must not access exchange")
        ),
        processed_at=NOW,
    )

    assert result.skipped == 1
    with sf() as session:
        row = session.get(StrategyBreakEvenConvergence, convergence_id)
        assert row.status == "blocked"
        assert row.reason_code == "automatic_break_even_disabled"


def test_stale_intermediate_phase_is_reclaimed_after_restart(tmp_path):
    sf = create_session_factory(tmp_path / "research.db")
    convergence_id = _seed(sf, status="preflight_verified")
    calls = []

    result = run_break_even_convergence_worker_tick(
        sf,
        deepcoin_client_factory=lambda: object(),
        executor=lambda _sf, **kwargs: calls.append(kwargs) or type(
            "Result", (), {"status": "completed", "reason_code": None}
        )(),
        processed_at=NOW + timedelta(seconds=121),
        lease_seconds=120,
    )

    assert result.executed == 1
    assert calls[0]["convergence_id"] == convergence_id
