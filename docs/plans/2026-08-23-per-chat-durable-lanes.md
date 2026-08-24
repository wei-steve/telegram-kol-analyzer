# Per-Chat Durable Lanes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans`
> (`superpowers:executing-plans`) to implement this plan task-by-task.

**Goal:** Correct `KeyedAsyncLockRegistry.lock_all()`, preserve durable same-chat
ordering, and safely run at most three work-conserving chat lanes in production.

**Architecture:** `message_processing_jobs` remains the sole durable ordering
authority. A writer-preference shared/exclusive admission barrier protects
process-local global/per-chat operations, while the single worker process uses
atomic SQLite claims and an explicit dynamically loaded cap. Settings changes
are routed to the ingest owner and persisted as one expected-state transaction.

**Tech Stack:** Python 3.12, asyncio, FastAPI/httpx, SQLAlchemy, SQLite, pytest,
systemd split worker/Web/ingest topology.

---

## Global Rules

- Work only in
  `/Users/steven/Documents/telegram获取消息/.worktrees/runtime-serialization`.
- Do not modify `docs/runtime-serialization-remediation-status.md` or any
  completed Phase 0-6 file.
- Claim `docs/per-chat-durable-lanes-status.md` before touching any other file.
- Stop on dirty tree, unexpected HEAD/upstream/remote, Git lock, or another
  owner. Do not pull, reset, clean, stash, or repair a mismatch.
- Never run `git add -A`. Stage only the paths named by the current task and run
  `git diff --cached --name-only` before every commit.
- Every production-code edit requires its focused failing test first.
- Run focused tests during Tasks 2-9. Run the complete suite only once on the
  assembled final candidate in Task 10.
- If production code changes after the Task 10 full suite, run the affected
  focused tests and one new final full suite.
- Do not deploy, restart, change production settings, connect to Telegram, or
  query the exchange before the plan reaches the explicit production tasks.
- Any schema or production-data mutation requirement is a stop condition. This
  work is L2, not L3.
- Any recognition, MiMo, strategy, position, management, execution, retry, or
  exchange-write semantic change is a misread of the plan and must stop.

Set the local interpreter once per shell without creating a new environment:

```bash
PROJECT_PYTHON=/Users/steven/Documents/telegram获取消息/.venv/bin/python
export PYTHONPATH="$PWD/src"
```

## Task 1: Claim the Independent Workstream

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Run the read-only claim gate

Run:

```bash
pwd
git status --porcelain=v2 --branch
git rev-parse HEAD
git rev-parse '@{upstream}'
git ls-remote --heads origin refs/heads/codex/deepcoin-auto-trading-v1
git log -1 --format=%H -- docs/per-chat-durable-lanes-status.md
rg -n '^(workstream_status|claimed_by|current_task):' \
  docs/per-chat-durable-lanes-status.md
git_dir=$(git rev-parse --git-dir)
common_dir=$(git rev-parse --git-common-dir)
find "$git_dir" "$common_dir" -maxdepth 4 -type f -name '*.lock' -print
```

Expected:

- exact fixed worktree;
- clean tree;
- HEAD equals the latest commit touching the independent status file;
- upstream and the remote branch tip equal the remote baseline recorded by the
  planning handoff; local planning-only commits may be ahead;
- `workstream_status: planned`, `claimed_by: null`;
- no Git lock files.

Stop on any mismatch.

### Step 2: Record the claim only

Set:

```yaml
workstream_status: claimed
claimed_by: <current-codex-session-id>
current_task: task-2-keyed-admission-barrier
```

Also append a history entry with the exact baseline SHA and timestamp. Change no
other file.

### Step 3: Validate and commit the claim

Run:

```bash
git diff --check -- docs/per-chat-durable-lanes-status.md
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "chore: claim per-chat durable lanes workstream"
```

Expected staged list:

```text
docs/per-chat-durable-lanes-status.md
```

### Step 4: Keep the claim local until exact-SHA integration authorization

Do not push the planning or claim commits merely to advertise ownership. The
shared checkout and committed status file are the ownership authority. Record
the claim commit SHA and continue locally.

Optional read-only confirmation:

```bash
git fetch origin codex/deepcoin-auto-trading-v1
git rev-parse origin/codex/deepcoin-auto-trading-v1
```

