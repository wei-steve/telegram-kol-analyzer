from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
)
from telegram_kol_research.position_mutation_intents import (
    PositionMutationIntentError,
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)


NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)


def _session_factory(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="strategy-1",
            kol_id="kol-1",
            chat_id=-1001,
            message_id=10,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="strategy-1",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            pos_id="pos-1",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        session.add(leg)
        session.commit()
        return session_factory, binding.id, leg.id


def test_reservation_is_idempotent_for_same_fingerprints(tmp_path):
    session_factory, binding_id, leg_id = _session_factory(tmp_path)

    first = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="management:1:1:cancel:ord-1",
        operation="cancel_position_sltp",
        strategy_instance_id="strategy-1",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-1",
        order_id="ord-1",
        authority_fingerprint="authority-fp",
        request_fingerprint="request-fp",
        request={"instId": "BTC-USDT-SWAP", "ordId": "ord-1"},
        reserved_at=NOW,
    )
    second = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="management:1:1:cancel:ord-1",
        operation="cancel_position_sltp",
        strategy_instance_id="strategy-1",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-1",
        order_id="ord-1",
        authority_fingerprint="authority-fp",
        request_fingerprint="request-fp",
        request={"instId": "BTC-USDT-SWAP", "ordId": "ord-1"},
        reserved_at=NOW,
    )

    assert first.id == second.id
    with session_factory() as session:
        assert session.query(PositionMutationIntent).count() == 1


def test_reservation_rejects_idempotency_conflict(tmp_path):
    session_factory, binding_id, leg_id = _session_factory(tmp_path)
    common = {
        "session_factory": session_factory,
        "idempotency_key": "management:1:1:cancel:ord-1",
        "operation": "cancel_position_sltp",
        "strategy_instance_id": "strategy-1",
        "execution_binding_id": binding_id,
        "execution_order_leg_id": leg_id,
        "pos_id": "pos-1",
        "order_id": "ord-1",
        "authority_fingerprint": "authority-fp",
        "request": {"instId": "BTC-USDT-SWAP", "ordId": "ord-1"},
        "reserved_at": NOW,
    }
    reserve_position_mutation_intent(
        request_fingerprint="request-fp",
        **common,
    )

    with pytest.raises(
        PositionMutationIntentError,
        match="position_mutation_intent_conflict",
    ):
        reserve_position_mutation_intent(
            request_fingerprint="changed-request-fp",
            **common,
        )


def test_transition_is_compare_and_set(tmp_path):
    session_factory, binding_id, leg_id = _session_factory(tmp_path)
    intent = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="management:1:1:set:stop",
        operation="set_position_sltp",
        strategy_instance_id="strategy-1",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-1",
        order_id=None,
        authority_fingerprint="authority-fp",
        request_fingerprint="request-fp",
        request={"posId": "pos-1", "slTriggerPx": "63895.725"},
        reserved_at=NOW,
    )

    assert transition_position_mutation_intent(
        session_factory,
        intent.id,
        expected_statuses={"reserved"},
        new_status="submitting",
        transitioned_at=NOW,
    )
    assert not transition_position_mutation_intent(
        session_factory,
        intent.id,
        expected_statuses={"reserved"},
        new_status="submitted",
        transitioned_at=NOW,
    )
