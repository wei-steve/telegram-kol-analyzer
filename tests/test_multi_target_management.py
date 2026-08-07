from datetime import UTC, datetime
import importlib
import importlib.util

import pytest

from telegram_kol_research import models
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    RawMessage,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def _target_module():
    module_name = "telegram_kol_research.management_message_targets"
    assert importlib.util.find_spec(module_name) is not None
    return importlib.import_module(module_name)


def _persist_projection_inputs(session):
    raw = RawMessage(chat_id=100, message_id=3465, text="BTC ETH partial TP")
    btc = StrategyLifecycle(
        chat_id=100,
        message_id=3000,
        symbol="BTC",
        side="short",
        signal_at=NOW,
    )
    eth = StrategyLifecycle(
        chat_id=100,
        message_id=3001,
        symbol="ETH",
        side="short",
        signal_at=NOW,
    )
    session.add_all([raw, btc, eth])
    session.flush()
    return raw, btc, eth


def test_bootstrap_creates_multi_target_ledgers(tmp_path):
    assert hasattr(models, "ManagementMessageEnvelope")
    assert hasattr(models, "ManagementMessageTarget")

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=3465, text="BTC ETH partial TP")
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=3000,
            symbol="BTC",
            side="short",
            signal_at=NOW,
        )
        session.add_all([raw, lifecycle])
        session.flush()

        envelope = models.ManagementMessageEnvelope(
            raw_message_id=raw.id,
            decision_fingerprint="d" * 64,
            normalized_action="partial_take_profit",
            shared_parameters_json="{}",
            projection_mode="shadow",
        )
        session.add(envelope)
        session.flush()
        target = models.ManagementMessageTarget(
            envelope_id=envelope.id,
            raw_message_id=raw.id,
            target_lifecycle_id=lifecycle.id,
            target_ordinal=0,
            symbol="BTC",
            side="short",
            normalized_action="partial_take_profit",
            parameters_json='{"fraction":0.5}',
            parameter_fingerprint="p" * 64,
            collision_group_fingerprint="c" * 64,
            admission_state="identified",
            execution_state="not_started",
        )
        session.add(target)
        session.commit()

        assert envelope.id is not None
        assert target.id is not None


def test_project_targets_is_idempotent_for_message_lifecycle_action_parameters(
    tmp_path,
):
    target_module = _target_module()
    session_factory = create_session_factory(tmp_path / "projection.db")
    with session_factory() as session:
        raw, btc, eth = _persist_projection_inputs(session)
        decision = {
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "management_fraction": 0.5,
            "targets": [
                {
                    "target_lifecycle_id": btc.id,
                    "symbol": "BTC",
                    "side": "short",
                },
                {
                    "target_lifecycle_id": eth.id,
                    "symbol": "ETH",
                    "side": "short",
                },
            ],
        }

        first = target_module.project_management_targets_in_session(
            session,
            raw_message_id=raw.id,
            decision=decision,
            decision_fingerprint="a" * 64,
            projection_mode="shadow",
        )
        second = target_module.project_management_targets_in_session(
            session,
            raw_message_id=raw.id,
            decision=decision,
            decision_fingerprint="a" * 64,
            projection_mode="shadow",
        )
        session.commit()

        assert [row.id for row in first] == [row.id for row in second]
        assert session.query(ManagementMessageEnvelope).count() == 1
        assert session.query(ManagementMessageTarget).count() == 2
        assert [row.target_ordinal for row in first] == [0, 1]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["confirmed", "confirmed"], "succeeded"),
        (["confirmed", "refused"], "partial_success"),
        (["confirmed", "failed"], "partial_success"),
        (["confirmed", "submit_unknown"], "attention_required"),
        (["confirmed", "recovery_required"], "attention_required"),
        (["refused", "failed"], "failed"),
    ],
)
def test_aggregate_status_is_derived(states, expected):
    target_module = _target_module()

    assert target_module.derive_envelope_status(states) == expected


def test_invalid_target_state_transition_is_rejected(tmp_path):
    target_module = _target_module()
    session_factory = create_session_factory(tmp_path / "transition.db")
    with session_factory() as session:
        raw, btc, _ = _persist_projection_inputs(session)
        target = target_module.project_management_targets_in_session(
            session,
            raw_message_id=raw.id,
            decision={
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "targets": [
                    {
                        "target_lifecycle_id": btc.id,
                        "symbol": "BTC",
                        "side": "short",
                    }
                ],
            },
            decision_fingerprint="b" * 64,
            projection_mode="shadow",
        )[0]

        with pytest.raises(ValueError, match="invalid execution transition"):
            target_module.transition_target_execution_state_in_session(
                session,
                target_id=target.id,
                new_state="confirmed",
                now=NOW,
            )
