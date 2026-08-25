# Trigger Protection Stale-Wait Terminalization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve exactly the three audited stale terminal trigger-protection intents and prevent unrelated instrument snapshot failures from rewriting other intents.

**Architecture:** Add a narrow, idempotent terminalization helper to the existing execution reconciliation path before its snapshot-error early return. Scope protection snapshot errors to the exact leg instrument while preserving the global fail-closed adoption barrier. Rehearse the frozen helper against a fresh production SQLite copy and require an exact three-row diff.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, existing `execution_bindings` reconciliation and trigger-protection intent state machine.

---

### Task 1: Claim the repair and prove exact stale-wait terminalization RED

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Claim the task in the canonical status**

Set `workstream_status: in_progress`, retain the existing exclusive `claimed_by`, and set `current_task: task-18-trigger-protection-stale-wait-repair`. Do not alter the completed runtime-serialization remediation pointer.

**Step 2: Add the failing integration test**

Reuse `_seed_trigger_protection_adoption()` and `_save_trigger_protection_intent()`. Update the saved intent and leg to the exact predicate, then reconcile with a client whose `list_trigger_order_history(inst_id="ETH-USDT-SWAP")` fails while BTC sources are valid.

The test must assert:

```python
assert intent.recovery_state == "resolved"
assert intent.recovery_disposition == "terminal"
assert intent.last_reason_code == "entry_leg_terminal_after_snapshot_wait"
assert intent.next_attempt_at is None
assert intent.retry_attempts == original_retry_attempts
assert intent.parent_trigger_order_id == original_parent_order_id
assert intent.adopted_order_id is None
assert json.loads(intent.last_evidence_json) == {
    "binding_id": binding_id,
    "execution_order_leg_id": leg_id,
    "instrument_id": "BTC-USDT-SWAP",
    "intent_id": intent_id,
    "leg_status": "manually_closed",
    "pos_id": "pos-1",
    "previous_reason_code": "snapshot_incomplete",
    "schema_version": 1,
    "terminal_reason": "manual_position_missing",
}
```

Run reconciliation twice and assert the second run does not change `updated_at` or add another refusal audit.

**Step 3: Add counterexample parametrization**

Cover one changed condition per case: intent state not `retrying`, disposition not `wait`, reason not `snapshot_incomplete`, nonterminal leg, unverified attribution, missing `pos_id`, parent/order mismatch, and binding mismatch. Each case must leave the intent unchanged.

**Step 4: Run RED**

Run:

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py::test_reconcile_resolves_exact_terminal_stale_wait_intent \
  tests/test_execution_bindings.py::test_reconcile_terminal_stale_wait_predicate_is_exact
```

Expected: the positive test fails because the intent remains `retrying`; counterexamples pass or fail only where the missing helper exposes over-broad behavior.

**Step 5: Commit the witnessed RED tests**

```bash
git add tests/test_execution_bindings.py docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "test: reproduce stale trigger intent wait"
```

### Task 2: Implement the minimal stale-wait terminalization GREEN

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Add the exact predicate helper**

Add a private helper with this contract:

```python
def _resolve_exact_terminal_stale_wait_trigger_protection_intents(
    session,
    *,
    legs: list[ExecutionOrderLeg],
    bindings_by_id: dict[int, ExecutionBinding],
    resolved_at: datetime,
) -> int:
    """Resolve only verified terminal legs stranded by snapshot wait."""
