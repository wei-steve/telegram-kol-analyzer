# Authoritative Failure Notification Idempotency Implementation Plan

> **For Codex:** REQUIRED SUB-SKILLS: use `executing-plans` and
> `test-driven-development` task by task. Do not start Claude, subagents,
> background agents, or parallel implementation sessions.

**Goal:** Deliver at most one Telegram authoritative-failure notification per
raw message while preserving the first alert and a later retry after an actual
send failure.

**Architecture:** Atomically claim `NULL|failed -> scheduled` on the existing
`recognition_decisions.notification_status` field before creating the sender
task. Durable MiMo processing retries remain unchanged and later attempts skip
notification scheduling after `scheduled`, `sent`, or `suppressed_*`.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite WAL, asyncio, pytest, the existing
exact-SHA gated updater.

---

### Task 1: Reproduce and specify the once-only boundary

**Files:**

- Modify: `tests/test_recognition_decisions.py`
- Modify: `tests/test_telegram_live_listener.py`

**Step 1: Write the failing persistence test**

Add a test for a wished-for
`claim_authoritative_failure_notification(...)` helper. Two claims for one row
must return `True` then `False`, leave `notification_status=scheduled`, and keep
the supplied automation outcome.

**Step 2: Write the failing delivery test**

Persist one terminal authoritative-failure decision, schedule and await its
notification twice, and assert the sender is called exactly once. Assert the
second schedule returns `None` and the row remains `sent`.

**Step 3: Run RED**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py -k authoritative_failure_notification_claim \
  tests/test_telegram_live_listener.py -k authoritative_notification_is_once_only
```

Expected: fail because the claim helper/once-only behavior does not exist.

### Task 2: Implement the minimum atomic claim

**Files:**

- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `tests/test_telegram_live_listener.py`

**Step 1: Add the persistence helper**

Use `BEGIN IMMEDIATE`, load the exact decision, and permit only `NULL` or
`failed` to transition to `scheduled`. Update automation fields and clear the
old notification error in the same transaction. Missing rows keep the existing
`LookupError` behavior.

**Step 2: Claim before scheduling**

Replace the unconditional `scheduled` update in
`_schedule_authoritative_notification` with the helper. Return `None` without
creating a task when the claim is not owned. Keep the existing sender success,
failure, and incident-capture paths unchanged.

**Step 3: Preserve send-failure retry**

Extend the existing notification-failure test so the first sender failure
persists `failed`, a later schedule is allowed, and the successful retry writes
`sent`. This is a regression guard for the approved design, not a new retry
loop.

**Step 4: Run GREEN and focused regression**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py -k 'authoritative_failure_notification or terminal_authoritative_failure_preserves' \
  tests/test_telegram_live_listener.py -k 'authoritative_notification or authoritative_mimo_failure' \
  tests/test_message_processing_worker.py -k 'authoritative_failure or retry'
```

Expected: all selected tests pass.

**Step 5: Commit exact paths**

```bash
git add \
  src/telegram_kol_research/recognition_decisions.py \
  src/telegram_kol_research/telegram_live_listener.py \
  tests/test_recognition_decisions.py \
  tests/test_telegram_live_listener.py
git diff --cached --name-only
git commit -m "fix: deduplicate authoritative failure notifications"
```

### Task 3: Assemble the final candidate

**Step 1: Run the complete focused slice**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py \
  tests/test_telegram_live_listener.py \
  tests/test_message_processing_worker.py \
  tests/test_semantic_disagreement_worker.py \
  tests/test_semantic_review_control.py
```

**Step 2: Run the one final full suite**

```bash
./.venv/bin/python -m pytest -q
git diff --check
```

Expected: zero failures and no unexpected XPASS. Any later production-code
change requires the affected focused tests and one new final full suite.

### Task 4: Push, deploy, and verify production

**Step 1: Prove the safe window**

Verify exact production SHA, active service, `semantic_review_enabled=false`,
`message_lock_mode=global`, `message_pipeline_mode=queue`,
`worker_command_mode=shadow`, unchanged topology, WAL, `quick_check=ok`,
`active_write_count=0`, and no active management mutation. One incomplete retry
fails closed.

**Step 2: Fast-forward push and exact-SHA deploy**

Verify the remote deploy branch is an ancestor, push without force, then run:

```bash
EXPECTED_COMMIT=<candidate-40-hex> ./scripts/server_git_update.sh
```

Never pull manually. Verify exact HEAD, active service, settings, and topology.

**Step 3: Observe the real duplicate boundary**

Use a quiet server-side monitor. If a natural MiMo authoritative failure retries,
prove its durable `attempt_count` increases while the persisted notification
does not transition back from `sent` to `scheduled` and no second delivery is
recorded. Also record new semantic-review/402 deltas, SQLite_BUSY, duplicates,
backlogs, loop stalls, and execution events. Do not manufacture traffic.

**Step 4: Update canonical status and stop**

Record the design, plan, claim, code, candidate, pushed and deployed SHAs;
focused/full-suite results; production evidence; rollback target; and any
remaining Phase 6R L2 traffic gate. Release the claim, stage only the status
file, commit, push, send the single required stop notification, and return
control. Do not resume Phase 6A in this turn.
