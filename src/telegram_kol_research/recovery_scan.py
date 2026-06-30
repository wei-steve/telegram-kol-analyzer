"""Restart recovery decisions for missed Telegram KOL entry signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Protocol

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig, TrackedSenderConfig
from telegram_kol_research.models import RawMessage, SignalCandidate, Source
from telegram_kol_research.time_utils import normalize_to_utc_naive
from telegram_kol_research.trading_decision import (
    ActivePosition,
    TradingDecisionInput,
    evaluate_trading_decision,
)


@dataclass(slots=True)
class RecoverySignal:
    kol_id: str
    chat_id: int
    message_id: int
    posted_at: datetime
    symbol: str | None
    side: str | None
    entry_range: tuple[float, float] | None
    stop_loss_text: str | None
    take_profit_text: str | None
    parse_source: str = "text"
    confidence: float = 0.0
    trading_mode: str = "notify_only"
    max_loss_usdt: float = 20.0
    symbol_whitelist: list[str] = field(default_factory=lambda: ["BTC", "ETH"])


@dataclass(slots=True)
class PriceCandle:
    opened_at: datetime
    high: float
    low: float


@dataclass(slots=True)
class OpenOrder:
    kol_id: str
    chat_id: int
    source_message_id: int
    symbol: str
    side: str
    order_id: str | None = None


@dataclass(slots=True)
class RecoveryDecision:
    action: str
    reason_codes: list[str]
    entry_range: tuple[float, float] | None = None
    max_loss_usdt: float = 20.0


class MarketDataProvider(Protocol):
    """Price data interface used by restart recovery scanning."""

    def load_candles(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[PriceCandle]:
        """Return candles for a symbol in UTC-naive storage time."""

    def get_current_price(self, *, symbol: str) -> float | None:
        """Return the latest price for a symbol."""


class AccountStateProvider(Protocol):
    """Read-only account state interface used by restart recovery scanning."""

    def load_active_positions(self) -> list[ActivePosition]:
        """Return normalized active positions from the trading venue."""

    def load_open_orders(self) -> list[OpenOrder]:
        """Return normalized open orders from the trading venue."""


@dataclass(slots=True)
class RecoveryEvaluation:
    signal: RecoverySignal
    decision: RecoveryDecision


def build_recovery_window(
    *,
    now: datetime,
    lookback_hours: int = 48,
) -> tuple[datetime, datetime]:
    """Return the UTC-naive scan window used after service restart."""

    end_at = normalize_to_utc_naive(now)
    return end_at - timedelta(hours=lookback_hours), end_at


def evaluate_recovery_signal(
    signal: RecoverySignal,
    *,
    candles_since_signal: list[PriceCandle],
    current_price: float | None,
    active_positions: list[ActivePosition] | None = None,
    open_orders: list[OpenOrder] | None = None,
) -> RecoveryDecision:
    """Decide whether a missed signal can be queued for recovery limit orders."""

    base_decision = evaluate_trading_decision(
        TradingDecisionInput(
            kol_id=signal.kol_id,
            chat_id=signal.chat_id,
            message_id=signal.message_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_text=_format_entry_range(signal.entry_range),
            stop_loss_text=signal.stop_loss_text,
            take_profit_text=signal.take_profit_text,
            parse_source=signal.parse_source,
            confidence=signal.confidence,
            trading_mode=signal.trading_mode,
            max_loss_usdt=signal.max_loss_usdt,
            symbol_whitelist=signal.symbol_whitelist,
        ),
        active_positions=active_positions,
    )
    if base_decision.action == "notify_only":
        return RecoveryDecision(
            action="skip",
            reason_codes=base_decision.reason_codes,
            entry_range=signal.entry_range,
            max_loss_usdt=base_decision.max_loss_usdt,
        )

    reason_codes = list(base_decision.reason_codes)
    if signal.entry_range is None:
        reason_codes.append("missing_entry_range")
    elif _entry_range_was_touched(signal.entry_range, candles_since_signal):
        reason_codes.append("entry_already_touched")
    elif current_price is not None and _price_in_range(current_price, signal.entry_range):
        reason_codes.append("current_price_in_entry_range")

    if _has_existing_order(signal, open_orders or []):
        reason_codes.append("existing_recovery_order")

    manual_reasons = [
        reason
        for reason in reason_codes
        if reason != "risk_checks_passed"
    ]
    if manual_reasons:
        return RecoveryDecision(
            action="manual_review",
            reason_codes=manual_reasons,
            entry_range=signal.entry_range,
            max_loss_usdt=base_decision.max_loss_usdt,
        )

    return RecoveryDecision(
        action="eligible_for_recovery_limit_order",
        reason_codes=["recovery_checks_passed"],
        entry_range=signal.entry_range,
        max_loss_usdt=base_decision.max_loss_usdt,
    )


def select_recovery_signals(
    signals: list[RecoverySignal],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[RecoverySignal]:
    """Select auto-trade signals whose posted time falls inside a restart scan window."""

    normalized_start = _storage_utc_naive(start_at)
    normalized_end = _storage_utc_naive(end_at)
    return [
        signal
        for signal in signals
        if signal.trading_mode == "auto_trade"
        and normalized_start <= _storage_utc_naive(signal.posted_at) <= normalized_end
    ]


def load_recovery_signals_from_db(
    session_factory: sessionmaker,
    *,
    group_config: GroupConfig,
    start_at: datetime,
    end_at: datetime,
) -> list[RecoverySignal]:
    """Load configured auto-trade entry candidates from the local database."""

    normalized_start = _storage_utc_naive(start_at)
    normalized_end = _storage_utc_naive(end_at)
    signals: list[RecoverySignal] = []

    with session_factory() as session:
        rows = (
            session.query(SignalCandidate, RawMessage, Source)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .outerjoin(Source, SignalCandidate.source_id == Source.id)
            .filter(RawMessage.posted_at >= normalized_start)
            .filter(RawMessage.posted_at <= normalized_end)
            .filter(SignalCandidate.event_type == "entry_signal")
            .order_by(RawMessage.posted_at.asc(), RawMessage.message_id.asc())
            .all()
        )

        for candidate, raw_message, source in rows:
            runtime_config = _resolve_runtime_config(
                group_config,
                raw_message=raw_message,
                source=source,
            )
            if runtime_config is None or runtime_config["trading_mode"] != "auto_trade":
                continue

            signals.append(
                RecoverySignal(
                    kol_id=runtime_config["kol_id"],
                    chat_id=raw_message.chat_id,
                    message_id=raw_message.message_id,
                    posted_at=raw_message.posted_at,
                    symbol=candidate.symbol,
                    side=candidate.side,
                    entry_range=_parse_entry_range(candidate.entry_text),
                    stop_loss_text=candidate.stop_loss_text,
                    take_profit_text=candidate.take_profit_text,
                    parse_source=candidate.parse_source,
                    confidence=candidate.confidence,
                    trading_mode=runtime_config["trading_mode"],
                    max_loss_usdt=runtime_config["max_loss_usdt"],
                    symbol_whitelist=runtime_config["symbol_whitelist"],
                )
            )

    return signals


def evaluate_recovery_signals_with_market_data(
    signals: list[RecoverySignal],
    *,
    market_data: MarketDataProvider,
    now: datetime,
    account_state: AccountStateProvider | None = None,
    active_positions: list[ActivePosition] | None = None,
    open_orders: list[OpenOrder] | None = None,
) -> list[RecoveryEvaluation]:
    """Evaluate recovery candidates using injected market history and current price."""

    end_at = _storage_utc_naive(now)
    resolved_active_positions = (
        active_positions
        if active_positions is not None
        else account_state.load_active_positions()
        if account_state is not None
        else None
    )
    resolved_open_orders = (
        open_orders
        if open_orders is not None
        else account_state.load_open_orders()
        if account_state is not None
        else None
    )
    evaluations: list[RecoveryEvaluation] = []

    for signal in signals:
        if not signal.symbol:
            evaluations.append(
                RecoveryEvaluation(
                    signal=signal,
                    decision=RecoveryDecision(
                        action="manual_review",
                        reason_codes=["missing_symbol"],
                        entry_range=signal.entry_range,
                        max_loss_usdt=signal.max_loss_usdt,
                    ),
                )
            )
            continue

        symbol = signal.symbol.upper()
        start_at = _storage_utc_naive(signal.posted_at)
        candles = market_data.load_candles(
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        )
        current_price = market_data.get_current_price(symbol=symbol)
        evaluations.append(
            RecoveryEvaluation(
                signal=signal,
                decision=evaluate_recovery_signal(
                    signal,
                    candles_since_signal=candles,
                    current_price=current_price,
                    active_positions=resolved_active_positions,
                    open_orders=resolved_open_orders,
                ),
            )
        )

    return evaluations


def _resolve_runtime_config(
    group_config: GroupConfig,
    *,
    raw_message: RawMessage,
    source: Source | None,
) -> dict[str, object] | None:
    for group in group_config.groups:
        if not group.enabled:
            continue
        sender = _match_sender(group.tracked_senders, raw_message=raw_message, source=source)
        if sender is not None:
            return {
                "kol_id": sender.custom_label or sender.display_name,
                "trading_mode": sender.trading_mode,
                "max_loss_usdt": sender.max_loss_usdt
                if sender.max_loss_usdt is not None
                else group.max_loss_usdt,
                "symbol_whitelist": sender.symbol_whitelist or group.symbol_whitelist,
            }
        if group.chat_id is not None and group.chat_id == raw_message.chat_id:
            return {
                "kol_id": f"group:{group.chat_id}",
                "trading_mode": group.trading_mode,
                "max_loss_usdt": group.max_loss_usdt,
                "symbol_whitelist": group.symbol_whitelist,
            }
    return None


def _match_sender(
    senders: list[TrackedSenderConfig],
    *,
    raw_message: RawMessage,
    source: Source | None,
) -> TrackedSenderConfig | None:
    for sender in senders:
        if sender.telegram_sender_id is not None and sender.telegram_sender_id in {
            raw_message.sender_id,
            source.telegram_sender_id if source is not None else None,
        }:
            return sender
        names = {
            raw_message.sender_name,
            source.display_name if source is not None else None,
            source.custom_label if source is not None else None,
        }
        if sender.display_name in names or (sender.custom_label and sender.custom_label in names):
            return sender
    return None


def _parse_entry_range(entry_text: str | None) -> tuple[float, float] | None:
    if not entry_text:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", entry_text)
    if len(values) == 1:
        price = float(values[0])
        return price, price
    if len(values) < 2:
        return None
    return float(values[0]), float(values[1])


def _storage_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return normalize_to_utc_naive(value)


def _format_entry_range(entry_range: tuple[float, float] | None) -> str | None:
    if entry_range is None:
        return None
    return f"{entry_range[0]:g}-{entry_range[1]:g}"


def _entry_range_was_touched(
    entry_range: tuple[float, float],
    candles: list[PriceCandle],
) -> bool:
    lower, upper = sorted(entry_range)
    return any(candle.low <= upper and candle.high >= lower for candle in candles)


def _price_in_range(price: float, entry_range: tuple[float, float]) -> bool:
    lower, upper = sorted(entry_range)
    return lower <= price <= upper


def _has_existing_order(signal: RecoverySignal, open_orders: list[OpenOrder]) -> bool:
    symbol = signal.symbol.upper() if signal.symbol else None
    side = signal.side.lower() if signal.side else None
    return any(
        order.kol_id == signal.kol_id
        and order.chat_id == signal.chat_id
        and order.source_message_id == signal.message_id
        and order.symbol.upper() == symbol
        and order.side.lower() == side
        for order in open_orders
    )
