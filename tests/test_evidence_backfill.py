from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research import evidence_backfill as evidence_backfill_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.evidence_backfill import (
    plan_mimo_evidence_backfill,
    run_mimo_evidence_backfill,
)
from telegram_kol_research.message_evidence import (
    build_message_input_fingerprint,
    claim_message_evidence_extraction,
    load_current_message_evidence,
    message_evidence_extraction_claim_is_current,
    release_message_evidence_extraction_claim,
    save_message_evidence_version,
)
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult


def _add_message(
    session_factory,
    *,
    chat_id=-1001,
    message_id,
    posted_at,
    text="BTC 做多",
    image_path=None,
):
    with session_factory() as session:
        message = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            posted_at=posted_at,
            text=text,
            archived_target_group=True,
        )
        session.add(message)
        session.flush()
        if image_path is not None:
            session.add(
                MediaAsset(
                    raw_message_id=message.id,
                    telegram_file_id=f"image-{message_id}",
                    kind="photo",
                    mime_type="image/png",
                    local_path=str(image_path),
                )
            )
        session.commit()
        return int(message.id)


def _fingerprint(session_factory, raw_message_id, media_root):
    with session_factory() as session:
        message = session.get(RawMessage, raw_message_id)
        assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message_id)
            .all()
        )
        return build_message_input_fingerprint(
            message,
            assets,
            media_root=media_root,
        )


def _save_evidence(
    session_factory,
    raw_message_id,
    fingerprint,
    *,
    status="completed",
):
    return save_message_evidence_version(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint=fingerprint,
        model="mimo-v2.5",
        prompt_versions={"mimo": 1},
        extraction_status=status,
        confidence=0.9 if status == "completed" else 0.0,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )


def test_plan_requires_an_explicit_chat_scope(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with pytest.raises(ValueError, match="chat"):
        plan_mimo_evidence_backfill(
            session_factory,
            chat_ids=[],
            media_root=tmp_path,
        )


def test_plan_is_oldest_first_bounded_and_classifies_current_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    ignored = _add_message(
        session_factory,
        chat_id=-2002,
        message_id=1,
        posted_at=base,
    )
    completed = _add_message(
        session_factory,
        message_id=2,
        posted_at=base + timedelta(minutes=2),
    )
    failed = _add_message(
        session_factory,
        message_id=3,
        posted_at=base + timedelta(minutes=3),
    )
    changed = _add_message(
        session_factory,
        message_id=4,
        posted_at=base + timedelta(minutes=4),
    )
    missing = _add_message(
        session_factory,
        message_id=5,
        posted_at=base + timedelta(minutes=5),
    )
    empty = _add_message(
        session_factory,
        message_id=6,
        posted_at=base + timedelta(minutes=6),
        text="",
    )
    _save_evidence(
        session_factory,
        completed,
        _fingerprint(session_factory, completed, tmp_path),
    )
    _save_evidence(
        session_factory,
        failed,
        _fingerprint(session_factory, failed, tmp_path),
        status="failed",
    )
    _save_evidence(session_factory, changed, "sha256:old")

    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001, -1001],
        media_root=tmp_path,
        start_at=base + timedelta(minutes=1),
        end_at=base + timedelta(minutes=6),
        limit=10,
    )

    assert ignored not in [item.raw_message_id for item in plan.items]
    assert [item.raw_message_id for item in plan.items] == [changed, missing]
    assert [item.status for item in plan.items] == ["process", "process"]
    assert plan.skipped_completed == 1
    assert plan.skipped_failed == 1
    assert plan.skipped_empty == 1
    assert plan.chat_ids == (-1001,)

    retry_plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
        start_at=base + timedelta(minutes=3),
        end_at=base + timedelta(minutes=3),
        retry_failed=True,
    )
    assert [(item.raw_message_id, item.status) for item in retry_plan.items] == [
        (failed, "process")
    ]


def test_plan_limit_caps_model_work_without_getting_stuck_on_completed_history(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    completed = _add_message(
        session_factory,
        message_id=7,
        posted_at=base,
    )
    missing = _add_message(
        session_factory,
        message_id=8,
        posted_at=base + timedelta(minutes=1),
    )
    _save_evidence(
        session_factory,
        completed,
        _fingerprint(session_factory, completed, tmp_path),
    )

    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
        limit=1,
    )

    assert [(item.raw_message_id, item.status) for item in plan.items] == [
        (missing, "process")
    ]
    assert plan.skipped_completed == 1
    assert plan.planned == 1


