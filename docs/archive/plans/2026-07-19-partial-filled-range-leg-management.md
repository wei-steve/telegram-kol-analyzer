# Partial Filled Range-Leg Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow management actions to execute for verified live legs of a partially filled range-entry strategy while sending 3-hour review notices for stale pending entry legs.

**Architecture:** Keep live-position management scoped to verified active entry legs and leave unresolved pending entry legs untouched. Reuse the existing pending-entry expiry review notifier, extending its query to entered lifecycles with unresolved pending entry legs and changing the review interval to 3 hours.

**Tech Stack:** Python, SQLAlchemy models, pytest, existing Deepcoin read-only reconciliation and system-operator notification flow.

---

### Task 1: Planner Allows Verified Live Subset

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Test: `tests/test_strategy_management_planner.py`

**Step 1: Write failing tests**

Add tests that create one binding with two entry legs:
- leg 1 `active`, `verified`, `pos_id="pos-live"`
- leg 2 `pending`, `unassigned`, no `pos_id`

Assert `partial_then_break_even` produces a ready/protection-ready batch with exactly one management leg for `pos-live`. Add a second test where both legs are pending and the planner remains blocked.

**Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/test_strategy_management_planner.py -k 'partial_range or pending_only' -v
```

Expected: the verified-subset test fails with `target_position_ownership_not_verified`.

**Step 3: Implement**

Replace the all-entry-leg preflight with a target-leg classifier:
- verified manageable legs: `status` in active/open/filled/partial_closed, `attribution_status="verified"`, has `pos_id`
- unresolved pending legs: non-terminal pending/open/submitted with no verified `pos_id`
- blocking legs: conflict, evidence unavailable, terminal verified target, duplicate `pos_id`, binding mismatch

Use manageable legs for economics, protection lookup, sizing, and management-leg creation.

**Step 4: Verify green**

Run the focused planner tests.

### Task 2: Executor Accepts Managed Subset

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write failing test**

Create a management batch with one management leg for the verified entry leg while the same binding also has a pending unassigned entry leg. Assert `_require_exact_entry_legs` accepts the batch and close execution reaches the mocked Deepcoin close call.

**Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/test_strategy_management_executor.py -k 'partial_subset' -v
```

Expected: fails with `batch_entry_set_not_exact`.

**Step 3: Implement**

Change `_require_exact_entry_legs` to require exactness against the verified manageable subset, while allowing unresolved pending entry legs to remain outside the batch. Continue blocking conflicts, evidence-unavailable legs, terminal live legs, and duplicate ownership.

**Step 4: Verify green**

Run the focused executor tests.

### Task 3: Three-Hour Pending-Leg Review

**Files:**
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py`
- Test: `tests/test_lifecycle_monitor.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write failing tests**

Add a lifecycle with status `entered`, signal time older than 3 hours, a binding with one verified live leg and one pending entry leg. Assert the monitor enqueues one expiry review payload. Also assert the default pending-entry review interval is 3 hours.

**Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/test_lifecycle_monitor.py -k 'pending_entry or range_leg' -v
```

Expected: no review is emitted for the entered lifecycle before implementation.

**Step 3: Implement**

Set the pending-entry recovery/review window to 3 hours. Extend `_request_pending_expiry_reviews` to select entered lifecycles whose binding has unresolved nonterminal pending entry legs. Build the same operator payload, adding enough context to show the pending leg/order identity when available.

For entered lifecycles, keep callback handling fail-closed: continue waiting may
update review state, but expire/cancel choices must preserve the entered
lifecycle and must not reuse the whole-binding pending-entry cancel path.

**Step 4: Verify green**

Run the focused lifecycle tests.

### Task 4: Regression Sweep

Run:

```bash
python3 -m pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_lifecycle_monitor.py tests/test_system_operator_bot.py -v
```

Expected: all pass. Full local suite collection may still require optional OCR
dependencies such as `pytesseract`; do not treat that dependency error as
evidence against the trading-management change.

Record that production still requires GitHub push, server pull/reinstall/restart, and read-only verification before the live system is fixed.