Expected: the remote remains the handoff baseline unless the owner separately
authorized and identified an exact integration SHA. Stop if it moved; do not
pull or merge it into this shared worktree.

## Task 2: Replace Snapshot `lock_all()` with Writer-Preference Admission

**Files:**

- Modify: `tests/test_keyed_async_locks.py`
- Modify: `src/telegram_kol_research/keyed_async_locks.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write the first RED tests

Add deterministic event-driven tests named:

```python
def test_lock_all_held_blocks_a_future_key(): ...
def test_waiting_lock_all_blocks_a_future_key_until_old_readers_drain(): ...
def test_waiting_lock_all_is_not_starved_by_continuous_new_keys(): ...
```

Do not use timing alone as the assertion. Use `asyncio.Event` objects to prove:

- an old key is active;
- the writer has announced intent;
- a future key has attempted entry;
- the future key remains outside until the writer exits.

Replace the old
`test_lock_all_acquires_locks_in_deterministic_sorted_order` test. That test
requires the snapshot implementation that this task intentionally deletes.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_keyed_async_locks.py \
  -k 'future_key or not_starved' -vv
```

Expected: the future key enters while the writer waits or holds, so at least one
new test fails against the snapshot implementation.

Record the command and exact failure summary in the workstream status history.

### Step 3: Implement the minimal shared/exclusive barrier

In `KeyedAsyncLockRegistry.__init__`, add condition-protected counters:

```python
self._admission = asyncio.Condition()
self._active_readers = 0
self._waiting_writers = 0
self._writer_active = False
```

Add private async context managers with this state machine:

```python
@asynccontextmanager
async def _shared_admission(self) -> AsyncIterator[None]:
    async with self._admission:
        await self._admission.wait_for(
            lambda: not self._writer_active and self._waiting_writers == 0
        )
        self._active_readers += 1
    try:
        yield
    finally:
        async with self._admission:
            self._active_readers -= 1
            if self._active_readers == 0:
                self._admission.notify_all()

@asynccontextmanager
async def _exclusive_admission(self) -> AsyncIterator[None]:
    acquired = False
    async with self._admission:
        self._waiting_writers += 1
        try:
            await self._admission.wait_for(
                lambda: not self._writer_active and self._active_readers == 0
            )
            self._waiting_writers -= 1
            self._writer_active = True
            acquired = True
        except BaseException:
            self._waiting_writers -= 1
            self._admission.notify_all()
            raise
    try:
        yield
    finally:
        if acquired:
            async with self._admission:
                self._writer_active = False
                self._admission.notify_all()
```

Integrate the shared admission around the entire per-key context, including the
wait for its key lock. Replace `_all_context()` with the exclusive admission;
do not enumerate `_locks`.

Keep one package-private shared-admission/key-only API for
`MessageLockProvider` Task 4, but do not expose mutable counters.

### Step 4: Run GREEN and the entire registry slice

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv
```

Expected: all registry tests pass.

### Step 5: Commit the checkpoint

Update `current_task: task-3-cancellation-cleanup`, record RED/GREEN evidence,
then run:

```bash
git add \
  tests/test_keyed_async_locks.py \
  src/telegram_kol_research/keyed_async_locks.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: block future keys during cross-chat admission"
```

Expected staged list contains exactly those three paths.

## Task 3: Prove Multiple Writers, Cancellation, Exceptions, and Cleanup

**Files:**

- Modify: `tests/test_keyed_async_locks.py`
- Modify: `src/telegram_kol_research/keyed_async_locks.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED tests for exceptional paths

Add:

```python
def test_multiple_lock_all_callers_are_exclusive_without_deadlock(): ...
def test_cancelled_waiting_lock_all_restores_reader_admission(): ...
def test_cancelled_held_lock_all_releases_exclusive_admission(): ...
def test_exception_inside_lock_all_releases_exclusive_admission(): ...
def test_cancelled_key_waiter_releases_ref_and_shared_admission(): ...
def test_registry_cleans_keys_after_mixed_reader_writer_cancellation(): ...
```

Use `asyncio.wait_for` only as a deadlock tripwire, not as the ordering proof.
Assert `known_key_count() == 0` after all tasks settle.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py \
  -k 'cancel or exception or multiple_lock_all or mixed_reader' -vv
