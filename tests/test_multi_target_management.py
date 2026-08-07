from datetime import UTC, datetime

from telegram_kol_research import models
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, StrategyLifecycle


NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


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
