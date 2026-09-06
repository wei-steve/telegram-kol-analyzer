# Entry Assembly Fingerprint Synchronization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make recovery-trigger entries persist the finalized assembly fingerprint before exchange submission and reconcile the one historical mismatch with exact append-only evidence.

**Architecture:** Keep assembly finalization authoritative, then compare-and-set both copies of assembly evidence in the still-pending trade signal before calling the live submitter. Preserve submitted rows and add a fingerprint-gated append-only repair event that the read-only production monitor validates mechanically.

**Tech Stack:** Python 3.11+, SQLAlchemy, SQLite, Typer, pytest, existing Telegram KOL execution/monitor modules.

---

Implementation must follow @test-driven-development and @bug-hunt. Complete
local implementation and review with @requesting-code-review before pushing.
Production verification remains server-only because credentials, Telegram
identity, and the Deepcoin allowlist are not available locally.

### Task 1: Return Complete Finalization Evidence

**Files:**
- Modify: `src/telegram_kol_research/entry_strategy_assembly.py:37-70,786-873`
- Modify: `tests/test_entry_strategy_assembly.py:772-815`

**Step 1: Write the failing result-contract test**

Extend `test_finalize_adjacent_entry_assembly_draft_is_idempotent_and_conflict_safe`
to assert the finalizer returns a frozen value object rather than only a string:

```python
assert first.assembly_id == assembly.assembly_id
assert first.strategy_instance_id == draft["strategy_instance_id"]
assert first.original_fingerprint == assembly.assembly_fingerprint
assert first.final_fingerprint != first.original_fingerprint
assert first.evidence["order_draft_snapshot"]["order_legs"][0]["price"] == 64200
assert repeated == first
```

Also assert the persisted `EntryStrategyAssembly.fingerprint` and canonical
`evidence_json` equal the returned final result.

**Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_strategy_assembly.py::test_finalize_adjacent_entry_assembly_draft_is_idempotent_and_conflict_safe -q
```

Expected: FAIL because the current function returns `str`.

**Step 3: Add the minimal immutable result type**

Add near `EntryAssemblyResult`:

```python
@dataclass(frozen=True, slots=True)
class FinalizedEntryAssemblyDraft:
    assembly_id: int
    strategy_instance_id: str
    original_fingerprint: str
    final_fingerprint: str
    evidence: dict[str, object]
```

Refactor `finalize_adjacent_entry_assembly_draft()` to return this type on the
new-write, already-finalized, and compare-and-set-race paths. Preserve the
current canonical serialization exactly:

```python
evidence_json = json.dumps(
    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
)
fingerprint = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
```

For an already-finalized row, derive `original_fingerprint` by copying the
persisted evidence, removing only `order_draft_snapshot` and
`final_entry_leg_count`, canonicalizing it, and hashing it. Return a defensive
JSON-compatible copy of `evidence` so callers cannot mutate ORM state.

**Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_strategy_assembly.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/entry_strategy_assembly.py tests/test_entry_strategy_assembly.py
git commit -m "refactor: return finalized entry assembly evidence"
```

### Task 2: Compare-and-Set a Pending Trade Signal

**Files:**
- Modify: `src/telegram_kol_research/trade_signals.py:1-130`
- Create: `tests/test_trade_signal_fingerprint_sync.py`

**Step 1: Write failing happy-path and fail-closed tests**

Create a fixture that enqueues a recovery signal whose top-level and nested
draft evidence contain `old_fp`. Add tests for:

```python
updated = synchronize_pending_entry_assembly_evidence(
    session_factory,
    signal_id=signal.id,
    strategy_instance_id="strategy-1",
    expected_payload=signal.payload,
    expected_fingerprint=old_fp,
    finalized_evidence={"assembly_id": 2, "assembly_fingerprint": final_fp},
    synchronized_at=NOW,
)
assert updated.status == "pending"
assert updated.payload["entry_preamble_assembly"]["assembly_fingerprint"] == final_fp
assert updated.payload["deepcoin_order_draft"]["entry_preamble_assembly"]["assembly_fingerprint"] == final_fp
```

