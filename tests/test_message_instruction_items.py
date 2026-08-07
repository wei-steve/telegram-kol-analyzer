import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.dml import Update

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_instruction_items import (
    claim_message_instruction_summary,
    claim_next_message_instruction_item,
    claim_next_visibility_retry_instruction_item,
    create_message_instruction_items_in_session,
    defer_message_instruction_item_for_visibility,
    finish_message_instruction_item,
    finish_message_instruction_summary_delivery,
    list_message_instruction_item_results,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _persist_targeted_management_items(session_factory, *, same_collision=False):
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=3465, text="BTC ETH partial TP")
        session.add(raw)
        session.flush()
        envelope = ManagementMessageEnvelope(
            raw_message_id=raw.id,
            decision_fingerprint="d" * 64,
            normalized_action="partial_take_profit",
            shared_parameters_json='{"management_fraction":0.5}',
            projection_mode="live",
        )
        session.add(envelope)
        session.flush()
        rows = []
        for ordinal, symbol in enumerate(("BTC", "ETH")):
            lifecycle = StrategyLifecycle(
                chat_id=100,
                message_id=3400 + ordinal,
                symbol=symbol,
                side="short",
                lifecycle_status="entered",
                signal_at=NOW,
            )
            session.add(lifecycle)
            session.flush()
            candidate = SignalCandidate(
                raw_message_id=raw.id,
                symbol=symbol,
                side="short",
                event_type="position_update",
                target_lifecycle_id=lifecycle.id,
                management_action="partial_take_profit",
                management_fraction=0.5,
            )
            session.add(candidate)
            session.flush()
            item = MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=ordinal,
                instruction_kind="management",
                idempotency_key=(f"{ordinal + 1}" * 64)[:64],
                status="pending",
            )
            session.add(item)
            session.flush()
            collision = "a" * 64 if same_collision else chr(97 + ordinal) * 64
            target = ManagementMessageTarget(
                envelope_id=envelope.id,
                raw_message_id=raw.id,
                target_lifecycle_id=lifecycle.id,
                target_ordinal=ordinal,
                symbol=symbol,
                side="short",
                normalized_action="partial_take_profit",
                parameters_json='{"management_fraction":0.5}',
                parameter_fingerprint="p" * 64,
                collision_group_fingerprint=collision,
                admission_state="admitted",
                execution_state="pending",
                signal_candidate_id=candidate.id,
                message_instruction_item_id=item.id,
            )
            session.add(target)
            session.flush()
            rows.append((target.id, item.id))
        raw_id = raw.id
        session.commit()
    return raw_id, rows


@pytest.mark.parametrize("finish_status", ["failed", "unknown"])
def test_terminal_target_does_not_block_next_disjoint_target(
    tmp_path,
    finish_status,
):
    session_factory = create_session_factory(
        tmp_path / f"target-continue-{finish_status}.db"
    )
    raw_id, rows = _persist_targeted_management_items(session_factory)

    first = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert first is not None
    finish_message_instruction_item(
        session_factory,
        item_id=first.id,
        status=finish_status,
        result={"status": finish_status},
        now=NOW,
    )
    second = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )

    assert second is not None
    assert second.id == rows[1][1]
    with session_factory() as session:
        first_target = session.get(ManagementMessageTarget, rows[0][0])
        second_target = session.get(ManagementMessageTarget, rows[1][0])
        assert first_target.execution_state == (
            "submit_unknown" if finish_status == "unknown" else "failed"
        )
        assert second_target.execution_state == "executing"


def test_submit_unknown_blocks_only_same_collision_group(tmp_path):
    session_factory = create_session_factory(tmp_path / "target-collision.db")
    raw_id, rows = _persist_targeted_management_items(
        session_factory,
        same_collision=True,
    )
    first = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert first is not None
    finish_message_instruction_item(
        session_factory,
        item_id=first.id,
        status="unknown",
        result={"status": "submit_unknown"},
        now=NOW,
    )

    assert (
        claim_next_message_instruction_item(
            session_factory,
            raw_message_id=raw_id,
            now=NOW,
        )
        is None
    )
    with session_factory() as session:
        assert (
            session.get(ManagementMessageTarget, rows[1][0]).execution_state
            == "pending"
        )


