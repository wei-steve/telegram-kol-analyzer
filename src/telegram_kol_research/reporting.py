"""Leaderboard and drill-down reporting helpers."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.analytics import compute_summary_metrics
from telegram_kol_research.models import RawMessage, SignalCandidate, Source, TradeIdea


def _compact_number(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_entry_assembly_summary(
    evidence: object,
) -> dict[str, object] | None:
    """Project legacy or v2 entry assembly evidence without raw model/API data."""

    if not isinstance(evidence, dict):
        return None
    if evidence.get("preamble_message_id") not in (None, ""):
        return format_entry_preamble_assembly_summary(evidence)
    try:
        mode = str(evidence["mode"])
        status = str(evidence.get("status") or "none")
        configured = float(evidence["configured_risk_budget_usdt"])
        multiplier = float(evidence.get("risk_multiplier", 1))
        effective = float(evidence["effective_risk_budget_usdt"])
        strategy_message_id = int(evidence["strategy_message_id"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        mode not in {"shadow", "live"}
        or status not in {"none", "proposed", "assembled", "unresolved", "blocked"}
        or not all(math.isfinite(value) for value in (configured, multiplier, effective))
        or configured <= 0
        or not 0 < multiplier <= 1
        or effective <= 0
        or strategy_message_id <= 0
    ):
        return None
    fragment_ids = []
    for value in list(evidence.get("fragment_ids") or [])[:5]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            fragment_ids.append(parsed)
    allocations = []
    for value in list(evidence.get("entry_allocations") or [])[:5]:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and 0 < parsed <= 1:
            allocations.append(parsed)
    supplemental = []
    for value in list(evidence.get("supplemental_entry_prices") or [])[:5]:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            supplemental.append(parsed)
    if status in {"none", "unresolved"}:
        state_label = "等待相邻仓位/补仓消息识别"
    elif status == "blocked":
        state_label = "相邻仓位/补仓消息需要人工处理"
    elif mode == "shadow":
        state_label = "相邻仓位/补仓影子方案"
    else:
        state_label = "相邻仓位/补仓方案已组装"
    result: dict[str, object] = {
        "mode": mode,
        "status": status,
        "state_label": state_label,
        "risk_calculation": (
            f"配置{_compact_number(configured)}U × "
            f"{_compact_number(multiplier * 100)}% = "
            f"实际风险预算{_compact_number(effective)}U"
        ),
        "configured_risk_budget_usdt": configured,
        "risk_multiplier": multiplier,
        "effective_risk_budget_usdt": effective,
        "strategy_message_id": strategy_message_id,
        "fragment_ids": fragment_ids,
        "source_summary": (
            f"策略消息 {strategy_message_id}"
            + (f" · 片段 {'/'.join(str(value) for value in fragment_ids)}" if fragment_ids else "")
        ),
    }
    if allocations:
        equal = len(set(round(value, 8) for value in allocations)) == 1
        count_label = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五"}[
            len(allocations)
        ]
        detail = (
            f"{count_label}档各{_compact_number(allocations[0] * 100)}%"
            if equal
            else "/".join(f"{_compact_number(value * 100)}%" for value in allocations)
        )
        result["allocation_summary"] = f"整单100%；{detail}"
    if supplemental:
        result["supplemental_summary"] = "补仓价 " + "/".join(
            _compact_number(value) for value in supplemental
        )
    return result


def format_entry_revision_summary(
    evidence: object,
) -> dict[str, object] | None:
    """Return a truthful bounded state label for one durable entry revision."""

    if not isinstance(evidence, dict):
        return None
    status = str(evidence.get("status") or "")
    labels = {
        "shadow_planned": "入场修订影子方案",
        "planned": "等待执行入场修订",
        "cancelling_old_entries": "正在撤销旧入场单",
        "old_entries_terminal": "旧入场单已终态，等待重建",
        "rebuilding": "正在重建入场单",
        "reconciling": "等待交易所读回确认",
        "succeeded": "入场修订已读回确认",
        "recovery_required": "入场修订需要人工处理",
        "blocked": "入场修订已阻断",
    }
    if status not in labels:
        return None
    reason = str(evidence.get("reason_code") or "")[:64]
    if reason and not re.fullmatch(r"[a-z0-9_:-]+", reason):
        reason = "entry_revision_reason_redacted"
    result: dict[str, object] = {
        "status": status,
        "label": labels[status],
        "orders_changed": status == "succeeded",
        "replacement_count": max(0, min(int(evidence.get("replacement_count") or 0), 5)),
    }
    if reason:
        result["reason_code"] = reason
    market = evidence.get("market_snapshot")
    risk = market.get("risk_decision") if isinstance(market, dict) else None
    if isinstance(risk, dict):
        try:
            headroom = float(risk.get("remaining_risk_usdt"))
        except (TypeError, ValueError, OverflowError):
            headroom = -1
        if math.isfinite(headroom) and headroom >= 0:
            result["remaining_headroom"] = (
                f"剩余风险余量 {_compact_number(headroom)}U"
            )
    return result


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
