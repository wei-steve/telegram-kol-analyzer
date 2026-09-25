"""乙 (2026-09-25): the expiry review only asks about what we might trade.

Of the 31 unfinished lifecycles in production that day, 5 were in a group that
places orders on a symbol the global whitelist allows. The other 26 asked a
person to decide the fate of a strategy nobody was ever going to execute -- and
one of them (军长's HBAR) named a symbol the executor refuses outright.

Every test here pairs the narrowing with what it must not touch: the in-scope
row that still notifies in the same sweep, and the out-of-scope row that still
notifies because the exchange may still hold an order for it.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import telegram_kol_research.lifecycle_monitor as lifecycle_monitor_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.lifecycle_monitor import (
    EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION,
    EXPIRY_OUT_OF_SCOPE_CLOSEOUT_NOTE_PREFIX,
    EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION,
    LifecycleMonitor,
    LifecycleMonitorConfig,
)
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=30)

AUTO_TRADE_CHAT = 101
NOTIFY_ONLY_CHAT = 202
UNCONFIGURED_CHAT = 303
TRADING_MODES = {
    AUTO_TRADE_CHAT: "auto_trade",
    NOTIFY_ONLY_CHAT: "notify_only",
}


def _session_factory(tmp_path, *, allowed_symbols: str = "BTC,ETH,SOL"):
    session_factory = create_session_factory(tmp_path / "research.db")
    # The production whitelist on 2026-09-25, stated rather than defaulted:
    # this is the value the scope gate must read, and reading the YAML's
    # per-group list instead would be a different (and wrong) answer.
    save_trading_settings(session_factory, {"allowed_symbols": allowed_symbols})
    return session_factory


def _lifecycle(
    session_factory,
    *,
    message_id: int,
    chat_id: int,
    symbol: str = "BTC",
    status: str = "pending_entry",
    management_action: str | None = None,
    notified_at: datetime | None = None,
    next_at: datetime | None = None,
    with_binding: bool = False,
    signal_at: datetime = LONG_AGO,
) -> int:
    with session_factory() as session:
        binding_id = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:{symbol}:long",
                kol_id=f"group:{chat_id}",
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side="long",
                status="active",
            )
            session.add(binding)
            session.flush()
            binding_id = binding.id
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side="long",
            lifecycle_status=status,
            signal_at=signal_at,
            entry_range_low=100,
            entry_range_high=110,
            stop_loss=90,
            take_profit="130",
            management_action=management_action,
            expiry_review_notified_at=notified_at,
            expiry_review_next_at=next_at,
            execution_binding_id=binding_id,
        )
        session.add(lifecycle)
        session.commit()
        return int(lifecycle.id)


def _monitor(session_factory, notifications, *, with_provider: bool = True):
    async def notifier(payload):
        notifications.append(payload)

    class _NoCandles(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return []

    return _NoCandles(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(max_age_hours=6),
        now_provider=lambda: NOW,
        expiry_review_notifier=notifier,
        group_trading_mode_provider=(
            (lambda chat: TRADING_MODES.get(int(chat), ""))
            if with_provider
            else None
        ),
    )


def _reviewed(notifications) -> list[int]:
    """Lifecycle ids a person was actually asked about."""

    return [
        int(payload["lifecycle_id"])
        for payload in notifications
        if "lifecycle_id" in payload
    ]


def _row(session_factory, lifecycle_id: int) -> StrategyLifecycle:
    with session_factory() as session:
        return session.get(StrategyLifecycle, lifecycle_id)


def test_the_four_combinations_of_group_mode_and_whitelist(tmp_path):
    """One sweep, four rows: only 开×白名单 reaches a person."""

    session_factory = _session_factory(tmp_path)
    auto_allowed = _lifecycle(
        session_factory, message_id=8001, chat_id=AUTO_TRADE_CHAT, symbol="BTC"
    )
    auto_denied = _lifecycle(
        session_factory, message_id=8002, chat_id=AUTO_TRADE_CHAT, symbol="HBAR"
    )
    notify_allowed = _lifecycle(
        session_factory, message_id=8003, chat_id=NOTIFY_ONLY_CHAT, symbol="ETH"
    )
    notify_denied = _lifecycle(
        session_factory, message_id=8004, chat_id=NOTIFY_ONLY_CHAT, symbol="PEPE"
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [auto_allowed]
    assert _row(session_factory, auto_allowed).management_action == (
        "expiry_review_requested"
    )
    for out_of_scope in (auto_denied, notify_allowed, notify_denied):
        row = _row(session_factory, out_of_scope)
        assert row.lifecycle_status == "expired"
        assert row.exit_reason == "expired"
        assert row.exited_at == NOW.replace(tzinfo=None)
        assert row.management_action == EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION
        assert (row.management_note or "").startswith(
            EXPIRY_OUT_OF_SCOPE_CLOSEOUT_NOTE_PREFIX
        )
        # Never notified, so never asked: the notified_at column stays empty.
        assert row.expiry_review_notified_at is None


def test_the_closeout_note_says_which_scope_the_row_fell_outside(tmp_path):
    session_factory = _session_factory(tmp_path)
    wrong_group = _lifecycle(
        session_factory, message_id=8101, chat_id=NOTIFY_ONLY_CHAT, symbol="BTC"
    )
    wrong_symbol = _lifecycle(
        session_factory, message_id=8102, chat_id=AUTO_TRADE_CHAT, symbol="HBAR"
    )

    asyncio.run(_monitor(session_factory, []).run_once())

    group_note = _row(session_factory, wrong_group).management_note or ""
    symbol_note = _row(session_factory, wrong_symbol).management_note or ""
    assert "群组未开启自动交易" in group_note
    assert "标的不在全局白名单" in symbol_note
    assert "HBAR" in symbol_note
    assert "BTC,ETH,SOL" in symbol_note
    # Distinguishable from a person's decision and from A2's timeout, in the
    # column an auditor reads first.
    for note in (group_note, symbol_note):
        assert not note.startswith("人工")
        assert not note.startswith("超时自动收口")


def test_an_out_of_scope_row_with_an_exchange_leg_is_still_reviewed(tmp_path):
    """The fail-closed exception, and the reason 乙 was allowed to ship.

    峰哥's group was switched from auto_trade to notify_only that morning with
    two unfinished strategies in it. "The group no longer trades" is not a
    reason to stop telling someone that an order is resting on the exchange --
    the 撤单 button lives on this notification.
    """

    session_factory = _session_factory(tmp_path)
    bound = _lifecycle(
        session_factory,
        message_id=8201,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="ETH",
        with_binding=True,
    )
    unbound = _lifecycle(
        session_factory,
        message_id=8202,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="ETH",
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [bound]
    bound_row = _row(session_factory, bound)
    assert bound_row.lifecycle_status == "pending_entry"
    assert bound_row.management_action == "expiry_review_requested"
    # The sibling proves the sweep ran rather than skipped this group entirely.
    assert _row(session_factory, unbound).management_action == (
        EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION
    )


def test_a_chat_the_group_config_never_mentions_is_out_of_scope(tmp_path):
    """Unknown means out: a chat nobody configured cannot place an order."""

    session_factory = _session_factory(tmp_path)
    unknown = _lifecycle(
        session_factory, message_id=8301, chat_id=UNCONFIGURED_CHAT, symbol="BTC"
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == []
    assert _row(session_factory, unknown).management_action == (
        EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION
    )


def test_an_entered_out_of_scope_lifecycle_is_silenced_but_never_expired(
    tmp_path,
):
    """A2's boundary, kept: ``entered`` loses the notification, not its status."""

    session_factory = _session_factory(tmp_path)
    entered = _lifecycle(
        session_factory,
        message_id=8401,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="BTC",
        status="entered",
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == []
    row = _row(session_factory, entered)
    assert row.lifecycle_status == "entered"
    assert row.exited_at is None
    assert row.management_action is None


def test_a_person_who_chose_to_keep_waiting_is_not_overruled(tmp_path):
    """继续等待 survives the narrowing; it only stops being re-asked.

    A2 refuses to touch these rows for the same reason, and the claim's
    ``expiry_review_next_at IS NULL`` predicate is what enforces it here.
    """

    session_factory = _session_factory(tmp_path)
    continued = _lifecycle(
        session_factory,
        message_id=8501,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="BTC",
        management_action="expiry_review_continued",
        notified_at=NOW - timedelta(days=3),
        next_at=NOW - timedelta(hours=1),
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == []
    row = _row(session_factory, continued)
    assert row.lifecycle_status == "pending_entry"
    assert row.management_action == "expiry_review_continued"
    assert row.exited_at is None


def test_a2_and_the_out_of_scope_closeout_never_both_claim_one_row(tmp_path):
    """Disjoint by predicate, not by ordering.

    A2 only touches rows that *were* notified and went unanswered for seven
    days; this one only rows that were never notified at all. The row below
    satisfies A2 and sits in a notify_only group, so it must come out with A2's
    action and A2's note -- once.
    """

    session_factory = _session_factory(tmp_path)
    notified_long_ago = _lifecycle(
        session_factory,
        message_id=8601,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="BTC",
        management_action="expiry_review_requested",
        notified_at=NOW - timedelta(days=9),
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    row = _row(session_factory, notified_long_ago)
    assert row.lifecycle_status == "expired"
    assert row.management_action == EXPIRY_REVIEW_AUTO_CLOSEOUT_ACTION
    assert row.management_action != EXPIRY_OUT_OF_SCOPE_CLOSEOUT_ACTION
    # A2 announces its batch; the out-of-scope path never notifies, so there is
    # exactly one payload and it is A2's summary.
    assert len(notifications) == 1
    assert notifications[0]["closed_count"] == 1


def test_a_row_not_yet_timed_out_is_left_alone_even_out_of_scope(tmp_path):
    """The gate runs after the due check, so a fresh signal keeps its life.

    This is what stops 乙 from expiring every new notify_only signal on
    arrival, which would end the candle replay those groups exist for.
    """

    session_factory = _session_factory(tmp_path)
    fresh = _lifecycle(
        session_factory,
        message_id=8701,
        chat_id=NOTIFY_ONLY_CHAT,
        symbol="BTC",
        signal_at=NOW - timedelta(hours=1),
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == []
    row = _row(session_factory, fresh)
    assert row.lifecycle_status == "pending_entry"
    assert row.management_action is None
    assert row.exited_at is None


def test_without_a_trading_mode_provider_nothing_is_narrowed(tmp_path):
    """A deployment that cannot answer the question behaves exactly as before."""

    session_factory = _session_factory(tmp_path)
    lifecycle_id = _lifecycle(
        session_factory, message_id=8801, chat_id=NOTIFY_ONLY_CHAT, symbol="PEPE"
    )
    notifications: list[dict] = []

    asyncio.run(
        _monitor(session_factory, notifications, with_provider=False).run_once()
    )

    assert _reviewed(notifications) == [lifecycle_id]
    assert _row(session_factory, lifecycle_id).management_action == (
        "expiry_review_requested"
    )


def test_an_unreadable_whitelist_notifies_rather_than_expires(
    tmp_path, monkeypatch
):
    """Fail-closed here means "ask anyway", not "close it out".

    A settings read that fails must not be able to expire rows: an extra
    notification costs one message, a suppressed one can cost an unattended
    exchange order.
    """

    session_factory = _session_factory(tmp_path)
    lifecycle_id = _lifecycle(
        session_factory, message_id=8901, chat_id=NOTIFY_ONLY_CHAT, symbol="PEPE"
    )

    def _unavailable(*args, **kwargs):
        raise RuntimeError("settings table unavailable")

    monkeypatch.setattr(
        lifecycle_monitor_module, "load_trading_settings", _unavailable
    )
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]
    assert _row(session_factory, lifecycle_id).lifecycle_status == "pending_entry"