@pytest.mark.parametrize(
    "reason",
    [
        "target_strategy_binding_not_visible_yet",
        "preceding_entry_context_unresolved",
    ],
)
def test_visibility_deferral_reasons_include_preceding_entry_context(reason):
    from telegram_kol_research.message_instruction_items import (
        should_defer_instruction_result,
    )

    assert should_defer_instruction_result(
        {"status": "deferred", "reason": reason}
    ) is True


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


def test_concurrent_item_creation_reloads_unique_conflict_winner(tmp_path):
    session_factory = create_session_factory(tmp_path / "concurrent-create.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    barrier = threading.Barrier(2)

    def synchronize_first_item_flush(session, _flush_context, _instances):
        if session.info.get("instruction_item_flush_synchronized"):
            return
        if not any(
            isinstance(item, MessageInstructionItem) for item in session.new
        ):
            return
        session.info["instruction_item_flush_synchronized"] = True
        barrier.wait(timeout=5)

    event.listen(Session, "before_flush", synchronize_first_item_flush)
    try:
        def create_items():
            with session_factory() as session:
                items = create_message_instruction_items_in_session(
                    session,
                    raw_message_id=raw_id,
                )
                session.commit()
                return [item.id for item in items]

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_ids, second_ids = list(
                pool.map(lambda _index: create_items(), range(2))
            )
    finally:
        event.remove(Session, "before_flush", synchronize_first_item_flush)

    assert first_ids == second_ids
    with session_factory() as session:
        assert session.query(MessageInstructionItem).count() == 2


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


def test_visibility_retry_keeps_item_pending_and_prevents_sequence_bypass(tmp_path):
    session_factory = create_session_factory(tmp_path / "visibility-retry.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        create_message_instruction_items_in_session(session, raw_message_id=raw_id)
        session.commit()

    management = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )
    assert management is not None
    status = defer_message_instruction_item_for_visibility(
        session_factory,
        item_id=management.id,
        result={
            "status": "deferred",
            "reason": "target_strategy_binding_not_visible_yet",
        },
        now=NOW,
    )

    assert status == "pending"
    assert (
        claim_next_message_instruction_item(
            session_factory,
            raw_message_id=raw_id,
            now=NOW + timedelta(seconds=4),
        )
        is None
    )
    retry = claim_next_visibility_retry_instruction_item(
        session_factory,
        now=NOW + timedelta(seconds=5),
    )
    assert retry is not None
    assert retry.id == management.id
    assert retry.visibility_retry_attempts == 1
    assert retry.visibility_first_failed_at is not None


def test_stale_executing_visibility_retry_is_reclaimed_after_lease(tmp_path):
    session_factory = create_session_factory(tmp_path / "visibility-stale.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        item = items[0]
        item.status = "executing"
        item.visibility_first_failed_at = NOW - timedelta(minutes=10)
        item.visibility_retry_attempts = 2
        item.visibility_next_attempt_at = NOW - timedelta(minutes=9)
        item.updated_at = NOW - timedelta(minutes=6)
        session.commit()
        item_id = item.id

    reclaimed = claim_next_visibility_retry_instruction_item(
        session_factory,
        now=NOW,
    )

    assert reclaimed is not None
    assert reclaimed.id == item_id
    assert reclaimed.status == "executing"


def test_visibility_retry_expiry_is_terminal_and_requests_notification(tmp_path):
    session_factory = create_session_factory(tmp_path / "visibility-expiry.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        item = items[0]
        item.status = "executing"
        item.visibility_first_failed_at = NOW - timedelta(hours=6)
        item.visibility_retry_attempts = 10
        session.commit()
        item_id = item.id

    status = defer_message_instruction_item_for_visibility(
        session_factory,
        item_id=item_id,
        result={"reason": "target_strategy_binding_not_visible_yet"},
        now=NOW,
    )

    assert status == "failed"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert item.visibility_next_attempt_at is None
        assert item.summary_notification_status == "pending"
        assert (
            json.loads(item.error_json)["reason"]
            == "target_strategy_binding_visibility_retry_expired"
        )
        assert json.loads(item.error_json)["priority"] == "high"


def test_expired_visibility_retry_is_failed_before_it_can_be_claimed(tmp_path):
    session_factory = create_session_factory(tmp_path / "visibility-old.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        item = items[0]
        item.status = "pending"
        item.visibility_first_failed_at = NOW - timedelta(hours=7)
        item.visibility_retry_attempts = 20
        item.visibility_next_attempt_at = NOW - timedelta(hours=6)
        session.commit()
        item_id = item.id

    assert (
        claim_next_visibility_retry_instruction_item(
            session_factory,
            now=NOW,
        )
        is None
    )
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert (
            json.loads(item.error_json)["reason"]
            == "target_strategy_binding_visibility_retry_expired"
        )


def test_racing_claim_cannot_bypass_pending_or_executing_management(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "claim-race.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        create_message_instruction_items_in_session(session, raw_message_id=raw_id)
        session.commit()

    delayed_worker_ready = threading.Event()
    release_delayed_worker = threading.Event()
    original_scalar = Query.scalar
    original_session_scalar = Session.scalar

    def pause_old_claim_gap(query):
        if threading.current_thread().name == "delayed-instruction-claimer":
            delayed_worker_ready.set()
            assert release_delayed_worker.wait(timeout=5)
        return original_scalar(query)

    def pause_atomic_claim(session, statement, *args, **kwargs):
        if (
            threading.current_thread().name == "delayed-instruction-claimer"
            and isinstance(statement, Update)
            and statement.table.name == MessageInstructionItem.__tablename__
        ):
            delayed_worker_ready.set()
            assert release_delayed_worker.wait(timeout=5)
        return original_session_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(Query, "scalar", pause_old_claim_gap)
    monkeypatch.setattr(Session, "scalar", pause_atomic_claim)
    delayed_result = []

    def delayed_claim():
        delayed_result.append(
            claim_next_message_instruction_item(
                session_factory,
                raw_message_id=raw_id,
                now=NOW,
            )
        )

    worker = threading.Thread(
        target=delayed_claim,
        name="delayed-instruction-claimer",
    )
    worker.start()
    assert delayed_worker_ready.wait(timeout=5)
    management = claim_next_message_instruction_item(
        session_factory,
        raw_message_id=raw_id,
        now=NOW,
    )
    release_delayed_worker.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert management is not None
    assert management.instruction_kind == "management"
    assert delayed_result == [None]
    with session_factory() as session:
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_id)
            .order_by(MessageInstructionItem.sequence)
            .all()
        )
        assert [item.status for item in items] == ["executing", "pending"]


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


def test_claim_never_returns_retired_pending_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "retired-pending.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_id,
        )
        items[0].retired_at = NOW
        session.commit()

    claimed = claim_next_message_instruction_item(
        session_factory,
        raw_message_id=raw_id,
        now=NOW,
    )

    assert claimed is not None
    assert claimed.instruction_kind == "entry"


def test_unknown_management_is_not_reclaimed_and_does_not_block_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-management.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session, raw_message_id=raw_id
        )
        items[0].status = "unknown"
        unknown_id = items[0].id
        session.commit()

    claimed = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_id, now=NOW
    )

    assert claimed is not None
    assert claimed.instruction_kind == "entry"
    assert claimed.id != unknown_id
    with session_factory() as session:
        assert session.get(MessageInstructionItem, unknown_id).status == "unknown"


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


