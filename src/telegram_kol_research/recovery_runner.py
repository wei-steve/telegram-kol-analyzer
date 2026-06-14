"""Dry-run orchestration for restart recovery scanning."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_scan import (
    AccountStateProvider,
    MarketDataProvider,
    RecoveryEvaluation,
    build_recovery_window,
    evaluate_recovery_signals_with_market_data,
    load_recovery_signals_from_db,
)


class RecoveryDryRunProviderMissingError(RuntimeError):
    """Raised when dry-run dependencies are intentionally not configured."""


@dataclass(slots=True)
class RecoveryDryRunResult:
    total_candidates: int
    action_counts: dict[str, int]
    evaluations: list[RecoveryEvaluation]


def run_recovery_dry_run(
    session_factory: sessionmaker,
    *,
    group_config: GroupConfig,
    now: datetime,
    market_data: MarketDataProvider | None = None,
    account_state: AccountStateProvider | None = None,
    lookback_hours: int = 48,
    persist: bool = False,
) -> RecoveryDryRunResult:
    """Load restart-recovery candidates and evaluate them without placing orders."""

    if market_data is None:
        raise RecoveryDryRunProviderMissingError("market data provider is not configured")

    start_at, end_at = build_recovery_window(now=now, lookback_hours=lookback_hours)
    signals = load_recovery_signals_from_db(
        session_factory,
        group_config=group_config,
        start_at=start_at,
        end_at=end_at,
    )
    evaluations = evaluate_recovery_signals_with_market_data(
        signals,
        market_data=market_data,
        account_state=account_state,
        now=end_at,
    )
    if persist:
        persist_recovery_evaluations(session_factory, evaluations, run_at=end_at)
    action_counts = Counter(evaluation.decision.action for evaluation in evaluations)
    return RecoveryDryRunResult(
        total_candidates=len(signals),
        action_counts=dict(action_counts),
        evaluations=evaluations,
    )
