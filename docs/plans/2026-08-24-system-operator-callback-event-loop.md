# System Operator Callback Event-Loop Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move system-operator callback processing off the worker asyncio thread while preserving its existing serialized management and Telegram response semantics.

**Architecture:** Wrap Deepcoin client construction and synchronous callback processing in one blocking unit and submit it to the existing process-wide `max_workers=1` management executor. The Bot loop awaits that unit before responding, so update ordering and exception handling remain unchanged while the event loop stays responsive.

**Tech Stack:** Python 3.12, asyncio, concurrent futures, pytest, SQLAlchemy.

---

### Task 1: Claim and document the approved repair

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`
- Create: `docs/plans/2026-08-24-system-operator-callback-event-loop-design.md`
- Create: `docs/plans/2026-08-24-system-operator-callback-event-loop.md`

**Step 1: Claim the workstream**

Set `workstream_status: in_progress` and
`current_task: task-16-system-operator-callback-event-loop-red-green`; record
the production stall evidence and local-only authorization boundary.

**Step 2: Verify and commit the claim**

Run:

```bash
git diff --check -- docs/per-chat-durable-lanes-status.md
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "chore: claim callback event-loop repair"
```

Expected: the staged list contains only the status file.

**Step 3: Commit the approved design and implementation plan explicitly**

Run `git diff --check`, stage each exact plan path, verify the cached list, and
commit the design and plan without staging any unrelated file.

### Task 2: RED — prove callback processing blocks the Bot event loop

**Files:**
- Modify: `tests/test_worker_loop_does_not_block_event_loop.py`
- Modify: `tests/test_runtime_event_loop_blocking_census.py`

**Step 1: Write the failing responsiveness test**

Add a fake system-operator Telegram update and replace
`process_system_operator_callback_data()` with a synchronous processor that
sleeps for `TICK_BLOCK_SECONDS`. Run the real
`run_system_operator_bot_command_loop()` beside the existing heartbeat helper.
Require at least `MIN_HEARTBEATS` and a worst gap below
`TICK_BLOCK_SECONDS`.

**Step 2: Write the failing thread-identity test**

Use an expiry-refresh callback that requires a Deepcoin client. Record the
thread names in both `deepcoin_client_factory()` and the callback processor.
Require both names to be identical and start with `mgmt-worker`.

**Step 3: Tighten the census expectation**

Remove exactly this allowlist entry:

```text
telegram_bot_commands.run_system_operator_bot_command_loop -> process_system_operator_callback_data
```

Leave the separate command-message and display-query entries unchanged.

**Step 4: Run RED**

Run:

```bash
PROJECT_PYTHON=/Users/steven/Documents/telegram获取消息/.venv/bin/python
PYTHONPATH=src "$PROJECT_PYTHON" -m pytest \
  tests/test_worker_loop_does_not_block_event_loop.py \
  -k 'system_operator_callback' -vv
PYTHONPATH=src "$PROJECT_PYTHON" -m pytest \
  tests/test_runtime_event_loop_blocking_census.py \
  -k 'blocking_call_census_matches' -vv
```

Expected: the responsiveness/thread tests fail because processing runs on
`MainThread`; the census fails with the repaired path still discovered.

**Step 5: Commit RED tests only**

Stage the two exact test paths, verify the cached list/check, and commit:

```bash
git commit -m "test: expose blocking system callback path"
```

### Task 3: GREEN — run one callback unit on the management worker

**Files:**
- Modify: `src/telegram_kol_research/telegram_bot_commands.py`

**Step 1: Add one synchronous callback unit**

Add a private helper that receives the session factory, callback data, and
optional Deepcoin client factory. Inside the helper, construct the client only
when `_expiry_callback_needs_deepcoin_client()` is true, then call
`process_system_operator_callback_data()` with that client.

Do not add retry, client-lifetime, result, or exception behavior.

**Step 2: Submit the unit to the shared executor**

Import `run_on_management_worker`. Replace the inline client construction and
processor call with:

```python
callback_response = await run_on_management_worker(
    _process_system_operator_callback_update,
    session_factory,
    callback_data,
    deepcoin_client_factory=deepcoin_client_factory,
)
```

**Step 3: Run GREEN**

Repeat the two RED commands. Expected: all selected tests pass.

**Step 4: Commit the production repair explicitly**

Run `git diff --check`, stage only
`src/telegram_kol_research/telegram_bot_commands.py`, verify the cached list,
and commit:

```bash
git commit -m "fix: move system callbacks off event loop"
```

Record this 40-hex commit as the rebuilt production-code candidate.

### Task 4: Freeze and verify the rebuilt candidate

Before freezing, independently review cancellation and lifespan shutdown. If
review finds that a queued callback can execute after cancellation, add an
atomic queued/started state so only started work drains; queued work must be
cancelled without client construction or processor invocation. If started work
fails during cancellation drain, retain `CancelledError` as the Bot result and
log the failure by safe update ID. Cover queued cancellation, running success,
running failure, and repeated cancellation in focused tests.

**Files:** none until status evidence is recorded.

**Step 1: Run focused compatibility verification**

Run:

```bash
PROJECT_PYTHON=/Users/steven/Documents/telegram获取消息/.venv/bin/python
PYTHONPATH=src "$PROJECT_PYTHON" -m pytest \
  tests/test_worker_loop_does_not_block_event_loop.py \
  tests/test_runtime_event_loop_blocking_census.py \
  tests/test_system_operator_bot.py \
  tests/test_web_app.py -q
```

Expected: pass. If production code changes after this command, repeat the
affected focused tests before freezing a new candidate.

**Step 2: Run static gates**

Run:

```bash
git diff --check
PYTHONPATH=src "$PROJECT_PYTHON" -m compileall -q src/telegram_kol_research tests
```

Expected: both exit zero.

**Step 3: Run one final complete suite**

Run exactly once after the final production edit:

```bash
PYTHONPATH=src "$PROJECT_PYTHON" -m pytest -q
```

Expected: pass. Any later production-code edit creates a new candidate and
requires affected focused tests plus one new final complete suite.

### Task 5: Record the local candidate without authorizing production

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Record bounded evidence**

Record the RED failures, GREEN results, focused/static/full-suite evidence, and
the exact production-code candidate SHA. Set:

```yaml
workstream_status: local_complete
claimed_by: codex-per-chat-20260823-root-68b9e88
current_task: task-17-review-push
local_candidate_commit: <production-code-candidate-sha>
deployment_authorized: false
cutover_authorized: false
```

State explicitly that push, deployment, restart, production settings, and
cutover remain unauthorized.

**Step 2: Commit status only**

Run:

```bash
git diff --check -- docs/per-chat-durable-lanes-status.md
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record callback event-loop candidate"
```

Expected: the staged list contains only the status file. Do not push.
