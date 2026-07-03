import asyncio
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.lifecycle_monitor import (
    LifecycleMonitor,
    LifecycleMonitorConfig,
    PriceCandle,
    StateTransition,
)
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import (
    ExecutionBinding,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeIdea,
)


def test_lifecycle_monitor_rejects_entry_before_signal_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor._apply_transitions(
        [
            StateTransition(
                signal_id=lifecycle_id,
                from_status="pending_entry",
                to_status="entered",
                trigger_price=62486.1,
                occurred_at=datetime(2026, 6, 19, 4, 54, tzinfo=UTC),
            )
        ]
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.entered_at is None
    assert lifecycle.entry_price_actual is None


def test_lifecycle_monitor_skips_simulated_exit_for_live_execution_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="alice",
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-live",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 11, 40, tzinfo=UTC),
            entry_price_actual=61563,
            stop_loss=62440,
            take_profit="59588",
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor._apply_transitions(
        [
            StateTransition(
                signal_id=lifecycle_id,
                from_status="entered",
                to_status="exited",
                exit_reason="stop_loss",
                trigger_price=62440,
                occurred_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
            )
        ]
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None


def test_lifecycle_backfill_keeps_entered_record_with_entry_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entry_price_actual=62486.1,
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor.backfill_from_trade_ideas()

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at == datetime(2026, 6, 19, 11, 32, 47)
    assert lifecycle.entry_price_actual == 62486.1


def test_lifecycle_backfill_keeps_entered_record_with_live_execution_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="alice",
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-live",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 11, 40, tzinfo=UTC),
            entry_range_low=60300,
            entry_range_high=60800,
            stop_loss=61300,
            take_profit="59600/58900/58200",
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor.backfill_from_trade_ideas()

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at == datetime(2026, 6, 19, 11, 40)


def test_lifecycle_backfill_skips_duplicate_active_trade_idea_from_repost(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        existing = StrategyLifecycle(
            chat_id=88,
            message_id=6609,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 20, 0, 34, 55, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 13, 36, tzinfo=UTC),
            entry_price_actual=1775,
            entry_range_low=1775,
            entry_range_high=1775,
            stop_loss=1800,
            take_profit="1755/1740/1715",
        )
        repost = RawMessage(
            chat_id=88,
            message_id=6618,
            posted_at=datetime(2026, 6, 21, 3, 20, 7, tzinfo=UTC),
            text="以太币 委托1775 附近 开空\n止损：1800\n止盈：1755-1740-1715",
        )
        session.add_all([existing, repost])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=repost.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
            entry_text="1775附近",
            stop_loss_text="1800",
            take_profit_text="1755/1740/1715",
            parse_source="text_ai",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        trade_idea = TradeIdea(
            primary_signal_candidate_id=candidate.id,
            chat_id=88,
            symbol="ETH",
            side="short",
            status="open",
            confidence=0.95,
            opened_at=repost.posted_at,
            created_at=repost.posted_at,
        )
        session.add(trade_idea)
        session.commit()
        candidate_id = candidate.id
        trade_idea_id = trade_idea.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())

    assert monitor.backfill_from_trade_ideas() == 0

    with session_factory() as session:
        candidate = session.get(SignalCandidate, candidate_id)
        trade_idea = session.get(TradeIdea, trade_idea_id)
        lifecycles = session.query(StrategyLifecycle).all()

    assert len(lifecycles) == 1
    assert candidate.event_type == "duplicate_entry_signal"
    assert "Duplicate active strategy lifecycle" in candidate.review_note
    assert trade_idea.status == "duplicate"


def test_lifecycle_backfill_applies_entry_correction_from_orphan_trade_idea(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        existing = StrategyLifecycle(
            chat_id=88,
            message_id=9079,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 22, 11, 57, 47, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 11, 58, tzinfo=UTC),
            entry_range_low=64600,
            entry_range_high=69000,
            stop_loss=66100,
            take_profit="62300/61200",
        )
        correction = RawMessage(
            chat_id=88,
            message_id=9080,
            posted_at=datetime(2026, 6, 22, 12, 18, 46, tzinfo=UTC),
            text="BTC 64600-64900附近做空 止损66100 止盈62300/61200",
        )
        session.add_all([existing, correction])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=correction.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="64600-64900",
            stop_loss_text="66100",
            take_profit_text="62300/61200",
            parse_source="glm_ocr_image",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        trade_idea = TradeIdea(
            primary_signal_candidate_id=candidate.id,
            chat_id=88,
            symbol="BTC",
            side="short",
            status="open",
            confidence=0.95,
            opened_at=correction.posted_at,
            created_at=correction.posted_at,
        )
        session.add(trade_idea)
        session.commit()
        candidate_id = candidate.id
        trade_idea_id = trade_idea.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())

    assert monitor.backfill_from_trade_ideas() == 0

    with session_factory() as session:
        candidate = session.get(SignalCandidate, candidate_id)
        trade_idea = session.get(TradeIdea, trade_idea_id)
        lifecycles = session.query(StrategyLifecycle).all()

    assert len(lifecycles) == 1
    assert lifecycles[0].entry_range_low == 64600
    assert lifecycles[0].entry_range_high == 64900
    assert lifecycles[0].management_signal_message_id == 9080
    assert lifecycles[0].management_action == "strategy_correction"
    assert candidate.event_type == "strategy_correction"
    assert trade_idea.status == "duplicate"


