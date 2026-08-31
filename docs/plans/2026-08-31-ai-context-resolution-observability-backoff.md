# AI Context Resolution Observability and Network Backoff Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist exact context-resolution cost/trigger telemetry and replace immediate network retries with durable conservative backoff without changing decisions.

**Architecture:** Five nullable columns extend the existing attempt row and are written only by context-resolution persistence. Network errors move the same durable row to `retry_pending`; the existing lifecycle worker later performs the bounded second request under a provider-keyed in-process circuit.

**Tech Stack:** Python, SQLAlchemy, SQLite compatibility migrations, httpx, pytest, systemd immutable release tooling.

---

### Task 1: Add nullable observability schema

**Files:** `src/telegram_kol_research/models.py`, `src/telegram_kol_research/db.py`, `tests/test_db_migrations.py`

1. Add RED tests proving all five columns migrate additively and legacy rows remain unchanged with null values.
2. Run the exact migration tests and confirm they fail because the columns do not exist.
3. Add nullable model columns and exact `ALTER TABLE ... ADD COLUMN` compatibility entries.
4. Run the migration tests to GREEN.

### Task 2: Persist triggers, phase, usage and component bytes

**Files:** `src/telegram_kol_research/context_resolution.py`, `src/telegram_kol_research/authoritative_recognition.py`, `tests/test_context_resolution.py`, `tests/test_authoritative_recognition.py`

1. Add RED tests for ordered trigger persistence, initial/reanalysis phase, raw provider usage, explicit unavailable usage, component byte keys and unchanged behavior when telemetry is null.
2. Run the exact tests and retain the expected failures.
3. Add a provider-result envelope, canonical byte accounting and nullable telemetry writes. Pass already-computed triggers and phase to the resolver without adding them to the prompt.
4. Run focused tests to GREEN and run existing context/authority regression modules.
5. Write rollback and RED/GREEN evidence to `docs/ai-context-resolution-optimization-status.md`.
6. Stage explicit Change A paths, review the exact base diff, and create commit A.

### Task 3: Add durable network retry and circuit

**Files:** `src/telegram_kol_research/context_resolution.py`, `src/telegram_kol_research/context_resolution_worker.py`, `tests/test_context_resolution.py`, `tests/test_context_resolution_worker.py`

1. Add RED tests for exponential `next_attempt_at`, isolated network failure followed by a successful second request within the scheduling window, unchanged decision, consecutive failures opening the circuit, one half-open admission, rescheduling without counter increments and unknown remaining fail-closed.
2. Run exact RED tests and confirm failures are due to immediate retry/current claim behavior.
3. Implement conservative environment-bounded retry policy, provider-keyed circuit state and phase-aware worker handling using existing `retry_pending` rows.
4. Run focused tests to GREEN and run all context/authority/worker regression modules.

### Task 4: Final verification and commit B

**Files:** production/test files from Task 3 and `docs/ai-context-resolution-optimization-status.md`

1. Run `git diff --check`, compilation and focused tests.
2. Run the repository full suite once after the last production-code change.
3. Perform a separate exact-base review for commit B and resolve all findings before committing.
4. Update the independent status file with final local evidence, exact SHAs and rollback.
5. Stage explicit Change B/status paths and create commit B.
6. Push exact HEAD and verify it equals `origin/codex/deepcoin-auto-trading-v1`.

### Task 5: Immutable deployment and production verification

1. Rehearse additive migration on a fresh production database copy; preserve backup, `quick_check`, affected/critical counts and rollback evidence.
2. Create exact action manifests, fresh immutable stage and receipt. Run no root import/execute of release code after receipt.
3. Rerun installer, compare the four exact unit-file pairs by SHA-256, verify environment release paths and `systemd-analyze verify`.
4. Construct and consume the bound activation authorization for web/monitor/ingest/worker, immutable source mode and rollback `6e2321cecbb3adf61d7a5972d391e662d4aea300`.
5. Verify identities, loops, entry unfrozen, auto trade enabled, one complete healthy monitor cycle and unchanged BTC trigger orders/lifecycle/binding.
6. Observe at least one new context attempt. If none occurs in the bounded window, record insufficient live sample without claiming the new telemetry live-verified.
