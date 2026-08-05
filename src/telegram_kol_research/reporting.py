"""Leaderboard and drill-down reporting helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.analytics import compute_summary_metrics
from telegram_kol_research.models import RawMessage, SignalCandidate, Source, TradeIdea


def format_entry_preamble_assembly_summary(
    evidence: object,
) -> dict[str, object] | None:
    """Project persisted sizing evidence into a bounded operator-facing summary."""

    if not isinstance(evidence, dict):
        return None
    try:
        mode = str(evidence["mode"])
        configured = float(evidence["configured_risk_budget_usdt"])
        multiplier = float(evidence["risk_multiplier"])
        applied_multiplier = float(
            evidence.get("applied_risk_multiplier", evidence["risk_multiplier"])
        )
        effective = float(evidence["effective_risk_budget_usdt"])
        preamble_message_id = int(evidence["preamble_message_id"])
        strategy_message_id = int(evidence["strategy_message_id"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        mode not in {"shadow", "live"}
        or configured < 0
        or not 0 < multiplier <= 1
        or not 0 < applied_multiplier <= 1
        or effective < 0
        or preamble_message_id <= 0
        or strategy_message_id <= 0
    ):
        return None

    def compact(value: float) -> str:
        return f"{value:.8f}".rstrip("0").rstrip(".")

    proposed_percent = compact(multiplier * 100)
    applied_percent = compact(applied_multiplier * 100)
    multiplier_label = "实际倍率" if mode == "shadow" else "仓位倍率"
    summary = {
        "mode": mode,
        "risk_calculation": (
            f"基础风险预算 {compact(configured)} USDT × {multiplier_label} {applied_percent}% "
            f"= 实际风险预算 {compact(effective)} USDT"
        ),
        "message_pair": (
            f"前置消息 {preamble_message_id} / 策略消息 {strategy_message_id}"
        ),
        "configured_risk_budget_usdt": configured,
        "risk_multiplier": multiplier,
        "effective_risk_budget_usdt": effective,
        "preamble_message_id": preamble_message_id,
        "strategy_message_id": strategy_message_id,
    }
    if mode == "shadow" and applied_multiplier != multiplier:
        summary["applied_risk_multiplier"] = applied_multiplier
        summary["proposed_risk_calculation"] = (
            f"影子建议：基础风险预算 {compact(configured)} USDT × "
            f"仓位倍率 {proposed_percent}% = 建议风险预算 "
            f"{compact(configured * multiplier)} USDT"
        )
    return summary


def render_leaderboard_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order leaderboard rows by quality-adjusted rank."""

    ranked_rows: list[dict[str, Any]] = []
    for row in rows:
        quality_adjusted_rank = row.get("win_rate", 0.0) * row.get("quality_score", 0.0)
        ranked_rows.append({**row, "quality_adjusted_rank": round(quality_adjusted_rank, 4)})

    return sorted(
        ranked_rows,
        key=lambda row: (
            row.get("quality_adjusted_rank", 0.0),
            row.get("quality_score", 0.0),
            row.get("win_rate", 0.0),
        ),
        reverse=True,
    )


def render_drilldown_rows(rows: list[dict[str, Any]], *, source: str) -> list[dict[str, Any]]:
    """Return drill-down rows for a single source."""

    return [row for row in rows if row.get("source") == source]


def load_leaderboard_rows(
    session_factory: sessionmaker,
    *,
    mode: str = "strict",
) -> list[dict[str, Any]]:
    """Load leaderboard rows from trade ideas, with candidate fallback."""

    with session_factory() as session:
        trade_query = (
            session.query(TradeIdea, Source)
            .join(Source, TradeIdea.source_id == Source.id)
            .filter(TradeIdea.status.in_(["win", "loss"]))
        )

        if mode == "strict":
            trade_query = trade_query.filter(TradeIdea.confidence >= 0.8)

        trade_rows = trade_query.all()
        if trade_rows:
            grouped_trades: dict[str, list[dict[str, Any]]] = {}
            for trade_idea, source in trade_rows:
                source_name = source.custom_label or source.display_name
                grouped_trades.setdefault(source_name, []).append(
                    {
                        "status": trade_idea.status,
                        "pnl": trade_idea.pnl_r_multiple or 0.0,
                    }
                )

            rows: list[dict[str, Any]] = []
            for source_name, trades in grouped_trades.items():
                summary = compute_summary_metrics(trades)
                rows.append(
                    {
                        "source": source_name,
                        "sample_size": summary.closed_trade_count,
                        "win_rate": summary.win_rate,
                        "quality_score": summary.quality_score,
                        "profit_factor": summary.profit_factor,
                    }
                )
            return render_leaderboard_rows(rows)

        candidate_query = (
            session.query(SignalCandidate, RawMessage)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
        )

        if mode == "strict":
            candidate_query = candidate_query.filter(
                SignalCandidate.review_status == "confirmed",
                SignalCandidate.confidence >= 0.8,
            )
        else:
            candidate_query = candidate_query.filter(
                SignalCandidate.review_status.in_(["confirmed", "pending"])
            )

        grouped_candidates: dict[str, dict[str, Any]] = {}
        for candidate, raw_message in candidate_query.all():
            source = raw_message.sender_name or str(raw_message.sender_id or "unknown")
            row = grouped_candidates.setdefault(
                source,
                {
                    "source": source,
                    "sample_size": 0,
                    "confirmed_count": 0,
                    "quality_score_total": 0.0,
                },
            )
            row["sample_size"] += 1
            row["quality_score_total"] += candidate.confidence
            if candidate.review_status == "confirmed":
                row["confirmed_count"] += 1

    rows: list[dict[str, Any]] = []
    for row in grouped_candidates.values():
        sample_size = row["sample_size"]
        confirmed_count = row["confirmed_count"]
        quality_score = row["quality_score_total"] / sample_size if sample_size else 0.0
        rows.append(
            {
                "source": row["source"],
                "sample_size": sample_size,
                "win_rate": round(confirmed_count / sample_size, 2) if sample_size else 0.0,
                "quality_score": round(quality_score, 2),
            }
        )

    return render_leaderboard_rows(rows)


def write_report(output_path: str | Path, payload: dict[str, Any]) -> Path:
    """Write a report payload to a local JSON file."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
