from datetime import UTC, datetime

from telegram_kol_research.recovery_scan import (
    AccountStateProvider,
    OpenOrder,
    PriceCandle,
    RecoverySignal,
    build_recovery_window,
    evaluate_recovery_signal,
    evaluate_recovery_signals_with_market_data,
    load_recovery_signals_from_db,
    select_recovery_signals,
)
from telegram_kol_research.trading_decision import ActivePosition
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig, TrackedSenderConfig
from telegram_kol_research.models import RawMessage, SignalCandidate, Source


def _signal(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "posted_at": datetime(2026, 6, 12, 8, 0),
        "symbol": "BTC",
        "side": "long",
        "entry_range": (68000.0, 68200.0),
        "stop_loss_text": "67500",
        "take_profit_text": "69000 / 70000",
        "parse_source": "text",
        "confidence": 0.9,
        "trading_mode": "auto_trade",
        "symbol_whitelist": ["BTC", "ETH"],
    }
    values.update(overrides)
    return RecoverySignal(**values)


def test_build_recovery_window_defaults_to_previous_48_hours_as_utc_naive():
    start_at, end_at = build_recovery_window(
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC)
    )

    assert start_at == datetime(2026, 6, 10, 18, 0)
    assert end_at == datetime(2026, 6, 12, 18, 0)


def test_recovery_skips_notify_only_kol():
    decision = evaluate_recovery_signal(
        _signal(trading_mode="notify_only"),
        candles_since_signal=[],
        current_price=67900,
    )

    assert decision.action == "skip"
    assert "notify_only_mode" in decision.reason_codes


def test_recovery_requires_manual_review_when_price_already_touched_entry_range():
    decision = evaluate_recovery_signal(
        _signal(),
        candles_since_signal=[
            PriceCandle(
                opened_at=datetime(2026, 6, 12, 9, 0),
                high=68100,
                low=67900,
            )
        ],
        current_price=67900,
    )

    assert decision.action == "manual_review"
    assert "entry_already_touched" in decision.reason_codes


def test_recovery_requires_manual_review_when_current_price_is_inside_entry_range():
    decision = evaluate_recovery_signal(
        _signal(),
        candles_since_signal=[],
        current_price=68100,
    )

    assert decision.action == "manual_review"
    assert "current_price_in_entry_range" in decision.reason_codes


def test_recovery_requires_manual_review_for_existing_same_strategy_order():
    decision = evaluate_recovery_signal(
        _signal(),
        candles_since_signal=[],
        current_price=67900,
        open_orders=[
            OpenOrder(
                kol_id="alice",
                chat_id=100,
                source_message_id=55,
                symbol="BTC",
                side="long",
                order_id="order-1",
            )
        ],
    )

    assert decision.action == "manual_review"
    assert "existing_recovery_order" in decision.reason_codes


def test_recovery_allows_limit_order_candidate_when_entry_range_was_never_touched():
    decision = evaluate_recovery_signal(
        _signal(),
        candles_since_signal=[
            PriceCandle(
                opened_at=datetime(2026, 6, 12, 9, 0),
                high=67950,
                low=67800,
            )
        ],
        current_price=67900,
    )

    assert decision.action == "eligible_for_recovery_limit_order"
    assert decision.reason_codes == ["recovery_checks_passed"]
    assert decision.entry_range == (68000.0, 68200.0)


def test_select_recovery_signals_keeps_only_auto_trade_signals_inside_scan_window():
    start_at = datetime(2026, 6, 10, 18, 0)
    end_at = datetime(2026, 6, 12, 18, 0)

    selected = select_recovery_signals(
        [
            _signal(message_id=1, posted_at=datetime(2026, 6, 10, 17, 59)),
            _signal(message_id=2, posted_at=datetime(2026, 6, 11, 8, 0)),
            _signal(message_id=3, posted_at=datetime(2026, 6, 12, 8, 0), trading_mode="notify_only"),
            _signal(message_id=4, posted_at=datetime(2026, 6, 12, 19, 0)),
        ],
        start_at=start_at,
        end_at=end_at,
    )

    assert [signal.message_id for signal in selected] == [2]


def test_load_recovery_signals_from_db_uses_auto_trade_group_chat_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
            sender_id=501,
            sender_name="Alice Trader",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="BTC long 68000-68200, SL 67500",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000 / 70000",
                parse_source="text",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

    signals = load_recovery_signals_from_db(
        session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="VIP BTC Room",
                    chat_id=9001,
                    trading_mode="auto_trade",
                    max_loss_usdt=50,
                    symbol_whitelist=["BTC"],
                )
            ]
        ),
        start_at=datetime(2026, 6, 10, 8, 0),
        end_at=datetime(2026, 6, 12, 18, 0),
    )

    assert len(signals) == 1
    assert signals[0].kol_id == "group:9001"
    assert signals[0].entry_range == (68000.0, 68200.0)
    assert signals[0].max_loss_usdt == 50.0
    assert signals[0].symbol_whitelist == ["BTC"]


