from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from telegram_kol_research import models
from telegram_kol_research.db import MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME
from telegram_kol_research.db import create_session_factory


def _management_models():
    batch_model = getattr(models, "StrategyManagementBatch", None)
    leg_model = getattr(models, "StrategyManagementLeg", None)
    assert batch_model is not None, "StrategyManagementBatch model is missing"
    assert leg_model is not None, "StrategyManagementLeg model is missing"
    return batch_model, leg_model


def _repository():
    return importlib.import_module("telegram_kol_research.strategy_management_batches")


def _batch_values(*, fingerprint: str = "batch-fingerprint", strategy: str = "strategy-1"):
    now = datetime(2026, 7, 15, 1, 2, 3, tzinfo=UTC)
    return {
        "idempotency_fingerprint": fingerprint,
        "raw_message_id": 101,
        "recognition_decision_id": 201,
        "recognition_generation": "generation-1",
        "target_lifecycle_id": 301,
        "strategy_instance_id": strategy,
        "execution_binding_id": 401,
        "intent": "partial_take_profit",
        "effective_action": "partial_take_profit",
        "requested_fraction": 0.5,
        "effective_fraction": 0.5,
        "partial_round_before": 0,
        "status": "ready",
        "reason_code": None,
        "target_fingerprint": "target-fingerprint",
        "target_snapshot_json": json.dumps(
            {"positions": [{"pos_id": "position-1", "size": "0.02"}]},
            sort_keys=True,
        ),
        "planned_at": now,
        "notification_state": "pending",
        "created_at": now,
        "updated_at": now,
    }


def test_management_batch_idempotency_fingerprint_is_unique(tmp_path):
    batch_model, _ = _management_models()
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        session.add(batch_model(**_batch_values()))
        session.commit()
        session.add(batch_model(**_batch_values(strategy="strategy-2")))
        with pytest.raises(IntegrityError):
            session.commit()


