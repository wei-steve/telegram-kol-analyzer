from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import RawMessage, SignalCandidate
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_runner import (
    RecoveryDryRunProviderMissingError,
    run_recovery_dry_run,
)
from telegram_kol_research.recovery_scan import PriceCandle


def test_run_recovery_dry_run_evaluates_db_candidates_with_injected_providers(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
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
                parse_source="text",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

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

    class EmptyAccountState:
        def load_active_positions(self):
            return []

        def load_open_orders(self):
            return []

    result = run_recovery_dry_run(
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
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
        market_data=FakeMarketData(),
        account_state=EmptyAccountState(),
    )

    assert result.total_candidates == 1
    assert result.action_counts == {"eligible_for_recovery_limit_order": 1}
    assert result.evaluations[0].signal.message_id == 77


def test_run_recovery_dry_run_can_persist_evaluations(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
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
                parse_source="text",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()

    class FakeMarketData:
        def load_candles(self, *, symbol, start_at, end_at):
            return []

        def get_current_price(self, *, symbol):
            return 68100

    run_recovery_dry_run(
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
        now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
        market_data=FakeMarketData(),
        persist=True,
    )

    rows = list_recovery_decisions(session_factory, limit=10)
    assert rows[0]["action"] == "manual_review"
    assert rows[0]["reason_codes"] == ["current_price_in_entry_range"]


def test_run_recovery_dry_run_requires_market_data_provider(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    try:
        run_recovery_dry_run(
            session_factory,
            group_config=GroupConfig(groups=[]),
            now=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
        )
    except RecoveryDryRunProviderMissingError as exc:
        assert "market data provider" in str(exc)
    else:
        raise AssertionError("expected missing provider error")
