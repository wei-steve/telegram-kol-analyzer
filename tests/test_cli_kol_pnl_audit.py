from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.kol_audit_market_data import AuditCandle


def _payloads(tmp_path):
    messages = [{
        "chat_id": -1002368892075,
        "message_id": 6496,
        "posted_at": "2026-06-08T01:17:06Z",
        "text": "比特币62450附近反弹，止损61700，止盈62950。",
    }]
    decisions = {
        "strategies": [{
            "audit_id": "-1002368892075:6496:BTC:long:1",
            "chat_id": -1002368892075,
            "symbol": "BTC",
            "side": "long",
            "ordinal": 1,
            "published_at": "2026-06-08T01:17:06Z",
            "source_message_id": 6496,
            "entry_legs": [{"price": "62450", "allocation_pct": "100"}],
            "stop": {"price": "61700", "trigger": "touch"},
            "take_profits": ["62950"],
            "confidence": "high",
            "reason_codes": [],
        }],
        "excluded_messages": [],
        "duplicate_messages": [],
        "event_links": [],
        "unresolved_events": [],
    }
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
    lifecycle_path = tmp_path / "lifecycles.json"
    lifecycle_path.write_text("[]", encoding="utf-8")
    return messages, decisions_path, lifecycle_path


def _candles():
    start = datetime(2026, 6, 8, 1, 20, tzinfo=UTC)
    return (
        AuditCandle(
            opened_at=start,
            closed_at=start + timedelta(minutes=5) - timedelta(milliseconds=1),
            open=62450,
            high=62500,
            low=62400,
            close=62480,
        ),
        AuditCandle(
            opened_at=start + timedelta(minutes=5),
            closed_at=start + timedelta(minutes=10) - timedelta(milliseconds=1),
            open=62500,
            high=63000,
            low=62480,
            close=62900,
        ),
    )


def test_audit_kol_pnl_accepts_stdin_and_writes_only_local_artifacts(
    tmp_path, monkeypatch
):
    messages, decisions_path, lifecycle_path = _payloads(tmp_path)
    calls = []

    def fake_candles(**kwargs):
        calls.append(kwargs)
        return _candles(), "btc-candle-digest"

    monkeypatch.setattr(
        "telegram_kol_research.cli._capture_or_load_audit_candles", fake_candles
    )
    output_dir = tmp_path / "audit-output"

    result = CliRunner().invoke(
        app,
        [
            "audit-kol-pnl",
            "--messages-json", "-",
            "--decisions-json", str(decisions_path),
            "--lifecycle-json", str(lifecycle_path),
            "--chat-id", "-1002368892075",
            "--symbol", "BTC",
            "--cutoff", "2026-06-08T01:40:00Z",
            "--output-dir", str(output_dir),
        ],
        input=json.dumps(messages),
    )

    assert result.exit_code == 0, result.output
    assert "Audit report written" in result.output
    assert (output_dir / "results.json").exists()
    assert (output_dir / "report.md").exists()
    assert (output_dir / "reconstruction.json").exists()
    assert not list(tmp_path.rglob("*.db"))
    assert calls[0]["offline"] is False
    report = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert report["summaries"]["BTC"]["cumulative_r"] == "0.6666666666666666666666666667"


def test_audit_kol_pnl_offline_mode_is_passed_to_cache_loader(tmp_path, monkeypatch):
    messages, decisions_path, _ = _payloads(tmp_path)
    observed = []

    def fake_candles(**kwargs):
        observed.append(kwargs["offline"])
        return _candles(), "offline-digest"

    monkeypatch.setattr(
        "telegram_kol_research.cli._capture_or_load_audit_candles", fake_candles
    )

    result = CliRunner().invoke(
        app,
        [
            "audit-kol-pnl",
            "--messages-json", "-",
            "--decisions-json", str(decisions_path),
            "--chat-id", "-1002368892075",
            "--symbol", "BTC",
            "--cutoff", "2026-06-08T01:40:00Z",
            "--output-dir", str(tmp_path / "offline-output"),
            "--offline",
        ],
        input=json.dumps(messages),
    )

    assert result.exit_code == 0, result.output
    assert observed == [True]


def test_audit_kol_pnl_rejects_broad_output_directory(tmp_path):
    messages, decisions_path, _ = _payloads(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "audit-kol-pnl",
            "--messages-json", "-",
            "--decisions-json", str(decisions_path),
            "--chat-id", "-1002368892075",
            "--symbol", "BTC",
            "--cutoff", "2026-06-08T01:40:00Z",
            "--output-dir", "/",
        ],
        input=json.dumps(messages),
    )

    assert result.exit_code == 2
    assert "bounded output directory" in result.output


def test_audit_kol_pnl_fails_closed_for_unreviewed_candidate(tmp_path):
    messages, decisions_path, _ = _payloads(tmp_path)
    messages.append({
        "chat_id": -1002368892075,
        "message_id": 6497,
        "posted_at": "2026-06-08T02:00:00Z",
        "text": "以太币1700附近做空，止损1720，止盈1650。",
    })
    output_dir = tmp_path / "unresolved"

    result = CliRunner().invoke(
        app,
        [
            "audit-kol-pnl",
            "--messages-json", "-",
            "--decisions-json", str(decisions_path),
            "--chat-id", "-1002368892075",
            "--symbol", "BTC",
            "--symbol", "ETH",
            "--cutoff", "2026-06-08T03:00:00Z",
            "--output-dir", str(output_dir),
        ],
        input=json.dumps(messages),
    )

    assert result.exit_code == 2
    assert "unresolved" in result.output.lower()
    assert (output_dir / "reconstruction.json").exists()
    assert not (output_dir / "results.json").exists()