def test_load_recovery_signals_from_db_treats_naive_window_bounds_as_utc_storage(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
            sender_id=501,
            sender_name="Alice Trader",
            posted_at=datetime(2026, 6, 10, 12, 0),
            text="BTC long 68000-68200, SL 67500",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                parse_source="text",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

    start_at, end_at = build_recovery_window(now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC))
    signals = load_recovery_signals_from_db(
        session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="VIP BTC Room",
                    chat_id=9001,
                    trading_mode="auto_trade",
                )
            ]
        ),
        start_at=start_at,
        end_at=end_at,
    )

    assert signals == []


def test_load_recovery_signals_from_db_uses_auto_trade_tracked_sender_override(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        source = Source(
            telegram_sender_id=501,
            chat_id=9001,
            display_name="Alice Trader",
        )
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
            sender_id=501,
            sender_name="Alice Trader",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="ETH short 2500-2550, SL 2600",
        )
        session.add_all([source, raw_message])
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                source_id=source.id,
                symbol="ETH",
                side="short",
                event_type="entry_signal",
                entry_text="2500-2550",
                stop_loss_text="2600",
                parse_source="text",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

    signals = load_recovery_signals_from_db(
        session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="VIP BTC Room",
                    chat_id=9001,
                    trading_mode="notify_only",
                    tracked_senders=[
                        TrackedSenderConfig(
                            display_name="Alice Trader",
                            telegram_sender_id=501,
                            custom_label="alice",
                            trading_mode="auto_trade",
                            max_loss_usdt=25,
                            symbol_whitelist=["ETH"],
                        )
                    ],
                )
            ]
        ),
        start_at=datetime(2026, 6, 10, 8, 0),
        end_at=datetime(2026, 6, 12, 18, 0),
    )

    assert len(signals) == 1
    assert signals[0].kol_id == "alice"
    assert signals[0].trading_mode == "auto_trade"
    assert signals[0].max_loss_usdt == 25.0
    assert signals[0].symbol_whitelist == ["ETH"]


def test_evaluate_recovery_signals_with_market_data_queries_from_signal_time_to_now():
    class FakeMarketData:
        def __init__(self):
            self.candle_calls = []
            self.price_calls = []

        def load_candles(self, *, symbol, start_at, end_at):
            self.candle_calls.append((symbol, start_at, end_at))
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 12, 9, 0),
                    high=67950,
                    low=67800,
                )
            ]

        def get_current_price(self, *, symbol):
            self.price_calls.append(symbol)
            return 67900

    provider = FakeMarketData()
    now = datetime(2026, 6, 12, 18, 0, tzinfo=UTC)
    results = evaluate_recovery_signals_with_market_data(
        [_signal()],
        market_data=provider,
        now=now,
    )

    assert results[0].decision.action == "eligible_for_recovery_limit_order"
    assert provider.candle_calls == [("BTC", datetime(2026, 6, 12, 8, 0), datetime(2026, 6, 12, 18, 0))]
    assert provider.price_calls == ["BTC"]


def test_evaluate_recovery_signals_with_market_data_manual_reviews_missing_symbol_without_provider_call():
    class FailingMarketData:
        def load_candles(self, **kwargs):
            raise AssertionError("market data should not be requested without a symbol")

        def get_current_price(self, **kwargs):
            raise AssertionError("price should not be requested without a symbol")

    results = evaluate_recovery_signals_with_market_data(
        [_signal(symbol=None)],
        market_data=FailingMarketData(),
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    assert results[0].decision.action == "manual_review"
    assert "missing_symbol" in results[0].decision.reason_codes


def test_evaluate_recovery_signals_with_account_state_blocks_duplicate_position():
    class FakeMarketData:
        def load_candles(self, *, symbol, start_at, end_at):
            return [
                PriceCandle(
                    opened_at=datetime(2026, 6, 12, 9, 0),
                    high=67950,
                    low=67800,
                )
            ]

        def get_current_price(self, *, symbol):
            return 67900

    class FakeAccountState:
        def __init__(self):
            self.position_calls = 0
            self.order_calls = 0

        def load_active_positions(self):
            self.position_calls += 1
            return [
                ActivePosition(
                    kol_id="alice",
                    chat_id=100,
                    symbol="BTC",
                    side="long",
                    pos_id="pos-1",
                )
            ]

        def load_open_orders(self):
            self.order_calls += 1
            return []

    account_state = FakeAccountState()
    results = evaluate_recovery_signals_with_market_data(
        [_signal()],
        market_data=FakeMarketData(),
        account_state=account_state,
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    assert results[0].decision.action == "manual_review"
    assert "duplicate_active_position" in results[0].decision.reason_codes
    assert account_state.position_calls == 1
    assert account_state.order_calls == 1


def test_evaluate_recovery_signals_with_account_state_blocks_existing_order():
    class FakeMarketData:
        def load_candles(self, *, symbol, start_at, end_at):
            return []

        def get_current_price(self, *, symbol):
            return 67900

    class FakeAccountState:
        def load_active_positions(self):
            return []

        def load_open_orders(self):
            return [
                OpenOrder(
                    kol_id="alice",
                    chat_id=100,
                    source_message_id=55,
                    symbol="BTC",
                    side="long",
                    order_id="order-1",
                )
            ]

    results = evaluate_recovery_signals_with_market_data(
        [_signal()],
        market_data=FakeMarketData(),
        account_state=FakeAccountState(),
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    assert results[0].decision.action == "manual_review"
    assert "existing_recovery_order" in results[0].decision.reason_codes
