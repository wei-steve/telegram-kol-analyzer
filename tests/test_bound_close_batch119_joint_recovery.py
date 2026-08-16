from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    DeepcoinExecutionOperation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageInstructionItem,
    PositionMutationIntent,
    PositionProtectionLeg,
    PositionProtectionLedger,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    TradeSignal,
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
_RECOVERY_TEST_PATH = Path(__file__).with_name(
    "test_bound_close_reservation_recovery.py"
)
_RECOVERY_TEST_SPEC = importlib.util.spec_from_file_location(
    "_joint_bound_close_test_support", _RECOVERY_TEST_PATH
)
assert _RECOVERY_TEST_SPEC is not None
assert _RECOVERY_TEST_SPEC.loader is not None
_RECOVERY_TEST_SUPPORT = importlib.util.module_from_spec(_RECOVERY_TEST_SPEC)
_RECOVERY_TEST_SPEC.loader.exec_module(_RECOVERY_TEST_SUPPORT)


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


def _add_confirmed_reservation_residue(factory, *, count=9):
    with factory() as session:
        binding_id = session.query(ExecutionBinding.id).order_by(
            ExecutionBinding.id
        ).first()[0]
        rows = [
            BoundPositionCloseReservation(
                pos_id=f"joint-confirmed-residue-{index}",
                execution_binding_id=binding_id,
                status="confirmed",
                last_error=None,
                created_at=NOW - timedelta(hours=2, minutes=index),
                updated_at=NOW - timedelta(hours=1, minutes=index),
            )
            for index in range(count)
        ]
        session.add_all(rows)
        session.commit()
        return tuple(row.id for row in rows)


def _add_historical_nonterminal_management_residue(factory):
    with factory() as session:
        target = session.get(StrategyManagementBatch, 119)
        target_leg = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=119)
            .one()
        )
        batches = []
        for index in range(3):
            batch = StrategyManagementBatch(
                idempotency_fingerprint=f"joint-historical-{index}",
                raw_message_id=target.raw_message_id,
                recognition_decision_id=target.recognition_decision_id,
                recognition_generation=f"joint-historical-{index}",
                target_lifecycle_id=target.target_lifecycle_id,
                strategy_instance_id=f"joint-historical-strategy-{index}",
                execution_binding_id=target.execution_binding_id,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                status="executing",
                target_fingerprint=f"joint-historical-target-{index}",
                target_snapshot_json="{}",
                planned_at=NOW - timedelta(minutes=30),
                created_at=NOW - timedelta(minutes=30),
                updated_at=NOW - timedelta(minutes=30),
            )
            session.add(batch)
            session.flush()
            batches.append(batch)
        components = []
        component_kinds = (
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        )
        for index in range(8):
            component = StrategyManagementComponent(
                management_batch_id=batches[index % len(batches)].id,
                strategy_management_leg_id=target_leg.id,
                strategy_management_leg_scope=target_leg.id,
                component_kind=component_kinds[index // len(batches)],
                sequence=100 + index,
                status="pending",
                idempotency_key=f"joint-historical-component-{index}",
                desired_json="{}",
                evidence_json="[]",
                created_at=NOW - timedelta(minutes=30),
                updated_at=NOW - timedelta(minutes=30),
            )
            session.add(component)
            components.append(component)
        session.commit()
        return batches[0].id, components[0].id


def _add_historical_unclassified_instruction_residue(factory):
    with factory() as session:
        items = []
        for index, status in enumerate(("submitted", "unknown", "unknown")):
            raw = RawMessage(
                chat_id=7000 + index,
                message_id=7100 + index,
                posted_at=NOW - timedelta(minutes=30),
                text="historical management residue",
                created_at=NOW - timedelta(minutes=30),
            )
            session.add(raw)
            session.flush()
            candidate = SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="position_update",
                target_lifecycle_id=None,
                management_action="partial_then_break_even",
                review_status="confirmed",
                created_at=NOW - timedelta(minutes=30),
            )
            session.add(candidate)
            session.flush()
            item = MessageInstructionItem(
                    raw_message_id=raw.id,
                    signal_candidate_id=candidate.id,
                    sequence=0,
                    instruction_kind="management",
                    strategy_instance_id=f"historical-instruction-{index}",
                    idempotency_key=f"historical-instruction-{index}",
                    status=status,
                    created_at=NOW - timedelta(minutes=30),
                    updated_at=NOW - timedelta(minutes=30),
            )
            session.add(item)
            items.append(item)
        session.commit()
        return items[1].id


