from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import StrategyManagementComponent
from telegram_kol_research.strategy_management_components import (
    claim_management_component,
    create_management_component,
    transition_management_component,
)


def _create(session, *, idempotency_key="batch:1:tp"):
    return create_management_component(
        session,
        management_batch_id=1,
        strategy_management_leg_id=None,
        component_kind="consume_take_profit_stage",
        sequence=10,
        idempotency_key=idempotency_key,
        desired={"policy": "consume_first_stage"},
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_component_identity_is_immutable_and_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        component = _create(session)
        session.commit()
        assert _create(session).id == component.id
        with pytest.raises(ValueError, match="immutable"):
            create_management_component(
                session,
                management_batch_id=1,
                strategy_management_leg_id=None,
                component_kind="consume_take_profit_stage",
                sequence=10,
                idempotency_key="different-key",
                desired={"policy": "consume_first_stage"},
                now=datetime(2026, 8, 4, tzinfo=UTC),
            )


def test_component_transition_is_compare_and_set(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        component = _create(session)
        session.flush()

        changed = transition_management_component(
            session,
            component_id=component.id,
            expected_status="pending",
            new_status="preflighting",
            now=datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
        )
        stale = transition_management_component(
            session,
            component_id=component.id,
            expected_status="pending",
            new_status="preflighting",
            now=datetime(2026, 8, 4, 0, 2, tzinfo=UTC),
        )

        assert changed is True
        assert stale is False


def test_restart_claims_only_safe_or_stale_local_states(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 4, 1, tzinfo=UTC)
    with session_factory() as session:
        component = _create(session)
        session.flush()
        assert claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )
        session.execute(
            StrategyManagementComponent.__table__.update()
            .where(StrategyManagementComponent.id == component.id)
            .values(
                status="preflighting",
                updated_at=now - timedelta(minutes=10),
            )
        )
        assert claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )


@pytest.mark.parametrize(
    "protected_status",
    ["awaiting_exchange", "confirmed", "operator_required"],
)
def test_restart_never_reclaims_exchange_or_terminal_state_as_new_write(
    tmp_path, protected_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 8, 4, 1, tzinfo=UTC)
    with session_factory() as session:
        component = _create(session)
        session.flush()
        session.execute(
            StrategyManagementComponent.__table__.update()
            .where(StrategyManagementComponent.id == component.id)
            .values(
                status=protected_status,
                updated_at=now - timedelta(hours=1),
            )
        )
        assert not claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        )
