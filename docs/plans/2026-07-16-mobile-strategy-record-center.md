# Mobile Strategy Record Center Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the current group-first Web workbench with a mobile-first strategy record center that shows recognition, lifecycle, real position, management execution, and attribution in one traceable read-only view.

**Architecture:** Add a batched, read-only strategy-record projection over the existing message, candidate, lifecycle, binding, execution, management, and recognition tables. Enrich that projection once per request with the existing Deepcoin snapshot and attribution helpers, then render lightweight list and dedicated detail routes with the current Jinja/CSS/vanilla-JavaScript stack. Preserve MiMo authority and every existing mutation boundary; the new projection never writes operational state.

**Tech Stack:** Python 3.14, SQLAlchemy, FastAPI, Jinja2, vanilla JavaScript, CSS, pytest, FastAPI TestClient.

---

## Working Rules

- Execute this plan in an isolated worktree when Git permissions allow it.
- Use `@superpowers:test-driven-development` for every behavior change.
- Use `@superpowers:verification-before-completion` before claiming a task or the feature complete.
- Keep Deepcoin interactions read-only during Web verification. Never submit, bind, close, cancel, or change TPSL while testing this redesign.
- Do not change recognition authority, lifecycle transitions, order construction, management planning, reconciliation, or live-action confirmation logic.
- Commit only the files named in each task. The audit screenshots under `artifacts/web-audit-2026-07-16/` are evidence, not implementation inputs.

### Task 1: Define The Strategy Record Projection And Attention Ordering

**Files:**
- Create: `src/telegram_kol_research/strategy_records.py`
- Create: `tests/test_strategy_records.py`
- Reference: `src/telegram_kol_research/models.py:35-184`
- Reference: `src/telegram_kol_research/models.py:379-668`
- Reference: `src/telegram_kol_research/models.py:782-861`
- Reference: `src/telegram_kol_research/web_queries.py:1283-1715`

**Step 1: Write failing projection tests**

Create database fixtures containing:

- a pending strategy with a clean authoritative recognition decision;
- an entered lifecycle with a live binding and no stop loss;
- an entered lifecycle without a binding;
- a lifecycle with `disagreement_severity="critical"`;
- a strategy with a failed execution event.

Assert the stable public shape:

```python
rows = load_strategy_record_summaries(
    session_factory,
    group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
    filter_name="needs_attention",
    limit=50,
    now=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
)

assert rows[0].keys() >= {
    "lifecycle_id", "chat_id", "group_name", "message_id",
    "symbol", "side", "lifecycle_state", "recognition_state",
    "execution_state", "attribution_state", "attention",
    "latest_changed_at", "detail_href",
}
assert rows[0]["attention"] == {
    "severity": "critical",
    "code": "missing_stop",
    "label": "真实持仓缺少止损",
}
assert all(row["attention"] is not None for row in rows)
assert [row["attention"]["severity"] for row in rows] == sorted(
    [row["attention"]["severity"] for row in rows],
    key={"critical": 0, "warning": 1, "review": 2}.get,
)
```

Also assert that `filter_name="all"` includes normal records and that `chat_id=20` returns only that group.

**Step 2: Run the tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_strategy_records.py -q
```

Expected: FAIL because `telegram_kol_research.strategy_records` does not exist.

**Step 3: Implement the minimal batched loader**

Create immutable view helpers and one public summary loader:

```python
ATTENTION_SEVERITY_RANK = {"critical": 0, "warning": 1, "review": 2}
FAILED_EXECUTION_STATUSES = {"failed", "rejected", "error"}
LIVE_BINDING_STATUSES = {"open", "active"}


