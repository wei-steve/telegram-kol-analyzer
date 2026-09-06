# Nondisruptive Focus Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep focus-recovery data checks current without replacing or dimming the positions page until the user chooses to apply a detected update.

**Architecture:** Fetch the latest positions panel into a detached fragment during focus recovery, compare it with the visible panel, and retain changed content as a pending snapshot. Render a lightweight update control outside the visible panel and commit the pending fragment only when the user activates that control.

**Tech Stack:** Vanilla JavaScript, FastAPI/Jinja HTML fragments, CSS, pytest.

---

### Task 1: Specify silent focus recovery

**Files:**
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Add a focused test that extracts `scheduleRecoveryRefresh` and asserts that it:

```python
assert "await refreshMonitorStatus();" in block
assert "await refreshFromDatabaseChanges();" in block
assert "await checkPositionsPanelForChanges();" in block
assert "ensureWorkbenchViewLoaded(activeView, { force: true })" not in block
```

Also assert that the change checker stores a pending fragment and does not write
to `container.innerHTML` or set `aria-busy`.

**Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py::test_focus_recovery_checks_positions_without_replacing_visible_panel -q
```

Expected: FAIL because `checkPositionsPanelForChanges` does not exist and focus
recovery still forces the active panel to reload.

### Task 2: Implement the pending positions snapshot

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Add the minimal implementation**

Add:

```javascript
let pendingPositionsFragment = null;
```

Extract the existing positions-fragment commit and event rebinding into
`commitPositionsPanel(fragment)`. Add `checkPositionsPanelForChanges()` to fetch
and compare a detached fragment without setting `aria-busy`. When changed, store
the fragment and render a notice with an apply button. When the button is
activated, commit the stored fragment and clear the notice.

Change `scheduleRecoveryRefresh()` so the positions view calls the new checker
instead of forcing `ensureWorkbenchViewLoaded`.

Add compact styling for the pending-update notice without opacity, overlay, or a
progress cursor.

**Step 2: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py::test_focus_recovery_checks_positions_without_replacing_visible_panel -q
```

Expected: PASS.

**Step 3: Run related regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py -q
```

Expected: PASS.

### Task 3: Review, publish, and verify production

**Files:**
- Verify: `src/telegram_kol_research/static/app.js`
- Verify: `src/telegram_kol_research/static/app.css`
- Verify: `tests/test_web_assets_smoke.py`

**Step 1: Review the diff**

Confirm the background path performs only GET requests, never sets a live-action
busy state, and never replaces the visible panel before user activation.

**Step 2: Commit and push**

Stage only the design, plan, JavaScript, CSS, and focused test files. Commit on
`codex/deepcoin-auto-trading-v1`, then push that branch.

**Step 3: Deploy**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the reviewed commit, reinstalls the editable package,
restarts `telegram-kol.service`, and reports an active service.

**Step 4: Verify production**

Open the production positions page, switch to another browser tab and return.
Confirm there is no progress cursor, opacity change, scroll jump, or automatic
panel replacement. If exchange data differs, activate the update notice and
confirm the latest panel appears with the selected tab and view restored.
