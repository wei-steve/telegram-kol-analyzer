# Instant Position Snapshot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render the exchange position panel immediately from the last successful persisted snapshot while refreshing Deepcoin in the background without weakening live trading checks.

**Architecture:** Add a thread-safe, JSON-backed snapshot store for display-only live position data. The focused position route reads that store, recomputes attribution from the current database, and schedules a single background refresh when needed; a bounded browser retry loop swaps in the refreshed version. No mutation path reads the store.

**Tech Stack:** Python 3.12, FastAPI/Starlette BackgroundTasks, Jinja2, standard-library JSON/threading/pathlib, vanilla JavaScript, pytest.

---

### Task 1: Snapshot store

**Files:**
- Create: `src/telegram_kol_research/live_position_snapshot.py`
- Create: `tests/test_live_position_snapshot.py`

**Step 1: Write failing serialization and persistence tests**

Cover:

- a snapshot containing nested lists and timezone-aware `datetime` values
  survives save/load;
- save writes a version and capture timestamp;
- a corrupt file returns an empty store instead of breaking startup;
- a failed refresh preserves the last successful payload;
- `begin_refresh()` grants only one caller until success/failure finishes;
- callers receive deep copies so template annotation cannot mutate the cache.

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_live_position_snapshot.py -q
```

Expected: collection/import failure because the module does not exist.

**Step 3: Implement the minimal store**

Implement:

```python
@dataclass(frozen=True)
class LivePositionSnapshot:
    payload: dict[str, Any]
    captured_at: datetime
    version: str
    last_error: str | None = None

class LivePositionSnapshotStore:
    def read(self) -> LivePositionSnapshot | None: ...
    def begin_refresh(self) -> bool: ...
    def finish_success(self, payload, *, captured_at) -> LivePositionSnapshot: ...
    def finish_failure(self, error: str) -> None: ...
```

Use a lock, `copy.deepcopy`, explicit datetime JSON tags, a same-directory
temporary file, and `Path.replace()`.

**Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_live_position_snapshot.py -q
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/live_position_snapshot.py tests/test_live_position_snapshot.py
git commit -m "feat: persist live position display snapshots"
```

### Task 2: Focused route cache and single-flight refresh

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing route tests**

Add tests proving:

- a cached snapshot returns positions without calling the Deepcoin factory;
- local attribution is recomputed on each cached response;
- a fresh cache schedules no refresh;
- a stale cache schedules one background refresh across concurrent requests;
- a successful background refresh replaces the cache;
- refresh failure keeps the prior cache and exposes an error state;
- no cache uses the existing synchronous fallback and persists it;
- an empty successful exchange snapshot replaces an older non-empty snapshot;
- tab/history routes and every mutation endpoint remain independent of the cache.

Inject a temporary `live_position_snapshot_path` and a controllable
`position_snapshot_now_provider` through `create_web_app`.

**Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_web_app.py \
  tests/test_web_page_render.py \
  -k "position_snapshot or positions_panel" -q
```

Expected: failures for missing app state, metadata and background refresh.

**Step 3: Implement the minimal route integration**

- Create/load the store during `create_web_app`.
- Add a refresh helper that owns the single-flight lifecycle.
- Schedule a startup refresh with `asyncio.to_thread` without blocking startup.
- Make `build_initial_positions_panel_context()` consume cached exchange data,
  recompute attribution, and return snapshot metadata.
- Add `BackgroundTasks` to the focused route and schedule refresh only when
  age exceeds five seconds.
- Keep the synchronous no-cache fallback.
- Never use the cache in full history tabs or write endpoints.

**Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_app.py tests/test_web_page_render.py
git commit -m "perf: serve live positions from persisted snapshots"
```

### Task 3: Snapshot status UI and bounded automatic refresh

**Files:**
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing UI contract tests**

Require:

- snapshot version/state/captured time data attributes;
- visible current/refreshing/stale/error copy;
- no wording that presents a stale snapshot as live;
- JavaScript retries only for non-current states;
- retry delays are bounded to 1, 2 and 4 seconds;
- retry timers are cancelled when the positions panel is replaced or hidden;
- existing selected tab/view state is retained.

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py tests/test_web_page_render.py \
  -k "snapshot or position" -q
