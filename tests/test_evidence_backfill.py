from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.evidence_backfill import (
    plan_mimo_evidence_backfill,
    run_mimo_evidence_backfill,
)
from telegram_kol_research.message_evidence import (
    build_message_input_fingerprint,
    load_current_message_evidence,
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
    assert [item.raw_message_id for item in plan.items] == [
        completed,
        failed,
        changed,
        missing,
        empty,
    ]
    assert [item.status for item in plan.items] == [
        "skip_completed",
        "skip_failed",
        "process",
        "process",
        "skip_empty",
    ]
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
    assert [(item.raw_message_id, item.status) for item in resumed.items] == [
        (first, "skip_failed"),
        (second, "skip_completed"),
    ]
