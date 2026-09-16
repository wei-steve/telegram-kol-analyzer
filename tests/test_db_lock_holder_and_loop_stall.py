"""The 30-second write lock and the event-loop stalls it caused.

``docs/plans/2026-09-16-db-lock-holder-and-loop-stall-analysis.md`` attributes
every stall of fifteen seconds or more between 09-13 and 09-16 to one shape of
bug: a thread holding SQLite's single write lock on connection A, and then --
still inside that transaction, still on that thread -- writing on connection B.
The inner write can only be granted once the outer one commits, and the outer
one commits only after the inner call returns. Nothing breaks the tie but
``busy_timeout``, thirty seconds later, with every other writer in the process
queued behind it.

Three things are covered here, matching sections 6.1, 6.2 and 6.3 of that
document:

* the notifications ``expire_stale_management_confirmations`` sends now happen
  after its commit, not inside its transaction;
* the strategy-management notification loop no longer runs synchronous database
  work on the event-loop thread, and no longer swallows its own exceptions;
* a guard that reports the same shape of bug anywhere else it still exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from telegram_kol_research.db import create_session_factory, statement_is_write
from telegram_kol_research.management_target_confirmation import (
    CONFIRMATION_TIMEOUT,
    expire_stale_management_confirmations,
)
from telegram_kol_research.management_target_verification import AWAITING_CONFIRMATION
from telegram_kol_research.models import (
    ExecutionEvent,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)

NOW = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
NAIVE = NOW.replace(tzinfo=None)
CHAT = -1002337721508
RAW_ID = 16975


def _awaiting_fixture(tmp_path, *, name="research.db"):
    session_factory = create_session_factory(tmp_path / name)
    with session_factory() as session:
        session.add(
            RawMessage(
                id=RAW_ID,
                chat_id=CHAT,
                message_id=RAW_ID,
                text="全部平掉",
                posted_at=NAIVE,
                created_at=NAIVE,
            )
        )
        candidate = SignalCandidate(
            raw_message_id=RAW_ID,
            symbol="BTC",
            side="long",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                id=1500,
                raw_message_id=RAW_ID,
                signal_candidate_id=int(candidate.id),
                sequence=0,
                instruction_kind="management",
                idempotency_key="item-1500",
                status=AWAITING_CONFIRMATION,
                last_progress_at=NAIVE,
                created_at=NAIVE,
                updated_at=NAIVE,
            )
        )
        session.commit()
    return session_factory


def _events(session_factory, action):
    with session_factory() as session:
        return (
            session.query(ExecutionEvent).filter(ExecutionEvent.action == action).all()
        )


class _Capture(logging.Handler):
    """Records straight off the named logger.

    Not ``caplog``: ``app_logging.configure_application_logging`` sets
    ``propagate = False`` on the ``telegram_kol_research`` logger, so once any
    test in the suite has called it nothing reaches the root handler ``caplog``
    installs -- these two cases passed alone and failed in the full run.
    Attaching here is independent of whoever ran first.
    """

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _captured(logger_name):
    handler = _Capture()
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# --------------------------------------------------------------------------
# 6.1 -- the notification leaves the transaction
# --------------------------------------------------------------------------


def test_the_confirmation_notification_is_sent_after_the_commit(tmp_path):
    """Order, not just outcome: inside the transaction is the whole bug."""

    session_factory = _awaiting_fixture(tmp_path)
    trace: list[str] = []

    class RecordingSession:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            trace.append("commit")
            return self._session.commit()

    class RecordingFactory:
        def __call__(self):
            inner = session_factory()

            class Ctx:
                def __enter__(self_inner):
                    return RecordingSession(inner.__enter__())

                def __exit__(self_inner, *exc):
                    return inner.__exit__(*exc)

            return Ctx()

    def notify(**kwargs):
        trace.append(f"notify:{kwargs['kind']}")

    result = expire_stale_management_confirmations(
        RecordingFactory(),
        now=NOW + timedelta(minutes=100),
        timeout_minutes=120,
        notify=notify,
    )

    assert result["reminded"] == (1500,)
    assert trace == ["commit", "notify:confirmation_reminder"]


def test_a_failing_notification_does_not_cost_the_committed_audit(tmp_path):
    """The audit event is the durable record; the alert is best effort."""

    session_factory = _awaiting_fixture(tmp_path)

    def exploding_notify(**_kwargs):
        raise RuntimeError("telegram down")

    with _captured(
        "telegram_kol_research.management_target_confirmation"
    ) as records:
        result = expire_stale_management_confirmations(
            session_factory,
            now=NOW + timedelta(minutes=100),
            timeout_minutes=120,
            notify=exploding_notify,
        )

    assert result["reminded"] == (1500,)
    assert len(_events(session_factory, "management_target_confirmation_reminder")) == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, 1500)
        assert json.loads(item.result_json)["confirmation_reminded_at"]
    assert any(
        "management confirmation notify failed" in record.getMessage()
        and record.exc_info
        for record in records
    )


def test_one_failing_notification_does_not_silence_the_others(tmp_path):
    session_factory = _awaiting_fixture(tmp_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                id=RAW_ID + 1,
                chat_id=CHAT,
                message_id=RAW_ID + 1,
                text="全部平掉",
                posted_at=NAIVE,
                created_at=NAIVE,
            )
        )
        candidate = SignalCandidate(
            raw_message_id=RAW_ID + 1,
            symbol="ETH",
            side="short",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                id=1501,
                raw_message_id=RAW_ID + 1,
                signal_candidate_id=int(candidate.id),
                sequence=0,
                instruction_kind="management",
                idempotency_key="item-1501",
                status=AWAITING_CONFIRMATION,
                last_progress_at=NAIVE,
                created_at=NAIVE,
                updated_at=NAIVE,
            )
        )
        session.commit()

    seen: list[int] = []

    def notify(*, raw_message_id, **_kwargs):
        seen.append(int(raw_message_id))
        if int(raw_message_id) == RAW_ID:
            raise RuntimeError("telegram down")

    with _captured("telegram_kol_research.management_target_confirmation"):
        result = expire_stale_management_confirmations(
            session_factory,
            now=NOW + timedelta(minutes=100),
            timeout_minutes=120,
            notify=notify,
        )

    assert sorted(result["reminded"]) == [1500, 1501]
    assert sorted(seen) == [RAW_ID, RAW_ID + 1]


def test_a_notifier_that_writes_on_its_own_connection_no_longer_self_locks(tmp_path):
    """The 2026-09-16 regression, on a real file database.

    Before 6.1 this notifier ran inside the open write transaction, so its own
    ``INSERT`` waited out ``busy_timeout`` and then failed. ``busy_timeout`` is
    lowered to one second here purely so the old behaviour would not make the
    suite wait thirty.
    """

    session_factory = _awaiting_fixture(tmp_path, name="self-lock.db")
    with session_factory() as session:
        session.execute(text("PRAGMA busy_timeout=1000"))
        session.commit()

    def notify(*, raw_message_id, kind, item_ids):
        # Exactly what ``_notify_confirmation_lapse`` does: a second connection.
        with session_factory() as session:
            session.add(
                RawMessage(
                    chat_id=CHAT,
                    message_id=990000 + len(item_ids),
                    text=f"incident:{kind}",
                    posted_at=NAIVE,
                    created_at=NAIVE,
                )
            )
            session.commit()

    started = time.monotonic()
    result = expire_stale_management_confirmations(
        session_factory,
        now=NOW + timedelta(minutes=100),
        timeout_minutes=120,
        notify=notify,
    )
    elapsed = time.monotonic() - started

    assert result["reminded"] == (1500,)
    # The self-lock cost a full busy_timeout; without it this is milliseconds.
    assert elapsed < 0.9
    with session_factory() as session:
        assert session.query(RawMessage).filter(RawMessage.text != "全部平掉").count() == 1
        item = session.get(MessageInstructionItem, 1500)
        assert json.loads(item.result_json)["confirmation_reminded_at"]


def test_the_timeout_branch_also_notifies_after_the_commit(tmp_path):
    session_factory = _awaiting_fixture(tmp_path)
    kinds: list[str] = []

    def notify(**kwargs):
        kinds.append(kwargs["kind"])
        with session_factory() as session:
            assert session.get(MessageInstructionItem, 1500).status == "failed"

    result = expire_stale_management_confirmations(
        session_factory,
        now=NOW + timedelta(minutes=121),
        timeout_minutes=120,
        notify=notify,
    )

    assert result["expired"] == (1500,)
    assert kinds == [CONFIRMATION_TIMEOUT]


# --------------------------------------------------------------------------
# 6.2 -- the notification loop lets go of the event loop
# --------------------------------------------------------------------------


def test_management_delivery_runs_its_database_work_off_the_event_loop(
    tmp_path, monkeypatch
):
    from telegram_kol_research import system_operator_bot as operator_bot_module
    from telegram_kol_research.models import StrategyManagementNotification

    session_factory = create_session_factory(tmp_path / "delivery.db")
    with session_factory() as session:
        session.add(
            StrategyManagementNotification(
                management_batch_id=158,
                state="recovery_required",
                payload_fingerprint="c" * 64,
                payload_json='{"batch_id":158,"state":"recovery_required"}',
                status="pending",
            )
        )
        session.commit()

    db_threads: set[int] = set()

    class ObservingFactory:
        def __call__(self):
            db_threads.add(threading.get_ident())
            return session_factory()

    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fake_send
    )

    async def scenario():
        loop_thread = threading.get_ident()
        delivered = await operator_bot_module.deliver_strategy_management_notifications(
            ObservingFactory(),
            config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
            delivery_after_id=0,
        )
        return delivered, loop_thread

    delivered, loop_thread = asyncio.run(scenario())

    assert delivered == 1
    assert len(sent) == 1
    assert db_threads, "no database session was opened at all"
    assert loop_thread not in db_threads
    with session_factory() as session:
        assert session.query(StrategyManagementNotification).one().status == "delivered"


def test_a_failed_management_delivery_also_writes_back_off_the_event_loop(
    tmp_path, monkeypatch
):
    from telegram_kol_research import system_operator_bot as operator_bot_module
    from telegram_kol_research.models import StrategyManagementNotification

    session_factory = create_session_factory(tmp_path / "delivery-failure.db")
    with session_factory() as session:
        session.add(
            StrategyManagementNotification(
                management_batch_id=159,
                state="recovery_required",
                payload_fingerprint="d" * 64,
                payload_json='{"batch_id":159,"state":"recovery_required"}',
                status="pending",
            )
        )
        session.commit()

    write_back_threads: list[int] = []
    real_mark_failed = operator_bot_module._mark_strategy_management_notification_failed

    def observing_mark_failed(*args, **kwargs):
        write_back_threads.append(threading.get_ident())
        return real_mark_failed(*args, **kwargs)

    monkeypatch.setattr(
        operator_bot_module,
        "_mark_strategy_management_notification_failed",
        observing_mark_failed,
    )

    async def failing_send(**_kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", failing_send
    )

    async def scenario():
        loop_thread = threading.get_ident()
        delivered = await operator_bot_module.deliver_strategy_management_notifications(
            session_factory,
            config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
            delivery_after_id=0,
        )
        return delivered, loop_thread

    delivered, loop_thread = asyncio.run(scenario())

    assert delivered == 0
    assert write_back_threads and loop_thread not in write_back_threads
    with session_factory() as session:
        row = session.query(StrategyManagementNotification).one()
        assert row.status == "failed"
        assert row.delivery_error == "RuntimeError"
        assert row.claim_token is None
        assert row.lease_expires_at is None


def test_a_failing_notification_loop_tick_is_logged_and_the_loop_continues(
    monkeypatch,
):
    from telegram_kol_research import system_operator_bot as operator_bot_module

    ticks: list[int] = []

    async def exploding_delivery(*_args, **_kwargs):
        ticks.append(1)
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(
        operator_bot_module,
        "deliver_strategy_management_notifications",
        exploding_delivery,
    )

    async def scenario():
        task = asyncio.create_task(
            operator_bot_module.run_strategy_management_notification_loop(
                session_factory=object(),
                config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
                interval_seconds=0.01,
            )
        )
        while len(ticks) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with _captured("telegram_kol_research.system_operator_bot") as records:
        asyncio.run(scenario())

    # It kept ticking, and it stopped being a black box.
    assert len(ticks) >= 2
    assert any(
        "strategy management notification loop tick failed" in record.getMessage()
        and record.exc_info
        for record in records
    )


# --------------------------------------------------------------------------
# 6.3 -- the guard that finds the next one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("INSERT INTO t VALUES (1)", True),
        ("  update t set a = 1", True),
        ("DELETE FROM t WHERE id = 1", True),
        ("REPLACE INTO t VALUES (1)", True),
        ("BEGIN IMMEDIATE", True),
        ("BEGIN", False),
        ("SELECT * FROM t", False),
        ("SELECT id FROM t WHERE x IN (SELECT y FROM u)", False),
        ("PRAGMA journal_mode=WAL", False),
        ("CREATE TABLE t (a int)", False),
        ("WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", True),
        ("WITH RECURSIVE a AS (SELECT 1), b AS (SELECT 2) UPDATE t SET x = 1", True),
        # A CTE *named* ``update`` must not read as one: better a missed
        # warning than a false one.
        ("WITH update AS (SELECT 1) SELECT * FROM update", False),
        ("-- why\nINSERT INTO t VALUES (1)", True),
        ("", False),
        (None, False),
    ],
)
def test_the_write_statement_test_errs_towards_silence(statement, expected):
    assert statement_is_write(statement) is expected


class _NestedWriteCapture(_Capture):
    """Only the guard's own warnings; the module logs other things too."""

    def emit(self, record):
        if "nested write on a second connection" in record.getMessage():
            self.records.append(record)


