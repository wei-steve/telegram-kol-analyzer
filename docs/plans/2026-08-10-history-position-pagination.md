# 历史仓位渐进加载与时间筛选 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep the historical-position tab fast at first render while allowing safe, stable, read-only browsing beyond the first 20 records and filtering by close-time range.

**Architecture:** The server fetches and normalizes the bounded DeepCoin history once per short-lived browser snapshot, then serves cursor pages from that in-memory immutable snapshot. The browser renders page one normally, appends later fragment pages through one single-flight request, and resets the snapshot when refreshing or changing filters. The existing DeepCoin client remains read-only and all existing position/order tabs keep their current contracts.

**Tech Stack:** FastAPI/Jinja, SQLAlchemy query helpers, Python `datetime`, vanilla JavaScript, pytest, Node.js browser-state harness.

---

### Task 1: Specify a bounded immutable browse-snapshot store

**Files:**
- Modify: `src/telegram_kol_research/web_app.py: module-level dashboard helpers and create_web_app state setup`
- Test: `tests/test_web_page_render.py: exchange tab route tests`

**Step 1: Write the failing tests**

Add deterministic clock-driven tests for a private `HistoryPositionBrowseSnapshotStore`:

```python
def test_history_browse_snapshot_returns_stable_cursor_pages():
    store = HistoryPositionBrowseSnapshotStore(now_provider=lambda: now)
    snapshot = store.create(rows=rows, filter_key=(None, None))

    assert store.page(snapshot.token, cursor=None, page_size=20).rows == rows[:20]
    assert store.page(snapshot.token, cursor=rows[19]["history_sort_id"], page_size=20).rows == rows[20:40]

def test_history_browse_snapshot_expires_and_refuses_filter_mismatch():
    ...
```

Test token opacity, a 20-item maximum page size, expiry, maximum snapshot count/eviction, and rejection of a cursor not belonging to the snapshot. Do not add persistence or database tables.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_page_render.py -k history_browse_snapshot -v`

Expected: FAIL because the snapshot store is not defined.

**Step 3: Implement the minimal store**

In `web_app.py`, add a small lock-protected in-memory store with:

- an opaque random token;
- immutable tuple rows plus normalized `(closed_after, closed_before)` filter key;
- creation and expiry timestamps supplied by `app.state.now_provider`;
- a fixed TTL, capacity, page-size cap of 20, and max-page cap;
- deterministic pagination over the already sorted `history_sort_id` sequence;
- no DeepCoin client reference and no mutation capability.

Inject this store into `app.state` inside `create_web_app`, allowing tests to pass a deterministic clock/store only through normal app construction seams.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_page_render.py -k history_browse_snapshot -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_page_render.py
git commit -m "feat: add history position browse snapshots"
```

### Task 2: Add validated, read-only paginated history route data

**Files:**
- Modify: `src/telegram_kol_research/web_app.py: _load_exchange_tab_snapshot, build_exchange_position_tab_context, positions_panel_tab_partial`
- Modify: `src/telegram_kol_research/templates/_exchange_position_tab.html: position-history fragment metadata and footer`
- Test: `tests/test_web_page_render.py: position-history tab route coverage`

**Step 1: Write the failing tests**

Use a fake client with 45 fully qualified historical positions and assert:

```python
first = client.get("/positions-panel/tabs/position-history?page_size=20")
assert 'data-history-browse-token=' in first.text
assert 'data-history-next-cursor=' in first.text
assert 'data-history-visible-count="20"' in first.text
assert 'data-history-has-more="true"' in first.text
assert fake.calls == [("list_position_history", "BTC-USDT-SWAP")]

second = client.get(
    "/positions-panel/tabs/position-history?browse_token=...&cursor=..."
)
assert "pos-20" in second.text
assert "pos-0" not in second.text
assert fake.calls == [("list_position_history", "BTC-USDT-SWAP")]
```

Add cases for expired/invalid token returning a retryable reload fragment, a last 5-item page with no next cursor, `closed_after` / `closed_before` filtering, invalid/reversed dates returning HTTP 422, and no calls beyond `list_position_history`.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_page_render.py -k "position_history and (pagination or filter)" -v`

Expected: FAIL because the route ignores pagination and filtering arguments.

**Step 3: Implement the minimal server contract**

- Extend only the `position-history` route with optional `browse_token`, `cursor`, `closed_after`, and `closed_before` query parameters.
- On a first request, call the existing `_load_exchange_tab_snapshot(..., order_limit=...)` high enough to capture the intended bounded DeepCoin result, normalize/sort with its existing stable sort key, apply an inclusive close-time filter, and store the filtered rows in a browse snapshot.
- On continuation, read only the browse snapshot. Do not create a DeepCoin client or call the exchange.
- Keep `open-orders` and `order-history` behavior byte-for-byte compatible.
- Pass `history_pagination` metadata to the template: token, next cursor, visible count, optional total count, has-more, active date filter, and capture time.
- In the position-history fragment, render metadata attributes and a semantic footer placeholder for the browser. Do not render a page-two footer in list/grouped card loops.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_page_render.py -k "position_history and (pagination or filter)" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/_exchange_position_tab.html tests/test_web_page_render.py
git commit -m "feat: paginate history position tab"
```

