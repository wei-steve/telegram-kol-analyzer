# Telegram KOL Win-Rate Research System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local macOS system that syncs selected archived Telegram strategy groups, reconstructs per-KOL trade ideas from text and image posts, and generates trustworthy win-rate and PnL research reports.

**Architecture:** Use a local-first Python application with `Telethon` for Telegram sync, `SQLite` for persistence, OCR for image-assisted parsing, and a staged analytics pipeline that separates raw messages, signal candidates, normalized trades, and final statistics. Prioritize correctness, replayability, and reviewability over broad automation.

**Tech Stack:** Python 3.12+, Telethon, SQLite, SQLAlchemy, Typer, Pillow, pytesseract, Pytest

---

### Task 1: Scaffold the Local Research App

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/telegram_kol_research/__init__.py`
- Create: `src/telegram_kol_research/config.py`
- Create: `src/telegram_kol_research/cli.py`
- Create: `tests/test_cli_smoke.py`

**Step 1: Write the failing test**

```python
from typer.testing import CliRunner

from telegram_kol_research.cli import app


def test_cli_help_renders():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.stdout
    assert "report" in result.stdout
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_smoke.py -v`
Expected: FAIL with import or module-not-found errors because the package and CLI do not exist yet.

**Step 3: Write minimal implementation**

Create a package skeleton with:

```python
import typer

app = typer.Typer()


@app.command()
def sync():
    """Sync Telegram messages."""


@app.command()
def report():
    """Generate leaderboard reports."""
```

Add `pyproject.toml` dependencies for `telethon`, `sqlalchemy`, `typer`, `pillow`, `pytesseract`, and `pytest`.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_smoke.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pyproject.toml README.md src/telegram_kol_research/__init__.py src/telegram_kol_research/config.py src/telegram_kol_research/cli.py tests/test_cli_smoke.py
git commit -m "chore: scaffold telegram research app"
```

### Task 2: Define Database Models and Bootstrap

**Files:**
- Create: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/models.py`
- Create: `tests/test_db_bootstrap.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


def test_database_bootstrap_creates_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]
    tables = set(Base.metadata.tables)
    assert "raw_messages" in tables
    assert "signal_candidates" in tables
    assert "trade_ideas" in tables
    assert engine is not None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_bootstrap.py -v`
Expected: FAIL because the database module and models are missing.

**Step 3: Write minimal implementation**

Define SQLAlchemy models for:

- `sources`
- `raw_messages`
- `media_assets`
- `signal_candidates`
- `trade_ideas`
- `trade_updates`
- `sync_checkpoints`

Provide `create_session_factory()` and `init_db()` helpers.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db_bootstrap.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/db.py src/telegram_kol_research/models.py tests/test_db_bootstrap.py
git commit -m "feat: add database bootstrap and core models"
```

### Task 3: Add Source Mapping and Target Group Configuration

**Files:**
- Create: `config/groups.example.yaml`
- Create: `src/telegram_kol_research/group_config.py`
- Create: `tests/test_group_config.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.group_config import load_group_config


def test_group_config_loads_target_groups(tmp_path):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: VIP BTC Room\n"
        "    enabled: true\n"
        "    tracked_senders:\n"
        "      - display_name: Alice\n"
    )

    config = load_group_config(config_path)
    assert config.groups[0].chat_title == "VIP BTC Room"
    assert config.groups[0].tracked_senders[0].display_name == "Alice"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_group_config.py -v`
Expected: FAIL because the configuration loader does not exist.

**Step 3: Write minimal implementation**

Define typed configuration objects for:

- Target groups
- Whether a group is enabled
- Explicit tracked senders
- Optional custom KOL labels
- Sync date bounds

Ship an example YAML file that the user can copy and fill in.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_group_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add config/groups.example.yaml src/telegram_kol_research/group_config.py tests/test_group_config.py
git commit -m "feat: add target group and sender mapping config"
```

### Task 4: Implement Telegram Auth and Dialog Discovery

**Files:**
- Create: `src/telegram_kol_research/telegram_client.py`
- Create: `tests/test_dialog_filtering.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.telegram_client import filter_target_dialogs


