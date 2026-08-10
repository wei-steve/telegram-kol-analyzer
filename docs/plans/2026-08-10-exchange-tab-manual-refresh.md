# Exchange Tab Manual Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add explicit, operator-triggered refresh controls for current orders, order history, and position history while preserving successful content and preventing duplicate Deepcoin reads.

**Architecture:** Keep the existing lazy tab routes and the per-tab single-flight promise map. Add response metadata to each successful partial, one active-tab refresh control in the positions shell, and a `force` option that bypasses only the browser's loaded-state shortcut. Refresh failures retain the last successful DOM; initial failures remain retryable.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, CSS, pytest, Starlette `TestClient`, Node-based browser behavior harnesses.

---

### Task 1: Specify the tab-partial refresh contract

**Files:**
- Modify: `tests/test_web_page_render.py:1242-1281`
- Modify: `src/telegram_kol_research/web_app.py:5180-5220`
- Modify: `src/telegram_kol_research/templates/_exchange_position_tab.html:1-36`

**Step 1: Write the failing route tests**

Extend the existing parameterized tab-route test so every successful response
has an item count and a UTC capture timestamp. Add an explicit retry assertion
to the existing failure test.

```python
captured_at = datetime(2026, 8, 10, 0, 5, 6, tzinfo=UTC)
client = TestClient(
    create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: exchange,
        now_provider=lambda: captured_at,
    )
)

response = client.get(f"/positions-panel/tabs/{tab_name}")

assert response.status_code == 200
assert 'data-exchange-tab-item-count="' in response.text
assert (
    'data-exchange-tab-captured-at="2026-08-10T00:05:06+00:00"'
    in response.text
)
```

For `test_positions_panel_tab_failure_stays_retryable`, add:

```python
assert 'data-exchange-tab-retry="open-orders"' in response.text
assert "重新加载" in response.text
assert "data-exchange-tab-captured-at" not in response.text
```

**Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_web_page_render.py::test_positions_panel_tab_route_reads_only_requested_dataset \
  tests/test_web_page_render.py::test_positions_panel_tab_failure_stays_retryable -q
```

Expected: FAIL because the partial has no count, capture time, or retry control.

**Step 3: Add bounded capture metadata to the route context**

In `build_exchange_position_tab_context()`, normalize the configured clock to
UTC and expose it only after a successful exchange read:

```python
captured_at = app.state.now_provider()
if captured_at.tzinfo is None:
    captured_at = captured_at.replace(tzinfo=UTC)
else:
    captured_at = captured_at.astimezone(UTC)

return {
    "tab_name": tab_name,
    "exchange_snapshot": exchange_snapshot,
    "exchange_tab_captured_at": (
        None if exchange_snapshot.get("error") else captured_at
    ),
}
```

Do not add a cache, database write, or new endpoint.

**Step 4: Render metadata and the initial retry action**

After the existing `items` selection in `_exchange_position_tab.html`, add
metadata to the root section:

```jinja2
data-exchange-tab-item-count="{{ items|length }}"
{% if exchange_tab_captured_at %}
data-exchange-tab-captured-at="{{ exchange_tab_captured_at.isoformat() }}"
{% endif %}
```

Replace the unavailable paragraph with:

```jinja2
<div class="exchange-tab-load-error" role="status">
  <p class="exchange-empty">Deepcoin 数据暂不可用，请稍后重试。</p>
  <button
    type="button"
    class="secondary-button"
    data-exchange-tab-retry="{{ tab_name }}"
  >重新加载</button>
</div>
```

**Step 5: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: PASS, with the existing method-isolation assertions unchanged.

**Step 6: Commit the server contract**

```bash
git add \
  tests/test_web_page_render.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_position_tab.html
git commit -m "feat: expose exchange tab refresh metadata"
```

### Task 2: Add the shared manual-refresh interface

**Files:**
- Modify: `tests/test_web_page_render.py:1800-1870`
- Modify: `tests/test_web_assets_smoke.py:100-125`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:248-266`
- Modify: `src/telegram_kol_research/static/app.css:1102-1190`

**Step 1: Write failing shell and CSS assertions**

Add shell assertions to the existing workbench render test:

```python
assert 'data-exchange-tab-refresh-controls' in response.text
assert 'data-exchange-tab-refresh' in response.text
assert 'data-exchange-tab-refresh-status' in response.text
assert 'data-exchange-position-label="当前委托"' in response.text
assert 'data-exchange-position-label="历史委托"' in response.text
assert 'data-exchange-position-label="历史仓位"' in response.text
```

Add CSS smoke assertions:

```python
assert ".exchange-view-toolbar" in css
assert ".exchange-tab-refresh-controls" in css
assert ".exchange-tab-refresh-status" in css
```

**Step 2: Run tests and verify RED**

```bash
uv run pytest \
  tests/test_web_page_render.py -k 'desktop_workbench' \
  tests/test_web_assets_smoke.py -k 'desktop or exchange' -q
```

Expected: FAIL because the toolbar and selectors do not exist.

**Step 3: Add stable tab labels and the shared refresh toolbar**

Add `data-exchange-position-label` to each tab button. Keep `持仓`
functionally unchanged, but give all buttons a stable base label so JavaScript
can update counts without parsing translated text.

Wrap the existing view switch in:

```jinja2
<div class="exchange-view-toolbar">
  <div class="exchange-view-switch" role="tablist" aria-label="交易持仓视图">
    ...existing view buttons...
  </div>
  <div
    class="exchange-tab-refresh-controls"
    data-exchange-tab-refresh-controls
    hidden
  >
    <button type="button" class="secondary-button" data-exchange-tab-refresh>
      刷新当前分页
    </button>
    <span
      class="exchange-tab-refresh-status"
      data-exchange-tab-refresh-status
      role="status"
      aria-live="polite"
    ></span>
  </div>
</div>
```

**Step 4: Add responsive styling**

```css
.exchange-view-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
}

.exchange-tab-refresh-controls {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.exchange-tab-refresh-controls[hidden] {
  display: none;
}

.exchange-tab-refresh-status {
  color: #94a3b8;
  font-size: 0.78rem;
}
```

Keep the existing history-tab orange treatment and dark-theme selectors.

**Step 5: Run tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 6: Commit the interface shell**

```bash
git add \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/static/app.css
git commit -m "feat: add exchange tab refresh controls"
```

### Task 3: Implement force refresh with single-flight protection

**Files:**
- Modify: `tests/test_web_assets_smoke.py:516-700`
- Modify: `src/telegram_kol_research/static/app.js:2311-2431`

**Step 1: Write a failing Node behavior test**

Add a focused Node harness alongside the existing exchange-tab persistence test.
The fake root must provide a loaded `open-orders` panel, shared refresh
button/status, a replacement fragment, and a fetch counter. Assert:

```javascript
await loadExchangePositionTab(root, 'open-orders');
if (fetchCount !== 0) throw new Error('normal load bypassed the cache');

const first = loadExchangePositionTab(root, 'open-orders', { force: true });
const second = loadExchangePositionTab(root, 'open-orders', { force: true });
if (first !== second) throw new Error('refresh did not reuse the in-flight request');
await first;
if (fetchCount !== 1) throw new Error(`expected one refresh, got ${fetchCount}`);
if (root.currentPanel !== refreshedPanel) throw new Error('fresh partial not committed');
if (refreshButton.disabled) throw new Error('refresh button stayed disabled');
if (tabButton.textContent !== '当前委托(5)') throw new Error('count not updated');
```

Add a second case where `fetchWorkbenchPartial` rejects during a forced
refresh. Assert that the original panel object remains mounted, its
`data-exchange-tab-loaded` stays `true`, the button is enabled, and the
status contains `刷新失败，当前展示上次成功数据`.

Add a retry-control case that activates `data-exchange-tab-retry` and verifies
it calls the same forced loader.

**Step 2: Run the behavior test and verify RED**

```bash
uv run pytest \
  tests/test_web_assets_smoke.py -k 'exchange_position_tab_manual_refresh' -q
```

Expected: FAIL because `force`, refresh-state synchronization, and metadata
commit behavior do not exist.

**Step 3: Add labels and refresh-state helpers**

Near `EXCHANGE_POSITION_TABS`, add:

```javascript
const EXCHANGE_POSITION_TAB_LABELS = {
  positions: '持仓',
  'open-orders': '当前委托',
  'order-history': '历史委托',
  'position-history': '历史仓位',
};
```

