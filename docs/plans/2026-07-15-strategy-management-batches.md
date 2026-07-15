# Deepcoin Strategy Management Batches Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Telegram partial take-profit, full exit, and stop-loss management target one exact strategy and all of its verified Deepcoin split positions through durable, idempotent management batches.

**Architecture:** Persist MiMo's lifecycle target and normalize its management intent, then replace the current symbol/side management lookup with an immutable batch planner. Separate planning, exchange submission, and reconciliation so API acceptance never masquerades as a confirmed close; use per-position batch legs, deterministic regular-order `clOrdId` values, complete TPSL snapshots, and explicit recovery states.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite compatibility migrations, FastAPI, Deepcoin REST API, pytest.

---

## Execution Constraints

- Start implementation in an isolated worktree from commit `e173607` or later.
- Keep production `auto_trade_enabled=false` throughout implementation and
  deployment.
- Add a separate `management_execution_mode` with safe default `disabled`.
  `shadow` may plan batches while global automatic trading is false; `live`
  must additionally require global automatic trading to be true.
- Do not place, cancel, or replace a real Deepcoin order during automated
  verification.
- Do not preserve two live management paths. Once batch execution is wired,
  the old generic management-signal dispatcher must not submit independently.
- Treat existing queued legacy management signals as review-only unless they
  carry a valid batch ID created by the new planner.

### Task 1: Persist the authoritative lifecycle target and management intent

**Files:**
- Modify: `src/telegram_kol_research/models.py:68-85`
- Modify: `src/telegram_kol_research/db.py:22-180`
- Modify: `src/telegram_kol_research/message_recognition.py:981-1137`
- Modify: `src/telegram_kol_research/message_recognition.py:1666-1743`
- Modify: `src/telegram_kol_research/authoritative_recognition.py:146-186`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Write failing schema and recognition tests**

Require `SignalCandidate` to persist:

```python
target_lifecycle_id: int | None
management_action: str | None
management_fraction: float | None
recognition_generation: str | None
```

Add tests proving that a MiMo `position_update` or `exit_position` with
`target_lifecycle_id=42` writes candidate target `42`, the normalized action,
and the exact claimed generation. Re-recognition must supersede the prior
candidate without making its old generation executable.

Add a bootstrap test that opens a legacy SQLite schema and confirms all four
columns are added without changing existing rows.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q \
  tests/test_message_recognition.py -k 'target_lifecycle or management_fraction' \
  tests/test_authoritative_recognition.py -k generation \
  tests/test_db_bootstrap.py -k signal_candidate
```

Expected: FAIL because the candidate drops lifecycle identity and generation.

**Step 3: Implement the minimal persistence path**

Extend `_apply_lifecycle_event_decision(...)` and the two candidate upsert
helpers to receive and persist normalized metadata. Pass
`assessment.authoritative_generation` into `apply_authoritative_mimo_payload`.
Normalize intent with one pure helper:

```python
def normalize_management_intent(decision: Mapping[str, Any], text: str) -> tuple[str, float | None]:
    # full_exit | partial_take_profit | adjust_stop_loss |
    # move_stop_to_break_even | partial_then_break_even
    ...
```

Do not infer a fraction by copying lifecycle TP/SL fields. An unqualified
partial intent persists `management_fraction=None`; the planner owns the
default 50% rule.

Add the SQLite compatibility `ALTER TABLE` statements in `db.py`.

**Step 4: Run GREEN tests**

Run the command from Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_message_recognition.py tests/test_authoritative_recognition.py \
  tests/test_db_bootstrap.py
git commit -m "fix: preserve management lifecycle identity"
```

### Task 2: Add durable management batch and leg models

**Files:**
- Modify: `src/telegram_kol_research/models.py:489-548`
- Modify: `src/telegram_kol_research/db.py:182-260`
- Create: `src/telegram_kol_research/strategy_management_batches.py`
- Create: `tests/test_strategy_management_batches.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Write failing model and uniqueness tests**

Add tests for:

- one batch per idempotency fingerprint;
- one leg per `(batch_id, pos_id)`;
- one nonterminal batch per `strategy_instance_id`;
- JSON round-tripping for target snapshots, TPSL snapshots, requests,
  responses, and errors;
- legacy-database bootstrap and indexes.

The batch model must contain at least:

```python
id, idempotency_fingerprint, raw_message_id, recognition_decision_id,
recognition_generation, target_lifecycle_id, strategy_instance_id,
execution_binding_id, intent, effective_action, requested_fraction,
effective_fraction, partial_round_before, status, reason_code,
target_fingerprint, planned_at, started_at, reconciled_at, completed_at,
notification_state, notification_fingerprint, created_at, updated_at
```

The leg model must contain at least:

```python
id, management_batch_id, execution_order_leg_id, pos_id, leg_index,
status, preflight_size, planned_close_size, avg_entry_price,
quantity_step, old_tpsl_json, planned_tpsl_json, client_order_id,
exchange_order_id, request_json, response_json, last_error,
last_exchange_snapshot_json, created_at, updated_at
```

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_batches.py \
  tests/test_db_bootstrap.py -k 'management_batch or management_leg'
```

