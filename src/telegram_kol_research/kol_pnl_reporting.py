"""Deterministic summaries and artifacts for read-only KOL PnL audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from telegram_kol_research.kol_pnl_audit import (
    AuditStrategyResult,
    NormalizedAuditStrategy,
)


@dataclass(frozen=True, slots=True)
class AuditSummary:
    symbol: str
    strategy_count: int
    entered_count: int
    open_count: int
    unresolved_count: int
    strict_closed_count: int
    profitable_count: int
    loss_count: int
    break_even_count: int
    win_rate: Decimal
    cumulative_r: Decimal
    average_r: Decimal
    profit_factor: Decimal | None
    max_drawdown_r: Decimal
    max_loss_streak: int


@dataclass(frozen=True, slots=True)
class LifecycleDifference:
    audit_id: str
    lifecycle_id: int | None
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class AuditReportMetadata:
    audit_cutoff: str
    source_sha256: str
    candle_sha256: str
    decision_sha256: str
    code_revision: str
    methodology_version: str


@dataclass(frozen=True, slots=True)
class WrittenAuditArtifacts:
    json_path: Path
    markdown_path: Path


def summarize_audit_results(
    results: Iterable[AuditStrategyResult],
    *,
    confidence: tuple[str, ...] = ("high", "medium"),
) -> dict[str, AuditSummary]:
    """Return strict BTC, ETH, and combined performance summaries."""

    rows = tuple(results)
    return {
        symbol: _summary_for_symbol(rows, symbol=symbol, confidence=confidence)
        for symbol in ("BTC", "ETH", "COMBINED")
    }


def compare_lifecycle_snapshot(
    strategies: Iterable[NormalizedAuditStrategy],
    lifecycle_rows: Iterable[Mapping[str, Any]],
) -> tuple[LifecycleDifference, ...]:
    """Compare an independent reconstruction with current lifecycle rows."""

    lifecycles = tuple(lifecycle_rows)
    differences: list[LifecycleDifference] = []
    for strategy in strategies:
        source_message_id = strategy.evidence[0].message_id
        matches = [
            row
            for row in lifecycles
            if _integer(row.get("chat_id")) == strategy.chat_id
            and _integer(row.get("message_id")) == source_message_id
            and _symbol(row.get("symbol")) == strategy.symbol
            and _side(row.get("side")) == strategy.side
        ]
        if not matches:
            differences.append(LifecycleDifference(
                audit_id=strategy.audit_id,
                lifecycle_id=None,
                code="missing_strategy",
                detail=f"no lifecycle row for source message {source_message_id}",
            ))
            continue
        if len(matches) > 1:
            differences.append(LifecycleDifference(
                audit_id=strategy.audit_id,
                lifecycle_id=None,
                code="duplicate_lifecycle",
                detail=f"{len(matches)} lifecycle rows match one audit strategy",
            ))
        statuses = {str(row.get("lifecycle_status") or "") for row in matches}
        if len(statuses) > 1:
            differences.append(LifecycleDifference(
                audit_id=strategy.audit_id,
                lifecycle_id=None,
                code="wrong_status",
                detail=f"duplicate lifecycle statuses disagree: {sorted(statuses)}",
            ))
        entry_prices = {item.price for item in strategy.entry_legs}
        for row in matches:
            lifecycle_id = _integer(row.get("id"))
            observed = {
                value
                for value in (
                    _decimal_or_none(row.get("entry_range_low")),
                    _decimal_or_none(row.get("entry_range_high")),
                    _decimal_or_none(row.get("entry_price_actual")),
                )
                if value is not None
            }
            if observed and not observed.intersection(entry_prices):
                differences.append(LifecycleDifference(
                    audit_id=strategy.audit_id,
                    lifecycle_id=lifecycle_id,
                    code="wrong_entry_price",
                    detail=(
                        f"audit entries={sorted(map(str, entry_prices))}; "
                        f"lifecycle entries={sorted(map(str, observed))}"
                    ),
                ))
            signal_at = _timestamp_or_none(row.get("signal_at"))
            entered_at = _timestamp_or_none(row.get("entered_at"))
            exited_at = _timestamp_or_none(row.get("exited_at"))
            impossible = (
                (signal_at is not None and entered_at is not None and entered_at < signal_at)
                or (
                    entered_at is not None
                    and exited_at is not None
                    and exited_at < entered_at
                )
                or (
                    signal_at is not None
                    and exited_at is not None
                    and exited_at < signal_at
                )
            )
            if impossible:
                differences.append(LifecycleDifference(
                    audit_id=strategy.audit_id,
                    lifecycle_id=lifecycle_id,
                    code="impossible_timestamp_order",
                    detail="lifecycle timestamps are not chronological",
                ))

        linked_management = {
            _integer(row.get(field))
            for row in matches
            for field in (
                "entry_signal_message_id",
                "exit_signal_message_id",
                "management_signal_message_id",
            )
            if _integer(row.get(field)) is not None
        }
        for event in strategy.management_events:
            if event.message_id not in linked_management:
                differences.append(LifecycleDifference(
                    audit_id=strategy.audit_id,
                    lifecycle_id=None,
                    code="missing_management_event",
                    detail=f"management message {event.message_id} is not linked",
                ))

    return tuple(sorted(
        differences,
        key=lambda item: (item.audit_id, item.code, item.lifecycle_id or -1, item.detail),
    ))


def render_audit_json(
    *,
    results: Iterable[AuditStrategyResult],
    summaries: Mapping[str, AuditSummary],
    differences: Iterable[LifecycleDifference],
    metadata: AuditReportMetadata,
) -> str:
    payload = {
        "metadata": _metadata_dict(metadata),
        "summaries": {
            key: _summary_dict(value) for key, value in sorted(summaries.items())
        },
        "results": [_result_dict(item) for item in sorted(results, key=_result_sort_key)],
        "lifecycle_differences": [
            {
                "audit_id": item.audit_id,
                "lifecycle_id": item.lifecycle_id,
                "code": item.code,
                "detail": item.detail,
            }
            for item in differences
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_audit_markdown(
    *,
    results: Iterable[AuditStrategyResult],
    summaries: Mapping[str, AuditSummary],
    differences: Iterable[LifecycleDifference],
    metadata: AuditReportMetadata,
) -> str:
    lines = [
        "# KOL Strategy PnL Audit",
        "",
        f"- Audit cutoff: `{metadata.audit_cutoff}`",
        f"- Source digest: `{metadata.source_sha256}`",
        f"- Candle digest: `{metadata.candle_sha256}`",
        f"- Decision digest: `{metadata.decision_sha256}`",
        f"- Code revision: `{metadata.code_revision}`",
        f"- Methodology: `{metadata.methodology_version}`",
        "",
        "## Summary",
        "",
        "| Symbol | Strategies | Strict closed | Profitable | Loss | Break-even | Win rate | Cumulative R | Max drawdown R |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for symbol in ("BTC", "ETH", "COMBINED"):
        row = summaries[symbol]
        lines.append(
            f"| {symbol} | {row.strategy_count} | {row.strict_closed_count} | "
            f"{row.profitable_count} | {row.loss_count} | {row.break_even_count} | "
            f"{_decimal_text(row.win_rate)}% | {_decimal_text(row.cumulative_r)} | "
            f"{_decimal_text(row.max_drawdown_r)} |"
        )
    lines.extend([
        "",
        "## Per-strategy results",
        "",
        "| Audit ID | Symbol | Side | Status | Confidence | Realized R | Open % | Exit reason |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ])
    for item in sorted(results, key=_result_sort_key):
        lines.append(
            f"| `{item.audit_id}` | {item.symbol} | {item.side} | {item.status} | "
            f"{item.confidence} | {_decimal_text(item.realized_r)} | "
            f"{_decimal_text(item.open_allocation_pct)} | {item.exit_reason or '--'} |"
        )
    lines.extend(["", "## Lifecycle differences", ""])
    differences = tuple(differences)
    if not differences:
        lines.append("No lifecycle differences were supplied.")
    else:
        lines.extend([
            "| Audit ID | Lifecycle ID | Code | Detail |",
            "| --- | ---: | --- | --- |",
        ])
        for item in differences:
            lines.append(
                f"| `{item.audit_id}` | {item.lifecycle_id or '--'} | "
                f"{item.code} | {item.detail} |"
            )
    return "\n".join(lines) + "\n"


def write_audit_artifacts(
    *,
    output_dir: str | Path,
    results: Iterable[AuditStrategyResult],
    summaries: Mapping[str, AuditSummary],
    differences: Iterable[LifecycleDifference],
    metadata: AuditReportMetadata,
) -> WrittenAuditArtifacts:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result_rows = tuple(results)
    difference_rows = tuple(differences)
    json_text = render_audit_json(
        results=result_rows,
        summaries=summaries,
        differences=difference_rows,
        metadata=metadata,
    )
    markdown_text = render_audit_markdown(
        results=result_rows,
        summaries=summaries,
        differences=difference_rows,
        metadata=metadata,
    )
    json_path = directory / "results.json"
    markdown_path = directory / "report.md"
    _atomic_write(json_path, json_text)
    _atomic_write(markdown_path, markdown_text)
    return WrittenAuditArtifacts(json_path=json_path, markdown_path=markdown_path)


def _summary_for_symbol(
    results: tuple[AuditStrategyResult, ...],
    *,
    symbol: str,
    confidence: tuple[str, ...],
) -> AuditSummary:
    scoped = results if symbol == "COMBINED" else tuple(
        item for item in results if item.symbol == symbol
    )
    strict = tuple(
        sorted(
            (
                item
                for item in scoped
                if item.status == "closed" and item.confidence in confidence
            ),
            key=_result_sort_key,
        )
    )
    profitable = tuple(item for item in strict if item.realized_r > 0)
    losses = tuple(item for item in strict if item.realized_r < 0)
    break_even = tuple(item for item in strict if item.realized_r == 0)
    decided = len(profitable) + len(losses)
    gross_profit = sum((item.realized_r for item in profitable), Decimal("0"))
    gross_loss = abs(sum((item.realized_r for item in losses), Decimal("0")))
    cumulative = sum((item.realized_r for item in strict), Decimal("0"))
    return AuditSummary(
        symbol=symbol,
        strategy_count=len(scoped),
        entered_count=sum(item.entry_price is not None for item in scoped),
        open_count=sum(item.status == "open" for item in scoped),
        unresolved_count=sum(
            item.status == "unresolved" or item.confidence not in confidence
            for item in scoped
        ),
        strict_closed_count=len(strict),
        profitable_count=len(profitable),
        loss_count=len(losses),
        break_even_count=len(break_even),
        win_rate=(
            Decimal(len(profitable)) / Decimal(decided) * Decimal("100")
            if decided
            else Decimal("0")
        ),
        cumulative_r=cumulative,
        average_r=cumulative / Decimal(len(strict)) if strict else Decimal("0"),
        profit_factor=(gross_profit / gross_loss if gross_loss else None),
        max_drawdown_r=_max_drawdown(strict),
        max_loss_streak=_max_loss_streak(strict),
    )


def _max_drawdown(results: tuple[AuditStrategyResult, ...]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    maximum = Decimal("0")
    for item in results:
        equity += item.realized_r
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _max_loss_streak(results: tuple[AuditStrategyResult, ...]) -> int:
    current = 0
    maximum = 0
    for item in results:
        if item.realized_r < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _result_sort_key(item: AuditStrategyResult) -> tuple[datetime, str]:
    fallback = datetime.max.replace(tzinfo=UTC)
    occurred_at = item.exits[-1].occurred_at if item.exits else item.entered_at or fallback
    return occurred_at, item.audit_id


def _metadata_dict(metadata: AuditReportMetadata) -> dict[str, str]:
    return {
        "audit_cutoff": metadata.audit_cutoff,
        "source_sha256": metadata.source_sha256,
        "candle_sha256": metadata.candle_sha256,
        "decision_sha256": metadata.decision_sha256,
        "code_revision": metadata.code_revision,
        "methodology_version": metadata.methodology_version,
    }


def _summary_dict(summary: AuditSummary) -> dict[str, Any]:
    return {
        "symbol": summary.symbol,
        "strategy_count": summary.strategy_count,
        "entered_count": summary.entered_count,
        "open_count": summary.open_count,
        "unresolved_count": summary.unresolved_count,
        "strict_closed_count": summary.strict_closed_count,
        "profitable_count": summary.profitable_count,
        "loss_count": summary.loss_count,
        "break_even_count": summary.break_even_count,
        "win_rate": _decimal_text(summary.win_rate),
        "cumulative_r": _decimal_text(summary.cumulative_r),
        "average_r": _decimal_text(summary.average_r),
        "profit_factor": (
            _decimal_text(summary.profit_factor)
            if summary.profit_factor is not None
            else None
        ),
        "max_drawdown_r": _decimal_text(summary.max_drawdown_r),
        "max_loss_streak": summary.max_loss_streak,
    }


def _result_dict(result: AuditStrategyResult) -> dict[str, Any]:
    return {
        "audit_id": result.audit_id,
        "symbol": result.symbol,
        "side": result.side,
        "status": result.status,
        "entry_price": _decimal_or_text(result.entry_price),
        "entered_at": _timestamp_text(result.entered_at),
        "filled_entry_allocation_pct": _decimal_text(
            result.filled_entry_allocation_pct
        ),
        "initial_risk": _decimal_or_text(result.initial_risk),
        "exits": [
            {
                "price": _decimal_text(item.price),
                "allocation_pct": _decimal_text(item.allocation_pct),
                "occurred_at": _timestamp_text(item.occurred_at),
                "reason": item.reason,
                "realized_r": _decimal_text(item.realized_r),
                "return_pct": _decimal_text(item.return_pct),
            }
            for item in result.exits
        ],
        "targets_reached": result.targets_reached,
        "realized_r": _decimal_text(result.realized_r),
        "realized_return_pct": _decimal_text(result.realized_return_pct),
        "open_allocation_pct": _decimal_text(result.open_allocation_pct),
        "exit_reason": result.exit_reason,
        "confidence": result.confidence,
        "reason_codes": list(result.reason_codes),
    }


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def _symbol(value: Any) -> str:
    normalized = str(value or "").upper().replace("-", "").replace("_", "")
    if normalized.startswith("BTC"):
        return "BTC"
    if normalized.startswith("ETH"):
        return "ETH"
    return normalized


def _side(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return {"多": "long", "空": "short"}.get(normalized, normalized)


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def _timestamp_or_none(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _decimal_or_text(value: Decimal | None) -> str | None:
    return _decimal_text(value) if value is not None else None
