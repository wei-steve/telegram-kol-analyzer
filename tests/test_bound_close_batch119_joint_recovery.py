from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import importlib.util
from pathlib import Path

import pytest

from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageInstructionItem,
    PositionMutationIntent,
    PositionProtectionLeg,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)


_COMPOSITE_TEST_PATH = Path(__file__).with_name(
    "test_composite_management_batch_recovery.py"
)
_COMPOSITE_TEST_SPEC = importlib.util.spec_from_file_location(
    "_joint_composite_test_support", _COMPOSITE_TEST_PATH
)
assert _COMPOSITE_TEST_SPEC is not None
assert _COMPOSITE_TEST_SPEC.loader is not None
_COMPOSITE_TEST_SUPPORT = importlib.util.module_from_spec(_COMPOSITE_TEST_SPEC)
_COMPOSITE_TEST_SPEC.loader.exec_module(_COMPOSITE_TEST_SUPPORT)
NOW = _COMPOSITE_TEST_SUPPORT.NOW
_seed_batch_119_false_submission = (
    _COMPOSITE_TEST_SUPPORT._seed_batch_119_false_submission
)


def _seed_joint_incident(tmp_path):
    factory, database, _, _, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        for index in range(29):
            position_id = f"joint-sensitive-position-{index}"
            strategy_id = f"joint-sensitive-strategy-{index}"
            close_order_id = f"joint-sensitive-close-order-{index}"
            binding = ExecutionBinding(
                strategy_instance_id=strategy_id,
                kol_id="joint-recovery-source",
                chat_id=8000 + index,
                message_id=9000 + index,
                symbol=f"COIN{index}USDT",
                side="long",
                venue="deepcoin",
                pos_id=position_id,
                margin_mode="cross",
                position_mode="split",
                status="closed",
                payload_json='{"source":"joint-test"}',
                created_at=NOW - timedelta(minutes=30),
                updated_at=NOW - timedelta(minutes=30),
            )
            session.add(binding)
            session.flush()
            entry = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=strategy_id,
                leg_index=0,
                purpose="entry",
                order_kind="market",
                order_id=f"joint-sensitive-entry-order-{index}",
                client_order_id=f"joint-sensitive-entry-client-{index}",
                pos_id=position_id,
                venue="deepcoin",
                attribution_status="verified",
                status="manually_closed",
                attribution_evidence_json='{"proof":"local"}',
                terminal_reason="manual_position_missing",
                request_json="{}",
                response_json="{}",
                last_verified_at=NOW - timedelta(minutes=30),
                created_at=NOW - timedelta(minutes=30),
                updated_at=NOW - timedelta(minutes=30),
            )
            session.add(entry)
            session.flush()
            session.add_all(
                [
                    BoundPositionCloseReservation(
                        pos_id=position_id,
                        execution_binding_id=binding.id,
                        status="submitted",
                        last_error=None,
                        created_at=NOW - timedelta(minutes=30),
                        updated_at=NOW - timedelta(minutes=1),
                    ),
                    ExecutionEvent(
                        execution_binding_id=binding.id,
                        strategy_instance_id=strategy_id,
                        venue="deepcoin",
                        action="close_bound_position_market",
                        status="submitted",
                        symbol=f"COIN{index}USDT",
                        side="long",
                        order_id=close_order_id,
                        pos_id=position_id,
                        before_json='{"position_size":"1"}',
                        after_json='{"close_size":"1"}',
                        request_json='{"closePosId":"private"}',
                        response_json='{"code":"0"}',
                        created_at=NOW - timedelta(minutes=30),
                    ),
                    PositionMutationIntent(
                        idempotency_key=f"joint-mutation-{index}",
                        venue="deepcoin",
                        operation="close_position",
                        strategy_instance_id=strategy_id,
                        execution_binding_id=binding.id,
                        execution_order_leg_id=entry.id,
                        pos_id=position_id,
                        order_id=close_order_id,
                        authority_fingerprint="a" * 64,
                        request_fingerprint="b" * 64,
                        status="confirmed",
                        request_json="{}",
                        response_json='{"code":"0"}',
                        reserved_at=NOW - timedelta(minutes=30),
                        submitted_at=NOW - timedelta(minutes=30),
                        confirmed_at=NOW - timedelta(minutes=30),
                        created_at=NOW - timedelta(minutes=30),
                        updated_at=NOW - timedelta(minutes=30),
                    ),
                ]
            )
        session.commit()
    return factory, database


