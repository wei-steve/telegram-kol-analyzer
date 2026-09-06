# Trigger Protection Filled-Leg Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make a verified filled trigger-entry leg the authoritative owner of its anonymous attached stop, so an unfilled sibling leg with the same size and stop price cannot block the exact `posId ↔ ordId` ledger mapping.

**Architecture:** Extend the pure trigger-protection adoption planner with a minimal snapshot of every sibling entry leg's authoritative fill state. Bind a verified filled `posId` to its logical protection legs before child-order adoption, then atomically bind the child `ordId`, write the protection ledger and revision, and transition the durable intent. Reuse the same planner and atomic finalizer for supervised repair of historical failed intents.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, Typer, Deepcoin REST client, systemd.

---

## Safety and execution rules

- Use `@test-driven-development` for every implementation task.
- Use `@systematic-debugging` for any unexpected test failure.
- Use `@requesting-code-review` after the local suite passes and before pushing.
- Do not submit, cancel, replace, or modify any Deepcoin order from local tests.
- Keep all exchange interaction in the existing read-only reconciliation snapshot and existing account authority lock.
- Preserve unrelated user changes, especially `uv.lock`, inspection directories, artifacts, and untracked planning files.
- Do not hard-code the current Shuqin IDs or prices in production code.
- The supervised historical repair may write only local identity ledgers; it must never write to Deepcoin.
- Do not combine the `1695 → 1795` stop correction with this attribution repair.

### Task 1: Make the pure adoption planner understand filled-leg ownership

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:56-90`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:632-779`
- Modify: `tests/test_entry_protection_ledger_repair.py:440-850`

**Step 1: Write the failing Shuqin-shaped planner test**

Add a test with two trigger-entry legs whose entry prices differ but whose protection signature is identical:

```python
def test_intent_adoption_ignores_same_signature_unfilled_sibling(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"price": "1828", "sz": "0.6", "slTriggerPx": "1695"},
        pending_update={
            "posId": "",
            "sz": "0.6",
            "slTriggerPx": "1695",
            "cTime": "1785609910000",
        },
        sibling_request={"price": "1808", "sz": "0.6", "slTriggerPx": "1695"},
        sibling_owner_state={
            "status": "pending",
            "attribution_status": "unassigned",
            "pos_id": None,
        },
    )

    assert result.action is not None
    assert result.action.order_id == "tpsl-new"
    assert result.action.pos_id == "pos-1"
```

Keep the sibling's entry price different to prove that price is not being used as a child-order join key.

**Step 2: Write the true-conflict tests**

Add cases proving the same anonymous child is refused when:

```python
@pytest.mark.parametrize(
    "sibling_owner_state",
    [
        {"status": "active", "attribution_status": "verified", "pos_id": "pos-2"},
        {"status": "active", "attribution_status": "verified", "pos_id": "pos-1-conflict"},
    ],
)
def test_intent_adoption_refuses_same_signature_filled_sibling(...):
    ...
    assert result.refusal.reason == "trigger_protection_candidate_not_unique"
```

Also retain refusal for two matching candidate child orders, a candidate already in the pre-submit baseline, and an exchange-returned conflicting `posId`.

**Step 3: Run the focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  -k "unfilled_sibling or filled_sibling" -q
```

Expected: the unfilled-sibling case fails with `trigger_protection_candidate_not_unique` because the current planner treats every pending intent as a possible owner.

**Step 4: Add an immutable owner-state value object**

Add near the existing planner result dataclasses:

```python
@dataclass(frozen=True, slots=True)
class TriggerProtectionOwnerState:
    execution_order_leg_id: int
    status: str
    attribution_status: str
    pos_id: str | None
    parent_order_id: str | None

    @property
    def is_verified_filled_owner(self) -> bool:
        return (
            self.status.lower() == "active"
            and self.attribution_status.lower() == "verified"
            and bool(str(self.pos_id or "").strip())
            and bool(str(self.parent_order_id or "").strip())
        )
```

Extend `plan_trigger_protection_intent_adoption()` with:

```python
existing_intent_owner_states: dict[int, TriggerProtectionOwnerState] | None = None,
```

**Step 5: Replace the pending-intent collision rule**

Keep the existing anonymous protection signature comparison, but block only when the matching other intent has an authoritative filled owner:

```python
other_state = existing_intent_owner_states.get(int(other_intent.id or 0))
same_anonymous_request = (
    anonymous_stop_key is not None
    and existing_intent_requests is not None
    and _anonymous_stop_request_key(
        existing_intent_requests.get(int(other_intent.id or 0), {})
    ) == anonymous_stop_key
)
return same_anonymous_request and (
    other_state is None or other_state.is_verified_filled_owner
)
```

Missing owner-state evidence remains fail-closed. Explicitly verified `pending`, `submitted`, terminal, unassigned, or empty-`posId` sibling states do not block.

**Step 6: Run the focused and adjacent planner tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py -q
```

