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
current_task: task-5-parallel-chat-setting
verification_level: L2
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