```

Expected: failures for missing markup and retry controller.

**Step 3: Implement the UI**

- Render the snapshot status inside the position panel.
- Add compact neutral/warning/error styles.
- Start bounded retries after `commitPositionsPanel`.
- Reuse `checkPositionsPanelForChanges()` and its non-disruptive comparison.
- Stop after a current snapshot, three attempts, panel replacement, or view
  change.

**Step 4: Run tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/static/app.css \
  tests/test_web_assets_smoke.py \
  tests/test_web_page_render.py
git commit -m "feat: show and refresh position snapshot state"
```

### Task 4: Versioned static asset caching

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write a failing cache-header test**

Require versioned `/static/app.js?v=<current>` and
`/static/app.css?v=<current>` responses to use:

```text
Cache-Control: public, max-age=31536000, immutable
```

An unversioned or mismatched request must remain revalidatable.

**Step 2: Run test and verify RED**

Run:

```bash
uv run pytest tests/test_web_app.py -k "static_asset_cache" -q
```

Expected: current StaticFiles response lacks the immutable policy.

**Step 3: Implement minimal middleware**

Add a narrowly scoped HTTP middleware that sets immutable caching only when
the path is `/static/app.js` or `/static/app.css` and the `v` query exactly
matches `app.state.asset_version`.

**Step 4: Run test and verify GREEN**

Run the command from Step 2. Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_app.py
git commit -m "perf: cache versioned workbench assets"
```

### Task 5: Regression, review and production rollout

**Files:**
- Modify: `docs/plans/2026-07-31-instant-position-snapshot.md`

**Step 1: Run regression**

```bash
uv run pytest \
  tests/test_live_position_snapshot.py \
  tests/test_deepcoin_client.py \
  tests/test_web_app.py \
  tests/test_web_assets_smoke.py \
  tests/test_web_page_render.py -q
uv run python -m compileall -q src tests
git diff --check
```

Expected: all pass.

**Step 2: Request code review**

Review the entire diff against the design. Resolve every Critical or Important
finding and rerun regression.

**Step 3: Push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Prove a safe deployment window**

Read-only checks must show:

- no management batch in `planned`, `executing`, `reconciling` or
  `submit_unknown`;
- no recent time-sensitive execution event or batch update;
- service currently active.

Do not deploy if this cannot be proven.

**Step 5: Deploy and prewarm**

```bash
./scripts/server_git_update.sh
```

Then make one read-only localhost request to
`/positions-panel?initial=positions` to populate the persistent snapshot.

**Step 6: Verify**

- deployed SHA equals pushed SHA;
- service is active;
- cache file exists with no credentials or secrets;
- first cached focused route is HTTP 200 and below 300 ms server-side;
- a new browser navigation to `/?view=positions` renders below one second when
  network conditions permit;
- stale/error metadata is accurate;
- background refresh changes the snapshot version;
- no exchange write, notification or trading setting changes during
  verification.

Record exact timings and deployment status in this plan, commit the
documentation-only update, push it, and update production only after another
safe-window check.

## Production rollout result

Completed on 2026-07-31:

- Production commit: `84a8e3458512a2efc8864788a181aa7631fb806c`.
- The initial `/?view=positions` document now server-renders the persisted
  position snapshot. It does not wait for a Deepcoin refresh.
- A stale focused panel response completed in `0.2087s`; three concurrent
  reads during its background refresh completed in `0.1067–0.1216s`.
- The background refresh changed the persisted snapshot version successfully.
- Warm localhost server-rendered position documents completed in
  `0.0337–0.0438s` (one additional sample was `0.4311s`). A request made while
  the service startup refresh was still active took `2.7345s`, without losing
  the persisted position cards.
- Chrome authenticated reloads commonly rendered the two position cards in
  `0.817–1.201s`. Two network/browser outliers remained (`6.939s` and
  `10.020s`); the latter occurred immediately after deployment and did not
  render the position fragment. These outliers are outside the now-fast
  application data path and should be investigated separately at the
  browser/authentication/network layer if they remain user-visible.
- Versioned JavaScript is served with
  `Cache-Control: public, max-age=31536000, immutable`.
- The persisted cache contained no credential marker, the service was active,
  and post-deployment logs contained no `Traceback`, `ERROR`, `Exception`, or
  `Failed` markers.
- Regression: `322 passed`; compile, JavaScript syntax, and whitespace checks
  also passed.
