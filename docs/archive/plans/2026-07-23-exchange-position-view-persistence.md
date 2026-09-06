# Exchange Position View Persistence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep the selected exchange-position view (real list or grouped by Telegram group) when the panel is asynchronously reloaded.

**Architecture:** Store the selected view mode in a narrowly scoped browser-storage key. When a newly fetched positions fragment is inserted, `bindExchangePositionTabs` restores that mode before the user interacts; the same binding persists later selections. Storage failures fall back to the existing `list` default.

**Tech Stack:** FastAPI static asset serving, vanilla JavaScript, pytest.

---

### Task 1: Specify the persisted view behavior

**Files:**
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

Assert that the delivered JavaScript defines a dedicated exchange-position view key, reads it before binding the panel, and persists the selected mode when a view button is clicked.

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_assets_smoke.py::test_app_js_restores_exchange_position_view_after_partial_reload -q`

Expected: FAIL because the app currently always renders `list` after a partial reload.

### Task 2: Restore and persist the selected view

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Implement the minimal code**

Add small helpers local to the exchange-position UI that read/write `telegram-workbench:exchange-position-view`. In `bindExchangePositionTabs`, apply the stored `grouped`/`list` mode to the newly rendered root and save changes made by the user. Invalid or unavailable storage defaults safely to `list`.

**Step 2: Run the focused test**

Run: `python -m pytest tests/test_web_assets_smoke.py::test_app_js_restores_exchange_position_view_after_partial_reload -q`

Expected: PASS.

### Task 3: Regression verification

**Files:**
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Run relevant tests**

Run: `python -m pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py -q`

Expected: PASS.

**Step 2: Commit only the intended files**

Run:

```bash
git add docs/plans/2026-07-23-exchange-position-view-persistence.md tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.js
git commit -m "fix: preserve exchange position view"
```
