# Protection-Recovery Full-Exit Bypass Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow an explicitly recognized, precisely attributed full-exit message to close only its verified Deepcoin position when protection recovery is unresolved, while every other management operation remains fail-closed.

**Architecture:** The planner admits a full-exit batch only after the normal unique binding, exact `posId`, and live-economics checks pass, then writes an immutable bypass marker into its target snapshot. The existing executor submits the exact `closePosId` market close and reconciliation alone can terminalize the lifecycle. Selective operator notifications expose bypass submission and outcome.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, Deepcoin, Telegram system-operator bot.

---

### Task 1: Plan only verified full exits through a protection incident

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_planner.py:106-128, 305-323, 575-650, 1252-1338`
- Test: `tests/test_strategy_management_planner.py`

**Step 1: Write the failing test**

Add a `PositionProtectionIncident` fixture for the exact entry leg. Assert a `full_exit` produces a `ready` batch with reason `protection_recovery_bypassed_for_full_exit` and an immutable snapshot marker containing the original reason, lifecycle ID, binding ID, and `target_pos_ids=["pos-b"]`. Parameterize `adjust_stop_loss`, `move_stop_to_break_even`, and `partial_take_profit`; each must remain `blocked/protection_recovery_required`. Also cover missing `posId`, non-verified ownership, competing bindings, and absent live target positions: all remain blocked with their existing exact-identity reason.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy_management_planner.py -k 'protection_incident or full_exit' -v`

Expected: FAIL because the current incident gate blocks every action.

**Step 3: Write minimal implementation**

Read the incident once after exact entry-leg planning. Keep `_persist_blocked(..., "protection_recovery_required")` for every non-full-exit intent. For `full_exit` only, continue through existing live snapshot, contract-spec, canonical economics, and exact-leg validation, then add a versioned `protection_recovery_bypass` snapshot object before `management_target_fingerprint`. Mark a ready live batch with `protection_recovery_bypassed_for_full_exit`. Permit replacement only of the same zero-leg `blocked/full_exit/protection_recovery_required` preflight record; do not relax active predecessor or partial/unknown recovery checks.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy_management_planner.py -v`

Expected: PASS.

**Step 5: Commit**

Run: `git add src/telegram_kol_research/strategy_management_planner.py tests/test_strategy_management_planner.py && git commit -m "feat: permit exact full exit during protection recovery"`

### Task 2: Guard exact, one-time bypass execution and reconciliation

**Files:**

- Modify: `src/telegram_kol_research/strategy_management_executor.py:2250-2365` (only if guard is missing)
- Modify: `src/telegram_kol_research/strategy_management_worker.py:90-350` (only if scheduling diagnostics are missing)
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py:70-205` (only if it must preserve bypass evidence)
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write the failing tests**

Create a ready bypass batch with a single verified short `pos-b`. Assert the sole exchange request is a market buy with `closePosId="pos-b"` and the preflight size; the batch is submitted/reconciling, not terminal. Add negative cases for changed economics or size, missing target, unverified leg, corrupted marker, and existing close reservation; every case must produce zero exchange writes. Add restart coverage proving a submitted bypass batch is reconciled rather than resubmitted. Finally, assert lifecycle terminalization occurs only after every exact target `posId` disappears from a coherent exchange snapshot; a still-present target or one remaining member of a multi-position batch must stay non-terminal.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy_management_executor.py tests/test_strategy_management_worker.py tests/test_strategy_management_reconciliation.py -k 'bypass or exact_close' -v`

Expected: FAIL until the bypass marker has executor guards and fixtures.

**Step 3: Write minimal implementation**

Accept the bypass only for a live `full_exit`/`full_close` when immutable marker lifecycle, binding, and managed-leg `posId` values exactly match current state. Reuse `_close_payload`, per-leg reservation, deterministic client IDs, `_record_leg_event`, and existing exact-position reconciliation. On marker or live-preflight drift, freeze with a deterministic reason and make no request. Do not call the manual close helper, scan by symbol, or terminalize on a submission event.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy_management_executor.py tests/test_strategy_management_worker.py tests/test_strategy_management_reconciliation.py -v`

Expected: PASS.

**Step 5: Commit**

Run: `git add src/telegram_kol_research/strategy_management_executor.py src/telegram_kol_research/strategy_management_worker.py src/telegram_kol_research/strategy_management_reconciliation.py tests/test_strategy_management_executor.py tests/test_strategy_management_worker.py tests/test_strategy_management_reconciliation.py && git commit -m "feat: guard protection-recovery full exits"`

### Task 3: Add auditable UI, notification, and server verification

**Files:**

- Modify: `src/telegram_kol_research/web_app.py:252-335`
- Modify: `src/telegram_kol_research/system_operator_bot.py:215-320, 760-920`
- Modify: `docs/runbook.md`
- Test: `tests/test_web_app.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write the failing tests**

Assert management API rows expose bounded bypass flag, original reason, exact `posId`s, and exchange order IDs. Assert one durable redacted operator alert for bypass submission and one for terminal/recovery outcome, with no duplicate from a repeat tick; a normal full exit must not get this extra alert.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_system_operator_bot.py tests/test_web_app.py -k 'management and bypass' -v`

Expected: FAIL because bypass state is neither exposed nor selectively notified.

**Step 3: Write minimal implementation**

Derive bypass status only from the immutable snapshot marker, not from free-form text. Add bounded API fields and use the existing notification fingerprint/lease for idempotent submit, success, and failure notices. Preserve the current `recovery_required` no-auto-retry label and redaction rules. Add a `sqlite3 -readonly` runbook query joining batch, leg, event, and lifecycle data without selecting raw signed payloads or credentials.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_system_operator_bot.py tests/test_web_app.py -v && pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_strategy_management_worker.py tests/test_strategy_management_reconciliation.py -v && git diff --check`

Expected: all tests PASS and no whitespace errors.

**Step 5: Commit and deploy**

Run: `git add src/telegram_kol_research/web_app.py src/telegram_kol_research/system_operator_bot.py docs/runbook.md tests/test_web_app.py tests/test_system_operator_bot.py && git commit -m "feat: audit protection-recovery full exits"`. Push reviewed commits to `codex/deepcoin-auto-trading-v1`, run `./scripts/server_git_update.sh`, then use the new read-only audit, service journal, and Deepcoin snapshot to verify production without opening a new live position.