def load_strategy_record_summaries(
    session_factory,
    *,
    group_labels_by_chat_id: dict[int, str],
    filter_name: str = "needs_attention",
    chat_id: int | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    ...
```

Use a bounded lifecycle query and batched `IN` queries keyed by lifecycle ID, raw message ID, binding ID, and strategy instance ID. Do not run a query inside the per-record formatting loop.

Use these local-only attention codes initially:

```python
ATTENTION_LABELS = {
    "recognition_failed": ("critical", "AI识别失败"),
    "recognition_disagreement": ("review", "AI识别存在关键分歧"),
    "entered_without_binding": ("critical", "策略已入场但没有唯一真实仓位"),
    "missing_stop": ("critical", "真实持仓缺少止损"),
    "execution_failed": ("critical", "交易执行失败"),
    "management_unconfirmed": ("warning", "仓位管理尚未确认交易所结果"),
}
```

When more than one condition applies, keep the highest severity as `attention` and expose all conditions as `attention_reasons`. Use the most recent of lifecycle, recognition, binding, execution-event, and management-batch timestamps for `latest_changed_at`.

Do not classify exchange attribution or TPSL mismatch yet; Task 2 adds those only when a current exchange snapshot exists.

**Step 4: Run focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_strategy_records.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py tests/test_strategy_records.py
git commit -m "feat: add strategy record projection"
```

### Task 2: Enrich Records With Current Exchange And Attribution Evidence

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_app.py:846-1120`
- Test: `tests/test_strategy_records.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing exchange-enrichment tests**

Cover four cases:

1. one uniquely bound current position;
2. one exchange position with no strategy attribution;
3. ambiguous attribution candidates;
4. Deepcoin unavailable.

Assert that unknown exchange state is never rendered as a confirmed zero:

```python
enriched = enrich_strategy_records_with_exchange(
    records,
    exchange_snapshot={"positions": [], "error": "exchange unavailable"},
)

assert enriched[0]["exchange_state"] == "unknown"
assert enriched[0]["real_position"] is None
assert enriched[0]["attention"]["code"] == "exchange_unavailable"
```

Assert that a unique matching `pos_id` produces `attribution_state="bound"`, while candidate or conflicting evidence produces `attribution_state="ambiguous"` or `"conflict"` with a visible reason.

**Step 2: Run focused tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_records.py \
  tests/test_web_page_render.py -q
```

Expected: FAIL because exchange enrichment is missing.

**Step 3: Implement a pure enrichment function**

Add:

```python
def enrich_strategy_records_with_exchange(
    records: list[dict[str, object]],
    *,
    exchange_snapshot: dict[str, object],
) -> list[dict[str, object]]:
    ...
```

Index exchange positions by `pos_id`, then use the already annotated `position["attribution"]` object produced by `_annotate_exchange_snapshot_attribution`. Never rematch exchange records independently in the projection.

Map states explicitly:

```python
if exchange_snapshot.get("error"):
    exchange_state = "unknown"
elif attribution_state == "bound":
    exchange_state = "confirmed"
elif attribution_state in {"candidate", "ambiguous"}:
    exchange_state = "unconfirmed"
elif attribution_state in {"conflict", "unassigned"}:
    exchange_state = "attention"
```

Add attention codes `exchange_unavailable`, `unattributed_position`, `attribution_ambiguous`, `attribution_conflict`, and `protection_mismatch`. Do not infer `protection_mismatch` unless the annotated exchange row contains concrete protection evidence.

**Step 4: Integrate one snapshot per request**

In `web_app.py`, reuse `build_positions_panel_context()` or extract its snapshot-building portion into a shared helper. The strategy list request must call Deepcoin at most once and must not call it from a per-card loop.

**Step 5: Run focused tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_records.py \
  tests/test_web_page_render.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py \
  src/telegram_kol_research/web_app.py \
  tests/test_strategy_records.py \
  tests/test_web_page_render.py
git commit -m "feat: attach exchange evidence to strategy records"
```

### Task 3: Build The Dedicated Strategy Detail Evidence Chain

**Files:**
- Modify: `src/telegram_kol_research/strategy_records.py`
- Test: `tests/test_strategy_records.py`

**Step 1: Write the failing detail test**

Persist one strategy with original message, media, candidate, recognition decision, lifecycle, binding, execution leg, execution events, management batch, and management leg. Assert:

```python
detail = load_strategy_record_detail(
    session_factory,
    lifecycle_id=lifecycle.id,
    group_labels_by_chat_id={10: "大镖客"},
)

assert detail["identity"]["lifecycle_id"] == lifecycle.id
assert detail["overview"]["authoritative_model"] == "mimo-v2.5"
assert [item["kind"] for item in detail["timeline"]] == [
    "message", "recognition", "strategy", "order", "fill", "management",
]
assert detail["execution"]["binding"]["pos_id"] == "pos-1"
assert detail["execution"]["management_batches"][0]["legs"][0]["pos_id"] == "pos-1"
assert detail["evidence"]["raw_message"]["text"] == "BTC 现价做多"
```

Add a 404 case for an unknown lifecycle ID and a partial-evidence case where binding or exchange evidence is absent.

**Step 2: Run the test and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_records.py::test_load_strategy_record_detail_builds_full_evidence_chain -q
```

Expected: FAIL because `load_strategy_record_detail` is missing.

**Step 3: Implement the detail loader**

Add:

```python
def load_strategy_record_detail(
    session_factory,
    *,
    lifecycle_id: int,
    group_labels_by_chat_id: dict[int, str],
) -> dict[str, object] | None:
    ...
```

Return four stable sections: `overview`, `timeline`, `execution`, and `evidence`. Sort timeline items by normalized UTC timestamp, then stable kind rank, then database ID. Include source identifiers in every event; never collapse entry, management, and exit messages into one generic message.

Parse JSON with a safe helper that returns an explicit parse-error marker rather than raising or hiding evidence. Redact known secret fields before rendering request/response payloads.

**Step 4: Run focused tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_strategy_records.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_records.py tests/test_strategy_records.py
git commit -m "feat: build strategy detail evidence chain"
```

### Task 4: Add Read-Only List And Detail Routes With Jinja Templates

**Files:**
- Create: `src/telegram_kol_research/templates/_strategy_record_list.html`
- Create: `src/telegram_kol_research/templates/strategy_record_detail.html`
- Create: `tests/test_web_strategy_records.py`
- Modify: `src/telegram_kol_research/web_app.py:2481-2665`
- Modify: `src/telegram_kol_research/templates/base.html`

**Step 1: Write failing route tests**

Test these routes:

```text
GET /strategy-records?filter=needs_attention&chat_id=&limit=50
GET /strategy-records/{lifecycle_id}
```

Assert list semantics:

```python
response = client.get("/strategy-records", params={"filter": "needs_attention"})
assert response.status_code == 200
assert 'data-strategy-record-list' in response.text
assert 'data-strategy-record-card' in response.text
assert 'data-attention-code="missing_stop"' in response.text
assert 'data-live-action' not in response.text
assert '市价平仓' not in response.text
```

Assert detail semantics:

```python
response = client.get(f"/strategy-records/{lifecycle.id}")
assert response.status_code == 200
assert 'data-strategy-record-detail' in response.text
assert 'data-strategy-detail-section="overview"' in response.text
assert 'data-strategy-detail-section="timeline"' in response.text
assert 'data-strategy-detail-section="execution"' in response.text
assert 'data-strategy-detail-section="evidence"' in response.text
```

Assert invalid filters return 422, unknown lifecycle IDs return 404, and `limit` stays between 1 and 100.

**Step 2: Run tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_web_strategy_records.py -q
```

Expected: FAIL with route 404 responses.

**Step 3: Implement the list route**

Add explicit validation:

```python
STRATEGY_RECORD_FILTERS = {
    "needs_attention", "all", "executing", "pending_entry", "finished"
}

@app.get("/strategy-records")
def strategy_record_list_partial(
    request: Request,
    filter: str = "needs_attention",
    chat_id: int | None = None,
    limit: int = 50,
):
    if filter not in STRATEGY_RECORD_FILTERS or not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="invalid strategy record query")
    ...
