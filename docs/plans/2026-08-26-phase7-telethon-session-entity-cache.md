# Phase 7 Telethon Session Entity Cache Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent Telethon's optional SQLite entity-cache writes from blocking
the ingest event loop while retaining the session durability required for login
and update recovery.

**Architecture:** Keep the existing Telethon `SQLiteSession` and single ingest
authority. Configure only its optional `save_entities` switch at the shared
client factory, and pin that behavior with a real SQLite lock regression test
plus an update-state persistence test.

**Tech Stack:** Python, Telethon 1.42, SQLite, pytest.

---

### Task 1: Add a failing factory and real-lock regression test

**Files:**
- Create: `tests/test_telegram_client_session.py`
- Read: `src/telegram_kol_research/telegram_client.py`

**Step 1: Write the failing test**

Create a real client with `create_telegram_client()` and a temporary session
path. Assert `client.session.save_entities is False`. Open a second SQLite
connection, start `BEGIN EXCLUSIVE`, and insert into the `entities` table so the
write lock is held. Call `client.session.process_entities()` with a synthetic
`telethon.tl.types.User`. Measure monotonic elapsed time and assert the call
returns well below the SQLite busy timeout. Close both connections and the
session in `finally` blocks.

**Step 2: Run the RED test**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_telegram_client_session.py::test_factory_disables_blocking_sqlite_entity_persistence
```

Expected: FAIL because the production factory currently leaves
`save_entities=True`.

**Step 3: Keep the failure evidence**

Record the exact failing assertion and do not modify production code until the
RED failure is observed.

### Task 2: Prove required session durability remains enabled

**Files:**
- Modify: `tests/test_telegram_client_session.py`

**Step 1: Add the durability test**

Use the factory session to call `set_update_state()` with a synthetic Telethon
`updates.State`, close the session, reopen the same `.session` file with
`SQLiteSession`, and assert the stored state values are present. Also assert the
reopened SQLite session remains a disk-backed session.

**Step 2: Run the focused test file**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_telegram_client_session.py
```

Expected before production change: the durability test passes and the factory
entity-cache contract remains RED.

### Task 3: Implement the minimal production change

**Files:**
- Modify: `src/telegram_kol_research/telegram_client.py`

**Step 1: Configure the constructed session**

Store the `TelegramClient(...)` result in a local variable, set
`client.session.save_entities = False`, and return the client. Do not change
connection settings, session type, auth behavior, or any caller.

**Step 2: Run the GREEN test**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_telegram_client_session.py
```

Expected: PASS, including real entity-table lock avoidance and update-state
durability.

**Step 3: Run directly related focused regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_telegram_client_session.py \
  tests/test_telegram_fetch.py \
  tests/test_runtime_role_selection.py \
  tests/test_runtime_event_loop_blocking_census.py
```

Expected: PASS.

**Step 4: Inspect and commit explicit paths**

Run `git diff --check`, inspect the source/test diff, stage only the two explicit
paths, verify `git diff --cached --name-only`, and commit the tested candidate.

### Task 4: Freeze and verify the final candidate

**Files:**
- Verify only; do not edit production code after this step begins.

**Step 1: Run the full suite exactly once**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. If production code changes afterward, the candidate is no
longer frozen and this task must be repeated according to repository policy.

### Task 5: Record the handoff and release the claim

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Record evidence**

Record the design, plan, claim, and candidate commit IDs; exact RED, GREEN,
focused, and full-suite results; the no-Telegram/no-production verification
boundary; and the remaining function-level uncertainty.

**Step 2: Release the claim**

Set `claimed_by: unclaimed`, `claim_base_sha: null`, and route `current_task` to
owner authorization for an exact non-force push. Keep deployment and cutover
authorization false. Phase 7 remains incomplete and Phase 8 remains ineligible.

**Step 3: Commit the status explicitly**

Stage only `docs/per-chat-durable-lanes-status.md`, verify the cached path, and
commit. Confirm final HEAD equals the last commit touching canonical status and
the worktree is clean.
