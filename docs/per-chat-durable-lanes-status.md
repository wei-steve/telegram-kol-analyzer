# Per-Chat Durable Lanes Status

This is the canonical checkpoint for the independent workstream that fixes
`KeyedAsyncLockRegistry.lock_all()` and safely enables bounded per-chat durable
message processing. It does not reopen, replace, or amend the completed runtime
serialization remediation status.

Before any implementation edit, a session must read this file and
`docs/plans/2026-08-23-per-chat-durable-lanes.md`, pass the claim gate, and commit
an exclusive claim to this file. The implementation base is the current commit
returned by:

```bash
git log -1 --format=%H -- docs/per-chat-durable-lanes-status.md
```

The working HEAD must equal that status commit before claiming. Upstream and the
remote deploy branch must separately equal the exact remote baseline stated by
the handoff; local planning-only commits may be ahead. A mismatch is a stop
condition, not permission to pull, reset, stash, clean, merge, or repair.

```yaml
project: per-chat-durable-lanes
design_doc: docs/plans/2026-08-23-per-chat-durable-lanes-design.md
implementation_plan: docs/plans/2026-08-23-per-chat-durable-lanes.md
canonical_status: docs/per-chat-durable-lanes-status.md
original_remediation_status: docs/runtime-serialization-remediation-status.md
deploy_branch: codex/deepcoin-auto-trading-v1
integration_branch: codex/phase0-deploy-integration
source_baseline: bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7
remote_baseline_at_planning: bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7
approved_design_commit: 1efd20cbd50be4e3c724d48874f6004fe6ad2c7c
workstream_status: claimed
claimed_by: codex-per-chat-20260823-root-68b9e88
current_task: task-10-final-candidate
verification_level: L2
local_candidate_commit: null
invalidated_local_candidate_commits:
  - c8f778201c123f0bbadddc06e718945307adf40b
  - c0e2471ed76b6d73bceb3be3d88304e57e44088d
local_focused_verification: null
local_full_suite_verification: null
local_compileall_verified: false
local_diff_check_verified: false
production_lock_mode_at_planning: global
compatibility_parallel_chat_limit: 20
target_parallel_chat_limit: 3
fail_closed_parallel_chat_limit: 1
schema_change_planned: false
production_data_mutation_planned: false
exchange_write_semantics_change_planned: false
deployment_authorized: false
cutover_authorized: false
```

## Fixed Boundaries

- `message_processing_jobs` remains the only durable queue and ordering
  authority.
- same-chat order remains the oldest non-terminal `raw_message_id` in the
  existing claim transaction.
- `KeyedAsyncLockRegistry` is process-local and must never be reported as a
  cross-process lock.
- the worker cap is per single worker process; production acceptance requires
  exactly one worker authority.
- production deployment requires separate authorization for an exact 40-hex
  SHA.
- production cutover requires a separately satisfied quiet-window gate.
- no historical replay, manufactured Telegram traffic, test trade, or extra
  exchange write is permitted.
- no DeepSeek semantic review or context fallback may be enabled.
- any schema/data-mutation requirement stops the L2 workstream.
- never run `git add -A`; stage and inspect exact paths.

## Approved Rollback

- lock/admission/ingest failure: atomically restore `message_lock_mode=global`
  while retaining cap 3;
- scheduler/duplicate/SQLite/execution/concurrency failure: atomically restore
  `message_lock_mode=global` and cap 1;
- neither rollback requires a restart;
- an unknown response requires an exact read before one reasoned retry.

## Acceptance Minimum

- final local production candidate: one complete suite after the last
  production-code edit;
- deployment remains global with compatibility cap 20 until cutover;
- cutover is one expected-state write to `per_chat + 3`;
- continuous two-hour natural-message observation;
- at least five natural messages, with at least two chats attempted;
- same-chat non-overlap/order, actual cross-chat progress, peak lanes at most 3;
- backlog convergence and zero duplicate job/decision/execution;
- zero SQLite lock, event-loop stall regression, session conflict,
  DeepSeek/402, and authority drift;
- complete read-only exchange history parity from the worker credential
  boundary;
- insufficient traffic or any failed gate rolls back and leaves the workstream
  incomplete without an automatic waiver.

## History

