# Terminal Entry Leg Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enforce that a strategy cannot terminate while an exact, unfilled entry leg remains live, and automatically cancel such legs before or immediately after a full position exit.

**Architecture:** Introduce one shared execution-exposure predicate and one serialized terminal-entry cleanup coordinator that reuses the existing exact Deepcoin cancellation path. Wire it into manual full close, exchange reconciliation, and lifecycle terminal guards; use the existing reconciliation cycle as the idempotent historical backstop and persist cleanup notification delivery.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI, pytest, Deepcoin REST API, Telegram Bot API.

---

### Task 1: Define the execution-exposure invariant

**Files:**
- Modify: `src/telegram_kol_research/lifecycle_exit_intents.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_lifecycle_exit_intents.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing predicate tests**

Add cases proving:

```python
def test_unknown_binding_with_pending_entry_leg_is_live_execution_exposure(...):
    binding.status = "unknown"
    pending_leg.status = "pending"
    pending_leg.pos_id = None
    assert has_live_execution_binding(session, lifecycle) is True


def test_unknown_binding_with_only_terminal_entry_legs_is_not_live_exposure(...):
    binding.status = "unknown"
    pending_leg.status = "cancelled"
    pending_leg.terminal_reason = "operator_cancelled_unfilled_entry_leg"
    assert has_live_execution_binding(session, lifecycle) is False
```

Also cover `submitted`, `partially_filled`, exact verified live `pos_id`, and a different
binding with the same symbol that must not match.

**Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_lifecycle_exit_intents.py \
  tests/test_execution_bindings.py -k "live_execution or unresolved_entry_leg"
```

Expected: the `unknown + pending leg` case fails because current code only accepts
binding status `open/active`.

**Step 3: Implement one shared predicate**

Move the nonterminal entry-leg classification into a public helper in
`execution_bindings.py`, using the existing `TERMINAL_ENTRY_LEG_STATES`. Update
`has_live_execution_binding()` to check the lifecycle's exact binding first and then the
exact `(venue, chat_id, message_id, symbol, side)` binding. Do not treat unrelated
orders as exposure.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/lifecycle_exit_intents.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_lifecycle_exit_intents.py \
  tests/test_execution_bindings.py
git commit -m "fix: treat pending entry legs as live exposure"
```

### Task 2: Build the serialized terminal-entry cleanup coordinator

**Files:**
- Create: `src/telegram_kol_research/terminal_entry_cleanup.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/trade_signals.py`
- Test: `tests/test_terminal_entry_cleanup.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write failing cleanup tests**

Cover these exact outcomes:

- a bound trigger entry is cancelled and absent on readback;
- a bound regular entry is cancelled and absent on readback;
- an already absent order returns `already_absent`;
- an API timeout followed by absent readback resolves safely;
- an API timeout followed by a still-present order returns `unknown`;
- a response claiming success while readback remains present returns `blocked`;
- a missing or ambiguous exact order identity performs zero exchange writes;
- binding status `unknown` does not block an exact, risk-reducing cancellation;
- repeated calls reuse terminal state and do not cancel twice.

Use a fake Deepcoin client that records call order:

```python
assert client.calls == [
    ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
    ("cancel_trigger_order", target_order_id),
    ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
]
```

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_terminal_entry_cleanup.py \
  tests/test_deepcoin_execution_actions.py -k "terminal_entry or pending_entry"
```

Expected: FAIL because the coordinator and mandatory readback do not exist.

**Step 3: Implement the minimal coordinator**

Create a result type with bounded fields:

```python
@dataclass(frozen=True, slots=True)
class TerminalEntryCleanupResult:
    status: Literal["resolved", "already_absent", "blocked", "unknown"]
    binding_id: int
    lifecycle_id: int
    leg_ids: tuple[int, ...]
    order_ids: tuple[str, ...]
    event_ids: tuple[int, ...]
```

The coordinator must run under `serialized_position_authority_mutation`, enqueue one
deterministic `cancel_entry` trade signal, call the existing exact cancellation code,
perform regular and trigger readbacks, then call `mark_trade_signal_submitted()` or
`mark_trade_signal_failed()`. Never store arbitrary exception strings in user-facing
notification payloads.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/terminal_entry_cleanup.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/trade_signals.py \
  tests/test_terminal_entry_cleanup.py \
  tests/test_deepcoin_execution_actions.py
git commit -m "feat: add terminal entry cleanup coordinator"
```

