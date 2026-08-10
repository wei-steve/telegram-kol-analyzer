import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    ExecutionBinding,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeSignal,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_deferred_item_can_currently_look_succeeded_without_exchange_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-deferred-item.db")

    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=9974,
            posted_at=NOW,
            text="anonymized entry strategy",
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="a" * 64,
            status="succeeded",
            result_json=json.dumps(
                {
                    "status": "deferred",
                    "reason": "adjacent_entry_context_pending",
                    "submitted": False,
                }
            ),
        )
        session.add(item)
        session.add(
            EntryAssemblyAttempt(
                strategy_raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                candidate_generation="legacy-characterization",
                cutoff_posted_at=NOW,
                cutoff_message_id=raw.message_id,
                cutoff_raw_message_id=raw.id,
                blocking_raw_message_ids_json="[998]",
                status="pending",
                fingerprint="b" * 64,
            )
        )
        session.commit()

    with session_factory() as session:
        stored = session.query(MessageInstructionItem).one()
        result = json.loads(stored.result_json or "{}")
        attempt = session.query(EntryAssemblyAttempt).one()

        assert stored.status == "succeeded"
        assert result == {
            "status": "deferred",
            "reason": "adjacent_entry_context_pending",
            "submitted": False,
        }
        assert attempt.status == "pending"
        assert session.query(TradeSignal).count() == 0
        assert session.query(ExecutionBinding).count() == 0


def test_price_entered_lifecycle_is_not_exchange_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-price-entered.db")

    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=101,
                message_id=4171,
                symbol="ETH",
                side="long",
                lifecycle_status="entered",
                signal_at=NOW,
                entered_at=NOW,
                entry_price_actual=3200.0,
            )
        )
        session.commit()

    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()

        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.execution_binding_id is None
        assert session.query(ExecutionBinding).count() == 0

