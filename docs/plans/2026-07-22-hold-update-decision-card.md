# Hold Update Decision Card Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Classify non-executable hold updates as informational decision cards rather than manual-review alerts.

**Architecture:** Add one actionability branch in `_build_message_decision_card` before the generic lifecycle-event fallback. It applies only to `position_update` + `hold_update`; all stored recognition and execution data remains unchanged.

**Tech Stack:** Python, FastAPI view model, pytest.

---

### Task 1: Lock the informational hold-update behavior with a test

**Files:**
- Modify: `tests/test_web_queries_messages.py`
- Modify: `src/telegram_kol_research/web_queries.py:717-740`

**Step 1: Write the failing test**

Create a `RecognitionDecision` with `position_update`, `management_action="hold_update"`, BTC short context, and no price changes. Assert its card state is `record_only`, label is `仅记录`, and recommended action is `无需操作`.

**Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/test_web_queries_messages.py -k hold_update -q`

Expected: FAIL because the generic lifecycle-event fallback currently returns `manual_review`.

**Step 3: Implement the minimal branch**

Insert an `elif` after the missing-stop branch that maps only `position_update` + `hold_update` to the informational card state.

**Step 4: Run focused and regression tests**

Run: `uv run pytest tests/test_web_queries_messages.py tests/test_web_group_messages_route.py -q`

Expected: PASS; the existing missing-stop test remains manual review.

**Step 5: Commit and deploy**

Commit the query mapping, test, and docs; push `codex/deepcoin-auto-trading-v1`; update the server and verify the service health.