Expected: FAIL because the tables and repository do not exist.

**Step 3: Implement models and repository primitives**

Provide typed records and small transaction helpers in
`strategy_management_batches.py`, including:

```python
create_management_batch(...)
load_management_batch(batch_id)
claim_ready_batch(batch_id)
transition_batch(batch_id, expected_statuses, new_status, ...)
transition_leg(leg_id, expected_statuses, new_status, ...)
list_recoverable_batches(limit=...)
```

Use database uniqueness for idempotency and the nonterminal strategy lock. If a
partial unique index is needed, add explicit SQLite index SQL in `db.py` and a
duplicate-safe bootstrap guard.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_batches.py tests/test_db_bootstrap.py
git commit -m "feat: persist strategy management batches"
```

### Task 3: Add a separate disabled, shadow, and live management gate

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py:20-88`
- Modify: `src/telegram_kol_research/web_app.py:3030-3105`
- Modify: `src/telegram_kol_research/templates/index.html:311-360`
- Modify: `src/telegram_kol_research/static/app.js:1703-1748`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing gate tests**

Require:

```python
management_execution_mode: Literal["disabled", "shadow", "live"] = "disabled"
```

Test that invalid values fail validation, shadow mode is allowed while
`auto_trade_enabled` is false, and live management refuses to execute unless
both mode is `live` and global automatic trading is true. Verify the Web form
labels shadow as no-exchange-write and live as high risk.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_trading_settings.py \
  tests/test_web_app.py -k management_execution_mode \
  tests/test_web_page_render.py -k management_execution_mode
```

Expected: FAIL because the setting does not exist.

**Step 3: Implement validation, persistence, API, and form**

Do not make shadow mode depend on group `auto_trade`; it must be able to plan a
real natural message safely while global trading is off. Live mode retains the
existing group/KOL auto-trade and confidence gates in addition to the two
global gates.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js tests/test_trading_settings.py \
  tests/test_web_app.py tests/test_web_page_render.py
git commit -m "feat: gate management planning and execution"
```

### Task 4: Build the exact-strategy batch planner

**Files:**
- Create: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:351-492`
- Modify: `src/telegram_kol_research/position_attribution.py:477-536`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_auto_trade_execution.py`

**Step 1: Write failing exact-target tests**

Cover:

- candidate lifecycle -> lifecycle binding -> strategy instance equality;
- selected lifecycle has no binding while another same-symbol binding exists;
- two active bindings for one strategy;
- unverified, terminal, conflicting, or evidence-unavailable entry legs;
- stale binding `pos_id` before a conditional order reconciliation;
- immutable target fingerprint changes before execution;
- shadow mode creates a batch but makes no Deepcoin write call.

The wrong-binding regression test must construct lifecycle B as the selected
target, give only lifecycle A a BTC-short binding, and assert `blocked` with
`target_strategy_binding_not_found`.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_planner.py \
  tests/test_auto_trade_execution.py -k management
```

Expected: FAIL because current code selects by chat/KOL/symbol/side.

**Step 3: Implement planning**

The planner API should be explicit:

```python
def plan_strategy_management_batch(
    session_factory,
    *,
    raw_message_id: int,
    deepcoin_client,
    contract_spec_provider,
    planned_at: datetime | None = None,
) -> ManagementPlanningResult:
    ...
```

Call `reconcile_deepcoin_execution_bindings` before freezing targets, then
reload every local row in a new session. Require exact strategy identity and
`require_verified_position_ownership` for every live target. Build position
economics from one exchange snapshot. For protection intents, call
`match_position_protection` and require `verified` for every position.

Change `_auto_process_management_signal` into orchestration only: plan, return
the shadow result, or pass the batch ID to the new executor. Remove
`_load_active_execution_binding` from the live management route.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/position_attribution.py \
  tests/test_strategy_management_planner.py tests/test_auto_trade_execution.py
git commit -m "feat: plan exact strategy management batches"
```

### Task 5: Implement the two-round partial take-profit policy and allocation

