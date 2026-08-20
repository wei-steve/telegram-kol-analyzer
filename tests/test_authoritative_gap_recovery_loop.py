"""Phase 3 Task 4: regression tests for the extracted gap-recovery loop.

Covers:
1. Gap recovery runs without any Telegram client present.
2. A message missing a decision is recovered within one fast-loop interval.
3. Recovery is bounded per pass.
4. A message older than the window is not executed, is recorded, and is
   classified correctly for both the stalled and the healthy case.
5. Stall-induced expiry notifications are rate limited.
6. ``run_reconcile_once`` behavior is unchanged by the extraction.
"""

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, RecognitionDecision, utc_now
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_live_listener import (
    EXPIRED_AFTER_SYSTEM_STALL,
    EXPIRED_STALE_INSTRUCTION,
    StallExpiryNotificationRateLimiter,
    recover_missing_authoritative_decisions,
    run_authoritative_gap_recovery_loop,
    run_reconcile_once,
)


BASE_NOW = datetime(2026, 8, 19, 12, 0, 0)


class _FakeClient:
    pass


async def _fake_discover_dialogs(client):
    return [{"id": 9001, "title": "VIP BTC Room", "archived": True}]


async def _no_messages(client, dialog, limit, media_root="data/media"):
    return []


def _fake_processing_result():
    """A minimal stand-in shaped like the real authoritative processing result.

    ``_build_authoritative_notification_payload`` unconditionally reads
    ``.assessment`` off whatever the processor returns, so every fake
    ``authoritative_processor`` used against the recovery path needs one of
    these rather than a bare ``None``.
    """

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


def _add_raw_message(session_factory, *, chat_id=9001, message_id=1, posted_at, text="BTC 多"):
    with session_factory() as session:
        message = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            posted_at=posted_at,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return message.id


# ── 1. no Telegram client anywhere in the call graph ──


def test_recover_missing_authoritative_decisions_has_no_client_parameter():
    """The whole point of the extraction: no Telegram dependency at all."""

    signature = inspect.signature(recover_missing_authoritative_decisions)
    assert "client" not in signature.parameters
    assert "discover_dialogs_fn" not in signature.parameters


def test_gap_recovery_runs_without_any_telegram_client(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-client.db")
    raw_message_id = _add_raw_message(session_factory, posted_at=BASE_NOW - timedelta(minutes=1))

    processed: list[int] = []

    def authoritative_processor(raw_message_id: int):
        processed.append(raw_message_id)
        return _fake_processing_result()

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            now_provider=lambda: BASE_NOW,
        )

    result = asyncio.run(scenario())

    assert processed == [raw_message_id]
    assert result["recovered_messages"] == 1
    assert result["expired_recovery_messages"] == 0


# ── 2. recovered within one fast-loop interval ──


def test_message_recovered_within_one_fast_loop_interval(tmp_path):
    session_factory = create_session_factory(tmp_path / "fast-loop.db")
    # run_authoritative_gap_recovery_loop has no now_provider of its own - it
    # always resolves recover_missing_authoritative_decisions's real-clock
    # default, so the fixture message must be recent by wall-clock time, not
    # relative to the fixed BASE_NOW used elsewhere in this file.
    _add_raw_message(session_factory, posted_at=utc_now())

    processed: list[int] = []

    def authoritative_processor(raw_message_id: int):
        processed.append(raw_message_id)
        return _fake_processing_result()

    async def scenario():
        loop = asyncio.get_running_loop()

        def provider():
            return {9001: "VIP BTC Room"}

        task = loop.create_task(
            run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=authoritative_processor,
                chat_titles_by_id_provider=provider,
                interval_seconds=0.01,
            )
        )
        try:
            deadline = loop.time() + 3.0
            while not processed and loop.time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert processed == [1]


# ── 3. bounded per pass ──


def test_recovery_is_bounded_per_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "bounded.db")
    for index in range(5):
        _add_raw_message(
            session_factory,
            message_id=index + 1,
            posted_at=BASE_NOW - timedelta(minutes=1, seconds=index),
        )

    processed: list[int] = []

    def authoritative_processor(raw_message_id: int):
        processed.append(raw_message_id)
        return _fake_processing_result()

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            message_limit=2,
            now_provider=lambda: BASE_NOW,
        )

    result = asyncio.run(scenario())

    assert result["recovered_messages"] == 2
    assert len(processed) == 2


# ── 4. expiry classification: stalled vs healthy ──


def test_expired_message_classified_stale_when_no_stall_recorded(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired-healthy.db")
    raw_message_id = _add_raw_message(
        session_factory, posted_at=BASE_NOW - timedelta(minutes=20)
    )

    processed: list[int] = []

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
        )

    result = asyncio.run(scenario())

    assert processed == []  # never executed
    assert result["expired_recovery_messages"] == 1
    assert result[EXPIRED_STALE_INSTRUCTION] == 1
    assert result[EXPIRED_AFTER_SYSTEM_STALL] == 0
    with session_factory() as session:
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one()
        )
    assert decision.automation_reason == "authoritative_gap_recovery_expired"
    assert decision.automation_status == "skipped"


def test_expired_message_classified_stall_when_stall_overlaps_window(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired-stalled.db")
    _add_raw_message(session_factory, posted_at=BASE_NOW - timedelta(minutes=20))

    processed: list[int] = []
    stall_at = (BASE_NOW - timedelta(minutes=18)).isoformat()

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=processed.append,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": stall_at},
        )

    result = asyncio.run(scenario())

    assert processed == []  # still never auto-executed - expiry stays fail-safe
    assert result[EXPIRED_AFTER_SYSTEM_STALL] == 1
    assert result[EXPIRED_STALE_INSTRUCTION] == 0


