"""2026-09-29 (design section 4): a refused entry is not asked about again.

舒琴 #19639 (ETH long) was refused by the entry price geometry check at
14:51:44Z and notified. Lifecycle 1356 was built as ``pending_entry`` with no
execution binding -- nothing ever reached the exchange -- and three hours later
the expiry review asked whether to cancel the exchange order. 32 of the last 40
geometry refusals were followed by that question; none had a binding.

The narrowing is exactly one fact wide: ``entry_price_geometry_rejected`` on the
lifecycle's own message. A row without it, or with a binding, is still asked.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_events import (
    enqueue_entry_price_geometry_rejection_notification,
)
from telegram_kol_research.lifecycle_monitor import (
    LifecycleMonitor,
    LifecycleMonitorConfig,
)
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import (
    ExecutionBinding,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings
import telegram_kol_research.system_operator_bot as operator_bot_module


NOW = datetime(2026, 9, 28, 17, 51, tzinfo=UTC)
SIGNALLED = datetime(2026, 9, 28, 14, 49, 54, tzinfo=UTC)
AUTO_TRADE_CHAT = -1009999999999
GROUP_LABELS = {AUTO_TRADE_CHAT: "舒琴"}
EXPECTED_NOTE = "入场已被价格几何校验拒绝，交易所无挂单，按超时直接过期，未发人工审批"


@pytest.fixture
def caplog(caplog, monkeypatch):
    """``caplog`` that still sees this package's records in a full run.

    ``app_logging.configure_application_logging`` sets the package logger's
    ``propagate = False`` for the whole process; same fixture as
    ``test_runtime_incident_detailed_summaries``.
    """

    monkeypatch.setattr(
        logging.getLogger("telegram_kol_research"), "propagate", True
    )
    return caplog


def _session_factory(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"allowed_symbols": "BTC,ETH,SOL"})
    return session_factory


def _message(session_factory, *, raw_id: int, message_id: int) -> None:
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_id,
                chat_id=AUTO_TRADE_CHAT,
                message_id=message_id,
                text="ETH 2400-2420 多，止损 2450",
                posted_at=SIGNALLED.replace(tzinfo=None),
                created_at=SIGNALLED.replace(tzinfo=None),
            )
        )
        session.commit()


def _geometry_refusal(session_factory, *, raw_id: int) -> int:
    return enqueue_entry_price_geometry_rejection_notification(
        session_factory,
        raw_message_id=raw_id,
        candidate_id=raw_id + 1,
        chat_id=AUTO_TRADE_CHAT,
        symbol="ETH",
        side="long",
        parse_source="mimo_authoritative",
        authoritative_generation="generation-19639",
        geometry={
            "geometry_status": "invalid",
            "reason_code": "entry_price_geometry_stop_side_invalid",
            "entry_domain": ["2400", "2420"],
            "entry_prices": ["2400", "2420"],
            "stop_loss": "2450",
            "take_profit_prices": ["2500"],
            "explicit_average_entry": None,
            "offending_field": "stop_loss",
            "offending_value": "2450",
        },
        created_at=SIGNALLED + timedelta(minutes=2),
    )


def _lifecycle(
    session_factory, *, message_id: int, with_binding: bool = False
) -> int:
    with session_factory() as session:
        binding_id = None
        if with_binding:
            binding = ExecutionBinding(
                strategy_instance_id=(
                    f"deepcoin:{AUTO_TRADE_CHAT}:{message_id}:ETH:long"
                ),
                kol_id=f"group:{AUTO_TRADE_CHAT}",
                chat_id=AUTO_TRADE_CHAT,
                message_id=message_id,
                symbol="ETH",
                side="long",
                status="active",
            )
            session.add(binding)
            session.flush()
            binding_id = binding.id
        row = StrategyLifecycle(
            chat_id=AUTO_TRADE_CHAT,
            message_id=message_id,
            symbol="ETH",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=SIGNALLED.replace(tzinfo=None),
            entry_range_low=2400,
            entry_range_high=2420,
            stop_loss=2450,
            take_profit="2500",
            execution_binding_id=binding_id,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _monitor(session_factory, notifications):
    async def notifier(payload):
        notifications.append(payload)

    class _NoCandles(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return []

    return _NoCandles(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(max_age_hours=3),
        now_provider=lambda: NOW,
        expiry_review_notifier=notifier,
        group_trading_mode_provider=lambda chat: (
            "auto_trade" if int(chat) == AUTO_TRADE_CHAT else ""
        ),
    )


def _reviewed(notifications) -> list[int]:
    return [
        int(payload["lifecycle_id"])
        for payload in notifications
        if "lifecycle_id" in payload
    ]


def _row(session_factory, lifecycle_id: int) -> StrategyLifecycle:
    with session_factory() as session:
        return session.get(StrategyLifecycle, lifecycle_id)


def test_r4a_a_geometry_refused_entry_expires_silently(tmp_path, caplog):
    """Lifecycle 1356's shape: refused, never bound, timed out."""

    session_factory = _session_factory(tmp_path)
    # A raw id unlike the Telegram message id, so the lookup has to go through
    # ``raw_messages`` rather than guess.
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31557)
    notifications: list[dict] = []

    caplog.set_level("INFO", logger="telegram_kol_research.lifecycle_monitor")
    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == []
    from telegram_kol_research.lifecycle_monitor import (
        EXPIRY_ENTRY_REFUSED_CLOSEOUT_ACTION,
    )

    row = _row(session_factory, lifecycle_id)
    assert row.lifecycle_status == "expired"
    assert row.exit_reason == "expired"
    assert row.exited_at == NOW.replace(tzinfo=None)
    assert row.management_action == EXPIRY_ENTRY_REFUSED_CLOSEOUT_ACTION
    assert row.management_action.startswith("expiry_")
    assert EXPECTED_NOTE in (row.management_note or "")
    assert not (row.management_note or "").startswith("人工")
    assert row.expiry_review_notified_at is None
    # Unnotified, so the log line is the batch's only announcement.
    assert "refused by the entry price geometry check" in caplog.text
    assert f"lifecycle_ids=[{lifecycle_id}]" in caplog.text