- `2026-08-23 planning`: read-only investigation at source baseline
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`; clean HEAD/upstream/remote and
  unclaimed completed original remediation were verified. Existing focused
  lock/worker/settings tests passed `202 passed in 3.35s`. A minimal reproduction
  confirmed a future key enters while snapshot `lock_all()` waits. No production
  code, test, configuration, deployment, server, Telegram, database, or exchange
  mutation was performed.
- `2026-08-23 design`: owner approved the shared/exclusive admission,
  work-conserving durable lanes, compatibility default 20, production cap 3,
  ingest-owned atomic transition, pure in-memory observability, and two-level
  rollback design. Design commit:
  `1efd20cbd50be4e3c724d48874f6004fe6ad2c7c`.
- `2026-08-24T06:23:14Z implementation claim`: session
  `codex-per-chat-20260823-root-68b9e88` claimed the independent workstream at
  exact baseline `68b9e88bbb9dd76227056a08376b7a94b553c5f8`. The upstream and
  remote deploy baseline remained
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`; the working tree was clean and
  the original remediation pointer remained completed. Authorization is local
  Tasks 1-10 only: no push, deploy, restart, production configuration, Telegram
  traffic, database mutation, or exchange action.
- `2026-08-24 task 2 RED`: ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -k 'future_key or not_starved' -vv`;
  all 3 selected tests failed because a future key entered while snapshot
  `lock_all()` was held or waiting.
- `2026-08-24 task 2 GREEN`: replaced snapshot enumeration with a
  writer-preference shared/exclusive admission barrier. Ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv`; result:
  `9 passed in 0.19s`.