def _set_test_state_column_nullable_and_null(
    database,
    *,
    table,
    row_id,
):
    with sqlite3.connect(database) as connection:
        schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute("PRAGMA writable_schema=ON")
        status_width = 64 if table == "bound_position_close_reservations" else 32
        connection.execute(
            "UPDATE sqlite_master SET sql=replace(sql, "
            f"'status VARCHAR({status_width}) NOT NULL', "
            f"'status VARCHAR({status_width})') "
            "WHERE type='table' AND name=?",
            (table,),
        )
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {table} SET status=NULL WHERE id=?", (row_id,))
        connection.commit()


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
        "batch119_material_fingerprint",
        "blocking_writer_count",
        "material_fingerprint",
        "reason_code",
        "reservation_count",
        "status",
    }
    assert "sensitive" not in repr(result)


def test_joint_admission_accepts_nine_confirmed_residue_rows(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    _add_confirmed_reservation_residue(factory)

    result = _inspect(database)

    assert result.status == "ready"
    assert result.reason_code is None
    assert result.reservation_count == 29
    assert result.batch119_incident_count == 1
    assert result.blocking_writer_count == 0


def test_confirmed_residue_field_drift_changes_joint_fingerprint(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    residue_ids = _add_confirmed_reservation_residue(factory)
    before = _inspect(database)
    with factory() as session:
        residue = session.get(BoundPositionCloseReservation, residue_ids[0])
        residue.updated_at = residue.updated_at + timedelta(seconds=1)
        session.commit()

    after = _inspect(database)

    assert before.status == "ready"
    assert after.status == "ready"
    assert after.material_fingerprint != before.material_fingerprint


@pytest.mark.parametrize("status", [None, "future_state", "submitted", "reserved"])
def test_joint_admission_refuses_nonconfirmed_extra_residue(tmp_path, status):
    factory, database = _seed_joint_incident(tmp_path)
    residue_ids = _add_confirmed_reservation_residue(factory, count=1)
    if status is None:
        _set_test_state_column_nullable_and_null(
            database,
            table="bound_position_close_reservations",
            row_id=residue_ids[0],
        )
    else:
        with factory() as session:
            residue = session.get(BoundPositionCloseReservation, residue_ids[0])
            residue.status = status
            session.commit()

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_material_invalid"


def test_joint_admission_accepts_known_historical_nonterminal_management_residue(
    tmp_path,
):
    factory, database = _seed_joint_incident(tmp_path)
    _add_historical_nonterminal_management_residue(factory)

    result = _inspect(database)

    assert result.status == "ready"
    assert result.reason_code is None
    assert result.reservation_count == 29
    assert result.batch119_incident_count == 1
    assert result.blocking_writer_count == 0


def test_joint_admission_accepts_historical_unclassified_instruction_residue(
    tmp_path,
):
    factory, database = _seed_joint_incident(tmp_path)
    _add_historical_unclassified_instruction_residue(factory)

    result = _inspect(database)

    assert result.status == "ready"
    assert result.reason_code is None
    assert result.blocking_writer_count == 0


def test_joint_admission_accepts_combined_production_historical_residue_shape(
    tmp_path,
):
    factory, database = _seed_joint_incident(tmp_path)
    _add_historical_nonterminal_management_residue(factory)
    _add_historical_unclassified_instruction_residue(factory)

    result = _inspect(database)

    assert result.status == "ready"
    assert result.reason_code is None
    assert result.blocking_writer_count == 0
    assert result.reservation_count == 29
    assert result.batch119_incident_count == 1


@pytest.mark.parametrize("drift_kind", ["fresh", "unknown"])
def test_joint_admission_still_refuses_fresh_or_unknown_instruction_residue(
    tmp_path,
    drift_kind,
):
    factory, database = _seed_joint_incident(tmp_path)
    item_id = _add_historical_unclassified_instruction_residue(factory)
    with factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        if drift_kind == "fresh":
            item.updated_at = NOW
        else:
            item.status = "future_unknown_state"
        session.commit()

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_writer_not_quiescent"
    assert result.blocking_writer_count >= 1


@pytest.mark.parametrize("residue_kind", ["management", "instruction"])
def test_joint_admission_refuses_null_residue_state(tmp_path, residue_kind):
    factory, database = _seed_joint_incident(tmp_path)
    if residue_kind == "management":
        row_id, _ = _add_historical_nonterminal_management_residue(factory)
        table = "strategy_management_batches"
    else:
        row_id = _add_historical_unclassified_instruction_residue(factory)
        table = "message_instruction_items"
    _set_test_state_column_nullable_and_null(
        database,
        table=table,
        row_id=row_id,
    )

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_writer_not_quiescent"
    assert result.blocking_writer_count >= 1


def test_joint_admission_refuses_historical_deepcoin_nonterminal_operation(
    tmp_path,
):
    factory, database = _seed_joint_incident(tmp_path)
    with factory() as session:
        signal = TradeSignal(
            signal_uid="joint-historical-deepcoin-signal",
            source_type="recovery",
            venue="deepcoin",
            kol_id="joint-recovery",
            chat_id=1,
            message_id=1,
            symbol="BTCUSDT",
            side="long",
            action="open_position",
            status="confirmed",
            payload_json="{}",
            created_at=NOW - timedelta(minutes=30),
            updated_at=NOW - timedelta(minutes=30),
        )
        session.add(signal)
        session.flush()
        session.add(
            DeepcoinExecutionOperation(
                operation_key="joint-historical-deepcoin-operation",
                trade_signal_id=signal.id,
                contract_version="1",
                phase="entry_readback",
                state="entry_confirmed",
                outcome_certainty="confirmed",
                request_fingerprint="d" * 64,
                economics_fingerprint="e" * 64,
                deadline_at=NOW - timedelta(minutes=29),
                attempt_count=0,
                state_version=0,
                evidence_json="{}",
                created_at=NOW - timedelta(minutes=30),
                updated_at=NOW - timedelta(minutes=30),
            )
        )
        session.commit()

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_writer_not_quiescent"
    assert result.blocking_writer_count >= 1


@pytest.mark.parametrize("residue_kind", ["batch", "component"])
@pytest.mark.parametrize("drift_kind", ["fresh", "unknown"])
def test_joint_admission_still_refuses_fresh_or_unknown_management_residue(
    tmp_path,
    residue_kind,
    drift_kind,
):
    factory, database = _seed_joint_incident(tmp_path)
    batch_id, component_id = _add_historical_nonterminal_management_residue(
        factory
    )
    model = (
        StrategyManagementBatch
        if residue_kind == "batch"
        else StrategyManagementComponent
    )
    row_id = batch_id if residue_kind == "batch" else component_id
    with factory() as session:
        row = session.get(model, row_id)
        if drift_kind == "fresh":
            row.updated_at = NOW
        else:
            row.status = "future_unknown_state"
        session.commit()

    result = _inspect(database)

    assert result.status == "refused"
    assert result.reason_code == "joint_writer_not_quiescent"
    assert result.blocking_writer_count >= 1


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


def test_closed_phase_refuses_manual_confirmation_without_apply_audit(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)

    before = _inspect(database, phase="bound_apply_pre")

    assert _inspect(database, phase="bound_apply_post").status == "refused"
    with factory() as session:
        session.query(BoundPositionCloseReservation).update(
            {BoundPositionCloseReservation.status: "confirmed"}
        )
        session.commit()

    result = _inspect(database, phase="bound_apply_post")
    assert before.status == "ready"
    assert result.status == "refused"
    assert result.reason_code == "joint_material_invalid"


def test_joint_pre_is_the_only_admission_that_can_bypass_exact_batch119_block(tmp_path):
    _, database = _seed_joint_incident(tmp_path)

    ordinary = __import__(
        "telegram_kol_research.bound_close_writer_quiescence",
        fromlist=["inspect_bound_close_writer_quiescence"],
    ).inspect_bound_close_writer_quiescence(database, now=NOW)
    joint = _inspect(database, phase="bound_apply_pre")

    assert ordinary["status"] == "refused"
    assert ordinary["blocking_writer_count"] > 0
    assert joint.status == "ready"
    assert joint.batch119_incident_count == 1


def test_real_bound_apply_changes_only_reservations_and_preserves_batch119(
    tmp_path,
    monkeypatch,
):
    _, database = _seed_joint_incident(tmp_path)
    before = _inspect(database, phase="bound_apply_pre")
    monkeypatch.setattr(
        _RECOVERY_TEST_SUPPORT.recovery_module,
        "_recovery_capture_now",
        lambda: _RECOVERY_TEST_SUPPORT.APPLY_TIME - timedelta(minutes=1),
    )
    source = _RECOVERY_TEST_SUPPORT.load_bound_close_reservation_source(database)
    http_client = _RECOVERY_TEST_SUPPORT._RecoveryReaderHttpClient(
        responses=_RECOVERY_TEST_SUPPORT._terminal_provider_responses(source)
    )
    reader, _transport, _selected = (
        _RECOVERY_TEST_SUPPORT._factory_recovery_reader(
            monkeypatch, http_client=http_client
        )
    )
    capture = (
        _RECOVERY_TEST_SUPPORT.capture_and_seal_bound_close_reservation_recovery(
            source,
            reader,
            deadline_monotonic=__import__("time").monotonic() + 30.0,
        )
    )
    assert capture.plan.status == "ready"
    request_count = len(http_client.requests)

    applied = _RECOVERY_TEST_SUPPORT._apply_ready(database, capture)
    after = _inspect(database, phase="bound_apply_post")

    assert applied.status in {"applied", "applied_after_deadline_verified"}
    assert applied.action_count == 29
    assert after.status == "ready"
    assert after.reservation_count == 29
    assert after.material_fingerprint != before.material_fingerprint
    assert (
        after.batch119_material_fingerprint
        == before.batch119_material_fingerprint
    )
    assert len(http_client.requests) == request_count


def test_real_bound_apply_preserves_confirmed_residue_and_post_authority(
    tmp_path,
    monkeypatch,
):
    factory, database = _seed_joint_incident(tmp_path)
    residue_ids = _add_confirmed_reservation_residue(factory)
    with factory() as session:
        residue_before = tuple(
            (
                row.id,
                row.pos_id,
                row.execution_binding_id,
                row.status,
                row.last_error,
                row.created_at,
                row.updated_at,
            )
            for row in session.query(BoundPositionCloseReservation)
            .filter(BoundPositionCloseReservation.id.in_(residue_ids))
            .order_by(BoundPositionCloseReservation.id)
        )
    before = _inspect(database, phase="bound_apply_pre")
    monkeypatch.setattr(
        _RECOVERY_TEST_SUPPORT.recovery_module,
        "_recovery_capture_now",
        lambda: _RECOVERY_TEST_SUPPORT.APPLY_TIME - timedelta(minutes=1),
    )
    source = _RECOVERY_TEST_SUPPORT.load_bound_close_reservation_source(database)
    http_client = _RECOVERY_TEST_SUPPORT._RecoveryReaderHttpClient(
        responses=_RECOVERY_TEST_SUPPORT._terminal_provider_responses(source)
    )
    reader, _transport, _selected = (
        _RECOVERY_TEST_SUPPORT._factory_recovery_reader(
            monkeypatch, http_client=http_client
        )
    )
    capture = (
        _RECOVERY_TEST_SUPPORT.capture_and_seal_bound_close_reservation_recovery(
            source,
            reader,
            deadline_monotonic=__import__("time").monotonic() + 30.0,
        )
    )

    applied = _RECOVERY_TEST_SUPPORT._apply_ready(database, capture)
    after = _inspect(database, phase="bound_apply_post")
    with factory() as session:
        residue_after = tuple(
            (
                row.id,
                row.pos_id,
                row.execution_binding_id,
                row.status,
                row.last_error,
                row.created_at,
                row.updated_at,
            )
            for row in session.query(BoundPositionCloseReservation)
            .filter(BoundPositionCloseReservation.id.in_(residue_ids))
            .order_by(BoundPositionCloseReservation.id)
        )

    assert before.status == "ready"
    assert capture.plan.action_count == 29
    assert applied.action_count == 29
    assert residue_after == residue_before
    assert after.status == "ready"
    assert after.reservation_count == 29


@pytest.mark.parametrize("drift", ["binding_timestamp", "missing_close_event"])
def test_bound_apply_post_refuses_descendant_drift_from_closed_audit(
    tmp_path,
    monkeypatch,
    drift,
):
    factory, database = _seed_joint_incident(tmp_path)
    monkeypatch.setattr(
        _RECOVERY_TEST_SUPPORT.recovery_module,
        "_recovery_capture_now",
        lambda: _RECOVERY_TEST_SUPPORT.APPLY_TIME - timedelta(minutes=1),
    )
    source = _RECOVERY_TEST_SUPPORT.load_bound_close_reservation_source(database)
    http_client = _RECOVERY_TEST_SUPPORT._RecoveryReaderHttpClient(
        responses=_RECOVERY_TEST_SUPPORT._terminal_provider_responses(source)
    )
    reader, _transport, _selected = (
        _RECOVERY_TEST_SUPPORT._factory_recovery_reader(
            monkeypatch, http_client=http_client
        )
    )
    capture = (
        _RECOVERY_TEST_SUPPORT.capture_and_seal_bound_close_reservation_recovery(
            source,
            reader,
            deadline_monotonic=__import__("time").monotonic() + 30.0,
        )
    )
    applied = _RECOVERY_TEST_SUPPORT._apply_ready(database, capture)
    assert applied.status in {"applied", "applied_after_deadline_verified"}
    with factory() as session:
        target = (
            session.query(BoundPositionCloseReservation)
            .filter_by(status="confirmed")
            .order_by(BoundPositionCloseReservation.id)
            .first()
        )
        assert target is not None
        if drift == "binding_timestamp":
            binding = session.get(ExecutionBinding, target.execution_binding_id)
            binding.updated_at = binding.updated_at + timedelta(seconds=1)
        else:
            event = (
                session.query(ExecutionEvent)
                .filter_by(
                    action="close_bound_position_market",
                    pos_id=target.pos_id,
                )
                .one()
            )
            session.delete(event)
        session.commit()

    after = _inspect(database, phase="bound_apply_post")

    assert after.status == "refused"
    assert after.reason_code == "joint_material_invalid"


def test_batch119_material_fingerprint_changes_only_with_batch119_authority(tmp_path):
    factory, database = _seed_joint_incident(tmp_path)
    before = _inspect(database, phase="bound_apply_pre")
    with factory() as session:
        batch = session.query(StrategyManagementBatch).filter_by(id=119).one()
        batch.created_at = NOW + timedelta(seconds=1)
        session.commit()

    after = _inspect(database, phase="bound_apply_pre")

    assert after.status == "ready"
    assert (
        after.batch119_material_fingerprint
        != before.batch119_material_fingerprint
    )


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


def _admission_document(
    *,
    capture_id: str,
    started: str,
    completed: str,
    phase: str = "joint_diagnostic",
    material_fingerprint: str = "a" * 64,
    batch119_material_fingerprint: str = "b" * 64,
):
    return {
        "batch119_incident_count": 1,
        "batch119_material_fingerprint": batch119_material_fingerprint,
        "blocking_writer_count": 0,
        "capture_completed_at": completed,
        "capture_id": capture_id,
        "capture_started_at": started,
        "material_fingerprint": material_fingerprint,
        "phase": phase,
        "reason_code": None,
        "reservation_count": 29,
        "schema_version": 1,
        "status": "ready",
    }


def _write_private_json(path, payload):
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_joint_admission_comparator_accepts_two_strict_stable_captures(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_private_json(
        first,
        _admission_document(
            capture_id="1" * 64,
            started="2026-08-16T10:00:00+00:00",
            completed="2026-08-16T10:00:01+00:00",
        ),
    )
    _write_private_json(
        second,
        _admission_document(
            capture_id="2" * 64,
            started="2026-08-16T10:00:02+00:00",
            completed="2026-08-16T10:00:03+00:00",
        ),
    )
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == '{"status":"stable"}\n'
    assert result.stderr == ""


def test_joint_admission_comparator_accepts_closed_bound_apply_transition(tmp_path):
    first = tmp_path / "bound-apply-pre.json"
    second = tmp_path / "bound-apply-post.json"
    _write_private_json(
        first,
        _admission_document(
            capture_id="1" * 64,
            started="2026-08-16T10:00:00+00:00",
            completed="2026-08-16T10:00:01+00:00",
            phase="bound_apply_pre",
            material_fingerprint="a" * 64,
        ),
    )
    _write_private_json(
        second,
        _admission_document(
            capture_id="2" * 64,
            started="2026-08-16T10:00:02+00:00",
            completed="2026-08-16T10:00:03+00:00",
            phase="bound_apply_post",
            material_fingerprint="c" * 64,
        ),
    )
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--bound-apply-transition",
            str(first),
            str(second),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == '{"status":"stable"}\n'
    assert result.stderr == ""


@pytest.mark.parametrize(
    "mutation",
    [
        "batch119_drift",
        "unchanged_joint_material",
        "wrong_pre_phase",
        "wrong_post_phase",
        "reused_capture",
    ],
)
def test_joint_admission_comparator_refuses_invalid_bound_apply_transition(
    tmp_path,
    mutation,
):
    pre = _admission_document(
        capture_id="1" * 64,
        started="2026-08-16T10:00:00+00:00",
        completed="2026-08-16T10:00:01+00:00",
        phase="bound_apply_pre",
    )
    post = _admission_document(
        capture_id="2" * 64,
        started="2026-08-16T10:00:02+00:00",
        completed="2026-08-16T10:00:03+00:00",
        phase="bound_apply_post",
        material_fingerprint="c" * 64,
    )
    if mutation == "batch119_drift":
        post["batch119_material_fingerprint"] = "d" * 64
    elif mutation == "unchanged_joint_material":
        post["material_fingerprint"] = pre["material_fingerprint"]
    elif mutation == "wrong_pre_phase":
        pre["phase"] = "joint_diagnostic"
    elif mutation == "wrong_post_phase":
        post["phase"] = "bound_apply_pre"
    else:
        post["capture_id"] = pre["capture_id"]
    paths = []
    for name, payload in (("pre.json", pre), ("post.json", post)):
        path = tmp_path / name
        _write_private_json(path, payload)
        paths.append(path)
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--bound-apply-transition",
            *(str(path) for path in paths),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == '{"status":"refused"}\n'
    assert result.stderr == ""


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=True),
        lambda payload: payload.update(reservation_count=28),
        lambda payload: payload.update(material_fingerprint="b" * 64),
        lambda payload: payload.update(phase="future_phase"),
        lambda payload: payload.update(phase="bound_apply_pre"),
        lambda payload: payload.update(status="refused"),
        lambda payload: payload.update(reason_code="joint_read_failed"),
        lambda payload: payload.update(capture_id="1" * 64),
        lambda payload: payload.update(extra_field="unexpected"),
        lambda payload: payload.pop("reason_code"),
        lambda payload: payload.update(capture_id="1" * 300),
        lambda payload: payload.update(
            capture_started_at="2026-08-16T10:00:00"
        ),
        lambda payload: payload.update(
            capture_started_at="2026-08-16T10:00:01+00:00"
        ),
        lambda payload: payload.update(
            capture_completed_at="2026-08-16T10:00:02+00:00"
        ),
    ],
)
def test_joint_admission_comparator_refuses_malformed_or_drifting_capture(
    tmp_path, mutation
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_payload = _admission_document(
        capture_id="1" * 64,
        started="2026-08-16T10:00:00+00:00",
        completed="2026-08-16T10:00:01+00:00",
    )
    second_payload = _admission_document(
        capture_id="2" * 64,
        started="2026-08-16T10:00:02+00:00",
        completed="2026-08-16T10:00:03+00:00",
    )
    mutation(second_payload)
    _write_private_json(first, first_payload)
    _write_private_json(second, second_payload)
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == '{"status":"refused"}\n'


def test_joint_admission_comparator_refuses_duplicate_json_key(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    payload = _admission_document(
        capture_id="1" * 64,
        started="2026-08-16T10:00:00+00:00",
        completed="2026-08-16T10:00:01+00:00",
    )
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    first.write_text(raw[:-1] + ',"status":"ready"}', encoding="utf-8")
    first.chmod(0o600)
    _write_private_json(
        second,
        _admission_document(
            capture_id="2" * 64,
            started="2026-08-16T10:00:02+00:00",
            completed="2026-08-16T10:00:03+00:00",
        ),
    )
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == '{"status":"refused"}\n'


def test_joint_admission_comparator_refuses_first_path_replacement(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / (
        "compare_bound_close_batch119_joint_admissions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_joint_admission_comparator_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    replacement = tmp_path / "replacement.json"
    first_payload = _admission_document(
        capture_id="1" * 64,
        started="2026-08-16T10:00:00+00:00",
        completed="2026-08-16T10:00:01+00:00",
    )
    second_payload = _admission_document(
        capture_id="2" * 64,
        started="2026-08-16T10:00:02+00:00",
        completed="2026-08-16T10:00:03+00:00",
    )
    replacement_payload = dict(first_payload)
    replacement_payload["material_fingerprint"] = "b" * 64
    _write_private_json(first, first_payload)
    _write_private_json(second, second_payload)
    _write_private_json(replacement, replacement_payload)
    original_read = module._read_private_file
    calls = 0

    def replacing_read(path_text):
        nonlocal calls
        result = original_read(path_text)
        calls += 1
        if calls == 1:
            replacement.replace(first)
        return result

    module._read_private_file = replacing_read

    with pytest.raises(module.JointAdmissionRefused):
        module._admissions_are_stable([str(first), str(second)])


def test_joint_operator_projector_outputs_only_safe_aggregate(tmp_path):
    payload = _admission_document(
        capture_id="1" * 64,
        started="2026-08-16T10:00:00+00:00",
        completed="2026-08-16T10:00:01+00:00",
    )
    script = Path(__file__).parents[1] / "scripts" / (
        "project_bound_close_batch119_joint_output.py"
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "batch119_incident_count": 1,
        "blocking_writer_count": 0,
        "reservation_count": 29,
        "schema_version": 1,
        "status": "ready",
    }
