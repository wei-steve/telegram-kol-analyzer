# Phase 3 — Compensation Window Repair

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Stop the system from silently discarding a backlog of unprocessed
messages after a stall, and make "too late to trade" a deliberate business
decision instead of a side effect of the system having been stuck.

**Nature:** Recovery semantics change. No new tables, no new processes.

**Prerequisite:** Phases 1 and 2 are complete. This phase reduces the damage from
a stall; the previous phases reduce the stalls themselves. Doing this one first
would mask the real problem.

## Why this phase exists

The only compensator for a missed message is the reconcile recovery loop at
`src/telegram_kol_research/telegram_live_listener.py:804`, which selects raw
messages that have no `RecognitionDecision` row:

```python
missing_decision_query = (
    session.query(RawMessage)
    .outerjoin(RecognitionDecision, RecognitionDecision.raw_message_id == RawMessage.id)
    .filter(RawMessage.chat_id.in_(chat_titles_by_id))
    .filter(RecognitionDecision.id.is_(None))
)
```

Three properties make this inadequate:

1. **Cadence.** `run_periodic_reconcile` runs every 300 seconds by default
   (`src/telegram_kol_research/web_app.py:3908`).
2. **Hard expiry.** `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE = timedelta(minutes=15)`
   (`src/telegram_kol_research/telegram_live_listener.py:64`). Anything older is
   routed to `_record_expired_authoritative_recovery_gap` (`:511`) and never
   executed. At 300s cadence that is roughly three attempts before permanent
   loss.
3. **Coupling.** Recovery is welded to the Telegram fetch pass. It calls
   `discover_dialogs_fn` first, so if Telegram is slow or the session is
   unhealthy, recovery of already-persisted local messages does not happen
   either — even though it needs no network at all.

The result: a stall longer than 15 minutes permanently drops the backlog, and the
drop is silent from a trading standpoint.

Note the expiry threshold is not wrong in principle. A 40-minute-old entry signal
usually *should* not be executed blind. What is wrong is that the decision is made
by a fixed timer with no reference to the instruction or the market, and that it
is indistinguishable from "the system was broken".

## Scope

Three changes:

1. Decouple local gap recovery from the Telegram fetch pass and run it on a fast
   cadence.
2. Replace silent expiry with an explicit, recorded, notified decision.
3. Make the expiry threshold configurable and make stall-induced expiry
   distinguishable from genuine staleness.

Out of scope: the durable job queue. That is a later phase.

