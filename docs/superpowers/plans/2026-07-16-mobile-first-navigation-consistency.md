# Mobile-First Navigation Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Web workbench phone-first, prevent home/settings overlap, and load the visible desktop message column from the strategy destination.

**Architecture:** Replace the two competing panel activators with one navigation coordinator in `app.js`. Keep phone strategy and message destinations separate, but add a non-blocking, request-guarded message companion load when the desktop strategy layout exposes the third column. Use the existing templates, design tokens, partial endpoints, and safety confirmations.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, CSS, pytest/TestClient, Chrome browser verification.

## Global Constraints

- Mobile browser usability is the first priority; desktop must remain functional.
- Do not create a separate `/mobile` frontend.
- Do not change trading business logic or trigger a Deepcoin mutation during verification.
- Preserve the existing GitHub push -> server pull/reinstall/restart deployment model.
- Ignore stale async responses when the selected Telegram group changes.
- Keep every interactive phone target at least 44px tall.

---

### Task 1: Navigation and responsive regression contracts

**Files:**
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: `/static/app.js`, `/static/app.css`, and the root Jinja render.
- Produces: regression contracts for `setWorkbenchView`, `openDashboardPanel`, `loadDesktopStrategyCompanion`, five phone destinations, and bottom-sheet CSS.

- [ ] **Step 1: Write failing tests**

Add focused tests asserting that the JavaScript exposes one workbench coordinator, settings activation clears workbench panels, desktop strategy loading calls a guarded companion loader, mobile navigation omits `management-batches`, and mobile CSS uses five columns plus a fixed settings sheet.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/pytest -q tests/test_web_assets_smoke.py tests/test_web_page_render.py`

Expected: failures for missing coordinator/companion markers and the existing six-column mobile navigation.

### Task 2: Single navigation coordinator

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`

**Interfaces:**
- Consumes: `[data-workbench-view]`, `[data-workbench-panel]`, `[data-dashboard-tab]`, and `[data-dashboard-panel]`.
- Produces: `setActiveDashboardPanel(tab)`, `setWorkbenchView(view)`, and `openDashboardPanel(tab)`.

- [ ] **Step 1: Implement the minimum coordinator**

Move workbench activation out of the nested callback so both navigation systems use the same functions. Opening a settings panel stores the prior workbench destination, clears every workbench panel, and activates exactly one dashboard panel. Returning restores the stored destination. Map `exchange-positions` to `positions`.

- [ ] **Step 2: Run focused tests and confirm GREEN for navigation**

Run: `.venv/bin/pytest -q tests/test_web_assets_smoke.py tests/test_web_page_render.py`

Expected: navigation assertions pass; any remaining failures are limited to responsive/companion work not yet implemented.

### Task 3: Desktop strategy message companion

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

**Interfaces:**
- Consumes: `fetchDetailPanel(chatId)`, `groupSwitchRequestId`, `bindDetailPanelControls()`, and `bindWorkflowFilters()`.
- Produces: `loadDesktopStrategyCompanion({ chatId, detailPanel, requestId }) -> Promise<void>`.

- [ ] **Step 1: Implement guarded companion loading**

At `min-width: 761px`, request the current group detail after the strategy panel is visible. Before writing, require the original request ID and selected group to remain current. Do not await the companion request before marking group selection successful.

- [ ] **Step 2: Run focused tests**

Run: `.venv/bin/pytest -q tests/test_web_assets_smoke.py tests/test_web_page_render.py`

Expected: all focused tests pass.

### Task 4: Phone-first navigation and settings presentation

**Files:**
- Modify: `src/telegram_kol_research/templates/_workbench_nav.html`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_page_render.py`

**Interfaces:**
- Consumes: existing workbench destinations and `.settings-dropdown` markup.
- Produces: five-item phone bottom navigation, desktop-only batch button, phone bottom-sheet settings menu, compact phone header, and sticky phone settings actions.

- [ ] **Step 1: Update navigation markup**

Render all six destinations in desktop navigation. Render only `home`, `positions`, `strategies`, `messages`, and `more` in mobile navigation. Keep the existing Batch entry in the More panel.

- [ ] **Step 2: Update responsive CSS**

Change the mobile bar to five equal columns. At 760px and below, render the settings menu as a fixed bottom sheet above the bottom navigation, hide redundant header links, preserve safe-area padding, and keep settings action rows reachable.

- [ ] **Step 3: Run focused tests**

Run: `.venv/bin/pytest -q tests/test_web_assets_smoke.py tests/test_web_page_render.py`

Expected: all focused tests pass.

### Task 5: Full verification and deployment

**Files:**
- Verify: `src/telegram_kol_research/static/app.js`
- Verify: `src/telegram_kol_research/static/app.css`
- Verify: `src/telegram_kol_research/templates/index.html`
- Verify: `src/telegram_kol_research/templates/_workbench_nav.html`

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: reviewed commit, pushed branch, restarted service, and production browser evidence.

- [ ] **Step 1: Run full local verification**

Run:

```bash
.venv/bin/pytest -q
node --check src/telegram_kol_research/static/app.js
git diff --check
```

Expected: zero failures and zero syntax/whitespace errors.

- [ ] **Step 2: Verify browser flows without mutations**

At 390x844 and desktop width, verify 首页 -> 设置菜单 -> 交易设置 -> 返回, all five phone destinations, 策略 -> desktop message companion, 消息, and group switching. Do not submit forms or invoke exchange actions.

- [ ] **Step 3: Review and commit**

Review the complete diff for unrelated changes, then commit only the design, plan, Web assets, templates, and tests.

- [ ] **Step 4: Reconcile and push the required branch**

Reconcile the local/remote branch history without discarding either side, then push `codex/deepcoin-auto-trading-v1`.

- [ ] **Step 5: Deploy and verify production**

Run the repository's approved server update helper, confirm the server commit and `telegram-kol.service` state, then repeat the read-only browser flow against production.

