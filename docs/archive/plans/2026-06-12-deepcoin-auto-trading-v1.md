# Deepcoin Auto-Trading V1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add the first conservative Telegram-to-trading decision layer without placing Deepcoin orders.

**Architecture:** Extend the current parser/config modules and add a small pure decision module. Keep ingestion and alert pipelines intact; future execution layers can consume the new decision output after explicit KOL opt-in.

**Tech Stack:** Python, pytest, SQLAlchemy models already present in the project, YAML group config.

---

### Task 1: Chinese Text Signal Parsing

**Files:**
- Modify: `src/telegram_kol_research/parsing/text_parser.py`
- Test: `tests/parsing/test_text_parser.py`

**Step 1: Write failing parser tests**

Add tests for:
- `比特币现货，62800-60000做多，均价61400，64200-65400-66600止盈，止损59500`
- `Btc 方向：空 建仓：63600-64700 止损：65100 止盈：62900-62200-61500`
- `现价开一层空单`
- `剩余仓位全部止盈出局`

**Step 2: Run test to verify failure**

Run: `PYTHONPATH=src pytest tests/parsing/test_text_parser.py -v`

Expected: new Chinese parser tests fail.

**Step 3: Implement minimal parser support**

Add symbol aliases for Chinese BTC/ETH names, Chinese side detection, Chinese entry-range labels, stop-loss labels, take-profit labels, and close/update event detection.

**Step 4: Run test to verify pass**

Run: `PYTHONPATH=src pytest tests/parsing/test_text_parser.py -v`

Expected: all parser tests pass.

### Task 2: KOL Trading Mode Config

**Files:**
- Modify: `src/telegram_kol_research/group_config.py`
- Modify: `config/groups.example.yaml`
- Test: `tests/test_group_config.py`

**Step 1: Write failing config tests**

Add tests proving group and sender trading modes default to `notify_only`, and explicit `auto_trade` is loaded for a sender.

**Step 2: Run test to verify failure**

Run: `PYTHONPATH=src pytest tests/test_group_config.py -v`

Expected: tests fail because fields do not exist.

**Step 3: Implement minimal config fields**

Add `trading_mode`, `max_loss_usdt`, and optional symbol whitelist fields to group/sender config dataclasses and loader.

**Step 4: Run test to verify pass**

Run: `PYTHONPATH=src pytest tests/test_group_config.py -v`

Expected: all group config tests pass.

### Task 3: Auto-Trade Decision Module

**Files:**
- Create: `src/telegram_kol_research/trading_decision.py`
- Test: `tests/test_trading_decision.py`

**Step 1: Write failing decision tests**

Add tests for:
- Notify-only mode returns `notify_only`.
- Missing stop loss returns `manual_review`.
- Non-BTC/ETH symbol returns `manual_review`.
- Image/vision provenance returns `manual_review`.
- Auto-enabled BTC/ETH signal with stop loss returns `eligible_for_auto_trade`.
- Existing same-KOL same-symbol same-side active position returns `manual_review`.

**Step 2: Run test to verify failure**

Run: `PYTHONPATH=src pytest tests/test_trading_decision.py -v`

Expected: import/module failure.

**Step 3: Implement minimal decision module**

Use dataclasses for input, active position summary, and decision output. Return stable reason codes suitable for alerts and UI.

**Step 4: Run test to verify pass**

Run: `PYTHONPATH=src pytest tests/test_trading_decision.py -v`

Expected: all decision tests pass.

### Task 4: Focused Regression

**Files:**
- No new files unless a regression appears.

**Step 1: Run focused test set**

Run: `PYTHONPATH=src pytest tests/parsing/test_text_parser.py tests/test_group_config.py tests/test_trading_decision.py tests/test_candidates.py tests/test_trade_merge.py -v`

Expected: all selected tests pass.

**Step 2: Fix any regression with failing tests first**

If a regression appears, add or adjust a focused failing test before changing production code.

### Task 5: Restart Recovery Decision Core

**Files:**
- Create: `src/telegram_kol_research/recovery_scan.py`
- Test: `tests/test_recovery_scan.py`

**Step 1: Write failing recovery tests**

Cover:
- Default recovery window is previous 48 hours in UTC-naive storage time.
- Notify-only KOLs are skipped.
- Already-touched entry ranges require manual review.
- Current price inside the entry range requires manual review.
- Existing same-strategy order requires manual review.
- Untouched entry range can become a recovery limit-order candidate.

**Step 2: Run test to verify failure**

Run: `PYTHONPATH=src pytest tests/test_recovery_scan.py -v`

Expected: import/module failure.

**Step 3: Implement minimal recovery decision module**

Add dataclasses for `RecoverySignal`, `PriceCandle`, `OpenOrder`, and `RecoveryDecision`. Reuse the existing trading-decision gates before applying recovery-specific price-touch checks.

**Step 4: Run test to verify pass**

Run: `PYTHONPATH=src pytest tests/test_recovery_scan.py -v`

Expected: all recovery tests pass.
