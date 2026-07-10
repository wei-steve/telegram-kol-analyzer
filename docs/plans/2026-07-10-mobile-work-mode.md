# Mobile Work Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the existing trading console fully usable in phone browsers through a responsive, single-page mobile work mode.

**Architecture:** Keep the server routes and data model unchanged. Add semantic mobile navigation in the existing dashboard template, then use client-side state to show one workbench region at a time below 760px. Reuse dashboard tabs for the exchange positions and settings panels, and wrap live-state-changing actions in a shared confirmation dialog.

**Tech Stack:** FastAPI, Jinja templates, vanilla JavaScript, CSS grid/media queries, pytest/TestClient.

---

### Task 1: Add mobile navigation markup and render coverage

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html:366-409`
- Modify: `tests/test_web_page_render.py:test_index_page_shows_group_list_and_messages`

**Step 1: Write the failing test**

Add assertions that `/` includes:

```python
assert 'data-mobile-work-nav' in response.text
assert 'data-mobile-work-view="overview"' in response.text
assert 'data-mobile-work-view="strategies"' in response.text
assert 'data-mobile-work-view="messages"' in response.text
assert 'data-mobile-work-view="positions"' in response.text
assert 'data-mobile-work-view="more"' in response.text
assert 'data-mobile-work-region="overview"' in response.text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_page_render.py::test_index_page_shows_group_list_and_messages -v`

Expected: FAIL because the mobile hooks do not exist.

**Step 3: Write minimal implementation**

In the main dashboard panel, add semantic `data-mobile-work-region` attributes to the group, strategy, and detail regions. Add a fixed, accessible `<nav data-mobile-work-nav>` after the dashboard panel with five buttons (`概览`, `策略`, `消息`, `持仓`, `更多`) and `data-mobile-work-view` values. Make `概览` active by default and use `aria-current="page"` for the active button.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_page_render.py::test_index_page_shows_group_list_and_messages -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_page_render.py src/telegram_kol_research/templates/index.html
git commit -m "feat: add mobile work navigation"
```

### Task 2: Implement responsive mobile work mode styling

**Files:**
- Modify: `src/telegram_kol_research/static/app.css:2597-2615`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Add a focused static-asset test that asserts the stylesheet contains:

```python
assert "[data-mobile-work-nav]" in response.text
assert "@media (max-width: 760px)" in response.text
assert ".mobile-work-nav" in response.text
assert "env(safe-area-inset-bottom" in response.text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_assets_smoke.py -k mobile_work_mode -v`

Expected: FAIL because the mobile navigation styles do not exist.

**Step 3: Write minimal implementation**

Within a `max-width: 760px` media query:

- Change `.trader-layout` to a scrollable viewport-aware layout with bottom padding for the nav.
- Display one `.trader-shell` region at a time based on a root mobile-view class.
- Make the overview show group and strategy context together, strategies show strategy context, and messages show the detail panel.
- Keep desktop rules untouched.
- Add an opaque fixed bottom nav with five equal touch targets, active-state feedback, and `env(safe-area-inset-bottom)` padding.
- Ensure settings/exchange dashboard tabs occupy the full mobile content area when activated.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_assets_smoke.py -k mobile_work_mode -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.css
git commit -m "feat: style mobile trading work mode"
```

### Task 3: Wire mobile navigation to existing dashboard panels

**Files:**
- Modify: `src/telegram_kol_research/static/app.js:1058-1075,2395-2418`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Add assertions for a `bindMobileWorkNavigation` function, the `data-mobile-work-view` selector, default `overview` handling, and reuse of `data-dashboard-tab="exchange-positions"`.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_assets_smoke.py -k mobile_work_navigation -v`

Expected: FAIL because the binding function has not been added.

**Step 3: Write minimal implementation**

Implement `bindMobileWorkNavigation()` and call it during `DOMContentLoaded`:

- Maintain the active mobile view as a class on `[data-trader-dashboard]`.
- Update button `aria-current` and active classes.
- `positions` triggers the existing exchange-position dashboard tab and `more` opens the settings menu without creating a new data source.
- `overview`, `strategies`, and `messages` activate the main dashboard panel and select the appropriate responsive region.
- Keep overview as the first-load default; do not persist it across reloads.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_assets_smoke.py -k mobile_work_navigation -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.js
git commit -m "feat: wire mobile work navigation"
```

### Task 4: Confirm live-state-changing mobile actions

**Files:**
- Modify: `src/telegram_kol_research/templates/execution.html:79-131`
- Modify: `src/telegram_kol_research/static/app.js:2183-2394`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Add a static test for the confirmation binding and dialog hooks:

```python
assert "requestLiveActionConfirmation" in response.text
assert "data-live-action-confirm" in response.text
assert "data-manual-close-lifecycle" in response.text
assert "data-bind-live-position" in response.text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_assets_smoke.py -k live_action_confirmation -v`

Expected: FAIL because no shared confirmation dialog exists.

**Step 3: Write minimal implementation**

- Add context data attributes to manual-close and bind buttons: symbol, side, group label, and action label when the template has the values.
- Add one accessible native `<dialog data-live-action-confirm>` to `execution.html`, with action context, cancel, and confirm controls.
- Implement `requestLiveActionConfirmation(button)` in `app.js`, using the dialog when supported and `window.confirm` as a fallback.
- Await confirmation before the existing fetches in `bindManualCloseButtons` and `bindLivePositionAttributionButtons`; leave sync/read-only refresh behavior unconfirmed.
- Style the dialog and controls for a small viewport, including a 44px minimum action target.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_assets_smoke.py -k live_action_confirmation -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/templates/execution.html src/telegram_kol_research/static/app.js src/telegram_kol_research/static/app.css
git commit -m "feat: confirm live actions on mobile"
```

### Task 5: Verify the integrated responsive dashboard

**Files:**
- Verify: `tests/test_web_page_render.py`
- Verify: `tests/test_web_assets_smoke.py`
- Verify: all `tests/`

**Step 1: Run focused web checks**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -v`

Expected: PASS.

**Step 2: Run all local checks**

Run: `pytest -q`

Expected: PASS.

**Step 3: Inspect working tree**

Run: `git status --short && git log --oneline -5`

Expected: only the intended commits and no uncommitted changes.

**Step 4: Deploy validation**

After reviewed commits are pushed, run the project’s documented server update helper and verify the real Telegram session and Deepcoin IP-allowlisted integration on the server:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```
