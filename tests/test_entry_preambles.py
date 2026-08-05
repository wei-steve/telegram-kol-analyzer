import sqlite3
from datetime import UTC, datetime
from importlib.util import find_spec

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_evidence import EntryPreambleEvidence
from telegram_kol_research import models
from telegram_kol_research.models import MessageEvidenceVersion, RawMessage
from telegram_kol_research.raw_ingest import (
    normalize_message_payload,
    persist_normalized_messages,
)
from telegram_kol_research.source_message_deletion import record_source_message_deleted


def test_entry_preamble_persistence_module_exists():
    assert find_spec("telegram_kol_research.entry_preambles") is not None


def test_fresh_database_starts_with_empty_entry_preamble_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    assert hasattr(models, "EntryPreamble")
    assert hasattr(models, "EntryStrategyAssembly")
    with session_factory() as session:
        assert session.query(models.EntryPreamble).count() == 0
        assert session.query(models.EntryStrategyAssembly).count() == 0


def test_entry_preamble_status_constraint_rejects_unknown_state(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO entry_preambles (
                    raw_message_id, chat_id, message_id, symbol, side,
                    risk_multiplier, evidence_version_id,
                    recognition_generation, fingerprint, status, reason,
                    created_at, updated_at
                ) VALUES (1, 2, 3, 'BTC', 'short', '0.5', 4, 'g1', 'fp',
                          'unknown', 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )


def test_entry_preamble_lookup_index_covers_chat_status_and_created_at(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(entry_preambles)"
            ).fetchall()
        }

    assert "ix_entry_preambles_chat_status_created" in indexes


