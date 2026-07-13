# Lazy Workbench Loading Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the authenticated workbench shell render quickly by deferring Deepcoin, position, strategy, and message content until the relevant destination needs it.

**Architecture:** Keep the existing FastAPI/Jinja/vanilla-JavaScript app. Make `GET /` a lightweight shell, add read-only home and position partials, reuse the current group detail/strategy partials, and guard all client-side lazy loads against duplicates and stale responses.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, Jinja2, vanilla JavaScript, pytest/TestClient.

---

### Task 1: Lock the lightweight root contract

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write failing tests**

Add route tests whose Deepcoin client factory raises if constructed during `GET /`. Assert that the root response still contains workbench navigation and loading containers, but not rendered message cards or exchange position cards. Add asset assertions that startup does not call `refreshFromDatabaseChanges({ force: true })` and persisted group restoration does not call `.click()`.

**Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_page_render.py -k "lazy or lightweight" -v
.venv/bin/python -m pytest tests/test_web_assets_smoke.py -k "lazy or startup" -v
```

Expected: FAIL because root currently constructs the Deepcoin client, renders all panels, and forces startup refreshes.

### Task 2: Add deferred server partials

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Create: `src/telegram_kol_research/templates/_workbench_loading.html`
- Test: `tests/test_web_page_render.py`

**Step 1: Implement the minimal root split**

Move the existing live home summary/event construction into a read-only `/home-dashboard` partial and the existing exchange panel construction into a read-only `/positions-panel` partial. Keep only fast group/freshness/config data in `GET /`. Render explicit loading containers for deferred destinations.

**Step 2: Run route tests to verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_page_render.py -k "lazy or lightweight or exchange" -v
```

Expected: PASS.

### Task 3: Load destinations on demand

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Add guarded loaders**

Add one loaded/in-flight registry for `home`, `positions`, `strategies`, and `messages`. Load home asynchronously after the shell is interactive. Invoke the positions partial only on first positions navigation. Invoke the existing visible group destination loader only on first strategy/message navigation. Rebind controls after replacing partial HTML.

Restore persisted group state with `syncSelectedGroupState` instead of programmatically clicking a group. Replace the startup forced refresh with a baseline-only freshness read, and coalesce focus/visibility recovery calls.

**Step 2: Run asset tests to verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py -k "lazy or startup or group" -v
node --check src/telegram_kol_research/static/app.js
```

Expected: PASS and JavaScript syntax exit code 0.

### Task 4: Regression verification and review

**Files:**
- Verify all files changed above.

**Step 1: Run focused web regression tests**

```bash
.venv/bin/python -m pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py tests/test_web_group_messages_route.py -q
node --check src/telegram_kol_research/static/app.js
git diff --check
```

**Step 2: Request independent code review**

Review the diff for missing lazy-load triggers, duplicate requests, stale DOM writes, live-action regressions, and misleading loading/error states. Resolve all Critical and Important findings.

### Task 5: Publish and production verification

**Files:**
- Commit all implementation and test changes.

**Step 1: Commit and push**

```bash
git add src tests docs/plans
git commit -m "perf: lazy load workbench destinations"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 2: Deploy using the existing helper**

```bash
./scripts/server_git_update.sh
```

**Step 3: Verify production**

Confirm server HEAD matches the pushed commit, `telegram-kol.service` is active/running, `/` and deferred partials return HTTP 200, root HTML is materially smaller, and root time-to-first-byte no longer includes the Deepcoin request duration.