def test_expired_message_with_no_posted_at_defaults_to_stale(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired-no-posted-at.db")
    _add_raw_message(session_factory, posted_at=None)
    stall_at = (BASE_NOW - timedelta(minutes=1)).isoformat()

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda raw_message_id: None,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": stall_at},
        )

    result = asyncio.run(scenario())

    assert result[EXPIRED_STALE_INSTRUCTION] == 1
    assert result[EXPIRED_AFTER_SYSTEM_STALL] == 0


# ── 5. stall-induced notifications are rate limited ──


def test_stall_expiry_notifications_bounded_within_one_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "notify-bounded.db")
    for index in range(4):
        _add_raw_message(
            session_factory,
            message_id=index + 1,
            posted_at=BASE_NOW - timedelta(minutes=20, seconds=index),
        )

    sent: list[dict] = []

    async def sender(*, config, payload):
        sent.append(payload)

    stall_at = (BASE_NOW - timedelta(minutes=18)).isoformat()

    async def scenario():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda raw_message_id: None,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": stall_at},
            system_operator_bot_config=SystemOperatorBotConfig(
                bot_token="tok", chat_id="chat"
            ),
            stall_expiry_notification_sender=sender,
        )

    result = asyncio.run(scenario())

    assert result[EXPIRED_AFTER_SYSTEM_STALL] == 4
    # One notification for the whole burst, not one per expired message.
    assert len(sent) == 1
    assert sent[0]["expired_count"] == 4


def test_stall_expiry_notifications_rate_limited_across_passes(tmp_path):
    """Two separate recovery passes, each finding a *newly appeared*
    stall-expired message (a realistic shape: the fast loop ticks every 20s,
    and a stall can keep expiring fresh backlog across several ticks). The
    rate limiter must still collapse them into far fewer notifications than
    expired messages, and must allow a fresh one once its window has passed.
    """

    session_factory = create_session_factory(tmp_path / "notify-rate-limited.db")
    _add_raw_message(session_factory, message_id=1, posted_at=BASE_NOW - timedelta(minutes=20))

    sent: list[dict] = []

    async def sender(*, config, payload):
        sent.append(payload)

    stall_at = (BASE_NOW - timedelta(minutes=18)).isoformat()
    clock = {"value": 0.0}
    limiter = StallExpiryNotificationRateLimiter(
        min_interval_seconds=300.0,
        monotonic=lambda: clock["value"],
    )
    config = SystemOperatorBotConfig(bot_token="tok", chat_id="chat")

    async def pass_once():
        return await recover_missing_authoritative_decisions(
            session_factory,
            chat_titles_by_id={9001: "VIP BTC Room"},
            authoritative_processor=lambda raw_message_id: None,
            now_provider=lambda: BASE_NOW,
            loop_lag_snapshot_provider=lambda: {"last_stall_at": stall_at},
            system_operator_bot_config=config,
            stall_expiry_notification_sender=sender,
            expiry_notification_rate_limiter=limiter,
        )

    async def scenario():
        await pass_once()  # message 1 is newly expired-stalled: notifies once
        assert len(sent) == 1

        # A second, genuinely new message shows up shortly after (10s later,
        # well inside the 300s window) - it IS newly eligible, but the
        # limiter must still suppress a second notification this soon.
        clock["value"] = 10.0
        _add_raw_message(
            session_factory, message_id=2, posted_at=BASE_NOW - timedelta(minutes=21)
        )
        await pass_once()
        assert len(sent) == 1

        # Once the window has fully elapsed, a further new message is free
        # to notify again.
        clock["value"] = 301.0
        _add_raw_message(
            session_factory, message_id=3, posted_at=BASE_NOW - timedelta(minutes=22)
        )
        await pass_once()
        assert len(sent) == 2

    asyncio.run(scenario())


def test_rate_limiter_allows_a_second_notification_after_the_window():
    clock = {"value": 0.0}
    limiter = StallExpiryNotificationRateLimiter(
        min_interval_seconds=300.0,
        monotonic=lambda: clock["value"],
    )
    assert limiter.should_notify() is True
    clock["value"] = 100.0
    assert limiter.should_notify() is False
    clock["value"] = 301.0
    assert limiter.should_notify() is True


# ── 6. run_reconcile_once behavior unchanged by the extraction ──


def test_run_reconcile_once_behavior_unchanged_by_extraction(tmp_path):
    session_factory = create_session_factory(tmp_path / "reconcile-unchanged.db")
    recoverable_id = _add_raw_message(
        session_factory,
        message_id=1,
        posted_at=utc_now(),
    )
    _add_raw_message(
        session_factory,
        message_id=2,
        posted_at=datetime(2026, 4, 10, 9, 0),
    )

    processed: list[int] = []

    def authoritative_processor(raw_message_id: int):
        processed.append(raw_message_id)
        return _fake_processing_result()

    async def scenario():
        return await run_reconcile_once(
            client=_FakeClient(),
            session_factory=session_factory,
            broker=None,
            target_titles={"VIP BTC Room"},
            authoritative_processor=authoritative_processor,
            discover_dialogs_fn=_fake_discover_dialogs,
            fetch_dialog_messages_fn=_no_messages,
        )

    result = asyncio.run(scenario())

    assert processed == [recoverable_id]
    assert result["recovered_messages"] == 1
    assert result["expired_recovery_messages"] == 1
    with session_factory() as session:
        expired_decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id != recoverable_id)
            .one()
        )
    assert expired_decision.authoritative_model == "recovery_guard"
    assert expired_decision.automation_reason == "authoritative_gap_recovery_expired"
    assert expired_decision.notification_status == "suppressed_expired_recovery"