### Task 3: Render the pagination and date-filter controls

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html: exchange view toolbar`
- Modify: `src/telegram_kol_research/static/app.css: exchange history pagination/filter styles`
- Test: `tests/test_web_page_render.py: initial panel markup`
- Test: `tests/test_web_assets_smoke.py: static asset assertions`

**Step 1: Write the failing tests**

Assert that the initial shell includes a history-only, hidden-by-default filter control and that a successful history fragment exposes:

```python
assert 'data-history-position-filter' in response.text
assert 'data-history-filter-preset="30d"' in response.text
assert 'data-history-load-more' in response.text
assert 'data-history-visible-count' in response.text
```

Also assert CSS scopes the controls under `data-exchange-history-panel`, supports narrow screens, contains disabled/busy styling, and introduces no global form selector changes.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_page_render.py -k history_filter -v && uv run pytest tests/test_web_assets_smoke.py -k history -v`

Expected: FAIL because the controls do not exist.

**Step 3: Implement the minimal markup and style**

- Add an expandable filter control in the shared toolbar, visible only when `position-history` is selected.
- Include preset buttons (7d, 30d, 90d), native `date` inputs, an apply button, and a clear button.
- Add a footer below both list and grouped history panels that contains load-more/retry and a polite count/status message.
- Use the existing dark DeepCoin history visual treatment and accessible labels; preserve the existing toolbar refresh control and its placement.
- Do not introduce a third-party UI library.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_page_render.py -k history_filter -v && uv run pytest tests/test_web_assets_smoke.py -k history -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/templates/_exchange_positions_panel.html src/telegram_kol_research/static/app.css tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "feat: add history position browse controls"
```

### Task 4: Implement browser-side continuation, reset, and single-flight behavior

**Files:**
- Modify: `src/telegram_kol_research/static/app.js: exchange position tab state and loaders`
- Test: `tests/test_web_assets_smoke.py: Node browser-state harness`

**Step 1: Write the failing browser behavior tests**

Extend the existing fake DOM harness to prove:

```javascript
await loadMoreHistoryPositions(root);
assert.equal(fetchCalls.length, 2); // initial + one continuation
assert.equal(renderedIds.size, 40);

await Promise.all([loadMoreHistoryPositions(root), loadMoreHistoryPositions(root)]);
assert.equal(continuationFetches, 1);
```

Also verify refresh clears continuation rows and restarts page one, filter apply sends dates with no old token/cursor, an error retains existing cards and exposes retry, and list/grouped mode plus active tab are preserved.

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_assets_smoke.py -k "history and (pagination or filter)" -v`

Expected: FAIL because no continuation or filter functions exist.

**Step 3: Implement the minimal JavaScript**

- Keep `loadExchangePositionTab()` as the page-one path and add explicit options for history query parameters and reset behavior.
- Store active history browse metadata per panel/root, not in localStorage; an expired page must always restart safely.
- Add `loadMoreHistoryPositions()` that reuses an in-flight promise, fetches the continuation fragment, extracts only new history cards, appends them to the active list and grouped views by stable `data-history-position-id`, then updates the footer metadata.
- Bind filter/apply/clear/load-more/retry controls idempotently after a fragment replacement.
- Make the existing manual refresh force a page-one reset only for `position-history`; its behavior for other tabs remains unchanged.
- Preserve successful content after continuation failure and announce “加载失败，重试”.

**Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_assets_smoke.py -k "history and (pagination or filter)" -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/static/app.js tests/test_web_assets_smoke.py
git commit -m "feat: load more history positions in browser"
```

### Task 5: Run regression checks and perform a safe production verification

**Files:**
- Modify: none unless a test reveals a targeted defect
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Run focused test suites**

Run:

```bash
uv run pytest tests/test_web_page_render.py -k "position_history or positions_panel_tab" -v
uv run pytest tests/test_web_assets_smoke.py -k "exchange or history" -v
```

Expected: PASS.

**Step 2: Run the broader local regression**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -q`

Expected: PASS.

**Step 3: Inspect the final patch**

Run: `git diff HEAD~4..HEAD --check && git status --short`

Expected: no whitespace errors; only the intended implementation files changed.

**Step 4: Commit any verification-only fixes**

```bash
git add <intended-files>
git commit -m "test: cover history position pagination"
```

Only commit if this task produced an actual corrective change.

**Step 5: Push and verify on the server in a proven safe window**

After review, push the branch and use the repository helper:

```bash
git push origin codex/deepcoin-auto-trading-v1
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

On the authenticated production dashboard: open 历史仓位, verify the first 20 cards load quickly, load at least two more pages, test one date preset and a custom range, refresh back to the newest page, and use browser DevTools/network logs to confirm only GET/read requests. Do not deploy or restart during an active time-sensitive strategy operation. If a safe window is unavailable, leave deployment pending and document the exact production checks still required.
