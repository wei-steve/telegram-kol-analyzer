# Telegram Source-Deletion Exit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a Telegram source message is deleted, durably stop that strategy, cancel every still-open entry order, and market-close every filled position belonging to the exact deleted-message ledger chain. A repost remains independent and may execute only after the deleted strategy is proven flat.

**Architecture:** Persist deletion as an idempotent source event, resolve ownership only through `chat_id + message_id -> strategy/lifecycle -> binding -> leg -> ordId/posId`, and place a synchronous automation barrier before any further entry submission. A recoverable worker performs cancellation first and then creates ordinary position-management mutations for full market exits. Completion requires exchange-confirmed flatness; ambiguous exchange outcomes enter `recovery_required` and are never guessed or blindly retried.

**Tech Stack:** Python, SQLAlchemy, Telethon, FastAPI lifespan workers, pytest, existing Deepcoin execution/ledger services.

## Safety invariants

- Build every behavior test-first and use systematic debugging for any unexpected failure.
- Never infer ownership from symbol, side, price, timing, or account position alone.
- Never apply historical Telegram absence as proof of deletion; only a live `MessageDeleted` event is authoritative.
- Persist the deletion event before attempting exchange work.
- Cancel exact open entry `ordId`s before planning closure of exact filled `posId`s.
- Treat timeout/unknown exchange outcomes as `recovery_required`; reconcile before resubmitting.
- Keep live exchange execution behind a setting that defaults to `false` for the first deployment.
- Do not test with real Telegram deletions or real exchange orders during rollout.

### Task 1: Persist source-message deletion identity

**Files:**

- Modify: `src/telegram_signal_monitor/models.py`
- Modify: `src/telegram_signal_monitor/db.py`
- Create: `src/telegram_signal_monitor/source_message_deletion.py`
- Create: `tests/test_source_message_deletion.py`

**Step 1: Write failing persistence tests**

Cover these cases:

- recording `(chat_id, message_id)` creates one immutable source event;
- receiving the same Telegram deletion twice returns the original event;
- a deletion marks the matching `RawMessage` as deleted without changing message text;
- a missing raw message records an unbound event for audit but cannot produce an execution target;
- the event fingerprint is stable across retries.

**Step 2: Run the focused tests and confirm failure**

Run: `pytest -q tests/test_source_message_deletion.py`

**Step 3: Add the durable schema**

Extend `RawMessage` with:

- `source_status` (`active` or `deleted`);
- `deleted_at`;
- `deletion_event_fingerprint`.

Add `TelegramSourceMessageEvent` containing chat/message IDs, Telegram event metadata, fingerprint, binding state, timestamps, and raw-message ID. Add `SourceMessageDeletionExit` containing the event ID, strategy/lifecycle target, state, attempt counters, last error, timestamps, and completion proof. Create unique indexes for source identity/fingerprint and migrations in the existing idempotent `init_db` path.

Implement `record_source_message_deleted(...)` as a single idempotent transaction.

**Step 4: Run tests**

Run: `pytest -q tests/test_source_message_deletion.py`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/models.py src/telegram_signal_monitor/db.py src/telegram_signal_monitor/source_message_deletion.py tests/test_source_message_deletion.py
git commit -m "feat: persist Telegram source deletion identity"
```

### Task 2: Ingest live Telegram deletions

**Files:**

- Modify: `src/telegram_signal_monitor/telegram_live_listener.py`
- Modify: `src/telegram_signal_monitor/web.py`
- Modify: `tests/test_telegram_live_listener.py`

**Step 1: Write failing listener tests**

Verify that the listener registers `events.MessageDeleted`, processes every `deleted_id`, requires an exact `chat_id`, and calls an injectable recorder. Confirm that channel-wide deletion notifications without resolvable chat identity are audited as errors and do not target a strategy.

**Step 2: Run tests and confirm failure**

Run: `pytest -q tests/test_telegram_live_listener.py -k deleted`

**Step 3: Implement the handler**

Register both the existing new-message handler and a deletion handler. For each exact source identity, acquire the existing operation lock, persist the event, and return quickly. The handler must perform no exchange calls and must not scan by symbol/time.

Wire the recorder through the web lifespan dependency construction so tests can substitute it.

**Step 4: Run tests**

Run: `pytest -q tests/test_telegram_live_listener.py`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/telegram_live_listener.py src/telegram_signal_monitor/web.py tests/test_telegram_live_listener.py
git commit -m "feat: ingest Telegram message deletions"
```

### Task 3: Add the synchronous deleted-source execution barrier

**Files:**

- Modify: `src/telegram_signal_monitor/source_message_deletion.py`
- Modify: `src/telegram_signal_monitor/auto_trade_execution.py`
- Modify: `src/telegram_signal_monitor/authoritative_recognition.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_authoritative_recognition.py`

