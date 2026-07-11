# Mobile-First Web Workbench Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the crowded three-column workbench with a mobile-first home dashboard, unified operational event feed, and focused positions, strategies, messages, and more destinations.

**Architecture:** Keep the Flask/Jinja server-rendered application and current action endpoints. Add one normalized, read-only home-feed query/view model, then reorganize shared template markup so CSS and small JavaScript state controllers provide mobile bottom navigation and an enhanced desktop rail without maintaining separate applications.

**Tech Stack:** Python 3, Flask, SQLAlchemy, Jinja2, vanilla JavaScript, CSS, pytest.

---

### Task 1: Normalize the home summary and operational event feed

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/web_app.py:156-270`
- Test: `tests/test_web_queries_dashboard.py`

**Step 1: Write failing tests for normalized events**

Create fixtures containing a recent raw message, strategy lifecycle row, and execution event. Assert that a new `load_home_event_rows()` returns newest-first rows with a common shape:

```python
assert rows[0].keys() >= {
    "id", "kind", "occurred_at", "source_label", "title",
    "summary", "symbol", "side", "status", "destination",
}
assert [row["occurred_at"] for row in rows] == sorted(
    [row["occurred_at"] for row in rows], reverse=True
)
```

Add tests for `kinds={"message"}` filtering, a stable limit, and missing optional symbol/side fields.

**Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_web_queries_dashboard.py -v`

Expected: FAIL because `load_home_event_rows` does not exist.

**Step 3: Implement the minimal normalized query**

In `web_queries.py`, add a small serializer per source and merge only the newest requested window in Python. Do not add a database table or copy authoritative state.

```python
def load_home_event_rows(session_factory, *, limit=50, kinds=None):
    selected = set(kinds or {"message", "strategy", "execution", "risk"})
    rows = []
    if "message" in selected:
        rows.extend(_load_home_message_events(session_factory, limit=limit))
    if "strategy" in selected:
        rows.extend(_load_home_strategy_events(session_factory, limit=limit))
    if "execution" in selected:
        rows.extend(_load_home_execution_events(session_factory, limit=limit))
    if "risk" in selected:
        rows.extend(_load_home_risk_events(session_factory, limit=limit))
    rows.sort(key=lambda row: row["occurred_at"], reverse=True)
    return rows[:limit]
```

Use destination dictionaries such as `{"view": "messages", "chat_id": ..., "message_id": ...}` so templates do not manufacture database relationships.

**Step 4: Add the dashboard summary view model**

Extend `_build_trader_dashboard_state()` in `web_app.py` with a `summary` object containing position count, unrealized PnL when supplied by the exchange snapshot, risk count, pending-confirmation count, and independent monitor/database/exchange states. Keep presentation labels out of the query layer.

**Step 5: Run focused tests**

Run: `pytest tests/test_web_queries_dashboard.py tests/test_web_app.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/web_queries.py src/telegram_kol_research/web_app.py tests/test_web_queries_dashboard.py
git commit -m "feat: add mobile dashboard event view model"
```

### Task 2: Render the new navigation shell and home destination

**Files:**
- Create: `src/telegram_kol_research/templates/_home_dashboard.html`
- Create: `src/telegram_kol_research/templates/_workbench_nav.html`
- Modify: `src/telegram_kol_research/templates/index.html:1-412`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing render assertions**

Assert that `/` contains:

```python
assert 'data-workbench-view="home"' in response.text
assert 'data-workbench-view="positions"' in response.text
assert 'data-home-risk-summary' in response.text
assert 'data-home-event-feed' in response.text
assert 'data-home-event-filter="risk"' in response.text
assert 'data-desktop-workbench-nav' in response.text
assert 'data-mobile-work-nav' in response.text
```

Also assert that dangerous action hooks such as `data-close-bound-position` do not appear inside `data-home-event-feed`.

**Step 2: Verify the test fails**

Run: `pytest tests/test_web_page_render.py -v`

Expected: FAIL on the new semantic hooks.

**Step 3: Build shared navigation markup**

Create `_workbench_nav.html` with the five confirmed destinations: `home`, `positions`, `strategies`, `messages`, and `more`. Render both a desktop rail and mobile bottom bar from the same Jinja destination list, using buttons with `aria-current` and visible text labels.

