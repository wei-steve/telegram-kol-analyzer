# Phase 5 — Queue Consumer Takeover

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Make the Telegram listener callback do nothing but persist and enqueue,
and move all recognition and execution work to workers that consume the durable
job table.

**Nature:** The real cutover. Highest risk phase in the remediation. Flagged, with
a proven instant rollback.

**Prerequisite:** Phase 4 is complete and `missing_job_count` has been zero over
at least one full trading session including a service restart. If that evidence is
not recorded in `docs/runtime-serialization-remediation-status.md`, stop and
finish Phase 4's observation instead.

## Why this phase exists

After Phase 4, the durable job row exists but nothing consumes it. Processing is
still inline in the Telethon callback
(`src/telegram_kol_research/telegram_live_listener.py:600` →
`persist_live_message_event` at `:109`), so an exception or a restart still loses
in-flight work, and the compensator is still an after-the-fact scan.

Making the consumer authoritative gives four properties the inline path cannot
have:

1. A restart resumes pending jobs instead of losing them.
2. Retry is scheduled in the database (`attempt_count`, `next_attempt_at`) rather
   than by an in-memory `asyncio.create_task` sleep — the current retry at
   `telegram_live_listener.py:475` dies with the process.
3. The listener returns in milliseconds, so Telegram delivery can never be
   back-pressured by LLM or exchange latency.
4. Failed execution becomes a visible job state rather than something inferred by
   a repair module.

## Scope

Add a consumer worker, extend the flag with a `queue` mode, and cut over. Do not
change what recognition or execution decides — only what invokes it.

### Task 1: Extract the processing body

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Create: `src/telegram_kol_research/message_processing_worker.py`
- Create: `tests/test_message_processing_worker.py`

**Step 1 — Split `persist_live_message_event` at the seam**

The seam is the point after `persist_normalized_messages` and after the shadow
enqueue added in Phase 4, right before the context resolution scheduling block at
`telegram_live_listener.py:187`.

Everything after the seam — reply target recovery, `authoritative_processor`,
failure notification, instruction summary delivery, strategy alerts, context
resolution — moves into `process_message_job(session_factory, *, raw_message_id, ...)`
in the new module.

**Step 2 — Keep the inline path calling the extracted function**

In `inline` and `shadow` modes, `persist_live_message_event` calls
`process_message_job` directly, exactly where the code used to run. This is a pure
refactor with no behavior change, and the entire existing test suite must pass
unchanged to prove it. Do not proceed to Task 2 until it does.

**Step 3 — Media download stays in the listener**

`_download_media_if_present` needs the live Telethon client and event. It stays
before the seam. Only work reachable from the persisted database row moves to the
worker.

Reply target recovery (`fetch_missing_reply_target`, `:222`) also needs the client.
Either keep it before the seam, or pass a client accessor to the worker. Prefer
keeping it in the listener — it is bounded and it preserves the worker's property
of needing nothing but the database.

### Task 2: Build the consumer worker

**Files:**
- Modify: `src/telegram_kol_research/message_processing_worker.py`
- Modify: `tests/test_message_processing_worker.py`

**Step 1 — Claim, process, settle**

One tick: select `pending` jobs whose `next_attempt_at` is null or due, claim them
atomically with a `claim_token` and a conditional update, process each, then settle
to `succeeded` or `failed`.

The claim must be atomic against a concurrent claimer. The codebase already has
this pattern — `claim_worker_batch` in the strategy management worker and the
claim logic in `context_resolution_worker.py:447` are the references to match.

**Step 2 — Shard by chat, run chats concurrently**

One in-flight job per `chat_id` at a time, multiple chats concurrently. This is the
same guarantee Phase 2 established with per-chat locks, now expressed in the job
selection. Process jobs within a chat in `raw_message_id` order.

**Step 3 — Durable retry with backoff**

On failure, increment `attempt_count` and set `next_attempt_at` with bounded
exponential backoff. After a maximum attempt count, settle `failed` with a reason
code and notify through the existing system operator path.

This replaces the in-memory retry at `telegram_live_listener.py:469`, which does
not survive a restart.

**Step 4 — Reclaim stale claims**

A job claimed by a process that died must become claimable again after a timeout.
Without this, one crash strands a message forever — the exact failure class this
phase exists to remove. Test it explicitly.

**Step 5 — Never block the event loop**

The worker loop must offload with `await asyncio.to_thread(...)` or a dedicated
executor. The blocking-call census test from Phase 0 must still pass. Do not
reintroduce the Phase 1 defect in the very worker built to fix Phase 3's problem.

