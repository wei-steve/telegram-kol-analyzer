# Message AI Result Display Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the loaded group-message AI result presentation as compact, truthful chips with layered details, DOM-only filters, and loaded-row statistics.

**Architecture:** Reuse the existing bulk-loaded records and add four read-only projections without adding a query. Render all semantic states and filter metadata in Jinja, then let plain JavaScript filter and summarize the currently loaded cards; CSS provides hierarchy and risk tones.

**Tech Stack:** Python, SQLAlchemy projection code, Jinja2, vanilla JavaScript, CSS, pytest/FastAPI TestClient.

---

### Task 1: Read-only display projections

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Test: `tests/test_web_page_render.py`

1. Add failing render assertions for raw recognition result, raw lifecycle event type, candidate total, and the three context shadow fields.
2. Run the focused tests and confirm they fail because the fields are absent.
3. Project only existing values; preserve missing values as `None`.
4. Re-run the focused tests and confirm they pass.

### Task 2: Chips and layered details

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`

1. Add failing render tests for classification, confidence tones, runtime/image/context states, three candidate outcomes, shadow semantics, missing-value degradation, and retained detail fields.
2. Confirm the tests fail for missing markup.
3. Add the minimal Jinja markup and CSS needed to satisfy the approved semantics.
4. Re-run the focused render tests.

### Task 3: DOM filters and loaded-row statistics

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Test: `tests/test_web_assets_smoke.py`

1. Add failing asset tests for mutually exclusive filters, loaded-row statistics, no URL persistence, and recomputation after history append.
2. Confirm the tests fail for missing functions and calls.
3. Implement DOM-only filter state, statistics, binding, and post-append refresh.
4. Re-run the asset smoke tests.

### Task 4: Candidate verification and deployment

**Files:**
- Modify after deployment: `docs/ai-context-resolution-optimization-status.md`

1. Run focused tests and `git diff --check`.
2. Run exactly one complete pytest suite for the final production-code candidate.
3. Review the exact diff, commit explicit paths, and push to `codex/deepcoin-auto-trading-v1`.
4. Read all three `/api/runtime/deployment-identity` endpoints and validate immutable release evidence; use the measured web SHA as rollback.
5. Stage and activate only the web role with `schema_changed=false`.
6. Perform L1 observation, record bounded evidence, update `current_runtime_role_shas`, commit and push the documentation update.