def _message_and_evidence(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-1002337721508,
            message_id=9901,
            posted_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
            text="BTC换手入场做空，半仓操作做个短线空单。",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=raw.id,
            version=1,
            input_fingerprint="sha256:preamble",
            model="mimo-v2.5",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=0.96,
            text_evidence_json="{}",
            image_evidence_json='{"images":[]}',
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.commit()
        return raw.id, evidence.id


def _half_risk_evidence():
    from decimal import Decimal

    return EntryPreambleEvidence(
        symbol="BTC",
        side="short",
        risk_multiplier=Decimal("0.5"),
        confidence=0.96,
        reason="半仓操作",
    )


def test_persist_entry_preamble_is_idempotent_for_authoritative_evidence(tmp_path):
    from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    now = datetime(2026, 8, 5, 12, 1, tzinfo=UTC)

    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        first = persist_entry_preamble_in_session(
            session,
            raw_message=raw,
            evidence_version_id=evidence_id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=now,
        )
        session.commit()
        first_id = first.id
    with session_factory() as session:
        repeated = persist_entry_preamble_in_session(
            session,
            raw_message=session.get(RawMessage, raw_id),
            evidence_version_id=evidence_id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=now,
        )
        session.commit()
        repeated_id = repeated.id

    assert repeated_id == first_id
    with session_factory() as session:
        assert session.query(models.EntryPreamble).count() == 1


def test_reassessment_supersedes_older_pending_generation(tmp_path):
    from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    now = datetime(2026, 8, 5, 12, 1, tzinfo=UTC)
    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        first = persist_entry_preamble_in_session(
            session,
            raw_message=raw,
            evidence_version_id=evidence_id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=now,
        )
        second = persist_entry_preamble_in_session(
            session,
            raw_message=raw,
            evidence_version_id=evidence_id,
            recognition_generation="generation-2",
            evidence=_half_risk_evidence(),
            now=now,
        )
        session.commit()

        assert first.status == "invalidated"
        assert first.invalidated_at == now.replace(tzinfo=None)
        assert second.status == "pending"
        assert session.query(models.EntryPreamble).filter_by(status="pending").count() == 1


def test_invalidate_changes_only_pending_entry_preamble(tmp_path):
    from telegram_kol_research.entry_preambles import (
        invalidate_pending_entry_preamble_in_session,
        persist_entry_preamble_in_session,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    now = datetime(2026, 8, 5, 12, 1, tzinfo=UTC)
    with session_factory() as session:
        row = persist_entry_preamble_in_session(
            session,
            raw_message=session.get(RawMessage, raw_id),
            evidence_version_id=evidence_id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=now,
        )
        session.flush()
        assert invalidate_pending_entry_preamble_in_session(
            session, raw_message_id=raw_id, now=now
        ) == 1
        row.status = "consumed"
        row.invalidated_at = None
        session.flush()
        assert invalidate_pending_entry_preamble_in_session(
            session, raw_message_id=raw_id, now=now
        ) == 0
        session.commit()


@pytest.mark.parametrize(
    ("mode", "expected_count"),
    [("disabled", 0), ("shadow", 1), ("live", 1)],
)
def test_authoritative_preamble_persistence_respects_rollout_mode(
    tmp_path, mode, expected_count
):
    from telegram_kol_research.entry_preambles import (
        persist_authoritative_entry_preamble,
    )

    session_factory = create_session_factory(tmp_path / f"{mode}.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)

    persisted = persist_authoritative_entry_preamble(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload={
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {"event_type": "none"},
            "entry_context": _half_risk_evidence().to_dict(),
        },
        mode=mode,
        now=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
    )

    assert (persisted is not None) is bool(expected_count)
    with session_factory() as session:
        assert session.query(models.EntryPreamble).count() == expected_count


@pytest.mark.parametrize(
    "payload",
    [
        {
            "recognition_result": "是策略",
            "strategy": {"symbol": "BTC", "side": "short", "entry": "market"},
            "lifecycle_event": {"event_type": "none"},
            "entry_context": _half_risk_evidence().to_dict(),
        },
        {
            "recognition_result": "是策略",
            "strategy": {},
            "lifecycle_event": {"event_type": "position_update"},
            "entry_context": _half_risk_evidence().to_dict(),
        },
    ],
)
def test_stray_entry_context_on_executable_message_is_not_persisted(tmp_path, payload):
    from telegram_kol_research.entry_preambles import persist_authoritative_entry_preamble

    session_factory = create_session_factory(tmp_path / "stray-context.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)

    result = persist_authoritative_entry_preamble(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload=payload,
        mode="live",
        now=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
    )

    assert result is None
    with session_factory() as session:
        assert session.query(models.EntryPreamble).count() == 0


def test_source_edit_invalidates_pending_preamble_in_same_ingest(tmp_path):
    session_factory = create_session_factory(tmp_path / "edit.db")
    persist_normalized_messages(
        session_factory,
        [
            normalize_message_payload(
                {
                    "chat_id": -1002337721508,
                    "message_id": 9901,
                    "text": "BTC做空，半仓操作",
                    "posted_at": "2026-08-05T12:00:00+00:00",
                },
                archived_target_group=True,
            )
        ],
    )
    with session_factory() as session:
        raw = session.query(RawMessage).one()
        evidence = MessageEvidenceVersion(
            raw_message_id=raw.id,
            version=1,
            input_fingerprint="sha256:before-edit",
            model="mimo-v2.5",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=0.96,
            text_evidence_json="{}",
            image_evidence_json='{"images":[]}',
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session

        persist_entry_preamble_in_session(
            session,
            raw_message=raw,
            evidence_version_id=evidence.id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
        )
        session.commit()

    persist_normalized_messages(
        session_factory,
        [
            normalize_message_payload(
                {
                    "chat_id": -1002337721508,
                    "message_id": 9901,
                    "text": "BTC做空，改为30%仓位",
                    "posted_at": "2026-08-05T12:00:00+00:00",
                    "edit_date": "2026-08-05T12:02:00+00:00",
                },
                archived_target_group=True,
            )
        ],
    )

    with session_factory() as session:
        row = session.query(models.EntryPreamble).one()
        assert row.status == "invalidated"
        assert row.invalidated_at is not None


def test_source_deletion_invalidates_only_pending_preamble(tmp_path):
    session_factory = create_session_factory(tmp_path / "delete.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    with session_factory() as session:
        persist_entry_preamble_in_session = __import__(
            "telegram_kol_research.entry_preambles",
            fromlist=["persist_entry_preamble_in_session"],
        ).persist_entry_preamble_in_session
        persist_entry_preamble_in_session(
            session,
            raw_message=session.get(RawMessage, raw_id),
            evidence_version_id=evidence_id,
            recognition_generation="generation-1",
            evidence=_half_risk_evidence(),
            now=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
        )
        session.commit()

    record_source_message_deleted(
        session_factory,
        chat_id=-1002337721508,
        message_id=9901,
        deleted_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    with session_factory() as session:
        row = session.query(models.EntryPreamble).one()
        assert row.status == "invalidated"
        assert row.invalidated_at is not None