Expected: PASS, including all existing ownership-conflict tests.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_entry_protection_ledger_repair.py
git commit -m "fix: scope trigger protection conflicts to filled legs"
```

### Task 2: Bind exact filled positions to planned protection legs before adoption

**Files:**

- Modify: `src/telegram_kol_research/position_protection_legs.py:123-160`
- Modify: `src/telegram_kol_research/execution_bindings.py:960-1040`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_position_protection_legs.py`

**Step 1: Write the failing logical-leg binding test**

Add a unit test proving all planned protection roles for one entry leg receive the already-verified `posId` without receiving an exchange child order ID:

```python
def test_bind_verified_filled_position_to_all_planned_protection_legs(session):
    rows = bind_verified_filled_position_protection(
        session,
        execution_order_leg_id=422,
        pos_id="pos-1",
    )

    assert {row.role for row in rows} == {
        "primary_stop", "backup_stop", "take_profit"
    }
    assert {row.pos_id for row in rows} == {"pos-1"}
    assert all(row.exchange_order_id is None for row in rows)
    assert all(row.status == "waiting_fill" for row in rows)
```

Add a conflicting existing `posId` case that raises and rolls back.

**Step 2: Run the focused test and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_position_protection_legs.py \
  -k "verified_filled_position" -q
```

Expected: FAIL because the bulk binding helper does not exist.

**Step 3: Implement the bulk binder**

Add:

```python
def bind_verified_filled_position_protection(
    session: Session,
    *,
    execution_order_leg_id: int,
    pos_id: str,
) -> list[PositionProtectionLeg]:
    entry_leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    if entry_leg is None:
        raise ValueError("protection_leg_entry_missing")
    if (
        str(entry_leg.status or "").lower() != "active"
        or str(entry_leg.attribution_status or "").lower() != "verified"
        or str(entry_leg.pos_id or "") != str(pos_id)
    ):
        raise ValueError("protection_leg_entry_not_verified_filled")
    rows = ...
    for row in rows:
        bind_filled_position(session, row, pos_id=pos_id)
    return rows
```

Do not create missing logical legs in this helper; trigger submission must have persisted them before the exchange write.

**Step 4: Invoke the binder after entry attribution is current**

In `_adopt_verified_trigger_entry_protection()`, before reconciling saved intents, bind `posId` for every `active + verified + trigger_limit` entry leg that already has planned protection legs. Use the same session so an immutable conflict rolls back reconciliation.

Do not bind pending or unassigned siblings.

**Step 5: Write and run the reconciliation test**

Add a production-shaped test with one active verified leg and one pending sibling. Assert before child adoption:

```python
assert primary.pos_id == "pos-1"
assert primary.exchange_order_id is None
assert sibling_primary.pos_id is None
```

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_position_protection_legs.py \
  tests/test_execution_bindings.py \
  -k "planned_protection or filled_position" -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/position_protection_legs.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_position_protection_legs.py \
  tests/test_execution_bindings.py
git commit -m "fix: bind planned protection to verified fills"
```

### Task 3: Atomically finalize child-order identity and the authoritative ledger

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:121-150`
- Modify: `src/telegram_kol_research/execution_bindings.py:1193-1345`
- Modify: `src/telegram_kol_research/protection_revisions.py`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_protection_revisions.py`

**Step 1: Write the failing atomic-finalization test**

Create a Shuqin-shaped reconciliation fixture and assert one successful tick produces all four durable results:

```python
assert primary_leg.pos_id == "pos-1"
assert primary_leg.exchange_order_id == "stop-child-1"
assert primary_leg.status == "verified"
assert intent.recovery_state == "adopted"
assert intent.adopted_order_id == "stop-child-1"
assert ledger.pos_id == "pos-1"
assert ledger.order_id == "stop-child-1"
assert ledger.execution_order_leg_id == filled_leg.id
```

Assert the protection revision contains the same exact order identity.

**Step 2: Write rollback and idempotency tests**

- Pre-bind the logical primary stop to another `ordId`; expect the whole transaction to roll back without a ledger row or adopted intent.
- Run the same reconciliation twice; expect one ledger row, one immutable logical-leg mapping, and one active equivalent revision.
- Pre-own the candidate `ordId` from another position; expect a conflict and no writes for the target.

**Step 3: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_protection_revisions.py \
  -k "atomic_trigger_protection or trigger_protection_idempotent" -q