- `2026-08-24 task 3 RED`: ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -k 'cancel or exception or multiple_lock_all or mixed_reader' -vv`;
  all 6 selected tests failed because the required admission snapshot was not
  yet implemented.
- `2026-08-24 task 3 GREEN`: added the pure in-memory admission snapshot and
  exact registered/acquired cancellation bookkeeping. Ran
  `$PROJECT_PYTHON -m pytest tests/test_keyed_async_locks.py -vv`; result:
  `15 passed in 0.19s`, including multiple writers, waiting/held cancellation,
  exception release, key-waiter cancellation, and mixed cleanup.
- `2026-08-24 task 4 RED`: ran the provider/rollback/mode-resolution slice;
  `2 failed, 2 passed`. The failures proved global work bypassed registry
  admission and a pre-created caller resolved `message_lock_mode` too early.
- `2026-08-24 task 4 GREEN`: provider operations now enter shared admission
  before resolving mode, while `lock_all()` always enters exclusive admission
  before the legacy global lock. Ran the registry, listener, reconcile, and
  position-authority compatibility slices; result: `49 passed in 2.61s`.
- `2026-08-24 task 5 RED`: ran the parallel-chat settings slice; all 13
  selected tests failed because the field was absent and invalid values were
  ignored.
- `2026-08-24 task 5 GREEN`: added compatibility default 20 and exact-integer
  bounds 1-20 for `message_processing_max_parallel_chats`, including storage
  preservation across unrelated saves. Full trading-settings result:
  `198 passed in 2.71s`.
- `2026-08-24 task 6 RED`: after replacing scheduler-sensitive start-order
  assertions with durable claimed-count evidence, the selected lane slice was
  `5 failed, 2 passed`: the old loop claimed 5-6 jobs at cap 3 and 3 jobs at
  cap 1; same-chat live/retry authority tests already passed.
- `2026-08-24 task 6 GREEN`: added pure in-memory lane activity and refactored
  the loop to dynamic, work-conserving, single-claim slot tasks. The focused
  lane slice passed `7 passed in 0.99s`; worker, pipeline-exclusivity, and
  shadow-enqueue compatibility passed `46 passed in 3.59s`.
- `2026-08-24 task 7 proof`: the Task 6 cancellation-finally implementation
  already satisfied the new restart/recovery assertions, so no additional
  production edit was required. The selected cancellation/stale/second-worker
  slice passed `3 passed in 0.41s`; the full worker file passed
  `21 passed in 1.88s`. Cancelled async slots retain their exact claim token
  and attempt count for stale-lease recovery, while later same-chat work stays
  blocked and a second worker cannot duplicate a live claim.
- `2026-08-24 task 8 RED`: the selected transaction/role slice failed all 6
  tests because the atomic helper, Web-to-ingest requester, ingest exclusive
  admission, and worker refusal were absent.
- `2026-08-24 task 8 GREEN`: added a `BEGIN IMMEDIATE` expected-state tuple
  transition and bounded localhost:8001 settings proxy. All 11 new transaction
  and role contracts passed; settings/Web selection passed
  `241 passed, 219 deselected in 7.19s`, and full settings/listener/pipeline
  compatibility passed `220 passed in 5.09s`. Unknown proxy outcomes are not
  retried, expected-state conflicts do not write, and unrelated saves avoid
  exclusive admission.
- `2026-08-24 task 9 RED`: the required observability/authority selection ran
  `5` tests: `2 failed, 3 passed`. The worker response had no shared lane
  activity object and the ingest response had no admission snapshot; the
  existing non-worker task partition and exchange-authority guard already
  passed.
- `2026-08-24 task 9 GREEN`: created one process-local
  `MessageProcessingActivity`, injected that exact instance into the worker
  loop, and exposed role-specific in-memory snapshots: worker/all reports only
  bounded lane counters and ingest/all reports only admission counters. No
  endpoint response contains chat IDs or reads settings, the database, or the
  exchange. Six new observability/process-boundary tests passed. The first
  affected run found one event-loop census violation from the Task 6
  `utc_now()` limit timestamp (`594 passed, 1 failed`); settings and observation
  time are now captured together in the existing worker thread. The census plus
  worker tests passed `24`, then the complete affected set passed
  `595 passed, 2 existing deprecation warnings in 46.21s`. Submission review
  strengthened the cross-process boundary assertion to prove the worker gets
  activity only, never the ingest registry/provider; that focused slice passed
  `3`. No schema, database data, recognition, strategy, execution, exchange
  semantics, production setting, deployment, restart, or external traffic
  changed.
- `2026-08-24 task 10 local candidate`: exact production-code candidate
  `c8f778201c123f0bbadddc06e718945307adf40b` passed the final local gate.
  The diff from implementation claim `0f40b7d` contains only the planned lock
  registry/provider, durable worker, trading-settings, Web wiring, tests, and
  this independent status document; it contains no schema, migration, model,
  recognition, strategy, execution, or original remediation pointer file.
  `git diff --check` passed and
  `$PROJECT_PYTHON -m compileall -q src/telegram_kol_research tests` exited zero
  in `0.68s`. The exact Task 9 focused command passed
  `589 passed, 2 warnings in 46.62s` (`48.24s` wall clock). The one authorized
  complete suite ran exactly once after the last production-code edit and
  passed `6215 passed, 1 skipped, 32 warnings in 524.52s` (`535.65s` wall
  clock). The warnings are existing deprecation warnings. Schema and production
  data are unchanged; recognition, strategy, position ownership, execution,
  exchange-write, and trading-decision semantics are unchanged. The workstream
  is now `local_complete`; Task 11 independent review, push, deployment
  authorization, and every production/cutover action remain unstarted and
  require their own gates.
- `2026-08-24 independent review`: the local Task 10 candidate was invalidated
  before further production edits. Read-only review at integration HEAD
  `4d53b83960d08598796308d1b244e468f5c57110` found three reproducible Important
  defects: explicit no-op expected-state requests bypassed concurrency-transition
  admission and role routing; an unrelated settings save could restore a stale
  concurrency tuple; and scheduler slots created under an old cap could claim
  after a lower cap was applied. The owner approved RED-to-GREEN remediation and
  rebuilding Task 10 under design A. Push, deployment, restart, production
  configuration, database mutation, and exchange action remain unauthorized.
- `2026-08-24 task 10 rebuilt candidate`: all three independent-review findings
  were repaired under RED-to-GREEN TDD. The new tests first proved `3` explicit
  no-op role/admission failures, `1` stale settings overwrite, and `1` lowered-cap
  oversubscription; paired expected fields without targets also failed `2`
  validation cases. The final implementation routes every explicit expected-state
  concurrency request through the owning role and exclusive admission, serializes
  all settings read-merge-write transactions with `BEGIN IMMEDIATE`, and performs
  scheduler-owned claim/refill only up to the current available capacity. The
  affected compatibility set passed `541 passed, 2 warnings in 36.65s`.
  `git diff --check` and compileall passed. Production candidate
  `c0e2471ed76b6d73bceb3be3d88304e57e44088d` then ran the one authorized final
  complete suite exactly once: `6222 passed, 1 skipped, 32 warnings in 503.19s`.
  The remote deploy branch remained exactly
  `bd862d74fdf4a3c9a792f2440ed301d9c5a1fba7`. No push, deployment, restart,
  production configuration, database mutation, Telegram traffic, or exchange
  action occurred; Task 11 review/push and all production/cutover gates remain
  separately unauthorized.
- `2026-08-24 rebuilt-candidate review`: read-only re-review confirmed the
  original three findings were closed, then found one new Important interleaving:
  a normal complete settings payload carrying an initially unchanged concurrency
  tuple could restore that stale tuple if cutover committed after the endpoint's
  second comparison but before `save_trading_settings()` acquired its write
  transaction. Candidate `c0e2471ed76b6d73bceb3be3d88304e57e44088d`
  and its `6222`-test evidence were invalidated before any further production
  edit. The workstream returned to Task 10 RED-to-GREEN remediation; push and all
  production actions remain unauthorized.
