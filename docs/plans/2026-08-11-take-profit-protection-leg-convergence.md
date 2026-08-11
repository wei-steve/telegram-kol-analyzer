# Take-Profit Protection-Leg Convergence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Converge a verified take-profit order onto its logical protection leg without exchange mutation and prevent equivalent decimal formatting from splitting durable truth again.

**Architecture:** The normal submit path uses a unique exact-decimal matcher instead of raw text equality. A separate supervised repair planner validates exact binding, leg, position, durable order ownership, and live pending-order evidence before an atomic logical-leg bind. Existing deployment safety behavior remains fail closed.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, Typer, pytest, Deepcoin read-only TPSL adapters.

---

### Task 1: Make filled-position binding idempotent

**Files:**
- Modify: `tests/test_position_protection_legs.py`
- Modify: `src/telegram_kol_research/position_protection_legs.py:189-200`

**Step 1: Write the failing test**

Add a test that binds a planned protection leg, records its resulting
`updated_at`, calls `bind_filled_position` again with the same `pos_id`, and
asserts the timestamp and status remain unchanged.

**Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest -q tests/test_position_protection_legs.py::test_bind_filled_position_does_not_touch_unchanged_leg
```

Expected: FAIL because the second call advances `updated_at`.

**Step 3: Implement the minimal fix**

Capture the original normalized `pos_id` and status, apply immutable binding
and the `planned -> waiting_fill` transition, and call `_touch` only if either
durable value changed. Keep `session.flush()` so callers retain current flush
semantics.

**Step 4: Run focused tests**

```bash
uv run pytest -q tests/test_position_protection_legs.py tests/test_execution_bindings.py
```

Expected: PASS.

### Task 2: Match logical take-profit legs by exact decimal value

**Files:**
- Modify: `tests/test_trigger_take_profit_convergence_executor.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py:209-243`

**Step 1: Write the failing tests**

Add a success test where the planned price is `1.2300` and the submitted
payload price is `1.23`; assert the logical leg becomes `verified` with the
returned order ID. Add refusal tests for malformed prices and two pending legs
that normalize to the same decimal value.

**Step 2: Run the tests to verify they fail**

```bash
uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py -k 'decimal_price or ambiguous_logical_leg'
```

Expected: the equivalent-price test leaves the leg pending and the ambiguity
test does not fail closed through a dedicated selection result.

**Step 3: Implement the minimal matcher**

Add a private exact-decimal parser and select only one take-profit leg belonging
to the exact execution leg whose non-zero planned trigger price equals the
payload price numerically. Treat zero, invalid, or multiple matches as a
post-write persistence conflict so the convergence freezes without retrying
the already attempted exchange write.

**Step 4: Run focused tests**

```bash
uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py tests/test_position_protection_legs.py
```

Expected: PASS.

### Task 3: Add supervised existing-order repair

**Files:**
- Create: `src/telegram_kol_research/take_profit_protection_leg_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_take_profit_protection_leg_repair.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing planner tests**

Create the production-shaped split truth: one active verified entry, one
pending logical TP leg, one active durable TP order, one exact verified
protection-ledger row, and one matching pending exchange row. Assert a dry-run
returns exactly one deterministic action. Add one-at-a-time refusal tests for
ambiguous logical legs, missing/mismatched ledger ownership, stale exchange
read-back, wrong position identity, and non-active entry state.

**Step 2: Verify planner tests fail**

```bash
uv run pytest -q tests/test_take_profit_protection_leg_repair.py -k plan
```

Expected: FAIL because the repair module does not exist.

**Step 3: Implement the read-only planner**

Load candidates in a read transaction, capture pending TPSL rows once per
instrument, and validate exact order ID plus binding, leg, `posId`, instrument,
side, normalized price, and active-order submitted size. Return bounded action
and refusal dataclasses with a SHA-256 fingerprint and derived confirmation
token; never include raw exchange response bodies in the fingerprint payload.

**Step 4: Write failing apply tests**

Assert apply refuses a wrong action ID, fingerprint, token, changed live order,
or ownership collision. Assert the valid path changes only the selected logical
leg to `verified`, stores bounded read-back evidence, performs zero exchange
writes, and is idempotent on a second dry-run.

**Step 5: Verify apply tests fail**

```bash
uv run pytest -q tests/test_take_profit_protection_leg_repair.py -k apply
```

Expected: FAIL because apply is not implemented.

**Step 6: Implement atomic apply and CLI**

Use the shared cross-process position-authority lock, rebuild the plan with a
fresh exchange snapshot, compare action ID/fingerprint/token, revalidate the
database row inside the write transaction, and call
`bind_verified_exchange_order`. Add `repair-take-profit-protection-leg` with
dry-run default and explicit `--apply`, `--action-id`,
`--expected-fingerprint`, and `--confirmation-token` requirements.

**Step 7: Run repair and CLI tests**

```bash
uv run pytest -q tests/test_take_profit_protection_leg_repair.py tests/test_cli_smoke.py
```

Expected: PASS.

### Task 4: Review, deploy repair, and resume Task 17

**Files:**
- Modify after successful deployment: `docs/runbook.md`
- Modify after successful deployment: `docs/plans/2026-08-10-unified-execution-truth-legacy-impact.md`

**Step 1: Run local gates**

```bash
git diff --check
uv run pytest -q tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_take_profit_protection_leg_repair.py \
  tests/test_deployment_preflight.py
uv run pytest -q
```

Expected: PASS with only known warnings.

**Step 2: Request strict code review**

Review for price ambiguity, ownership by price alone, exchange-write leakage,
TOCTOU, unknown-outcome retry, unsafe repair evidence, and historical replay.
Resolve all Critical and Important findings.

**Step 3: Commit and push reviewed code**

```bash
git add src/telegram_kol_research/position_protection_legs.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  src/telegram_kol_research/take_profit_protection_leg_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_take_profit_protection_leg_repair.py tests/test_cli_smoke.py
git commit -m "fix: converge verified take profit protection"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Run production dry-run and supervised apply**

From an exact detached candidate checkout on the server, run the new command in
dry-run mode. Require exactly one action and no refusals affecting that action.
Re-run with its exact action ID, fingerprint, and confirmation token. Confirm
that no exchange writer method is called and the logical leg becomes verified.

**Step 5: Rerun managed deployment**

```bash
EXPECTED_COMMIT=<reviewed-full-sha> CHANGE_CLASS=schema_compatible \
  ./scripts/server_git_update.sh
```

Require backup and migration dry-run success, no fresh active exchange work,
exact deployed SHA, active service, HTTP 200, disabled execution-contract mode,
zero production contract rows, and no activation-created trade/binding/mutation
or exchange event.

**Step 6: Record bounded deployment evidence**

Update the Task 17 runbook and legacy-impact evidence with commit, repair count,
mode, watermarks, table counts, tests, health checks, monitor result, and
rollback command. Commit and push documentation without raw messages, order
IDs, position IDs, or credentials.
