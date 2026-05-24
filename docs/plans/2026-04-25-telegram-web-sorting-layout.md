# Telegram Web Sorting and Layout Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve the Telegram web UI ordering and middle-panel layout.

**Architecture:** The backend continues to own chronological data ordering. The frontend refreshes server-rendered partials for the group list and message panel so live updates and manual refreshes stay consistent with initial page ordering.

**Tech Stack:** FastAPI, Jinja2 templates, vanilla JavaScript, CSS, pytest.

---

### Task 1: Group List Partial

**Files:**
- Create: `src/telegram_kol_research/templates/_groups.html`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_group_messages_route.py`

**Steps:**
- Write a failing route test for `GET /groups` that expects groups ordered by latest message.
- Implement `_groups.html` and a `/groups` endpoint that renders it with `load_group_rows()`.
- Include `data-groups-list` on the sidebar list so JavaScript can replace it.

### Task 2: Message Header Layout

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Steps:**
- Write failing tests for a `data-message-sticky-header` marker and CSS sticky rule.
- Add the marker to the message header and split panel scrolling so the header stays fixed while messages scroll.
- Keep the message list ordered oldest-to-newest and preserve existing load-more behavior.

### Task 3: Frontend Refresh Wiring

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

**Steps:**
- Write failing static asset assertions for `refreshGroupList()` and calls from refresh/live-update paths.
- Implement a group-list partial fetch that preserves active state and rebinds group buttons.
- Call it after manual refreshes and SSE events, including events for groups that are not currently selected.
