import importlib.util
from datetime import UTC, datetime, timedelta

import pytest

import telegram_kol_research.semantic_review_control as control
from telegram_kol_research.db import (
    create_existing_session_factory,
    create_session_factory,
)
from telegram_kol_research.models import RawMessage, RecognitionDecision
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 22, 10, 0)


def test_semantic_review_control_module_exists():
    assert importlib.util.find_spec(
        "telegram_kol_research.semantic_review_control"
    ) is not None


def _seed_decision(
    session_factory,
    *,
    message_id: int,
    comparison_status: str,
    agreement_status: str = "pending",
    updated_at: datetime | None = None,
):
    with session_factory() as session:
        raw = RawMessage(chat_id=7, message_id=message_id, text=f"message {message_id}")
        session.add(raw)
        session.flush()
        row = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload_json='{"recognition_result":"非策略"}',
            agreement_status=agreement_status,
            differences_json="[]",
            automation_status="skipped",
            automation_reason="fixture",
            prompt_versions_json='{"mimo":1}',
            comparison_status=comparison_status,
            comparison_error=(
                "402 Payment Required" if comparison_status == "failed" else None
            ),
            comparison_attempts=3 if comparison_status == "failed" else 0,
            comparison_next_attempt_at=(
                NOW + timedelta(minutes=5)
                if comparison_status == "pending"
                else None
            ),
            comparison_started_at=(
                NOW - timedelta(minutes=1)
                if comparison_status == "running"
                else None
            ),
            comparison_claim_token=(
                "running-token" if comparison_status == "running" else None
            ),
            created_at=NOW - timedelta(hours=1),
            updated_at=updated_at or NOW - timedelta(minutes=10),
        )
        session.add(row)
        session.commit()
        return raw.id


def test_disable_plan_is_deterministic_read_only_and_targets_only_pending_failed(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    pending_id = _seed_decision(
        session_factory, message_id=1, comparison_status="pending"
    )
    failed_id = _seed_decision(
        session_factory, message_id=2, comparison_status="failed"
    )
    _seed_decision(session_factory, message_id=3, comparison_status="running")
    _seed_decision(
        session_factory,
        message_id=4,
        comparison_status="completed",
        agreement_status="agreed",
    )
    _seed_decision(
        session_factory,
        message_id=5,
        comparison_status="completed",
        agreement_status="authoritative_failed",
    )
    _seed_decision(
        session_factory,
        message_id=6,
        comparison_status="completed",
        agreement_status="review_disabled",
    )
    with session_factory() as session:
        before = [
            (row.raw_message_id, row.comparison_status, row.updated_at)
            for row in session.query(RecognitionDecision)
            .order_by(RecognitionDecision.raw_message_id)
            .all()
        ]

    first = control.build_semantic_review_disable_plan(
        session_factory,
        cutoff=NOW,
    )
    second = control.build_semantic_review_disable_plan(
        session_factory,
        cutoff=NOW,
    )

    assert [target.raw_message_id for target in first.targets] == [
        pending_id,
        failed_id,
    ]
    assert first.status_counts == {
        "completed": 3,
        "failed": 1,
        "pending": 1,
        "running": 1,
    }
    assert first.running_count == 1
    assert len(first.plan_sha) == 64
    assert first.plan_sha == second.plan_sha
    assert first.targets == second.targets
    assert first.quick_check == "ok"
    assert first.provider_call_count == 0
    assert first.notification_count == 0
    assert first.exchange_write_count == 0
    assert all(len(target.row_fingerprint) == 64 for target in first.targets)
    with session_factory() as session:
        after = [
            (row.raw_message_id, row.comparison_status, row.updated_at)
            for row in session.query(RecognitionDecision)
            .order_by(RecognitionDecision.raw_message_id)
            .all()
        ]
    assert after == before


def test_existing_session_factory_refuses_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_existing_session_factory(tmp_path / "missing.db")


def _decision_snapshot(session_factory, raw_message_id: int) -> dict[str, object]:
    with session_factory() as session:
        row = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_message_id
        ).one()
        return {
            column.name: getattr(row, column.name)
            for column in RecognitionDecision.__table__.columns
        }