def test_public_results_include_persisted_sequence_and_strategy_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "public-results.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        create_message_instruction_items_in_session(session, raw_message_id=raw_id)
        session.commit()

    results = list_message_instruction_item_results(
        session_factory,
        raw_message_id=raw_id,
    )

    assert [item["sequence"] for item in results] == [0, 1]
    assert [item["strategy_instance_id"] for item in results] == [
        "deepcoin:100:20:BTC:short",
        "deepcoin:100:55:ETH:long",
    ]


def test_terminal_message_summary_is_claimed_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "summary-claim.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_id,
        )
        for item in items:
            item.status = "succeeded"
            item.result_json = json.dumps({"status": "completed"})
        session.commit()

    first = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
        chat_title="VIP room",
    )
    second = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
        chat_title="VIP room",
    )

    assert first is not None
    assert first["chat_title"] == "VIP room"
    assert first["chat_id"] == 100
    assert first["message_id"] == 55
    assert [item["sequence"] for item in first["items"]] == [0, 1]
    assert second is None
    finish_message_instruction_summary_delivery(
        session_factory,
        claim_token=first["notification_claim_token"],
        item_ids=first["notification_item_ids"],
        delivered=True,
        completed_at=NOW,
    )

    with session_factory() as session:
        session.add(
            SignalCandidate(
                raw_message_id=raw_id,
                symbol="SOL",
                side="long",
                event_type="entry_signal",
            )
        )
        session.flush()
        items = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_id,
        )
        items[-1].status = "succeeded"
        items[-1].result_json = json.dumps({"status": "completed"})
        session.commit()

    changed = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
    )
    repeated = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
    )

    assert changed is not None
    assert len(changed["items"]) == 3
    assert repeated is None
    finish_message_instruction_summary_delivery(
        session_factory,
        claim_token=changed["notification_claim_token"],
        item_ids=changed["notification_item_ids"],
        delivered=True,
        completed_at=NOW,
    )


