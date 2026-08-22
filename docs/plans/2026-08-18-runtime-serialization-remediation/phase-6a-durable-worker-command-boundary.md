# Phase 6A Durable Worker Command Boundary Implementation Plan

> **For Codex:** REQUIRED SUB-SKILLS: use `executing-plans` and
> `test-driven-development` to implement this plan task by task. Do not start
> subagents, Claude, background agents, or parallel implementation sessions.

**Goal:** Put the four Web exchange-authority routes behind a worker-owned
durable job boundary without changing their successful interface or any
recognition, strategy, position-ownership, execution, or exchange-write
semantics.

**Architecture:** A new SQLite `worker_command_jobs` table owns command
identity, claim/lease state, results, and uncertainty. Web validates, enqueues,
and waits asynchronously. A worker consumer alone invokes thin adapters around
the existing domain functions. Migration-only `inline` and `shadow` modes are
used for parity, then a hardened queue-only route candidate removes all legacy
Web authority reachability before Phase 6 resumes.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy, SQLite WAL, pytest, vanilla
JavaScript, the existing gated GitHub-to-server updater, and read-only Deepcoin
history APIs.

---

## Scope and hard invariants

Read this file completely before implementation. Do not read another phase
file. The approved design is
`docs/plans/2026-08-22-phase-6a-durable-worker-command-boundary-design.md`.

This phase owns only:

- `POST /api/execution/sync-deepcoin`;
- `POST /api/execution/close-bound-position`;
- `POST /api/recovery-live-submit`;
- `POST /api/trade-signals/process-next`;
- the durable command schema, worker, result bridge, uncertainty audit, modes,
  tests, migration rehearsal, cutover, and rollback needed by those routes.

It must preserve:

- the current request validation and normal status/body for all four routes;
- the exact existing domain functions and their arguments;
- recognition results, contextual strategy resolution, position attribution,
  and all exchange-write parameters and ordering;
- `message_lock_mode=global` and `message_pipeline_mode=queue`;
- the single-process production topology until Phase 6 resumes.

It must not absorb DeepSeek 402, Monitor A, L3 historical repair, per-chat lock
enablement, systemd process separation, or cleanup from any later phase.

This is L3 because it adds a production table, and it also carries L2 durable
consumer/recovery risk. Never create a live order merely to satisfy a test.
Incomplete external evidence is unknown; after one reasoned retry, fail closed,
leave the phase `in_progress`, record the evidence path, and stop.

## Verification discipline

For every production-code edit below:

1. Add the named focused test first.
2. Run it and capture the expected failure.
3. Make the minimum production change.
4. Rerun only the affected focused tests.
5. Commit the bounded task with explicit staged paths after checking
   `git diff --cached --name-only`.

Do not run the full suite during ordinary task development. Candidate A gets
one full-suite run after all of its production code is assembled. Candidate B
removes the migration-only legacy path after production shadow/queue evidence;
because that is a later production-code change, rerun affected focused tests
and exactly one new full suite for Candidate B.

Never use `git add -A`, `git pull`, force push, reset, clean, or stash. Send no
extra Telegram messages. Use the project's one required stop notification only
when returning control to the user.

## Task 0: Exclusive preflight and claim

**Files:**

- Modify: `docs/runtime-serialization-remediation-status.md`

**Step 1: Verify the canonical pointer and exclusive checkout**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/deepcoin-auto-trading-v1
git diff --check
rg -n "^(current_phase|phase_name|phase_status|claimed_by|current_phase_file):" \
  docs/runtime-serialization-remediation-status.md
find "$(git rev-parse --git-dir)" -maxdepth 1 \
  \( -name '*.lock' -o -name 'index.lock' \) -print
lsof +D "$PWD" 2>/dev/null
```

Expected: clean tree; `current_phase: 6a`,
`phase_name: durable-worker-command-boundary`, `phase_status: planned`, and
`claimed_by: null`; no other writer or lock. A cwd-only shell/Codex process is
informational. Any contradictory canonical state or real concurrent writer is
a hard stop; do not repair it yourself.

**Step 2: Claim before touching implementation files**

Set `phase_status: claimed` and `claimed_by` to this session id. Stage only the
status file, inspect the staged path, and commit:

```bash
git add docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "chore: claim runtime serialization phase 6a"
```

Record the exact claim SHA in the status YAML. Only then continue.

## Task 1: Freeze the four existing HTTP contracts

**Files:**

- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_assets_smoke.py`
- Test: `tests/test_process_boundary_authority.py`

**Step 1: Add characterization tests before changing routes**

Add parameterized tests that capture, for each route:

- accepted request shape and exact normal `200` JSON;
- existing `400`, `409`, `422`, `502`, and bounded `500` mappings that apply;
- exact arguments/timestamp/provider passed to the existing domain function;
- current notification ordering for sync and close;
- no implicit retry after an adapter exception.

Also freeze the three first-party UI call sites for sync, close, and recovery.
`process-next` currently has no first-party UI caller; assert that rather than
inventing one.

