# Versioned Protection Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recover an exact, time-bounded management instruction after temporary Deepcoin TPSL visibility loss while retaining immutable protection history.

**Architecture:** Add append-only protection revisions and preflight observations. Only the exact-ledger-ID visibility failure enters a five-minute retry lane; every retry rebuilds ownership, position, and protection evidence before reusing the same idempotency batch.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI/Jinja, pytest, Deepcoin REST client.

---

### Task 1: Persist immutable revision and observation records

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/protection_revisions.py`
- Test: `tests/test_protection_revisions.py`

1. Write tests proving one exact position has one active revision, a replacement supersedes rather than overwrites its predecessor, and observations are append-only.
2. Run `pytest tests/test_protection_revisions.py -v`; expect failure because the model/helper does not exist.
3. Add revision and snapshot-observation models, compatibility migration, unique partial active-revision index, and pure repository helpers.
4. Run the focused test; expect pass.
5. Commit: `feat: record versioned position protection`.

### Task 2: Capture Deepcoin pending-TPSL completeness evidence

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_deepcoin_client.py`
- Test: `tests/test_execution_bindings.py`

1. Add failing tests for response count/ID digest, invalid schema, endpoint error, and unknown pagination metadata.
2. Implement a redacted pending-TPSL observation that preserves exact IDs only for already-owned protection, response structure, errors, and apparent pagination/cap warning.
3. Make snapshot loading return observations without treating an incomplete response as proof of absent protection.
4. Run focused tests and commit: `feat: capture protection snapshot evidence`.

### Task 3: Add bounded recovery planning

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_planner.py`

1. Add failing tests for a verified-ledger ID missing once then visible, expiry after five minutes, and non-retryable ownership/price/size conflicts.
2. Introduce retry metadata on batches: first failure, expiry, next attempt, attempts, and last observation.
3. Permit replacement of an empty blocked batch only for the exact temporary-visibility reason; retain its source message, idempotency fingerprint, fraction, and partial policy.
4. Ensure visibility must include every expected active revision/ledger order ID and revalidate exact position economics.
5. Run focused tests and commit: `feat: retry temporary protection visibility failures`.

### Task 4: Drive retry scheduling and post-replacement visibility

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_management_executor.py`

1. Add failing worker tests for 5/15/30/60/120-second cadence, restart-safe due-time claiming, and one submission maximum.
2. Implement a recovery lane that only claims due, unexpired temporary-visibility batches.
3. Create a protection revision after each TPSL replacement; mark it visible only after fresh exact-ID observation, leaving it `replacing` otherwise.
4. On expiry, terminalize the recovery attempt and create an actionable alert without exchange mutation.
5. Run tests and commit: `feat: recover management after TPSL visibility delay`.

### Task 5: Expose lineage and recovery state

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Modify: `src/telegram_kol_research/templates/_strategy_detail.html`
- Test: `tests/test_web_queries.py`
- Test: `tests/test_web_page_render.py`

1. Add failing read-model/render tests for initial/replacement protection versions, attempts, remaining window, and terminal refusal reason.
2. Render only redacted IDs/prices/statuses and link each version to its source management message.
3. Run focused UI tests and commit: `feat: show protection history and recovery status`.

### Task 6: Regression and production verification

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_management_executor.py`

1. Run focused tests, then the full local suite.
2. Document server-side read-only verification of expected order IDs, observation history, retry window, and no duplicate close submission.
3. Request review, commit documentation, push the reviewed branch, deploy through `scripts/server_git_update.ps1`, and observe first natural recovery in shadow mode before live activation.
