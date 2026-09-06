# Deepcoin Reviewed Pending-Entry Cancellation Quiescence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a durable cross-process lease that prevents the live entry-revision worker and reviewed pending-entry cancellation CLI from overlapping exchange writes while leaving protection workers active.

**Architecture:** Store one closed-schema authority record in a dedicated `TradingSetting` row and acquire it atomically with SQLite `BEGIN IMMEDIATE`. Both entry-revision execution and reviewed cancellation apply honor the lease; cancellation additionally requires frozen auto-trade and disabled entry revision. Release only on proven pre-write exits or fully confirmed terminal completion, and retain on unknown outcomes or unhandled exceptions.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, existing trading settings, entry-revision executor, mutation-intent and reviewed cancellation modules.

---

### Task 1: Implement the durable authority lease primitive

**Files:**
- Create: `src/telegram_kol_research/entry_revision_exchange_authority.py`
- Create: `tests/test_entry_revision_exchange_authority.py`

**Step 1: Write failing closed-schema and atomic-acquisition tests**

Cover absent/idle acquisition, independent-session contention, malformed JSON,
unsupported version/state/owner, exact token ownership and cancellation setting
requirements. Assert the second owner receives a bounded reason and never
replaces the first token.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q tests/test_entry_revision_exchange_authority.py
```

Expected: collection fails because the lease module does not exist.

**Step 3: Implement the minimal primitive**

Create bounded immutable acquisition/release result types and these operations:

```python
acquire_entry_revision_exchange_authority(
    session_factory,
    *,
    owner_kind: Literal[
        "entry_revision_worker",
        "reviewed_pending_entry_cancel",
    ],
    owner_id: str,
    acquired_at: datetime,
    require_cancel_quiescence: bool,
) -> EntryRevisionExchangeAuthorityAcquisition

release_entry_revision_exchange_authority(
    session_factory,
    *,
    token: str,
    owner_kind: str,
    released_at: datetime,
) -> EntryRevisionExchangeAuthorityRelease
```

Use a dedicated `TradingSetting` key, `BEGIN IMMEDIATE`, strict JSON parsing and
an exact closed schema. Cancellation acquisition must parse the global settings
and require `auto_trade_enabled is False` plus
`entry_revision_v2_mode == "disabled"` in the same transaction. A missing
global settings row uses the application's safe defaults; malformed persisted
settings fail closed. No timeout or takeover is implemented.

Release must require the exact token and owner. Persist a closed idle document
only on a valid release.

**Step 4: Run GREEN**

```bash
.venv/bin/python -m pytest -q tests/test_entry_revision_exchange_authority.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/entry_revision_exchange_authority.py \
  tests/test_entry_revision_exchange_authority.py
git diff --cached --name-only
git commit -m "feat: add entry revision exchange authority lease"
```

### Task 2: Route the live revision worker through the lease

**Files:**
- Modify: `src/telegram_kol_research/entry_revision_executor.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `tests/test_entry_revision_executor.py`
- Modify: `tests/test_strategy_revision_planner.py`

**Step 1: Write failing worker-boundary tests**

Prove that a cancellation-owned lease returns
`entry_revision_exchange_authority_busy` before any revision batch claim or
exchange method. Prove a normal live worker execution owns the worker lease
during its exchange call and releases it on a fully recorded return. Prove an
unhandled exception retains the worker lease. Cover both the v2 worker and the
legacy `execute_strategy_revision` orchestration; the legacy route must also
refuse before planning when global auto trade is frozen.

Prove that cancel unknown, replacement unknown/readback mismatch and a
post-write claim loss retain worker authority even when the executor converts
them into a normal `recovery_required` or `in_progress` return.

Disabled and shadow modes must remain write-free and must not acquire the lease.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q tests/test_entry_revision_executor.py \
  -k "exchange_authority or disabled_mode or shadow_mode"