**Step 2: Run the characterization slice**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_web_app.py -k 'sync_deepcoin or close_bound_position or recovery_live_submit or trade_signal_process_next' \
  tests/test_web_assets_smoke.py -k 'sync_deepcoin or close_bound_position or recovery_live_submit' \
  tests/test_process_boundary_authority.py
```

Expected: characterization tests pass; authority result remains exactly
`1 passed, 1 strict-xfailed`. Any different blocker inventory stops the phase.

**Step 3: Commit tests only**

```bash
git add tests/test_web_app.py tests/test_web_assets_smoke.py
git diff --cached --name-only
git commit -m "test: freeze worker command route contracts"
```

## Task 2: Add the dormant command mode setting

**Files:**

- Modify: `tests/test_trading_settings.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write the failing setting tests**

Require `worker_command_mode` to default to `inline`, round-trip through the
existing settings store/API, accept only `inline|shadow|queue`, and fail closed
on all other values. Assert this does not alter `message_lock_mode` or
`message_pipeline_mode`.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py -k worker_command_mode \
  tests/test_web_app.py -k worker_command_mode
```

Expected RED: missing field/parser/API value.

**Step 2: Implement the minimum setting**

Add the typed field and strict parser using the existing
`message_pipeline_mode` pattern. Do not change either existing mode default.

**Step 3: Rerun and commit**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py -k 'worker_command_mode or message_pipeline_mode or message_lock_mode' \
  tests/test_web_app.py -k worker_command_mode
git add tests/test_trading_settings.py tests/test_web_app.py \
  src/telegram_kol_research/trading_settings.py
git diff --cached --name-only
git commit -m "feat: add dormant worker command mode"
```

## Task 3: Add the durable schema and idempotent bootstrap

**Files:**

- Create: `tests/test_worker_command_jobs_schema.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`

**Step 1: Write failing schema tests**

Assert exact columns from the approved design, closed status and command-type
checks, unique `command_id`, unique non-null
`(command_type, idempotency_key)`, claim-scan and fingerprint indexes, empty
initial table, and repeated bootstrap idempotence. Build a legacy fixture DB and
prove two bootstraps preserve every legacy row.

```bash
./.venv/bin/python -m pytest -q tests/test_worker_command_jobs_schema.py
```

Expected RED: `worker_command_jobs` is absent.

**Step 2: Add the model and bootstrap indexes**

Implement `WorkerCommandJob` with bounded `String` fields, `Text` JSON fields,
UTC timestamps, SQL check constraints, and explicit indexes. Use SQLite's
multiple-NULL unique behavior for optional idempotency keys. Add explicit index
bootstrap constants only where `Base.metadata.create_all()` is insufficient.
Do not add or rewrite columns on existing business tables.

**Step 3: Rerun schema tests and existing migration tests**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_worker_command_jobs_schema.py \
  tests/test_message_processing_jobs_schema.py \
  tests/test_db_migrations.py tests/test_migration_assets.py
```

Expected GREEN.

**Step 4: Commit**

```bash
git add tests/test_worker_command_jobs_schema.py \
  src/telegram_kol_research/models.py src/telegram_kol_research/db.py
git diff --cached --name-only
git commit -m "feat: add durable worker command schema"
```

## Task 4: Implement enqueue, claim, settlement, and uncertainty

**Files:**

- Create: `tests/test_worker_command_jobs.py`
- Create: `src/telegram_kol_research/worker_command_jobs.py`

**Step 1: RED — canonical enqueue and idempotency**

Test canonical JSON/fingerprint generation, payload size/secret rejection,
unique command ids, same-key/same-payload reuse, same-key/different-payload
`409` classification, and keyless one-request-one-command behavior.

```bash
./.venv/bin/python -m pytest -q tests/test_worker_command_jobs.py \
  -k 'enqueue or fingerprint or idempotency or secret or bounded'
```

Expected RED: module missing.

Implement only enqueue and result lookup, then rerun GREEN.

**Step 2: RED — atomic claims and leases**

Test two independent session factories racing for one row, claim-token guarded
updates, deterministic oldest-first selection, attempt count, and exclusion of
shadow/terminal/uncertain rows. Test three independent processes against a WAL
test DB; exactly one owns each command and `SQLITE_BUSY=0` under the configured
30-second timeout.

Implement an atomic conditional update claim and lease metadata. Rerun the
claim/concurrency slice.

**Step 3: RED — execution boundary and recovery**

Test:

- stale `claimed` with no `side_effect_started_at` returns to pending;
- `executing` is committed before the injected adapter starts;
- stale/lost `executing` becomes `uncertain`, never pending;
- succeeded/failed settlement requires the current claim token;
- process death before `executing` is reclaimable after restart;
- process death after `executing` is not replayed after restart;
- bounded result/error persistence and explicit result schema version.

Implement `mark_executing`, guarded terminal settlement, and the uncertainty
sweeper. There is no automatic transition out of `uncertain`.

**Step 4: Run the full focused file and commit**

```bash
./.venv/bin/python -m pytest -q tests/test_worker_command_jobs.py
git add tests/test_worker_command_jobs.py \
  src/telegram_kol_research/worker_command_jobs.py
