# Composite Snapshot Method Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make composite management preflight use the real Deepcoin trigger-history interface and prevent future test-double drift.

**Architecture:** Keep `DeepcoinTradingClientProtocol.list_trigger_order_history()` as the single canonical interface. Change the executor snapshot call and its test double to that name, with one production-shaped regression test proving the snapshot reaches planning instead of failing on a missing method.

**Tech Stack:** Python, pytest, SQLAlchemy test database, Deepcoin client protocol

---

### Task 1: Capture the production interface mismatch

**Files:**
- Modify: `tests/test_strategy_management_executor.py`

1. Rename the composite fake's trigger-history method to the production singular spelling.
2. Add or adjust the focused test so the fake exposes no plural alias.
3. Run the exact focused test and verify RED with an `AttributeError`-driven `recovery_required` result.

### Task 2: Apply the minimal contract fix

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Test: `tests/test_strategy_management_executor.py`

1. Replace the plural trigger-history call with `list_trigger_order_history()`.
2. Re-run the RED test and verify GREEN.
3. Run the focused composite-management test slice and verify no regression.
4. Review the explicit diff and commit only the two code paths plus these plan files.