```

Expected: at least the revision assertion fails because current trigger adoption does not activate a protection revision.

**Step 4: Add one shared finalizer**

In `entry_protection_ledger_repair.py`, add a helper that accepts an already-planned action and performs no exchange I/O:

```python
def finalize_trigger_protection_adoption(
    session,
    *,
    action: EntryProtectionLedgerRepairAction,
    intent: TriggerProtectionIntent,
    seen_at: datetime,
) -> PositionProtectionLedger:
    row = upsert_entry_protection_ledger_action(
        session,
        action,
        evidence_source="reconciliation_trigger_protection_intent",
        seen_at=seen_at,
    )
    bind ... exact primary protection leg ...
    transition ... intent to adopted ...
    activate_protection_revision(... exact order identity ...)
    return row
```

The helper must verify the action binding, leg and `posId` agree with the intent and entry leg before writing. It must use the caller's transaction and must not call `commit()`.

**Step 5: Replace the split reconciliation writes**

Replace the current independent calls to ledger upsert, intent transition and `_bind_adopted_primary_protection_leg()` with the shared finalizer. Keep the local in-memory `existing_ledger_rows` update only after the finalizer succeeds.

**Step 6: Pass authoritative sibling owner states from reconciliation**

Build:

```python
intent_owner_states = {
    int(intent.id): TriggerProtectionOwnerState(
        execution_order_leg_id=int(leg.id),
        status=str(leg.status or ""),
        attribution_status=str(leg.attribution_status or ""),
        pos_id=str(leg.pos_id) if leg.pos_id else None,
        parent_order_id=str(leg.order_id) if leg.order_id else None,
    )
    for intent in saved_intents
    if (leg := legs_by_id.get(int(intent.execution_order_leg_id))) is not None
}
```

Pass it to the pure planner together with the immutable requests.

**Step 7: Run focused and adjacent tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_revisions.py \
  tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 8: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/protection_revisions.py \
  tests/test_execution_bindings.py \
  tests/test_protection_revisions.py
git commit -m "fix: finalize trigger protection identity atomically"
```

### Task 4: Preserve recoverability across unavailable snapshots and retries

**Files:**

- Modify: `src/telegram_kol_research/execution_bindings.py:1160-1191`
- Modify: `src/telegram_kol_research/execution_bindings.py:1350-1375`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_trigger_protection_intents.py`

**Step 1: Write the failing unavailable-snapshot test**

Create an exact filled entry leg with a saved intent and return a pending-TPSL read error for more than five reconciliation ticks. Assert:

```python
assert intent.recovery_state == "retrying"
assert intent.retry_attempts == 0
assert intent.adopted_order_id is None
```

The incident/audit may deduplicate, but exchange unavailability must not convert an otherwise valid durable identity intent into permanent `failed`.

**Step 2: Write the bounded evidence tests**

- `trigger_protection_not_yet_observable` consumes bounded attempts and eventually becomes `failed`.
- An immutable identity conflict is recorded and remains fail-closed.
- A later complete snapshot can adopt an intent that previously saw unavailable evidence.

**Step 3: Run the focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_trigger_protection_intents.py \
  -k "snapshot_unavailable or later_complete_snapshot" -q
```

Expected: the unavailable case fails because `_schedule_trigger_intent_retry()` currently increments attempts to `failed`.

**Step 4: Separate availability backoff from evidence attempts**

Add an explicit retry mode:

```python
def _schedule_trigger_intent_retry(
    session,
    intent,
    now,
    transition,
    *,
    consume_attempt: bool = True,
) -> None:
    attempts = int(intent.retry_attempts or 0)
    if consume_attempt:
        attempts = min(attempts + 1, _TRIGGER_PROTECTION_RETRY_LIMIT)
    ...
```

Use `consume_attempt=False` only for exchange snapshot unavailability. Keep bounded attempts for complete snapshots that cannot observe the child order or prove an immutable refusal.

**Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_trigger_protection_intents.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_execution_bindings.py \
  tests/test_trigger_protection_intents.py
git commit -m "fix: retain trigger protection intents through outages"
```

### Task 5: Add supervised repair for historical failed trigger intents

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:150-328`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py:430-620`
- Modify: `src/telegram_kol_research/cli.py:3466-3530`
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write the failing dry-run repair test**

Build a historical fixture with:

- one `failed` trigger protection intent;
- its entry leg `active + verified + posId`;
- one sibling leg still `pending + unassigned + no posId`;
- one post-baseline anonymous stop matching the filled leg's request;
- no existing protection ledger owner.

Assert the dry-run plan contains exactly one action with bounded evidence:

```python
assert action.evidence["match"] == "verified_filled_leg_unique_child"
assert action.pos_id == "pos-1"
assert action.order_id == "stop-child-1"
assert action.evidence["sibling_states"] == [
    {
        "execution_order_leg_id": sibling.id,
        "status": "pending",
        "attribution_status": "unassigned",
        "has_pos_id": False,
    }
]
```

Do not include raw payloads or sensitive response data in CLI output.

**Step 2: Write the approval and stale-snapshot tests**

- Apply without action ID, target `posId`, expected fingerprint or single-use confirmation token must refuse.
- A changed sibling state, position size, candidate order set or ledger owner must change the plan fingerprint and refuse apply.
- Apply must write the logical protection leg, ledger, revision and adopted intent in one transaction.
- Apply must never call a Deepcoin write method.

**Step 3: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py \
  -k "failed_trigger_intent or repair_entry_protection_ledger" -q
```

