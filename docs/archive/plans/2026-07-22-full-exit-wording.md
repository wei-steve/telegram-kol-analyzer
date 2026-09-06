# Full-Exit Wording Recognition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recognize `余仓全出` and equivalent `全出` wording as a complete exit so an already verified attributed position enters the existing exact-close flow.

**Architecture:** Extend the two existing exit-wording helpers in `message_recognition.py`; do not add a new execution path. The lifecycle resolver and management executor continue to enforce chat, lifecycle, binding, and exact-position ownership checks.

**Tech Stack:** Python 3, SQLAlchemy, pytest.

---

### Task 1: Lock down full-exit recognition

**Files:**
- Modify: `tests/test_message_recognition.py`
- Modify: `src/telegram_kol_research/message_recognition.py`

**Step 1: Write the failing test**

Import `_parse_explicit_exit_signal` and assert that `BTC 多单余仓全出` returns `("BTC", "long")`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_message_recognition.py -k 'remaining_position_all_exit' -v`

Expected: FAIL because `全出` is not currently an exit term.

**Step 3: Write minimal implementation**

Add `全出` and `全部出` to the Chinese full-exit terms in `_has_full_exit_instruction` and `_parse_explicit_exit_signal`.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_message_recognition.py -k 'remaining_position_all_exit' -v`

Expected: PASS.

### Task 2: Prevent downgrade of an AI full-exit decision

**Files:**
- Modify: `tests/test_message_recognition.py`
- Modify: `src/telegram_kol_research/message_recognition.py`

**Step 1: Write the failing test**

Create an entered BTC lifecycle, submit the text `BTC 多单余仓全出`, and apply an `exit_position` decision. Assert that the persisted candidate is a `close_signal` with management action `full_exit`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_message_recognition.py -k 'remaining_position_all_exit_keeps_full_exit' -v`

Expected: FAIL because the decision is downgraded to `position_update`.

**Step 3: Write minimal implementation**

Reuse the Task 1 expanded full-exit helper; make no lifecycle or execution changes.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_message_recognition.py -k 'remaining_position_all_exit' -v`

Expected: PASS.

### Task 3: Verify and deliver

**Files:**
- Modify: `tests/test_message_recognition.py`
- Modify: `src/telegram_kol_research/message_recognition.py`

**Step 1: Run focused recognition tests**

Run: `pytest tests/test_message_recognition.py -v`

Expected: PASS.

**Step 2: Run management-close regression tests**

Run: `pytest tests/test_auto_trade_execution.py -k 'close_signal' -v`

Expected: PASS.

**Step 3: Review and commit**

Run `git diff --check`, inspect the scoped diff, then commit only the two source/test files and the two plan documents with message `fix: recognize remaining-position full exits`.

**Step 4: Push and deploy**

Push `codex/deepcoin-auto-trading-v1`, then run `powershell -ExecutionPolicy Bypass -File .\\scripts\\server_git_update.ps1`. Validate the service restart and use a read-only production query/snapshot to verify the new code is active.