```

Expected: at least one cancellation/cleanup assertion fails before hardening.

### Step 3: Make cancellation bookkeeping exact

Adjust the barrier so:

- `waiting_writers` is decremented once on every acquisition/cancellation path;
- `writer_active` is cleared only by its holder;
- partially acquired per-key references are released;
- condition notifications occur after every state change that can unblock a
  waiter;
- counters never become negative.

Add a pure in-memory `snapshot()` returning counts needed by Task 7:

```python
{
    "active_shared_admissions": self._active_readers,
    "waiting_exclusive_admissions": self._waiting_writers,
    "exclusive_admission_active": self._writer_active,
    "known_key_count": len(self._locks),
}
```

### Step 4: Run GREEN

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv
```

Expected: all pass and no pending-task warnings.

### Step 5: Commit

Set `current_task: task-4-provider-integration`, then stage exactly:

```bash
git add \
  tests/test_keyed_async_locks.py \
  src/telegram_kol_research/keyed_async_locks.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "test: harden keyed admission cancellation"
```

## Task 4: Make Global and Per-Chat Provider Modes Share Admission

**Files:**

- Modify: `tests/test_live_listener_chat_isolation.py`
- Modify: `tests/test_keyed_async_locks.py`
- Modify: `src/telegram_kol_research/message_lock_provider.py`
- Modify: `src/telegram_kol_research/keyed_async_locks.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED provider tests

Add deterministic tests proving:

```python
def test_provider_lock_all_waits_for_global_operation_and_blocks_new_global_work(): ...
def test_provider_lock_all_waits_for_per_chat_operations_and_blocks_new_chat(): ...
def test_provider_resolves_mode_only_after_shared_admission(): ...
def test_global_rollback_serializes_two_different_chats_again(): ...
```

The stale-mode test must create a caller before the mode changes, hold it
outside admission, perform the transition, and prove it uses the mode observed
inside admission rather than the old value.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_live_listener_chat_isolation.py \
  tests/test_keyed_async_locks.py \
  -k 'provider or rollback or resolves_mode' -vv
```

Expected: global work bypasses registry admission or mode resolves too early.

### Step 3: Implement a provider-owned async context

Change `MessageLockProvider.__call__` to return an async context that:

1. enters the registry shared admission;
2. loads `message_lock_mode` inside admission;
3. acquires the global lock or the registry key-only lock;
4. holds both through the caller context.

Change `lock_all()` to enter registry exclusive admission and then the legacy
global lock. Do not branch `lock_all()` on the configured mode.

Keep `mode()` for existing inspection callers, but do not use its result before
admission in `__call__`.

### Step 4: Run GREEN and listener/reconcile compatibility

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_keyed_async_locks.py \
  tests/test_live_listener_chat_isolation.py \
  tests/test_reconcile_live_history.py \
  tests/test_position_authority_boundary_coverage.py -q
```

Expected: all pass; global behavior and per-chat isolation remain intact.

### Step 5: Commit

Set `current_task: task-5-parallel-chat-setting`, then run:

```bash
git add \
  tests/test_live_listener_chat_isolation.py \
  tests/test_keyed_async_locks.py \
  src/telegram_kol_research/message_lock_provider.py \
  src/telegram_kol_research/keyed_async_locks.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: share admission across message lock modes"
```

## Task 5: Add the Strict Dynamic Parallel-Chat Setting

**Files:**

- Modify: `tests/test_trading_settings.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED settings tests

Add:

```python
def test_message_parallel_chat_limit_defaults_to_compatibility_twenty(): ...
def test_message_parallel_chat_limit_round_trips(): ...

@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 21, 1.0, "3", None, [], {}]
)
def test_message_parallel_chat_limit_rejects_invalid_values(value): ...
```

Also prove saving an unrelated field preserves both `message_lock_mode` and the
new cap.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_trading_settings.py \
  -k 'parallel_chat_limit' -vv
```

Expected: missing attribute or invalid values are accepted/fall back.

### Step 3: Implement the field and strict parser

Add:

```python
message_processing_max_parallel_chats: int = 20
```

Parse it with an exact-type bounded helper:

```python
def _bounded_int_setting(
    value: Any, *, field_name: str, minimum: int, maximum: int
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f"{field_name} must be an integer from {minimum} to {maximum}"
        )
    return value