Expected: the failed-intent action is absent or apply refuses it because the current apply path accepts only `response_anchored_order` actions.

**Step 4: Reuse the production planner and atomic finalizer**

For `include_trigger_entries=True`, load saved intents, entry legs and sibling owner states and invoke the same `plan_trigger_protection_intent_adoption()` used by normal reconciliation. Do not create a second attribution policy in the repair path.

Permit apply only for `verified_filled_leg_unique_child` actions whose target intent is still `failed`, whose exchange and database snapshot regenerates the same fingerprint, and whose confirmation token is valid.

Call `finalize_trigger_protection_adoption()` with an evidence source distinguishing supervised repair. Do not reset the intent to `pending` first.

**Step 5: Keep the CLI safe by default**

The existing command remains dry-run unless `--apply` is present. Require:

```text
--include-trigger-entries
--binding-id
--pos-id
--action-id
--expected-fingerprint
--confirmation-token
```

Reject an apply that would affect more than one action or position.

**Step 6: Run focused and adjacent tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py \
  tests/test_repair_confirmation.py \
  tests/test_strategy_records.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py
git commit -m "feat: repair failed trigger protection identities"
```

### Task 6: Verify the full invariant locally

**Files:**

- Modify only if a test exposes a defect in an already-touched file.

**Step 1: Run formatting and static syntax checks**

Run:

```bash
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: both commands succeed with no output.

**Step 2: Run the focused protection suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_protection_intents.py \
  tests/test_protection_revisions.py \
  tests/test_trigger_backup_stop_executor.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_position_tpsl_display.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 3: Run the complete local suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. Tests requiring production credentials must remain skipped or mocked locally.

**Step 4: Audit the final diff**

Confirm:

- no exchange write was added to reconciliation or repair planning;
- an unfilled sibling is excluded only with explicit persisted leg-state evidence;
- missing sibling evidence remains fail-closed;
- a filled sibling still blocks anonymous child adoption;
- logical-leg binding, ledger upsert, intent transition and revision activation share one transaction;
- no Shuqin ID, price or message number appears in production code;
- `1695 → 1795` is not part of the attribution diff.

**Step 5: Request code review**

Use `@requesting-code-review` and address all correctness or safety findings before pushing.

**Step 6: Commit any review-only corrections**

```bash
git add <only-reviewed-files>
git commit -m "fix: harden filled-leg protection attribution"
```

Skip this commit if review requires no changes.

### Task 7: Push, deploy, and perform read-only production verification

**Files:**

- No source changes expected.

**Step 1: Confirm branch and clean scoped diff**

Run:

```bash
git status --short --branch
git log --oneline -8
```

Expected: branch is `codex/deepcoin-auto-trading-v1`; unrelated user changes remain untouched.

**Step 2: Push reviewed commits**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds.

**Step 3: Prove a safe deployment window**

Before restarting, use server read-only checks to confirm there is no active time-sensitive strategy write, unknown exchange outcome, or in-flight management batch. If a safe window cannot be proven, stop after the push and record the exact pending production verification.

**Step 4: Deploy through the standard helper**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the branch, reinstalls the editable package, restarts `telegram-kol.service`, and reports it active.

**Step 5: Verify the deployed revision and service**

Read-only checks must confirm:

- production SHA equals the pushed SHA;
- `telegram-kol.service` is active;
- listener, recognition and reconciliation workers are running;
- no new protection-attribution exceptions or repeated restart loop appears.

**Step 6: Generate the Shuqin repair dry-run**

Run the existing repair command with `--include-trigger-entries`, exact binding/position filters, and without `--apply`. Expected output must show one repairable action only if the current exchange and database still prove:

```text
filled entry leg 422
pending sibling leg 423 without posId
posId 1001124534587219
candidate stop ordId 1001124534587218
stop 1695
size 0.6
```

If any identity, order, position, price, size, sibling state or snapshot completeness differs, do not apply.

**Step 7: Stop for explicit approval before the local ledger write**

Present the dry-run fingerprint, bounded non-sensitive evidence and expected Web change. Do not generate or consume a confirmation token and do not run `--apply` without a separate explicit user instruction.

**Step 8: Verify rollback readiness**

Record the previous production SHA. Code rollback uses the normal Git deployment path. Never delete an already-verified identity ledger row as part of code rollback; any later exchange conflict requires an explicit incident repair.