**Step 4: Build the home partial**

Create `_home_dashboard.html` with:

- Four summary metrics.
- A priority risk banner when present.
- Filter buttons for all, messages, strategies, executions, and risks.
- Compact linked event cards.
- Explicit empty, stale, and error placeholders.

Keep details and mutation controls outside this partial.

**Step 5: Replace the three-column root shell**

Refactor `index.html` so each primary destination is a sibling `[data-workbench-panel]`. Reuse `_exchange_positions_panel.html`, `_strategy_mid_panel.html`, `_kol_strategy_list.html`, and `_strategy_detail.html` initially; do not rewrite their internal business actions in this task.

**Step 6: Run render tests**

Run: `pytest tests/test_web_page_render.py tests/test_web_app.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/templates/index.html src/telegram_kol_research/templates/_home_dashboard.html src/telegram_kol_research/templates/_workbench_nav.html tests/test_web_page_render.py
git commit -m "feat: add mobile-first workbench shell"
```

### Task 3: Add navigation, filtering, and non-disruptive live updates

**Files:**
- Modify: `src/telegram_kol_research/static/app.js:1050-1105`
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `tests/test_live_update_stream.py`

**Step 1: Write failing static behavior assertions**

Require semantic controllers for `[data-workbench-view]`, `[data-workbench-panel]`, `[data-home-event-filter]`, and `[data-new-home-events]`. Add a live-update test proving a new event increments a pending count without replacing the visible feed immediately.

**Step 2: Verify failure**

Run: `pytest tests/test_web_assets_smoke.py tests/test_live_update_stream.py -v`

Expected: FAIL because the new controllers and event payload are absent.

**Step 3: Implement accessible destination switching**

Replace the current mobile-only class switcher with one `setWorkbenchView(view)` controller that:

- Activates the matching panel.
- Synchronizes mobile and desktop navigation.
- Updates `aria-current` and focus predictably.
- Defaults to `home`.
- Keeps existing dashboard settings routes reachable from `more`.

**Step 4: Implement event filters**

Filter already-rendered event cards by `data-home-event-kind`. Preserve an all-events option and update the empty-state text when a filter has no matches.

**Step 5: Implement the new-event indicator**

When the live stream reports new activity and the user is not at the top of the feed, increment `[data-new-home-events]`. Only prepend or refresh events after the user activates that control. Do not force scroll position.

**Step 6: Run focused tests**

Run: `pytest tests/test_web_assets_smoke.py tests/test_live_update_stream.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/static/app.js tests/test_web_assets_smoke.py tests/test_live_update_stream.py
git commit -m "feat: add workbench navigation and event filters"
```

### Task 4: Apply the mobile-first visual system and desktop enhancement

**Files:**
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Add failing CSS contract tests**

Assert the stylesheet contains named layout hooks for the workbench rail, home summary grid, event cards, priority risk banner, mobile safe area, 44px minimum touch targets, and desktop detail drawer. Keep the existing assertion that mobile navigation is hidden by default outside its media query.

**Step 2: Verify failure**

Run: `pytest tests/test_web_assets_smoke.py -v`

Expected: FAIL on the new layout contracts.

**Step 3: Implement base visual tokens**

Add CSS custom properties for surface, border, primary text, muted text, profit, loss, and risk. Use color plus written labels; do not encode state through color alone.

**Step 4: Implement mobile layout first**

At widths up to 760px:

- Use one visible workbench panel at a time.
- Fix the five-item bottom navigation with `env(safe-area-inset-bottom)`.
- Render the summary as a two-column metric grid.
- Allow the document to scroll naturally.
- Give interactive controls a minimum 44px target.
- Collapse the summary to a compact state using a root class controlled by scroll position.

**Step 5: Implement the desktop enhancement**

At desktop widths, show a left navigation rail, fluid central content, and an optional right detail drawer. Cap line lengths and card widths so timeline content does not stretch across the viewport.

**Step 6: Run tests**

Run: `pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/static/app.css tests/test_web_assets_smoke.py
git commit -m "feat: style mobile-first trading workbench"
```

### Task 5: Focus positions, strategies, and messages without changing semantics

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/templates/_strategy_mid_panel.html`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Modify: `src/telegram_kol_research/templates/_kol_strategy_list.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write failing semantic render tests**