```

Use `minimum=1`, `maximum=20`. `asdict()` will include it in API/storage
round-trips.

### Step 4: Run GREEN

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_trading_settings.py -q
```

Expected: all pass.

### Step 5: Commit

Set `current_task: task-6-work-conserving-worker`, then run:

```bash
git add \
  tests/test_trading_settings.py \
  src/telegram_kol_research/trading_settings.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: configure bounded parallel chat lanes"
```

## Task 6: Make the Durable Worker Work-Conserving at the Dynamic Cap

**Files:**

- Modify: `tests/test_message_processing_worker.py`
- Modify: `tests/test_message_pipeline_mode_exclusivity.py`
- Modify: `src/telegram_kol_research/message_processing_worker.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED durable-lane tests

Add tests for:

```python
def test_worker_loop_never_exceeds_three_active_chat_lanes(): ...
def test_slow_lane_does_not_prevent_two_free_slots_from_refilling(): ...
def test_worker_loop_reloads_parallel_limit_before_each_refill(): ...
def test_lowered_limit_stops_new_claims_without_cancelling_inflight(): ...
def test_live_claim_blocks_later_same_chat_job_while_other_chats_progress(): ...
def test_retry_not_due_blocks_later_same_chat_job_while_other_chats_progress(): ...
```

For the slow-lane test create at least five chats. Hold chat 1, allow chats 2
and 3 to finish, and prove chats 4 and 5 start before chat 1 is released. Track
the active peak and assert it is exactly three.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py \
  -k 'three_active or slow_lane or reloads_parallel or lowered_limit or blocks_later' \
  -vv
```

Expected: the current batch-gather loop blocks chats 4 and 5 behind the slow
member, and the cap remains hidden at 20.

### Step 3: Add pure in-memory activity tracking

Add `MessageProcessingActivity` with event-loop-owned state and a pure
`snapshot()`:

```python
class MessageProcessingActivity:
    def apply_limit(self, limit: int, *, applied_at: datetime) -> None: ...
    def enter(self, chat_id: int) -> None: ...
    def leave(self, chat_id: int) -> None: ...
    def note_refill(self, claimed: int) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
```

It stores counts, not chat IDs in the returned snapshot. Reset
`peak_active_chat_lanes_since_limit_change` only when the applied limit changes.

Wrap each `run_claim()` body with `activity.enter(claim.chat_id)` and a
`finally: activity.leave(claim.chat_id)`.

### Step 4: Implement single-claim slot tasks

Keep `run_message_processing_worker_tick()` as the existing focused primitive.
Add an optional activity dependency and retain its `limit` argument for tests.

Refactor `run_message_processing_worker_loop()` to maintain a set of slot tasks.
Each slot runs one tick with `limit=1`. The loop:

```python
while True:
    settings = await asyncio.to_thread(load_trading_settings, session_factory)
    cap = settings.message_processing_max_parallel_chats
    activity.apply_limit(cap, applied_at=utc_now())

    if settings.message_pipeline_mode != "queue":
        if in_flight:
            await asyncio.gather(*in_flight)
        return

    while len(in_flight) < cap:
        in_flight.add(asyncio.create_task(
            run_message_processing_worker_tick(
                session_factory,
                limit=1,
                activity=activity,
                **tick_kwargs,
            )
        ))

    done, in_flight = await asyncio.wait(
        in_flight,
        timeout=max(0.01, float(interval_seconds)),
        return_when=asyncio.FIRST_COMPLETED,
    )
    claimed = 0
    for task in done:
        claimed += task.result().claimed
    activity.note_refill(claimed)
    if done and claimed == 0:
        await asyncio.sleep(max(0.01, float(interval_seconds)))
```

The final implementation may use a mutable set rather than reassigning the
`pending` return value, but must preserve these semantics. Avoid a busy loop
when all single-claim ticks return zero.

In a cancellation `finally`, cancel and await all remaining slot tasks with
`return_exceptions=True`; do not settle or reset their durable claims.

### Step 5: Run GREEN and recovery slices

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py \
  tests/test_message_processing_shadow_enqueue.py -q
```

Expected: all pass, peak never exceeds configured cap, and no same-chat
overtaking occurs.

### Step 6: Commit

Set `current_task: task-7-restart-cancellation`, then run:

```bash
git add \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py \
  src/telegram_kol_research/message_processing_worker.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: refill bounded durable chat lanes"