```

Return `_strategy_record_list.html`. Include `last_success_at`, independent service states, summary counts, applied filters, and group options in the context.

**Step 4: Implement the detail route and template**

Add:

```python
@app.get("/strategy-records/{lifecycle_id}")
def strategy_record_detail(request: Request, lifecycle_id: int):
    detail = load_strategy_record_detail(...)
    if detail is None:
        raise HTTPException(status_code=404, detail="strategy record not found")
    return templates.TemplateResponse(
        request,
        "strategy_record_detail.html",
        {"record": detail, "exchange": exchange_evidence},
    )
```

Use semantic `<main>`, `<header>`, `<nav aria-label>`, `<section aria-labelledby>`, `<ol>` for the timeline, `<dl>` for metrics, and text labels for every state. Keep any reused dangerous controls behind the existing exact-position validation and confirmation attributes; do not add a new mutation endpoint.

**Step 5: Run route tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/base.html \
  src/telegram_kol_research/templates/_strategy_record_list.html \
  src/telegram_kol_research/templates/strategy_record_detail.html \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py
git commit -m "feat: add strategy record list and detail routes"
```

### Task 5: Replace The Workbench Navigation And Make Strategy The Default

**Files:**
- Modify: `src/telegram_kol_research/templates/_workbench_nav.html`
- Modify: `src/telegram_kol_research/templates/index.html:5-420`
- Modify: `src/telegram_kol_research/static/app.js:12-20`
- Modify: `src/telegram_kol_research/static/app.js:1218-1528`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Update tests first**