Parametrize failures for non-pending status, strategy mismatch, exact payload
drift, absent/malformed nested draft, and wrong old fingerprint. After every
failure, reload and assert no payload mutation occurred.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_signal_fingerprint_sync.py -q
```

Expected: collection/import FAIL because the helper does not exist.

**Step 3: Implement the strict synchronization helper**

Add:

```python
class TradeSignalFingerprintSyncError(RuntimeError):
    pass


def synchronize_pending_entry_assembly_evidence(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    strategy_instance_id: str,
    expected_payload: Mapping[str, Any],
    expected_fingerprint: str,
    finalized_evidence: Mapping[str, Any],
    synchronized_at: datetime,
) -> TradeSignalRecord:
    ...
```

Serialize `expected_payload` with the queue's existing
`ensure_ascii=False, sort_keys=True` convention. Validate both existing
evidence locations if present; require at least the finalizer-derived top-level
evidence and a mapping draft. Deep-copy through JSON serialization, write the
same final evidence into both locations, then execute:

```python
result = session.execute(
    update(TradeSignal)
    .where(
        TradeSignal.id == int(signal_id),
        TradeSignal.status == "pending",
        TradeSignal.strategy_instance_id == strategy_instance_id,
        TradeSignal.payload_json == expected_payload_json,
    )
    .values(payload_json=updated_payload_json, updated_at=synchronized_at)
)
if int(result.rowcount or 0) != 1:
    raise TradeSignalFingerprintSyncError("entry_assembly_signal_cas_failed")
```

Commit, reload in a new session, and validate pending status plus both final
fingerprints before returning. Use fixed error codes; never partially repair an
unexpected payload.

**Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trade_signal_fingerprint_sync.py tests/test_trade_signals.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trade_signals.py tests/test_trade_signal_fingerprint_sync.py
git commit -m "feat: synchronize pending entry assembly evidence"
```

### Task 3: Fix the Recovery-Trigger Submission Order

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py:20-60,760-890`
- Modify: `tests/test_auto_trade_execution.py:1040-1105`

**Step 1: Write a production-shaped regression test**

Add a test that forces the `auto_draft is None` recovery-trigger branch while
V2 assembly mode is live. Use a fake Deepcoin client that records a callback at
its first submission. In that callback, query durable state and assert:

```python
assert assembly.fingerprint == signal_payload["entry_preamble_assembly"]["assembly_fingerprint"]
assert assembly.fingerprint == signal_payload["deepcoin_order_draft"]["entry_preamble_assembly"]["assembly_fingerprint"]
```

After submission, assert the same fingerprint appears in the resulting
`ExecutionBinding.payload_json`. First reproduce and preserve the failure
against the current ordering.

Add a second test that monkeypatches the synchronization helper to raise
`TradeSignalFingerprintSyncError`; assert the fake Deepcoin client's request
list stays empty and the signal remains pending.

**Step 2: Run regression tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_auto_trade_execution.py -k "recovery_trigger and fingerprint" -q
```

Expected: the binding or durable signal contains the old fingerprint.

**Step 3: Wire finalization and synchronization before submit**

Import `synchronize_pending_entry_assembly_evidence`. Adapt the normal branch
to use `.final_fingerprint` from Task 1.

In the recovery-trigger branch:

```python
finalized = finalize_adjacent_entry_assembly_draft(
    session_factory,
    assembly_id=int(assembly.assembly_id),
    order_draft=recovery_draft,
)
final_evidence = {
    **assembly_evidence,
    "assembly_fingerprint": finalized.final_fingerprint,
}
trade_signal = synchronize_pending_entry_assembly_evidence(
    session_factory,
    signal_id=trade_signal.id,
    strategy_instance_id=finalized.strategy_instance_id,
    expected_payload=trade_signal.payload,
    expected_fingerprint=finalized.original_fingerprint,
    finalized_evidence=final_evidence,
    synchronized_at=now,
)
assembly_evidence = final_evidence
```

Only after the helper returns may the branch call
`process_trade_signal_live()`. Ensure the returned result exposes final rather
than stale evidence.