git diff --cached --name-only
git commit -m "feat: implement durable worker command lifecycle"
```

## Task 5: Add thin worker-owned adapters

**Files:**

- Create: `tests/test_worker_command_executor.py`
- Create: `src/telegram_kol_research/worker_command_executor.py`
- Modify: `src/telegram_kol_research/web_app.py` only to expose existing
  dependencies through a worker dependency bundle; do not change routes yet

**Step 1: RED — one adapter at a time**

For each approved command type, write a focused failing test that compares its
arguments, result JSON, error class/status, timestamp placement, and notification
ordering with Task 1's frozen route contract:

1. `sync_deepcoin_execution`;
2. `close_bound_position`;
3. `recovery_live_submit`;
4. `process_next_trade_signal`.

Use injected fake clients only. Assert no adapter changes an order field or
calls a domain function twice.

**Step 2: Implement and test each adapter separately**

Blocking exchange/domain work must run as one bounded unit through
`asyncio.to_thread`; async notification delivery remains ordered after its
current domain action. Map exceptions to a versioned durable result without
storing raw exchange payloads or secrets.

```bash
./.venv/bin/python -m pytest -q tests/test_worker_command_executor.py
```

Expected GREEN after the fourth adapter.

**Step 3: RED — consumer tick/loop**

Test claim -> durable executing -> exactly-one adapter -> settlement, unknown
command fail closed, cancellation, mode checks, stale pre-execution recovery,
and no automatic uncertain replay.

Implement `run_worker_command_tick` and `run_worker_command_loop`. A tick must
load the current setting and claim only in `queue`; `shadow` rows are never
eligible.

**Step 4: Check the event-loop census and commit**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_worker_command_executor.py \
  tests/test_runtime_event_loop_blocking_census.py
git add tests/test_worker_command_executor.py \
  src/telegram_kol_research/worker_command_executor.py \
  src/telegram_kol_research/web_app.py
git diff --cached --name-only
git commit -m "feat: execute durable commands on worker authority"
```

Do not add a blocking-census allowlist entry. If one is needed, the offloading
boundary is wrong; stop and diagnose.

## Task 6: Build compatibility-candidate Web enqueue/wait paths

**Files:**

- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: RED — shadow behavior**

Test that `shadow` preserves each frozen inline result/error while recording one
non-claimable shadow command and a bounded response fingerprint. A shadow
enqueue failure must fail closed before any exchange write, not silently fall
back. Existing idempotency/domain evidence remains unchanged.

**Step 2: Implement shadow, then rerun its four route slices**

Keep the old direct calls only in a clearly named migration-only helper. Do not
change the domain calls themselves.

**Step 3: RED — queue behavior and non-blocking wait**

Test that `queue` routes:

- validate before enqueue;
- pass optional `Idempotency-Key`;
- enqueue exactly once and never call a domain function in the request task;
- poll durable state without blocking a concurrent event-loop heartbeat;
- return the frozen success/error body/status;
- return `504` with `worker_command_timeout` and `command_id` when the wait
  deadline expires while leaving the job live;
- return `503` with `worker_command_uncertain` and `command_id` for uncertainty;
- attach a same-key retry to the same command;
- return `409` for same-key/different-payload conflict.

**Step 4: Implement queue enqueue/wait**

Convert the two synchronous route functions to async where needed. Run every
synchronous DB lookup through `asyncio.to_thread`. Keep the compatibility
candidate's `inline` behavior unchanged.

**Step 5: Rerun and commit**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_web_app.py -k 'worker_command or sync_deepcoin or close_bound_position or recovery_live_submit or trade_signal_process_next' \
  tests/test_runtime_event_loop_blocking_census.py
git add tests/test_web_app.py src/telegram_kol_research/web_app.py
git diff --cached --name-only
git commit -m "feat: route exchange commands through durable jobs"
```

The architecture xfail is expected to remain at this migration stage because
the compatibility candidate intentionally retains the legacy branch. Do not
remove the xfail yet and do not call Phase 6 unblocked.

## Task 7: Add first-party UI idempotency keys

**Files:**

- Modify: `tests/test_web_assets_smoke.py`
- Modify: `src/telegram_kol_research/static/app.js`

**Step 1: RED — stable per-action key tests**

Require sync, close, and live recovery fetches to send `Idempotency-Key` from
`crypto.randomUUID()` with a bounded fallback. The same confirmed action keeps
one key for its request/retry; a new confirmed action gets a new key. Dry-run
gate calls must not receive a live-action key.

```bash
./.venv/bin/python -m pytest -q tests/test_web_assets_smoke.py \
  -k 'idempotency or sync_deepcoin or close_bound_position or recovery_live_submit'