def test_management_leg_is_unique_per_batch_and_position(tmp_path):
    batch_model, leg_model = _management_models()
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        batch = batch_model(**_batch_values())
        session.add(batch)
        session.flush()
        session.add_all(
            [
                leg_model(
                    management_batch_id=batch.id,
                    execution_order_leg_id=501,
                    pos_id="position-1",
                    leg_index=0,
                    status="planned",
                ),
                leg_model(
                    management_batch_id=batch.id,
                    execution_order_leg_id=502,
                    pos_id="position-1",
                    leg_index=1,
                    status="planned",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_management_batch_allows_only_one_unsafe_batch_per_strategy(tmp_path):
    batch_model, _ = _management_models()
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        session.add(batch_model(**_batch_values(fingerprint="ready")))
        session.commit()
        recovery_values = _batch_values(
            fingerprint="recovery", strategy="strategy-1"
        )
        recovery_values["status"] = "recovery_required"
        session.add(batch_model(**recovery_values))
        with pytest.raises(IntegrityError):
            session.commit()

    with session_factory() as session:
        session.query(batch_model).delete()
        partial_failed_values = _batch_values(fingerprint="partial-failed")
        partial_failed_values["status"] = "partial_failed"
        session.add(batch_model(**partial_failed_values))
        session.commit()
        session.add(batch_model(**_batch_values(fingerprint="next-ready")))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("safe_status", ["succeeded", "blocked", "resolved"])
def test_management_batch_safe_terminal_status_releases_strategy_lock(
    tmp_path, safe_status
):
    batch_model, _ = _management_models()
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        values = _batch_values(fingerprint=f"terminal-{safe_status}")
        values["status"] = safe_status
        session.add(batch_model(**values))
        session.commit()
        session.add(batch_model(**_batch_values(fingerprint=f"ready-{safe_status}")))
        session.commit()


def test_management_repository_round_trips_json_and_transitions(tmp_path):
    repository = _repository()
    session_factory = create_session_factory(tmp_path / "research.db")
    planned_at = datetime(2026, 7, 15, 1, 2, 3, tzinfo=UTC)
    target_snapshot = {
        "positions": [{"pos_id": "position-1", "size": "0.02"}],
        "protection": [{"order_id": "sl-old", "trigger_price": "60000"}],
    }
    old_tpsl = [{"order_id": "sl-old", "trigger_price": "60000"}]
    planned_tpsl = [{"trigger_price": "61000", "size": "0"}]
    request = {"closePosId": "position-1", "sz": "0.01"}
    response = {"code": "0", "data": {"orderId": "close-order-1"}}
    error = {"code": "timeout", "retry_allowed": False}

    created = repository.create_management_batch(
        session_factory,
        idempotency_fingerprint="fingerprint-1",
        raw_message_id=101,
        recognition_decision_id=201,
        recognition_generation="generation-1",
        target_lifecycle_id=301,
        strategy_instance_id="strategy-1",
        execution_binding_id=401,
        intent="partial_take_profit",
        effective_action="partial_take_profit",
        requested_fraction=0.5,
        effective_fraction=0.5,
        partial_round_before=0,
        target_fingerprint="target-1",
        target_snapshot=target_snapshot,
        planned_at=planned_at,
        legs=[
            repository.ManagementLegCreate(
                execution_order_leg_id=501,
                pos_id="position-1",
                leg_index=0,
                preflight_size="0.02",
                planned_close_size="0.01",
                avg_entry_price="62000",
                quantity_step="0.001",
                old_tpsl=old_tpsl,
                planned_tpsl=planned_tpsl,
                request=request,
                response=response,
                last_error=error,
                last_exchange_snapshot={"pos": "0.02"},
            )
        ],
    )

    loaded = repository.load_management_batch(session_factory, created.id)
    assert loaded.target_snapshot == target_snapshot
    assert loaded.legs[0].old_tpsl == old_tpsl
    assert loaded.legs[0].planned_tpsl == planned_tpsl
    assert loaded.legs[0].request == request
    assert loaded.legs[0].response == response
    assert loaded.legs[0].last_error == error
    assert loaded.legs[0].last_exchange_snapshot == {"pos": "0.02"}

    claimed = repository.claim_ready_batch(
        session_factory, loaded.id, claimed_at=planned_at + timedelta(seconds=1)
    )
    assert claimed is not None
    assert claimed.status == "executing"
    assert claimed.started_at == planned_at + timedelta(seconds=1)
    assert repository.claim_ready_batch(session_factory, loaded.id) is None

    assert repository.transition_leg(
        session_factory,
        loaded.legs[0].id,
        expected_statuses={"planned"},
        new_status="reserved",
        request={"closePosId": "position-1", "sz": "0.01", "ordType": "market"},
    )
    assert not repository.transition_leg(
        session_factory,
        loaded.legs[0].id,
        expected_statuses={"planned"},
        new_status="submitted",
    )
    assert repository.transition_batch(
        session_factory,
        loaded.id,
        expected_statuses={"executing"},
        new_status="reconciling",
        transitioned_at=planned_at + timedelta(seconds=2),
    )


def test_management_repository_lists_recoverable_batches_oldest_first(tmp_path):
    repository = _repository()
    session_factory = create_session_factory(tmp_path / "research.db")
    first = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    def create(fingerprint: str, strategy: str, planned_at: datetime):
        return repository.create_management_batch(
            session_factory,
            idempotency_fingerprint=fingerprint,
            raw_message_id=101,
            recognition_decision_id=201,
            recognition_generation=f"generation-{fingerprint}",
            target_lifecycle_id=301,
            strategy_instance_id=strategy,
            execution_binding_id=401,
            intent="full_exit",
            effective_action="full_exit",
            requested_fraction=None,
            effective_fraction=1.0,
            partial_round_before=0,
            target_fingerprint=f"target-{fingerprint}",
            target_snapshot={"positions": []},
            planned_at=planned_at,
            legs=[],
        )

    oldest = create("oldest", "strategy-1", first)
    middle = create("middle", "strategy-2", first + timedelta(seconds=1))
    create("ready", "strategy-3", first + timedelta(seconds=2))
    repository.claim_ready_batch(session_factory, oldest.id, claimed_at=first)
    repository.claim_ready_batch(session_factory, middle.id, claimed_at=first)
    repository.transition_batch(
        session_factory,
        middle.id,
        expected_statuses={"executing"},
        new_status="reconciling",
        transitioned_at=first + timedelta(seconds=3),
    )

    records = repository.list_recoverable_batches(session_factory, limit=1)
    assert [record.id for record in records] == [oldest.id]


@pytest.mark.parametrize(
    "missing_index",
    [
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME,
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME,
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME,
    ],
)
def test_management_mutations_fail_closed_when_required_index_is_missing(
    tmp_path, missing_index
):
    repository = _repository()
    database_path = tmp_path / f"{missing_index}.db"
    session_factory = create_session_factory(database_path)
    created = repository.create_management_batch(
        session_factory,
        idempotency_fingerprint="existing-fingerprint",
        raw_message_id=101,
        recognition_decision_id=201,
        recognition_generation="generation-existing",
        target_lifecycle_id=301,
        strategy_instance_id="strategy-existing",
        execution_binding_id=401,
        intent="full_exit",
        effective_action="full_exit",
        requested_fraction=None,
        effective_fraction=1.0,
        partial_round_before=0,
        target_fingerprint="target-existing",
        target_snapshot={"positions": [{"pos_id": "position-1"}]},
        legs=[
            repository.ManagementLegCreate(
                execution_order_leg_id=501,
                pos_id="position-1",
                leg_index=0,
                last_error={"code": "existing-error"},
            )
        ],
    )
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text(f'DROP INDEX "{missing_index}"'))
    batch_model, leg_model = _management_models()
    with session_factory() as session:
        if missing_index == MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME:
            duplicate_values = _batch_values(
                fingerprint="existing-fingerprint", strategy="strategy-duplicate"
            )
            duplicate_values["status"] = "succeeded"
            session.add(batch_model(**duplicate_values))
        elif missing_index == MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME:
            session.add(
                batch_model(
                    **_batch_values(
                        fingerprint="duplicate-active",
                        strategy="strategy-existing",
                    )
                )
            )
        else:
            session.add(
                leg_model(
                    management_batch_id=created.id,
                    execution_order_leg_id=502,
                    pos_id="position-1",
                    leg_index=1,
                    status="planned",
                )
            )
        session.commit()
    engine.dispose()

    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        indexes = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            ).fetchall()
        }
    assert missing_index not in indexes

    def snapshot():
        with session_factory() as session:
            batch_rows = session.execute(
                text(
                    "SELECT id, status, reason_code FROM strategy_management_batches "
                    "ORDER BY id"
                )
            ).fetchall()
            leg_rows = session.execute(
                text(
                    "SELECT id, status, last_error FROM strategy_management_legs "
                    "ORDER BY id"
                )
            ).fetchall()
        return batch_rows, leg_rows

    before = snapshot()
    with pytest.raises(repository.ManagementSchemaSafetyError, match=missing_index):
        repository.create_management_batch(
            session_factory,
            idempotency_fingerprint="new-fingerprint",
            raw_message_id=102,
            recognition_decision_id=202,
            recognition_generation="generation-new",
            target_lifecycle_id=302,
            strategy_instance_id="strategy-new",
            execution_binding_id=402,
            intent="full_exit",
            effective_action="full_exit",
            requested_fraction=None,
            effective_fraction=1.0,
            partial_round_before=0,
            target_fingerprint="target-new",
            target_snapshot={"positions": []},
            legs=[],
        )
    assert snapshot() == before

    with pytest.raises(repository.ManagementSchemaSafetyError, match=missing_index):
        repository.claim_ready_batch(session_factory, created.id)
    assert snapshot() == before

    with pytest.raises(repository.ManagementSchemaSafetyError, match=missing_index):
        repository.transition_batch(
            session_factory,
            created.id,
            expected_statuses={"ready"},
            new_status="blocked",
            reason_code="must-not-write",
        )
    assert snapshot() == before

    with pytest.raises(repository.ManagementSchemaSafetyError, match=missing_index):
        repository.transition_leg(
            session_factory,
            created.legs[0].id,
            expected_statuses={"planned"},
            new_status="failed",
            last_error={"code": "must-not-write"},
        )
    assert snapshot() == before