```

## Task 7: Prove Cancellation, Restart, and Stale-Lease Recovery

**Files:**

- Modify: `tests/test_message_processing_worker.py`
- Modify: `src/telegram_kol_research/message_processing_worker.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED recovery tests

Add:

```python
def test_cancelled_loop_leaves_claim_for_stale_recovery(): ...
def test_cancelled_first_job_keeps_second_same_chat_blocked(): ...
def test_stale_recovery_processes_first_once_then_releases_second(): ...
def test_second_worker_cannot_duplicate_a_live_claim(): ...
```

Use an async fake processor that can actually be cancelled. Do not claim that
the test stops an arbitrary synchronous `to_thread` call. Assert claim token,
status, attempt count, job order, and processor invocation identity.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_message_processing_worker.py \
  -k 'cancelled_loop or stale_recovery or second_worker' -vv
```

Expected: an unfinished slot task leaks, is eagerly reset, or cannot be
deterministically recovered before the cancellation cleanup is hardened.

### Step 3: Harden only scheduler cancellation

Make cancellation idempotently cancel/await slot wrappers. Do not change:

- lease duration;
- retry count/backoff;
- claim token settlement;
- terminal status behavior;
- processor or exchange idempotency semantics.

### Step 4: Run GREEN

Run:

```bash
$PROJECT_PYTHON -m pytest tests/test_message_processing_worker.py -q
```

Expected: all pass.

### Step 5: Commit

Set `current_task: task-8-atomic-settings-transition`, then run:

```bash
git add \
  tests/test_message_processing_worker.py \
  src/telegram_kol_research/message_processing_worker.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "test: prove durable lane restart recovery"
```

## Task 8: Add the Ingest-Owned Atomic Settings Transition

**Files:**

- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED transaction tests

In `tests/test_trading_settings.py`, add:

```python
def test_concurrency_transition_writes_mode_and_cap_in_one_transaction(): ...
def test_concurrency_transition_rejects_expected_mode_mismatch_without_write(): ...
def test_concurrency_transition_rejects_expected_cap_mismatch_without_write(): ...
def test_global_to_per_chat_requires_both_target_and_expected_fields(): ...
def test_global_rollback_can_keep_cap_three(): ...
def test_fail_closed_rollback_can_set_global_and_cap_one_atomically(): ...
```

Use two session factories/connections to assert readers see either the full old
tuple or the full new tuple, never a partial pair.

### Step 2: Write RED process-owner API tests

In `tests/test_web_app.py`, add:

```python
def test_web_role_proxies_concurrency_transition_to_ingest_without_local_save(): ...
def test_ingest_role_holds_exclusive_message_admission_during_transition(): ...
def test_worker_role_refuses_direct_concurrency_transition(): ...
def test_transition_unknown_outcome_does_not_blindly_retry(): ...
def test_unrelated_settings_save_does_not_take_exclusive_admission(): ...
```

The ingest test must hold an old global/per-chat operation, start the settings
request, prove a future chat cannot enter, release the old operation, and then
observe the complete new tuple.

### Step 3: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  -k 'concurrency_transition or expected_cap or proxies_concurrency or exclusive_message_admission' \
  -vv
```

Expected: no atomic expected-state helper or ingest routing exists.

### Step 4: Implement the transactional helper

In `trading_settings.py`, factor the existing row serialization into a helper
usable inside an existing session. Add a concurrency-specific function that:

```python
with session_factory() as session:
    session.execute(text("BEGIN IMMEDIATE"))
    current = _load_trading_settings_in_session(session)
    _require_expected_concurrency_state(current, expected_mode, expected_cap)
    candidate = trading_settings_from_payload({**current.to_dict(), **payload})
    _persist_trading_settings_in_session(session, candidate, updated_at)
    session.commit()
```

Strip expected-state keys before candidate persistence. Reject missing paired
fields for `global -> per_chat`. Return the saved `TradingSettings`.

Do not change schema or add a settings row beyond the existing global row.

### Step 5: Implement bounded ingest routing

In `web_app.py`:

- add a strict localhost URL resolver for `/api/trading-settings` on port 8001;
- add an injectable requester for tests;
- bound response size and validate JSON, following the existing ingest refresh
  proxy pattern;
