"""Conservative auto-trading eligibility decisions for parsed KOL signals."""

from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_SYMBOL_WHITELIST = ["BTC", "ETH"]


@dataclass(slots=True)
class TradingDecisionInput:
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str | None
    side: str | None
    entry_text: str | None
    stop_loss_text: str | None
    take_profit_text: str | None
    parse_source: str = "text"
    confidence: float = 0.0
    trading_mode: str = "notify_only"
    max_loss_usdt: float = 20.0
    symbol_whitelist: list[str] = field(default_factory=lambda: DEFAULT_SYMBOL_WHITELIST.copy())


@dataclass(slots=True)
class ActivePosition:
    kol_id: str
    chat_id: int
    symbol: str
    side: str
    pos_id: str | None = None


@dataclass(slots=True)
class TradingDecision:
    action: str
    reason_codes: list[str]
    max_loss_usdt: float


def evaluate_trading_decision(
    signal: TradingDecisionInput,
    *,
    active_positions: list[ActivePosition] | None = None,
) -> TradingDecision:
    """Return whether a parsed signal is eligible for future automated execution."""

    if signal.trading_mode != "auto_trade":
        return TradingDecision(
            action="notify_only",
            reason_codes=["notify_only_mode"],
            max_loss_usdt=signal.max_loss_usdt,
        )

    reason_codes: list[str] = []
    symbol = signal.symbol.upper() if signal.symbol else None
    side = signal.side.lower() if signal.side else None
    symbol_whitelist = {allowed.upper() for allowed in signal.symbol_whitelist}

    if symbol is None or symbol not in symbol_whitelist:
        reason_codes.append("symbol_not_whitelisted")
    if side not in {"long", "short"}:
        reason_codes.append("missing_side")
    if not signal.stop_loss_text:
        reason_codes.append("missing_stop_loss")
    if signal.parse_source in {"image", "vision", "ocr"}:
        reason_codes.append("vision_requires_review")
    if _has_duplicate_active_position(signal, active_positions or []):
        reason_codes.append("duplicate_active_position")

    if reason_codes:
        return TradingDecision(
            action="manual_review",
            reason_codes=reason_codes,
            max_loss_usdt=signal.max_loss_usdt,
        )

    return TradingDecision(
        action="eligible_for_auto_trade",
        reason_codes=["risk_checks_passed"],
        max_loss_usdt=signal.max_loss_usdt,
    )


def _has_duplicate_active_position(
    signal: TradingDecisionInput,
    active_positions: list[ActivePosition],
) -> bool:
    signal_symbol = signal.symbol.upper() if signal.symbol else None
    signal_side = signal.side.lower() if signal.side else None
    return any(
        position.kol_id == signal.kol_id
        and position.chat_id == signal.chat_id
        and position.symbol.upper() == signal_symbol
        and position.side.lower() == signal_side
        for position in active_positions
    )
