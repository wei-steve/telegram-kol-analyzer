from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path

from telegram_kol_research.kol_pnl_audit import (
    AuditStrategyResult,
    NormalizedAuditStrategy,
)
from telegram_kol_research.kol_pnl_reporting import (
    AuditReportMetadata,
    compare_lifecycle_snapshot,
    render_audit_json,
    render_audit_markdown,
    summarize_audit_results,
    write_audit_artifacts,
)


FIXTURES = Path(__file__).parent / "fixtures" / "kol_pnl_audit"


def _result(
    audit_id,
    symbol,
    realized_r,
    *,
    status="closed",
    confidence="high",
    minute=0,
):
    entered = None if status in {"unfilled", "cancelled"} else datetime(
        2026, 7, 1, 0, minute, tzinfo=UTC
    )
    return AuditStrategyResult(
        audit_id=audit_id,
        symbol=symbol,
        side="long",
        status=status,
        entry_price=Decimal("100") if entered else None,
        entered_at=entered,
        filled_entry_allocation_pct=Decimal("100") if entered else Decimal("0"),
        initial_risk=Decimal("10") if entered else None,
        exits=(),
        targets_reached=1 if Decimal(str(realized_r)) > 0 else 0,
        realized_r=Decimal(str(realized_r)),
        realized_return_pct=Decimal(str(realized_r)),
        open_allocation_pct=Decimal("0") if status == "closed" else Decimal("100") if entered else Decimal("0"),
        exit_reason="test" if status == "closed" else None,
        confidence=confidence,
        reason_codes=(),
    )


def _strategy(payload, message_id, *, side="long", symbol="BTC", management=False):
    strategy_payload = {
        **payload,
        "audit_id": f"-1002368892075:{message_id}:{symbol}:{side}:1",
        "symbol": symbol,
        "side": side,
        "evidence": [{
            "message_id": message_id,
            "posted_at": payload["published_at"],
            "role": "strategy",
        }],
    }
    if management:
        strategy_payload["evidence"].append({
            "message_id": 1005,
            "posted_at": "2026-06-09T02:17:06Z",
            "role": "management",
        })
        strategy_payload["management_events"] = [{
            "event_type": "move_stop_to_break_even",
            "message_id": 1005,
            "occurred_at": "2026-06-09T02:17:06Z",
        }]
    return NormalizedAuditStrategy.from_dict(strategy_payload)


def _base_payload():
    return {
        "chat_id": -1002368892075,
        "ordinal": 1,
        "published_at": "2026-06-08T01:17:06Z",
        "entry_legs": [{"price": "62450", "allocation_pct": "100"}],
        "stop": {"price": "61700", "trigger": "close", "interval": "15m"},
        "take_profits": ["62950", "63250"],
        "management_events": [],
        "confidence": "high",
        "reason_codes": [],
    }


def test_strict_summary_calculates_risk_metrics_by_symbol_and_combined():
    results = [
        _result("btc-1", "BTC", "2", minute=1),
        _result("btc-2", "BTC", "-1", minute=2),
        _result("btc-3", "BTC", "-0.5", minute=3),
        _result("btc-4", "BTC", "1", minute=4),
        _result("btc-open", "BTC", "0.2", status="open", minute=5),
        _result("btc-low", "BTC", "9", confidence="low", minute=6),
        _result("eth-1", "ETH", "1.5", minute=7),
        _result("eth-be", "ETH", "0", minute=8),
        _result("eth-unfilled", "ETH", "0", status="unfilled"),
    ]

    summaries = summarize_audit_results(results)

    btc = summaries["BTC"]
    assert btc.strategy_count == 6
    assert btc.strict_closed_count == 4
    assert btc.profitable_count == 2
    assert btc.loss_count == 2
    assert btc.win_rate == Decimal("50")
    assert btc.cumulative_r == Decimal("1.5")
    assert btc.profit_factor == Decimal("2")
    assert btc.max_drawdown_r == Decimal("1.5")
    assert btc.max_loss_streak == 2
    assert summaries["ETH"].break_even_count == 1
    assert summaries["COMBINED"].strict_closed_count == 6


def test_lifecycle_comparison_reports_missing_duplicates_corruption_and_events():
    payload = _base_payload()
    strategies = [
        _strategy(payload, 1001, management=True),
        _strategy(payload, 1003),
    ]
    lifecycles = json.loads(
        (FIXTURES / "lifecycles.json").read_text(encoding="utf-8")
    )

    differences = compare_lifecycle_snapshot(strategies, lifecycles)
    codes = {item.code for item in differences}

    assert {
        "missing_strategy",
        "duplicate_lifecycle",
        "wrong_entry_price",
        "impossible_timestamp_order",
        "wrong_status",
        "missing_management_event",
    } <= codes


def test_json_and_markdown_reports_are_deterministic_and_evidence_backed(tmp_path):
    result = _result("btc-1", "BTC", "1.25", minute=1)
    summaries = summarize_audit_results([result])
    metadata = AuditReportMetadata(
        audit_cutoff="2026-08-01T02:19:12Z",
        source_sha256="source-digest",
        candle_sha256="candle-digest",
        decision_sha256="decision-digest",
        code_revision="abc123",
        methodology_version="1",
    )

    first_json = render_audit_json(
        results=[result], summaries=summaries, differences=[], metadata=metadata
    )
    second_json = render_audit_json(
        results=[result], summaries=summaries, differences=[], metadata=metadata
    )
    markdown = render_audit_markdown(
        results=[result], summaries=summaries, differences=[], metadata=metadata
    )

    assert first_json == second_json
    assert json.loads(first_json)["metadata"]["source_sha256"] == "source-digest"
    assert "# KOL Strategy PnL Audit" in markdown
    assert "btc-1" in markdown
    assert "BTC" in markdown

    written = write_audit_artifacts(
        output_dir=tmp_path,
        results=[result],
        summaries=summaries,
        differences=[],
        metadata=metadata,
    )
    assert written.json_path.read_text(encoding="utf-8") == first_json
    assert written.markdown_path.read_text(encoding="utf-8") == markdown