```

Expected RED.

**Step 2: Implement and rerun**

Add a small local helper and set only the new header. Do not alter button
confirmation, payload, success text, refresh timing, or error text.

**Step 3: Commit**

```bash
git add tests/test_web_assets_smoke.py src/telegram_kol_research/static/app.js
git diff --cached --name-only
git commit -m "feat: key first-party exchange commands idempotently"
```

## Task 8: Wire the monolith worker lifecycle and mode exclusivity

**Files:**

- Create: `tests/test_worker_command_mode_exclusivity.py`
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/worker_command_executor.py`

**Step 1: RED — lifespan and transitions**

Test one consumer task starts/cancels cleanly in the current monolith, reacts to
runtime mode changes, drains only queue rows, and never executes shadow rows.
Test transitions in both directions with an in-flight command:

- shadow -> queue does not adopt the shadow row;
- queue -> inline cannot rewrite/execute a `claimed` or `executing` row;
- rollback is refused unless `claimed=0` and `executing=0`;
- `uncertain` survives every mode transition;
- shutdown cannot hang behind a blocking adapter.

**Step 2: Implement lifecycle and transition guard**

Follow the existing message-processing worker lifecycle/cancellation pattern.
Do not add a systemd unit or runtime role; that belongs to Phase 6.

**Step 3: Rerun focused lifecycle/concurrency tests and commit**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_worker_command_mode_exclusivity.py \
  tests/test_worker_command_executor.py \
  tests/test_web_app.py -k 'worker_command or lifespan' \
  tests/test_runtime_event_loop_blocking_census.py
git add tests/test_worker_command_mode_exclusivity.py tests/test_web_app.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/worker_command_executor.py
git diff --cached --name-only
git commit -m "feat: own command consumption in worker lifecycle"
```

## Task 9: Add read-only uncertain reconciliation and guarded terminalization

**Files:**

- Create: `tests/test_worker_command_reconciliation.py`
- Create: `src/telegram_kol_research/worker_command_reconciliation.py`
- Create: `tests/test_worker_command_cli.py`
- Modify: `src/telegram_kol_research/cli.py`

**Step 1: RED — evidence outcomes**

For each command type, use fixtures for exact `posId`, mutation intent,
execution event, binding/order leg, client/exchange order identity, and direct
Deepcoin history. Require exactly these outcomes:

- `confirmed_succeeded`;
- `confirmed_no_submission`;
- `conflict`;
- `evidence_incomplete`.

Prove incomplete external pages are never treated as empty, one reasoned retry
is the maximum, and neither conflict nor incomplete evidence changes the job.
Prove `clOrdId`/tag alone is not exchange proof where parent/child/`posId`
lineage is required.

**Step 2: Implement a pure read-only evidence evaluator**

The evaluator returns a bounded report and performs no DB or exchange write.
It must reuse existing identity/evidence helpers rather than invent new
position ownership rules.

**Step 3: RED — CLI dry run and guarded apply**

Add `worker-command-reconcile --command-id <id>` as dry-run default. Add
`--apply-confirmed` that updates only the command row, only for
`confirmed_succeeded` or `confirmed_no_submission`, and only after rechecking
the row is still `uncertain`. It never submits an order. Test unknown command,
concurrent state drift, and refusal for conflict/incomplete evidence.

**Step 4: Implement, rerun, and commit**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_worker_command_reconciliation.py tests/test_worker_command_cli.py
git add tests/test_worker_command_reconciliation.py \
  src/telegram_kol_research/worker_command_reconciliation.py \
  tests/test_worker_command_cli.py src/telegram_kol_research/cli.py
git diff --cached --name-only
git commit -m "feat: reconcile uncertain worker commands safely"
```

No production `--apply-confirmed` is authorized merely by implementing this
plan. If an uncertain production row appears, collect the dry-run evidence and
stop for exact row-specific approval unless the later user approval explicitly
includes it.

## Task 10: Assemble and verify compatibility Candidate A

**Files:** all production/test files changed in Tasks 1-9.

**Step 1: Run focused acceptance once more**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_worker_command_jobs_schema.py \
  tests/test_worker_command_jobs.py \
  tests/test_worker_command_executor.py \
  tests/test_worker_command_mode_exclusivity.py \
  tests/test_worker_command_reconciliation.py \
  tests/test_trading_settings.py \
  tests/test_web_app.py -k 'worker_command or sync_deepcoin or close_bound_position or recovery_live_submit or trade_signal_process_next or lifespan' \
  tests/test_web_assets_smoke.py -k 'idempotency or sync_deepcoin or close_bound_position or recovery_live_submit' \
  tests/test_runtime_event_loop_blocking_census.py