def test_apply_requires_disabled_setting_exact_sha_and_no_running_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "guarded.db")
    _seed_decision(session_factory, message_id=1, comparison_status="pending")
    running_id = _seed_decision(
        session_factory, message_id=2, comparison_status="running"
    )
    plan = control.build_semantic_review_disable_plan(session_factory, cutoff=NOW)

    with pytest.raises(control.SemanticReviewControlError, match="running"):
        control.apply_semantic_review_disable_plan(
            session_factory,
            plan,
            expected_plan_sha=plan.plan_sha,
            applied_at=NOW,
        )

    with session_factory() as session:
        running = session.query(RecognitionDecision).filter_by(
            raw_message_id=running_id
        ).one()
        running.comparison_status = "completed"
        running.agreement_status = "agreed"
        running.comparison_started_at = None
        running.comparison_claim_token = None
        session.commit()
    plan = control.build_semantic_review_disable_plan(session_factory, cutoff=NOW)

    with pytest.raises(control.SemanticReviewControlError, match="SHA"):
        control.apply_semantic_review_disable_plan(
            session_factory,
            plan,
            expected_plan_sha="0" * 64,
            applied_at=NOW,
        )

    save_trading_settings(session_factory, {"semantic_review_enabled": True})
    with pytest.raises(control.SemanticReviewControlError, match="enabled"):
        control.apply_semantic_review_disable_plan(
            session_factory,
            plan,
            expected_plan_sha=plan.plan_sha,
            applied_at=NOW,
        )


def test_apply_preserves_audit_fields_and_fresh_plan_is_empty(tmp_path):
    session_factory = create_session_factory(tmp_path / "apply.db")
    pending_id = _seed_decision(
        session_factory, message_id=1, comparison_status="pending"
    )
    failed_id = _seed_decision(
        session_factory, message_id=2, comparison_status="failed"
    )
    before = {
        raw_id: _decision_snapshot(session_factory, raw_id)
        for raw_id in (pending_id, failed_id)
    }
    plan = control.build_semantic_review_disable_plan(session_factory, cutoff=NOW)
    applied_at = NOW + timedelta(minutes=1)

    result = control.apply_semantic_review_disable_plan(
        session_factory,
        plan,
        expected_plan_sha=plan.plan_sha,
        applied_at=applied_at,
    )

    assert result.changed_count == 2
    assert len(result.post_apply_sha) == 64
    assert result.provider_call_count == 0
    assert result.notification_count == 0
    assert result.exchange_write_count == 0
    changed_fields = {
        "comparison_status",
        "agreement_status",
        "comparison_next_attempt_at",
        "comparison_started_at",
        "comparison_claim_token",
        "updated_at",
    }
    for raw_id in (pending_id, failed_id):
        after = _decision_snapshot(session_factory, raw_id)
        assert after["comparison_status"] == "completed"
        assert after["agreement_status"] == "review_disabled"
        assert after["comparison_next_attempt_at"] is None
        assert after["comparison_started_at"] is None
        assert after["comparison_claim_token"] is None
        assert after["updated_at"] == applied_at
        assert {
            key: value for key, value in after.items() if key not in changed_fields
        } == {
            key: value
            for key, value in before[raw_id].items()
            if key not in changed_fields
        }
    fresh = control.build_semantic_review_disable_plan(
        session_factory,
        cutoff=applied_at + timedelta(seconds=1),
    )
    assert fresh.targets == ()
    repeated = control.apply_semantic_review_disable_plan(
        session_factory,
        plan,
        expected_plan_sha=plan.plan_sha,
        applied_at=applied_at + timedelta(minutes=1),
    )
    assert repeated.changed_count == 0
    assert repeated.post_apply_sha == result.post_apply_sha


def test_apply_refuses_plan_from_another_database(tmp_path):
    first_factory = create_session_factory(tmp_path / "first.db")
    second_factory = create_session_factory(tmp_path / "second.db")
    _seed_decision(first_factory, message_id=1, comparison_status="pending")
    _seed_decision(second_factory, message_id=1, comparison_status="pending")
    plan = control.build_semantic_review_disable_plan(first_factory, cutoff=NOW)

    with pytest.raises(control.SemanticReviewControlError, match="database"):
        control.apply_semantic_review_disable_plan(
            second_factory,
            plan,
            expected_plan_sha=plan.plan_sha,
            applied_at=NOW + timedelta(minutes=1),
        )


