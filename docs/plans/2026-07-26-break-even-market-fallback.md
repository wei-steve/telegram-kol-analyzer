# Break-Even Market Fallback Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Validate every break-even stop against a fresh Deepcoin market price and convert an invalid break-even stop into an exact-position market full exit.

**Architecture:** Add one pure, Decimal-based market policy shared by the planner and executor. The planner persists a versioned decision in the immutable target snapshot and chooses either protection replacement or `full_exit`; the executor re-evaluates the same policy immediately before the first exchange write and rejects stale decisions.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, existing Deepcoin client and durable management batch state machine.

---

### Task 1: Add the pure break-even market policy

**Files:**
- Create: `src/telegram_kol_research/strategy_management_market_policy.py`
- Create: `tests/test_strategy_management_market_policy.py`

**Step 1: Write failing policy tests**

Cover:

```python
assert assess_break_even_market(side="long", entry_price="100", market_price="101").allowed
assert not assess_break_even_market(side="long", entry_price="101", market_price="101").allowed
assert assess_break_even_market(side="short", entry_price="101", market_price="100").allowed
assert not assess_break_even_market(side="short", entry_price="100", market_price="101").allowed
```

Also assert invalid side, missing price, non-finite price and non-positive price raise
`BreakEvenMarketPolicyError`.

**Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/test_strategy_management_market_policy.py
```

Expected: FAIL because the policy module does not exist.

**Step 3: Implement the minimal policy**

Create an immutable `BreakEvenMarketDecision` containing normalized side, entry
price, market price, comparison, `allowed`, and fallback action. Parse with
`Decimal`; use strict `<` for long and strict `>` for short.

**Step 4: Run the tests and verify GREEN**

Run:

```bash
pytest -q tests/test_strategy_management_market_policy.py
```

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_market_policy.py tests/test_strategy_management_market_policy.py
git commit -m "feat: define break-even market policy"
```

### Task 2: Make the planner choose protection or exact full exit

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `tests/test_strategy_management_planner.py`

**Step 1: Write failing planner tests**

Extend `_ReadOnlyDeepcoin` with `get_ticker_price`. Test:

- short entry `64602.9`, ticker `64688.6` produces
  `intent=move_stop_to_break_even`, `effective_action=full_exit`,
  `effective_fraction=1.0`, and full preflight size;
- long and short allowed cases remain protection actions;
- equality becomes `full_exit`;
- missing/invalid ticker produces a blocked batch and no exchange write;
- a protection incident does not block the risk-reducing fallback full exit;
- the target snapshot contains an exact versioned
  `break_even_market_decision`.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q tests/test_strategy_management_planner.py -k "break_even and market"
```

Expected: assertions fail because the planner does not read ticker or promote the
effective action.

**Step 3: Implement the minimal planner change**

Pass the read-only Deepcoin client into the locked planner. After exact economics
are proven, read one ticker for the lifecycle instrument and call the shared
policy for `move_stop_to_break_even`. Set `effective_action="full_exit"` and
`effective_fraction=1.0` when disallowed. Move the protection-recovery decision
below this effective-action decision and key bypass behavior off
`effective_action`, not only the original intent. Persist the decision in the
target snapshot and resolve a restored predecessor when the effective action is
full exit.

**Step 4: Run planner tests and verify GREEN**

Run:

```bash
pytest -q tests/test_strategy_management_planner.py
```

Expected: all planner tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py tests/test_strategy_management_planner.py
git commit -m "feat: plan market fallback for invalid break-even"
```

### Task 3: Recheck the market at the final write boundary

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `tests/test_strategy_management_executor.py`

**Step 1: Write failing executor tests**

Test both persisted decision forms:

- allowed protection decision whose fresh ticker crosses entry is blocked before
  any TPSL cancellation;
- fallback full-exit decision whose fresh ticker returns to the allowed side is
  blocked before deferred-entry cancellation or close submission;
- unchanged invalid short decision reaches one exact market close request;
- absent, malformed or tampered decision marker is rejected;
- missing ticker blocks without exchange writes.

