"""Performance analytics helpers for normalized trade ideas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(slots=True)
class SummaryMetrics:
    closed_trade_count: int
    win_rate: float
    average_win: float
    average_loss: float
    profit_factor: float
    risk_reward: float
    max_loss_streak: int
    quality_score: float


def _compute_max_loss_streak(trades: Iterable[dict]) -> int:
    max_streak = 0
    current_streak = 0
    for trade in trades:
        if trade.get("status") == "loss":
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    return max_streak


def compute_summary_metrics(trades: list[dict]) -> SummaryMetrics:
    """Compute summary metrics over closed trade records."""

    closed_trades = [
        trade for trade in trades if trade.get("status") in {"win", "loss"}
    ]
    wins = [trade.get("pnl", 0.0) for trade in closed_trades if trade.get("status") == "win"]
    losses = [trade.get("pnl", 0.0) for trade in closed_trades if trade.get("status") == "loss"]

    closed_trade_count = len(closed_trades)
    win_rate = (len(wins) / closed_trade_count) if closed_trade_count else 0.0
    average_win = (sum(wins) / len(wins)) if wins else 0.0
    average_loss = (sum(losses) / len(losses)) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else gross_profit
    risk_reward = (average_win / abs(average_loss)) if average_loss else average_win
    max_loss_streak = _compute_max_loss_streak(closed_trades)
    quality_score = round(win_rate * min(1.0, closed_trade_count / 10), 2)

    return SummaryMetrics(
        closed_trade_count=closed_trade_count,
        win_rate=round(win_rate, 2),
        average_win=round(average_win, 2),
        average_loss=round(average_loss, 2),
        profit_factor=round(profit_factor, 2),
        risk_reward=round(risk_reward, 2),
        max_loss_streak=max_loss_streak,
        quality_score=quality_score,
    )


def filter_strict_trades(trades: Iterable[dict]) -> list[dict]:
    """Return only high-confidence confirmed trades for strict reporting."""

    return [
        trade
        for trade in trades
        if trade.get("review_status") == "confirmed"
        and trade.get("confidence", 0.0) >= 0.8
    ]


def filter_expanded_trades(trades: Iterable[dict]) -> list[dict]:
    """Return trades allowed in expanded reporting."""

    return [
        trade
        for trade in trades
        if trade.get("review_status") in {"confirmed", "pending"}
    ]