**Files:**
- Create: `src/telegram_kol_research/strategy_management_sizing.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Test: `tests/test_strategy_management_sizing.py`
- Test: `tests/test_strategy_management_planner.py`

**Step 1: Write failing policy and sizing tests**

Test these exact examples:

```python
assert effective_action(round_before=0, fraction=None) == ("partial_close", 0.5)
assert effective_action(round_before=0, fraction=0.3) == ("partial_close", 0.3)
assert effective_action(round_before=1, fraction=None) == ("full_close", 1.0)
assert effective_action(round_before=1, fraction=0.3) == ("full_close", 1.0)
```

Sizing cases must include `6 + 4 -> 3 + 2`, two `0.02` positions at a legal
step, uneven integer-contract sizes, a target below minimum quantity, and a
case where deterministic remainder allocation is needed. Never over-close.

Test that only a fully exchange-confirmed first partial batch makes
`partial_round_before=1`; submitted, unknown, failed, and partially confirmed
batches freeze or remain round zero. A duplicate message returns the existing
batch and never increments the round.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_planner.py -k partial
```

Expected: FAIL because close size is currently `current_size * fraction` per
position with no contract-step or round policy.

**Step 3: Implement pure Decimal-based sizing**

Use `Decimal(str(value))`, the verified `DeepcoinContractSpec.quantity_step`,
and `min_quantity`. Compute the aggregate target first, floor proportional leg
allocations, then distribute whole-step remainder deterministically by largest
fractional remainder and stable leg index. If all positions cannot participate
safely or the aggregate target cannot meet minimum rules, block the whole batch.

Derive the confirmed round from succeeded, reconciled batches for the same
target lifecycle. Do not trust `StrategyLifecycle.management_action` as a
counter.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_sizing.py \
  src/telegram_kol_research/strategy_management_planner.py \
  tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_planner.py
git commit -m "feat: add finite partial take profit policy"
```

### Task 6: Submit close legs with durable reservations and deterministic IDs

**Files:**
- Create: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_client.py:62-110`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:309-424`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_deepcoin_client.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write failing submission and crash-window tests**

Require every close leg to:

- transition `planned -> reserved` in a committed transaction before API call;
- send exact `closePosId`, planned size, `ordType=market`, and deterministic
  `clOrdId` bounded to Deepcoin's documented length;
- persist response and order ID before moving to `submitted`;
- continue remaining preflighted legs after one definite API failure;
- enter `submit_unknown` on timeout or lost response;
- never mark binding, entry leg, or lifecycle closed on submission.

Test process interruption immediately before the call, after the call, and
before response persistence.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_executor.py -k close \
  tests/test_deepcoin_client.py -k client_order \
  tests/test_deepcoin_execution_actions.py -k close_position
```

Expected: FAIL because automated close has no batch reservation and marks local
state closed immediately.

**Step 3: Implement close execution and retire optimistic closure**

Expose only a batch-ID executor:

```python
def execute_management_batch(session_factory, *, batch_id, deepcoin_client, executed_at=None):
    ...
```

Reuse payload normalization from exact bound close, but do not reuse its
one-way manual reservation table. Batch-leg uniqueness is the reservation.
Record execution events with batch ID and leg ID in structured request/after
data until dedicated event columns are justified.

Change or remove the optimistic status updates at the end of
`close_position_market`. Legacy callers must delegate through a valid batch or
fail with `legacy_management_signal_requires_batch`.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/deepcoin_client.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  tests/test_strategy_management_executor.py tests/test_deepcoin_client.py \
  tests/test_deepcoin_execution_actions.py
git commit -m "feat: submit idempotent management close legs"
```

### Task 7: Reconcile close results and advance lifecycle only from exchange truth

**Files:**
- Create: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/execution_bindings.py:273-430`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write failing reconciliation tests**

Cover:

- submitted order exists but position size is unchanged;
- planned partial size is fully reflected in live remaining size;
- one leg partially fills;
- all full-close positions disappear;
- deterministic `clOrdId` resolves an unknown submission;
- no matching order leaves `submit_unknown` and forbids automatic retry;
- first complete partial advances the round exactly once;
- all-position full close marks binding/legs terminal and lifecycle exited only
  after exchange confirmation;
- partially confirmed first partial freezes the strategy.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_reconciliation.py
```

Expected: FAIL because no batch reconciler exists.

**Step 3: Implement one-snapshot reconciliation**

For each batch, load positions and regular order evidence once per instrument.
Resolve submitted/unknown close legs by deterministic client ID and compare
preflight versus current exact-position size. Transition the batch to
`succeeded`, `partial_failed`, or `recovery_required` without guessing.

Integrate this reconciler after the existing binding reconciliation so newly
triggered entry legs are current before management transitions are derived.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_reconciliation.py
git commit -m "feat: reconcile management batches from exchange state"
```