Add these public helper shapes so tests can extract them reliably:

```javascript
function syncExchangeTabRefreshControls(root, tab) { ... }
function setExchangeTabRefreshBusy(root, tab, busy) { ... }
function updateExchangeTabRefreshMetadata(root, tab, fragment) { ... }
function setExchangeTabRefreshStatus(root, message, isError = false) { ... }
```

The helpers must:

- hide refresh controls for `positions`;
- label the control from `EXCHANGE_POSITION_TAB_LABELS`;
- disable it and show `刷新中…` while busy;
- update the tab button using `fragment.dataset.exchangeTabItemCount`;
- format `fragment.dataset.exchangeTabCapturedAt` as `HH:mm:ss UTC`;
- expose only bounded fixed status messages, never raw exchange errors.

Call `syncExchangeTabRefreshControls(root, selectedTab)` at the end of
`setExchangePositionTab()`.

**Step 4: Bind shared refresh and retry controls idempotently**

In `bindExchangePositionTabs()`, bind the shared control once:

```javascript
const refreshButton = root.querySelector('[data-exchange-tab-refresh]');
if (refreshButton && refreshButton.dataset.exchangeTabRefreshBound !== 'true') {
  refreshButton.dataset.exchangeTabRefreshBound = 'true';
  refreshButton.addEventListener('click', () => {
    const tab = exchangePositionUiState(root).tab;
    loadExchangePositionTab(root, tab, { force: true });
  });
}
```

Add an idempotent `bindExchangeTabRetryControls(root)` for buttons contained
in new partials. Call it during initial binding and after every partial
replacement.

**Step 5: Add the force option without weakening normal lazy caching**

Change the loader signature and loaded guard:

```javascript
async function loadExchangePositionTab(root, tab, { force = false } = {}) {
  ...
  const wasLoaded = panel.dataset.exchangeTabLoaded === 'true';
  if (wasLoaded && !force) return true;
  ...
}
```

Check `requests.has(tab)` before starting new visual or network work. During a
forced refresh, keep the panel mounted, mark the control busy, and do not replace
the current list with a loading placeholder.

On success:

```javascript
current.replaceWith(fragment);
setExchangePositionTab(root, exchangePositionTab());
setExchangePositionView(root, exchangePositionViewMode());
updateExchangeTabRefreshMetadata(root, tab, fragment);
bindExchangeTabRetryControls(root);
bindBoundPositionCloseButtons();
bindLivePositionAttributionButtons();
```

Return whether the new fragment has `data-exchange-tab-loaded="true"`. If the
route returns an unavailable partial, leave it retryable and show a bounded
failure status.

In `catch`, branch on `wasLoaded && force`:

- forced refresh: keep the current panel unchanged and show the preserved-data
  message;
- initial load: keep the retryable unavailable path.

Always clear busy state and delete the promise-map entry in `finally`.

**Step 6: Run focused behavior tests and verify GREEN**

```bash
uv run pytest \
  tests/test_web_assets_smoke.py -k \
  'exchange_position_tab_manual_refresh or lazy_loads_exchange_position_tabs or exchange_position_tab_persists' -q
```

Expected: PASS.

**Step 7: Commit the browser behavior**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.js
git commit -m "feat: refresh exchange tabs on demand"
```

### Task 4: Run focused and full local verification

**Files:**
- Verify: `src/telegram_kol_research/static/app.js`
- Verify: `src/telegram_kol_research/static/app.css`
- Verify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Verify: `src/telegram_kol_research/templates/_exchange_position_tab.html`
- Verify: `src/telegram_kol_research/web_app.py`
- Verify: `tests/test_web_assets_smoke.py`
- Verify: `tests/test_web_page_render.py`

**Step 1: Run focused web tests**

```bash
uv run pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_page_render.py -q
```

Expected: PASS.

**Step 2: Run the broader relevant regression set**

```bash
uv run pytest \
  tests/test_live_position_snapshot.py \
  tests/test_deepcoin_client.py \
  tests/test_web_app.py \
  tests/test_web_assets_smoke.py \
  tests/test_web_page_render.py -q
