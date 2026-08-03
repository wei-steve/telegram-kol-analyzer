# Production Monitor History Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop terminal management history from causing permanent production-monitor failures, make live SQLite snapshot retries reliable, and safely converge the seven exact historical unresolved batches without replaying exchange writes.

**Architecture:** Keep the existing all-history audit, but classify completed fail-closed batches separately from actionable outcomes. Add one bounded retry family for transient SQLite component changes. Add an operator-only, exact-ID dry-run/apply recovery workflow that reuses the read-only Deepcoin reconciliation snapshot, requires an unchanged evidence fingerprint, and records a terminal audit event without invoking submission APIs.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy, SQLite WAL, pytest, existing Deepcoin read-only reconciliation snapshot and production safety monitor.

---

### Task 1: Classify terminal blocked history without hiding actionable legs

**Files:**
- Modify: `src/telegram_kol_research/cli.py:245-260,960-1135`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:74-80,1268-1371`
- Test: `tests/test_cli_smoke.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1: Write the failing CLI audit tests**

Add fixtures covering:

```python
def test_management_audit_counts_completed_fail_closed_batch_as_terminal_history(...):
    # blocked + completed_at + only planned/failed/no legs
    assert payload["counts"]["terminal_blocked"] == 1
    assert payload["counts"]["blocked"] == 0


@pytest.mark.parametrize(
    "leg_status",
    ["reserved", "submitted", "submit_unknown", "partial", "inconsistent", "partial_failed", "recovery_required"],
)
def test_management_audit_keeps_blocked_batch_with_actionable_leg_alerting(...):
    assert payload["counts"]["blocked"] == 1
    assert payload["counts"]["terminal_blocked"] == 0


def test_management_audit_keeps_uncompleted_blocked_batch_alerting(...):
    assert payload["counts"]["blocked"] == 1
```

Also assert that malformed evidence is never classified terminal.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_cli_smoke.py -k "terminal_blocked or actionable_leg or uncompleted_blocked" -q
```

Expected: failures because `terminal_blocked` is absent and all blocked rows are actionable.

**Step 3: Implement the minimal audit classification**

Extend the required batch schema with `completed_at`. Add an SQL predicate equivalent to:

```sql
b.status = 'blocked'
AND b.completed_at IS NOT NULL
AND NOT EXISTS (
  SELECT 1 FROM strategy_management_legs terminal_leg
  WHERE terminal_leg.management_batch_id = b.id
    AND terminal_leg.status IN (
      'reserved', 'submitted', 'submit_unknown', 'partial', 'inconsistent',
      'partial_failed', 'recovery_required'
    )
)
```

Keep the existing informational-noop exclusion. Set `counts.blocked` to the actionable blocked count and add `counts.terminal_blocked`. Do not alter or delete any stored row.

**Step 4: Write and run monitor-policy tests**

Add tests proving:

```python
def test_terminal_blocked_history_does_not_make_monitor_unhealthy(): ...
def test_actionable_blocked_still_sets_audit_abnormal(): ...
def test_partial_failed_recovery_required_and_submit_unknown_remain_abnormal(): ...
```

Run:

```bash
uv run pytest tests/test_production_safety_monitor.py tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py src/telegram_kol_research/production_safety_monitor.py tests/test_cli_smoke.py tests/test_production_safety_monitor.py
git commit -m "fix: classify terminal management audit history"
```

### Task 2: Retry transient live-WAL snapshot changes exactly once

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:350-380,470-490`
- Test: `tests/test_production_safety_monitor.py:1860-2000`

**Step 1: Write failing retry tests**

Parameterize the transient reasons:

```python
@pytest.mark.parametrize(
    "reason",
    [
        "source_snapshots_differ",
        "source_component_changed_during_read",
        "source_component_set_changed",
    ],
)
def test_transient_private_snapshot_reason_retries_exactly_once(reason): ...
```