```

Expected: all focused tests pass. The standalone authority test still has the
one deliberate strict xfail until Task 13 hardening.

**Step 2: Run Candidate A's one final full suite**

```bash
./.venv/bin/python -m pytest -q
git diff --check
```

Expected: zero failures, only established skips/warnings, and no unexpected
XPASS. Record exact counts/runtime. If production code changes after this run,
Candidate A is invalid and this step must be repeated once on the new candidate.

**Step 3: Commit any test-only assembly changes**

Stage explicit paths only. If nothing changed, do not create an empty commit.
Record exact Candidate A SHA in the status file as `phase_6a_compat_candidate`.

## Task 11: L3 migration and rollback rehearsal on a production copy

**Files:**

- Modify only evidence/status documentation after the rehearsal; do not modify
  the local database.

**Step 1: Push Candidate A by fast-forward only**

Verify the remote is an ancestor, then push the reviewed local commits to
`codex/deepcoin-auto-trading-v1`. Stop if not a fast-forward. Do not force push.

**Step 2: Create server backup and two rehearsal copies**

Using SQLite online backup, create:

- immutable backup of production `research.db`;
- migration rehearsal copy;
- rollback rehearsal copy derived from the migrated rehearsal copy.

Store raw output under
`/opt/telegram-kol-analyzer/data/backups/phase6a-<candidate-sha>-<utc>/`.
Never run bootstrap against the live DB during rehearsal.

**Step 3: Capture before evidence**

On the immutable backup and rehearsal copy record:

- exact candidate SHA and database byte size;
- `PRAGMA quick_check`;
- table/index/trigger schema;
- counts for every table;
- targeted hashes for critical business tables, including raw messages,
  recognition decisions, strategy instances/lifecycles, trade signals,
  position mutation intents, execution events/bindings/order legs, management
  batches/legs/components, and protection tables.

Do not hash every table unless counts or quick check reveal an anomaly.

**Step 4: Apply bootstrap twice to the migration copy**

Use Candidate A's normal `create_session_factory()`/`init_db()` entry point.
Verify:

- quick check remains `ok` after each pass;
- only `worker_command_jobs` and its indexes/constraints are added;
- new table count is zero;
- all pre-existing counts and targeted hashes match before;
- second application produces no schema/data delta.

**Step 5: Rehearse physical rollback on the rollback copy**

Drop only the new table on the rollback copy. Verify quick check, original
schema, counts, and targeted hashes match the immutable backup. Preserve all
three DB files and a bounded JSON summary.

Any mismatch is a hard stop: keep Phase 6A `in_progress`, record exact evidence,
do not deploy.

## Task 12: Deploy Candidate A dormant, then prove shadow and queue

**Step 1: Prove the safe deployment window**

At one pre-deploy checkpoint verify exact production SHA, service health,
`message_lock_mode=global`, `message_pipeline_mode=queue`,
`worker_command_mode` absent/default-inline, `active_write_count=0`, no active
management batch/mutation, queue backlog state, WAL mode, and quick check. An
incomplete query gets one retry, then hard stop.

**Step 2: Deploy exact Candidate A through the gated updater**

Use `scripts/server_git_update.ps1` or the existing gated shell wrapper with
the exact 40-hex `EXPECTED_COMMIT`. Never pull manually. Verify updater exit
code, production HEAD exact, service active, and the new table/indexes. Keep
`worker_command_mode=inline`; topology remains one service.

**Step 3: Enable shadow through the settings API**

At a second quiet-window check, switch only `worker_command_mode` to `shadow`.
Re-read settings and prove the two required existing modes remain global/queue.
Observe available real actions without manufacturing a trade:

- require at least one real sync command;
- for close/recovery/process-next, use a naturally user-authorized action if it
  occurs; otherwise record no production sample and rely only on injected/local
  parity until the owner supplies an authorized action;
- every observed shadow row is non-claimable and matches the inline status/body
  fingerprint;
- no duplicate domain/exchange event appears.

If a required parity sample is unavailable and the plan's completion gate
requires it, stop in shadow and ask for an explicit safe action; do not create a
position or order for coverage.

**Step 4: Cut over to queue only in a proven quiet window**

Require zero claimed/executing commands, zero unresolved shadow mismatch, zero
active exchange write, and no active management batch. Switch only
`worker_command_mode=queue`, then verify:

- one consumer owns each command;
- Web request tasks make no domain/exchange call;
- success/error contracts match shadow;
- backlog, duplicate settlements, and SQLite_BUSY remain zero;
- direct Deepcoin history shows no duplicate for every observed write-capable
  command.

**Step 5: Rehearse crash recovery safely**

On a separate server-side copy with injected fake adapters, terminate a worker
once before `executing` and once after `executing`. Prove pre-execution reclaim
and post-boundary `uncertain` without replay. Preserve evidence. In production,
perform one gated service restart only with zero claimed/executing commands;
prove pending backlog resumes, uncertain rows remain untouched, modes remain
global/queue/queue, and topology stays monolithic.

Do not interrupt a real exchange write to manufacture uncertainty.

## Task 12A: Add the owner-approved exchange-read-only shadow probe

> **For Codex:** Execute this supplement with `executing-plans` and
> `test-driven-development`. The approved design is
> `docs/plans/2026-08-22-phase-6a-safe-sync-shadow-probe-design.md`. Do not
> start Claude, subagents, background agents, or a parallel implementation
> session.

This task exists only because the ordinary sync route cannot satisfy the
owner's Task 12 sample authorization while production liveness is live. The
probe is migration-only and must be removed in Candidate B. Requests without
the probe header keep the exact existing full sync behavior.

### Task 12A.1: Make manual-close reconciliation explicitly mutation-free

**Files:**

- Modify first: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`

