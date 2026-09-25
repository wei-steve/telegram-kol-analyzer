"""A2 (2026-09-25): the expiry review that nobody answered closes itself out.

The pairs here are the point. Every test that shows the timeout firing has a
sibling assertion, from the same fixture and the same clock, showing it not
firing for the one row it must never touch -- because "it was not expired" and
"the sweep never reached it" are the same observation otherwise.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.lifecycle_monitor import (
    EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION,
    EXPIRY_REVIEW_AUTO_CLOSEOUT_NOTE_PREFIX,
    EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT,
    EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT_DAYS,
    LifecycleMonitor,
    LifecycleMonitorConfig,
)
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import (
    ExecutionBinding,
    StrategyLifecycle,
)
from telegram_kol_research.system_operator_bot import (
    PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND,
    PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_LISTED_MAX,
    format_pending_entry_expiry_auto_closeout_message,
)


NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=30)


def _lifecycle(
    session_factory,
    *,
    message_id: int,
    management_action: str | None = "expiry_review_requested",
    notified_at: datetime | None = None,
    next_at: datetime | None = None,
    status: str = "pending_entry",
    with_binding: bool = False,
    signal_at: datetime = LONG_AGO,
    chat_id: int = 88,
) -> int:
    with session_factory() as session:
        binding_id = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:BTC:short",
                kol_id=f"group:{chat_id}",
                chat_id=chat_id,
                message_id=message_id,
                symbol="BTC",
                side="short",
                status="active",
            )
            session.add(binding)
            session.flush()
            binding_id = binding.id
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol="BTC",
            side="short",
            lifecycle_status=status,
            signal_at=signal_at,
            entry_range_low=60300,
            entry_range_high=60800,
            stop_loss=61300,
            take_profit="59600",
            management_action=management_action,
            expiry_review_notified_at=notified_at,
            expiry_review_next_at=next_at,
            execution_binding_id=binding_id,
        )
        session.add(lifecycle)
        session.commit()
        return int(lifecycle.id)


def _monitor(session_factory, notifications, *, now: datetime = NOW):
    async def notifier(payload):
        notifications.append(payload)

    class _NoCandles(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return []

    return _NoCandles(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(max_age_hours=6),
        now_provider=lambda: now,
        expiry_review_notifier=notifier,
    )


def test_the_auto_closeout_timeout_is_seven_days():
    assert EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT_DAYS == 7
    assert EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT == timedelta(days=7)


def test_unanswered_unbound_review_is_auto_expired_and_says_it_was_automatic(
    tmp_path,
):
    """Seven days of silence, no binding: expired, and legibly not by a person."""

    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _lifecycle(
        session_factory,
        message_id=7001,
        notified_at=NOW - timedelta(days=8),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "expired"
        assert lifecycle.exit_reason == "expired"
        assert lifecycle.exited_at == NOW.replace(tzinfo=None)
        assert lifecycle.management_action == EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION
        note = lifecycle.management_note or ""
        # The note must distinguish itself from a human decision after the
        # fact. Every manual note the Telegram buttons write starts with 人工.
        assert note.startswith(EXPIRY_REVIEW_AUTO_CLOSEOUT_NOTE_PREFIX)
        assert "无人答复" in note
        assert not note.startswith("人工")
        # The record of when we asked is kept, not overwritten.
        assert lifecycle.expiry_review_notified_at == (
            NOW - timedelta(days=8)
        ).replace(tzinfo=None)

    assert len(notifications) == 1
    summary = notifications[0]
    assert summary["notification_kind"] == PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND
    assert summary["closed_count"] == 1
    assert [item["lifecycle_id"] for item in summary["closed"]] == [lifecycle_id]
    assert summary["timeout_days"] == 7


def test_unanswered_review_with_an_execution_binding_is_never_auto_expired(
    tmp_path,
):
    """The one that matters. Two rows, identical but for the binding.

    Closing out a bound lifecycle means cancelling live exchange orders. A
    clock running out may not authorize a real write, so the bound row keeps
    waiting for a person no matter how long that takes -- and the unbound row
    in the same sweep proves the sweep actually ran.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    bound_id = _lifecycle(
        session_factory,
        message_id=7101,
        notified_at=NOW - timedelta(days=30),
        with_binding=True,
    )
    unbound_id = _lifecycle(
        session_factory,
        message_id=7102,
        notified_at=NOW - timedelta(days=30),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        bound = session.get(StrategyLifecycle, bound_id)
        unbound = session.get(StrategyLifecycle, unbound_id)
        assert bound.lifecycle_status == "pending_entry"
        assert bound.management_action == "expiry_review_requested"
        assert bound.exited_at is None
        assert bound.execution_binding_id is not None
        assert unbound.lifecycle_status == "expired"
        assert unbound.management_action == EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION

    assert [item["lifecycle_id"] for item in notifications[0]["closed"]] == [
        unbound_id
    ]


def test_a_binding_attached_after_the_select_blocks_the_write(tmp_path):
    """The no-binding rule has to hold at the write, not at the read.

    ``_claim_expiry_auto_closeout`` repeats every predicate, so a binding that
    appears between the two makes the conditional UPDATE match nothing rather
    than expire a lifecycle that now has exchange orders.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _lifecycle(
        session_factory,
        message_id=7201,
        notified_at=NOW - timedelta(days=9),
    )
    with session_factory() as session:
        row = session.get(StrategyLifecycle, lifecycle_id)
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:7201:BTC:short",
            kol_id="group:88",
            chat_id=88,
            message_id=7201,
            symbol="BTC",
            side="short",
            status="active",
        )
        session.add(binding)
        session.flush()
        deadline = (NOW - EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT).replace(tzinfo=None)
        # Same row object the sweep would hold, but the database now disagrees
        # with it -- which is exactly the race the claim exists for.
        session.query(StrategyLifecycle).filter(
            StrategyLifecycle.id == lifecycle_id
        ).update(
            {StrategyLifecycle.execution_binding_id: binding.id},
            synchronize_session=False,
        )
        claimed = LifecycleMonitor._claim_expiry_auto_closeout(
            session,
            row,
            now=NOW,
            deadline=deadline,
        )
        session.commit()

    assert claimed is False
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert lifecycle.management_action == "expiry_review_requested"


def test_a_manual_continue_is_not_taken_over_by_the_timeout(tmp_path):
    """人工「继续等待」keeps its own semantics; the new path must not steal it.

    The continued row is far past the seven days and still carries a scheduled
    ``expiry_review_next_at``. It gets a fresh review request through the
    existing path -- not an automatic expiry -- while the row beside it, whose
    only difference is that nobody answered, is closed out.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    continued_id = _lifecycle(
        session_factory,
        message_id=7301,
        management_action="expiry_review_continued",
        notified_at=NOW - timedelta(days=20),
        next_at=NOW - timedelta(hours=1),
    )
    unanswered_id = _lifecycle(
        session_factory,
        message_id=7302,
        notified_at=NOW - timedelta(days=20),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        continued = session.get(StrategyLifecycle, continued_id)
        unanswered = session.get(StrategyLifecycle, unanswered_id)
        assert continued.lifecycle_status == "pending_entry"
        # Existing semantics: the due review is re-requested, exactly as before.
        assert continued.management_action == "expiry_review_requested"
        assert "上次人工选择继续等待" in (continued.management_note or "")
        assert unanswered.lifecycle_status == "expired"
        assert unanswered.management_action == EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION

    kinds = [payload.get("notification_kind") for payload in notifications]
    assert kinds.count(PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND) == 1
    review_payloads = [
        payload
        for payload in notifications
        if payload.get("notification_kind") is None
    ]
    assert [payload["lifecycle_id"] for payload in review_payloads] == [continued_id]


def test_a_review_notified_less_than_seven_days_ago_is_left_alone(tmp_path):
    """Six days is still waiting; eight is not."""

    session_factory = create_session_factory(tmp_path / "research.db")
    recent_id = _lifecycle(
        session_factory,
        message_id=7401,
        notified_at=NOW - timedelta(days=6),
    )
    overdue_id = _lifecycle(
        session_factory,
        message_id=7402,
        notified_at=NOW - timedelta(days=8),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        assert (
            session.get(StrategyLifecycle, recent_id).lifecycle_status
            == "pending_entry"
        )
        assert (
            session.get(StrategyLifecycle, overdue_id).lifecycle_status == "expired"
        )

    assert [item["lifecycle_id"] for item in notifications[0]["closed"]] == [
        overdue_id
    ]


def test_an_entered_lifecycle_is_out_of_scope_for_the_timeout(tmp_path):
    """The sweep only ever reaches ``pending_entry``."""

    session_factory = create_session_factory(tmp_path / "research.db")
    entered_id = _lifecycle(
        session_factory,
        message_id=7501,
        status="entered",
        notified_at=NOW - timedelta(days=30),
    )
    pending_id = _lifecycle(
        session_factory,
        message_id=7502,
        notified_at=NOW - timedelta(days=30),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        assert session.get(StrategyLifecycle, entered_id).lifecycle_status == "entered"
        assert session.get(StrategyLifecycle, pending_id).lifecycle_status == "expired"


def test_three_rows_are_closed_out_under_one_summary_not_three(tmp_path):
    """The batch is the unit of notification, never the individual lifecycle."""

    session_factory = create_session_factory(tmp_path / "research.db")
    ids = [
        _lifecycle(
            session_factory,
            message_id=7600 + offset,
            notified_at=NOW - timedelta(days=10 + offset),
        )
        for offset in range(3)
    ]
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    assert len(notifications) == 1
    assert notifications[0]["closed_count"] == 3
    assert sorted(
        item["lifecycle_id"] for item in notifications[0]["closed"]
    ) == sorted(ids)
    with session_factory() as session:
        assert {
            session.get(StrategyLifecycle, lifecycle_id).lifecycle_status
            for lifecycle_id in ids
        } == {"expired"}


def test_the_sweep_runs_once_a_day_even_when_new_rows_become_eligible(tmp_path):
    """A second eligible row on the same day waits for tomorrow's sweep.

    The monitor's cycle is 60 seconds, so without a day marker this path would
    notify on every cycle that anything crosses the line. The second row here
    is eligible one minute after the first sweep and must still be untouched;
    the same row on the following day must be closed out, or "once a day" would
    be indistinguishable from "never again".
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    first_id = _lifecycle(
        session_factory,
        message_id=7601,
        notified_at=NOW - timedelta(days=10),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())
    assert [item["lifecycle_id"] for item in notifications[0]["closed"]] == [
        first_id
    ]

    second_id = _lifecycle(
        session_factory,
        message_id=7602,
        notified_at=NOW - timedelta(days=9),
    )
    asyncio.run(
        monitor._request_pending_expiry_reviews(NOW + timedelta(minutes=1))
    )

    assert len(notifications) == 1
    with session_factory() as session:
        assert (
            session.get(StrategyLifecycle, second_id).lifecycle_status
            == "pending_entry"
        )

    asyncio.run(monitor._request_pending_expiry_reviews(NOW + timedelta(days=1)))

    assert len(notifications) == 2
    assert [item["lifecycle_id"] for item in notifications[1]["closed"]] == [
        second_id
    ]
    with session_factory() as session:
        assert (
            session.get(StrategyLifecycle, second_id).lifecycle_status == "expired"
        )


def test_no_summary_is_sent_when_nothing_was_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _lifecycle(
        session_factory,
        message_id=7701,
        notified_at=NOW - timedelta(days=1),
    )
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)

    asyncio.run(monitor.run_once())

    assert notifications == []


def test_without_a_notifier_nothing_is_auto_expired(tmp_path):
    """No channel to announce it, no silent close-out."""

    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _lifecycle(
        session_factory,
        message_id=7801,
        notified_at=NOW - timedelta(days=30),
    )

    class _NoCandles(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return []

    monitor = _NoCandles(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(max_age_hours=6),
        now_provider=lambda: NOW,
    )

    asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert lifecycle.management_action == "expiry_review_requested"


def test_the_summary_message_names_every_lifecycle_and_says_it_was_automatic():
    text = format_pending_entry_expiry_auto_closeout_message(
        {
            "notification_kind": PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND,
            "timeout_days": EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT_DAYS,
            "closed_at": NOW,
            "closed_count": 2,
            "closed": [
                {
                    "lifecycle_id": 909,
                    "chat_id": -100123,
                    "message_id": 8200,
                    "symbol": "ETH",
                    "side": "long",
                    "signal_at": LONG_AGO,
                },
                {
                    "lifecycle_id": 910,
                    "chat_id": -100124,
                    "message_id": 8201,
                    "symbol": "BTC",
                    "side": "short",
                    "signal_at": LONG_AGO,
                },
            ],
        }
    )

    assert "超时自动收口" in text
    assert "不是人工判定" in text
    assert "7 天无人答复" in text
    assert "本轮自动标记过期: 2 条" in text
    assert "909" in text and "910" in text
    assert "ETH long" in text and "BTC short" in text
    # It is not a review request, so it must not read like one.
    assert "请确认如何处理" not in text
    # Nothing was dropped, so nothing claims to have been.
    assert "未逐条列出" not in text


def test_a_long_batch_keeps_the_count_exact_and_stays_under_telegram_limit():
    """The first real batch is the standing backlog, not one day's worth.

    Telegram rejects a message over 4096 characters and a rejected send loses
    the whole notification, so the list is capped. The count above it must stay
    exact, and the message must say how many it did not name.
    """

    over = PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_LISTED_MAX + 12
    text = format_pending_entry_expiry_auto_closeout_message(
        {
            "notification_kind": PENDING_ENTRY_EXPIRY_AUTO_CLOSEOUT_KIND,
            "timeout_days": EXPIRY_REVIEW_AUTO_CLOSEOUT_TIMEOUT_DAYS,
            "closed_at": NOW,
            "closed_count": over,
            "closed": [
                {
                    "lifecycle_id": 1000 + index,
                    "chat_id": -1001234567890,
                    "message_id": 20000 + index,
                    "symbol": "ETH",
                    "side": "long",
                    "signal_at": LONG_AGO,
                }
                for index in range(over)
            ],
        }
    )

    assert f"本轮自动标记过期: {over} 条" in text
    # The first listed row is named; the last one, past the cap, is not.
    assert "内部ID 1000 " in text
    assert f"内部ID {1000 + over - 1} " not in text
    assert "…另有 12 条未逐条列出" in text
    assert len(text) < 4096
