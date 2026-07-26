# Exchange Position Tab Persistence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve the selected exchange-position subtab across automatic positions-panel reloads.

**Architecture:** Store the selected subtab in a dedicated local-storage key and restore it whenever `bindExchangePositionTabs` binds a newly fetched fragment. Route both clicks and restoration through one validated DOM setter, falling back to `positions` for unavailable storage, unsupported values, or incomplete fragments.

**Tech Stack:** Vanilla JavaScript, FastAPI static asset serving, pytest.

---

### Task 1: Specify persisted subtab behavior

**Files:**
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Add a focused test that extracts the exchange-position tab functions from
`/static/app.js` and asserts the presence of:

```python
assert "const EXCHANGE_POSITION_TAB_KEY" in js
assert "function exchangePositionTab()" in js
assert "function saveExchangePositionTab(tab)" in js
assert "function setExchangePositionTab(root, tab)" in js
assert "function restoreExchangePositionTab(root)" in js
assert "saveExchangePositionTab(target);" in bind_block
assert "restoreExchangePositionTab(root);" in bind_block
```

Also assert that all four supported tab values are validated and the getter
falls back to `positions`.

**Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py::test_app_js_restores_exchange_position_tab_after_partial_reload -q
```

Expected: FAIL because the tab storage key and restore functions do not exist.

### Task 2: Implement validated subtab persistence

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Add the minimal implementation**

Define a local-storage key and supported-value list:

```javascript
const EXCHANGE_POSITION_TAB_KEY = 'telegram-workbench:exchange-position-tab';
const EXCHANGE_POSITION_TABS = [
  'positions',
  'open-orders',
  'order-history',
  'position-history',
];
```

Extract the existing DOM selection code into:

```javascript
function setExchangePositionTab(root, tab) {
  const availableTabs = Array.from(root.querySelectorAll('[data-exchange-position-tab]'));
  const selectedTab = EXCHANGE_POSITION_TABS.includes(tab)
    && availableTabs.some((item) => item.dataset.exchangePositionTab === tab)
    ? tab
    : 'positions';
  // Update tab state and matching panel state.
}
```

Add guarded load/save helpers. On click, call the setter and save the target.
At the end of binding, restore the stored tab.

**Step 2: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py::test_app_js_restores_exchange_position_tab_after_partial_reload -q
```

Expected: PASS.

**Step 3: Run the complete asset smoke suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py -q
```

Expected: PASS.

**Step 4: Commit**

```bash
git add src/telegram_kol_research/static/app.js tests/test_web_assets_smoke.py docs/plans/2026-07-27-exchange-position-tab-persistence.md
git commit -m "fix: preserve exchange position tab"
```

### Task 3: Review, publish, and verify production

**Files:**
- Verify: `src/telegram_kol_research/static/app.js`
- Verify: `tests/test_web_assets_smoke.py`

**Step 1: Review the committed diff**

Confirm the change is limited to persisted UI state and performs no trading or
API writes.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: the remote branch advances to the reviewed commit.

**Step 3: Deploy through the project helper**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the branch, reinstalls the editable package, restarts
`telegram-kol.service`, and reports healthy service status.

**Step 4: Verify the production interaction**

Open the production positions page, select `历史委托`, trigger a normal
focus/visibility recovery refresh, and confirm the refreshed data remains on
`历史委托`. Repeat with `历史仓位`.
