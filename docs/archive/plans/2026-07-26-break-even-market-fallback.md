# Per-Position Break-Even Market Execution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute a break-even message per exact position: set break-even where the market permits and market-close only positions where it does not.

**Architecture:** The planner creates one immutable `break_even_by_market` batch without freezing a volatile market choice. At the executor’s final pre-write boundary, one structured Deepcoin ticker read produces an append-only, unique batch decision containing one action per exact `posId`; all exchange writes then resume from that durable decision.

**Tech Stack:** Python 3.14, SQLAlchemy/SQLite, pytest, existing Deepcoin client, management batch executor and reconciliation state machines.

---

### Task 1: Pure directional market policy

**Files:**
- Create: `src/telegram_kol_research/strategy_management_market_policy.py`
- Create: `tests/test_strategy_management_market_policy.py`

1. Write tests for strict long `<`, short `>`, equality rejection and invalid decimals.
2. Run `pytest -q tests/test_strategy_management_market_policy.py`; verify RED.
3. Implement immutable Decimal-based `BreakEvenMarketDecision`.
4. Re-run; expect all pass.
5. Commit `feat: define break-even market policy`.

### Task 2: Structured Deepcoin ticker evidence

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `tests/test_deepcoin_client.py`

1. Write failing tests for `get_ticker_quote(inst_id=...)` returning exactly:

```python
{
    "instrument_id": "BTC-USDT-SWAP",
    "price": "64688.6",
    "price_field": "last",
}
```

2. Test `lastPx` fallback, duplicate target instruments, missing `last/lastPx`,
   invalid values and wrong instrument; all unsafe cases return no quote or raise
   a deterministic read error.
3. Run focused tests and verify RED.
4. Implement the structured reader; keep `get_ticker_price` as a compatibility
   wrapper over it.
5. Run the full Deepcoin client suite; expect all pass.
6. Commit `feat: expose structured Deepcoin ticker evidence`.

### Task 3: Append-only per-position decision model

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py` only if legacy bootstrap needs an index
- Create: `src/telegram_kol_research/strategy_management_market_decisions.py`
- Create: `tests/test_strategy_management_market_decisions.py`
- Modify: `tests/test_db_bootstrap.py`

1. Write failing model/bootstrap tests for a new
   `StrategyManagementMarketDecision` table with one unique row per batch.
2. Write failing persistence tests that:
   - store stable, sorted per-position decisions and a SHA-256 fingerprint;
   - return the same row for an identical retry;
   - reject a conflicting second decision;
   - require batch intent/action and exact leg identity.
3. Run focused tests and verify RED.
4. Add model fields for batch identity, instrument, quote price/field,
   observed-at, decision JSON and fingerprint. Add FK and unique batch index.
5. Implement create-or-load with transaction-local CAS validation.
6. Run focused tests and bootstrap tests; expect all pass.
7. Commit `feat: persist per-position break-even decisions`.

### Task 4: Plan immutable market-managed batches

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Modify: `tests/test_strategy_management_planner.py`

1. Write failing tests showing `move_stop_to_break_even` plans:
   - `effective_action=break_even_by_market`;
   - one exact leg per owned `posId`;
   - no preselected close/protection action;
   - per-leg cost, size and contract step;
   - no ticker read during planning.
2. Test that an open protection incident does not prematurely block planning;
   final market decision will decide whether that leg needs protection evidence.
3. Test unsupported intent/action pairs remain blocked.
4. Run focused tests and verify RED.
5. Implement minimal planner/batch support without changing message idempotency or
   immutable target fingerprint.
6. Run the complete planner suite; expect all pass.
7. Commit `feat: plan per-position break-even execution`.

### Task 5: Pre-write decision and zero-write validation

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `tests/test_strategy_management_executor.py`

1. Write failing tests for final-boundary decisions:
   - one allowed short -> `set_break_even`;
   - one invalid/equal short -> `full_exit`;
   - mixed multi-position decisions remain per-position;
   - missing/unsafe ticker -> blocked with zero writes;
   - identity or size drift -> blocked with zero writes;
   - any allowed leg lacking complete unique TPSL evidence -> blocked with zero
     writes;
   - exact retry loads the prior durable decision without another ticker read;
   - conflicting stored decision -> recovery required with zero writes.
2. Run focused tests and verify RED.
3. Implement a single final pre-write function that reads exact positions,
   deferred entries, TPSL and structured ticker; computes all leg actions; validates
   every required protection row; then commits the unique decision.
4. Assert the function is called before deferred-entry cancellation and every
   close/TPSL mutation.
5. Run focused and full executor tests; expect all pass.
6. Commit `feat: reserve per-position break-even actions`.

### Task 6: Execute mixed close and protection legs

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_strategy_management_reconciliation.py`

1. Write failing tests showing:
   - invalid legs submit exact-size market closes once;
   - allowed legs replace complete TPSL at their own average entry;
   - deferred entries are cancelled iff at least one leg is `full_exit`;
   - close unknown is never retried;
   - TPSL rejection restores only that leg’s protection and ledger;
   - mixed results aggregate to the correct batch status;
   - reconciliation closes only confirmed full-exit legs and confirms only proven
     protection legs.
2. Run focused tests and verify RED.
3. Route each leg by the persisted decision. Reuse existing close reservation,
   protection replacement and compensation primitives; do not duplicate exchange
   submit logic.
4. Update worker/reconciliation accepted action sets and terminal aggregation.
5. Run complete executor, worker and reconciliation suites; expect all pass.
6. Commit `feat: execute mixed break-even position actions`.

### Task 7: Authorization and predecessor recovery

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/strategy_management_batches.py`
- Modify relevant tests.

1. Write failing tests that only
   `move_stop_to_break_even -> break_even_by_market` is authorized for this path.
2. Test worker-direct execution requires a valid decision record after reservation.
3. Add an explicit transaction helper that resolves a proven restored predecessor
   and creates its successor atomically; do not use JSON-only auto-resolution.
4. Test transaction rollback leaves predecessor unchanged when successor creation
   fails.
5. Run tests; verify GREEN.
6. Commit `fix: authorize durable break-even market execution`.

### Task 8: Regression and review

1. Run:

```bash
PYTHONPATH=src python -m pytest -q \
  tests/test_strategy_management_market_policy.py \
  tests/test_deepcoin_client.py \
  tests/test_strategy_management_market_decisions.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_position_management_remediation.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_db_bootstrap.py
```

2. Run `git diff --check` and inspect the exact commit range.
3. Obtain independent code review for Critical/Important findings.
4. Fix findings with new failing tests first; repeat regression.

### Task 9: Deploy and finish the approved real-position action

1. Integrate reviewed commits into `codex/deepcoin-auto-trading-v1` and push.
2. Keep `telegram-kol.service` stopped; pull from GitHub and reinstall editable
   package on the server.
3. Run focused server tests and verify deployed revision.
4. Back up production DB with size and SHA-256.
5. Prove position `1001124377347114` still exists at exact size, old stop
   `1001124377347113` is absent, and restored stop `1001124380272499` is pending
   at `65200` for size `16`.
6. Repair only those proven ledger facts.
7. Atomically resolve restored batch 67 and create the new
   `break_even_by_market` batch.
8. Open the live gate only for the confirmed batch, execute once, and reconcile
   to a proven terminal result. Never retry unknown submission.
9. Close the gate, audit every real position/message/protection row, restart the
   service, and inspect status/logs.
10. Report newly discovered unapproved actions without executing them.
