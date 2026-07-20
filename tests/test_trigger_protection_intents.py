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
        with pytest.raises(ValueError, match="immutable"):
            create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=leg_id,
                request_fingerprint="f" * 64,
                pre_submit_tpsl_baseline_json='{"orders":[]}',
                correlation_id="corr-1",
            )
        with pytest.raises(ValueError, match="correlation"):
            create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=leg_id,
                request_fingerprint="f" * 64,
                pre_submit_tpsl_baseline_json=baseline,
                correlation_id="corr-changed",
            )


@pytest.mark.parametrize(
    ("request_fingerprint", "baseline", "correlation_id"),
    [
        ("", "{}", "corr-1"),
        ("not-json", "{}", "corr-1"),
        ("a" * 64, "", "corr-1"),
        ("a" * 64, "not-json", "corr-1"),
        ("a" * 64, "{}", ""),
    ],
)
def test_create_or_get_rejects_empty_or_non_normalized_immutable_evidence(
    tmp_path, request_fingerprint, baseline, correlation_id
):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)

    with session_factory() as session:
        with pytest.raises(ValueError, match="immutable evidence"):
            create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=leg_id,
                request_fingerprint=request_fingerprint,
                pre_submit_tpsl_baseline_json=baseline,
                correlation_id=correlation_id,
            )


def test_create_or_get_rejects_non_entry_or_cross_venue_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    exit_leg_id = _leg(session_factory, leg_index=0)
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, exit_leg_id)
        leg.purpose = "take_profit"
        session.commit()
        with pytest.raises(ValueError, match="entry"):
            create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=exit_leg_id,
                request_fingerprint="d" * 64,
                pre_submit_tpsl_baseline_json="{}",
                correlation_id="corr-exit",
            )

    entry_leg_id = _leg(session_factory, leg_index=1)
    with session_factory() as session:
        with pytest.raises(ValueError, match="venue"):
            create_or_get_trigger_protection_intent(
                session,
                venue="other-venue",
                execution_order_leg_id=entry_leg_id,
                request_fingerprint="e" * 64,
                pre_submit_tpsl_baseline_json="{}",
                correlation_id="corr-venue",
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


def test_identity_claims_strip_surrounding_whitespace_and_reject_blank_values(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)
    with session_factory() as session:
        intent = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="d" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-identities",
        )
        record_trigger_protection_parent(session, intent, parent_trigger_order_id=" parent-1 ")
        transition_trigger_protection_intent(
            session,
            intent,
            recovery_state="adopted",
            adopted_order_id=" adopted-1 ",
        )
        assert intent.parent_trigger_order_id == "parent-1"
        assert intent.adopted_order_id == "adopted-1"
        with pytest.raises(ValueError, match="nonempty"):
            record_trigger_protection_parent(session, intent, parent_trigger_order_id=" \t ")
        with pytest.raises(ValueError, match="nonempty"):
            transition_trigger_protection_intent(
                session,
                intent,
                recovery_state="adopted",
                adopted_order_id=" \n ",
            )


def test_transition_rejects_unknown_state_and_negative_retries(tmp_path):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)
    with session_factory() as session:
        intent = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="e" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-transition",
        )
        with pytest.raises(ValueError, match="recovery state"):
            transition_trigger_protection_intent(
                session, intent, recovery_state="invented"
            )
        with pytest.raises(ValueError, match="retry attempts"):
            transition_trigger_protection_intent(
                session, intent, recovery_state="retrying", retry_attempts=-1
            )


def test_create_or_get_recovers_from_competing_committed_intent(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "intents.db")
    leg_id = _leg(session_factory)
    import telegram_kol_research.trigger_protection_intents as intents_module

    original_flush = intents_module.Session.flush
    competing_done = False

    def competing_flush(session, *args, **kwargs):
        nonlocal competing_done
        if not competing_done:
            competing_done = True
            with session_factory() as competing:
                competing.add(
                    intents_module.TriggerProtectionIntent(
                        venue="deepcoin",
                        execution_binding_id=1,
                        execution_order_leg_id=leg_id,
                        request_fingerprint="f" * 64,
                        pre_submit_tpsl_baseline_json="{}",
                        correlation_id="corr-race",
                    )
                )
                competing.commit()
        return original_flush(session, *args, **kwargs)

    monkeypatch.setattr(intents_module.Session, "flush", competing_flush)
    with session_factory() as session:
        recovered = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            request_fingerprint="f" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-race",
        )
        assert recovered.execution_order_leg_id == leg_id
        assert session.is_active


def test_parent_claim_recovers_from_competing_committed_owner(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "intents.db")
    first_leg_id = _leg(session_factory, leg_index=0)
    second_leg_id = _leg(session_factory, leg_index=1)
    with session_factory() as session:
        first = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=first_leg_id,
            request_fingerprint="1" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-first-race",
        )
        second = create_or_get_trigger_protection_intent(
            session,
            venue="deepcoin",
            execution_order_leg_id=second_leg_id,
            request_fingerprint="2" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="corr-second-race",
        )
        session.commit()
        first_id, second_id = first.id, second.id

    import telegram_kol_research.trigger_protection_intents as intents_module

    original_execute = intents_module.Session.execute
    competing_done = False

    def competing_execute(session, statement, *args, **kwargs):
        nonlocal competing_done
        if not competing_done:
            competing_done = True
            with session_factory() as competing:
                competing_intent = competing.get(
                    intents_module.TriggerProtectionIntent, second_id
                )
                record_trigger_protection_parent(
                    competing,
                    competing_intent,
                    parent_trigger_order_id="parent-race",
                )
                competing.commit()
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(intents_module.Session, "execute", competing_execute)
    with session_factory() as session:
        first = session.get(intents_module.TriggerProtectionIntent, first_id)
        with pytest.raises(ValueError, match="already owned"):
            record_trigger_protection_parent(
                session, first, parent_trigger_order_id="parent-race"
            )
        assert session.is_active


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
