# Phase 4 — Durable Job Table, Shadow Enqueue

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Create the durable message-processing job record and start writing it
in shadow mode, so that the queue's correctness can be proven against live
traffic before anything consumes it.

**Nature:** Additive and dormant. Nothing reads these rows in this phase. No
processing behavior changes at all.

**Prerequisite:** Phase 3 complete. If the reported symptoms are already gone,
confirm with the user that phases 4 through 6 are still wanted before starting —
this is the point where the remediation shifts from fixing the common failure to
removing a residual one.

## Why this phase exists

Message processing is inline in the Telethon callback. `handle_new_message`
(`src/telegram_kol_research/telegram_live_listener.py:600`) calls
`persist_live_message_event` (`:109`), which runs the full chain: persist, media
download, reply recovery, `authoritative_processor`, notification, contextual
resolution.

There is no durable record that "this message needs processing" separate from
"this message was received". So a restart, an unhandled exception, or a stalled
loop loses the in-flight message, and the only compensator is the after-the-fact
gap scan improved in Phase 3 — which infers pending work by the *absence* of a
`RecognitionDecision` row rather than from an explicit job record.

Inference-by-absence works for the recognition step only. It cannot represent a
message that was recognized but whose execution failed midway, which is why that
case is handled by a completely separate set of repair modules today.

An explicit job row with a status state machine, an attempt count, and a next
attempt time fixes both, but only if the enqueue is proven complete first. Hence
shadow mode.

## Scope

Add the table, write rows on the live path and the recovery path, add a parity
projection, and prove enqueue matches reality. Do not consume the rows.

### Task 1: Add the job table

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `tests/test_message_processing_jobs_schema.py`

**Step 1 — Define the model**

Add `message_processing_jobs`. Required columns:

| Column | Purpose |
|---|---|
| `id` | primary key |
| `raw_message_id` | FK to `raw_messages` |
| `chat_id` | shard key, indexed |
| `status` | `pending` / `claimed` / `succeeded` / `failed` / `expired` |
| `attempt_count` | integer, default 0 |
| `next_attempt_at` | datetime, nullable, indexed |
| `claim_token` | nullable, for at-most-one-consumer claiming |
| `claimed_at` | nullable |
| `last_reason` | nullable reason code |
| `enqueued_at` | datetime |
| `completed_at` | nullable |
| `shadow` | boolean, true while the row is not authoritative |

Follow the conventions already in `models.py`: 80 tables exist, match their
naming, nullability, and index style.

**Step 2 — Unique index on raw_message_id**

One job per raw message. Add a unique index. Idempotent enqueue depends on it.

**Step 3 — SQLite compatibility path**

`db.py` maintains `SQLITE_COMPAT_COLUMNS` (`src/telegram_kol_research/db.py:61`)
for additive columns on existing tables. A brand-new table is created by
`Base.metadata.create_all`, but verify explicitly that bootstrapping an existing
production-shaped database creates it, and add the unique index to the same
bootstrap path that already creates
`POSITION_OWNERSHIP_UNIQUE_INDEX_SQL` and friends (`db.py:16` onward).

**Step 4 — Prove the migration on a copy of production**

Take a copy of the production database and run bootstrap against it. Confirm the
table and index are created and no existing table is altered. Record the row
counts before and after to prove nothing else moved.

### Task 2: Shadow enqueue on the live path

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Create: `tests/test_message_processing_shadow_enqueue.py`

**Step 1 — Add the flag**

Add `message_pipeline_mode: Literal["inline", "shadow"] = "inline"` to
`TradingSettings`, matching the existing flag style at
`src/telegram_kol_research/trading_settings.py:63` onward. `inline` writes no
rows and is the default.

**Step 2 — Enqueue immediately after persist**

In `persist_live_message_event`, immediately after `persist_normalized_messages`
returns `inserted_keys`, write a `pending` shadow job row for each newly inserted
message. This must happen **before** any recognition work, because the whole point
is that the job survives a failure in the work that follows.

**Step 3 — Mark terminal status at the end of the inline chain**

When the inline chain finishes, update the shadow row to `succeeded` or `failed`
with a reason. This is what makes the parity check meaningful: a shadow row stuck
in `pending` means the inline path died without completing, which is exactly the
loss this remediation is about.

**Step 4 — Never let enqueue break the live path**

