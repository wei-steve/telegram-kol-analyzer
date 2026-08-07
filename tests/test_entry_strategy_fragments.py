from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryStrategyFragment,
    MessageEvidenceVersion,
    RawMessage,
)


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def _message_and_evidence(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-1003825498321,
            message_id=559,
            posted_at=NOW,
            text="轻仓入场，50%仓位！",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=raw.id,
            version=1,
            input_fingerprint="sha256:fragment",
            model="mimo-v2.5",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=0.95,
            text_evidence_json="{}",
            image_evidence_json='{"images":[]}',
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.commit()
        return raw.id, evidence.id


def _payload(*fragments):
    return {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {"event_type": "none"},
        "entry_fragments": list(fragments),
    }


def _risk(multiplier="0.5", reason="明确50%仓位"):
    return {
        "kind": "risk_multiplier",
        "symbol": "BTC",
        "side": "long",
        "risk_multiplier": multiplier,
        "confidence": 0.95,
        "reason": reason,
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("disabled", 0), ("shadow", 1), ("live", 1)],
)
def test_authoritative_fragment_persistence_respects_mode(tmp_path, mode, expected):
    from telegram_kol_research.entry_strategy_fragments import (
        persist_authoritative_entry_fragments,
    )

    session_factory = create_session_factory(tmp_path / f"{mode}.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)

    rows = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload=_payload(_risk()),
        mode=mode,
        now=NOW,
    )

    assert len(rows) == expected
    with session_factory() as session:
        assert session.query(EntryStrategyFragment).count() == expected


def test_persisting_multiple_fragments_is_idempotent(tmp_path):
    from telegram_kol_research.entry_strategy_fragments import (
        persist_authoritative_entry_fragments,
    )

    session_factory = create_session_factory(tmp_path / "multiple.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    payload = _payload(
        _risk("1", "正常仓位操作"),
        {
            "kind": "leg_allocation",
            "symbol": "BTC",
            "side": "long",
            "allocations": ["0.5", "0.5"],
            "confidence": 0.94,
            "reason": "两个点位各半仓",
        },
    )

    first = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload=payload,
        mode="shadow",
        now=NOW,
    )
    repeated = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload=payload,
        mode="shadow",
        now=NOW,
    )

    assert [row.id for row in repeated] == [row.id for row in first]
    with session_factory() as session:
        rows = session.query(EntryStrategyFragment).order_by(EntryStrategyFragment.id).all()
        assert len(rows) == 2
        assert {row.fragment_kind for row in rows} == {
            "risk_multiplier",
            "leg_allocation",
        }


def test_new_generation_invalidates_only_older_pending_fragments(tmp_path):
    from telegram_kol_research.entry_strategy_fragments import (
        persist_authoritative_entry_fragments,
    )

    session_factory = create_session_factory(tmp_path / "generation.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)
    first = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload=_payload(_risk()),
        mode="live",
        now=NOW,
    )[0]
    with session_factory() as session:
        row = session.get(EntryStrategyFragment, first.id)
        row.status = "consumed"
        row.consumed_at = NOW
        session.commit()

    second = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-2",
        payload=_payload(_risk("1", "正常仓位操作")),
        mode="live",
        now=NOW + timedelta(seconds=1),
    )[0]

    with session_factory() as session:
        assert session.get(EntryStrategyFragment, first.id).status == "consumed"
        assert session.get(EntryStrategyFragment, second.id).status == "pending"


def test_management_event_never_persists_entry_fragments(tmp_path):
    from telegram_kol_research.entry_strategy_fragments import (
        persist_authoritative_entry_fragments,
    )

    session_factory = create_session_factory(tmp_path / "management.db")
    raw_id, evidence_id = _message_and_evidence(session_factory)

    rows = persist_authoritative_entry_fragments(
        session_factory,
        raw_message_id=raw_id,
        evidence_version_id=evidence_id,
        recognition_generation="generation-1",
        payload={
            **_payload(_risk()),
            "lifecycle_event": {"event_type": "position_update"},
        },
        mode="live",
        now=NOW,
    )

    assert rows == ()