**Step 6 — Respect the expiry policy**

Reuse the expiry classification from Phase 3. A job whose message is older than the
configured window settles `expired` with the correct classification and is not
executed. Expiry stays fail-safe.

### Task 3: Add `queue` mode and wire the worker

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

Extend `message_pipeline_mode` to
`Literal["inline", "shadow", "queue"]`, default unchanged.

In `queue` mode: `persist_live_message_event` enqueues and returns without calling
`process_message_job`, and the consumer worker task is started from the lifespan,
matching the existing `asyncio.create_task` plus `_log_background_task_result`
shape.

Guard against double processing: in `queue` mode the inline call must be off, and
in the other modes the consumer must not be running. Assert both directions in
tests. A message processed twice could place a duplicate order — this is the single
most dangerous defect available in this phase, so test it before deploying.

### Task 4: Cutover regression tests

**Files:**
- `tests/test_message_processing_worker.py`
- Create: `tests/test_message_pipeline_mode_exclusivity.py`

Assert:

1. `inline` and `shadow` behave exactly as before the phase.
2. In `queue` mode, the listener callback returns without invoking recognition.
3. In `queue` mode, the worker produces the same recognition decision and the same
   execution outcome as the inline path for identical input.
4. A job claimed and then abandoned by a simulated crash is reclaimed and completed.
5. No mode processes a message twice.
6. Retry backoff and maximum attempts behave as specified, and survive a simulated
   restart.
7. Per-chat ordering is preserved; different chats proceed concurrently.

Test 3 and test 5 are the acceptance gate for the cutover.

### Task 5: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

The default is unchanged, so the whole existing suite must pass. Record before and
after counts.

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/message_processing_worker.py \
  src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py
git diff --cached --name-only
git commit -m "feat: add durable message processing worker behind a dormant queue mode"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 6: Deploy dormant, prove rollback, then cut over

**Step 1 — Deploy in `shadow` mode**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
There are no change classes on this branch — the only required argument is the
commit, and schema changes are detected automatically.

[local] Commit, push to the branch recorded as `deploy_branch` in the status
file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and `-Branch <deploy_branch>`.

The updater enforces the safe window itself with an active-write check, before
and after it stops the service. Exit code 3 means an exchange write is genuinely
in flight — wait and retry later, do not work around it.

Confirm the mode is `shadow` and behavior is unchanged. The refactor from Task 1
is live at this point, so verify recognition and execution still work normally
**before** touching the mode.

**Step 2 — Prove the rollback path first**

Switch `shadow` → `queue` → `shadow`, confirming each transition takes effect and
that no message is dropped or double-processed at either boundary. The transition
boundary is the risky moment: verify explicitly that a message in flight during the
switch is handled exactly once.

**Step 3 — Cut over during a quiet window**

Enable `queue` when there is no active time-sensitive strategy operation and no
in-flight management batch.

**Step 4 — Observe**

For at least one full trading session:

- The parity endpoint shows `stuck_pending_count` zero and no growing backlog.
- `oldest_pending_age_seconds` stays low.
- Recognition decisions and execution events continue at the normal rate.
- No duplicate orders. Check `execution_events` and exchange order history
  directly, not just internal counters.
- Loop lag from Phase 0 stays healthy.
- Restart the service once deliberately, mid-traffic, and confirm pending jobs
  resume instead of being lost. **This is the property the whole phase exists for
  — do not skip it.**

**Step 5 — If anything is wrong**

Set `message_pipeline_mode: shadow`. Immediate, no restart. Record what happened
before attempting a second cutover.

## Completion criteria

- The refactor in Task 1 shipped with the existing suite passing unchanged.
- Mode exclusivity is enforced and tested in both directions.
- `queue` enabled in production, with rollback proven working beforehand.
- A deliberate mid-traffic restart demonstrably resumed pending jobs.
- One full trading session with no duplicate orders and no growing backlog.

## Rollback

Set `message_pipeline_mode: shadow` in trading settings. Immediate, no restart and
no deploy — `deployment-procedure.md` rollback level 1.

Jobs already claimed complete on their own; new work returns to the inline path.
The table and the worker stay in place, inert. If the code must go, redeploy the
previous known good SHA.

## Status file update

Set `phase_status: completed`, `current_phase: 6`,
`phase_name: process-separation`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-6-process-separation.md`.
Record the cutover time, the observation window, the restart-resume evidence, and
explicitly whether duplicate orders were checked against exchange history rather
than only internal state.