### Task 3: Make manual full close cancel deferred entries first

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing close-order tests**

Add a two-leg binding with one verified live `pos_id` and one pending trigger entry.
Assert the exchange call sequence is:

```python
[
    "list pending entries",
    "cancel exact pending entry",
    "read back pending entries",
    "read exact live position",
    "submit exact market close",
]
```

Add failure cases asserting `place_order` is never called when cleanup is `blocked` or
`unknown`. Verify the API returns HTTP 409 with a bounded reason code.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py -k "close_bound_position and pending_entry"
```

Expected: FAIL because `close_bound_position_market()` currently proceeds directly to
the position close.

**Step 3: Integrate the coordinator**

Call terminal-entry cleanup inside the same serialized close path before reserving or
submitting the close. Continue only for `resolved` or `already_absent`. Return cleanup
event IDs in the internal result while keeping the public endpoint free of credentials
and raw exchange payloads.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/web_app.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py
git commit -m "fix: cancel deferred entries before manual full close"
```

### Task 4: Prevent simulated or message-driven premature terminal states

**Files:**
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/lifecycle_exit_intents.py`
- Test: `tests/test_lifecycle_monitor.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write the regression tests**

Reproduce lifecycle 625 with:

```python
lifecycle.lifecycle_status = "entered"
binding.status = "unknown"
first_leg.status = "filled"
second_leg.status = "pending"
second_leg.pos_id = None
```

Assert a simulated take-profit transition and a KOL exit/cancel message record an exit
intent but do not set the lifecycle to `exited`. Add a control case where all entry legs
are terminal and the ordinary non-live lifecycle transition still works.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_lifecycle_monitor.py \
  tests/test_message_recognition.py -k "unknown_binding or pending_second_leg"
```

Expected: FAIL on the reproduced incident.

**Step 3: Replace local status-only guards**

Make lifecycle monitor persistence and message recognition call the shared execution
exposure predicate. Do not add Deepcoin writes to the candle monitor or recognition
path; they only preserve the nonterminal lifecycle and schedule authoritative execution
or reconciliation.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/lifecycle_monitor.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/lifecycle_exit_intents.py \
  tests/test_lifecycle_monitor.py \
  tests/test_message_recognition.py
git commit -m "fix: block terminal lifecycle with pending entry legs"
```

### Task 5: Clean deferred entries during exchange-confirmed closure

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing reconciliation tests**

Cover:

- a verified position disappears while its second entry leg remains pending;
- an exchange-native final TP/SL leaves a pending entry leg;
- cleanup succeeds, then lifecycle becomes terminal;
- cleanup is unknown, so lifecycle remains nonterminal with
  `management_action="terminal_cleanup_required"`;
- a later reconciliation confirms the order absent and completes the lifecycle;
- unrelated bindings and orders are unchanged.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py \
  tests/test_web_app.py -k "manual_closed or terminal_cleanup"
```

Expected: FAIL because reconciliation can currently separate lifecycle terminalization
from deferred-entry cleanup.

**Step 3: Integrate cleanup into existing reconciliation**

Give `sync_manual_closed_deepcoin_positions()` the trading-client capability already
available from `app.state.deepcoin_client_factory()`. Before terminalizing a lifecycle,
run the coordinator. Preserve the lifecycle and exact failure state unless cleanup is
proven complete. Do not create a new background loop.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_execution_bindings.py \
  tests/test_web_app.py
git commit -m "fix: reconcile deferred entries before lifecycle exit"
```

### Task 6: Add the terminal-lifecycle invariant backstop

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing historical-anomaly tests**

Create `exited + pending entry leg` rows and assert the existing reconciliation call:

- discovers the bounded anomaly;
- cancels only the exact bound order;
- confirms absence;
- terminalizes the leg without reopening the lifecycle;
- is idempotent on a second cycle;
- refuses ambiguous or identity-less orders with zero exchange writes.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py -k "terminal_lifecycle_pending_entry"
```

Expected: FAIL because no invariant sweep exists.

**Step 3: Add a bounded reconciliation query**

Run the query inside the current Deepcoin reconciliation cycle. Order by lifecycle ID,
apply a fixed batch limit, and return counts for `resolved`, `already_absent`, `blocked`,
and `unknown`. Do not add a timer, daemon, feature flag, or shadow branch.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_execution_bindings.py
git commit -m "fix: repair terminal lifecycle entry exposure"
```

### Task 7: Persist and deliver cleanup notifications

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/execution_events.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_db_bootstrap.py`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing migration and delivery tests**

Test idempotent SQLite addition of nullable cleanup-notification fields on
`execution_events`. Test one deterministic notification per cleanup fingerprint,
`pending → delivering → delivered`, bounded failure summaries, finite retry, and
successful delivery after a service restart.

**Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_db_bootstrap.py \
  tests/test_system_operator_bot.py \
  tests/test_web_app.py -k "cleanup_notification"
```

Expected: FAIL because cleanup event delivery state is not persisted.

**Step 3: Add durable event delivery**

Add nullable notification status, fingerprint, Telegram message ID, error summary,
attempt count, next-attempt time, and notified-at fields. Claim delivery atomically,
send through the KOL event-processing bot, and never repeat the exchange cancellation
from the notification worker.

**Step 4: Run focused tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/execution_events.py \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/web_app.py \
  tests/test_db_bootstrap.py \
  tests/test_system_operator_bot.py \
  tests/test_web_app.py
git commit -m "feat: notify terminal entry cleanup outcomes"
```

### Task 8: Run regression tests and document operations

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_context_resolution_replay.py`

**Step 1: Document exact operational checks**

Document:

- the lifecycle terminal invariant;
- the server-side read-only query for terminal lifecycle entry exposure;
- exact success and blocked notification meanings;
- no feature flag and no shadow behavior;
- Git revert/redeploy rollback;
- the rule that a confirmed cancellation is never recreated during rollback.

**Step 2: Run focused and broad local tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_terminal_entry_cleanup.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_execution_bindings.py \
  tests/test_lifecycle_exit_intents.py \
  tests/test_lifecycle_monitor.py \
  tests/test_message_recognition.py \
  tests/test_strategy_management_executor.py \
  tests/test_context_resolution_replay.py \
  tests/test_system_operator_bot.py \
  tests/test_web_app.py \
  tests/test_db_bootstrap.py
```

Expected: PASS.

**Step 3: Run the full local suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS, excluding only tests explicitly documented as requiring production
identity or network access.

**Step 4: Request code review**

Use the `requesting-code-review` skill. Resolve all Critical and Important findings,
then rerun the affected tests.

**Step 5: Commit documentation**

```bash
git add docs/runbook.md docs/server-deployment.md docs/migration-handoff.md
git commit -m "docs: document terminal entry cleanup invariant"
```

### Task 9: Push, deploy, and verify production

**Files:**
- No additional source files.

**Step 1: Confirm deployment safety**

Before restart, verify there is no active time-sensitive strategy operation, no
in-flight management batch, and no unresolved exchange submission whose outcome is
unknown. Stop if a safe window cannot be proven.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub accepts the reviewed commits.

**Step 3: Deploy through the standard server workflow**

```bash
./scripts/server_git_update.sh
```

Expected: the server fast-forwards to the reviewed SHA, reinstalls the editable package,
restarts `telegram-kol.service`, and reports it active.

**Step 4: Run server-only verification**

Verify:

```bash
systemctl is-active telegram-kol.service
git -C /opt/telegram-kol-analyzer rev-parse HEAD
```

Then run the documented read-only cross-check of all positions, regular orders, trigger
orders, execution bindings, entry legs, lifecycle states, execution events, and cleanup
notification delivery. Do not open a real order for testing.

Expected:

- no `terminal lifecycle + nonterminal entry leg` anomaly;
- no exchange order without an exact local leg;
- no local pending leg absent from the exchange without a terminal reason;
- Telegram cleanup notifications have a durable delivery result;
- existing positions retain exact ownership and protection.

**Step 5: Roll back only if verification fails**

Revert the reviewed repair commits, push, and redeploy the previous known-good code.
Do not recreate any order that the repaired version already confirmed cancelled.