Wrap enqueue in its own try/except that logs and continues. In shadow mode a
bookkeeping failure must not stop a real trade. Test this explicitly.

### Task 3: Shadow enqueue on the recovery path

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `tests/test_message_processing_shadow_enqueue.py`

The gap recovery path introduced in Phase 3, and the recovery block inside
`run_reconcile_once`, must also enqueue and mark terminal status. Enqueue must be
idempotent — the unique index on `raw_message_id` plus an upsert, so a message
touched by both paths yields one row, not two or an error.

### Task 4: Parity projection

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

Add read-only `GET /api/runtime/message-pipeline-parity` returning, over a
bounded recent window:

- `raw_messages` count in window
- shadow job count in window
- `missing_job_count` — raw messages in window with no job row
- `orphan_job_count` — job rows with no raw message
- `stuck_pending_count` — jobs `pending` older than a threshold
- `status_breakdown`
- `oldest_pending_age_seconds`

`missing_job_count` is the number that must reach zero. It is the proof that
enqueue is complete and that a consumer would not silently skip messages.

Bound the scan so the endpoint cannot become a slow query as tables grow.

### Task 5: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Default is `inline`, so the existing suite must be entirely unaffected.

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_message_processing_jobs_schema.py \
  tests/test_message_processing_shadow_enqueue.py
git diff --cached --name-only
git commit -m "feat: add dormant message processing job table and shadow enqueue"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 6: Deploy dormant, then enable shadow

**Step 1 — Deploy with `message_pipeline_mode: inline`**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
There are no change classes on this branch — the only required argument is the
commit, and schema changes are detected automatically.

This phase adds a table, so the updater's automatic schema detection trips: it
takes a SQLite backup and runs a migration dry run with `PRAGMA quick_check` and
a watermark comparison. Keep that evidence — this phase depends on it.

[local] Commit, push to the branch recorded as `deploy_branch` in the status
file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and `-Branch <deploy_branch>`.

The updater enforces the safe window itself with an active-write check, before
and after it stops the service. Exit code 3 means an exchange write is genuinely
in flight — wait and retry later, do not work around it.

After it completes, confirm the table exists and is empty, and that
`message_pipeline_mode` reads `inline`.

**Step 2 — Prove the disable path**

Enable `shadow`, confirm rows appear, set back to `inline`, confirm rows stop.
Prove the off switch before leaving it on.

**Step 3 — Record a watermark, then enable shadow**

Record the current maximum `raw_messages.id` as the shadow watermark, so it is
unambiguous that no historical replay occurred. Enable `shadow`.

**Step 4 — Run shadow across real traffic**

Let it run across at least one full trading session with real multi-group
traffic, including at least one service restart, then check parity:

[server] — `127.0.0.1` is the server's loopback, not yours:

```bash
ssh -i ~/.ssh/tecent.pem root@43.167.220.225 'curl -s http://127.0.0.1:8000/api/runtime/message-pipeline-parity'
```

Required results:

- `missing_job_count` is zero
- `orphan_job_count` is zero
- `stuck_pending_count` is zero, **or** every stuck row corresponds to a real
  observed incident — in which case that is a genuine finding about the inline
  path, record it

**Step 5 — Confirm zero trading impact**

Confirm order counts, recognition decisions, and execution events show no
deviation attributable to this phase. Shadow mode writes bookkeeping rows and
nothing else.

## Completion criteria

- Table and unique index exist in production, migration proven on a database
  copy.
- Shadow enqueue live on both the live and recovery paths, idempotent.
- Parity endpoint reports `missing_job_count` zero over a full trading session
  including a restart.
- The disable path was proven before shadow was left enabled.
- No trading behavior changed.

## Rollback

Set `message_pipeline_mode: inline`. Rows stop being written immediately, no
restart and no deploy — `deployment-procedure.md` rollback level 1.

Leave the table in place. Nothing reads it, an unused table is harmless, and
dropping it would be a destructive migration for no benefit. If the code must go,
redeploy the previous known good SHA — the table already
exists on the server, so the rollback deployment is not schema-affecting.

## Status file update

Set `phase_status: completed`, `current_phase: 5`,
`phase_name: queue-consumer-takeover`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-5-queue-consumer-takeover.md`.
Record the shadow watermark, the parity numbers, and the observation duration.
Phase 5 must not start until parity has been clean for at least one full trading
session including a restart.