- in Web role, proxy actual concurrency transitions;
- in worker role, return 503 with a stable code;
- in ingest/all role, acquire `message_lock_provider.lock_all()`, reload current
  state, run all existing worker-command/MiMo validations unchanged, and call
  the transaction helper;
- avoid nested `lock_all()` when a payload contains both MiMo and concurrency
  fields.

Do not route unrelated settings writes through ingest and do not alter the
existing MiMo activation contract.

### Step 6: Run GREEN and full settings/Web focused slices

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  -k 'trading_settings or concurrency_transition or message_lock or queue_mode_api' \
  -q
```

Then run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_trading_settings.py \
  tests/test_live_listener_chat_isolation.py \
  tests/test_message_pipeline_mode_exclusivity.py -q
```

Expected: all selected tests pass.

### Step 7: Commit

Set `current_task: task-9-observability-authority`, then run:

```bash
git add \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: gate message concurrency transitions in ingest"
```

## Task 9: Expose Pure In-Memory Metrics and Lock Process Boundaries

**Files:**

- Modify: `tests/test_web_app.py`
- Modify: `tests/test_runtime_role_selection.py`
- Modify: `tests/test_process_boundary_authority.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Write RED observability and architecture tests

Add:

```python
def test_worker_loop_health_exposes_message_lane_activity_without_database(): ...
def test_ingest_loop_health_exposes_admission_state_without_database(): ...
def test_non_worker_roles_never_start_message_processing_slots(): ...
def test_concurrency_settings_route_cannot_reach_exchange_mutation(): ...
def test_ingest_lock_registry_is_not_claimed_as_cross_process_worker_lock(): ...
```

Monkeypatch the session factory and Deepcoin client to raise if the loop-health
endpoint touches either.

### Step 2: Run RED

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_web_app.py \
  tests/test_runtime_role_selection.py \
  tests/test_process_boundary_authority.py \
  -k 'lane_activity or admission_state or processing_slots or concurrency_settings_route' \
  -vv
```

Expected: metrics are absent or the architecture guard lacks the new route.

### Step 3: Wire pure snapshots

Create one `MessageProcessingActivity` in app state, pass it to the worker loop,
and include its snapshot only for worker/all roles. Include the registry
snapshot only for ingest/all roles.

Return no chat IDs. Do not load settings from the endpoint; report the last cap
actually applied by the scheduler.

Extend the static authority test so the new route cannot reach recognition,
position mutation, or Deepcoin write sinks. Preserve the exact role task sets.

### Step 4: Run GREEN and all affected focused tests

Run:

```bash
$PROJECT_PYTHON -m pytest \
  tests/test_keyed_async_locks.py \
  tests/test_live_listener_chat_isolation.py \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  tests/test_runtime_role_selection.py \
  tests/test_process_boundary_authority.py \
  tests/test_worker_loop_does_not_block_event_loop.py -q
```

Expected: all pass.

### Step 5: Commit the final production-code checkpoint

Set `current_task: task-10-final-candidate`, then run:

```bash
git add \
  tests/test_web_app.py \
  tests/test_runtime_role_selection.py \
  tests/test_process_boundary_authority.py \
  src/telegram_kol_research/web_app.py \
  docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: observe bounded message lane concurrency"
```

## Task 10: Assemble and Verify the Final Local Candidate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Inspect the exact candidate diff

Run:

```bash
git status --short --branch
git diff --check
git diff --stat <implementation-claim-commit>..HEAD
git diff --name-only <implementation-claim-commit>..HEAD
$PROJECT_PYTHON -m compileall -q src/telegram_kol_research tests
```

Expected:

- clean tracked tree before the status update;
- only the planned source, test, and independent workstream documentation;
- no schema, migration, model, execution, recognition, strategy, or original
  remediation pointer file;
- compileall exits zero.

### Step 2: Run the final focused acceptance once more

Run the Task 9 GREEN command. Expected: all pass.

### Step 3: Run exactly one complete suite

Run:

```bash
$PROJECT_PYTHON -m pytest -q
```

Expected: zero failures. Record exact passed/skipped/warnings count and elapsed
time. The historical baseline was 6162 passed and 1 skipped; do not require the
new count to equal it because this work adds tests.

### Step 4: Record the final candidate

Update status with:

