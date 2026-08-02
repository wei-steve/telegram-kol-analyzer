# Trigger Protection Live Evidence Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Require current live-position and post-fill timing evidence before trigger-protection adoption, and make protection-order ownership immutable across every ledger writer.

**Architecture:** Introduce one immutable live-position evidence value object shared by normal reconciliation and supervised repair. Feed it into the existing pure trigger-protection planner, then harden the shared protection-ledger upsert and trigger finalizer so stale or competing owners cannot overwrite an established `ordId` mapping.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, pytest, Typer, Deepcoin REST client, systemd.

---

## Safety rules

- Use `@test-driven-development` for every code change.
- Use `@systematic-debugging` for unexpected failures.
- Use `@requesting-code-review` after the focused and full suites pass.
- Do not call any Deepcoin write method from tests, planning or reconciliation.
- Preserve unrelated user changes, especially `uv.lock`, inspection directories, artifacts and untracked documents.
- Do not deploy or restart during an active time-sensitive strategy operation.
- Do not combine this work with stop-price corrections or strategy-recognition changes.

### Task 1: Define and validate exact live-position evidence

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write failing value-object validation tests**

Add tests for a new immutable `TriggerProtectionLivePosition` and pure builder/validator:

```python
def test_trigger_protection_live_position_requires_exact_identity_and_size():
    result = build_trigger_protection_live_position(
        entry_leg=entry_leg,
        position_rows=[live_position(size="0.6", c_time="1785609910000")],
        observed_at=NOW,
    )

    assert result.position is not None
    assert result.position.pos_id == "pos-1"
    assert result.position.size_text == "0.6"
    assert result.position.created_at == datetime.fromtimestamp(
        1785609910, UTC
    ).replace(tzinfo=None)
```

Add separate refusal cases for no matching position, duplicate `posId`, instrument mismatch, side mismatch, current size mismatch and missing/unparseable `cTime`.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  -k "live_position" -q
```

Expected: FAIL because the value object and builder do not exist.

**Step 3: Implement the minimal value object and pure builder**

Add:

```python
@dataclass(frozen=True, slots=True)
class TriggerProtectionLivePosition:
    pos_id: str
    instrument_id: str
    side: str
    size_text: str
    created_at: datetime
    observed_at: datetime
```

Return either one validated value or one bounded `EntryProtectionLedgerRepairRefusal`. Reuse existing numeric comparison and datetime parsing helpers. Require a nonzero position and exact requested size.

**Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_entry_protection_ledger_repair.py
git commit -m "fix: require live position evidence for protection adoption"
```

### Task 2: Enforce post-fill candidate timing in the pure planner

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`

**Step 1: Write failing candidate-time tests**

Add four tests around `plan_trigger_protection_intent_adoption()`:

```python
def test_pending_trigger_protection_candidate_before_position_is_refused(...):
    result = plan_trigger_protection_intent_adoption(
        ...,
        live_position=live_position(created_at=datetime(2026, 8, 2, 4, 18)),
        pending_tpsl_rows=[anonymous_stop(c_time="1785609800000")],
    )
    assert result.refusal.reason == "trigger_protection_candidate_predates_fill"
```

Also cover history-before-position, missing candidate `cTime`, and candidate time equal to position time.

**Step 2: Run the tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  -k "candidate_before_position or candidate_time_unavailable or candidate_at_position_time" -q
```

Expected: the early/missing-time cases are incorrectly accepted or deferred.

**Step 3: Add the live-position parameter and common time gate**

Require `live_position: TriggerProtectionLivePosition` in the planner. Before adding either pending or history rows to `candidates`, parse creation time from creation-only fields and apply:

```python
if candidate_created_at is None:
    return refusal("trigger_protection_candidate_time_unavailable")
if candidate_created_at < live_position.created_at:
    return refusal("trigger_protection_candidate_predates_fill")
```

Do not use `uTime` as the sole creation proof. Retain the existing history parent-reference and upper-bound checks.

**Step 4: Update all planner fixtures with explicit live evidence**

Update tests to pass a position whose identity and size match the target. Do not add a permissive default parameter; missing live evidence must fail closed at the caller boundary.

**Step 5: Run the full planner file**

