# Entry Protection Ledger Gap Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record and repair verified entry-protection TPSL ledger rows for the BTC market-entry sibling shape and ETH trigger-entry fill shape.

**Architecture:** Reuse the existing strict ledger and repair model. Add response-anchored sibling matching to online market-entry recording, and add a trigger-entry repair planner that matches verified entry legs to unique pending TPSL rows by size/time/requested TP/SL.

**Tech Stack:** Python, SQLAlchemy, Typer CLI, pytest.

---

### Task 1: Market Entry Response-Anchored Sibling

**Files:**
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`

**Steps:**
1. Write a failing test where pending TPSL rows omit `posId`, one returned order id anchors TP, and a unique sibling anchors SL.
2. Run the single test and verify it fails with no ledger rows.
3. Add strict response-anchor and sibling matching with event-time limits.
4. Run the market-entry ledger tests.

### Task 2: Trigger Entry Protection Repair

**Files:**
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/cli.py`

**Steps:**
1. Write failing tests for a trigger-entry repair plan and an ambiguous duplicate refusal.
2. Run the single tests and verify they fail because no actions are planned.
3. Add an opt-in `include_trigger_entries` planner flag and CLI option.
4. Run repair tests and CLI smoke if needed.

### Task 3: Production Repair and Verification

**Files:**
- Production database: `/opt/telegram-kol-analyzer/data/research.db`

**Steps:**
1. Run local focused tests and adjacent tests.
2. Commit and push the code.
3. Deploy through `scripts/server_git_update.sh`.
4. Dry-run BTC binding `154`, apply with fingerprint if it still matches.
5. Dry-run ETH binding `152` with trigger repair enabled, apply with fingerprint if count/fingerprint match.
6. Verify the four target TPSL orders now have verified ledger rows and the web page no longer renders them as unverified.
