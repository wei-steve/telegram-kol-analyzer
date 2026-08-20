import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import MessageProcessingJob, RawMessage
from telegram_kol_research.telegram_live_listener import (
    persist_live_message_event,
    recover_missing_authoritative_decisions,
    run_reconcile_once,
)
from telegram_kol_research.trading_settings import (
    load_trading_settings,
    save_trading_settings,
    trading_settings_from_payload,
)


BASE_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _FakeMessage:
    id = 42
    sender_id = 7
    message = "BTC 多"
    reply_to_msg_id = None
    date = BASE_NOW
    edit_date = None
    media = None

    async def get_sender(self):
        return SimpleNamespace(first_name="Alice", last_name="Trader")


class _FakeEvent:
    chat_id = 123
    message = _FakeMessage()


def _processing_result():
    return SimpleNamespace(
        recognition=SimpleNamespace(status="非策略"),
        assessment=SimpleNamespace(
            agreement_status="agreed",
            differences=[],
            mimo=SimpleNamespace(
                model="mimo-v2.5",
                status="非策略",
                payload={},
                error_message=None,
            ),
            deepseek_payload=None,
        ),
        automation={"status": "skipped", "reason": "mimo_no_action"},
    )


def _add_raw_message(
    session_factory,
    *,
    chat_id=9001,
    message_id=1,
    posted_at=BASE_NOW - timedelta(minutes=1),
):
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text="BTC 多",
            posted_at=posted_at,
        )
        session.add(raw_message)
        session.commit()
        session.refresh(raw_message)
        return raw_message.id


@pytest.mark.parametrize("mode", ["inline", "shadow"])
def test_message_pipeline_mode_round_trips_and_defaults_to_inline(tmp_path, mode):
    session_factory = create_session_factory(tmp_path / "settings.db")

    assert load_trading_settings(session_factory).message_pipeline_mode == "inline"

    saved = save_trading_settings(session_factory, {"message_pipeline_mode": mode})

    assert saved.message_pipeline_mode == mode
    assert load_trading_settings(session_factory).message_pipeline_mode == mode


@pytest.mark.parametrize("value", ["live", "disabled", True, [], {}, 1, None])
def test_message_pipeline_mode_rejects_values_that_could_enable_a_consumer(value):
    with pytest.raises(ValueError, match="message_pipeline_mode"):
        trading_settings_from_payload({"message_pipeline_mode": value})


def test_inline_default_writes_no_job_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "inline.db")

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
        )
    )

    with session_factory() as session:
        assert session.query(MessageProcessingJob).count() == 0


def test_live_shadow_job_is_pending_before_recognition_then_succeeds(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-shadow.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    observed_statuses = []

    def authoritative_processor(raw_message_id):
        with session_factory() as session:
            observed_statuses.append(
                session.query(MessageProcessingJob.status)
                .filter(MessageProcessingJob.raw_message_id == raw_message_id)
                .scalar()
            )
        return _processing_result()

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=authoritative_processor,
        )
    )

    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
        raw_message = session.query(RawMessage).one()

    assert observed_statuses == ["pending"]
    assert job.raw_message_id == raw_message.id
    assert job.chat_id == raw_message.chat_id
    assert job.status == "succeeded"
    assert job.attempt_count == 0
    assert job.shadow is True
    assert job.last_reason == "inline_completed"
    assert job.completed_at is not None


def test_live_shadow_marks_failed_and_preserves_the_inline_exception(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-failed.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})

    def authoritative_processor(_raw_message_id):
        raise RuntimeError("recognition exploded")

    with pytest.raises(RuntimeError, match="recognition exploded"):
        asyncio.run(
            persist_live_message_event(
                event=_FakeEvent(),
                session_factory=session_factory,
                broker=LiveUpdateBroker(),
                media_root=tmp_path / "media",
                ai_recognition_config=AiRecognitionConfig(),
                authoritative_processor=authoritative_processor,
            )
        )

    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()

    assert job.status == "failed"
    assert job.last_reason == "inline_error:RuntimeError"
    assert job.completed_at is not None


def test_shadow_enqueue_failure_never_breaks_live_processing(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "enqueue-failed.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    processed = []
    enqueue_calls = []

    def failing_enqueue(*args, **kwargs):
        enqueue_calls.append((args, kwargs))
        raise RuntimeError("db busy")

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener._enqueue_shadow_processing_jobs",
        failing_enqueue,
        raising=False,
    )

    result = asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=LiveUpdateBroker(),
            media_root=tmp_path / "media",
            ai_recognition_config=AiRecognitionConfig(),
            authoritative_processor=lambda raw_message_id: (
                processed.append(raw_message_id) or _processing_result()
            ),
        )
    )

    assert result["inserted_messages"] == 1
    assert len(processed) == 1
    assert len(enqueue_calls) == 1
    with session_factory() as session:
        assert session.query(MessageProcessingJob).count() == 0