def test_lifecycle_monitor_rejects_protective_stop_before_management_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1395,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 17, 10, 26, 13, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
            entry_price_actual=63794.4,
            entry_range_low=61800,
            entry_range_high=63800,
            stop_loss=63794.4,
            take_profit="65500/66500/67500",
            management_signal_message_id=1400,
            management_action="partial_take_profit, move_stop_to_protect",
        )
        management_message = RawMessage(
            chat_id=88,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, 45, tzinfo=UTC),
            text="现价64500附近提前止盈一半带保护",
        )
        session.add_all([lifecycle, management_message])
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor._apply_transitions(
        [
            StateTransition(
                signal_id=lifecycle_id,
                from_status="entered",
                to_status="exited",
                exit_reason="stop_loss",
                trigger_price=63794.4,
                occurred_at=datetime(2026, 6, 18, 5, 8, tzinfo=UTC),
            )
        ]
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exited_at is None
    assert lifecycle.exit_reason is None


def test_lifecycle_monitor_enters_market_signal_when_current_price_is_near_reference(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal_at = datetime(2026, 6, 23, 7, 30, 25, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=12944,
            posted_at=signal_at,
            text="ETH 做空 1695市价直接地板空 1765挂单 止损1830 止盈1673/1638",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
            entry_text="1695市价/1765挂单",
            stop_loss_text="1830",
            take_profit_text="1673/1638",
            parse_source="text_ai",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=88,
            message_id=12944,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=signal_at,
            entry_range_low=1695,
            entry_range_high=1765,
            stop_loss=1830,
            take_profit="1673/1638",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    class FakeLifecycleMonitor(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 23, 7, 31, tzinfo=UTC),
                    high=1694,
                    low=1692,
                )
            ]

        async def _fetch_current_price(self, contract):
            return 1693.2

    monitor = FakeLifecycleMonitor(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(market_entry_tolerance_ratio=0.0015),
        now_provider=lambda: datetime(2026, 6, 23, 7, 32, tzinfo=UTC),
    )

    transitions = asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert transitions[0]["from"] == "pending_entry"
    assert transitions[0]["to"] == "entered"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_price_actual == 1693.22875
    assert lifecycle.entered_at == datetime(2026, 6, 23, 7, 31)


def test_lifecycle_monitor_enters_nearby_entry_price_with_tolerance(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal_at = datetime(2026, 6, 26, 2, 25, 42, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=421,
            posted_at=signal_at,
            text=(
                "米娅BTC短线合约交易策略\n"
                "做多\n"
                "进场点位：58300附近\n"
                "止损点位：57300\n"
                "止盈点位：60600"
            ),
        )
        session.add(raw_message)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=421,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=signal_at,
            entry_range_low=58300,
            entry_range_high=58300,
            stop_loss=57300,
            take_profit="60600",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    class FakeLifecycleMonitor(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 26, 2, 26, tzinfo=UTC),
                    high=58360,
                    low=58340,
                )
            ]

        async def _fetch_current_price(self, contract):
            return 58350

    monitor = FakeLifecycleMonitor(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(market_entry_tolerance_ratio=0.0015),
        now_provider=lambda: datetime(2026, 6, 26, 2, 27, tzinfo=UTC),
    )

    transitions = asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert transitions[0]["from"] == "pending_entry"
    assert transitions[0]["to"] == "entered"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_price_actual == 58350
    assert lifecycle.entered_at == datetime(2026, 6, 26, 2, 26)


def test_lifecycle_monitor_enters_flexible_entry_range_at_current_price(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal_at = datetime(2026, 6, 24, 7, 25, 10, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=3833,
            posted_at=signal_at,
            text=(
                "币姐\nEth\n方向：空\n建仓：1675-1700\n止损：1720\n"
                "止盈：1655-1635-1615\n进场灵活，不必踩点。轻仓，太横盘了，怕突然来一下"
            ),
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
            entry_text="1675-1700",
            stop_loss_text="1720",
            take_profit_text="1655/1635/1615",
            parse_source="text_ai",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=88,
            message_id=3833,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=signal_at,
            entry_range_low=1675,
            entry_range_high=1700,
            stop_loss=1720,
            take_profit="1655/1635/1615",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    class FakeLifecycleMonitor(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 24, 7, 26, tzinfo=UTC),
                    high=1673.8,
                    low=1672.0,
                )
            ]

        async def _fetch_current_price(self, contract):
            return 1670.0

    monitor = FakeLifecycleMonitor(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(market_entry_tolerance_ratio=0.0015),
        now_provider=lambda: datetime(2026, 6, 24, 7, 27, tzinfo=UTC),
    )

    transitions = asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert transitions[0]["from"] == "pending_entry"
    assert transitions[0]["to"] == "entered"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_price_actual == 1673.14375
    assert lifecycle.entered_at == datetime(2026, 6, 24, 7, 26)


def test_lifecycle_monitor_expires_pending_before_late_price_touch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal_at = datetime(2026, 6, 30, 0, 0, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3888,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=signal_at,
            entry_range_low=60300,
            entry_range_high=60800,
            stop_loss=61300,
            take_profit="59600/58900/58200",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    class FakeLifecycleMonitor(LifecycleMonitor):
        async def _fetch_candles_full(self, contract, from_, to_):
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
                    high=60600,
                    low=60400,
                )
            ]

    monitor = FakeLifecycleMonitor(
        session_factory,
        LiveUpdateBroker(),
        config=LifecycleMonitorConfig(max_age_hours=6),
        now_provider=lambda: datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
    )

    transitions = asyncio.run(monitor.run_once())

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert transitions[0]["from"] == "pending_entry"
    assert transitions[0]["to"] == "expired"
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.entered_at is None
    assert lifecycle.entry_price_actual is None
