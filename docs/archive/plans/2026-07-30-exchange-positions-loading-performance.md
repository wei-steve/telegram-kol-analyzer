# Exchange Positions Loading Performance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the exchange positions workbench show live positions quickly by loading unrelated exchange tabs only when selected.

**Architecture:** Add a focused initial positions loader and three tab-specific server partials while preserving the existing full snapshot helper for strategy-record consumers. The browser loads and caches tab partials independently, and refresh checks use the focused positions response instead of the full four-tab snapshot. Reuse one HTTP connection within a Deepcoin client and remove duplicate TPSL reads where the same focused request already has the evidence.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, httpx, vanilla JavaScript, pytest, Node-based JavaScript smoke harnesses.

---

### Task 1: Lock the focused server-call contract with failing tests

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`

**Step 1: Write failing route tests**

Add clients that record calls to `list_positions`, `list_open_orders`,
`list_order_history`, `list_trigger_orders_pending`,
`list_trigger_order_history`, and `list_position_history`.

Assert:

- `GET /positions-panel?initial=positions` calls only `list_positions` and,
  when a live position exists, one pending-trigger read for its instrument.
- `GET /positions-panel/tabs/open-orders` calls only the open-order and pending
  trigger methods.
- `GET /positions-panel/tabs/order-history` calls only regular and trigger
  history methods.
- `GET /positions-panel/tabs/position-history` calls only position history.

**Step 2: Write failing browser asset assertions**

Assert that:

- the initial request uses `/positions-panel?initial=positions`;
- clicking a tab calls a dedicated lazy-tab loader;
- a loaded tab is not fetched twice without invalidation;
- the positions refresh check uses the focused initial route.

**Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_web_page_render.py -k "positions_panel_initial or positions_panel_tab" \
  tests/test_web_assets_smoke.py -k "lazy_exchange_position"
```

Expected: failures because the focused routes and browser loader do not exist.

**Step 4: Commit tests**

```bash
git add tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "test: specify lazy positions loading"
```

### Task 2: Implement focused server loaders and partial routes

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `src/telegram_kol_research/templates/_exchange_position_macros.html`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Create: `src/telegram_kol_research/templates/_exchange_position_tab.html`
- Test: `tests/test_web_page_render.py`

**Step 1: Extract reusable card macros**

Move the existing card-rendering macros unchanged into
`_exchange_position_macros.html`. Import them from the full panel and the new
tab partial.

**Step 2: Add focused loaders**

Add:

```python
def _load_exchange_live_snapshot(...): ...
def _load_exchange_tab_snapshot(..., tab: str, order_limit: int = 20): ...
```

The live loader calls `_load_deepcoin_live_position_rows` only. The tab loader
uses a strict tab allowlist and calls only the methods needed by that tab.
Existing attribution and exact binding helpers remain unchanged.

**Step 3: Add focused routes**

Keep the legacy `/positions-panel` behavior when `initial` is absent for
existing internal consumers and tests. When `initial=positions`, return the
shell populated only with live positions and mark the other panels lazy.

Add:

```python
@app.get("/positions-panel/tabs/{tab_name}")
def positions_panel_tab_partial(...): ...
```

Reject unsupported tabs with HTTP 404.

**Step 4: Run focused server tests and verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_web_page_render.py -k "positions_panel_initial or positions_panel_tab"
```

Expected: pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_position_macros.html \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/templates/_exchange_position_tab.html \
  tests/test_web_page_render.py
git commit -m "perf: split exchange position tab reads"
```

### Task 3: Implement browser lazy loading and bounded refresh

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Add tab loading state**

Track one promise per tab and use the panel's `data-exchange-tab-loaded`
attribute as the committed state. Do not start a second request while the same
tab is loading.

**Step 2: Load on selection**

After selecting a tab, fetch
`/positions-panel/tabs/${encodeURIComponent(tab)}`, parse the matching
`data-exchange-position-panel`, replace only that panel, restore the selected
list/grouped view, and rebind position actions if applicable.

Render tab-local loading, retry, and error notices without replacing already
loaded live positions.

**Step 3: Use the focused initial route**