Prove a second transient failure remains incomplete and that
`rollback_journal_present`, malformed JSON, and other non-transient failures do
not retry.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "transient_private_snapshot" -q
```

Expected: the two new reasons do not retry.

**Step 3: Implement one shared transient-reason allowlist**

Introduce:

```python
_TRANSIENT_AUDIT_SNAPSHOT_REASONS = frozenset({
    "source_snapshots_differ",
    "source_component_changed_during_read",
    "source_component_set_changed",
})
```

Use it both when adapting a nonzero child result and when deciding whether
`run_daily_management_audit` invokes `run_once` a second time. Keep the maximum
at two total attempts.

**Step 4: Run focused tests**

```bash
uv run pytest tests/test_production_safety_monitor.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "fix: retry transient management audit snapshots"
```

### Task 3: Add exact, read-only history recovery decisions

**Files:**
- Create: `src/telegram_kol_research/management_history_recovery.py`
- Test: `tests/test_management_history_recovery.py`

**Step 1: Write failing decision tests**

Define a pure decision API:

```python
decision = plan_management_history_recovery(
    session_factory,
    batch_id=22,
    snapshot=complete_snapshot,
    planned_at=now,
)
```

Cover:

- unknown batch and non-actionable batch refuse;
- incomplete positions/order-history/trade-fill/TPSL observations refuse;
- planned/no-reservation/no-intent/no-event/no-exchange-order produces
  `terminal_no_submission`;
- submitted close requires exact binding, leg, `posId`, order ID or client order
  ID, and exchange terminal result;
- exact order success plus exact position absence produces
  `terminal_exchange_confirmed`;
- position absence without exact order evidence refuses;
- restored protection or exact position absence can terminalize the historical
  `partial_failed` protection replacement;
- every decision contains a canonical SHA-256 evidence fingerprint and bounded,
  redacted evidence only.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_management_history_recovery.py -q
```

Expected: import failure because the module does not exist.

**Step 3: Implement the pure planner**

Add immutable dataclasses for the decision and refusal. Reuse
`_load_reconcile_snapshot` normalization helpers where possible, but keep the
planner free of Deepcoin submission calls. Canonicalize only exact durable IDs,
terminal exchange states, completeness flags, and hashes; never retain raw API
errors or credentials.

**Step 4: Run focused tests**

```bash
uv run pytest tests/test_management_history_recovery.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/management_history_recovery.py tests/test_management_history_recovery.py
git commit -m "feat: plan exact management history recovery"
```

### Task 4: Add fingerprint-guarded, idempotent convergence

**Files:**
- Modify: `src/telegram_kol_research/management_history_recovery.py`
- Test: `tests/test_management_history_recovery.py`

**Step 1: Write failing apply tests**

Add tests proving:

```python
result = apply_management_history_recovery(
    session_factory,
    decision=decision,
    expected_fingerprint=decision.evidence_fingerprint,
    applied_at=now,
)
```

- a changed fingerprint or changed source row aborts before write;
- only `recovery_required`/`partial_failed` source states can transition;
- terminal no-submission becomes `resolved` with a bounded reason;
- terminal exchange-confirmed becomes `succeeded` only when the existing
  lifecycle semantics support success, otherwise `resolved`;
- one `ExecutionEvent(action="management_history_recovery", status="resolved")`
  records before/after status and the evidence fingerprint;
- a second identical apply is idempotent and creates no second event;
- injected fake Deepcoin clients observe zero mutation-method calls.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_management_history_recovery.py -k "apply or idempotent or zero_mutation" -q
```

Expected: failures because apply is absent.

**Step 3: Implement one transactional compare-and-set**

Reload the batch and dependent evidence, recompute the durable portion of the
fingerprint, verify the expected source status/reason/update timestamp, update
batch and leg terminal fields, insert the execution event, and commit once.
Never accept symbol/side as identity and never invoke the exchange client.

**Step 4: Run tests**

```bash
uv run pytest tests/test_management_history_recovery.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_batches.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/management_history_recovery.py tests/test_management_history_recovery.py
git commit -m "feat: converge proven management history"
```

### Task 5: Expose a safe operator CLI

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write failing CLI tests**

Add `recover-management-history` tests for:

- exactly one required `--batch-id`;
- default dry-run JSON output;
- `--apply` requires `--evidence-fingerprint`;
- incomplete exchange snapshot exits nonzero without a database write;
- fingerprint mismatch exits nonzero;
- apply returns a bounded result and performs no notification or order call.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_cli_smoke.py -k "recover_management_history" -q
```

