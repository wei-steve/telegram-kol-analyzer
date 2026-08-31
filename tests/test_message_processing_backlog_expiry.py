import hashlib
import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_processing_backlog_expiry import (
    BacklogExpiryRefused,
    apply_message_processing_backlog_expiry,
    build_message_processing_backlog_expiry_plan,
)
from telegram_kol_research.models import (
    MessageProcessingJob,
    MessageRecognition,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
)
from telegram_kol_research.telegram_live_listener import (
    _record_expired_authoritative_recovery_gap_in_session,
)


MIN_RAW_ID = 13877
WATERMARK_RAW_ID = 14030
EXPECTED_COUNT = 154
COMPLETED_AT = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)


def _seed_exact_backlog(session_factory) -> None:
    with session_factory() as session:
        for raw_id in range(MIN_RAW_ID, WATERMARK_RAW_ID + 1):
            session.add(
                RawMessage(
                    id=raw_id,
                    chat_id=-1000000000000 - (raw_id % 5),
                    message_id=raw_id,
                    text=f"message-{raw_id}",
                    posted_at=datetime(2026, 8, 30, 1, 0),
                )
            )
            session.add(
                MessageProcessingJob(
                    raw_message_id=raw_id,
                    chat_id=-1000000000000 - (raw_id % 5),
                    status="pending",
                    attempt_count=0,
                    shadow=False,
                )
            )
        session.add(
            RecognitionDecision(
                raw_message_id=13912,
                input_kind="text+image",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json='{"recognition_result":"非策略"}',
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status="execution_pending",
            )
        )
        session.commit()


def _build_plan(session_factory):
    return build_message_processing_backlog_expiry_plan(
        session_factory,
        minimum_raw_message_id=MIN_RAW_ID,
        watermark_raw_message_id=WATERMARK_RAW_ID,
        expected_pending_count=EXPECTED_COUNT,
    )


def _apply(session_factory):
    return apply_message_processing_backlog_expiry(
        session_factory,
        minimum_raw_message_id=MIN_RAW_ID,
        watermark_raw_message_id=WATERMARK_RAW_ID,
        expected_pending_count=EXPECTED_COUNT,
        completed_at=COMPLETED_AT,
    )


def test_session_scoped_expired_audit_core_never_commits(tmp_path):
    session_factory = create_session_factory(tmp_path / "core.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="stale")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        _record_expired_authoritative_recovery_gap_in_session(
            session,
            raw_message=raw,
            classification="expired_stale_instruction",
        )
        session.rollback()

    with session_factory() as session:
        assert session.query(RecognitionDecision).count() == 0
        assert session.query(MessageRecognition).count() == 0


def test_exact_backlog_plan_is_read_only_and_manifest_is_deterministic(tmp_path):
    session_factory = create_session_factory(tmp_path / "plan.db")
    _seed_exact_backlog(session_factory)

    plan = _build_plan(session_factory)

    expected_ids = tuple(range(MIN_RAW_ID, WATERMARK_RAW_ID + 1))
    expected_manifest = ("\n".join(map(str, expected_ids)) + "\n").encode()
    assert plan.target_raw_message_ids == expected_ids
    assert plan.target_manifest_sha256 == hashlib.sha256(expected_manifest).hexdigest()
    assert plan.execution_running_count == 0
    assert plan.decision_13912_preimage["comparison_status"] == "execution_pending"
    with session_factory() as session:
        assert session.query(MessageProcessingJob).filter_by(status="pending").count() == 154
        assert session.query(MessageRecognition).count() == 0
        assert session.query(RecognitionDecision).one().comparison_status == "execution_pending"