**Step 4: Run execution regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_auto_trade_execution.py tests/test_recovery_live_submit.py tests/test_trade_signal_fingerprint_sync.py -q
```

Expected: PASS and zero exchange calls in every synchronization-failure case.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py tests/test_auto_trade_execution.py
git commit -m "fix: finalize recovery entry evidence before submit"
```

### Task 4: Build the Append-Only Reconciliation Planner

**Files:**
- Create: `src/telegram_kol_research/entry_assembly_fingerprint_repair.py`
- Create: `tests/test_entry_assembly_fingerprint_repair.py`

**Step 1: Write failing read-only planner tests**

Create a production-shaped database fixture containing one finalized
`EntryStrategyAssembly`, stale `TradeSignal`, matching `ExecutionBinding`, and
bounded `ExecutionOrderLeg` rows. Compute the old fingerprint by removing only
the two finalization keys.

Test that `build_entry_assembly_fingerprint_repair_plan()`:

- returns exactly one action for exact IDs;
- returns a stable SHA-256 plan fingerprint;
- does not change database bytes;
- rejects wrong strategy, non-derivable old fingerprint, draft/leg identity
  mismatch, missing finalization fields, and conflicting prior events.

**Step 2: Run planner tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_assembly_fingerprint_repair.py -q
```

Expected: import FAIL because the module does not exist.

**Step 3: Implement bounded data contracts and canonical proof**

Create frozen dataclasses:

```python
@dataclass(frozen=True, slots=True)
class EntryAssemblyFingerprintRepairAction:
    assembly_id: int
    execution_binding_id: int
    trade_signal_id: int | None
    strategy_instance_id: str
    old_fingerprint: str
    final_fingerprint: str
    repair_fingerprint: str


@dataclass(frozen=True, slots=True)
class EntryAssemblyFingerprintRepairPlan:
    action: EntryAssemblyFingerprintRepairAction | None
    conflicts: tuple[str, ...]
    fingerprint: str
```

Expose shared pure helpers:

```python
RECONCILIATION_ACTION = "entry_assembly_fingerprint_reconciled"
RECONCILIATION_POLICY = "entry-assembly-fingerprint-reconciliation-v1"

def canonical_fingerprint(payload: Mapping[str, Any]) -> str: ...
def derive_pre_finalization_fingerprint(final_evidence: Mapping[str, Any]) -> str: ...
def build_reconciliation_fingerprint(...exact identity fields...) -> str: ...
```

The planner must query only the exact supplied IDs, parse bounded JSON, verify
full and derived hashes, verify strategy/source/symbol/side plus order-leg
identity, and return fixed conflict codes instead of guessing.

**Step 4: Write failing apply/idempotency tests**

Add tests that apply refuses a wrong expected plan fingerprint; exact apply
creates one `ExecutionEvent`; repeated exact apply returns the existing event;
and a unique-key collision with different evidence raises a conflict. Assert
the assembly, binding, signal, order-leg, and order counts/payloads are
unchanged.

**Step 5: Implement exact append-only apply**

Implement:

```python
def apply_entry_assembly_fingerprint_repair_plan(
    session_factory,
    *,
    assembly_id: int,
    execution_binding_id: int,
    expected_plan_fingerprint: str,
    applied_at: datetime,
) -> int:
    ...
```

Rebuild the plan inside apply, require one conflict-free action and exact
fingerprint, then insert `ExecutionEvent(action=RECONCILIATION_ACTION,
status="resolved", ...)`. Store bounded before/after JSON and put the repair
fingerprint in `notification_fingerprint` with `notification_status=None`.
Handle `IntegrityError` only by reloading and exactly comparing every event
field.

**Step 6: Run repair tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_assembly_fingerprint_repair.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/entry_assembly_fingerprint_repair.py tests/test_entry_assembly_fingerprint_repair.py
git commit -m "feat: add entry fingerprint reconciliation evidence"
```