**Step 1: Write failing barrier tests**

Cover:

- an already-deleted raw message cannot submit an entry;
- a deletion arriving between recognition and authoritative apply is stopped by the second gate;
- a repost with the same chat/symbol/side is held as `waiting_source_deletion_exit` while the prior deletion exit is nonterminal;
- unrelated strategies pass;
- disabling exchange execution does not disable the safety barrier.

**Step 2: Run tests and confirm failure**

Run: `pytest -q tests/test_auto_trade_execution.py tests/test_authoritative_recognition.py -k source_delet`

**Step 3: Implement one shared barrier**

Add `source_execution_barrier(...)` returning a typed allow/block/hold decision. Call it immediately before automatic processing and again at the authoritative write/apply boundary. Persist the reason `source_message_deleted` or `waiting_source_deletion_exit` in existing status fields.

The repost hold is a sequencing barrier only: it must never merge the repost into the deleted lifecycle.

**Step 4: Run tests**

Run: `pytest -q tests/test_auto_trade_execution.py tests/test_authoritative_recognition.py`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/source_message_deletion.py src/telegram_signal_monitor/auto_trade_execution.py src/telegram_signal_monitor/authoritative_recognition.py tests/test_auto_trade_execution.py tests/test_authoritative_recognition.py
git commit -m "fix: block automation for deleted strategy sources"
```

### Task 4: Cancel exact pending entries through a recoverable worker

**Files:**

- Create: `src/telegram_signal_monitor/source_message_deletion_worker.py`
- Modify: `src/telegram_signal_monitor/terminal_entry_cleanup.py`
- Create: `tests/test_source_message_deletion_worker.py`

**Step 1: Write failing cancellation tests**

Seed two strategies sharing symbol and side. Delete only one and prove that the worker cancels only entry `ordId`s reachable through the deleted source ledger. Add cases for partial fills, already-cancelled orders, a fill during cancellation, API timeout, duplicate worker ticks, and restart recovery.

**Step 2: Run tests and confirm failure**

Run: `pytest -q tests/test_source_message_deletion_worker.py -k cancel`

**Step 3: Implement cancellation-first state transitions**

Claim jobs with durable compare-and-set transitions under the position authority lock. Reuse or factor the exact-ID logic in `cleanup_terminal_entry_legs` / `cancel_pending_entry_legs`; add reason `source_message_deleted`. Reconcile every order after cancellation. Newly filled quantities stay attached to their original leg. Unknown outcomes set `recovery_required` and block later stages.

**Step 4: Run tests**

Run: `pytest -q tests/test_source_message_deletion_worker.py -k cancel`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/source_message_deletion_worker.py src/telegram_signal_monitor/terminal_entry_cleanup.py tests/test_source_message_deletion_worker.py
git commit -m "feat: cancel entries for deleted source strategies"
```

### Task 5: Plan full market exit for exact filled positions

**Files:**

- Modify: `src/telegram_signal_monitor/source_message_deletion_worker.py`
- Modify: `src/telegram_signal_monitor/strategy_management_planner.py`
- Modify: `src/telegram_signal_monitor/strategy_management_batches.py`
- Modify: `tests/test_source_message_deletion_worker.py`
- Modify: `tests/test_strategy_management_planner.py`

**Step 1: Write failing full-exit tests**

Cover one filled plus one pending leg, multiple filled legs, a late fill discovered after cancellation, duplicate worker ticks, restart recovery, and another strategy on the same symbol/side. Assert the generated actions close the full exact quantity for each owned `posId` once and never perform a symbol-only close.

**Step 2: Run tests and confirm failure**

Run: `pytest -q tests/test_source_message_deletion_worker.py tests/test_strategy_management_planner.py -k source_delet`

**Step 3: Add a deletion full-exit planner**

Factor the existing full-exit planning behavior into `plan_source_deletion_full_exit(...)`. Preserve ancestry to the original recognition decision and use a deterministic generation such as `source_deleted:<fingerprint>`. Store the deletion event in the target snapshot. Create ordinary management mutations so the existing position-mutation executor performs the market close; the Telegram callback must never call the exchange directly.

**Step 4: Run tests**