def test_grouped_summary_payload_keeps_refused_target_without_instruction(tmp_path):
    session_factory = create_session_factory(tmp_path / "grouped-target-summary.db")
    raw_id, rows = _persist_targeted_management_items(session_factory)
    with session_factory() as session:
        confirmed_target = session.get(ManagementMessageTarget, rows[0][0])
        confirmed_item = session.get(MessageInstructionItem, rows[0][1])
        refused_target = session.get(ManagementMessageTarget, rows[1][0])
        refused_item = session.get(MessageInstructionItem, rows[1][1])
        confirmed_target.execution_state = "confirmed"
        confirmed_item.status = "succeeded"
        confirmed_item.result_json = '{"status":"confirmed"}'
        refused_target.admission_state = "refused"
        refused_target.execution_state = "not_started"
        refused_target.closed_reason_code = "target_not_verified"
        refused_target.message_instruction_item_id = None
        refused_item.retired_at = NOW
        stale_envelope = ManagementMessageEnvelope(
            raw_message_id=raw_id,
            decision_fingerprint="e" * 64,
            normalized_action="exit_full",
            shared_parameters_json="{}",
            projection_mode="shadow",
        )
        session.add(stale_envelope)
        session.flush()
        session.add(
            ManagementMessageTarget(
                envelope_id=stale_envelope.id,
                raw_message_id=raw_id,
                target_lifecycle_id=refused_target.target_lifecycle_id,
                target_ordinal=0,
                symbol="STALE",
                side="short",
                normalized_action="exit_full",
                parameters_json="{}",
                parameter_fingerprint="q" * 64,
                collision_group_fingerprint="z" * 64,
                admission_state="refused",
                execution_state="not_started",
                closed_reason_code="stale_projection",
            )
        )
        session.commit()

    payload = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
    )

    assert payload is not None
    assert [target["symbol"] for target in payload["targets"]] == ["BTC", "ETH"]
    assert payload["targets"][0]["execution_state"] == "confirmed"
    assert payload["targets"][1]["admission_state"] == "refused"
    assert payload["targets"][1]["reason_code"] == "target_not_verified"


def test_expired_summary_delivery_claim_can_be_recovered(tmp_path):
    session_factory = create_session_factory(tmp_path / "summary-lease.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_id,
        )
        for item in items:
            item.status = "succeeded"
            item.result_json = json.dumps({"status": "completed"})
        session.commit()

    first = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
    )
    assert first is not None

    recovered = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW + timedelta(minutes=6),
    )

    assert recovered is not None
    assert recovered["notification_claim_token"] != first["notification_claim_token"]


def test_in_progress_message_summary_cannot_be_claimed(tmp_path):
    session_factory = create_session_factory(tmp_path / "summary-in-progress.db")
    raw_id, _, _, _ = _persist_dual_instruction_message(session_factory)
    with session_factory() as session:
        items = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_id,
        )
        items[0].status = "succeeded"
        session.commit()

    assert claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_id,
        claimed_at=NOW,
    ) is None


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
        "retired_at",
        "summary_notification_claimed_at",
        "summary_notification_status",
        "summary_notification_claim_token",
        "summary_notification_error",
        "summary_notified_at",
    } <= columns
    assert {
        "uq_message_instruction_items_message_candidate",
        "uq_message_instruction_items_idempotency",
        "ix_message_instruction_items_message_status_sequence",
    } <= indexes