**Step 1: Write one focused failing test**

Build a missing-position binding with a visible pending entry order and a fake
client whose cancel methods record calls. Call:

```python
result = sync_manual_closed_deepcoin_positions(
    session_factory,
    client=client,
    synced_at=NOW,
    allow_exchange_mutations=False,
)
```

Assert the client receives zero cancel/submit/amend calls, the pending leg is
not terminalized, the binding remains fail-closed/open, and permitted local
reconciliation fields are committed. Add a companion assertion that omitting
the keyword retains the existing cancellation behavior and call ordering.

**Step 2: Verify RED**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  -k 'manual_closed and exchange_mutations'
```

Expected RED: `sync_manual_closed_deepcoin_positions()` rejects the new
keyword.

**Step 3: Implement the minimum guard**

Add a keyword-only parameter whose default preserves current behavior:

```python
def sync_manual_closed_deepcoin_positions(
    session_factory,
    *,
    client,
    synced_at=None,
    allow_exchange_mutations: bool = True,
):
```

Call `_cleanup_terminal_lifecycle_entry_exposure` and
`_cleanup_missing_position_deferred_entries` only when the flag is true. Use
an empty cleanup-status map otherwise. Do not change any other branch or the
decorator/lock boundary.

**Step 4: Verify GREEN and the existing cleanup slice**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  -k 'manual_closed or terminal_entry_cleanup'
```

**Step 5: Commit explicitly**

```bash
git add tests/test_execution_bindings.py \
  src/telegram_kol_research/execution_bindings.py
git diff --cached --name-only
git commit -m "feat: isolate mutation-free manual close sync"
```

### Task 12A.2: Add a bounded reconcile-only sync policy

**Files:**

- Modify first: `tests/test_worker_command_executor.py`
- Modify: `src/telegram_kol_research/worker_command_executor.py`

**Step 1: RED — normal policy remains byte-for-byte compatible**

Extend the current sync-adapter characterization so an empty request still
calls, in order, the write-capable reconciler, manual-close sync with its
default mutation policy, then the three configured notification deliverers.
The request/result body and error mapping must stay unchanged.

Run:

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_worker_command_executor.py -k 'sync_adapter and full_policy'
```

Expected RED: the adapter does not yet expose or validate an effects policy.

**Step 2: GREEN — introduce strict policy parsing only**

Accept exactly `{}` as `full` and
`{"effects_policy":"reconcile_only"}` as the temporary probe request. Reject
every other sync request before constructing a Deepcoin client. Pass the claim
request into `_execute_sync`; keep the full branch identical.

**Step 3: RED — reconcile-only cannot reach mutation or notification**

Add a test client that supplies every required read method but raises
`AssertionError` from submit/cancel/amend/close methods. Require the probe to:

- call `reconcile_deepcoin_execution_bindings_read_only` rather than the full
  reconciler;
- call manual-close sync with `allow_exchange_mutations=False`;
- omit the contract-spec provider;
- call no notification deliverer;
- return the existing six-key `200` body;
- map incomplete read evidence through the existing bounded failure contract;
- execute the blocking unit exactly once.

Run the new test and observe the expected failure before production changes.

**Step 4: GREEN — add the minimum read-only orchestration**

Add a small explicit read-only client facade that forwards only:

```text
list_positions, list_open_orders, read_trigger_orders_pending,
list_trigger_orders_pending, list_order_history, list_trade_fills,
list_trigger_order_history, list_position_history
```

Any other attribute is unavailable. Use it only in the reconcile-only branch.
Call the existing read-only reconciler and mutation-disabled manual-close sync.
Do not catch a mutation attempt and retry through the full branch.

**Step 5: Verify and commit**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_worker_command_executor.py -k sync
git add tests/test_worker_command_executor.py \
  src/telegram_kol_research/worker_command_executor.py
git diff --cached --name-only
git commit -m "feat: add reconcile-only sync policy"
```

### Task 12A.3: Gate the temporary real HTTP shadow probe

**Files:**

- Modify first: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`

**Step 1: RED — freeze the no-header path**

Assert a normal sync request still enqueues `{}` and invokes the current full
reconciler, manual-close sync, and enabled notification delivery in the frozen
order. This test must pass before the route is edited and continue to pass
afterward.

**Step 2: RED — add exact probe admission tests**

For `X-Worker-Command-Probe: reconcile-only`, require:

- `worker_command_mode=shadow`;
- `system_operator_bot_enabled(app.state.notification_bot_config) == false`;
- no JSON payload;
- shadow request JSON exactly
  `{"effects_policy":"reconcile_only"}`;
- the reconcile-only orchestration result settles the one shadow job with the
  exact returned status/body fingerprint;
- attribution, protection, and cleanup delivery calls remain zero.

Require an unknown header value to return bounded `400`, and an otherwise valid
probe in inline/queue mode or with the notification bot enabled to return
bounded `409`. Each refusal occurs before enqueue, reconciliation, or Deepcoin
client creation.

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_web_app.py -k 'sync_deepcoin and (probe or no_header)'
```