Replace old navigation assertions with exactly five phone destinations:

```python
for view, label in (
    ("strategies", "策略"),
    ("positions", "持仓"),
    ("activity", "动态"),
    ("groups", "群组"),
    ("more", "更多"),
):
    assert f'data-workbench-view="{view}"' in mobile_nav
    assert label in mobile_nav

assert 'data-workbench-view="home"' not in mobile_nav
assert 'data-workbench-view="messages"' not in mobile_nav
assert 'data-workbench-view="management-batches"' not in mobile_nav
```

Assert `strategies` owns `aria-current="page"` on initial render and the lightweight root page still does not construct a Deepcoin client.

**Step 2: Run focused tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py -q
```

Expected: FAIL on the old `首页 / 持仓 / 策略 / 消息 / 更多` navigation.

**Step 3: Implement one navigation model**

Set:

```javascript
const WORKBENCH_VIEWS = ['strategies', 'positions', 'activity', 'groups', 'more'];
```

Make `strategies` the root default. Remove `home` from `workbenchLoadState`; add `activity` and `groups`. Do not reuse the current three-column `strategies` panel as the primary phone experience. Keep legacy group strategy/message partial routes available during migration, but route navigation through the new destinations.

The root `/` remains a lightweight shell. The initial list loads from `/strategy-records?filter=needs_attention` after first paint.

**Step 4: Move existing destinations without deleting capability**

- Move service health and risk counters into the strategy summary header.
- Keep current position partial under `positions`.
- Render existing message event data under `activity`.
- Render group rows and AI/auto-trade controls under `groups`.
- Keep management batches, settings, prompt center, profiles, execution page, and logs under `more` or strategy detail.

**Step 5: Run navigation tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/templates/_workbench_nav.html \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py
git commit -m "feat: make strategy records the primary workbench"
```

### Task 6: Add Mobile Filters, State Restoration, And Non-Disruptive Refresh

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/templates/_strategy_record_list.html`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_strategy_records.py`

**Step 1: Write failing static and render assertions**

Require these stable hooks:

```text
data-strategy-record-filter
data-strategy-group-filter
data-strategy-record-scroll
data-strategy-new-changes
data-strategy-record-retry
```

Assert JavaScript contains separate persisted keys and a request guard:

```javascript
telegram-workbench:strategy-filter
telegram-workbench:strategy-group
telegram-workbench:strategy-scroll
strategyRecordRequestId
```

Assert the fetch completion checks the request ID before replacing list content.

**Step 2: Run focused tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py -q
```

Expected: FAIL because filters and guards are absent.

**Step 3: Implement the list controller**

Add one controller with these responsibilities:

```javascript
let strategyRecordRequestId = 0;
let strategyRecordPendingChanges = 0;

async function loadStrategyRecords({ force = false, revealChanges = false } = {}) {
  const requestId = ++strategyRecordRequestId;
  const params = currentStrategyRecordParams();
  const response = await fetch(`/strategy-records?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  const html = await response.text();
  if (requestId !== strategyRecordRequestId) return;
  ...
}
```

On normal SSE or freshness events, increment the new-changes badge without replacing the list. Only explicit refresh, filter changes, or tapping the badge may replace the list. Save scroll position before entering a detail route and restore it after back navigation.

Use native `<select>` or the existing searchable overlay pattern for group filtering; do not create a second group-selection state that affects the `群组` destination.

**Step 4: Add failure-state behavior**

Preserve the last successful DOM on fetch failure. Add a visible stale/error notice with the last successful timestamp and a retry button. Never replace the list with an empty state merely because the request failed.

**Step 5: Run focused tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/templates/_strategy_record_list.html \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py
git commit -m "feat: preserve mobile strategy list context"
```

### Task 7: Implement The Mobile-First Visual System And Accessibility Contracts

**Files:**
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/templates/_strategy_record_list.html`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_web_strategy_records.py`

**Step 1: Write failing CSS-contract tests**

Assert presence of selectors and minimum interaction rules:

```python
assert ".strategy-record-card" in css
assert ".strategy-record-detail" in css
assert "min-height: 44px" in css
assert "env(safe-area-inset-bottom)" in css
assert "overflow-wrap: anywhere" in css
assert "@media (min-width: 761px)" in css
assert ":focus-visible" in css
```

Render a long Chinese message, four take profits, multiple orders, and multiple position IDs; assert the templates expose dedicated wrapping containers rather than one unbroken `<code>` row.

**Step 2: Run tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py -q
```

Expected: FAIL because the new styles do not exist.

**Step 3: Implement phone-first styles**

Start from the existing dark tokens and colors. Add:

- one-column cards below 761px;
- sticky compact summary and filters without covering content;
- 44px controls and safe-area bottom padding;
- narrow text labels for attention, recognition, execution, and attribution;
- reduced border density;
- `overflow-wrap: anywhere` for identifiers and message text;
- independent scroll only where it does not trap the whole phone page;
- visible `:focus-visible` outlines.

Do not encode state using color alone. Do not introduce emoji as functional icons or hand-built SVG assets.

**Step 4: Implement the desktop enhancement**

At 761px and above, render a bounded strategy list beside the selected detail or retain the dedicated detail route with a readable centered maximum width. Keep the same DOM meaning, filter model, and routes as phone.

**Step 5: Run focused tests and manual local browser checks**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py -q
```

Then verify with the in-app browser at 390x844 and 1440x900:

- needs-attention list;
- all-strategy filter;
- group filter;
- long card;
- detail overview;
- full timeline;
- execution evidence;
- raw evidence;
- back navigation and scroll restoration;
- stale and failed states.

Expected: no horizontal overflow, no covered content, and every primary control at least 44px.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/static/app.css \
  src/telegram_kol_research/templates/_strategy_record_list.html \
  src/telegram_kol_research/templates/strategy_record_detail.html \
  tests/test_web_assets_smoke.py \
  tests/test_web_strategy_records.py
git commit -m "feat: style mobile strategy record center"
```

### Task 8: Add Cross-Links And Protect Existing Live-Action Boundaries

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Modify: `src/telegram_kol_research/templates/_strategy_record_list.html`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`
- Test: `tests/test_web_strategy_records.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing cross-link tests**

Assert:

- a recognized message with a lifecycle links to `/strategy-records/{id}`;
- a bound position card links to its lifecycle record;
- a management batch in detail shows lifecycle ID, binding ID, position IDs, and leg statuses;
- a list card contains no close, bind, TPSL, or submit control;
- reused detail actions retain their existing confirmation attributes.

**Step 2: Run focused tests and verify failure**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py -q
```

Expected: FAIL because the cross-links are absent.

**Step 3: Add cross-links using authoritative IDs only**

Link only when a concrete lifecycle ID is present. Do not build links from symbol/side guesses. For unassigned or ambiguous positions, link to the position evidence and display the attribution state without guessing a strategy.

**Step 4: Run safety-focused tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py \
  tests/test_position_authority_lock.py \
  tests/test_position_attribution.py \
  tests/test_protection_attribution.py \
  tests/test_strategy_management_batches.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/templates/_messages.html \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/templates/_strategy_record_list.html \
  src/telegram_kol_research/templates/strategy_record_detail.html \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py
git commit -m "feat: link strategy records to source evidence"
```

### Task 9: Full Regression, Documentation, And Production Verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/context/telegram-deepcoin-auto-trading-context.md`
- Modify: `docs/runbook.md`
- Test: all relevant tests

**Step 1: Document the durable UI contract**

Record:

- strategy records are the primary phone landing view;
- all groups plus `需要处理` are the defaults;
- the projection is read-only and not a source of truth;
- the authority chain and Deepcoin unknown-state rule;
- the five primary destinations;
- the list/detail cross-link contract;
- server-only live-data verification requirements.

**Step 2: Run formatting and static checks**

```bash
git diff --check
node --check src/telegram_kol_research/static/app.js
```

Expected: both exit 0.

**Step 3: Run focused Web and safety suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_records.py \
  tests/test_web_strategy_records.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  tests/test_web_queries_dashboard.py \
  tests/test_position_authority_lock.py \
  tests/test_position_attribution.py \
  tests/test_protection_attribution.py \
  tests/test_strategy_management_batches.py -q
```

Expected: PASS.

**Step 4: Run the complete local suite**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: PASS, or exactly the recorded pre-existing baseline with no new failures. Do not waive a new Web, attribution, binding, management, or live-action failure.

**Step 5: Commit documentation**

```bash
git add docs/migration-handoff.md \
  docs/context/telegram-deepcoin-auto-trading-context.md \
  docs/runbook.md
git commit -m "docs: record strategy record workbench contract"
```

**Step 6: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: the branch updates successfully.

**Step 7: Deploy through the established server workflow**

From the local repository after push:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected server gates:

- server `HEAD` matches the pushed commit;
- editable package reinstall succeeds;
- `telegram-kol.service` is active;
- `GET /` and the strategy-record routes return HTTP 200;
- the served JavaScript and CSS contain the new stable markers.

**Step 8: Perform read-only production browser verification**

Verify at 390x844 first, then desktop:

- the default is all groups plus needs-attention;
- a real strategy opens its complete record;
- MiMo recognition, source message, binding, execution events, current position, TPSL, and management batch evidence agree;
- unassigned and ambiguous real positions remain visibly unconfirmed;
- Deepcoin failure preserves last-known data and never shows a confirmed zero;
- no trade mutation is submitted during verification.

**Step 9: Final production evidence**

Record the deployed commit, service state, route results, phone screenshots, desktop screenshot, and any intentionally deferred limitations in `docs/migration-handoff.md`.

Expected: the production evidence is sufficient to reproduce the verification without relying on chat history.

---

## Completion Gate

The feature is complete only when:

- all nine tasks are committed;
- local focused and complete tests meet the recorded baseline;
- phone and desktop layouts are visually verified;
- the production server is on the reviewed commit;
- a real strategy record demonstrates the full authority chain;
- no unsafe mutation occurred during verification;
- durable context documentation is updated.
