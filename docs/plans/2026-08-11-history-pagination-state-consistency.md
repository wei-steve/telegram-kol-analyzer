# 历史仓位分页状态一致性修复 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make lazy historical-position controls survive frontend deployments and keep the displayed loaded count equal to the unique positions actually present in both views.

**Architecture:** Every rendered page and partial exposes the running server asset version, and the shared partial fetcher rejects a fragment from a different build before touching the DOM, then performs one guarded full reload while preserving the existing saved exchange-tab/view state. Historical continuation fragments report page-local metadata only; the browser owns the cumulative unique-position count and applies the same accepted IDs to the real and grouped views.

**Tech Stack:** FastAPI/Jinja, vanilla JavaScript, pytest, Node.js DOM harness, Chrome production verification.

---

### Task 1: Add a workbench asset-version handshake

**Files:**
- Modify: `src/telegram_kol_research/templates/base.html: html root element`
- Modify: `src/telegram_kol_research/web_app.py: cache/version middleware and partial contexts`
- Modify: `src/telegram_kol_research/static/app.js: fetchWorkbenchPartial and reload guard`
- Test: `tests/test_web_app.py: versioned asset/response tests`
- Test: `tests/test_web_assets_smoke.py: partial-fetch Node harness`
- Test: `tests/test_web_page_render.py: page/partial metadata assertions`

**Step 1: Write failing server-contract tests**

Add assertions that the root page exposes the current version and every workbench partial response returns the same version header:

```python
def test_workbench_pages_and_partials_expose_running_asset_version(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    root = client.get("/")
    partial = client.get("/positions-panel?initial=positions")

    assert f'data-workbench-asset-version="{app.state.asset_version}"' in root.text
    assert partial.headers["x-workbench-asset-version"] == str(app.state.asset_version)
```

Cover at least `/positions-panel`, `/positions-panel/tabs/position-history`, `/groups`, and one more lazy panel so the contract is shared rather than history-specific.

**Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py -k workbench_asset_version -v`

Expected: FAIL because the root attribute and partial response header do not exist.

**Step 3: Implement the server contract**

- Add `data-workbench-asset-version="{{ asset_version }}"` to the `<html>` element in `base.html`.
- Extend the existing HTTP middleware to set `X-Workbench-Asset-Version` on HTML and fragment responses while keeping the current immutable-cache behavior for matching static assets.
- Ensure all page contexts continue to include `asset_version`; do not duplicate version calculation per request.

**Step 4: Run the server tests and verify GREEN**

Run: `uv run pytest tests/test_web_app.py tests/test_web_page_render.py -k workbench_asset_version -v`

Expected: PASS.

**Step 5: Write the failing browser mismatch test**

Extract `fetchWorkbenchPartial()` and its version helpers into the existing Node harness. Simulate a document version of `100`, a response header version of `101`, and valid fragment HTML. Assert that:

```javascript
await fetchWorkbenchPartial('/positions-panel', '[data-exchange-position-tabs]');
if (replaceCalls !== 0) throw new Error('mismatched fragment reached the DOM');
if (reloadCalls !== 1) throw new Error('version mismatch did not reload once');
```

Call it twice for the same target version and assert the session guard prevents a reload loop.

**Step 6: Run the browser test and verify RED**

Run: `uv run pytest tests/test_web_assets_smoke.py -k asset_version_mismatch -v`

Expected: FAIL because the fetcher does not compare build versions.

**Step 7: Implement the guarded mismatch handler**

- Read the current version from `document.documentElement.dataset.workbenchAssetVersion`.
- Read `X-Workbench-Asset-Version` before parsing response HTML.
- If both exist and differ, do not parse or return the fragment.
- Reuse the existing localStorage-backed exchange tab and view persistence; show `检测到新版本，正在刷新…` in the nearest available status region.
- Store a target-version marker in `sessionStorage`, call `window.location.reload()` once, and throw a dedicated mismatch error so callers cannot continue DOM work.
- If the same target marker already exists, do not reload again; keep the explanatory status visible.

**Step 8: Run the browser test and verify GREEN**

Run: `uv run pytest tests/test_web_assets_smoke.py -k asset_version_mismatch -v`

Expected: PASS.

**Step 9: Commit**

```bash
git add src/telegram_kol_research/templates/base.html src/telegram_kol_research/web_app.py src/telegram_kol_research/static/app.js tests/test_web_app.py tests/test_web_assets_smoke.py tests/test_web_page_render.py
git commit -m "fix: reload workbench on asset version mismatch"
```

### Task 2: Replace page-local visible count with a cumulative unique count

**Files:**
- Modify: `src/telegram_kol_research/web_app.py: history_pagination metadata`
- Modify: `src/telegram_kol_research/templates/_exchange_position_tab.html: history metadata/footer`
- Modify: `src/telegram_kol_research/static/app.js: loadMoreHistoryPositions and grouped merge`
- Test: `tests/test_web_page_render.py: history fragment metadata`
- Test: `tests/test_web_assets_smoke.py: history pagination Node harness`

**Step 1: Write failing fragment-contract tests**

Update the position-history route test to require explicit page-local naming:

```python
assert 'data-history-page-item-count="20"' in first.text
assert 'data-history-total-count="45"' in first.text
assert 'data-history-visible-count' not in first.text
```

Assert the second fragment also reports `page_item_count=20`, proving it does not claim that only 20 items are cumulatively visible.

**Step 2: Run the fragment test and verify RED**

Run: `uv run pytest tests/test_web_page_render.py -k position_history_tab_serves_continuation -v`

Expected: FAIL because the template still renders `data-history-visible-count`.

**Step 3: Implement page-local server metadata**

- Rename `history_pagination.visible_count` to `page_item_count` in both first-page and continuation contexts.
- Render `data-history-page-item-count` and `data-history-total-count` on the history panel.
- Keep the first-page footer text server-rendered from `page_item_count / total_count` for no-JavaScript readability.
- Do not calculate cumulative counts on the server because continuation requests are stateless views into the immutable browse snapshot.

**Step 4: Run the fragment test and verify GREEN**

Run: `uv run pytest tests/test_web_page_render.py -k position_history_tab_serves_continuation -v`

Expected: PASS.

**Step 5: Write the failing cumulative-count browser test**

Build a focused fake history panel with 20 stable IDs, then return a continuation fragment containing 20 new IDs. Assert after `loadMoreHistoryPositions(root)`:

```javascript
if (listUniqueIds.size !== 40) throw new Error('list did not reach 40 unique rows');
if (panel.dataset.historyLoadedCount !== '40') throw new Error('cumulative count is wrong');
if (status.textContent !== '已显示 40 / 100 条') throw new Error('footer count is wrong');
```

Return another page containing one existing ID and nineteen new IDs; assert the result advances only to 59. Run the same harness with grouped mode active and assert list/grouped toggling does not alter the shared count.

**Step 6: Run the browser test and verify RED**

Run: `uv run pytest tests/test_web_assets_smoke.py -k history_cumulative_unique_count -v`

Expected: FAIL because the browser replaces the footer with a page-local `20 / total` footer and appends duplicate cards.

**Step 7: Implement cumulative unique state**

- Initialize `data-history-loaded-count` from the unique IDs in the first list view.
- Before appending, build the existing stable-ID set and accept only incoming IDs not already present.
- Append accepted list cards only and pass the same accepted-ID set to `appendHistoryGroups()` so grouped view receives the identical logical rows.
- Increase the loaded count by `acceptedIds.size`, never by the raw fragment count.
- Update cursor and `has_more` only after successful merge.
- Preserve the existing footer/status node; replace or remove only its load-more button, then render `已显示 N / total 条` or `已显示全部 total 条`.
- On failure, leave IDs, count, cursor and `has_more` unchanged.

**Step 8: Run the browser test and verify GREEN**

Run: `uv run pytest tests/test_web_assets_smoke.py -k history_cumulative_unique_count -v`

Expected: PASS.

**Step 9: Commit**

```bash
git add src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/_exchange_position_tab.html src/telegram_kol_research/static/app.js tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "fix: track cumulative history position count"
```

### Task 3: Verify refresh/reset behavior and deploy

**Files:**
- Modify: none unless a focused regression requires a minimal correction
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_app.py`

**Step 1: Add reset-path assertions**

Extend the Node harness so manual `刷新历史仓位`, applying a date filter, and clearing a filter each replace the panel with a fresh first page whose loaded count is initialized from that page. Assert old cumulative count, cursor and mismatch marker are not reused.

**Step 2: Run focused regression tests**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py -k "history or asset_version" -v
uv run pytest tests/test_web_page_render.py -k "position_history or workbench_asset_version" -v
uv run pytest tests/test_web_app.py -k "asset_version or versioned_workbench_assets" -v
```

Expected: PASS.

**Step 3: Run the complete Web regression**

Run: `uv run pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py tests/test_web_app.py -q`

Expected: PASS.

**Step 4: Review the patch**

Run: `git diff --check && git status --short`

Expected: no whitespace errors and only intended files staged/committed; preserve all unrelated dirty-worktree files.

**Step 5: Push and deploy**

```bash
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

Expected: the server fast-forwards to the pushed SHA, reinstalls the editable package, and reports `telegram-kol.service` active.

**Step 6: Verify production in Chrome**

- Keep one pre-deployment page open to verify the documented one-time hard-refresh boundary.
- After full refresh, confirm the new script version is loaded and the history load-more button is bound.
- Open history positions in real-list mode: confirm `20 / total`, click load more, then confirm 40 unique list cards and `40 / total`.
- Switch to grouped mode and confirm the same loaded count and the same unique position-ID set.
- Trigger one additional page when available and confirm 60, or the exact remaining count on the last page.
- Inspect console output for pagination/version errors and verify all history requests are GET/read-only.
- Confirm `systemctl is-active telegram-kol.service` and root HTTP 200 after verification.
