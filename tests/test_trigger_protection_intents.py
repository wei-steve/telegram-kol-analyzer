from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect

from telegram_kol_research.db import create_session_factory, init_db
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg
from telegram_kol_research.trigger_protection_intents import (
    create_or_get_trigger_protection_intent,
    record_trigger_protection_parent,
    transition_trigger_protection_intent,
)


NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _leg(session_factory, *, leg_index: int = 0) -> int:
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            venue="deepcoin",
            kol_id="kol-1",
            chat_id=101,
            message_id=202,
            symbol="BTCUSDT",
            side="long",
        ),
    )
    return upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=leg_index,
            purpose="entry",
            venue="deepcoin",
            order_kind="market",
        ),
    )


def test_create_or_get_preserves_immutable_normalized_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)
    request = '{"size":"1","stop":"60000"}'
    baseline = '{"orders":[{"ordId":"old-1"}]}'

    with session_factory() as session:
        created = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="f" * 64,
            pre_submit_tpsl_baseline_json=baseline,
            correlation_id="corr-1",
        )
        again = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="f" * 64,
            pre_submit_tpsl_baseline_json=baseline,
            correlation_id="corr-1",
        )
        session.commit()

        assert created.id == again.id
        assert created.execution_binding_id is not None
        assert created.request_fingerprint == "f" * 64
        assert created.pre_submit_tpsl_baseline_json == baseline
        assert created.recovery_state == "pending"

        with pytest.raises(ValueError, match="immutable"):
            create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=leg_id,
                request_fingerprint="e" * 64,
                pre_submit_tpsl_baseline_json=baseline,
                correlation_id="corr-1",
            )


def test_parent_and_adopted_order_identities_are_exclusive_per_venue(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    first_leg_id = _leg(session_factory, leg_index=0)
    second_leg_id = _leg(session_factory, leg_index=1)

    with session_factory() as session:
        first = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=first_leg_id,
            request_fingerprint="a" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-1",
        )
        record_trigger_protection_parent(session, first, parent_trigger_order_id="parent-1")
        transition_trigger_protection_intent(
            session,
            first,
            recovery_state="adopted",
            adopted_order_id="adopted-1",
            retry_attempts=1,
            next_attempt_at=NOW + timedelta(minutes=5),
        )
        session.commit()

        second = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=second_leg_id,
            request_fingerprint="b" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-2",
        )
        with pytest.raises(ValueError, match="parent"):
            record_trigger_protection_parent(session, second, parent_trigger_order_id="parent-1")
        with pytest.raises(ValueError, match="adopted"):
            transition_trigger_protection_intent(
                session,
                second,
                recovery_state="adopted",
                adopted_order_id="adopted-1",
            )


def test_transition_is_idempotent_and_updates_recovery_fields(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)
    with session_factory() as session:
        intent = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="c" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-3",
        )
        record_trigger_protection_parent(session, intent, parent_trigger_order_id="parent-3")
        first = transition_trigger_protection_intent(
            session,
            intent,
            recovery_state="retrying",
            retry_attempts=2,
            next_attempt_at=NOW,
        )
        again = transition_trigger_protection_intent(
            session,
            intent,
            recovery_state="retrying",
            retry_attempts=2,
            next_attempt_at=NOW,
        )
        session.commit()

        assert first.id == again.id
        assert intent.parent_trigger_order_id == "parent-3"
        assert intent.recovery_state == "retrying"
        assert intent.retry_attempts == 2
        assert intent.next_attempt_at.replace(tzinfo=UTC) == NOW


def test_init_db_adds_trigger_protection_intent_table_and_indexes_to_old_sqlite_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    ExecutionBinding.__table__.create(engine)
    ExecutionOrderLeg.__table__.create(engine)

    init_db(engine)

    inspector = inspect(engine)
    assert "trigger_protection_intents" in inspector.get_table_names()
    index_names = {item["name"] for item in inspector.get_indexes("trigger_protection_intents")}
    unique_names = {item["name"] for item in inspector.get_unique_constraints("trigger_protection_intents")}
    assert "ix_trigger_protection_intents_recovery_next_attempt" in index_names
    assert "uq_trigger_protection_intents_venue_leg" in unique_names
    assert "uq_trigger_protection_intents_venue_adopted_order" in index_names