def test_recovery_shadow_enqueue_is_idempotent_and_marks_success(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-shadow.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    raw_message_id = _add_raw_message(session_factory)
    observed_statuses = []

    def authoritative_processor(candidate_id):
        with session_factory() as session:
            observed_statuses.append(
                session.query(MessageProcessingJob.status)
                .filter(MessageProcessingJob.raw_message_id == candidate_id)
                .scalar()
            )
        return _processing_result()

    async def recover_once():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            now_provider=lambda: BASE_NOW,
        )

    asyncio.run(recover_once())
    asyncio.run(recover_once())

    with session_factory() as session:
        jobs = session.query(MessageProcessingJob).all()

    assert observed_statuses == ["pending", "pending"]
    assert len(jobs) == 1
    assert jobs[0].raw_message_id == raw_message_id
    assert jobs[0].status == "succeeded"
    assert jobs[0].last_reason == "recovery_completed"


def test_recovery_retry_resets_existing_terminal_job_to_pending_before_work(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-retry.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    raw_message_id = _add_raw_message(session_factory)
    with session_factory() as session:
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_message_id,
                chat_id=9001,
                status="failed",
                last_reason="recovery_error:RuntimeError",
                completed_at=BASE_NOW - timedelta(seconds=30),
                shadow=True,
            )
        )
        session.commit()
    observed_statuses = []

    def authoritative_processor(candidate_id):
        with session_factory() as session:
            observed_statuses.append(
                session.query(MessageProcessingJob.status)
                .filter(MessageProcessingJob.raw_message_id == candidate_id)
                .scalar()
            )
        return _processing_result()

    asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            now_provider=lambda: BASE_NOW,
        )
    )

    assert observed_statuses == ["pending"]


def test_recovery_shadow_marks_failed_without_changing_recovery_semantics(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-failed.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    _add_raw_message(session_factory)

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda _raw_message_id: (_ for _ in ()).throw(
                RuntimeError("claim already held")
            ),
            now_provider=lambda: BASE_NOW,
        )
    )

    assert result["recovered_messages"] == 0
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "failed"
    assert job.last_reason == "recovery_error:RuntimeError"


def test_recovery_shadow_marks_expired_without_executing(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-expired.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    raw_message_id = _add_raw_message(
        session_factory,
        posted_at=BASE_NOW - timedelta(minutes=20),
    )
    processed = []

    result = asyncio.run(
        recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
        )
    )

    assert processed == []
    assert result["expired_recovery_messages"] == 1
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.raw_message_id == raw_message_id
    assert job.status == "expired"
    assert job.last_reason == "recovery_expired:expired_stale_instruction"


def test_history_reconcile_shadow_enqueues_every_insert_before_processing(tmp_path):
    session_factory = create_session_factory(tmp_path / "history-shadow.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})
    observed_statuses = []

    async def discover_dialogs(_client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fetch_messages(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": message_id,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": f"message {message_id}",
                "posted_at": BASE_NOW - timedelta(seconds=message_id),
                "media": None,
            }
            for message_id in (77, 78)
        ]

    def authoritative_processor(raw_message_id):
        with session_factory() as session:
            observed_statuses.append(
                session.query(MessageProcessingJob.status)
                .filter(MessageProcessingJob.raw_message_id == raw_message_id)
                .scalar()
            )
        return _processing_result()

    asyncio.run(
        run_reconcile_once(
            client=object(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=discover_dialogs,
            fetch_dialog_messages_fn=fetch_messages,
        )
    )

    with session_factory() as session:
        jobs = (
            session.query(MessageProcessingJob)
            .order_by(MessageProcessingJob.raw_message_id)
            .all()
        )

    assert observed_statuses == ["pending", "pending"]
    assert len(jobs) == 2
    assert [job.status for job in jobs] == ["succeeded", "succeeded"]
    assert [job.last_reason for job in jobs] == [
        "history_reconcile_completed",
        "history_reconcile_completed",
    ]


def test_history_reconcile_shadow_marks_failed_and_preserves_exception(tmp_path):
    session_factory = create_session_factory(tmp_path / "history-failed.db")
    save_trading_settings(session_factory, {"message_pipeline_mode": "shadow"})

    async def discover_dialogs(_client):
        return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]

    async def fetch_messages(_client, _dialog, **_kwargs):
        return [
            {
                "chat_id": 9001,
                "message_id": 77,
                "sender_id": 501,
                "sender_name": "VIP BTC Room",
                "text": "message 77",
                "posted_at": BASE_NOW,
                "media": None,
            }
        ]

    with pytest.raises(RuntimeError, match="history recognition exploded"):
        asyncio.run(
            run_reconcile_once(
                client=object(),
                session_factory=session_factory,
                broker=None,
                target_titles={"VIP BTC Room"},
                authoritative_processor=lambda _raw_message_id: (_ for _ in ()).throw(
                    RuntimeError("history recognition exploded")
                ),
                discover_dialogs_fn=discover_dialogs,
                fetch_dialog_messages_fn=fetch_messages,
            )
        )

    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()

    assert job.status == "failed"
    assert job.last_reason == "history_reconcile_error:RuntimeError"
