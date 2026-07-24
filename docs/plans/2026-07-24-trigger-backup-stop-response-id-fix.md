# Trigger Backup Stop Response ID Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist a Deepcoin backup-stop order ID when `trigger-order` returns it as a string, object, or list item.

**Architecture:** Keep the exchange request and fail-closed reservation flow unchanged. Expand the pure response-ID extractor to normalize the observed response shapes before the durable row and execution event are marked active.

**Tech Stack:** Python 3.12, pytest, SQLAlchemy.

---

### Task 1: Normalize trigger-order response IDs

**Files:**
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write the failing test**

Cover `{"code":"0","data":"backup-1"}` and assert reconciliation persists `backup-1` as active rather than `unknown_exchange_outcome`.

**Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_execution_bindings.py -k string_trigger_response`

**Step 3: Implement the minimal normalizer**

Accept a nonempty string `data`, a response object with an ID field, and existing list rows; reject unsupported values without retrying the exchange request.

**Step 4: Run focused and integration tests**

Run: `uv run pytest -q tests/test_execution_bindings.py tests/test_trigger_backup_stop.py`

**Step 5: Deploy safely**

Commit, push `codex/deepcoin-auto-trading-v1`, run the server update helper, then use only `list_trigger_orders_pending` and the local ledger for confirmation.