def test_management_transitions_preserve_omitted_fields_and_clear_explicit_none(
    tmp_path,
):
    repository = _repository()
    session_factory = create_session_factory(tmp_path / "research.db")
    created = repository.create_management_batch(
        session_factory,
        idempotency_fingerprint="fingerprint-unset",
        raw_message_id=101,
        recognition_decision_id=201,
        recognition_generation="generation-unset",
        target_lifecycle_id=301,
        strategy_instance_id="strategy-unset",
        execution_binding_id=401,
        intent="full_exit",
        effective_action="full_exit",
        requested_fraction=None,
        effective_fraction=1.0,
        partial_round_before=0,
        target_fingerprint="target-unset",
        target_snapshot={"positions": [{"pos_id": "position-1"}]},
        reason_code="existing-reason",
        legs=[
            repository.ManagementLegCreate(
                execution_order_leg_id=501,
                pos_id="position-1",
                leg_index=0,
                client_order_id="client-1",
                exchange_order_id="exchange-1",
                request={"request": "existing"},
                response={"response": "existing"},
                last_error={"error": "existing"},
                last_exchange_snapshot={"snapshot": "existing"},
            )
        ],
    )

    assert repository.transition_batch(
        session_factory,
        created.id,
        expected_statuses={"ready"},
        new_status="executing",
    )
    assert repository.transition_leg(
        session_factory,
        created.legs[0].id,
        expected_statuses={"planned"},
        new_status="reserved",
    )
    preserved = repository.load_management_batch(session_factory, created.id)
    assert preserved.reason_code == "existing-reason"
    assert preserved.legs[0].client_order_id == "client-1"
    assert preserved.legs[0].exchange_order_id == "exchange-1"
    assert preserved.legs[0].request == {"request": "existing"}
    assert preserved.legs[0].response == {"response": "existing"}
    assert preserved.legs[0].last_error == {"error": "existing"}
    assert preserved.legs[0].last_exchange_snapshot == {"snapshot": "existing"}

    assert repository.transition_batch(
        session_factory,
        created.id,
        expected_statuses={"executing"},
        new_status="reconciling",
        reason_code=None,
    )
    assert repository.transition_leg(
        session_factory,
        created.legs[0].id,
        expected_statuses={"reserved"},
        new_status="submitted",
        client_order_id=None,
        exchange_order_id=None,
        request=None,
        response=None,
        last_error=None,
        last_exchange_snapshot=None,
    )
    cleared = repository.load_management_batch(session_factory, created.id)
    assert cleared.reason_code is None
    assert cleared.legs[0].client_order_id is None
    assert cleared.legs[0].exchange_order_id is None
    assert cleared.legs[0].request is None
    assert cleared.legs[0].response is None
    assert cleared.legs[0].last_error is None
    assert cleared.legs[0].last_exchange_snapshot is None