def test_filter_target_dialogs_keeps_only_enabled_group_titles():
    dialogs = [
        {"title": "VIP BTC Room", "archived": True},
        {"title": "Friends", "archived": False},
    ]
    titles = {"VIP BTC Room"}
    filtered = filter_target_dialogs(dialogs, titles)
    assert filtered == [{"title": "VIP BTC Room", "archived": True}]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_dialog_filtering.py -v`
Expected: FAIL because the Telegram client helpers are missing.

**Step 3: Write minimal implementation**

Add:

- Telethon session bootstrap
- Environment/config loading for `api_id`, `api_hash`, and session path
- Dialog enumeration
- Filtering helpers that keep selected archived groups

Keep the first implementation focused on discovery and filtering, not full sync.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_dialog_filtering.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/telegram_client.py tests/test_dialog_filtering.py
git commit -m "feat: add telegram auth and dialog discovery helpers"
```

### Task 5: Persist Raw Messages and Media Metadata

**Files:**
- Create: `src/telegram_kol_research/raw_ingest.py`
- Create: `tests/test_raw_ingest.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.raw_ingest import normalize_message_payload


def test_normalize_message_payload_keeps_reply_and_media_metadata():
    payload = {
        "chat_id": 1001,
        "message_id": 77,
        "sender_id": 501,
        "text": "BTC long 68000-68200",
        "reply_to_msg_id": 70,
        "media": {"kind": "photo", "path": "media/77.jpg"},
    }
    normalized = normalize_message_payload(payload)
    assert normalized.reply_to_message_id == 70
    assert normalized.media_kind == "photo"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_raw_ingest.py -v`
Expected: FAIL because the raw-ingest module does not exist.

**Step 3: Write minimal implementation**

Implement helpers that:

- Normalize Telethon messages into storage records
- Preserve Telegram IDs for deduplication
- Capture reply relationships
- Track media metadata and local paths
- Mark whether a message came from an archived target group

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_raw_ingest.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/raw_ingest.py tests/test_raw_ingest.py
git commit -m "feat: add raw message normalization and media metadata"
```

### Task 6: Build Historical Backfill Sync

**Files:**
- Create: `src/telegram_kol_research/backfill.py`
- Create: `tests/test_backfill_windows.py`

**Step 1: Write the failing test**

```python
from datetime import datetime, timedelta, timezone

from telegram_kol_research.backfill import compute_backfill_start


def test_compute_backfill_start_defaults_to_90_days():
    now = datetime(2026, 4, 7, tzinfo=timezone.utc)
    start = compute_backfill_start(now=now, days=90)
    assert start == now - timedelta(days=90)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_backfill_windows.py -v`
Expected: FAIL because the backfill module does not exist.

**Step 3: Write minimal implementation**

Implement a backfill runner that:

- Reads target groups from config
- Uses sync checkpoints
- Pulls messages backward from a configurable date window
- Stores raw messages idempotently
- Records progress per chat

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_backfill_windows.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/backfill.py tests/test_backfill_windows.py
git commit -m "feat: add historical backfill pipeline"
```

### Task 7: Add Incremental Listening for New and Edited Messages

**Files:**
- Create: `src/telegram_kol_research/listener.py`
- Create: `tests/test_event_routing.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.listener import should_process_event


def test_should_process_event_rejects_untracked_chat():
    event = {"chat_id": 42}
    tracked_chat_ids = {1001}
    assert should_process_event(event, tracked_chat_ids) is False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_event_routing.py -v`
Expected: FAIL because the listener module does not exist.

**Step 3: Write minimal implementation**

Implement an event listener that:

- Subscribes to new messages and edits
- Filters to tracked groups
- Reuses raw-ingest normalization
- Updates sync checkpoints
- Downloads relevant images when needed

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_event_routing.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/listener.py tests/test_event_routing.py
git commit -m "feat: add incremental listener for tracked groups"
```

### Task 8: Implement Text Parsing for Trade Signal Candidates

**Files:**
- Create: `src/telegram_kol_research/parsing/text_parser.py`
- Create: `tests/parsing/test_text_parser.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.parsing.text_parser import parse_signal_text