### Task 5: Teach the Monitor to Verify Exact Reconciliation

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:656-749`
- Modify: `tests/test_production_safety_monitor.py:326-550`

**Step 1: Write failing monitor acceptance and rejection tests**

Extend the fixture schema with `entry_strategy_assemblies.id/evidence_json`,
`execution_bindings.id/trade_signal_id`, and `execution_events`. Add one valid
reconciliation event and assert the known mismatch clears.

Parametrize mutations of event action, status, assembly ID, binding ID, trade
signal ID, strategy ID, old fingerprint, final fingerprint, policy version,
repair fingerprint, and derived pre-finalization evidence; each mutation must
retain `live_entry_preamble_binding_evidence_missing`.

Add a two-assembly test proving one valid event does not hide another mismatch.
Keep the existing test showing databases without `execution_events` are strict.

**Step 2: Run monitor tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py -k entry_preamble -q
```

Expected: valid reconciliation is not yet recognized.

**Step 3: Add a query-only reconciliation verifier**

Select assembly ID, strategy ID, fingerprint, evidence JSON, binding ID,
trade-signal ID, and binding payload in the bounded evidence query. On mismatch,
query only events for the exact binding and fixed action. Parse their bounded
JSON and call pure proof helpers from Task 4.

Accept only exactly one complete matching event. On absent table, malformed
JSON, duplicate/conflict, or any failed equality, add the existing reason code.
Do not introduce a new ignore list or write connection.

**Step 4: Run focused monitor tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_production_safety_monitor.py -q
```

Expected: PASS and database-byte no-write assertions remain true.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "fix: verify reconciled entry fingerprint evidence"
```

### Task 6: Add the Dry-Run/Apply CLI and Runbook

**Files:**
- Modify: `src/telegram_kol_research/cli.py:1-110,3910-4075`
- Modify: `tests/test_cli_smoke.py:1-150`
- Modify: `docs/runbook.md`

**Step 1: Write failing CLI tests**

Add tests asserting:

- root help contains `repair-entry-assembly-fingerprint`;
- command help exposes exact database, assembly, binding, apply, and expected
  fingerprint options;
- dry-run prints redacted JSON and creates no event;
- `--apply` without `--expected-plan-fingerprint` exits 2 before writes;
- exact apply creates one event and prints its ID;
- no command output contains full payloads or message text.

**Step 2: Run CLI tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli_smoke.py -k entry_assembly_fingerprint -q
```

Expected: command not found or help assertion FAIL.

**Step 3: Implement the isolated command**

Add imports for the planner/apply functions and a Typer command:

```python
@app.command("repair-entry-assembly-fingerprint")
def repair_entry_assembly_fingerprint(
    database_path: Path = Path("data/research.db"),
    assembly_id: int = typer.Option(..., "--assembly-id"),
    execution_binding_id: int = typer.Option(..., "--execution-binding-id"),
    apply: bool = typer.Option(False, "--apply"),
    expected_plan_fingerprint: str | None = typer.Option(
        None, "--expected-plan-fingerprint"
    ),
) -> None:
    ...
```

Build and print the current redacted plan first. If applying, require a single
conflict-free action and exact expected fingerprint before calling apply. Do
not construct a Deepcoin or Telegram client.

**Step 4: Document the operator workflow**

Add a runbook section containing:

- the fixed defect and invariant;
- dry-run command;
- fields that must be reviewed;
- explicit prohibition on replay/order mutation;
- apply command requiring the exact dry-run fingerprint;
- no-notify monitor verification;
- rollback behavior.

State that production apply requires a fresh explicit approval after dry-run.

**Step 5: Run CLI and focused feature tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_cli_smoke.py \
  tests/test_entry_assembly_fingerprint_repair.py \
  tests/test_production_safety_monitor.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_smoke.py docs/runbook.md
git commit -m "feat: expose bounded entry fingerprint repair"
```

### Task 7: Review and Complete Local Verification

**Files:**
- Review all files changed in Tasks 1-6

**Step 1: Run diff and architecture checks**

Run:

```bash
git diff --check HEAD~6..HEAD
git status --short
rg -n "process_trade_signal_live|synchronize_pending_entry_assembly_evidence" src/telegram_kol_research/auto_trade_execution.py
```