**Step 2: Run focused tests and verify RED**

Run:

```bash
pytest -q tests/test_strategy_management_executor.py -k "break_even and market"
```

Expected: assertions fail because no final market-decision validator exists.

**Step 3: Implement the final-boundary validator**

Validate the versioned snapshot marker against batch intent, effective action,
instrument, side and per-leg entry price. Fetch ticker immediately before the
first exchange mutation and evaluate the shared policy. Require the fresh result
to match the persisted allowed/fallback choice. On drift, transition to a
retryable blocked state without canceling or submitting anything.

**Step 4: Run executor tests and verify GREEN**

Run:

```bash
pytest -q tests/test_strategy_management_executor.py
```

Expected: all executor tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_executor.py tests/test_strategy_management_executor.py
git commit -m "feat: guard break-even writes with fresh market"
```

### Task 4: Preserve authorization and retry semantics

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write failing compatibility tests**

Assert that only the exact pair
`("move_stop_to_break_even", "full_exit")` is accepted as a break-even fallback;
unrelated intent/action mismatches remain rejected. Assert a batch blocked solely
because its market decision changed can be replaced by a new plan with the same
message idempotency identity.

**Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/test_deepcoin_execution_actions.py tests/test_strategy_management_planner.py -k "break_even or retry"
```

Expected: the new pair and retry state are not recognized.

**Step 3: Implement the minimal compatibility changes**

Add the exact fallback pair to action matching. Add one bounded retryable reason
for pre-submit market-decision drift; require no submission evidence before
replacement.

**Step 4: Run tests and verify GREEN**

Run:

```bash
pytest -q tests/test_deepcoin_execution_actions.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_worker.py tests/test_deepcoin_execution_actions.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py
git commit -m "fix: authorize break-even full-exit fallback"
```

### Task 5: Run local regression checks

**Files:**
- No source changes unless a regression is found.

**Step 1: Run focused management suites**

```bash
pytest -q \
  tests/test_strategy_management_market_policy.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_management_remediation.py \
  tests/test_deepcoin_execution_actions.py
```

Expected: all pass.

**Step 2: Inspect scope**

```bash
git status --short
git diff --check
git log --oneline -8
```

Expected: unrelated `uv.lock` and existing untracked inspection artifacts remain
untouched; no whitespace errors.

### Task 6: Deploy and complete the approved live remediation

**Files:**
- Production database and service state only; no guessed writes.

**Step 1: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch reaches the reviewed local HEAD.

**Step 2: Update the server while the service remains stopped**

Use the repository’s server update helper or the equivalent explicit pull,
editable install and revision verification. Do not start the service yet.

**Step 3: Run server-side focused tests**

Run the market policy, planner, executor and remediation suites against the
deployed checkout. Expected: all pass.

**Step 4: Repair the restored TPSL ledger from exchange proof**

For position `1001124377347114`, prove old stop
`1001124377347113` is absent, restored stop `1001124380272499` is pending at
`65200` for size `16`, and the exact position is still live. Mark only the proven
old ledger row cancelled and upsert the proven restored row.

**Step 5: Resolve the compensated predecessor**

Only after Step 4, terminally resolve batch `67` as a restored failed attempt.
Regenerate the same approved message action. Verify the new immutable batch has
`effective_action=full_exit`, exact `posId=1001124377347114`, close size `16`,
and a current invalid-break-even market marker.

**Step 6: Execute exactly once and reconcile**

Keep the live gate open only for this confirmed batch, submit once, and reconcile
until a proven terminal result. Never retry an unknown submission.

**Step 7: Restore safe settings and service**

Close the live gate, verify no approved remediation batch remains unresolved,
restart `telegram-kol.service`, and inspect service status and recent logs.

**Step 8: Final real-position audit**

Cross-check every current Deepcoin position, its owning message, management
history, pending TPSL rows and protection ledger. Report newly discovered,
unapproved actions separately without executing them.