def _inspect(database, *, phase="joint_diagnostic"):
    from telegram_kol_research.bound_close_batch119_joint_recovery import (
        inspect_joint_recovery_material_authority,
    )

    return inspect_joint_recovery_material_authority(
        database,
        phase=phase,
        now=NOW,
    )


def test_exact_joint_incident_is_ready_and_public_projection_is_aggregate(tmp_path):
    _, database = _seed_joint_incident(tmp_path)

    result = _inspect(database)

    assert result.status == "ready"
    assert result.reason_code is None
    assert result.reservation_count == 29
    assert result.batch119_incident_count == 1
    assert result.blocking_writer_count == 0
    assert len(result.material_fingerprint) == 64
    assert set(asdict(result)) == {
        "batch119_incident_count",
        "blocking_writer_count",
        "material_fingerprint",
        "reason_code",
        "reservation_count",
        "status",
    }
    assert "sensitive" not in repr(result)


def test_only_batch_and_leg_retry_heartbeat_changes_are_normalized(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database)

    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=119
        ).one()
        batch.updated_at = NOW + timedelta(seconds=4)
        leg.updated_at = NOW + timedelta(seconds=4)
        session.commit()

    after = _inspect(database)
    assert after.status == "ready"
    assert after.material_fingerprint == before.material_fingerprint


@pytest.mark.parametrize("model", [StrategyManagementBatch, StrategyManagementLeg])
def test_retry_heartbeat_is_the_only_omitted_material_column(tmp_path, model):
    factory, _ = _seed_joint_incident(tmp_path)
    module = __import__(
        "telegram_kol_research.composite_management_batch_recovery",
        fromlist=["_batch119_material_row_payload"],
    )
    with factory() as session:
        row = (
            session.get(model, 119)
            if model is StrategyManagementBatch
            else session.query(model).filter_by(management_batch_id=119).one()
        )
        payload = module._batch119_material_row_payload(
            row, exclude_retry_heartbeat=True
        )

    assert set(payload) == {
        column.name for column in model.__table__.columns
    } - {"updated_at"}


@pytest.mark.parametrize(
    "model",
    [
        StrategyManagementComponent,
        ExecutionBinding,
        ExecutionEvent,
        ExecutionOrderLeg,
        MessageInstructionItem,
        PositionMutationIntent,
        PositionProtectionLedger,
        RawMessage,
        StrategyLifecycle,
    ],
)
def test_all_columns_of_every_other_local_authority_are_material(tmp_path, model):
    factory, _ = _seed_joint_incident(tmp_path)
    module = __import__(
        "telegram_kol_research.composite_management_batch_recovery",
        fromlist=["_batch119_material_row_payload"],
    )
    with factory() as session:
        row = session.query(model).first()
        assert row is not None
        payload = module._batch119_material_row_payload(row)

    assert set(payload) == {column.name for column in model.__table__.columns}