Expected RED: the header is currently ignored and the shadow request remains
`{}`.

**Step 3: Implement the migration-only Web branch**

Parse the header before `_prepare_web_worker_command`. Load settings through
the existing async/off-thread pattern for the gate. For an admitted probe, pass
the internal request policy to durable admission and run the bounded
reconcile-only orchestration. Requests without the header remain on the exact
existing route body. Do not add a UI control or persist a new trading setting.

**Step 4: Verify focused route and event-loop coverage**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_web_app.py \
  -k 'worker_command or sync_deepcoin or lifespan'
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_worker_command_executor.py -k sync \
  tests/test_runtime_event_loop_blocking_census.py
```

**Step 5: Commit explicitly**

```bash
git add tests/test_web_app.py src/telegram_kol_research/web_app.py
git diff --cached --name-only
git commit -m "feat: gate phase 6a safe sync probe"
```

### Task 12A.4: Assemble the replacement Candidate A

**Step 1: Run the affected acceptance slice**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  -k 'manual_closed or terminal_entry_cleanup'
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_worker_command_executor.py \
  tests/test_web_app.py \
  -k 'worker_command or sync_deepcoin or close_bound_position or recovery_live_submit or trade_signal_process_next or lifespan'
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_process_boundary_authority.py \
  tests/test_runtime_event_loop_blocking_census.py
```

Expected: zero failures and the existing Candidate A authority strict-xfail
only. Run `git diff --check`.

**Step 2: Run one new final full suite**

```bash
/Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q
```

Record exact counts and runtime. If production code changes afterward, run the
affected focused tests and exactly one new final full suite on the replacement
candidate.

**Step 3: Record and push the replacement candidate**

Use explicit paths for any test-only assembly commit. Verify the actual remote
is the local candidate's ancestor, then push fast-forward only to
`codex/deepcoin-auto-trading-v1`. The original Candidate A remains the rollback
target until the replacement deploy passes.

### Task 12A.5: Rehearse, deploy, and collect exactly one real sample

**Step 1: Production-copy rehearsal**

Create a fresh online production backup and a rehearsal copy. Run the admitted
probe orchestration against the copy with a captured/fake read-only exchange
snapshot and mutation-trap client. Record `PRAGMA quick_check`, before/after
counts and targeted hashes for worker jobs, bindings/legs, lifecycles, execution
events, protection tables, and both notification outboxes. Prove no mutation
method and no notification deliverer was called. The prior schema/physical
rollback rehearsal remains authoritative because no schema changed.

**Step 2: Pre-deploy gate and exact-SHA deploy**

Require exact current production SHA, active service, global/queue/shadow,
notification bot effectively disabled, zero active management/write state,
zero claimed/executing worker commands, WAL, quick check, complete queue state,
and a recoverable rollback SHA. Use only the gated updater with the exact
replacement Candidate A SHA.

**Step 3: Freeze exact old outbox state**

Using SQLite URI `mode=ro` plus `query_only=1`, record a deterministic digest of
every pre-existing attribution/protection outbox row including id, status,
notification timestamps, error, claim/lease fields where present, and payload
fingerprint. Record counts by state. `total_changes` must remain zero.

**Step 4: Recheck and invoke once**

Immediately recheck notification disabled state, zero active management/write
state, modes, and worker inflight counts. Then invoke exactly one real
`POST /api/execution/sync-deepcoin` with the reconcile-only probe header and a
fresh idempotency key. Do not retry an accepted request. An incomplete response
or evidence query gets at most one reasoned read-only evidence retry, not a
second sync.

**Step 5: Verify parity and absence of forbidden effects**

Require exactly one non-claimable shadow job whose request and terminal
status/body fingerprint match the HTTP response. Recompute the exact old-outbox
digest and require equality. Verify no notification delivery/claim, no exchange
submit/cancel/amend/close, no duplicate domain/execution event, no
`SQLITE_BUSY`, and complete read-only Deepcoin order/fill/trigger/position
history spanning the sample. Normal permitted DB reconciliation is recorded,
not treated as drift.

Any failed or incomplete gate stops in shadow, keeps Phase 6A `in_progress`,
records evidence, and does not proceed to queue. If the sample passes, resume
Task 12 Step 4; Candidate B must delete the probe branch and its tests while
retaining the normal-contract characterization.

## Task 13: Remove migration-only Web authority for Candidate B

**Files:**

- Modify first: `tests/test_process_boundary_authority.py`
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify as needed: `src/telegram_kol_research/trading_settings.py`
- Modify as needed: `tests/test_trading_settings.py`

Proceed only after Candidate A queue evidence is complete and rollback SHA is
recorded.

**Step 1: RED — make the authority gate unconditional**