def test_plan_scan_is_bounded_and_returns_a_stable_keyset_cursor(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    completed = _add_message(
        session_factory,
        message_id=9,
        posted_at=base,
    )
    missing = _add_message(
        session_factory,
        message_id=10,
        posted_at=base + timedelta(minutes=1),
    )
    _save_evidence(
        session_factory,
        completed,
        _fingerprint(session_factory, completed, tmp_path),
    )

    first_page = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
        limit=1,
        scan_limit=1,
    )
    inserted_older = _add_message(
        session_factory,
        message_id=8,
        posted_at=base - timedelta(minutes=1),
    )
    second_page = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
        limit=1,
        scan_limit=1,
        scan_cursor=first_page.next_scan_cursor,
    )
    fresh_sweep = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
        limit=1,
        scan_limit=1,
    )

    assert first_page.items == ()
    assert first_page.scanned == 1
    assert first_page.next_scan_cursor
    assert [item.raw_message_id for item in second_page.items] == [missing]
    assert second_page.next_scan_cursor
    assert [item.raw_message_id for item in fresh_sweep.items] == [inserted_older]


def test_dry_run_never_calls_mimo_or_writes_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=10,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )

    def forbidden_runner(*args, **kwargs):
        raise AssertionError("MiMo must not run during dry-run")

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=False,
        delay_seconds=0,
        mimo_runner=forbidden_runner,
    )

    assert result.mode == "dry_run"
    assert result.planned == 1
    assert result.succeeded == 0
    assert load_current_message_evidence(session_factory, raw_message_id) is None


def test_apply_rechecks_evidence_claim_before_calling_mimo(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=11,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    _save_evidence(
        session_factory,
        raw_message_id,
        _fingerprint(session_factory, raw_message_id, tmp_path),
    )

    def forbidden_runner(*args, **kwargs):
        raise AssertionError("matching evidence added after planning must be reused")

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=forbidden_runner,
    )

    assert result.succeeded == 0
    assert result.failed == 0
    assert result.skipped_completed == 1


def test_claim_prevents_duplicate_same_input_but_new_input_supersedes_it(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=12,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    first = claim_message_evidence_extraction(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint="sha256:first",
    )
    duplicate = claim_message_evidence_extraction(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint="sha256:first",
    )
    edited = claim_message_evidence_extraction(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint="sha256:edited",
    )
    release_message_evidence_extraction_claim(
        session_factory,
        raw_message_id=raw_message_id,
        claim_token=first,
    )

    assert first
    assert duplicate is None
    assert edited and edited != first
    assert not message_evidence_extraction_claim_is_current(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint="sha256:first",
        claim_token=first,
    )
    assert message_evidence_extraction_claim_is_current(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint="sha256:edited",
        claim_token=edited,
    )
    assert (
        claim_message_evidence_extraction(
            session_factory,
            raw_message_id=raw_message_id,
            input_fingerprint="sha256:edited",
        )
        is None
    )


def test_apply_skips_an_active_extraction_claim_without_calling_mimo(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=15,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    claim_token = claim_message_evidence_extraction(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint=plan.items[0].input_fingerprint,
    )

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an active extraction claim must suppress MiMo")
        ),
    )

    assert claim_token
    assert result.skipped_claimed == 1
    assert result.resume_required is True
    assert load_current_message_evidence(session_factory, raw_message_id) is None


def test_apply_discards_model_result_when_message_changes_during_inference(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=13,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )

    def editing_runner(_session_factory, *, raw_message_id, **kwargs):
        with session_factory() as session:
            raw = session.get(RawMessage, raw_message_id)
            raw.text = "ETH 已编辑"
            session.commit()
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload={
                "recognition_result": "非策略",
                "strategy": {},
                "lifecycle_event": {"event_type": "none"},
                "evidence": {"text": {}, "images": [], "conflicts": []},
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        )

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=editing_runner,
    )

    assert result.failed == 1
    assert result.rows[0]["status"] == "stale_input"
    assert result.rows[0]["error_code"] == "message_input_changed"
    assert result.resume_required is True
    assert load_current_message_evidence(session_factory, raw_message_id) is None


def test_apply_redacts_runner_exception_from_result(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _add_message(
        session_factory,
        message_id=14,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )

    def leaking_runner(*args, **kwargs):
        raise RuntimeError("Authorization: secret; raw provider response")

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=leaking_runner,
    )

    rendered = str(result.rows)
    assert "secret" not in rendered
    assert "provider response" not in rendered
    assert result.rows[0]["error_code"] == "mimo_exception"