```

It must query only `pending`/`retrying` candidates needed to test the exact predicate, but resolve only `retrying / wait / snapshot_incomplete`. Derive the instrument from `request_json.instId`; use the normalized owning binding instrument only when the request omits it. Require exact intent/leg binding and parent/leg order identity.

For each exact match:

```python
intent.next_attempt_at = None
transition_trigger_protection_intent(
    session,
    intent,
    recovery_state="resolved",
    recovery_disposition="terminal",
    last_reason_code="entry_leg_terminal_after_snapshot_wait",
    last_evidence={
        "schema_version": 1,
        "intent_id": int(intent.id),
        "binding_id": int(intent.execution_binding_id),
        "execution_order_leg_id": int(leg.id),
        "instrument_id": instrument_id,
        "pos_id": str(leg.pos_id),
        "leg_status": str(leg.status).lower(),
        "terminal_reason": str(leg.terminal_reason) if leg.terminal_reason else None,
        "previous_reason_code": "snapshot_incomplete",
    },
)
```

Return the number of transitions. Do not touch `retry_attempts`, immutable submit fields, parent identity, or adopted order identity.

**Step 2: Invoke before snapshot-error early return**

In `_apply_reconcile_snapshot()`, after entry legs and binding maps exist and after manual-terminal leg normalization, invoke the helper before `if snapshot.errors:`. This guarantees terminal recovery is independent of unrelated snapshot availability.

**Step 3: Run GREEN and adjacent intent tests**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py::test_reconcile_resolves_exact_terminal_stale_wait_intent \
  tests/test_execution_bindings.py::test_reconcile_terminal_stale_wait_predicate_is_exact \
  tests/test_execution_bindings.py::test_reconcile_never_legacy_adopts_a_saved_terminal_or_inflight_intent
```

Expected: PASS.

**Step 4: Commit the minimal GREEN**

```bash
git add src/telegram_kol_research/execution_bindings.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: terminalize exact stale trigger waits"
```

### Task 3: Prove unrelated-instrument error fanout RED

**Files:**
- Modify: `tests/test_execution_bindings.py`

**Step 1: Add an instrument-isolation client fixture**

Seed one active BTC saved intent. Make BTC pending/history reads succeed and only ETH trigger history fail. Preserve a second ETH binding so both instruments are included in the coherent snapshot.

**Step 2: Add exact RED assertions**

Assert the BTC intent keeps its original state, retry count, next attempt, disposition, reason, evidence, and `updated_at`; no `protection_adoption_refused` audit is created for the BTC leg; `result.protection_snapshot_unavailable == 0` for the unrelated BTC exposure.

Add two GREEN-preserving controls:

- `trigger_history:BTC-USDT-SWAP` still schedules `wait / snapshot_incomplete`;
- generic `trigger_history` still schedules `wait / snapshot_incomplete`.

**Step 3: Run RED**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py::test_reconcile_unrelated_instrument_history_error_does_not_rewrite_intent \
  tests/test_execution_bindings.py::test_reconcile_target_or_generic_history_error_still_waits
```

Expected: unrelated-instrument case fails because the BTC intent is rewritten with the ETH source; relevant controls pass.

**Step 4: Commit the witnessed RED tests**

```bash
git add tests/test_execution_bindings.py
git diff --cached --name-only
git diff --cached --check
git commit -m "test: reproduce trigger snapshot error fanout"
```

### Task 4: Implement instrument-scoped protection errors GREEN

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Add pure source-selection helpers**

Implement:

```python
_TRIGGER_PROTECTION_SNAPSHOT_SOURCES = frozenset(
    {"pending_trigger_orders", "trigger_history"}
)

def _trigger_protection_snapshot_errors_for_instrument(
    errors: dict[str, str], *, instrument_id: str | None
) -> list[str]:
    result = []
    normalized_instrument = str(instrument_id or "").strip().upper()
    for key in sorted(errors):
        source, separator, scoped_instrument = key.partition(":")
        if source not in _TRIGGER_PROTECTION_SNAPSHOT_SOURCES:
            continue
        if not separator or (
            normalized_instrument
            and scoped_instrument.strip().upper() == normalized_instrument
        ):
            result.append(key)
    return result
