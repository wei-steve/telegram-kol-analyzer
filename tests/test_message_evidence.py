from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_evidence import (
    load_current_message_evidence,
    save_message_evidence_version,
)
from telegram_kol_research.models import RawMessage


def _message(session_factory) -> RawMessage:
    with session_factory() as session:
        row = RawMessage(
            chat_id=-1001,
            message_id=1460,
            posted_at=datetime(2026, 7, 21, 9, 3, tzinfo=UTC),
            text="BTC 做多",
            archived_target_group=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def test_save_message_evidence_is_idempotent_for_same_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    message = _message(session_factory)

    first = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.94,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC", "side": "long"},
    )
    repeated = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.94,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC", "side": "long"},
    )

    assert first.id == repeated.id
    assert repeated.version == 1
    assert load_current_message_evidence(session_factory, message.id).id == first.id


def test_changed_input_supersedes_prior_evidence_version(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    message = _message(session_factory)

    first = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.8,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    second = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:two",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.9,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC"},
    )

    assert second.version == 2
    assert second.id != first.id
    with session_factory() as session:
        old = session.get(type(first), first.id)
        assert old.superseded_at is not None
    assert load_current_message_evidence(session_factory, message.id).id == second.id


def test_failed_extraction_can_recover_once_for_same_message_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovered.db")
    message = _message(session_factory)
    failed = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:same-input",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="failed",
        confidence=0.0,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    failed_id = failed.id

    recovered = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:same-input",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.91,
        text_evidence={"observed_text": "BTC 做多"},
        image_evidence={"images": [{"asset_id": 1}]},
        normalized_evidence={"recognition_result": "是策略"},
    )

    assert recovered.id == failed_id
    assert recovered.version == 1
    assert recovered.extraction_status == "completed"
    assert recovered.confidence == 0.91


def test_concurrent_same_input_recovery_keeps_one_completed_winner(tmp_path):
    session_factory = create_session_factory(tmp_path / "concurrent-recovery.db")
    message = _message(session_factory)
    failed = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:concurrent",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="failed",
        confidence=0.0,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    failed_id = failed.id

    def recover(label: str):
        return save_message_evidence_version(
            session_factory,
            raw_message_id=message.id,
            input_fingerprint="sha256:concurrent",
            model="mimo-v2.5",
            prompt_versions={"evidence": "v1"},
            extraction_status="completed",
            confidence=0.9,
            text_evidence={"observed_text": label},
            image_evidence={"images": []},
            normalized_evidence={"winner": label},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovered = list(executor.map(recover, ["first", "second"]))

    assert {row.id for row in recovered} == {failed_id}
    with session_factory() as session:
        rows = session.query(type(failed)).all()
        assert len(rows) == 1
        assert rows[0].extraction_status == "completed"
        assert rows[0].normalized_evidence_json in {
            '{"winner":"first"}',
            '{"winner":"second"}',
        }