def test_batch_business_state_drift_refuses_without_raw_evidence(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        batch.reason_code = "different-business-reason"
        session.commit()

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_material_invalid"
    assert result.batch119_incident_count == 0
    assert "different-business-reason" not in repr(result)


def test_nonheartbeat_timestamp_changes_material_fingerprint(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        batch.created_at = NOW + timedelta(seconds=1)
        session.commit()

    after = _inspect(database)
    assert after.status == "ready"
    assert after.material_fingerprint != before.material_fingerprint


def test_complete_reservation_binding_row_changes_material_fingerprint(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database)
    with factory() as session:
        binding = session.query(ExecutionBinding).filter(
            ExecutionBinding.strategy_instance_id.like(
                "joint-sensitive-strategy-%"
            )
        ).first()
        binding.chat_id = int(binding.chat_id) + 123456
        session.commit()

    after = _inspect(database)
    assert after.status == "ready"
    assert after.material_fingerprint != before.material_fingerprint


def test_complete_reservation_event_row_changes_material_fingerprint(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database)
    with factory() as session:
        event = session.query(ExecutionEvent).filter_by(
            action="close_bound_position_market"
        ).first()
        event.reason = "material-only-change"
        session.commit()

    after = _inspect(database)
    assert after.status == "ready"
    assert after.material_fingerprint != before.material_fingerprint


def test_additional_fresh_writer_refuses(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        binding = session.query(ExecutionBinding).filter_by(
            strategy_instance_id="deepcoin:incident:btc:long"
        ).one()
        entry = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding.id
        ).one()
        session.add(
            PositionProtectionLeg(
                protection_leg_id="additional-writer-leg",
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry.id,
                role="take_profit",
                leg_index=99,
                pos_id="additional-writer-position",
                status="planned",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    result = _inspect(database)
    assert result.status == "refused"
    assert result.reason_code == "joint_writer_not_quiescent"
    assert result.blocking_writer_count == 1


def test_malformed_batch_json_refuses_closed(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        batch.target_snapshot_json = "{"
        session.commit()

    result = _inspect(database)
    assert result.status == "refused"
    assert result.reason_code == "joint_material_invalid"


def test_second_management_leg_refuses(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        original = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=119
        ).one()
        duplicate = StrategyManagementLeg(
            management_batch_id=119,
            execution_order_leg_id=original.execution_order_leg_id,
            leg_index=original.leg_index + 1,
            pos_id=original.pos_id + "-duplicate",
            preflight_size=original.preflight_size,
            planned_close_size=original.planned_close_size,
            avg_entry_price=original.avg_entry_price,
            quantity_step=original.quantity_step,
            status=original.status,
            last_exchange_snapshot_json=original.last_exchange_snapshot_json,
            last_error=original.last_error,
            created_at=original.created_at,
            updated_at=original.updated_at,
        )
        session.add(duplicate)
        session.commit()

    assert _inspect(database).status == "refused"


@pytest.mark.parametrize(
    "status",
    ["confirmed", "future_state"],
)
def test_preapply_reservation_status_drift_refuses(tmp_path, status):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        row = session.query(BoundPositionCloseReservation).first()
        row.status = status
        session.commit()

    assert _inspect(database).status == "refused"


@pytest.mark.parametrize("remove_count", [1, 2])
def test_reservation_population_must_be_exactly_29(tmp_path, remove_count):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        rows = session.query(BoundPositionCloseReservation).limit(remove_count).all()
        for row in rows:
            session.delete(row)
        session.commit()

    assert _inspect(database).status == "refused"


def test_reservation_population_overflow_refuses(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    binding_id = None
    with factory() as session:
        binding_id = session.query(ExecutionBinding.id).first()[0]
        session.add_all(
            BoundPositionCloseReservation(
                pos_id=f"overflow-sensitive-position-{index}",
                execution_binding_id=binding_id,
                status="submitted",
                created_at=NOW,
                updated_at=NOW,
            )
            for index in range(36)
        )
        session.commit()

    assert _inspect(database).status == "refused"


def test_missing_required_source_schema_refuses_without_bootstrap(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        session.execute(__import__("sqlalchemy").text("DROP TABLE execution_events"))
        session.commit()

    result = _inspect(database)
    assert result.status == "refused"
    assert result.reason_code == "joint_material_invalid"
    with factory() as session:
        tables = {
            row[0]
            for row in session.execute(
                __import__("sqlalchemy").text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
        }
    assert "execution_events" not in tables


def test_missing_reservation_descendant_refuses(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        event = session.query(ExecutionEvent).filter_by(
            action="close_bound_position_market"
        ).first()
        session.delete(event)
        session.commit()

    result = _inspect(database)
    assert result.status == "refused"
    assert result.reservation_count == 0


def test_reservation_nonheartbeat_timestamp_changes_material_fingerprint(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database)
    with factory() as session:
        row = session.query(BoundPositionCloseReservation).first()
        row.updated_at = NOW + timedelta(seconds=1)
        session.commit()

    after = _inspect(database)
    assert after.status == "ready"
    assert after.material_fingerprint != before.material_fingerprint


def test_closed_phase_requires_confirmed_reservations_after_bound_apply(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)

    assert _inspect(database, phase="bound_apply_post").status == "refused"
    with factory() as session:
        session.query(BoundPositionCloseReservation).update(
            {BoundPositionCloseReservation.status: "confirmed"}
        )
        session.commit()

    result = _inspect(database, phase="bound_apply_post")
    assert result.status == "ready"
    assert result.reservation_count == 29


def test_unknown_phase_is_rejected_before_database_access(tmp_path):
    from telegram_kol_research.bound_close_batch119_joint_recovery import (
        inspect_joint_recovery_material_authority,
    )

    result = inspect_joint_recovery_material_authority(
        tmp_path / "missing.db",
        phase="future_phase",
        now=NOW,
    )
    assert result.status == "refused"
    assert result.reason_code == "joint_phase_invalid"