@pytest.fixture
def nested_write_warnings():
    handler = _NestedWriteCapture()
    logger = logging.getLogger("telegram_kol_research.db")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _add_message(session, message_id):
    session.add(
        RawMessage(
            chat_id=CHAT,
            message_id=message_id,
            text="x",
            posted_at=NAIVE,
            created_at=NAIVE,
        )
    )


def test_repeated_writes_on_one_connection_never_warn(tmp_path, nested_write_warnings):
    """The case that must not cry wolf, or nobody will read the log."""

    session_factory = create_session_factory(tmp_path / "same-connection.db")
    with session_factory() as session:
        for offset in range(5):
            _add_message(session, 1000 + offset)
            session.flush()
        session.commit()

    assert nested_write_warnings == []
    with session_factory() as session:
        assert session.query(RawMessage).count() == 5


def test_a_nested_read_never_warns(tmp_path, nested_write_warnings):
    session_factory = create_session_factory(tmp_path / "nested-read.db")
    with session_factory() as outer:
        _add_message(outer, 2000)
        outer.flush()
        with session_factory() as inner:
            inner.query(RawMessage).count()
        outer.commit()

    assert nested_write_warnings == []


def test_a_write_after_the_commit_never_warns(tmp_path, nested_write_warnings):
    session_factory = create_session_factory(tmp_path / "after-commit.db")
    with session_factory() as outer:
        _add_message(outer, 3000)
        outer.commit()
    with session_factory() as later:
        _add_message(later, 3001)
        later.commit()

    assert nested_write_warnings == []