### Task 1: Extract local gap recovery into its own loop

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_authoritative_gap_recovery_loop.py`

**Step 1 — Split the function**

Extract the recovery block at `telegram_live_listener.py:804-876` into a standalone
`recover_missing_authoritative_decisions(...)` that takes `chat_titles_by_id`
directly and performs **no Telegram network calls**. `run_reconcile_once` then
calls the extracted function, so its behavior is unchanged.

**Step 2 — Add a dedicated fast loop**

Add `run_authoritative_gap_recovery_loop(...)` with a default interval of 20
seconds, started from the Web lifespan alongside the other background tasks
(`src/telegram_kol_research/web_app.py:3956` onward — match the existing
`asyncio.create_task` plus `_log_background_task_result` shape).

It must resolve `chat_titles_by_id` from local configuration or the database, not
from `discover_dialogs`. It must offload its synchronous work with
`await asyncio.to_thread(...)` — do not reintroduce the Phase 1 defect.

**Step 3 — Bound it**

Keep the existing `limit(message_limit)` bound so one pass cannot run unbounded.
The fast cadence is what provides coverage, not a larger batch.

**Step 4 — Acquire the right lock**

If Phase 2 shipped `per_chat` mode, this loop must take the per-chat lock for each
message's `chat_id`, matching what the reconcile recovery loop does. Do not let it
run lock-free — it invokes the same `authoritative_processor` as the live path.

### Task 2: Make expiry explicit, recorded, and notified

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Create/modify: tests for `_record_expired_authoritative_recovery_gap`

**Step 1 — Classify the cause**

When a message is about to expire, record whether the system was healthy or
degraded during the message's lifetime. Use the Phase 0 loop health snapshot plus
the message's age: if a stall episode overlapped the message's window, classify
`expired_after_system_stall`; otherwise `expired_stale_instruction`.

These two cases mean completely different things operationally and must not share
one reason code.

**Step 2 — Notify on stall-induced expiry**

`expired_after_system_stall` is a production incident: the system dropped
tradeable instructions because it was broken. Route it through the existing
system operator notification path so it is visible, following how
`_handle_authoritative_failure_notification` (`:429`) already does this. Rate
limit it — a stall can expire many messages at once and must produce a bounded
number of notifications, not one per message.

`expired_stale_instruction` stays quiet and is recorded only.

**Step 3 — Never auto-execute an expired message**

Expired messages must still not be executed automatically. Expiry remains
fail-safe. This phase makes the loss visible and reviewable, it does not make old
signals fire.

### Task 3: Make the window configurable

**Files:**
- Modify: `src/telegram_kol_research/config.py` or
  `src/telegram_kol_research/trading_settings.py` (match where comparable
  runtime tunables already live)
- Modify: tests accordingly

Replace the hardcoded `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE` constant with a
configurable value defaulting to the current 15 minutes, so the default changes
nothing. Making it tunable means a future stall can be recovered from by widening
the window deliberately and temporarily, under human judgment, instead of losing
the backlog.

### Task 4: Regression tests

**Files:**
- `tests/test_authoritative_gap_recovery_loop.py`

Assert:

1. Gap recovery runs without any Telegram client present.
2. A message missing a decision is recovered within one fast-loop interval.
3. Recovery is bounded per pass.
4. A message older than the window is not executed, is recorded, and is
   classified correctly for both the stalled and the healthy case.
5. Stall-induced expiry notifications are rate limited.
6. `run_reconcile_once` behavior is unchanged by the extraction.

### Task 5: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/web_app.py \
  tests/test_authoritative_gap_recovery_loop.py
git diff --cached --name-only
git commit -m "fix: decouple authoritative gap recovery and make expiry explicit"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 6: Deploy and verify

**Step 1 — Deploy**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
This phase deploys with `-ChangeClass execution_writer`.

Because the class is `execution_writer`, capture a prior independent live
position snapshot first and pass `-PreviousLiveSnapshotPath`.

[local] Commit, push to the deploy branch recorded as `deploy_branch` in the
status file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and the change class above.

The updater enforces the safe window itself through `deployment-preflight`
before it stops the service. If it returns `BLOCK`, read the reason, wait, and
record it — do not retry blindly.

**Step 2 — Verify the fast loop is running**

Confirm from the journal that the gap recovery loop ticks on its interval and
that it does not call `discover_dialogs`.

**Step 3 — Verify recovery latency improved**

The measurable claim is: time from "message persisted without a decision" to
"decision produced" drops from up to 300 seconds to roughly the fast interval.
Record the observed value.

**Step 4 — Verify no behavior regression**

Confirm normal live messages are still processed by the live path, not by the
recovery loop. The recovery loop should be idle almost all the time. If it is
routinely recovering messages, that means the live path is failing and is a
finding to record, not to paper over.

## Completion criteria

- Gap recovery runs independently of the Telegram fetch pass, on a fast cadence,
  off the event loop, under the correct lock.
- Expiry is classified, recorded, and notified when caused by a stall.
- The window is configurable with the default unchanged.
- Recovery latency improvement measured and recorded.
- The recovery loop is observed idle during healthy operation.

## Rollback

No settings flag exists in this phase, so rollback is a redeploy of the previous
known good 40-hex SHA with `-ChangeClass execution_writer` and a fresh
`-PreviousLiveSnapshotPath`. See `deployment-procedure.md`, rollback level 2.

The extraction keeps `run_reconcile_once` behaviorally identical, so reverting
restores exactly the prior recovery path. There is no database migration.

The configurable window added in Task 3 defaults to the current 15 minutes, so if
only the window needs reverting, change the setting rather than redeploying.

## Status file update

Set `phase_status: completed`, `current_phase: 4`,
`phase_name: durable-job-shadow-enqueue`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-4-durable-job-shadow-enqueue.md`.
Append `local_tests` and `server_verification` entries including the measured
recovery latency before and after.

## Stopping point

If the reported symptoms are gone after this phase, stopping here is a legitimate
outcome. Phases 4 through 6 address the residual failure class — losing in-flight
work on restart, and web traffic perturbing execution — not the common one.
Record the decision in the status file either way.