Expected: command is unknown.

**Step 3: Implement the command**

Build the existing Deepcoin client from environment, load one complete read-only
reconciliation snapshot, call the planner, and optionally call apply. Emit only
redacted JSON. Require one batch per invocation so production evidence and
operator intent remain unambiguous.

**Step 4: Run focused tests**

```bash
uv run pytest tests/test_cli_smoke.py tests/test_management_history_recovery.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_smoke.py
git commit -m "feat: add management history recovery command"
```

### Task 6: Regression review and rollout documentation

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`
- Verify: all changed source and tests

**Step 1: Document operator procedure and invariants**

Document dry-run, fingerprint confirmation, one-batch apply, rerun audit, and the
rule that incomplete evidence stays unresolved. Document rollback and that no
history is deleted.

**Step 2: Run local verification**

```bash
uv run pytest tests/test_management_history_recovery.py tests/test_production_safety_monitor.py tests/test_cli_smoke.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_batches.py -q
uv run pytest -q
git diff --check
```

Expected: focused tests pass; full-suite result is recorded with any pre-existing
unrelated failure separated.

**Step 3: Review the complete change**

Use the project code-review workflow. Fix every critical or important finding
with a new failing test before code changes, rerun the focused suite, and record
the reviewed commit range.

**Step 4: Commit documentation**

```bash
git add docs/runbook.md docs/migration-handoff.md
git commit -m "docs: add management history recovery runbook"
```

### Task 7: Deploy without changing live trading settings

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: production settings and database through read-only checks

**Step 1: Confirm repository scope and push**

Preserve the existing user-owned `uv.lock` and unrelated untracked files. Push
only reviewed commits to `codex/deepcoin-auto-trading-v1`.

**Step 2: Prove a fresh safe window**

Require zero `execution_running`, zero in-flight management batches, zero
active/unknown position mutations, zero active rescues, and no recent Telegram
or execution event. Abort deployment if the snapshot changes.

**Step 3: Deploy normally**

Use `scripts/server_git_update.ps1`. Verify server SHA, service health, HTTP 200,
focused server tests, and that every trading setting—including effective
`trigger_protection_stop_rescue_mode=live`—matches the pre-deployment snapshot.

**Step 4: Verify monitor classification before historical writes**

Run `audit-management-batches` and the safety monitor. Confirm the 32 terminal
blocked rows remain visible but no longer contribute to actionable `blocked`.
Do not expect green until the seven unresolved rows are handled.

### Task 8: Dry-run and converge the seven production rows

**Files:**
- Modify only production database rows through the reviewed CLI
- Record evidence in `docs/migration-handoff.md` after results are known

**Step 1: Dry-run each exact batch**

Run the command individually for production batch IDs `17`, `22`, `23`, `28`,
`38`, `40`, and `86`. Record only decision, refusal code, fingerprint, and
bounded exact-ID evidence. Verify Deepcoin write count remains zero.

**Step 2: Apply only proven terminal decisions**

Re-prove the safe window before each apply batch. Pass the exact dry-run
fingerprint. If evidence changes or is incomplete, leave the row unchanged.

**Step 3: Post-apply verification**

Confirm no exchange submission event was created, no live position/protection
changed, and the service and stop-rescue live mode remain healthy. Rerun the
audit and safety monitor.

**Step 4: Record final evidence**

Document resolved IDs, unchanged IDs and refusal reasons, final actionable
audit counts, monitor result, deployed SHA, and rollback facts. Commit and push
the documentation-only evidence without another production restart.