def test_apply_expires_exact_backlog_with_existing_recovery_guard_audit(tmp_path):
    session_factory = create_session_factory(tmp_path / "apply.db")
    _seed_exact_backlog(session_factory)
    with session_factory() as session:
        raw_before = tuple(
            tuple(row)
            for row in session.execute(
                RawMessage.__table__.select().order_by(RawMessage.id)
            )
        )

    result = _apply(session_factory)

    assert result.changed_count == EXPECTED_COUNT
    assert result.plan.target_raw_message_ids == tuple(
        range(MIN_RAW_ID, WATERMARK_RAW_ID + 1)
    )
    assert result.transaction_lock_seconds >= 0
    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).order_by(
            MessageProcessingJob.raw_message_id
        ).all()
        assert len(jobs) == EXPECTED_COUNT
        assert {
            (
                row.status,
                row.attempt_count,
                row.last_reason,
                row.claim_token,
                row.claimed_at,
                row.next_attempt_at,
                row.completed_at,
            )
            for row in jobs
        } == {
            (
                "expired",
                0,
                "expired_stale_instruction",
                None,
                None,
                None,
                COMPLETED_AT.replace(tzinfo=None),
            )
        }
        decisions = session.query(RecognitionDecision).all()
        assert len(decisions) == EXPECTED_COUNT
        assert {row.input_kind for row in decisions} == {"recovery_guard"}
        assert {row.authoritative_model for row in decisions} == {"recovery_guard"}
        assert {row.authoritative_status for row in decisions} == {"识别失败"}
        assert {row.agreement_status for row in decisions} == {"authoritative_failed"}
        assert {row.comparison_status for row in decisions} == {"completed"}
        assert {row.automation_status for row in decisions} == {"skipped"}
        assert {row.automation_reason for row in decisions} == {
            "authoritative_gap_recovery_expired"
        }
        assert {row.notification_status for row in decisions} == {
            "suppressed_expired_recovery"
        }
        for row in decisions:
            payload = json.loads(row.authoritative_payload_json)
            assert payload["reason"] == "authoritative_gap_recovery_expired"
            assert payload["expiry_classification"] == "expired_stale_instruction"
        recognitions = session.query(MessageRecognition).all()
        assert len(recognitions) == EXPECTED_COUNT
        assert {row.status for row in recognitions} == {"识别失败"}
        assert {row.engine for row in recognitions} == {"recovery_guard"}
        assert session.query(SignalCandidate).count() == 0
        raw_after = tuple(
            tuple(row)
            for row in session.execute(
                RawMessage.__table__.select().order_by(RawMessage.id)
            )
        )
    assert raw_after == raw_before


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (lambda row: setattr(row, "status", "claimed"), "status_not_pending"),
        (lambda row: setattr(row, "attempt_count", 1), "attempt_count_not_zero"),
        (lambda row: setattr(row, "claim_token", "claim"), "claim_present"),
        (lambda row: setattr(row, "claimed_at", COMPLETED_AT), "claim_present"),
        (lambda row: setattr(row, "shadow", True), "shadow_target_present"),
    ],
)
def test_plan_refuses_any_exact_target_guard_drift(
    tmp_path, mutation, expected_reason
):
    session_factory = create_session_factory(tmp_path / f"{expected_reason}.db")
    _seed_exact_backlog(session_factory)
    with session_factory() as session:
        row = session.query(MessageProcessingJob).filter_by(
            raw_message_id=MIN_RAW_ID
        ).one()
        mutation(row)
        session.commit()

    with pytest.raises(BacklogExpiryRefused, match=expected_reason):
        _build_plan(session_factory)


def test_plan_refuses_missing_id_and_execution_running(tmp_path):
    missing_factory = create_session_factory(tmp_path / "missing.db")
    _seed_exact_backlog(missing_factory)
    with missing_factory() as session:
        session.query(MessageProcessingJob).filter_by(
            raw_message_id=MIN_RAW_ID
        ).delete()
        session.commit()
    with pytest.raises(BacklogExpiryRefused, match="target_set_mismatch"):
        _build_plan(missing_factory)

    running_factory = create_session_factory(tmp_path / "running.db")
    _seed_exact_backlog(running_factory)
    with running_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=13912
        ).one()
        decision.comparison_status = "execution_running"
        session.commit()
    with pytest.raises(BacklogExpiryRefused, match="execution_running"):
        _build_plan(running_factory)


@pytest.mark.parametrize(
    ("minimum", "watermark", "count"),
    [
        (13878, 14030, 153),
        (13877, 14029, 153),
        (13877, 14031, 155),
        (1, 154, 154),
    ],
)
def test_plan_refuses_any_non_phase1_contract(
    tmp_path, minimum, watermark, count
):
    session_factory = create_session_factory(tmp_path / "wrong-contract.db")
    _seed_exact_backlog(session_factory)

    with pytest.raises(BacklogExpiryRefused, match="phase1_contract_mismatch"):
        build_message_processing_backlog_expiry_plan(
            session_factory,
            minimum_raw_message_id=minimum,
            watermark_raw_message_id=watermark,
            expected_pending_count=count,
        )


def test_apply_rolls_back_every_audit_and_queue_write_on_failure(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "rollback.db")
    _seed_exact_backlog(session_factory)
    import telegram_kol_research.message_processing_backlog_expiry as expiry_module

    original = expiry_module._record_expired_authoritative_recovery_gap_in_session

    def fail_mid_batch(session, *, raw_message, classification):
        if raw_message.id == 13920:
            raise RuntimeError("injected failure")
        return original(
            session,
            raw_message=raw_message,
            classification=classification,
        )

    monkeypatch.setattr(
        expiry_module,
        "_record_expired_authoritative_recovery_gap_in_session",
        fail_mid_batch,
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        _apply(session_factory)

    with session_factory() as session:
        assert session.query(MessageProcessingJob).filter_by(status="pending").count() == 154
        assert session.query(MessageProcessingJob).filter_by(status="expired").count() == 0
        assert session.query(MessageRecognition).count() == 0
        decisions = session.query(RecognitionDecision).all()
        assert len(decisions) == 1
        assert decisions[0].raw_message_id == 13912
        assert decisions[0].comparison_status == "execution_pending"
