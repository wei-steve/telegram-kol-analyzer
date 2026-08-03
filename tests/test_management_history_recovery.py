from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    create_management_batch,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _snapshot(**overrides):
    values = {
        "positions": [],
        "open_orders": [],
        "pending_trigger_orders": [],
        "order_history": [],
        "trade_fills": [],
        "trigger_history": [],
        "pending_tpsl_observations": [{"complete": True}],
        "errors": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _seed_batch(tmp_path, *, batch_status="recovery_required", leg_status="planned"):
    session_factory = create_session_factory(tmp_path / "history.db")
    strategy_id = "deepcoin:100:10:BTC:short"
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=20, text="exit", posted_at=NOW)
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        binding = ExecutionBinding(
            strategy_instance_id=strategy_id,
            kol_id="kol",
            chat_id=100,
            message_id=10,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        )
        session.add_all([decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=strategy_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="entry-1",
            pos_id="pos-1",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(entry)
        session.commit()
        ids = raw.id, decision.id, lifecycle.id, binding.id, entry.id

    batch = create_management_batch(
        session_factory,
        idempotency_fingerprint="h" * 64,
        raw_message_id=ids[0],
        recognition_decision_id=ids[1],
        recognition_generation="generation-history",
        target_lifecycle_id=ids[2],
        strategy_instance_id=strategy_id,
        execution_binding_id=ids[3],
        intent="full_exit",
        effective_action="full_exit",
        requested_fraction=1.0,
        effective_fraction=1.0,
        partial_round_before=0,
        target_fingerprint="t" * 64,
        target_snapshot={"identity": {"execution_binding_id": ids[3]}},
        legs=[
            ManagementLegCreate(
                execution_order_leg_id=ids[4],
                pos_id="pos-1",
                leg_index=0,
                preflight_size="1",
                planned_close_size="1",
                client_order_id=("TMCLIENT1" if leg_status == "submitted" else None),
                exchange_order_id=("close-1" if leg_status == "submitted" else None),
                status=leg_status,
            )
        ],
        planned_at=NOW,
        status=batch_status,
        reason_code=(
            "management_reconciliation_identity_mismatch"
            if leg_status == "submitted"
            else "close_final_preflight_failed"
        ),
    )
    return session_factory, batch.id


def test_planned_batch_with_complete_zero_submission_evidence_can_resolve(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path)

    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert decision.status == "ready"
    assert decision.decision == "terminal_no_submission"
    assert len(decision.evidence_fingerprint) == 64
    assert decision.evidence["batch_id"] == batch_id
    assert "strategy_instance_id" not in decision.evidence


def test_evidence_fingerprint_is_stable_across_operator_invocations(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path)

    first = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW,
    )
    second = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW + timedelta(minutes=5),
    )

    assert first.evidence_fingerprint == second.evidence_fingerprint


def test_incomplete_snapshot_refuses_without_guessing(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path)

    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(errors={"order_history": "timeout"}),
        planned_at=NOW,
    )

    assert decision.status == "refused"
    assert decision.reason_code == "exchange_snapshot_incomplete"


def test_exact_filled_order_and_absent_position_can_resolve(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path, leg_status="submitted")
    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(
            order_history=[
                {
                    "ordId": "close-1",
                    "clOrdId": "TMCLIENT1",
                    "posId": "pos-1",
                    "state": "filled",
                }
            ]
        ),
        planned_at=NOW,
    )

    assert decision.status == "ready"
    assert decision.decision == "terminal_exchange_confirmed"


def test_position_absence_without_exact_order_evidence_refuses(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path, leg_status="submitted")
    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert decision.status == "refused"
    assert decision.reason_code == "exact_terminal_order_evidence_missing"


def test_partial_failed_restoration_can_resolve_when_exact_position_is_absent(
    tmp_path,
):
    from telegram_kol_research.management_history_recovery import (
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(
        tmp_path,
        batch_status="partial_failed",
        leg_status="restored",
    )
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.reason_code = "protection_replacement_failed_and_restored"
        session.commit()

    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(positions=[]),
        planned_at=NOW,
    )

    assert decision.status == "ready"
    assert decision.decision == "terminal_position_absent"