```

Reuse the same canonical leg-instrument helper from Task 2.

**Step 2: Scope retry evidence per leg**

In `_retry_saved_trigger_protection_intents_for_unavailable_snapshot()`, compute relevant sources for each leg. Skip the intent when the list is empty. Preserve current retry-budget and reason semantics for relevant sources.

Scope `_trigger_protection_exposure_count()` to the same relevant-error rule so the unavailable metric no longer counts unaffected instruments. Generic errors remain account-wide.

**Step 3: Run GREEN and outage regression tests**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_execution_bindings.py::test_reconcile_unrelated_instrument_history_error_does_not_rewrite_intent \
  tests/test_execution_bindings.py::test_reconcile_target_or_generic_history_error_still_waits \
  tests/test_execution_bindings.py::test_reconcile_saved_intent_records_unavailable_snapshot_and_retries \
  tests/test_execution_bindings.py::test_reconcile_saved_intent_outage_does_not_exhaust_retry_budget \
  tests/test_execution_bindings.py::test_reconcile_protection_adoption_counts_unavailable_pending_snapshot
```

Expected: PASS.

**Step 4: Commit the GREEN**

```bash
git add src/telegram_kol_research/execution_bindings.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: scope trigger snapshot errors by instrument"
```

### Task 5: Freeze and verify the final local candidate

**Files:**
- Verify: `src/telegram_kol_research/execution_bindings.py`
- Verify: `tests/test_execution_bindings.py`

**Step 1: Run the complete affected file**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q tests/test_execution_bindings.py
```

Expected: PASS.

**Step 2: Run adjacent compatibility tests**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_trigger_protection_assignment.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_position_management_liveness_recovery.py
```

Expected: PASS.

**Step 3: Run static checks**

```bash
git diff --check
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m compileall -q src/telegram_kol_research tests
```

Expected: both exit zero.

**Step 4: Run the final complete suite exactly once**

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q
```

Expected: all tests pass. Any later production-code change invalidates this run.

### Task 6: Rehearse the exact three-row change on a fresh production copy

**Files:**
- Read: `/opt/telegram-kol-analyzer/data/research.db`
- Create privately: `/Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-<UTC>/research-online-backup.db`
- Create privately: `/Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-<UTC>/rehearsal-copy.db`
- Create privately: `/Users/steven/.codex/evidence/trigger-protection-stale-wait-rehearsal-<UTC>/rehearsal-summary.json`

**Step 1: Pass read-only server gates**

Verify exact deployed SHA, split-service state, disk, zero unsafe management, zero claimed/executing worker commands, zero claimed message jobs after at most one reasoned retry, and source `PRAGMA quick_check`. Do not stop or restart services.

**Step 2: Create and verify a fresh online backup**

Use Python `sqlite3.Connection.backup()` against the production source, preserve mode `0600`, record SHA-256, and copy the immutable backup into the local private evidence directory. Verify `quick_check=ok` and foreign-key count zero.

**Step 3: Freeze the exact before state**

Record all columns for intents `138`, `141`, and `147`, critical table counts, and a deterministic logical digest. Query the exact predicate and require the matching set to equal `{138, 141, 147}`.

**Step 4: Apply only the shared helper to the rehearsal copy**

Open the rehearsal copy with the local candidate session factory, load the relevant legs/bindings, and call `_resolve_exact_terminal_stale_wait_trigger_protection_intents()` at a fixed UTC timestamp. Commit only the copy transaction.

Require exactly three transitions and an exact after state. Compare every other row and critical count to the before snapshot.

**Step 5: Prove idempotence and restore**

Reinvoke the helper and require zero transitions and no digest change. Restore the exact three before rows on the copy, then require the complete logical digest, `quick_check`, foreign-key count, and target rows to equal the starting state.

**Step 6: Preserve evidence and stop**

Write a concise manifest with candidate SHA, backup SHA, predicate set, apply/idempotence/restore counts, integrity checks, and artifact paths. Do not create a production apply plan.

### Task 7: Record the local candidate and rehearsal result

**Files:**
- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Update status**

Record exact RED failures, GREEN focused results, affected compatibility results, final complete-suite result, candidate SHA, copy-backup SHA, exact three-row rehearsal result, evidence path, and the unchanged authorization boundary.

Set `workstream_status: local_complete`, retain the current claim, and set the next task to independent review. Keep production apply, push, deploy, restart, cutover, Telegram traffic, and exchange writes unauthorized.

**Step 2: Commit documentation only**

```bash
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record stale trigger intent candidate"
```

No production-code change is allowed after the final complete suite.