- exact production-code candidate SHA;
- focused commands/results;
- full-suite command/result/time;
- compileall and diff-check results;
- explicit statements that schema and trading semantics did not change;
- `workstream_status: local_complete`;
- `current_task: task-11-review-push`.

This is documentation-only after the full suite and does not require rerunning
it.

### Step 5: Commit the status checkpoint

Run:

```bash
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record per-chat lane local candidate"
```

## Task 11: Independent Review, Push, and Deployment Authorization Gate

**Files:**

- Modify only if review finds documentation errors:
  `docs/per-chat-durable-lanes-status.md`

### Step 1: Review without modifying production

Review the candidate for:

- barrier counter/cancellation correctness;
- no reader admission between queued writers;
- same-chat durable ownership and token-guarded settlement;
- no batch head-of-line blocking;
- cap type/range and non-preemptive lowering;
- atomic expected-state transaction;
- Web-to-ingest process ownership;
- no in-memory cross-process claims;
- no recognition/execution semantic change;
- test determinism without sleep-only ordering assertions.

Any production-code fix invalidates the Task 10 candidate. Return to its focused
RED/GREEN test and run a new final full suite once.

### Step 2: Request exact candidate-SHA integration authorization

Report the reviewed local 40-hex candidate, full-suite evidence, and complete
planned commit range. Do not push until the owner explicitly authorizes that
exact SHA for integration into `codex/deepcoin-auto-trading-v1`.

### Step 3: Recheck remote and push the authorized reviewed commits

Run:

```bash
git fetch origin codex/deepcoin-auto-trading-v1
git status --porcelain=v2 --branch
test "$(git merge-base HEAD origin/codex/deepcoin-auto-trading-v1)" = \
  "$(git rev-parse origin/codex/deepcoin-auto-trading-v1)"
git push origin HEAD:codex/deepcoin-auto-trading-v1
git ls-remote --heads origin refs/heads/codex/deepcoin-auto-trading-v1
```

Expected: fast-forward push and remote tip equals the reviewed local HEAD. Do not
force push.

### Step 4: Stop unless the user authorizes the exact deployment SHA

Deployment requires explicit authorization for the exact remote 40-hex SHA. A
plan, local completion, or push does not authorize deployment, restart, or
settings changes.

## Task 12: Deploy Dormant/Compatible Code and Prove Rollback Before Cutover

**Files:** none locally unless status evidence is later recorded.

### Step 1: Re-run production pre-deploy gates read-only

On the authorized production server, require:

- production checkout/remote candidate and authorized exact SHA agree;
- worktree clean and no updater/maintenance owner is active;
- `active_write_count=0`;
- message jobs `claimed=0` and active inflight=0;
- active management=0;
- worker commands `claimed=0` and `executing=0`;
- WAL normal and `PRAGMA quick_check=ok`;
- worker/Web/ingest topology correct;
- monolith inactive/disabled;
- ingest is the only Telegram session holder;
- no active time-sensitive strategy operation;
- complete read-only worker-owned exchange baseline, allowing one reasoned retry
  only when the first query is incomplete.

Any incomplete query is unknown, not zero.

### Step 2: Deploy the exact SHA through the gated updater

From the approved workstation, run exactly one helper with the authorized SHA:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 `
  -ExpectedCommit <authorized-40-hex-sha>
```

Or on macOS/Linux:

```bash
EXPECTED_COMMIT=<authorized-40-hex-sha> ./scripts/server_git_update.sh
```

Expected: updater exit zero and production HEAD equals the authorized SHA.

### Step 3: Verify compatible post-deploy state

Before any cutover, require:

- `message_lock_mode=global`;
- absent/new cap resolves to compatibility value 20;
- split services active and monolith inactive;
- worker loop-health reports applied cap 20;
- queue backlog, duplicates, SQLite, event-loop, session, DeepSeek/402, and
  exchange checks are clean;
- no setting or exchange mutation occurred during observation.

### Step 4: Prove the no-restart transition and rollback route while global

Use the expected-state transition with old and target values both
`global + 20`. Require successful exclusive admission, unchanged persisted
settings, and a clean ingest barrier snapshot afterward.

Then test the fail-closed target validation without applying it: an incorrect
expected cap must return conflict and leave settings unchanged.

Do not set per_chat yet.

## Task 13: Atomic Cutover to `per_chat + 3`

**Files:** none locally unless status evidence is later recorded.