def test_repeated_apply_fingerprint_survives_sqlite_utc_timestamp_reload(tmp_path):
    session_factory = create_session_factory(tmp_path / "utc-reload.db")
    raw_id = _seed_decision(
        session_factory, message_id=1, comparison_status="failed"
    )
    plan = control.build_semantic_review_disable_plan(session_factory, cutoff=NOW)

    first = control.apply_semantic_review_disable_plan(
        session_factory,
        plan,
        expected_plan_sha=plan.plan_sha,
        applied_at=datetime(2026, 8, 22, 18, 0, tzinfo=UTC),
    )
    with session_factory() as session:
        reloaded = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        assert reloaded.updated_at.tzinfo is None
    repeated = control.apply_semantic_review_disable_plan(
        session_factory,
        plan,
        expected_plan_sha=plan.plan_sha,
        applied_at=datetime(2026, 8, 22, 18, 1, tzinfo=UTC),
    )

    assert repeated.changed_count == 0
    assert repeated.post_apply_sha == first.post_apply_sha


def test_apply_detects_drift_and_rolls_back_all_targets(tmp_path):
    session_factory = create_session_factory(tmp_path / "drift.db")
    first_id = _seed_decision(
        session_factory, message_id=1, comparison_status="pending"
    )
    second_id = _seed_decision(
        session_factory, message_id=2, comparison_status="failed"
    )
    plan = control.build_semantic_review_disable_plan(session_factory, cutoff=NOW)
    with session_factory() as session:
        drifted = session.query(RecognitionDecision).filter_by(
            raw_message_id=second_id
        ).one()
        drifted.comparison_attempts += 1
        drifted.updated_at = NOW - timedelta(minutes=2)
        session.commit()

    with pytest.raises(control.SemanticReviewControlError, match="drift"):
        control.apply_semantic_review_disable_plan(
            session_factory,
            plan,
            expected_plan_sha=plan.plan_sha,
            applied_at=NOW + timedelta(minutes=1),
        )

    assert _decision_snapshot(session_factory, first_id)["comparison_status"] == "pending"


def test_targeted_rollback_restores_only_fields_changed_by_apply(tmp_path):
    session_factory = create_session_factory(tmp_path / "rollback.db")
    pending_id = _seed_decision(
        session_factory, message_id=1, comparison_status="pending"
    )
    failed_id = _seed_decision(
        session_factory, message_id=2, comparison_status="failed"
    )
    before = {
        raw_id: _decision_snapshot(session_factory, raw_id)
        for raw_id in (pending_id, failed_id)
    }
    disable_plan = control.build_semantic_review_disable_plan(
        session_factory, cutoff=NOW
    )
    control.apply_semantic_review_disable_plan(
        session_factory,
        disable_plan,
        expected_plan_sha=disable_plan.plan_sha,
        applied_at=NOW + timedelta(minutes=1),
    )
    rollback_plan = control.build_semantic_review_rollback_plan(
        session_factory, preimage_plan=disable_plan
    )

    result = control.apply_semantic_review_rollback_plan(
        session_factory,
        rollback_plan,
        expected_plan_sha=rollback_plan.plan_sha,
    )

    assert result.changed_count == 2
    assert len(result.post_rollback_sha) == 64
    assert result.provider_call_count == 0
    assert result.notification_count == 0
    assert result.exchange_write_count == 0
    for raw_id in (pending_id, failed_id):
        assert _decision_snapshot(session_factory, raw_id) == before[raw_id]


def test_rollback_refuses_one_row_drift_atomically(tmp_path):
    session_factory = create_session_factory(tmp_path / "rollback-drift.db")
    first_id = _seed_decision(
        session_factory, message_id=1, comparison_status="pending"
    )
    second_id = _seed_decision(
        session_factory, message_id=2, comparison_status="failed"
    )
    disable_plan = control.build_semantic_review_disable_plan(
        session_factory, cutoff=NOW
    )
    control.apply_semantic_review_disable_plan(
        session_factory,
        disable_plan,
        expected_plan_sha=disable_plan.plan_sha,
        applied_at=NOW + timedelta(minutes=1),
    )
    rollback_plan = control.build_semantic_review_rollback_plan(
        session_factory, preimage_plan=disable_plan
    )
    with session_factory() as session:
        drifted = session.query(RecognitionDecision).filter_by(
            raw_message_id=second_id
        ).one()
        drifted.comparison_error = "changed after rollback planning"
        drifted.updated_at = NOW + timedelta(minutes=2)
        session.commit()

    with pytest.raises(control.SemanticReviewControlError, match="drift"):
        control.apply_semantic_review_rollback_plan(
            session_factory,
            rollback_plan,
            expected_plan_sha=rollback_plan.plan_sha,
        )

    assert _decision_snapshot(session_factory, first_id)["agreement_status"] == (
        "review_disabled"
    )