```bash
.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_entry_protection_ledger_repair.py
git commit -m "fix: reject pre-fill trigger protection candidates"
```

### Task 3: Gate normal reconciliation on the current positions snapshot

**Files:**

- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write failing reconciliation tests**

Add production-shaped tests proving:

- an `active + verified` local leg whose `posId` is absent from `snapshot.positions` does not adopt a visible stale TPSL;
- a live position whose current size differs from the entry request does not adopt;
- an exact live position with a post-fill unique child still adopts.

Assert no logical-leg order ID, ledger row, adopted intent or protection revision is written in refusal cases.

**Step 2: Run tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  -k "trigger_protection_position_not_live or trigger_protection_position_size_changed" -q
```

Expected: stale or size-drifted positions currently adopt.

**Step 3: Build live evidence from the existing bounded snapshot**

In `_reconcile_saved_trigger_protection_intents()`, build the target live-position evidence from `snapshot.positions` before invoking the planner. Pass the same evidence to the planner. On refusal, use the existing bounded audit path and do not call the finalizer.

Do not change the historical choice to retain a closed leg's verified attribution; change only its eligibility to acquire new protection identity.

**Step 4: Verify unavailable snapshot behavior**

Run existing snapshot-unavailable tests and assert the durable intent stays retryable without consuming evidence attempts.

**Step 5: Run adjacent tests**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_trigger_protection_intents.py \
  tests/test_position_protection_legs.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_execution_bindings.py
git commit -m "fix: gate protection adoption on live positions"
```

### Task 4: Make supervised repair use the same fresh position evidence

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing repair-plan tests**

Add tests proving `include_trigger_entries=True`:

- calls `list_positions()` once and performs no exchange write;
- returns `trigger_protection_position_not_live` when the target is absent;
- returns `trigger_protection_position_size_changed` when size differs;
- places bounded position identity, size, creation time and observation time in action evidence.

**Step 2: Write the stale-plan test**

Build a repairable plan, change the fake live position size, rebuild, and assert the fingerprint changes or the rebuilt plan contains no action. Applying the old expected fingerprint must fail before any database mutation.

**Step 3: Run tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py \
  -k "repair_entry_protection and live_position" -q
```

Expected: current planning does not read positions, so stale evidence remains repairable.

**Step 4: Read one bounded positions snapshot in the repair planner**

When trigger entries are included, read positions once, validate response shape, and reuse it for all target intents. Pass exact evidence into the existing pure planner. Add only bounded normalized fields to action/refusal evidence so `_plan_fingerprint()` captures every safety-relevant change.

**Step 5: Preserve dry-run and apply boundaries**

Keep dry-run as the default. Keep the existing exact binding, `posId`, action ID, expected fingerprint and single-use token requirements. Do not add any exchange write method.

**Step 6: Run focused tests**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py \
  tests/test_repair_confirmation.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_cli_smoke.py
git commit -m "fix: bind repair approval to live position evidence"
```

### Task 5: Enforce immutable protection-ledger ownership

**Files:**

- Modify: `src/telegram_kol_research/protection_ledger.py`
- Modify: `tests/test_protection_ledger.py`

**Step 1: Write failing owner-conflict tests**

Create one ledger row, then call `upsert_protection_ledger_row()` with the same `venue + order_id` and a different binding, leg, strategy, `posId`, instrument or side. Parameterize each owner field and assert:

```python
with pytest.raises(ValueError, match="protection_ledger_owner_conflict"):
    upsert_protection_ledger_row(...)

session.rollback()
assert persisted_owner == original_owner
```

Add a same-owner idempotency test proving mutable evidence can still refresh.

**Step 2: Run tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_ledger.py \
  -k "owner_conflict or same_owner" -q
```

Expected: owner-conflict cases currently overwrite the row.

**Step 3: Add immutable-owner validation before assignments**

When a row already exists, normalize and compare all owner fields before changing any column. Raise `ValueError("protection_ledger_owner_conflict")` on mismatch. Keep trigger price, size, status, evidence source/evidence and observation timestamps refreshable for the same owner.

**Step 4: Audit every existing caller**

Review all calls returned by:

```bash
rg -n "upsert_protection_ledger_row\\(" src/telegram_kol_research
```

Confirm each call either preserves the original owner or should explicitly fail. Do not add compatibility fallbacks that reassign ownership.

**Step 5: Run all ledger writers' tests**

```bash
.venv/bin/python -m pytest \
  tests/test_protection_ledger.py \
  tests/test_backup_stop_repair.py \
  tests/test_current_protection_backfill.py \
  tests/test_strategy_management_executor.py \
  tests/test_trigger_backup_stop.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_break_even_convergence_executor.py -q