def test_parse_signal_text_extracts_basic_long_setup():
    parsed = parse_signal_text("BTC long 68000-68200, SL 67500, TP 69000 / 70000")
    assert parsed.symbol == "BTC"
    assert parsed.side == "long"
    assert parsed.stop_loss == 67500
    assert parsed.take_profits == [69000, 70000]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/parsing/test_text_parser.py -v`
Expected: FAIL because the text parser does not exist.

**Step 3: Write minimal implementation**

Create a parser that:

- Normalizes symbol aliases
- Detects side and leverage
- Extracts entry ranges, stop loss, and take-profit targets
- Returns confidence and event type

Keep the implementation rule-based in v1.

**Step 4: Run test to verify it passes**

Run: `pytest tests/parsing/test_text_parser.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/parsing/text_parser.py tests/parsing/test_text_parser.py
git commit -m "feat: add text parser for signal candidates"
```

### Task 9: Implement OCR-Assisted Parsing for Image Posts

**Files:**
- Create: `src/telegram_kol_research/parsing/ocr_parser.py`
- Create: `tests/parsing/test_ocr_parser.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.parsing.ocr_parser import merge_caption_and_ocr_text


def test_merge_caption_and_ocr_text_keeps_both_sources():
    merged = merge_caption_and_ocr_text(
        caption="BTC long setup",
        ocr_text="Entry 68000-68200 TP 69000 SL 67500",
    )
    assert "BTC long setup" in merged
    assert "Entry 68000-68200" in merged
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/parsing/test_ocr_parser.py -v`
Expected: FAIL because the OCR parser module does not exist.

**Step 3: Write minimal implementation**

Implement OCR helpers that:

- Read local image paths
- Extract text with `pytesseract`
- Merge caption text plus OCR text into one parse input
- Lower confidence by default for image-only signals

**Step 4: Run test to verify it passes**

Run: `pytest tests/parsing/test_ocr_parser.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/parsing/ocr_parser.py tests/parsing/test_ocr_parser.py
git commit -m "feat: add ocr-assisted parsing for image posts"
```

### Task 10: Add Candidate Review States and Persistence

**Files:**
- Create: `src/telegram_kol_research/candidates.py`
- Create: `tests/test_candidates.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.candidates import classify_candidate


def test_classify_candidate_marks_low_confidence_as_pending_review():
    result = classify_candidate(confidence=0.42)
    assert result.review_status == "pending"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_candidates.py -v`
Expected: FAIL because the candidate classification logic does not exist.

**Step 3: Write minimal implementation**

Add candidate handling that:

- Stores parse results
- Assigns `confirmed`, `pending`, or `rejected`
- Keeps parse provenance such as `text`, `ocr`, or `text+ocr`
- Provides simple filters for strict versus expanded reporting

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_candidates.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/candidates.py tests/test_candidates.py
git commit -m "feat: add candidate confidence and review states"
```

### Task 11: Merge Related Posts into Trade Ideas

**Files:**
- Create: `src/telegram_kol_research/trade_merge.py`
- Create: `tests/test_trade_merge.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.trade_merge import merge_candidate_batch


def test_merge_candidate_batch_uses_reply_chain_first():
    candidates = [
        {"message_id": 100, "symbol": "BTC", "side": "long", "event_type": "entry_signal"},
        {"message_id": 101, "reply_to_message_id": 100, "symbol": "BTC", "side": "long", "event_type": "stop_loss_update"},
    ]
    trades = merge_candidate_batch(candidates)
    assert len(trades) == 1
    assert trades[0]["events"][1]["event_type"] == "stop_loss_update"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_trade_merge.py -v`
Expected: FAIL because the trade merge logic does not exist.

**Step 3: Write minimal implementation**

Implement a merge engine that:

- Prioritizes reply-chain linkage
- Falls back to same source, symbol, side, and time window
- Marks ambiguous overlap as reviewable instead of forcing a merge
- Produces normalized trade ideas plus update events

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_trade_merge.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trade_merge.py tests/test_trade_merge.py
git commit -m "feat: merge related messages into trade ideas"
```

### Task 12: Compute Strict and Expanded Performance Metrics

**Files:**
- Create: `src/telegram_kol_research/analytics.py`
- Create: `tests/test_analytics.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.analytics import compute_summary_metrics


def test_compute_summary_metrics_returns_win_rate_and_profit_factor():
    trades = [
        {"status": "win", "pnl": 2.0},
        {"status": "loss", "pnl": -1.0},
    ]
    summary = compute_summary_metrics(trades)
    assert summary.win_rate == 0.5
    assert summary.profit_factor == 2.0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_analytics.py -v`
Expected: FAIL because the analytics module does not exist.

**Step 3: Write minimal implementation**

Add analytics functions for:

- Closed trade counts
- Win rate
- Average win and loss
- Profit factor
- Risk/reward
- Maximum loss streak
- Data quality score

Support strict and expanded filters.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_analytics.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/analytics.py tests/test_analytics.py
git commit -m "feat: add per-source performance analytics"
```

### Task 13: Generate Leaderboard and Drill-Down Reports

**Files:**
- Create: `src/telegram_kol_research/reporting.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_reporting.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.reporting import render_leaderboard_rows


def test_render_leaderboard_rows_orders_by_quality_adjusted_rank():
    rows = render_leaderboard_rows(
        [
            {"source": "Alice", "win_rate": 0.6, "quality_score": 0.9},
            {"source": "Bob", "win_rate": 0.7, "quality_score": 0.4},
        ]
    )
    assert rows[0]["source"] == "Alice"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_reporting.py -v`
Expected: FAIL because reporting logic does not exist.

**Step 3: Write minimal implementation**

Implement reporting that:

- Exports leaderboard rows for strict and expanded modes
- Includes sample size and quality score
- Supports per-source drill-down exports
- Wires the `report` CLI command to a local output path

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_reporting.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/reporting.py src/telegram_kol_research/cli.py tests/test_reporting.py
git commit -m "feat: add leaderboard and drill-down reporting"
```

### Task 14: Add a Manual Review Workflow for Ambiguous Signals

**Files:**
- Create: `src/telegram_kol_research/review_queue.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_review_queue.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.review_queue import list_pending_candidates


def test_list_pending_candidates_returns_only_pending_records():
    candidates = [
        {"id": 1, "review_status": "pending"},
        {"id": 2, "review_status": "confirmed"},
    ]
    pending = list_pending_candidates(candidates)
    assert pending == [{"id": 1, "review_status": "pending"}]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_review_queue.py -v`
Expected: FAIL because the review queue module does not exist.

**Step 3: Write minimal implementation**

Implement a lightweight review workflow that:

- Lists pending candidates
- Shows source message references
- Allows marking a candidate `confirmed` or `rejected`
- Keeps review notes for later audit

Prefer a CLI review flow in v1 instead of a web UI.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_review_queue.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/review_queue.py src/telegram_kol_research/cli.py tests/test_review_queue.py
git commit -m "feat: add manual review workflow for ambiguous signals"
```

### Task 15: Add End-to-End Smoke Documentation and Operator Commands

**Files:**
- Modify: `README.md`
- Create: `docs/runbook.md`
- Create: `tests/test_readme_commands.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_readme_mentions_sync_and_report_commands():
    text = Path("README.md").read_text()
    assert "python -m telegram_kol_research.cli sync" in text
    assert "python -m telegram_kol_research.cli report" in text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_readme_commands.py -v`
Expected: FAIL because the README does not document the operator workflow yet.

**Step 3: Write minimal implementation**

Document:

- Local setup
- Telethon auth flow
- How to populate the target group config
- How to run backfill
- How to start incremental listening
- How to review candidates
- How to generate reports

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_readme_commands.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add README.md docs/runbook.md tests/test_readme_commands.py
git commit -m "docs: add operator runbook for telegram research workflow"
```