def test_atomic_finalize_refuses_edit_and_claim_takeover_before_commit(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _add_message(
        session_factory,
        message_id=18,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    real_finalize = evidence_backfill_module.finalize_claimed_mimo_message_evidence

    def edit_then_finalize(*args, **kwargs):
        with session_factory() as session:
            raw = session.get(RawMessage, raw_message_id)
            raw.text = "edited after inference"
            session.commit()
        edited_fingerprint = _fingerprint(
            session_factory,
            raw_message_id,
            tmp_path,
        )
        assert claim_message_evidence_extraction(
            session_factory,
            raw_message_id=raw_message_id,
            input_fingerprint=edited_fingerprint,
        )
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(
        evidence_backfill_module,
        "finalize_claimed_mimo_message_evidence",
        edit_then_finalize,
    )

    def runner(_session_factory, *, raw_message_id, **kwargs):
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload={
                "recognition_result": "非策略",
                "strategy": {},
                "lifecycle_event": {"event_type": "none"},
                "evidence": {"text": {}, "images": [], "conflicts": []},
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        )

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=runner,
    )

    assert result.failed == 1
    assert result.rows[0]["status"] == "stale_claim"
    assert result.rows[0]["error_code"] == "evidence_finalize_refused"
    assert load_current_message_evidence(session_factory, raw_message_id) is None


def test_apply_continues_after_a_persistence_failure(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    first = _add_message(
        session_factory,
        message_id=16,
        posted_at=base,
    )
    second = _add_message(
        session_factory,
        message_id=17,
        posted_at=base + timedelta(minutes=1),
    )
    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    real_finalize = evidence_backfill_module.finalize_claimed_mimo_message_evidence

    def finalize(*args, raw_message_id, **kwargs):
        if raw_message_id == first:
            raise RuntimeError("database detail that must stay private")
        return real_finalize(*args, raw_message_id=raw_message_id, **kwargs)

    monkeypatch.setattr(
        evidence_backfill_module,
        "finalize_claimed_mimo_message_evidence",
        finalize,
    )

    def runner(_session_factory, *, raw_message_id, **kwargs):
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload={
                "recognition_result": "非策略",
                "strategy": {},
                "lifecycle_event": {"event_type": "none"},
                "evidence": {"text": {}, "images": [], "conflicts": []},
            },
            input_kind="text",
            model="mimo-v2.5",
            status="非策略",
        )

    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0,
        mimo_runner=runner,
    )

    assert result.failed == 1
    assert result.succeeded == 1
    assert result.rows[0]["error_code"] == "evidence_persistence_failed"
    assert "private" not in str(result.rows)
    assert load_current_message_evidence(session_factory, second) is not None


def test_apply_persists_separated_evidence_continues_after_failure_and_resumes(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = _add_message(
        session_factory,
        message_id=20,
        posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    image_path = tmp_path / "position.png"
    image_path.write_bytes(b"not-a-real-png-but-readable")
    second = _add_message(
        session_factory,
        message_id=21,
        posted_at=datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
        text="ETH 更新",
        image_path=image_path,
    )
    calls = []
    sleeps = []

    def runner(_session_factory, *, raw_message_id, **kwargs):
        calls.append(raw_message_id)
        if raw_message_id == first:
            return MimoAuthoritativeResult(
                raw_message_id=raw_message_id,
                payload={},
                input_kind="text",
                model="mimo-v2.5",
                status="识别失败",
                error_message="temporary failure",
                prompt_versions={"mimo": 1},
            )
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload={
                "recognition_result": "非策略",
                "reason": "context only",
                "summary": "update with screenshot",
                "confidence": 0.88,
                "strategy": {},
                "lifecycle_event": {"event_type": "none"},
                "evidence": {
                    "text": {
                        "observed_text": "ETH 更新",
                        "fields": {"symbol": {"value": "ETH", "source": "text"}},
                    },
                    "images": [
                        {
                            "asset_id": 1,
                            "image_type": "position_screenshot",
                            "fields": {
                                "symbol": {"value": "BTC", "source": "image"}
                            },
                            "confidence": 0.91,
                        }
                    ],
                    "conflicts": ["symbol"],
                },
            },
            input_kind="text+image",
            model="mimo-v2.5",
            status="非策略",
            prompt_versions={"mimo": 1},
        )

    plan = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    result = run_mimo_evidence_backfill(
        session_factory,
        plan=plan,
        ai_recognition_config=object(),
        media_root=tmp_path,
        apply=True,
        delay_seconds=0.25,
        mimo_runner=runner,
        sleeper=sleeps.append,
    )

    assert calls == [first, second]
    assert sleeps == [0.25]
    assert result.succeeded == 1
    assert result.failed == 1
    failed_evidence = load_current_message_evidence(session_factory, first)
    completed_evidence = load_current_message_evidence(session_factory, second)
    assert failed_evidence.extraction_status == "failed"
    assert completed_evidence.extraction_status == "completed"
    assert '"observed_text":"ETH 更新"' in completed_evidence.text_evidence_json
    assert '"image_type":"position_screenshot"' in (
        completed_evidence.image_evidence_json
    )

    resumed = plan_mimo_evidence_backfill(
        session_factory,
        chat_ids=[-1001],
        media_root=tmp_path,
    )
    assert resumed.items == ()
    assert resumed.skipped_failed == 1
    assert resumed.skipped_completed == 1
