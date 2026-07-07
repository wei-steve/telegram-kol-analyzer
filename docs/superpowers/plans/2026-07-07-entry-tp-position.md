# Entry TP Position Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop multiplying entry positions by take-profit targets while preserving strategy-level full and partial close behavior.

**Architecture:** The order draft remains the source of entry-leg count. Live submission will submit exactly the selected draft `order_legs` and use take-profit data only as protection metadata on the submitted order or position.

**Tech Stack:** Python, SQLAlchemy, pytest, FastAPI/Jinja project conventions.

## Global Constraints

- Make code changes locally and verify with local tests that do not require production secrets.
- Production verification and deployment happen by pushing to GitHub and running `scripts/server_git_update.ps1`.
- Do not change Deepcoin authentication or server deployment scripts.
- Use test-first changes for behavior corrections.

---

### Task 1: Pin Entry Leg Count

**Files:**
- Modify: `tests/test_recovery_live_submit.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`

**Interfaces:**
- Consumes: `draft["order_legs"]`, `draft["take_profit_legs"]`
- Produces: `_submission_order_legs(draft: dict[str, Any], order_legs: list[Any]) -> list[dict[str, Any]]`

- [ ] **Step 1: Write failing tests**

Update expectations so two draft entry legs produce two submitted trigger orders, and hybrid auto-trade produces one market order plus one trigger order.

- [ ] **Step 2: Run focused tests to verify failure**

Run: `python -m pytest tests/test_recovery_live_submit.py::test_submit_recovery_order_live_places_orders_and_persists_binding tests/test_auto_trade_execution.py::test_auto_process_range_entry_uses_half_market_half_midpoint_limit_when_near_edge -q`

Expected: failures showing old code submits four or three trigger orders.

- [ ] **Step 3: Implement minimal code**

Change `_submission_order_legs()` so it returns the original draft entry legs without splitting limit legs by take-profit targets. Remove now-unused helper functions if no tests or code paths use them.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_recovery_live_submit.py::test_submit_recovery_order_live_places_orders_and_persists_binding tests/test_auto_trade_execution.py::test_auto_process_range_entry_uses_half_market_half_midpoint_limit_when_near_edge -q`

Expected: both tests pass.

### Task 2: Preserve Management Close Coverage

**Files:**
- Modify: `tests/test_deepcoin_execution_actions.py`

**Interfaces:**
- Consumes: `close_position_market(...)`
- Produces: verified all-bound-position full close and proportional partial close behavior.

- [ ] **Step 1: Run existing close tests**

Run: `python -m pytest tests/test_deepcoin_execution_actions.py::test_process_trade_signal_live_closes_all_bound_position_ids tests/test_deepcoin_execution_actions.py::test_process_trade_signal_live_closes_only_requested_bound_position_id -q`

Expected: both tests pass without code changes.

### Task 3: Regression Suite

**Files:**
- No production files expected beyond Task 1.

**Interfaces:**
- Consumes: updated order submission behavior.
- Produces: local verification evidence.

- [ ] **Step 1: Run related suites**

Run: `python -m pytest tests/test_recovery_live_submit.py tests/test_auto_trade_execution.py tests/test_deepcoin_execution_actions.py -q`

Expected: all related tests pass.

- [ ] **Step 2: Review diff**

Run: `git diff -- tests/test_recovery_live_submit.py tests/test_auto_trade_execution.py src/telegram_kol_research/recovery_live_submit.py docs/superpowers/specs/2026-07-07-entry-tp-position-design.md docs/superpowers/plans/2026-07-07-entry-tp-position.md`

Expected: only scoped behavior, test, and documentation changes.