```

Expected: PASS. If a legitimate caller changes an owner field, investigate its data model instead of weakening the invariant.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/protection_ledger.py \
  tests/test_protection_ledger.py
git commit -m "fix: make protection ledger ownership immutable"
```

### Task 6: Close finalizer race and logical-leg conflicts

**Files:**

- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `tests/test_execution_bindings.py`

**Step 1: Write the plan-after-owner-race test**

Generate an adoption action, then insert a ledger owner for the same `ordId` but another position before calling `finalize_trigger_protection_adoption()`. Assert the finalizer raises and, after rollback:

- the old ledger owner is unchanged;
- primary logical leg has no new exchange order ID;
- intent is not adopted;
- no new protection revision exists.

**Step 2: Write the duplicate logical-leg test**

Bind the candidate `ordId` to another `PositionProtectionLeg` and assert the target finalizer refuses before writing.

**Step 3: Run tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  -k "owner_race or logical_leg_order_conflict" -q
```

Expected: the existing finalizer can overwrite the ledger owner or allow duplicate logical-leg identity.

**Step 4: Add finalizer preconditions in its existing transaction**

Before binding the primary leg, query the ledger and all logical protection legs for the candidate `ordId`. Require them to be absent or exact-owner matches. Then use the hardened shared upsert. Do not commit inside the finalizer.

The existing unique `position_protection_ledger(venue, order_id)` index remains the final concurrent-insert guard. Convert resulting uniqueness/owner failures into the explicit conflict error and allow the caller transaction to roll back.

**Step 5: Run atomicity and idempotency tests**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_revisions.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add \
  src/telegram_kol_research/entry_protection_ledger_repair.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py
git commit -m "fix: finalize protection identity without owner races"
```

### Task 7: Verify the complete safety invariant

**Files:**

- Modify only files already in scope if verification exposes a defect.

**Step 1: Run syntax and whitespace checks**

```bash
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: both commands succeed without output.

**Step 2: Run the focused protection suite**

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_execution_bindings.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_ledger.py \
  tests/test_trigger_protection_intents.py \
  tests/test_protection_revisions.py \
  tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 3: Run the full local suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS, with only previously known warnings/skips.

**Step 4: Audit the final diff**

Confirm:

- every adoption requires a current exact nonzero position;
- every candidate has creation time at or after the position creation/fill time;
- repair fingerprint includes normalized live-position evidence;
- the shared ledger cannot change owner fields;
- finalizer conflicts roll back all four durable objects;
- no Deepcoin write call was added;
- no production-specific IDs or prices were hard-coded.

**Step 5: Request code review**

Use `@requesting-code-review`. Fix every Critical or Important finding before push.

### Task 8: Push, deploy and perform read-only verification

**Files:**

- No source changes expected.

**Step 1: Confirm scoped repository state**

```bash
git status --short --branch
git log --oneline -10
```

Preserve all unrelated dirty and untracked user files.

**Step 2: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 3: Prove a safe deployment window**

Use read-only server and exchange checks. Do not restart while any time-sensitive strategy write, active management batch, submitted mutation intent or unknown exchange outcome exists.

**Step 4: Deploy through the standard helper**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

**Step 5: Verify production read-only**

Confirm production SHA, active service state, worker startup, absence of new attribution errors and no restart loop. Do not submit, cancel or modify any order for verification.

**Step 6: Run the historical repair dry-run only**

Use exact binding and `posId` filters with `--include-trigger-entries` and without `--apply`. Confirm the output now includes current live-position evidence and produces either one exact repairable action or a bounded refusal.

**Step 7: Stop for explicit approval before supervised apply**

Do not generate or consume a confirmation token and do not execute the ledger repair without a separate explicit user instruction.