```

Expected: PASS. These deterministic local checks do not replace server
verification with the real Telegram session and Deepcoin allowlist.

**Step 3: Run syntax and diff checks**

```bash
uv run python -m compileall -q src tests
git diff --check
git status --short
```

Expected: compilation and diff checks succeed. Confirm unrelated existing
changes such as `uv.lock` and user artifacts remain unstaged.

**Step 4: Review the complete implementation diff**

Review against
`docs/plans/2026-08-10-exchange-tab-manual-refresh-design.md`. Block any
finding that can:

- create periodic or focus-triggered refreshes;
- duplicate a Deepcoin read;
- erase successful content on refresh failure;
- change a mutation authorization boundary;
- disturb the live-position snapshot path.

**Step 5: Commit any test-first corrections**

If review requires corrections, apply them test-first and commit only the files
owned by this feature.

### Task 5: Push, deploy in a proven safe window, and verify production

**Files:**
- Reference: `AGENTS.md`
- Reference: `docs/runtime-incident-agent-runbook.md:45-88`
- Use: `scripts/server_git_update.ps1`

**Step 1: Confirm branch and reviewed commits**

```bash
git branch --show-current
git log --oneline --decorate -5
git status --short
```

Expected: branch is `codex/deepcoin-auto-trading-v1`; only intended commits
are pushed; unrelated user files remain untouched.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub reports the reviewed implementation SHA on the target branch.

**Step 3: Prove a production safe window with read-only checks**

Before restarting, confirm:

- `telegram-kol.service` and listener are healthy;
- Telegram checkpoint/freshness is current;
- no recognition, entry, management, exit, rescue, or reconciliation action is
  in flight;
- no management batch is `planned`, `executing`, `reconciling`,
  `submit_unknown`, `partial_failed`, or `recovery_required`;
- protection incidents and the production safety monitor show no blocker;
- the read-only snapshot is complete.

Expected: all checks are clean. If any check is unknown or active, stop after
the push and record exact pending verification; do not restart.

**Step 4: Deploy through the existing helper**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the reviewed branch, reinstalls the editable package,
restarts `telegram-kol.service`, and reports it active.

**Step 5: Run server-side verification**

On the server, confirm the deployed SHA, editable installation, active service,
clean recent journal, resumed listener, and healthy reconciliation. Run:

```bash
uv run pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_page_render.py -q
```

Expected: PASS with the production-installed code.

**Step 6: Verify all three manual refresh paths**

In the authenticated browser, for `当前委托`, `历史委托`, and `历史仓位`:

1. Open the tab and let initial loading finish.
2. Confirm the refresh label matches the active tab.
3. Click once and confirm existing content remains visible while the button
   shows `刷新中…`.
4. Confirm the item count and `HH:mm:ss UTC` timestamp update.
5. Compare visible rows with Deepcoin.
6. Rapidly click twice once and confirm only one request is active.

Also confirm that switching tabs does not refresh an already loaded tab, no
timer/focus refresh occurs, `持仓` behavior is unchanged, and verification
creates no exchange write or Telegram notification.

**Step 7: Record rollout result**

Append a rollout-result section with deployed SHA, safe-window evidence summary,
server tests, service status, and the three UI checks. Commit and push that
documentation-only update. Do not restart for the documentation-only commit.

## Rollout Status — Deployment Deferred (2026-08-10 UTC)

The reviewed implementation through `4fb47b9` was pushed to
`codex/deepcoin-auto-trading-v1`. Local focused Web tests, the broader relevant
regression set, compilation, and diff checks passed; the final review reported
no remaining P1/P2 findings.

The production service and independent safety monitor were healthy, but the
pre-restart read-only window was not safe. The database showed active
deleted-source exit claims in `cancelling_entries`, plus current trigger
take-profit convergence work with submitted and submit-unknown states. Recent
lifecycle updates were also still occurring. The production service was not
restarted and production remains on its prior commit.

Before deployment, wait for these operations to reach terminal states, then
capture two fresh stable read-only snapshots confirming zero in-flight source
deletion, trigger-convergence, management, mutation, rescue, recognition, and
reconciliation work; confirm the safety monitor remains healthy and the live
exchange snapshot is complete. Only then run the standard server-update helper
and complete the browser checks for all three manual-refresh tabs.

## Execution Handoff

Execute task-by-task with test-first checkpoints. Local completion is not the
terminal state: push the reviewed branch, prove a safe deployment window, deploy
through the existing helper, and finish the authenticated production checks.