### Task 8: Replace stop loss across every split position with compensation

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:176-306`
- Modify: `src/telegram_kol_research/protection_attribution.py:49-225`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_protection_attribution.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write failing all-position TPSL tests**

Require:

- one explicit stop price on every position;
- per-position `avgPx` for break-even;
- preservation of every take-profit row, not only the last TP price;
- zero cancels if any position's protection is ambiguous or changes after
  preflight;
- exact cancellation by old TPSL order ID;
- replacement failure immediately recreates the complete old protection for
  that position and stops later legs;
- restore success produces `restored/partial_failed`;
- restore failure produces `recovery_required`;
- earlier successful positions remain changed and are reported explicitly.

Add a composite regression for `partial_then_break_even`: close legs must be
exchange-confirmed before the protection phase starts; if the second partial
promotes to full exit, no replacement stop is created.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_executor.py -k 'stop or break_even' \
  tests/test_protection_attribution.py \
  tests/test_deepcoin_execution_actions.py -k tpsl
```

Expected: FAIL because current adjustment requires exactly one position and
flattens multiple take profits into one value.

**Step 3: Implement full snapshots and compensation**

Represent protection snapshots as ordered rows, including purpose, trigger
price, size/full-position semantics, trigger type, and order ID. Build one
replacement request per required Deepcoin TPSL row. Re-read pending protection
immediately before the first cancel and compare its fingerprint with preflight.

Move automated TPSL changes behind the batch executor. Keep exact manual
helpers only if they preserve the same ownership and ambiguity gates.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/protection_attribution.py \
  tests/test_strategy_management_executor.py \
  tests/test_protection_attribution.py tests/test_deepcoin_execution_actions.py
git commit -m "feat: adjust protection for every strategy position"
```

### Task 9: Add restart recovery and per-strategy serialization

**Files:**
- Create: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/web_app.py:1810-2035`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing recovery and concurrency tests**

Test:

- only one nonterminal batch can claim a strategy;
- full exit blocks stop changes and partial closes;
- worker restart discovers `reserved`, `submitted`, `submit_unknown`, and
  `reconciling` batches;
- recovery queries exchange before any state transition;
- `submit_unknown` is never submitted again;
- `recovery_required` remains paused;
- two workers racing claim only one batch.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_strategy_management_worker.py \
  tests/test_web_app.py -k management_worker
```

Expected: FAIL because there is no resumable worker.

**Step 3: Implement a bounded background worker**

Use the existing Web lifespan/background-task pattern. Process a small bounded
number per tick, catch per-batch failures, and leave durable status for the
next tick. Keep the existing in-process position-authority mutex as a local
optimization, with database claims as the actual concurrency authority.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/strategy_management_batches.py \
  tests/test_strategy_management_worker.py tests/test_web_app.py
git commit -m "feat: recover and serialize management batches"
```

### Task 10: Add deduplicated operator notifications and batch visibility

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write failing notification and rendering tests**

Require messages for `blocked`, `partial_failed`, `submit_unknown`, and
`recovery_required` with batch, source message, lifecycle, strategy, binding,
and per-position results. Test deduplication by `(batch_id, state,
payload_fingerprint)` and a new notification only when state changes.

Add a read-only management-batch panel/API showing shadow/live mode, intent,
effective action, round, targets, planned sizes, protection snapshots, state,
and reason. Never render credentials or complete raw API headers.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_system_operator_bot.py -k management \
  tests/test_web_app.py -k management_batch \
  tests/test_web_page_render.py -k management_batch \
  tests/test_web_assets_smoke.py -k management_batch
```

Expected: FAIL because notification and UI paths do not exist.

**Step 3: Implement formatter, delivery claim, and read-only view**

Reuse the operator bot transport and durable claim pattern, not the strategy
marketing-alert path. Redact API payloads to business fields. The UI must label
shadow batches as `未调用交易 API` and recovery-required batches as
`禁止自动重试`.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/static/app.css \
  tests/test_system_operator_bot.py tests/test_web_app.py \
  tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "feat: report strategy management batch state"
```

### Task 11: Cut over the authoritative path and block legacy management writes

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py:45-69`
- Modify: `src/telegram_kol_research/recovery_live_submit.py:128-177`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:59-106`
- Modify: `src/telegram_kol_research/trade_signals.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_recovery_live_submit.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write failing cutover tests**

Prove:

- authoritative management in disabled mode records a safe skip;
- shadow mode creates only a batch;
- live mode plans then executes only by batch ID;
- entry-signal auto trading retains its existing gate and path;
- a legacy pending `close_position`, `adjust_stop_loss`, or composite trade
  signal without batch ID fails closed before any API call;
- no candidate can reach both old and new management submission paths.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py -k management \
  tests/test_deepcoin_execution_actions.py -k legacy
```