def test_a_write_after_a_rollback_never_warns(tmp_path, nested_write_warnings):
    session_factory = create_session_factory(tmp_path / "after-rollback.db")
    with session_factory() as outer:
        _add_message(outer, 3100)
        outer.flush()
        outer.rollback()
    with session_factory() as later:
        _add_message(later, 3101)
        later.commit()

    assert nested_write_warnings == []


def test_a_nested_write_on_a_second_connection_warns_once_with_a_stack(
    tmp_path, nested_write_warnings
):
    """The shape of the 2026-09-16 outage, reported rather than blocked."""

    session_factory = create_session_factory(tmp_path / "nested-write.db")
    with session_factory() as session:
        session.execute(text("PRAGMA busy_timeout=200"))
        session.commit()

    with session_factory() as outer:
        _add_message(outer, 4000)
        outer.flush()
        with session_factory() as inner:
            _add_message(inner, 4001)
            # Whether the inner write wins or times out is SQLite's business;
            # the guard's business is to have said so either way.
            try:
                inner.commit()
            except Exception:
                inner.rollback()
        outer.commit()

    assert len(nested_write_warnings) == 1
    record = nested_write_warnings[0]
    assert record.levelno == logging.WARNING
    assert record.stack_info is not None
    assert "statement=" in record.getMessage()


def test_the_nested_write_warning_is_throttled_per_thread(
    tmp_path, nested_write_warnings
):
    session_factory = create_session_factory(tmp_path / "throttled.db")
    with session_factory() as session:
        session.execute(text("PRAGMA busy_timeout=200"))
        session.commit()

    for offset in range(3):
        with session_factory() as outer:
            _add_message(outer, 5000 + offset * 10)
            outer.flush()
            with session_factory() as inner:
                _add_message(inner, 5001 + offset * 10)
                try:
                    inner.commit()
                except Exception:
                    inner.rollback()
            outer.commit()

    # Three offences inside one minute, one line.
    assert len(nested_write_warnings) == 1


def test_the_guard_is_only_installed_on_sqlite():
    """Other dialects have no database-wide write lock to deadlock against."""

    from sqlalchemy import create_engine

    from telegram_kol_research.db import _install_nested_write_guard

    engine = create_engine("sqlite://", future=True)
    engine.dialect.name = "postgresql"
    _install_nested_write_guard(engine)
    assert not engine.dispatch.before_cursor_execute

    engine.dialect.name = "sqlite"
    _install_nested_write_guard(engine)
    assert engine.dispatch.before_cursor_execute
