"""丙 (2026-09-25): the geometry refusal notice follows 乙's scope.

The pre-candidate geometry check runs before the group-mode gate and before the
whitelist gate, so a group that places no orders and a symbol the executor
refuses outright were both producing 人工复核 notifications --
书'shu-crypto's TRUTH was one of each at once.

The narrowing is a predicate on the notice, not a move of the notice: the
branches between it and those two gates all return early, and one of them
(the global ``auto_trade_disabled`` switch) already has a test of its own
asserting that a geometry alert still goes out. Silencing that would be a
second change nobody asked for.
"""

import json
from datetime import datetime

from telegram_kol_research.auto_trade_execution import (
    auto_process_message_trade_signal,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import ExecutionEvent, RawMessage, SignalCandidate
from telegram_kol_research.recovery_scan import load_recovery_signals_from_db
from telegram_kol_research.trading_settings import save_trading_settings

from tests.test_auto_trade_execution import (
    _FakeDeepcoinClient,
    _StaticContractSpecProvider,
    _persist_candidate,
)


CHAT_ID = 100
# The known-bad short: the stop sits below the entry, which
# ``validate_candidate_entry_price_geometry`` refuses as stop_side_invalid.
BAD_GEOMETRY = {
    "entry_text": "69900",
    "stop_loss_text": "61600",
    "take_profit_text": "67900 / 66600",
    "side": "short",
}


def _config(*, trading_mode: str, chat_id: int = CHAT_ID) -> GroupConfig:
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="Scoped Group",
                chat_id=chat_id,
                enabled=True,
                trading_mode=trading_mode,
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC", "ETH"],
            )
        ]
    )


def _run(session_factory, group_config, *, symbol: str = "BTC", message_id: int = 11767):
    raw_message_id = _persist_candidate(
        session_factory,
        text="redacted fixture",
        symbol=symbol,
        message_id=message_id,
        parse_source="mimo_authoritative",
        recognition_generation="generation-7",
        **BAD_GEOMETRY,
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "allowed_symbols": ["BTC", "ETH"]},
    )
    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=group_config,
        deepcoin_client=_FakeDeepcoinClient(session_factory),
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "entry_price_geometry_rejected")
            .all()
        )
    return result, events


def test_an_auto_trade_group_on_a_whitelisted_symbol_still_alerts(tmp_path):
    """The control. Everything below must differ from this row only in scope."""

    session_factory = create_session_factory(tmp_path / "in-scope.db")

    result, events = _run(session_factory, _config(trading_mode="auto_trade"))

    assert result["reason"] == "entry_price_geometry_stop_side_invalid"
    assert len(events) == 1
    assert events[0].notification_status == "pending"


def test_a_notify_only_group_no_longer_alerts(tmp_path):
    session_factory = create_session_factory(tmp_path / "notify-only.db")

    result, events = _run(session_factory, _config(trading_mode="notify_only"))

    # The skip reason and its order are untouched; only the notification is gone.
    assert result["reason"] == "kol_or_group_auto_trade_disabled"
    assert events == []


def test_a_symbol_outside_the_whitelist_no_longer_alerts(tmp_path):
    """书'shu-crypto's TRUTH, the production instance."""

    session_factory = create_session_factory(tmp_path / "off-whitelist.db")

    result, events = _run(
        session_factory,
        _config(trading_mode="auto_trade"),
        symbol="TRUTH",
        message_id=11768,
    )

    assert result["reason"] == "symbol_not_allowed"
    assert events == []


def test_a_chat_the_group_config_never_mentions_no_longer_alerts(tmp_path):
    session_factory = create_session_factory(tmp_path / "unconfigured.db")

    result, events = _run(
        session_factory,
        _config(trading_mode="auto_trade", chat_id=CHAT_ID + 1),
    )

    assert result["reason"] == "group_not_configured_for_auto_trade"
    assert events == []


def _recovery_candidate(
    session_factory, *, symbol: str, message_id: int
) -> None:
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=message_id,
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="redacted fixture",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol=symbol,
                side="short",
                event_type="entry_signal",
                entry_text=BAD_GEOMETRY["entry_text"],
                stop_loss_text=BAD_GEOMETRY["stop_loss_text"],
                take_profit_text=BAD_GEOMETRY["take_profit_text"],
                parse_source="mimo_authoritative",
                recognition_generation="generation-9",
                confidence=0.9,
                review_status="confirmed",
            )
        )
        session.commit()


def test_the_recovery_scan_alerts_only_for_whitelisted_symbols(tmp_path):
    """Same rule on the restart-recovery path, which only checked the mode.

    The whitelist read there is the group config's, because the only caller
    passes a config already through ``apply_trading_settings_to_group_config``,
    which replaces every group's list with the global one.
    """

    session_factory = create_session_factory(tmp_path / "recovery-scope.db")
    _recovery_candidate(session_factory, symbol="BTC", message_id=15840)
    _recovery_candidate(session_factory, symbol="TRUTH", message_id=15841)

    signals = load_recovery_signals_from_db(
        session_factory,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="VIP BTC Room",
                    chat_id=9001,
                    trading_mode="auto_trade",
                    symbol_whitelist=["BTC", "ETH"],
                )
            ]
        ),
        start_at=datetime(2026, 6, 10, 8, 0),
        end_at=datetime(2026, 6, 12, 18, 0),
    )

    # Both candidates are still loaded as recovery signals -- the narrowing is
    # about who gets told, not about what the scan sees.
    assert {signal.symbol for signal in signals} == {"BTC", "TRUTH"}
    with session_factory() as session:
        events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "entry_price_geometry_rejected")
            .all()
        )
    assert len(events) == 1
    assert json.loads(events[0].response_json)["symbol"] == "BTC"