Expected: no whitespace errors; synchronization precedes the recovery-trigger
live submit; only known user-owned unrelated files remain dirty/untracked.

**Step 2: Run the complete focused safety suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_strategy_assembly.py \
  tests/test_trade_signal_fingerprint_sync.py \
  tests/test_auto_trade_execution.py \
  tests/test_recovery_live_submit.py \
  tests/test_entry_assembly_fingerprint_repair.py \
  tests/test_production_safety_monitor.py \
  tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 3: Run the full local suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. Record total tests and duration.

**Step 4: Request code review**

Use @requesting-code-review against the design document and this plan. Resolve
all correctness, regression, missing-test, and unsafe-production findings; rerun
the affected tests after each correction.

**Step 5: Commit review corrections if needed**

```bash
git add <only reviewed task files>
git commit -m "fix: harden entry fingerprint reconciliation"
```

Expected: clean task diff and no unrelated user files staged.

### Task 8: Push, Deploy in a Safe Window, and Dry-Run Production Repair

**Files:**
- Modify only if verification evidence requires it: `docs/runbook.md`

**Step 1: Push reviewed commits**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to the reviewed local HEAD.

**Step 2: Prove a safe deployment window**

Use existing read-only production diagnostics to require zero in-flight
recognition/context claims, instruction execution, management batches,
position mutations, protection rescue, Runtime Agent work, runtime
notifications, and recovery submission. Capture two stable snapshots when the
runbook requires them.

Expected: no time-sensitive strategy operation is active. If this cannot be
proven, stop before restart and report the exact remaining verification.

**Step 3: Deploy from GitHub**

Run the repository helper appropriate to the local shell:

```bash
./scripts/server_git_update.sh
```

Preferred Windows equivalent:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls exact reviewed SHA, reinstalls editable package, and
restarts `telegram-kol.service` successfully.

**Step 4: Verify server code and services**

Confirm exact SHA/editable package, main service, Runtime Agent, scanner,
monitor timer, and HTTP 200. Run the focused server test list from Task 7.

Expected: all checks PASS; no exchange mutation is triggered by tests.

**Step 5: Run the production repair dry-run only**

Run on the server with the exact production database path:

```bash
telegram-kol repair-entry-assembly-fingerprint \
  --database-path <production-db> \
  --assembly-id 2 \
  --execution-binding-id 266
```

Expected: exactly one conflict-free action for trade signal `398`; old and
final fingerprints equal the reviewed incident values; no database counts or
payloads change.

**Step 6: Stop for explicit production-write approval**

Report the redacted plan fingerprint and server verification. Do not pass
`--apply` in this task. The append-only production write requires a fresh user
approval based on this exact dry-run.

### Task 9: Apply the Approved Event and Observe Natural Traffic

**Files:**
- No source files unless a newly discovered defect requires a separate plan

**Step 1: Reprove safe state and rebuild the plan**

After explicit approval, repeat the safe-state gate and dry-run. Require the
same exact plan fingerprint.

Expected: one unchanged action and no conflicts.

**Step 2: Apply exactly once**

Run:

```bash
telegram-kol repair-entry-assembly-fingerprint \
  --database-path <production-db> \
  --assembly-id 2 \
  --execution-binding-id 266 \
  --apply \
  --expected-plan-fingerprint <exact-dry-run-sha256>
```

Expected: one resolved append-only event; no changes to assemblies, trade
signals, bindings, order legs, or exchange state.

**Step 3: Verify the monitor without notification**

Run the established no-notify production monitor diagnostic.

Expected: healthy, no monitor error, notification disabled, and no
`live_entry_preamble_binding_evidence_missing` reason.

**Step 4: Verify idempotency read-only**

Repeat dry-run and query the exact event.

Expected: no second actionable repair and exactly one matching event.

**Step 5: Observe the next natural recovery-trigger entry**

Without replay or test trading, inspect the first naturally occurring matching
message end to end. Compare the assembly, trade-signal top-level evidence,
nested draft evidence, and execution-binding fingerprint.

Expected: all four final fingerprints match before/after normal submission and
no new reconciliation event is needed.
