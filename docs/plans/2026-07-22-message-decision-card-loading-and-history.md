# Message Decision Card Loading and History Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make a desktop group switch render the strategy summary and message decision cards without a serial wait, while keeping the decision card as the sole default-visible AI conclusion.

**Architecture:** The desktop group switch intentionally prioritizes the active workbench panel before loading the desktop companion panel; an existing regression test protects that behavior. Do not replace it with a `Promise.all` barrier. The group-panel bootstrap must load the restored selected group after it restores its visual selection. When a structured decision card exists, retain the legacy raw analysis for diagnostics but keep it closed and label it as historical/debug information so it cannot compete with the current decision.

**Tech Stack:** FastAPI/Jinja templates, vanilla JavaScript, pytest/TestClient.

---

### Task 1: Restore the selected group after group-panel bootstrap

**Files:**
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `src/telegram_kol_research/static/app.js`

**Step 1: Write the failing test**

Assert that `loadGroupsPanel` calls `loadSelectedGroupDestination('groups')` after `syncSelectedGroupState(selectedChatId)`.

**Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/test_web_assets_smoke.py -k group_switch -q`

Expected: FAIL because the code currently restores only the selected visual state.

**Step 3: Implement the minimal change**

After the group list is inserted and the selected group is synchronized, return the result of `loadSelectedGroupDestination('groups')`. Preserve the existing sequential active-destination behavior; it avoids blocking the selected workbench on both panels.

**Step 4: Run the focused test and verify it passes**

Run: `uv run pytest tests/test_web_assets_smoke.py -k group_switch -q`

Expected: PASS with no loading-contract regression.

### Task 2: Put legacy AI details behind the decision card

**Files:**
- Modify: `tests/test_web_group_messages_route.py`
- Modify: `src/telegram_kol_research/templates/_messages.html`

**Step 1: Write the failing route-rendering test**

For a message with `decision_card`, assert the response contains the decision card and the closed summary `历史 AI 细节（调试）`, rather than an open duplicate `AI识别结果` block.

**Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/test_web_group_messages_route.py -k decision_card -q`

Expected: FAIL because strategy-related legacy details are currently open by default.

**Step 3: Implement the minimal template change**

Conditionally add a history class, suppress the `open` attribute, and change only the legacy summary/toggle wording when `decision_card` exists. Preserve the legacy content for investigations.

**Step 4: Run the focused test and verify it passes**

Run: `uv run pytest tests/test_web_group_messages_route.py -k decision_card -q`

Expected: PASS.

### Task 3: Verify, review in Chrome, and deploy

**Files:**
- Verify: `tests/test_web_assets_smoke.py`
- Verify: `tests/test_web_group_messages_route.py`
- Verify: `tests/test_web_queries_messages.py`

**Step 1: Run the scoped suite**

Run: `uv run pytest tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py tests/test_web_queries_messages.py -q`

Expected: PASS.

**Step 2: Review the production page in Chrome**

Switch to the affected group and confirm the message card appears as the main content, the historical section is closed, and the selected group panels populate together.

**Step 3: Commit, push, and deploy after review**

Commit only the implementation and test files, push to `codex/deepcoin-auto-trading-v1`, then run the existing server-update workflow and verify `telegram-kol.service` is active.