def test_apply_is_fingerprint_guarded_and_idempotent(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        apply_management_history_recovery,
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path)
    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    first = apply_management_history_recovery(
        session_factory,
        decision=decision,
        expected_fingerprint=decision.evidence_fingerprint,
        applied_at=NOW + timedelta(seconds=1),
    )
    second = apply_management_history_recovery(
        session_factory,
        decision=decision,
        expected_fingerprint=decision.evidence_fingerprint,
        applied_at=NOW + timedelta(seconds=2),
    )

    assert first.status == "resolved"
    assert second.status == "already_resolved"
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch_id
        ).one()
        assert batch.status == "resolved"
        assert batch.reason_code == "history_no_submission_confirmed"
        assert leg.status == "failed"
        assert (
            session.query(ExecutionEvent)
            .filter_by(action="management_history_recovery")
            .count()
            == 1
        )


def test_apply_rejects_changed_source_row(tmp_path):
    from telegram_kol_research.management_history_recovery import (
        ManagementHistoryRecoveryConflict,
        apply_management_history_recovery,
        plan_management_history_recovery,
    )

    session_factory, batch_id = _seed_batch(tmp_path)
    decision = plan_management_history_recovery(
        session_factory,
        batch_id=batch_id,
        snapshot=_snapshot(),
        planned_at=NOW,
    )
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.reason_code = "changed_after_plan"
        batch.updated_at = NOW + timedelta(seconds=1)
        session.commit()

    with pytest.raises(ManagementHistoryRecoveryConflict):
        apply_management_history_recovery(
            session_factory,
            decision=decision,
            expected_fingerprint=decision.evidence_fingerprint,
            applied_at=NOW + timedelta(seconds=2),
        )


def test_recover_management_history_cli_defaults_to_redacted_dry_run(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    session_factory, batch_id = _seed_batch(tmp_path)
    database_path = session_factory.kw["bind"].url.database
    monkeypatch.setattr(cli_module, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli_module,
        "load_deepcoin_execution_reconciliation_snapshot_read_only",
        lambda session_factory, *, client: _snapshot(),
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "recover-management-history",
            "--database-path",
            str(database_path),
            "--batch-id",
            str(batch_id),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = __import__("json").loads(result.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["decision"]["status"] == "ready"
    assert "pos-1" not in result.stdout


def test_recover_management_history_cli_apply_requires_fingerprint(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    session_factory, batch_id = _seed_batch(tmp_path)
    database_path = session_factory.kw["bind"].url.database
    monkeypatch.setattr(cli_module, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli_module,
        "load_deepcoin_execution_reconciliation_snapshot_read_only",
        lambda session_factory, *, client: _snapshot(),
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "recover-management-history",
            "--database-path",
            str(database_path),
            "--batch-id",
            str(batch_id),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    with session_factory() as session:
        assert session.get(StrategyManagementBatch, batch_id).status == "recovery_required"


def test_read_only_reconciliation_snapshot_loader_does_not_persist_observations(
    tmp_path, monkeypatch
):
    import telegram_kol_research.execution_bindings as bindings_module
    from telegram_kol_research.execution_bindings import (
        load_deepcoin_execution_reconciliation_snapshot_read_only,
    )
    from telegram_kol_research.models import PositionReconciliationObservation

    session_factory, _ = _seed_batch(tmp_path)
    expected = _snapshot(
        pending_tpsl_observations=[
            {"complete": True, "instrument_id": "BTC-USDT-SWAP"}
        ]
    )
    monkeypatch.setattr(
        bindings_module,
        "_load_reconcile_snapshot",
        lambda client, *, instruments: expected,
    )

    result = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory,
        client=object(),
    )

    assert result is expected
    with session_factory() as session:
        assert session.query(PositionReconciliationObservation).count() == 0