Change both first load and live-position change checking to
`/positions-panel?initial=positions`. Preserve the existing non-disruptive
"检测到新的持仓数据" update control.

**Step 4: Run JavaScript smoke tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_web_assets_smoke.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  tests/test_web_assets_smoke.py
git commit -m "perf: lazy load exchange position tabs"
```

### Task 4: Reuse Deepcoin HTTP connections safely

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `tests/test_deepcoin_client.py`

**Step 1: Write a failing lifecycle test**

Create a fake `httpx.Client` factory and assert that multiple reads through one
`DeepcoinRestClient` use one client, while `close()` releases it exactly once.
Assert that externally injected clients are not closed by the wrapper.

**Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest -q tests/test_deepcoin_client.py -k "reuses_http_connection"
```

Expected: fail because the client currently creates and closes a new
`httpx.Client` inside every request.

**Step 3: Implement explicit client lifecycle**

Make `DeepcoinRestClient` own a lazily created persistent client and expose an
idempotent `close()` plus context-manager methods. Register the production web
client lifecycle with the FastAPI lifespan. Preserve injected-client ownership
semantics and write-path outcome handling.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_deepcoin_client.py tests/test_web_app.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py tests/test_deepcoin_client.py
git commit -m "perf: reuse Deepcoin HTTP connections"
```

### Task 5: Regression, review, and production verification

**Files:**
- Modify if needed: focused implementation and test files only

**Step 1: Run the relevant suite**

Run:

```bash
uv run pytest -q \
  tests/test_deepcoin_client.py \
  tests/test_web_app.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  tests/test_position_tpsl_display.py
```

Expected: pass.

**Step 2: Run static checks**

Run:

```bash
git diff --check
uv run python -m compileall -q src/telegram_kol_research
```

Expected: no output or errors.

**Step 3: Review the complete change**

Review from the design commit through the current HEAD, with particular focus
on:

- live data never coming from a stale history cache;
- mutation paths remaining unchanged;
- tab requests not multiplying Deepcoin calls;
- legacy strategy-record snapshot behavior remaining intact;
- unrelated dirty worktree files remaining untouched.

Resolve every critical or important finding and rerun focused tests.

**Step 4: Push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 5: Prove a safe deployment window**

Before restart, inspect current strategy operations and service state using
read-only server checks. If a time-sensitive strategy operation is active, do
not deploy; record the exact pending verification.

**Step 6: Deploy and verify**

When safe:

```bash
./scripts/server_git_update.sh
```

Verify:

- deployed SHA equals the pushed SHA;
- `telegram-kol.service` is active;
- initial focused route returns 200;
- no exchange-history methods run on the initial route;
- production initial response is below 50 KB;
- three measured initial requests meet the latency target;
- lazy tabs return correct counts and existing exact attribution;
- no exchange write or notification was produced by verification.

## Deployment Status — 2026-07-30

Local implementation and review completed with 317 focused tests passing,
static compilation passing, and no remaining Critical or Important review
finding. The reviewed branch was pushed to GitHub.

The later read-only safe-window check classified the previously ambiguous
durable states without changing them:

- the historical `partial_failed` and `recovery_required` batches had not
  changed since July 21–23;
- there was no `planned`, `executing`, `reconciling`, or `submit_unknown`
  management batch;
- the only raw message in the preceding 30 minutes had been authoritatively
  classified as `非策略` with automation `skipped`;
- there was no execution event or management-batch update in the preceding
  30 minutes.

Production was then fast-forwarded and restarted through
`scripts/server_git_update.sh`. The deployed SHA was
`84bc61e01e9eb85c5b180cba47274b027a632384`, and
`telegram-kol.service` returned to `active/running`.

Read-only production verification returned:

- focused initial route: HTTP 200, 11,667 bytes;
- three steady-state initial requests: 0.291 s, 0.292 s, and 0.279 s;
- open-orders tab: HTTP 200 in 0.520 s;
- order-history tab: HTTP 200 in 0.840 s;
- position-history tab: HTTP 200 in 2.368 s.

The first request immediately after restart took 4.973 s while the process and
upstream connection path were cold. All subsequent measured initial requests
met the sub-two-second target. Verification performed no exchange write,
notification, or trading-setting change.
