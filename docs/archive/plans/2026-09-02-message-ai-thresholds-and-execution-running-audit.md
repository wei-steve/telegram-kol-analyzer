# Message AI Thresholds And Execution-Running Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finish the approved Web-only AI display calibration, record the known deferred issues, deploy the exact reviewed release, and then audit the 28 stuck `execution_running` decisions without mutating production data.

**Architecture:** Keep all display classification in the existing `_messages.html` projection and keep label-drift comparison in `web_queries.py`; no recognition, context, candidate, execution, schema, or network behavior changes. Production investigation opens SQLite with `mode=ro` plus `query_only`, uses only the worker role's existing GET exchange snapshot, and records only aggregate or structured IDs/timestamps.

**Tech Stack:** Python 3.12, FastAPI/Jinja, vanilla JavaScript, SQLAlchemy/SQLite, pytest, immutable release deployment helpers.

---

### Task 1: Add RED display-threshold and drift tests

**Files:**
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_web_assets_smoke.py`

1. Add render cases for confidence `0.24`, `0.25`, `0.79`, `0.8`, and `None`.
2. Add a runtime fixture with `became_authoritative=false` and assert its chip stays visible and neutral while it does not independently produce `is-ai-warning`.
3. Assert the server-rendered statistics placeholder is exactly `统计中…` and contains no zero counts.
4. Add label-drift cases where confidence differs by less than `1e-9` and by more than `1e-9`.
5. Run the focused tests and require failures caused by the old thresholds, warning class, placeholder, and direct float comparison.

### Task 2: Implement the minimal display changes

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/web_queries.py`

1. Define low confidence as `<0.25` and medium confidence as `>=0.25 and <0.8`; preserve `None` as a separate neutral state.
2. Remove `runtime_not_authoritative` only from `warning_state`, retain its chip with neutral styling, and leave `image_not_sent` dangerous.
3. Replace the statistics numeric placeholder with `统计中…`; leave JavaScript calculation unchanged.
4. Compare non-null confidence snapshots with absolute tolerance `<1e-9`, while retaining strict equality for recognition result and lifecycle event type and exact `None` handling.
5. Run the focused tests until green.

### Task 3: Record deferred observations

**Files:**
- Modify: `docs/known-issues-and-deferred-work.md`

1. Merge the queue-mode stall-expiry notification gap into the existing message-pipeline visibility item.
2. Merge lifecycle IDs `1043`, `1044`, `1051`, and `1053` into the existing stale local protection/state projection item.
3. Record all 28 `execution_running` decisions, the authoritative-write rejection, and backlog-expiry refusal consequences.
4. Correct the historical boundary: explicit expiry classification began in commit `3eabde7c` on 2026-08-19.

### Task 4: Verify and deploy the Web-only release

**Files:**
- Modify after deployment: `docs/ai-context-resolution-optimization-status.md`

1. Run the complete pytest suite once on the final production-code candidate.
2. Review the diff, stage explicit paths only, commit, and push to `codex/deepcoin-auto-trading-v1`.
3. Query `/api/runtime/deployment-identity` on ports `8000`, `8001`, and `8002`; require verified artifacts and use the observed Web commit as rollback.
4. Create exact stage/activate manifests declaring Web-only scope and `schema_changed=false`; stage and activate as separate actions.
5. Verify the Web runtime identity and focused rendered behavior, then complete the L1 observation required by `AGENTS.md`.
6. Update `current_runtime_role_shas`, commit the deployment evidence, and push.

### Task 5: Run the read-only execution-running audit

**Files:**
- Create: `docs/2026-09-02-execution-running-read-only-audit.md`

1. Open production SQLite with `mode=ro`, `PRAGMA query_only=ON`, a short busy timeout, and no temporary tables.
2. Report the 28 rows by day and align timestamps only to repository-recorded deployment/restart windows.
3. Project authoritative result, event type, target lifecycle, message-time lifecycle state, and current lifecycle state without message content.
4. Obtain the complete exchange position/protection snapshot only through worker port `8002`; report incomplete reads as unknown.
5. Inventory claim-token values and actual transition/recovery/CLI paths in code.
6. Search for persisted refusal or failure evidence caused by `execution_running_decision_present`.
7. Align IDs `14278`, `14279`, and `14281`–`14286` to the documented September 1 activation/restart timestamps.
8. Commit only the report/status-document changes and push.