```

Expected: the new busy/ownership/retention assertions fail against the
single-process-only executor.

**Step 3: Implement the minimal worker wrapper**

Keep disabled/shadow checks outside the v2 lease. For live mode, acquire owner
kind `entry_revision_worker` before the batch claim and call the existing
executor body under the existing single-process position lock. Route the legacy
revision orchestration through the same owner kind before planning, and refuse
that legacy path while `auto_trade_enabled=false`. Return a bounded in-progress
result on contention. Track whether an exchange-write boundary was reached
across converted results: release only after a fully recorded return; retain
every later unknown/incomplete result, claim loss and escaping exception.

Do not route protection, rescue or non-revision management writers through the
new lease.

**Step 4: Run GREEN and adjacent worker tests**

```bash
.venv/bin/python -m pytest -q tests/test_entry_revision_executor.py \
  tests/test_strategy_revision_planner.py \
  tests/test_auto_trade_execution.py \
  tests/test_position_authority_lock.py \
  tests/test_position_authority_boundary_coverage.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/entry_revision_executor.py \
  tests/test_entry_revision_executor.py
git diff --cached --name-only
git commit -m "fix: serialize live revision exchange authority"
```

### Task 3: Enforce quiescence throughout one exact cancellation apply

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`

**Step 1: Write failing cancellation-boundary tests**

Cover these independent cases:

- auto trade enabled blocks before confirmation consumption and exchange write;
- entry revision live/shadow blocks before confirmation consumption and write;
- a worker-owned lease blocks apply without replacing the worker token;
- after cancellation acquisition, a simulated revision-worker acquisition is
  refused during the Deepcoin call;
- plan drift, intent reservation conflict, last gate failure and intent
  transition failure release because no exchange write started;
- confirmed cancellation plus complete terminalization releases;
- transport error, unconfirmed response, unavailable/changed readback and local
  terminalization failure retain the cancellation lease;
- a retained lease makes a repeated apply fail closed before exchange access;
- the existing exact-one-action and confirmation-token contracts remain.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py \
  -k "quiescence or exchange_authority"
```

Expected: new tests fail because apply does not acquire or retain durable
authority.

**Step 3: Implement the minimal apply integration**

Keep initial validation and the first fresh read-only plan. Acquire cancellation
authority before reserving the mutation intent, then rebuild the plan while the
lease is held. Preserve single-order selection and the existing last-moment
database gate.

Track whether an exchange write may have started. Release on explicit, proven
pre-write refusal or drift and only on final `cancelled` completion after local
terminalization. Every escaping exception retains even when it occurs before
the exchange call. Never release on any post-write unknown/incomplete path.
Return bounded quiescence reason codes without leaking lease tokens.

**Step 4: Run GREEN and the complete cancellation file**

```bash
.venv/bin/python -m pytest -q tests/test_reviewed_pending_entry_cancel.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/reviewed_pending_entry_cancel.py \
  tests/test_reviewed_pending_entry_cancel.py
git diff --cached --name-only
git commit -m "fix: enforce pending-entry cancellation quiescence"
```

### Task 4: Verify, review and record the local candidate

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Run focused and adjacent regression**

```bash
.venv/bin/python -m pytest -q \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_entry_revision_executor.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_deployment_active_write_check.py \
  tests/test_position_authority_lock.py \
  tests/test_position_authority_boundary_coverage.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py
```

**Step 2: Run one final full suite**

```bash
.venv/bin/python -m pytest -q
```

Run it only after production code is settled. Any later production-code edit
creates a new final candidate and requires one new final suite.

**Step 3: Request independent review**

Review the exact base-to-candidate diff for cross-process races, lease stealing,
unknown-result release, settings parsing, protection-worker coupling, token
leakage and missing tests. Resolve every Critical and Important finding before
completion.

**Step 4: Update canonical status**

Record exact base, design/plan/code SHAs, RED and GREEN evidence, focused/full
results, review result, retained TOCTOU boundaries and all prohibited actions
that did not occur.

**Step 5: Commit the status by explicit path**

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: record cancellation quiescence candidate"
```

Do not push, deploy, SSH, freeze, restart or perform any production, database or
exchange write.