Delete `KNOWN_BLOCKING_WEB_ROUTE_AUTHORITY` and the strict xfail. Require the
scanner to report `{}`. Add route tests proving the hardened candidate refuses
migration-only inline/shadow execution rather than calling the legacy helper.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_process_boundary_authority.py \
  tests/test_web_app.py -k 'worker_command and hardened'
```

Expected RED: legacy Web authority paths still exist.

**Step 2: Remove the legacy direct branch**

Delete the migration-only inline/shadow route executor and every direct route
reference to the four authority functions. The queue path and worker adapters
remain unchanged. In the hardened candidate, an unexpected inline/shadow
setting must fail closed without an exchange call. Operational rollback is the
exact Candidate A SHA through the gated updater, followed by the quiet-window
inline switch; do not retain a hidden direct callback to make rollback easier.

**Step 3: Run affected focused tests**

```bash
./.venv/bin/python -m pytest -q \
  tests/test_process_boundary_authority.py \
  tests/test_worker_command_jobs.py \
  tests/test_worker_command_executor.py \
  tests/test_worker_command_mode_exclusivity.py \
  tests/test_web_app.py -k 'worker_command or sync_deepcoin or close_bound_position or recovery_live_submit or trade_signal_process_next or lifespan' \
  tests/test_web_assets_smoke.py -k 'idempotency or sync_deepcoin or close_bound_position or recovery_live_submit' \
  tests/test_runtime_event_loop_blocking_census.py
```

Expected: authority tests pass normally, with no xfail/allowlist; all focused
tests pass.

**Step 4: Run Candidate B's one final full suite**

```bash
./.venv/bin/python -m pytest -q
git diff --check
```

Record exact counts/runtime. Any later production-code change invalidates this
candidate and requires affected focused tests plus one new final full suite.

**Step 5: Commit, fast-forward push, and deploy exact Candidate B**

```bash
git add <each exact changed path>
git diff --cached --name-only
git commit -m "feat: enforce worker-only exchange command authority"
```

Verify remote ancestry, push without force, and deploy only via the gated
updater with exact SHA. Verify production remains
`message_lock_mode=global`, `message_pipeline_mode=queue`,
`worker_command_mode=queue`, and one monolithic service. Phase 6 systemd/process
work has not started.

## Task 14: L2 operational observation on the hardened L3 candidate

Run one quiet server-side monitor and inspect at no more than the normal
post-cutover, post-restart, and observation-end checkpoints. Observe 30
continuous minutes and at least five real Telegram messages, trying to cover two
chats. If five messages do not arrive in 30 minutes, stop at 30 minutes, keep
Phase 6A `in_progress`, and record the limited traffic.

At observation end record:

- exact deployed SHA and time window;
- the three runtime modes and unchanged monolith topology;
- command counts by type/status and oldest backlog age;
- claimed/executing/uncertain counts;
- attempts, stale pre-execution reclaims, duplicate command/result count;
- SQLite_BUSY and other database errors;
- event-loop stall/census anomalies;
- message queue backlog/duplicate processing for the unchanged message path;
- direct exchange order/fill/trigger/position history for every observed
  write-capable command;
- migration backup/rehearsal/rollback evidence paths;
- exact Candidate A rollback SHA and proof that rollback preconditions can be
  evaluated.

Any unresolved `uncertain`, duplicate execution, Web authority path, incomplete
Deepcoin response after one retry, SQLite_BUSY anomaly, semantic drift, unsafe
window, or failed rollback check leaves the phase `in_progress` and stops Phase
6 from resuming.

## Task 15: Update the canonical status and stop

**Files:**

- Modify: `docs/runtime-serialization-remediation-status.md`

If every gate passes:

- set Phase 6A completed and release `claimed_by`;
- set `last_completed_phase: 6a` and exact Candidate B SHA;
- restore `current_phase: 6`, `phase_name: process-separation`,
  `phase_status: planned`, and the existing Phase 6 file pointer;
- record exact claim/design/plan/Candidate A/Candidate B/deployed SHAs;
- record focused/full-suite counts, L3 rehearsal and rollback paths, deploy
  window, topology, modes, SQLite_BUSY, uncertain/duplicate counts, direct
  exchange evidence, restart proof, observation traffic, and no semantic drift;
- update the ledger so Phase 6A is completed and Phase 6 is planned to resume at
  Task 1 authority gate.

If any gate is incomplete or failed:

- keep `current_phase: 6a`, `phase_status: in_progress`;
- release `claimed_by` when stopping;
- record the exact blocker, safe live mode/SHA, rollback state, evidence path,
  tests actually run, and uncompleted task;
- do not advance the Phase 6 pointer.

Append one honest `local_tests` entry and one `server_verification` entry. Stage
only the status file, inspect it, commit, and fast-forward push the status commit
only if deployment/integration has already been authorized by the implementation
approval:

```bash
git add docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "docs: record phase 6a durable boundary result"
```

Send the single required stop notification and return control to the user. Do
not start Phase 6 in the same turn.