def test_r4b_an_unbound_row_without_a_geometry_refusal_is_still_reviewed(tmp_path):
    """Entry admission still queued, say: the exchange may get an order yet."""

    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19700, message_id=31600)
    lifecycle_id = _lifecycle(session_factory, message_id=31600)
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]
    assert _row(session_factory, lifecycle_id).management_action == (
        "expiry_review_requested"
    )


def test_r4b_a_refusal_of_another_message_does_not_count(tmp_path):
    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _message(session_factory, raw_id=19640, message_id=31558)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31558)
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]


def test_r4c_a_bound_row_is_still_reviewed_even_after_a_refusal(tmp_path):
    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31557, with_binding=True)
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]
    assert _row(session_factory, lifecycle_id).lifecycle_status == "pending_entry"


def test_a_person_who_chose_to_keep_waiting_is_asked_again_not_overruled(tmp_path):
    """A continued review carries a next_at, which the shared claim refuses."""

    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31557)
    with session_factory() as session:
        row = session.get(StrategyLifecycle, lifecycle_id)
        row.management_action = "expiry_review_continued"
        row.expiry_review_notified_at = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        row.expiry_review_next_at = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
        session.commit()
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]
    assert _row(session_factory, lifecycle_id).lifecycle_status == "pending_entry"


def test_a_failed_refusal_lookup_reviews_as_before(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31557)

    def broken(_session, _row):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(LifecycleMonitor, "_entry_refused_by_geometry", staticmethod(broken))
    notifications: list[dict] = []

    asyncio.run(_monitor(session_factory, notifications).run_once())

    assert _reviewed(notifications) == [lifecycle_id]


def test_a_refused_entry_that_is_not_yet_due_is_left_alone(tmp_path):
    """Only at expiry: context resolution may still point at it before then."""

    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    lifecycle_id = _lifecycle(session_factory, message_id=31557)
    notifications: list[dict] = []
    monitor = _monitor(session_factory, notifications)
    monitor._now = lambda: SIGNALLED + timedelta(hours=1)

    asyncio.run(monitor.run_once())

    assert _reviewed(notifications) == []
    row = _row(session_factory, lifecycle_id)
    assert row.lifecycle_status == "pending_entry"
    assert row.management_action is None


def _geometry_event(chat_id=AUTO_TRADE_CHAT):
    return SimpleNamespace(
        id=4716,
        action="entry_price_geometry_rejected",
        status="manual_review",
        response_json=json.dumps(
            {
                "raw_message_id": 19639,
                "candidate_id": 2701,
                "chat_id": chat_id,
                "symbol": "ETH",
                "side": "long",
                "entry_domain": ["2400", "2420"],
                "offending_field": "stop_loss",
                "offending_value": "2450",
                "reason_code": "entry_price_geometry_stop_side_invalid",
                "parse_source": "mimo_authoritative",
                "authoritative_generation": "generation-19639",
            }
        ),
    )


def test_r4d_the_geometry_alert_names_the_group_not_its_chat_id():
    rendered = operator_bot_module.format_terminal_entry_cleanup_notification(
        _geometry_event(), group_label_for=GROUP_LABELS.get
    )

    assert "群: 舒琴" in rendered
    assert "Chat:" not in rendered
    assert re.search(r"-100\d{10,}", rendered) is None
    assert "9999999999" not in rendered


def test_r4d_an_unknown_group_is_said_as_such():
    for group_label_for in (None, lambda _chat: None):
        rendered = operator_bot_module.format_terminal_entry_cleanup_notification(
            _geometry_event(), group_label_for=group_label_for
        )
        assert "群: 未知群" in rendered
        assert "9999999999" not in rendered


def test_r4d_the_delivered_geometry_alert_carries_the_group_name(
    tmp_path, monkeypatch
):
    session_factory = _session_factory(tmp_path)
    _message(session_factory, raw_id=19639, message_id=31557)
    _geometry_refusal(session_factory, raw_id=19639)
    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
        return 9911

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fake_send
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
            delivered_at=NOW,
            group_label_for=GROUP_LABELS.get,
        )
    )

    assert delivered == 1
    assert "群: 舒琴" in sent[0]
    assert "9999999999" not in sent[0]