Require positions tabs for open positions, orders, and history; strategy filters for executing, confirmation, pending entry, completed, and abnormal; group rows with last activity and unread/attention indicators; and detail-only containers for bound-position close and binding actions.

**Step 2: Verify failure**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -v`

Expected: FAIL for the new information hierarchy.

**Step 3: Refactor position cards**

Reorder existing fields so direction, PnL, entry/mark price, size, stops, take profits, and attribution are scannable. Keep current endpoint hooks and confirmation behavior unchanged. Move dangerous controls into an expandable/detail region.

**Step 4: Refactor strategy cards and details**

Expose the confirmed filter names and a human-readable blocking reason. Reuse `_strategy_lifecycle_timeline.html` for the full message-to-execution history.

**Step 5: Refactor message navigation**

Keep backend latest-activity ordering. Make group/KOL rows compact and keep the message conversation chronological. Move recognition metadata to the selected-message detail instead of expanding it in every list row.

**Step 6: Run regression tests**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/templates/_exchange_positions_panel.html src/telegram_kol_research/templates/_strategy_mid_panel.html src/telegram_kol_research/templates/_strategy_detail.html src/telegram_kol_research/templates/_kol_strategy_list.html src/telegram_kol_research/static/app.css tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "feat: focus trading detail destinations"
```

### Task 6: Make stale, empty, error, and mutation states explicit

**Files:**
- Modify: `src/telegram_kol_research/templates/_home_dashboard.html`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write failing state tests**

Cover independent Telegram/database/Deepcoin states, last-success timestamp on stale content, legitimate empty results versus load errors, persistent action result messages, disabled pending buttons, and duplicate-submit protection.

**Step 2: Verify failure**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -v`

Expected: FAIL on missing state hooks or labels.

**Step 3: Implement explicit state components**

Add reusable markup/classes for `is-loading`, `is-empty`, `is-stale`, and `is-error`. Preserve last successful data on refresh failures and display its timestamp. Do not replace specific service failures with a single generic system status.

**Step 4: Harden mutation feedback**

On submit, disable the initiating control and set `aria-busy=true`. Restore it only when the request finishes. Keep success or error text in an `aria-live` status region and preserve the existing `dialog.returnValue` reset before every confirmation opening.

**Step 5: Run focused tests**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_app.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/templates/_home_dashboard.html src/telegram_kol_research/templates/_exchange_positions_panel.html src/telegram_kol_research/static/app.js src/telegram_kol_research/static/app.css tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "fix: clarify dashboard operational states"
```

### Task 7: Complete local regression and server verification handoff

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/context/telegram-deepcoin-auto-trading-context.md`

**Step 1: Run formatting and syntax checks**

Run: `python -m compileall -q src tests`

Expected: exit 0.

**Step 2: Run focused web coverage**

Run: `pytest tests/test_web_queries_dashboard.py tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_group_messages_route.py tests/test_live_update_stream.py -v`

Expected: PASS.

**Step 3: Run the full local suite**

Run: `pytest -q`

Expected: PASS, or document any independently reproduced pre-existing failures without attributing them to this redesign.

**Step 4: Perform manual responsive QA**

Check at 390x844, 430x932, 768x1024, and 1440x900. Verify navigation, natural page scrolling, long Chinese text wrapping, new-event behavior, stale data, empty data, and confirmation dialogs. Do not use production secrets locally.

**Step 5: Record the durable UI decision**

Update the repository context docs with the mobile-first home/event-feed architecture, the five primary destinations, and the detail-only dangerous-action rule. Do not store screenshots containing account data or secrets.

**Step 6: Commit documentation**

```bash
git add docs/migration-handoff.md docs/context/telegram-deepcoin-auto-trading-context.md
git commit -m "docs: record mobile-first workbench architecture"
```

**Step 7: Push and verify on the production server**

After review, push `codex/deepcoin-auto-trading-v1`, then use the established Mac/Linux helper:

```bash
./scripts/server_git_update.sh
```

Expected: the server pulls from GitHub, reinstalls the editable package, restarts `telegram-kol.service`, and reports `ActiveState=active` and `SubState=running`. On the server, verify real Telegram freshness, Deepcoin snapshot rendering, live event arrival, and confirmation-backed actions without changing trading policy.

