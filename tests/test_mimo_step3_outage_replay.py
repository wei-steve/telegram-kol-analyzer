"""step-18 step 3: what an MiMo provider outage gives back, and what it may do.

On 2026-09-12 every message in fourteen hours failed recognition and then aged
out of the 15-minute recovery window for good -- the window measured the
outage, not our processing. The ruling of that day decides what happens now:

* the recovery window does not count the time the provider refused a message;
* after an announced recovery, auto_trade messages the outage delayed are
  queued again, oldest first, exactly once;
* an entry among them is never executed late -- a person is told instead;
* a management instruction among them executes only while young (effective
  age within 15 minutes) and while its target position is still open;
  otherwise a person decides.

Each gate is tested with one fixture and one difference -- delayed or not --
and both outcomes, so the difference is shown to be the cause.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research import auto_trade_execution
from telegram_kol_research import provider_outage_replay as replay
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_processing_worker import (
    MessageProcessingClaim,
    _classify_claim_expiry,
)
from telegram_kol_research.mimo_recognition_runs import (
    record_mimo_attempt,
    start_mimo_run,
)
from telegram_kol_research.models import RawMessage, RuntimeIncident
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_terminal_authoritative_decision,
    update_recognition_execution_outcome,
)
from telegram_kol_research.system_operator_bot import (
    format_runtime_incident_notification,
)
from telegram_kol_research.telegram_live_listener import (
    run_authoritative_gap_recovery_loop,
)


T0 = datetime(2026, 9, 12, 3, 0, 0)  # naive UTC, as SQLite returns it
BALANCE = "mimo_provider_unavailable.insufficient_balance.http_402"
AUTO_CHAT = -1001
QUIET_CHAT = -1002


def _worker_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_ROLE", "worker")
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED", "true")


def _factory(tmp_path, name="replay.db"):
    return create_session_factory(tmp_path / name)


def _raw(session_factory, *, raw_id, chat_id=AUTO_CHAT, posted_at=T0, text="BTC 77000 多"):
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_id,
                chat_id=chat_id,
                message_id=raw_id,
                text=text,
                posted_at=posted_at,
            )
        )
        session.commit()
    return raw_id


def _attempt(session_factory, *, raw_id, at, status="http_error", error_code=BALANCE):
    run = start_mimo_run(
        session_factory,
        raw_message_id=raw_id,
        run_kind="v1_authoritative",
        contract_version="v1",
        model="mimo-v2.5",
        input_kind="text",
        input_fingerprint="fp",
        prompt_versions={},
        started_at=at,
    )
    record_mimo_attempt(
        session_factory,
        run_id=run.id,
        ordinal=1,
        status=status,
        error_code=None if status == "completed" else error_code,
        error_message=None if status == "completed" else "failed",
        duration_ms=0,
        started_at=at,
        completed_at=at,
        attempt_phase="v1_authoritative",
    )


def _failed_decision(session_factory, *, raw_id, reason="mimo_authoritative_failed"):
    save_terminal_authoritative_decision(
        session_factory,
        RecognitionDecisionRecord(
            raw_message_id=raw_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="识别失败",
            authoritative_payload={},
            auxiliary_model=None,
            auxiliary_status=None,
            auxiliary_payload=None,
            agreement_status="authoritative_failed",
            differences=[],
            prompt_versions={},
        ),
    )
    update_recognition_execution_outcome(
        session_factory,
        raw_message_id=raw_id,
        automation_status="skipped",
        automation_reason=reason,
    )


def _incidents(session_factory, incident_type):
    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == incident_type)
            .all()
        )


def _utc(value):
    return value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Effective age
# --------------------------------------------------------------------------


def test_the_outage_is_not_counted_in_a_delayed_messages_age(tmp_path):
    """Posted 03:00, refused from 03:01 to 17:00, provider answers at 17:00.

    At 17:05 the message is 14h05m old by the clock, and 6 minutes old by
    the part of its life the provider was actually available to it.
    """

    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)
    _attempt(session_factory, raw_id=1, at=T0 + timedelta(minutes=1))
    _attempt(session_factory, raw_id=1, at=T0 + timedelta(hours=13))
    _raw(session_factory, raw_id=2, posted_at=T0 + timedelta(hours=14))
    _attempt(session_factory, raw_id=2, at=T0 + timedelta(hours=14), status="completed")

    verdict = replay.replay_verdict(
        session_factory, raw_message_id=1, now=_utc(T0 + timedelta(hours=14, minutes=5))
    )

    assert verdict.delayed is True
    assert verdict.effective_age == timedelta(minutes=6)


def test_a_message_the_provider_never_refused_keeps_its_plain_age(tmp_path):
    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)
    _attempt(session_factory, raw_id=1, at=T0 + timedelta(minutes=1), status="completed")

    verdict = replay.replay_verdict(
        session_factory, raw_message_id=1, now=_utc(T0 + timedelta(hours=14))
    )

    assert verdict.delayed is False
    assert replay.effective_message_age(
        posted_at=T0, now=T0 + timedelta(hours=14), span=None
    ) == timedelta(hours=14)


def test_an_unfinished_outage_does_not_age_the_message(tmp_path):
    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)
    _attempt(session_factory, raw_id=1, at=T0 + timedelta(minutes=2))

    verdict = replay.replay_verdict(
        session_factory, raw_message_id=1, now=_utc(T0 + timedelta(hours=5))
    )

    assert verdict.effective_age == timedelta(minutes=2)


def test_the_outage_starts_at_its_earliest_failure_whatever_the_id_order():
    """Chat lanes finish in parallel, so a later id can carry an earlier time.

    Found by the replay test below: taking "the last row read" as the start put
    it a minute late, and the message whose attempt finished first was left
    out of the replay.
    """

    from telegram_kol_research.mimo_provider_health import derive_provider_outage

    rows_newest_first = [
        ("completed", None, T0 + timedelta(hours=14)),
        ("http_error", BALANCE, T0 + timedelta(hours=1, minutes=4)),
        ("http_error", BALANCE, T0 + timedelta(hours=1, minutes=2)),
        ("http_error", BALANCE, T0 + timedelta(hours=1, minutes=3)),
    ]

    outage = derive_provider_outage(rows_newest_first)

    assert outage.started_at == _utc(T0 + timedelta(hours=1, minutes=2))
    assert outage.last_failure_at == _utc(T0 + timedelta(hours=1, minutes=4))
    assert outage.failures == 3


# --------------------------------------------------------------------------
# Parking delayed management items before they are claimed
# --------------------------------------------------------------------------


def _instruction_item(session_factory, *, raw_id, kind, status="pending", item_id=1):
    from telegram_kol_research.models import MessageInstructionItem, SignalCandidate

    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=raw_id,
            event_type="entry_signal" if kind == "entry" else "position_management",
            target_lifecycle_id=None if kind == "entry" else 7,
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                id=item_id,
                raw_message_id=raw_id,
                signal_candidate_id=candidate.id,
                sequence=item_id,
                instruction_kind=kind,
                idempotency_key=f"key-{item_id}",
                status=status,
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "delayed,kind,status,parked",
    [
        (True, "management", "pending", True),
        (False, "management", "pending", False),
        (True, "entry", "pending", False),
        (True, "management", "executing", False),
    ],
)
def test_only_pending_delayed_management_items_that_may_not_replay_are_parked(
    tmp_path, monkeypatch, delayed, kind, status, parked
):
    from telegram_kol_research import management_target_verification as mtv

    session_factory = _factory(tmp_path)
    if delayed:
        _delayed_young_message(session_factory)
    else:
        _raw(session_factory, raw_id=1)
    _instruction_item(session_factory, raw_id=1, kind=kind, status=status)
    calls = []
    monkeypatch.setattr(
        mtv,
        "request_management_target_confirmation",
        lambda factory, **kwargs: calls.append(kwargs) or (1,),
    )

    moved = replay.hold_delayed_management_for_confirmation(
        session_factory,
        raw_message_id=1,
        now=_utc(T0 + timedelta(hours=14, minutes=40)),  # effective age 36 min
    )

    assert bool(calls) is parked
    assert moved == ((1,) if parked else ())
    if parked:
        assert calls[0]["raw_message_id"] == 1
        assert calls[0]["snapshot_stale"] is False


# --------------------------------------------------------------------------
# The recovery window (worker expiry)
# --------------------------------------------------------------------------


def _claim(raw_id):
    return MessageProcessingClaim(
        job_id=1, raw_message_id=raw_id, chat_id=AUTO_CHAT, attempt_count=0, claim_token="t"
    )


@pytest.mark.parametrize("delayed,expired", [(True, False), (False, True)])
def test_a_replayed_message_is_not_expired_on_its_first_claim(tmp_path, delayed, expired):
    """Same message, same clock; only whether the provider refused it differs."""

    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)
    _attempt(
        session_factory,
        raw_id=1,
        at=T0 + timedelta(minutes=1),
        status="http_error" if delayed else "completed",
        error_code=BALANCE if delayed else None,
    )
    _raw(session_factory, raw_id=2, posted_at=T0 + timedelta(hours=14))
    _attempt(session_factory, raw_id=2, at=T0 + timedelta(hours=14), status="completed")

    result = _classify_claim_expiry(
        session_factory,
        claim=_claim(1),
        now=_utc(T0 + timedelta(hours=14, minutes=5)),
        loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
    )

    assert (result is not None) is expired


def test_a_delayed_message_still_expires_once_its_effective_age_is_past_the_window(
    tmp_path,
):
    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)
    _attempt(session_factory, raw_id=1, at=T0 + timedelta(minutes=1))
    _raw(session_factory, raw_id=2, posted_at=T0 + timedelta(hours=14))
    _attempt(session_factory, raw_id=2, at=T0 + timedelta(hours=14), status="completed")

    result = _classify_claim_expiry(
        session_factory,
        claim=_claim(1),
        now=_utc(T0 + timedelta(hours=14, minutes=20)),
        loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
    )

    assert result is not None


# --------------------------------------------------------------------------
# Management replay eligibility
# --------------------------------------------------------------------------


def _delayed_young_message(session_factory, raw_id=1):
    _raw(session_factory, raw_id=raw_id)
    _attempt(session_factory, raw_id=raw_id, at=T0 + timedelta(minutes=1))
    _raw(session_factory, raw_id=raw_id + 100, posted_at=T0 + timedelta(hours=14))
    _attempt(
        session_factory,
        raw_id=raw_id + 100,
        at=T0 + timedelta(hours=14),
        status="completed",
    )
    return _utc(T0 + timedelta(hours=14, minutes=5))


def test_a_delayed_management_instruction_past_the_window_is_not_replayed(tmp_path):
    session_factory = _factory(tmp_path)
    _delayed_young_message(session_factory)

    assert replay.management_replay_allowed(
        session_factory,
        raw_message_id=1,
        target_lifecycle_ids=[7],
        now=_utc(T0 + timedelta(hours=14, minutes=30)),
    ) == (False, replay.MANAGEMENT_TOO_OLD)


def test_an_unreadable_position_snapshot_is_not_an_open_position(tmp_path):
    """No reconcile round recorded: "we do not know" does not execute."""

    session_factory = _factory(tmp_path)
    now = _delayed_young_message(session_factory)

    assert replay.management_replay_allowed(
        session_factory, raw_message_id=1, target_lifecycle_ids=[7], now=now
    ) == (False, replay.MANAGEMENT_SNAPSHOT_STALE)


@pytest.mark.parametrize(
    "verified,expected",
    [
        (True, (True, replay.MANAGEMENT_TARGET_OPEN)),
        (False, (False, replay.MANAGEMENT_TARGET_NOT_OPEN)),
    ],
)
def test_a_young_delayed_instruction_replays_only_on_an_open_position(
    tmp_path, monkeypatch, verified, expected
):
    from telegram_kol_research import management_target_verification as mtv

    session_factory = _factory(tmp_path)
    now = _delayed_young_message(session_factory)
    monkeypatch.setattr(
        mtv, "load_verified_position_ids", lambda session, **kwargs: frozenset({"pos-1"})
    )
    monkeypatch.setattr(
        mtv,
        "verify_lifecycle_targets",
        lambda session, ids, **kwargs: {
            int(i): SimpleNamespace(verified=verified) for i in ids
        },
    )

    assert replay.management_replay_allowed(
        session_factory, raw_message_id=1, target_lifecycle_ids=[7], now=now
    ) == expected


def test_a_message_the_outage_never_touched_is_not_gated(tmp_path):
    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=1)

    assert replay.management_replay_allowed(
        session_factory,
        raw_message_id=1,
        target_lifecycle_ids=[7],
        now=_utc(T0 + timedelta(hours=20)),
    ) == (True, None)


# --------------------------------------------------------------------------
# The execution gates (ruling: entries never replay; management conditionally)
# --------------------------------------------------------------------------


class _Reached(Exception):
    pass


def _entry_candidate(raw_message):
    return (
        raw_message,
        SimpleNamespace(
            id=11,
            symbol="BTC",
            side="long",
            entry_text="77000-77500",
            stop_loss_text="75700",
            take_profit_text="80000",
        ),
        None,
        False,
    )


@pytest.mark.parametrize("delayed", [True, False])
def test_a_delayed_entry_is_refused_and_announced_and_a_normal_one_is_not(
    tmp_path, monkeypatch, delayed
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)
    if delayed:
        now = _delayed_young_message(session_factory)
    else:
        _raw(session_factory, raw_id=1)
        now = _utc(T0 + timedelta(minutes=1))
    with session_factory() as session:
        raw_message = session.get(RawMessage, 1)
        session.expunge(raw_message)
    events = []
    monkeypatch.setattr(
        auto_trade_execution, "_load_best_management_candidate", lambda *a, **k: None
    )
    monkeypatch.setattr(
        auto_trade_execution,
        "_load_best_entry_candidate",
        lambda *a, **k: _entry_candidate(raw_message),
    )
    monkeypatch.setattr(
        auto_trade_execution, "record_execution_event", lambda _f, event: events.append(event)
    )

    def reached(**kwargs):
        raise _Reached()

    monkeypatch.setattr(
        auto_trade_execution, "validate_candidate_entry_price_geometry", reached
    )

    if not delayed:
        with pytest.raises(_Reached):
            auto_trade_execution._auto_process_single_message_trade_signal(
                session_factory,
                raw_message_id=1,
                group_config=SimpleNamespace(groups=()),
                deepcoin_client=None,
                processed_at=now,
                instruction_kind="entry",
            )
        assert _incidents(session_factory, "provider_outage_entry_not_replayed") == []
        return

    result = auto_trade_execution._auto_process_single_message_trade_signal(
        session_factory,
        raw_message_id=1,
        group_config=SimpleNamespace(groups=()),
        deepcoin_client=None,
        processed_at=now,
        instruction_kind="entry",
    )

    assert result == {"status": "skipped", "reason": replay.ENTRY_NOT_REPLAYED}
    assert [event.reason for event in events] == [replay.ENTRY_NOT_REPLAYED]
    incidents = _incidents(session_factory, "provider_outage_entry_not_replayed")
    assert len(incidents) == 1
    text = format_runtime_incident_notification(incidents[0])
    assert "故障期间的入场未执行" in text
    assert "77000-77500" in text
    assert "BTC 77000 多" in text
    assert f"群: {AUTO_CHAT}" in text


def _management_candidate(raw_message):
    return (
        raw_message,
        SimpleNamespace(
            id=21,
            symbol="BTC",
            side="long",
            event_type="position_management",
            management_action="partial_close",
            management_contract_json=None,
            management_contract_fingerprint=None,
            target_lifecycle_id=7,
        ),
        None,
        False,
    )


@pytest.mark.parametrize("delayed", [True, False])
def test_a_delayed_management_instruction_that_may_not_replay_is_refused(
    tmp_path, monkeypatch, delayed
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)
    if delayed:
        _delayed_young_message(session_factory)
        now = _utc(T0 + timedelta(hours=14, minutes=40))  # effective age 36 min
    else:
        _raw(session_factory, raw_id=1)
        now = _utc(T0 + timedelta(minutes=1))
    with session_factory() as session:
        raw_message = session.get(RawMessage, 1)
        session.expunge(raw_message)
    monkeypatch.setattr(
        auto_trade_execution,
        "_load_best_management_candidate",
        lambda *a, **k: _management_candidate(raw_message),
    )
    monkeypatch.setattr(
        auto_trade_execution, "_is_informational_management_candidate", lambda c: False
    )
    monkeypatch.setattr(
        auto_trade_execution,
        "load_trading_settings",
        lambda _f: SimpleNamespace(
            management_planning_enabled=True,
            effective_composite_management_v2_mode="off",
        ),
    )
    monkeypatch.setattr(
        auto_trade_execution, "_composite_management_gate_reason", lambda c, s: None
    )
    monkeypatch.setattr(
        auto_trade_execution, "record_execution_event", lambda _f, event: None
    )

    def reached(*args, **kwargs):
        raise _Reached()

    monkeypatch.setattr(auto_trade_execution, "_auto_process_management_signal", reached)

    call = lambda: auto_trade_execution._auto_process_single_message_trade_signal(  # noqa: E731
        session_factory,
        raw_message_id=1,
        group_config=SimpleNamespace(groups=()),
        deepcoin_client=object(),
        processed_at=now,
    )
    if not delayed:
        with pytest.raises(_Reached):
            call()
        assert _incidents(session_factory, "provider_outage_management_not_replayed") == []
        return

    result = call()

    assert result == {"status": "skipped", "reason": replay.MANAGEMENT_TOO_OLD}
    incidents = _incidents(session_factory, "provider_outage_management_not_replayed")
    assert len(incidents) == 1
    assert "扣除故障时长后仍超过 15 分钟" in format_runtime_incident_notification(
        incidents[0]
    )


def test_an_entry_refusal_finishes_as_a_refusal_not_a_failure():
    """A skipped result must finish cleanly; a parked-after-claim item would not."""

    from telegram_kol_research.instruction_execution_outcomes import (
        legacy_status_for_instruction_result,
    )

    assert legacy_status_for_instruction_result(
        {"status": "skipped", "reason": replay.ENTRY_NOT_REPLAYED},
        intent_kind="entry",
        enforcement_mode="disabled",
    ) == "succeeded"


# --------------------------------------------------------------------------
# The replay after recovery
# --------------------------------------------------------------------------


def _announced_outage(session_factory, *, recovered_at):
    """Record the recovery notice the replay waits for."""

    from telegram_kol_research.mimo_provider_health import load_latest_provider_outage

    outage = load_latest_provider_outage(session_factory)
    assert outage is not None and outage.recovered_at is not None
    with session_factory() as session:
        session.add(
            RuntimeIncident(
                source_kind="mimo_provider",
                source_record_id=outage.key,
                incident_type="mimo_provider_recovered",
                severity="high",
                fingerprint="f" * 64,
                generation=1,
                redacted_summary="{}",
                status="pending",
                repeat_count=1,
                first_occurred_at=recovered_at,
                last_occurred_at=recovered_at,
                notification_status="pending",
                recovery_status="not_requested",
                feature_policy_version="v",
                prompt_version="v",
                tool_policy_version="v",
                created_at=recovered_at,
                updated_at=recovered_at,
            )
        )
        session.commit()
    return outage


def _outage_with_three_messages(session_factory):
    # Two auto_trade messages whose id order is the reverse of their posted
    # order (raw 3 was posted first), and one message in a quiet group. The
    # replay must follow the posted order, so it queues [3, 2]; ordering by id
    # would queue [2, 3].
    _raw(session_factory, raw_id=3, posted_at=T0 + timedelta(minutes=10))
    _raw(session_factory, raw_id=2, posted_at=T0 + timedelta(minutes=30))
    _raw(session_factory, raw_id=4, chat_id=QUIET_CHAT, posted_at=T0 + timedelta(minutes=5))
    for raw_id in (3, 2, 4):
        _attempt(session_factory, raw_id=raw_id, at=T0 + timedelta(hours=1, minutes=raw_id))
        _failed_decision(session_factory, raw_id=raw_id)
    _raw(session_factory, raw_id=99, posted_at=T0 + timedelta(hours=14))
    _attempt(session_factory, raw_id=99, at=T0 + timedelta(hours=14), status="completed")


def _modes(chat_id):
    return "auto_trade" if chat_id == AUTO_CHAT else "notify_only"


def test_nothing_is_replayed_before_the_recovery_is_announced(tmp_path):
    session_factory = _factory(tmp_path)
    _outage_with_three_messages(session_factory)

    assert replay.run_provider_outage_replay_tick(
        session_factory, group_trading_mode_provider=_modes
    ) == {"state": "recovery_not_yet_announced"}


def test_the_delayed_auto_trade_messages_are_queued_once_oldest_first(
    tmp_path, monkeypatch
):
    _worker_env(monkeypatch)
    session_factory = _factory(tmp_path)
    _outage_with_three_messages(session_factory)
    _announced_outage(session_factory, recovered_at=T0 + timedelta(hours=14))
    queued = []

    def enqueue(factory, **kwargs):
        queued.append(kwargs)

    first = replay.run_provider_outage_replay_tick(
        session_factory,
        group_trading_mode_provider=_modes,
        now=_utc(T0 + timedelta(hours=14, minutes=1)),
        enqueue=enqueue,
    )

    assert first == {"state": "replay_enqueued", "messages": 2}
    assert queued == [
        {
            "raw_message_ids": [3, 2],
            "last_reason": replay.REPLAY_QUEUE_REASON,
            "resume_terminal_jobs": True,
        }
    ]
    started = _incidents(session_factory, "provider_outage_replay_started")
    assert len(started) == 1
    assert json.loads(started[0].redacted_summary)["retry_count"] == 2
    assert "补做消息: 2 条" in format_runtime_incident_notification(started[0])

    # The replay ran: each message now has a run after the recovery.
    for raw_id in (2, 3):
        _attempt(session_factory, raw_id=raw_id, at=T0 + timedelta(hours=14, minutes=2), status="completed")

    second = replay.run_provider_outage_replay_tick(
        session_factory,
        group_trading_mode_provider=_modes,
        now=_utc(T0 + timedelta(hours=14, minutes=3)),
        enqueue=enqueue,
    )

    assert second == {"state": "nothing_to_replay"}
    assert len(queued) == 1
    assert len(_incidents(session_factory, "provider_outage_replay_started")) == 1


def test_the_executor_parks_delayed_management_before_claiming_items(monkeypatch):
    """The order is the property: an item parked after its claim cannot be
    finished (the finish requires ``executing`` and raises otherwise), so the
    park has to run before the executor looks at instruction items at all."""

    from telegram_kol_research import source_message_deletion

    calls = []
    monkeypatch.setattr(
        source_message_deletion,
        "source_execution_barrier",
        lambda *args, **kwargs: SimpleNamespace(status="allow", reason=None),
    )
    monkeypatch.setattr(
        replay,
        "hold_delayed_management_for_confirmation",
        lambda factory, **kwargs: calls.append("park") or (),
    )

    def has_items(factory, **kwargs):
        calls.append("items")
        return False

    monkeypatch.setattr(auto_trade_execution, "has_message_instruction_items", has_items)
    monkeypatch.setattr(
        auto_trade_execution,
        "_auto_process_single_message_trade_signal",
        lambda *args, **kwargs: calls.append("execute") or {"status": "skipped", "reason": "x"},
    )

    auto_trade_execution.auto_process_message_trade_signal(
        object(),
        raw_message_id=1,
        group_config=SimpleNamespace(groups=()),
        deepcoin_client=None,
        processed_at=_utc(T0),
    )

    assert calls == ["park", "items", "execute"]


def test_without_a_group_mode_provider_nothing_is_replayed_and_it_says_so(tmp_path):
    session_factory = _factory(tmp_path)
    _outage_with_three_messages(session_factory)
    _announced_outage(session_factory, recovered_at=T0 + timedelta(hours=14))

    assert replay.run_provider_outage_replay_tick(
        session_factory, group_trading_mode_provider=None
    ) == {"state": "no_group_mode_provider"}


def test_a_message_that_failed_for_another_reason_is_not_the_outages_to_give_back(
    tmp_path,
):
    session_factory = _factory(tmp_path)
    _raw(session_factory, raw_id=2, posted_at=T0 + timedelta(minutes=10))
    _attempt(
        session_factory,
        raw_id=2,
        at=T0 + timedelta(hours=1),
        error_code="mimo_request_rejected.http_400",
    )
    _failed_decision(session_factory, raw_id=2)

    assert replay.select_replay_candidates(
        session_factory,
        auto_trade_chat_ids=[AUTO_CHAT],
        since=_utc(T0),
        recovered_at=_utc(T0 + timedelta(hours=14)),
    ) == []


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_loop_runs_the_replay_by_default_and_web_app_gives_it_the_group_modes():
    parameters = inspect.signature(run_authoritative_gap_recovery_loop).parameters
    assert (
        parameters["provider_outage_replay_tick"].default
        is replay.run_provider_outage_replay_tick
    )
    assert "group_trading_mode_provider" in parameters

    source = inspect.getsource(web_app_module)
    start = source.index("app.state.authoritative_gap_recovery_runner(")
    call = source[start : source.index("add_done_callback", start)]
    assert "group_trading_mode_provider=" in call


def test_one_loop_iteration_passes_the_group_modes_to_the_replay(tmp_path):
    session_factory = _factory(tmp_path)
    seen = []

    def replay_tick(factory, **kwargs):
        seen.append(kwargs)
        raise asyncio.CancelledError()

    async def run_once():
        with pytest.raises(asyncio.CancelledError):
            await run_authoritative_gap_recovery_loop(
                session_factory=session_factory,
                authoritative_processor=None,
                chat_titles_by_id_provider=lambda: {},
                interval_seconds=0,
                provider_health_tick=None,
                provider_outage_replay_tick=replay_tick,
                group_trading_mode_provider=_modes,
            )

    asyncio.run(run_once())

    assert seen == [{"group_trading_mode_provider": _modes}]


def test_the_three_replay_types_can_never_be_silenced_by_an_env_line():
    for incident_type in (
        "provider_outage_entry_not_replayed",
        "provider_outage_management_not_replayed",
        "provider_outage_replay_started",
    ):
        assert incident_type in ALWAYS_NOTIFIED_INCIDENT_TYPES, incident_type