Run: `pytest -q tests/test_source_message_deletion_worker.py tests/test_strategy_management_planner.py`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/source_message_deletion_worker.py src/telegram_signal_monitor/strategy_management_planner.py src/telegram_signal_monitor/strategy_management_batches.py tests/test_source_message_deletion_worker.py tests/test_strategy_management_planner.py
git commit -m "feat: market exit positions from deleted sources"
```

### Task 6: Require strict flatness before completion

**Files:**

- Modify: `src/telegram_signal_monitor/source_message_deletion_worker.py`
- Modify: `src/telegram_signal_monitor/strategy_management_worker.py`
- Modify: `tests/test_source_message_deletion_worker.py`

**Step 1: Write failing completion tests**

Prove deletion exit cannot succeed while any owned entry order is open, any owned position quantity is nonzero, any management mutation is nonterminal, or an exchange snapshot failed. Verify pending-only strategies end cancelled, filled strategies end exited, and both carry `exit_reason=source_message_deleted`.

**Step 2: Run tests and confirm failure**

Run: `pytest -q tests/test_source_message_deletion_worker.py -k completion`

**Step 3: Implement strict finalization**

Reconcile exact order and position IDs, then transactionally update the source event, deletion-exit job, and lifecycle. Save the proof snapshot used to declare flatness. Release held reposts only after this transaction commits.

**Step 4: Run tests**

Run: `pytest -q tests/test_source_message_deletion_worker.py`

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/source_message_deletion_worker.py src/telegram_signal_monitor/strategy_management_worker.py tests/test_source_message_deletion_worker.py
git commit -m "fix: prove deleted strategies fully exited"
```

### Task 7: Add dormant rollout controls and operator visibility

**Files:**

- Modify: `src/telegram_signal_monitor/trading_settings.py`
- Modify: `src/telegram_signal_monitor/web.py`
- Modify: `src/telegram_signal_monitor/bot.py`
- Modify: relevant settings, web, and bot tests

**Step 1: Write failing rollout tests**

Verify `telegram_source_deletion_exit_enabled` defaults to `false`; events and barriers remain active while false; the worker makes no exchange calls while false; enabling starts processing. Verify operator views expose source deletion, cancellation, exit, recovery, and flat-proof states.

**Step 2: Run focused tests and confirm failure**

Run the settings/web/bot tests selected by `-k source_delet`.

**Step 3: Wire the worker and visibility**

Start the worker from FastAPI lifespan, but gate exchange effects with the setting. Add concise operator alerts and strategy-record fields. Alerts must include exact strategy/lifecycle and state, not credentials or raw API payloads.

**Step 4: Run focused tests**

Run the same settings/web/bot selections plus `tests/test_source_message_deletion_worker.py`.

**Step 5: Commit**

```bash
git add src/telegram_signal_monitor/trading_settings.py src/telegram_signal_monitor/web.py src/telegram_signal_monitor/bot.py tests
git commit -m "feat: expose deleted-source emergency exits"
```

### Task 8: Add the Shuqin delete-and-repost regression

**Files:**

- Create: `tests/test_shuqin_deleted_repost_regression.py`

**Step 1: Build the regression fixture**

Seed message `3428` with ETH long entries `1828/1808`, stop `1695`, and targets `1853/1885/1930`. Seed its exact binding, legs, orders, and a cancellation/fill race. Then ingest deletion of `3428` and repost message `3429` with stop `1795`.

**Step 2: Write end-to-end assertions**

Assert:

- message `3428` becomes terminal and every fill is market-closed exactly once;
- no open order or position remains under its ledger chain;
- message `3429` is held until the old chain is proven flat;
- after release, new entry and protection order IDs belong only to `3429`, including stop `1795`;
- neither strategy can inherit the other's order or position IDs.

**Step 3: Run and fix only the generalized implementation**

Run: `pytest -q tests/test_shuqin_deleted_repost_regression.py`

Do not add message-ID-specific production logic.

**Step 4: Commit**

```bash
git add tests/test_shuqin_deleted_repost_regression.py
git commit -m "test: cover deleted and reposted strategy lifecycle"
```

### Task 9: Verify, review, push, and deploy dormant

**Step 1: Run local verification**

```bash
python -m compileall -q src tests
git diff --check
pytest -q
```

**Step 2: Audit invariants and request code review**

Use the requesting-code-review skill. Specifically inspect for symbol/time fallback, direct exchange calls from the Telegram callback, non-idempotent submissions, missing restart paths, and flag-default mistakes. Address all confirmed high-severity findings and rerun the relevant tests.

**Step 3: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Verify a safe deployment window**

On the server, perform read-only checks for active time-sensitive strategy operations. If safety cannot be proven, do not restart; record the exact remaining verification and stop.

**Step 5: Deploy with execution dormant**

Confirm `telegram_source_deletion_exit_enabled=false`, then use the repository helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Verify the service is healthy, schema exists, listener is receiving deletion events, no exchange action was emitted, and current ledger audits remain clean.

**Step 6: Stop for explicit activation approval**

Report shadow/dormant evidence and a rollback procedure. Do not enable live deletion exits until the user explicitly approves activation after server verification.