### Step 1: Freeze the cutover gate

Immediately before the write, recheck:

- active writes=0;
- message claimed/inflight=0;
- active management=0;
- worker commands claimed/executing=0;
- WAL/quick_check healthy;
- topology and sole session holder unchanged;
- no current technical anomaly;
- complete exchange baseline.

### Step 2: Submit one expected-state transition

The exact logical payload is:

```json
{
  "message_lock_expected_mode": "global",
  "message_processing_expected_max_parallel_chats": 20,
  "message_lock_mode": "per_chat",
  "message_processing_max_parallel_chats": 3
}
```

Use the authenticated/approved production control path. Never split these
fields into separate writes.

### Step 3: Handle the response

- Success: GET settings and require `per_chat + 3`.
- Conflict/validation failure: confirm settings remain `global + 20` and stop.
- Timeout/transport failure: classify outcome unknown, GET both fields, and do
  not retry unless the read proves the transition did not apply.

### Step 4: Prove applied state before the window

Require:

- worker loop-health applied cap exactly 3;
- active lane count at most 3;
- ingest exclusive waiter/holder zero after transition;
- no old global work remained in flight at release;
- topology/session/SQLite gates remain clean.

## Task 14: Two-Hour Natural-Traffic Acceptance Window

**Files:** none locally until final status recording.

### Step 1: Start one continuous quiet monitor

Observe exactly two continuous hours. Do not manufacture Telegram messages,
stitch windows, deploy, restart, invoke worker commands, change settings, or
trigger exchange writes from the observer.

Keep raw JSON, order rows, and long logs in one server-side evidence directory.
Use no more than the normal post-cutover and observation-end checkpoints unless
an anomaly appears.

### Step 2: Required natural traffic

Require at least five natural messages and attempt to cover at least two chats.
Do not extend the window if traffic is insufficient.

### Step 3: Required technical evidence

Prove:

- same-chat durable order and zero overlap;
- actual overlap between different chats when traffic permits;
- peak active chat lanes at most 3;
- a slow lane does not prevent the other two slots progressing;
- backlog converges and ends without stuck eligible work;
- duplicate job, decision, execution, and exchange submission groups are zero;
- no SQLite BUSY/database locked;
- no event-loop stall regression;
- no Telegram session conflict;
- no DeepSeek call/re-enable and no HTTP 402;
- Web/worker/ingest authority does not drift;
- complete final exchange history matches local execution identity and counts;
- the observer itself made no exchange write.

### Step 4: Success or rollback decision

- All traffic and technical gates pass: proceed to Task 15 completion.
- Fewer than five messages: execute lock rollback to global, record insufficient
  traffic, and keep status incomplete.
- Lock/admission/ingest anomaly: rollback to `global`, keep cap 3.
- Scheduler, duplicate, SQLite, execution, or concurrency anomaly: atomically
  rollback to `global + 1`.
- Unknown rollback response: GET exact settings before one reasoned retry.

Any failed technical or exchange query is fail-closed.

## Task 15: Record Completion or Incomplete Evidence

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Record bounded evidence

Record only:

- exact local/pushed/deployed candidate SHA;
- deployment/cutover/window timestamps;
- before/after modes and applied cap;
- traffic count and distinct chats;
- peak active lanes and ordering/overlap result;
- backlog/duplicate/SQLite/loop/session/DeepSeek/402/topology metrics;
- exchange snapshot completeness/parity result;
- anomaly and rollback result, if any;
- server-side evidence path and SHA-256;
- what remains outstanding.

Do not paste raw order rows or long logs into the status file.

### Step 2: Set the terminal planning state

Only if every required gate passed:

```yaml
workstream_status: completed
claimed_by: null
current_task: done
```

Otherwise:

```yaml
workstream_status: in_progress
claimed_by: null
current_task: <exact-next-gate>
```

An incomplete natural-traffic window is not completion and does not grant its
own waiver.

### Step 3: Commit and push the status only

Run:

```bash
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record per-chat durable lane acceptance"
git fetch origin codex/deepcoin-auto-trading-v1
test "$(git rev-parse HEAD^)" = \
  "$(git rev-parse origin/codex/deepcoin-auto-trading-v1)"
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

Expected staged list contains only the independent status file. No deploy or
restart is required for this documentation-only commit.
