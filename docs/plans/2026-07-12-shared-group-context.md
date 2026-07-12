# Shared Group Context Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add one persistent group context control shared by the strategy and message destinations, with a mobile bottom sheet and desktop popover.

**Architecture:** Reuse the server-rendered group row data and the existing fragment routes. Introduce one JavaScript group-context controller as the only writer of selected group state; existing sidebar rows and the new picker delegate to it.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, CSS, pytest.

---

### Task 1: Render the shared group context and picker

**Files:**
- Create: `src/telegram_kol_research/templates/_group_context.html`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing render tests**

Require `data-group-context`, `data-group-context-trigger`, `data-group-picker`, `data-group-picker-search`, and one `data-group-picker-option` per group. Assert the message-only `<select data-message-group-select>` is removed.

**Step 2: Verify failure**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py::test_shared_group_context_renders_all_groups -v`

Expected: FAIL because the shared context partial does not exist.

**Step 3: Implement the shared partial**

Render the current label, dialog/popover shell, search input, selected checkmark, last activity, holding count, and pending count. Include it once above the shared strategy/message workbench, not inside either detail fragment.

**Step 4: Remove the message-only selector**

Delete `data-message-group-select` markup and its dedicated binding so there is one canonical selection surface.

**Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py -q`

```bash
git add src/telegram_kol_research/templates tests/test_web_page_render.py
git commit -m "feat: add shared group context picker"
```

### Task 2: Implement the single group-context controller

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_group_messages_route.py`

**Step 1: Write failing behavior-contract tests**

Require one `setSelectedGroupContext(chatId)` path, local storage key `telegram-workbench:selected-group`, picker filtering, valid persisted-group restoration, and pending/error state hooks.

**Step 2: Verify failure**

Run: `.venv/bin/python -m pytest tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py -q`

Expected: FAIL on missing controller contracts.

**Step 3: Implement selection and persistence**

Make picker rows and legacy group rows delegate to `setSelectedGroupContext`. Validate persisted IDs against rendered picker options. Fetch the visible destination first, commit state only on success, synchronize selected markers, then persist.

**Step 4: Implement search and recent groups**

Filter by normalized title/alias text. Store at most three recent IDs and render/reorder without duplicating rows.

**Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py -q`

```bash
git add src/telegram_kol_research/static/app.js tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py
git commit -m "feat: share group state across strategy and messages"
```

### Task 3: Add responsive bottom-sheet and popover presentation

**Files:**
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write failing CSS contract tests**

Require sticky context bar, 48px targets, mobile fixed bottom sheet with safe-area padding, desktop anchored popover, internal result scrolling, selected state, and reduced-motion behavior.

**Step 2: Verify failure**

Run: `.venv/bin/python -m pytest tests/test_web_assets_smoke.py::test_group_context_responsive_contract -v`

Expected: FAIL on missing selectors.

**Step 3: Implement responsive styling**

Use one semantic picker. Apply bottom-sheet geometry at 760px and below; apply anchored popover geometry above 760px. Preserve visible close/search controls while results scroll.

**Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_web_assets_smoke.py -q`

```bash
git add src/telegram_kol_research/static/app.css tests/test_web_assets_smoke.py
git commit -m "feat: style responsive group context picker"
```

### Task 4: Regression, durable docs, and production verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/context/telegram-deepcoin-auto-trading-context.md`

**Step 1: Run focused regression**

Run: `.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py -q`

Expected: PASS except any independently reproduced baseline assertion documented before this feature.

**Step 2: Run syntax checks**

Run: `node --check src/telegram_kol_research/static/app.js && .venv/bin/python -m compileall -q src tests && git diff --check`

Expected: exit 0.

**Step 3: Update durable context**

Record that strategy/message share one persisted group context, while home/positions/more remain global.

**Step 4: Commit, push, and deploy**

```bash
git add docs/migration-handoff.md docs/context/telegram-deepcoin-auto-trading-context.md
git commit -m "docs: record shared group context"
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

**Step 5: Verify production**

Confirm server HEAD, `ActiveState=active`, `SubState=running`, HTTP 200, picker markup, mobile switching between strategy/message, and persisted group restoration.

