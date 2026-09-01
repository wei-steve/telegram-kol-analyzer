# Main Recognition Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add nullable, attempt-grained main-recognition provider usage and request-component byte telemetry without changing provider input, prompts, recognition decisions, retries, or any trading path.

**Architecture:** Measure the exact payload object already passed to the MiMo provider and retain the provider's unmodified `usage` object as side-channel metadata. Carry that metadata through the existing v1/v2 audit calls into new nullable columns on `mimo_recognition_attempts`; no existing reader or decision object consumes those columns. Preserve existing v1/v2 attempt-row semantics, including v1's aggregate audit row and v2's one-row-per-request retries.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite compatibility migrations, pytest, existing immutable stage/activation helpers.

---

### Task 1: RED tests for additive schema and attempt persistence

**Files:**
- Modify: `tests/test_db_migrations.py`
- Modify: `tests/test_mimo_recognition_runs.py`

1. Add a legacy-table migration test for nullable `attempt_phase`, `provider_request_count`, `provider_usage_json`, and `request_component_bytes_json` columns on `mimo_recognition_attempts`.
2. Add persistence tests proving returned usage is preserved verbatim, missing usage is explicit unavailable, and callers omitting all new arguments retain the old view/decision behavior.
3. Run the exact tests and observe failure because the columns and arguments do not exist.

### Task 2: GREEN schema and narrow persistence API

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/mimo_recognition_runs.py`

1. Add the four nullable model columns and exact SQLite `ALTER TABLE ... ADD COLUMN` compatibility statements.
2. Extend only `record_mimo_attempt()` with optional telemetry arguments and canonical JSON storage; keep `MimoRecognitionAttemptView` and every existing reader unchanged.
3. Run the exact RED tests until GREEN.

### Task 3: RED/GREEN exact provider side-channel telemetry

**Files:**
- Modify: `tests/test_recognition_experiments.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`

1. Add tests that capture the exact `httpx.post(json=payload)` object before and after instrumentation and require canonical byte equality and identical parsed/projection decisions.
2. Require raw provider usage or explicit `provider_usage_not_returned`, exact provider request count and v1/v2 phase, and canonical UTF-8 component bytes for system prompt, current message text, image evidence, direct reply, authoritative context, and structural/other remainder.
3. Require retries to retain separate v2 rows while the v1 aggregate row records the actual internal request count, without changing old attempt counts or selection semantics.
4. Implement metadata on the existing provider result/exception side channel and pass it only to `record_mimo_attempt()`.
5. Run focused recognition, migration, persistence and authoritative-decision tests.

### Task 4: Review, full verification and push

**Files:**
- Modify: `docs/ai-context-resolution-optimization-status.md`

1. Confirm no new telemetry column is read outside tests/analysis and no payload/prompt/criterion/settings diff exists.
2. Request independent exact-base code review and resolve all Critical/Important findings with focused RED/GREEN.
3. Run one final full suite after the last production-code edit.
4. Update the status checkpoint, commit explicit paths, and push the exact candidate to `codex/deepcoin-auto-trading-v1`.

### Task 5: Separate L3 schema step and runtime-only deployment

1. Create and verify the established SQLite backup; add only the four nullable columns in one `BEGIN IMMEDIATE` transaction with legacy NULL and critical-count proofs.
2. Take a fresh read-only Deepcoin window snapshot; fail closed on incomplete state or insufficient protection.
3. Stage exact pushed HEAD with `schema_changed=false`, install candidate monitor env/units, and run the exact filename/hash/env/static gates.
4. Activate web/monitor/ingest/worker with rollback `3205b074642436ed0f6aa35fefef7941a4f3f62f`, then immediately remove the exact freeze line and restart worker, web, ingest in that order.
5. Verify runtime identity, health, monitor, exchange state, and a bounded L1 natural-traffic window. Report zero samples honestly and also read any naturally produced shadow telemetry without affecting its authority.
