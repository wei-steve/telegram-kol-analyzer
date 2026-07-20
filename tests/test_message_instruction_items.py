import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_instruction_items import (
    claim_next_message_instruction_item,
    create_message_instruction_items_in_session,
    finish_message_instruction_item,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _persist_dual_instruction_message(session_factory):
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=55, text="close old and open new")
        session.add(raw)
        session.flush()

        old_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:20:BTC:short",
            kol_id="kol-1",
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side="short",
        )
        session.add(old_binding)
        session.flush()

        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=20,
            symbol="BTC",
            side="short",
            signal_at=NOW,
            execution_binding_id=old_binding.id,
        )
        session.add(lifecycle)
        session.flush()

        management = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="position_update",
            target_lifecycle_id=lifecycle.id,
            management_action="close",
        )
        entry = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
        )
        session.add_all([management, entry])
        session.flush()
        ids = raw.id, management.id, entry.id, lifecycle.id
        session.commit()
    return ids


def test_items_are_unique_and_management_sorts_before_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "items.db")
    raw_id, management_id, entry_id, lifecycle_id = _persist_dual_instruction_message(
        session_factory
    )

    with session_factory() as session:
        first = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        second = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        session.commit()

        assert [(item.instruction_kind, item.sequence) for item in first] == [
            ("management", 0),
            ("entry", 1),
        ]
        assert [item.id for item in second] == [item.id for item in first]
        assert len({item.idempotency_key for item in first}) == 2
        assert first[0].strategy_instance_id == "deepcoin:100:20:BTC:short"
        assert first[1].strategy_instance_id == "deepcoin:100:55:ETH:long"

        expected_management_key = hashlib.sha256(
            (
                f"{raw_id}:{management_id}:management:{lifecycle_id}:"
                "deepcoin:100:20:BTC:short"
            ).encode()
        ).hexdigest()
        expected_entry_key = hashlib.sha256(
            (
                f"{raw_id}:{entry_id}:entry::deepcoin:100:55:ETH:long"
            ).encode()
        ).hexdigest()
        assert first[0].idempotency_key == expected_management_key
        assert first[1].idempotency_key == expected_entry_key

        stored = session.query(MessageInstructionItem).all()
        assert len(stored) == 2


def test_claim_transitions_only_pending_items_in_sequence(tmp_path):
    session_factory = create_session_factory(tmp_path / "claim.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        create_message_instruction_items_in_session(session, raw_message_id=raw_id)
        session.commit()

    management = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert management is not None
    assert management.instruction_kind == "management"
    assert management.status == "executing"

    finish_message_instruction_item(
        session_factory,
        item_id=management.id,
        status="failed",
        result={"reason": "definite rejection"},
        now=NOW,
    )
    entry = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert entry is not None
    assert entry.instruction_kind == "entry"
    assert entry.status == "executing"


def test_claim_never_returns_unknown_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        for item in items:
            item.status = "unknown"
        session.commit()

    assert (
        claim_next_message_instruction_item(
            session_factory, raw_message_id=raw_id, now=NOW
        )
        is None
    )


@pytest.mark.parametrize(
    ("status", "result_column", "empty_column"),
    [
        ("submitted", "result_json", "error_json"),
        ("succeeded", "result_json", "error_json"),
        ("failed", "error_json", "result_json"),
        ("unknown", "error_json", "result_json"),
    ],
)
def test_finish_persists_result_in_the_matching_channel(
    tmp_path, status, result_column, empty_column
):
    session_factory = create_session_factory(tmp_path / f"finish-{status}.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        create_message_instruction_items_in_session(session, raw_message_id=raw_id)
        session.commit()
    item = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert item is not None

    payload = {"status": status, "nested": {"ok": status == "succeeded"}}
    finish_message_instruction_item(
        session_factory,
        item_id=item.id,
        status=status,
        result=payload,
        now=NOW,
    )

    with session_factory() as session:
        stored = session.get(MessageInstructionItem, item.id)
        assert stored is not None
        assert stored.status == status
        assert json.loads(getattr(stored, result_column)) == payload
        assert getattr(stored, empty_column) is None


def test_finish_rejects_invalid_or_unclaimed_transitions(tmp_path):
    session_factory = create_session_factory(tmp_path / "invalid-finish.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        session.commit()
        pending_id = items[0].id

    with pytest.raises(ValueError, match="finish status"):
        finish_message_instruction_item(
            session_factory,
            item_id=pending_id,
            status="executing",
            result={},
            now=NOW,
        )
    with pytest.raises(RuntimeError, match="not executing"):
        finish_message_instruction_item(
            session_factory,
            item_id=pending_id,
            status="failed",
            result={},
            now=NOW,
        )


def test_database_bootstrap_migrates_instruction_item_indexes(tmp_path):
    database_path = tmp_path / "migration.db"
    create_session_factory(database_path)

    connection = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(message_instruction_items)"
        ).fetchall()
    }
    indexes = {
        row[1]
        for row in connection.execute(
            "PRAGMA index_list(message_instruction_items)"
        ).fetchall()
    }
    connection.close()

    assert {
        "raw_message_id",
        "signal_candidate_id",
        "sequence",
        "instruction_kind",
        "strategy_instance_id",
        "idempotency_key",
        "status",
        "result_json",
        "error_json",
    } <= columns
    assert {
        "uq_message_instruction_items_message_candidate",
        "uq_message_instruction_items_idempotency",
        "ix_message_instruction_items_message_status_sequence",
    } <= indexes