Expected: FAIL because the generic trade-signal route still owns management.

**Step 3: Implement the single-path cutover**

Route management candidates to planner/batch executor. Keep trade signals for
entry/recovery workflows. If compatibility requires a management trade-signal
record for audit, it must contain `management_batch_id` and dispatch only that
batch; otherwise reject it.

Before production deployment, add a read-only command/query documenting counts
of pending legacy management signals so the operator can review them. Do not
auto-convert or execute them.

**Step 4: Run GREEN tests**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/trade_signals.py \
  tests/test_auto_trade_execution.py tests/test_recovery_live_submit.py \
  tests/test_deepcoin_execution_actions.py
git commit -m "fix: route management through durable batches"
```

### Task 12: Document, verify, deploy disabled, and observe shadow plans

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Test: `tests/test_cli_smoke.py`

**Step 1: Add documentation and smoke-test assertions**

Document:

- identity chain and batch states;
- disabled/shadow/live semantics;
- read-only queries for recent batches and legs;
- legacy pending management-signal audit;
- recovery-required operator procedure;
- explicit prohibition on enabling live management during this rollout.

Add any read-only CLI needed for `audit-management-batches`; it must print
counts and redacted identifiers and never mutate exchange or database state.

**Step 2: Run focused verification**

```bash
.venv/bin/pytest -q \
  tests/test_strategy_management_batches.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_sizing.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_auto_trade_execution.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_protection_attribution.py \
  tests/test_system_operator_bot.py
```

Expected: PASS.

**Step 3: Run the full local suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS with no new failures. Record any accepted pre-existing baseline
separately; do not hide a new management-related failure in the baseline.

**Step 4: Run read-only local schema and mode checks**

Create a temporary database, initialize it twice, and verify idempotent schema
creation. Confirm defaults are:

```text
auto_trade_enabled=false
management_execution_mode=disabled
```

**Step 5: Update durable project docs and commit**

```bash
git add docs/migration-handoff.md docs/runbook.md docs/server-deployment.md \
  tests/test_cli_smoke.py src/telegram_kol_research/cli.py
git commit -m "docs: add strategy management batch runbook"
```

**Step 6: Request code review before deployment**

Use `superpowers:requesting-code-review`. Resolve all high-risk identity,
idempotency, TPSL, and recovery findings, then rerun focused and full tests.

**Step 7: Push and deploy with both live gates off**

```bash
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

Verify on the server:

```bash
cd /opt/telegram-kol-analyzer
git rev-parse HEAD
systemctl is-active telegram-kol.service
sqlite3 -readonly data/research.db \
  "SELECT value_json FROM trading_settings WHERE key='global';"
```

Expected: server HEAD equals the reviewed commit, service is `active`, global
automatic trading is false, and management mode is `disabled`.

**Step 8: Run server tests and read-only audits**

Run focused server tests. Run the legacy signal and batch audit. Do not pass an
apply/live flag and do not use a test order.

**Step 9: Enable shadow mode only after disabled verification**

Change only `management_execution_mode` to `shadow`; keep
`auto_trade_enabled=false`. Observe naturally arriving partial take-profit,
full-exit, and stop-adjustment messages. For each, compare the planned
lifecycle, strategy, binding, every `posId`, quantity, TPSL snapshot, and reason
against read-only Deepcoin data.

**Step 10: Stop at the approval gate**

Report the shadow observations, test counts, commit SHA, server service state,
legacy-signal count, blocked/recovery batches, and any mismatch. Do not enable
`live`. Live management requires a new explicit user approval after reviewing
this evidence.

---

## Completion Criteria

- Management never selects by symbol and side alone.
- Every target position has unique verified ownership and immutable batch-leg
  identity.
- First partial uses explicit percentage or default 50%; the second distinct
  partial closes all remaining strategy positions.
- All split positions participate proportionally within contract constraints.
- Full exit and partial close use exact `closePosId` and deterministic regular
  order `clOrdId` values.
- Stop changes cover all strategy positions and preserve every take profit.
- Unknown submission and failed TPSL restoration never auto-retry.
- Lifecycle and partial round advance only from reconciled exchange truth.
- Duplicate messages, worker races, and restarts cannot duplicate a close.
- Production deployment finishes in disabled mode and stops after shadow
  evidence, pending separate live approval.
