# Evidence-Based Deployment Preflight Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `updated_at`-based deployment liveness with explicit work evidence and a phase-bound two-stage updater, then prove the reviewed MiMo v2 retirement can be classified without touching Batch 119 or weakening unknown-outcome safety.

**Architecture:** A new pure work-evidence registry classifies every known execution table into in-flight, unknown, restart-safe, historical, terminal, or malformed facts. Artifact schema v2 binds production/candidate commits, effective change surface, phase A and phase B database facts, while the updater postpones every production mutation until the final artifact passes.

**Tech Stack:** Python 3.11, dataclasses, SQLite query-only transactions, Typer CLI, Bash/systemd updater, pytest, Git exact-commit worktrees.

---

## Execution rules

- Work only in `/Users/steven/Documents/telegram获取消息-deployment-gate-repair` on `codex/deployment-preflight-evidence-gate`.
- Base is `7813150b7b33cd8ce3d90a6145889c6fef192dc7`; design commit is `da1754b`.
- Use `@systematic-debugging` before changing a hypothesis, `@test-driven-development` for every task, and `@requesting-code-review` before any push request.
- Do not edit production data, Batch 119, watermarks, recognition settings, or exchange state.
- Do not push until the user explicitly approves the reviewed SHA.
- Do not deploy until a second explicit approval after server shadow verification.
- If a test proves any Deepcoin POST can start without durable pre-submit evidence, stop this plan and write a global writer-lease design. Do not add an exception.

### Task 1: Introduce the pure decision vocabulary

**Files:**
- Create: `src/telegram_kol_research/deployment_work_evidence.py`
- Create: `tests/test_deployment_work_evidence.py`
- Modify: `src/telegram_kol_research/deployment_preflight.py:23-172`

**Step 1: Write the failing decision-matrix tests**

Add tests that construct bounded category counts without a database:

```python
@pytest.mark.parametrize("change_class", ["code", "schema_compatible"])
def test_restart_safe_wait_warns_for_non_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )
    assert result.blocking_reason_codes == ()
    assert result.warning_reason_codes == ("deployment_restart_safe_wait",)


@pytest.mark.parametrize("change_class", ["execution_writer", "live_promotion"])
def test_restart_safe_wait_blocks_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )
    assert result.blocking_reason_codes == ("deployment_restart_safe_wait",)


def test_unknown_outcome_always_blocks():
    result = classify_deployment_work(
        counts={"unknown_outcome": {"execution_order_legs": 1}},
        change_class="code",
    )
    assert result.blocking_reason_codes == ("deployment_unknown_outcome",)
```

Also assert `in_flight_write` and `malformed` always block, while terminal-only
facts add no reason code.

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_deployment_work_evidence.py -k 'warns or blocks'
```

Expected: FAIL because `deployment_work_evidence` does not exist.

**Step 3: Implement the minimum pure policy**

Create string constants and immutable return types:

```python
WORK_CLASSIFICATIONS = (
    "in_flight_write",
    "unknown_outcome",
    "restart_safe_wait",
    "historical_residue",
    "terminal",
    "malformed",
)

@dataclass(frozen=True, slots=True)
class DeploymentWorkDecision:
    blocking_reason_codes: tuple[str, ...]
    warning_reason_codes: tuple[str, ...]


def classify_deployment_work(*, counts, change_class):
    blocking: set[str] = set()
    warnings: set[str] = set()
    if counts.get("in_flight_write"):
        blocking.add("deployment_in_flight_write")
    if counts.get("unknown_outcome"):
        blocking.add("deployment_unknown_outcome")
    if counts.get("malformed"):
        blocking.add("deployment_evidence_malformed")
    for category, reason in (
        ("restart_safe_wait", "deployment_restart_safe_wait"),
        ("historical_residue", "deployment_historical_residue"),
    ):
        if counts.get(category):
            (blocking if change_class in WRITER_SENSITIVE else warnings).add(reason)
    return DeploymentWorkDecision(tuple(sorted(blocking)), tuple(sorted(warnings)))
```

Validate that every top-level category is known and every count is a bounded,
non-negative integer. Invalid input must raise
`DeploymentPreflightInputError("deployment_evidence_malformed")`.

**Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_deployment_work_evidence.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deployment_work_evidence.py \
  src/telegram_kol_research/deployment_preflight.py \
  tests/test_deployment_work_evidence.py
git commit -m "feat: define deployment work evidence policy"
```

### Task 2: Replace the timestamp heuristic with explicit adapters

**Files:**
- Modify: `src/telegram_kol_research/deployment_work_evidence.py`
- Modify: `src/telegram_kol_research/deployment_preflight.py:175-392,577-695`
- Modify: `tests/test_deployment_work_evidence.py`
- Modify: `tests/test_deployment_preflight.py:393-733`

**Step 1: Write the Batch-119-shaped RED test**

Create a sanitized SQLite fixture with one old management batch and leg:

```python
connection.execute(
    """INSERT INTO strategy_management_batches
       (id, status, reason_code, created_at, last_progress_at, updated_at)
       VALUES (1, 'reconciling',
               'management_close_pending_exchange_confirmation', ?, NULL, ?)""",
    (old, now),
)
connection.execute(
    """INSERT INTO strategy_management_legs
       (id, management_batch_id, status, client_order_id, exchange_order_id,
        request_json, response_json, created_at, updated_at)
       VALUES (1, 1, 'submitted', NULL, NULL, NULL, NULL, ?, ?)""",
    (old, now),
)
```

Collect facts, change only both `updated_at` values, collect again, and assert:

```python
assert first.work_classification_counts == second.work_classification_counts
assert first.work_evidence_fingerprint == second.work_evidence_fingerprint
assert first.work_classification_counts["historical_residue"] == {
    "management_batches": 1,
    "management_legs": 1,
}
```

Build artifacts for both change families. Assert `schema_compatible` is WARN and
`execution_writer` is BLOCK. Assert serialized JSON contains neither IDs nor
the raw reason string.

**Step 2: Run the exact test and verify RED**

```bash
uv run pytest -q tests/test_deployment_work_evidence.py::test_management_heartbeat_is_historical_evidence_not_fresh_write
```

Expected: FAIL because the collector still emits `fresh_active_work`.

**Step 3: Define the adapter contract**

Replace `_TableWorkSpec` with an immutable adapter:

```python
@dataclass(frozen=True, slots=True)
class WorkEvidenceAdapter:
    output_name: str
    table: str
    state_column: str
    in_flight_states: frozenset[str]
    unknown_states: frozenset[str]
    restart_safe_states: frozenset[str]
    required_columns: frozenset[str]
    progress_column: str | None = None
    origin_column: str = "created_at"
    restart_surface_files: tuple[str, ...] = ()
```

The row classifier must:

1. classify unknown outcomes before any age logic;
2. classify true in-flight states as in-flight unless an adapter has an
   explicit expired-lease/no-attempt rule;
3. classify restart-safe rows as historical only from authoritative
   `last_progress_at` or origin time, never `updated_at`;
4. emit malformed for an unknown state or missing required evidence;
5. hash only table name, state, classification, and presence/boolean evidence,
   never IDs or payloads.

**Step 4: Register every existing work table**

Migrate the complete old `_WORK_SPECS` table list. Use this conservative state
policy as the initial authority:

- `execution_order_legs`: in-flight `submitting/cancel_submitting`; unknown
  `submit_unknown/unknown_exchange_outcome/unknown`.
- `message_instruction_items`: restart-safe `pending`; in-flight `executing`;
  unknown `unknown`.
- `trade_signals`: restart-safe `pending`; in-flight `processing`; unknown
  `unknown_exchange_outcome/partial_submission_failed`.
- `instruction_execution_contracts`: restart-safe `pending/deferred`; in-flight
  `submitting`; unknown `submit_unknown`.
- `strategy_revision_batches`: restart-safe `planned/old_entries_terminal/
  reconciling`; in-flight `cancelling_old_entries/submitting_replacements/
  rebuilding`; unknown `recovery_required`.
- `strategy_management_batches`: restart-safe `ready/pending/submitted/
  reconciling/protection_ready`; in-flight `reserved/executing`; unknown
  `submit_unknown/partial_failed/recovery_required`.
- `strategy_management_legs`: restart-safe `planned/submitted`; in-flight
  `reserved`; unknown `submit_unknown/recovery_required`.
- `strategy_management_components`: restart-safe `pending/definitely_rejected`;
  in-flight `preflighting/submitting`; unknown `awaiting_exchange/
  recovery_required`.
- `position_mutation_intents`: restart-safe `submitted`; in-flight
  `reserved/submitting`; unknown `submit_unknown/recovery_required`.
- `bound_position_close_reservations`: restart-safe `submitted`; in-flight
  `reserved`; unknown `submit_unknown/unknown_exchange_outcome/recovery_required`.
- `position_backup_stop_orders`: in-flight `submitting`; unknown
  `pending_readback/unknown_exchange_outcome`.
- `position_take_profit_orders`: unknown `cancel_requested` until exact
  cancellation readback proves terminal.
- `position_protection_legs`: restart-safe `planned/waiting_fill`; in-flight
  `submitting`; unknown `protection_recovery_pending`.
- `trigger_protection_intents`: restart-safe `pending`; in-flight `submitting`;
  unknown `retrying`.
- `trigger_protection_stop_rescues`: restart-safe `ready/submitted`; in-flight
  `reserved`; unknown `submit_unknown/recovery_required`.
- `trigger_take_profit_convergences`: restart-safe `ready/submitted`; in-flight
  `reserved`; unknown `submit_unknown`.
- `strategy_break_even_convergences`: restart-safe `planned`; in-flight
  `claimed/preflight_verified/deciding_by_market/executing_market_decisions`;
  unknown `recovery_required`.
- `strategy_break_even_convergence_legs`: in-flight `decision_reserved`;
  unknown `submit_unknown/recovery_required`.
- `source_message_deletion_exits`: restart-safe `pending/reconciling`; in-flight
  `cancelling_entries/closing_positions`; unknown `recovery_required`.

Before coding each row, read its producer and reconciler transition tests. If a
producer contradicts this table, update the plan/design before implementation;
do not guess or silently loosen the policy.

**Step 5: Integrate facts without breaking non-work checks**

Replace these fields:

```python
fresh_active_work
historical_active_residue_count
historical_unknown_outcome_count
```

with:

```python
work_classification_counts: Mapping[str, Mapping[str, int]]
work_evidence_fingerprint: str
```

Keep exchange snapshot, protection, backup, migration, shadow evidence, and
database watermark behavior unchanged. Remove `_DEFAULT_ACTIVE_WINDOW` only
after all callers and tests stop depending on it.

**Step 6: Run focused regression tests**

```bash
uv run pytest -q tests/test_deployment_work_evidence.py \
  tests/test_deployment_preflight.py
```

Expected: PASS; no test may assert `fresh_active_exchange_work`.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/deployment_work_evidence.py \
  src/telegram_kol_research/deployment_preflight.py \
  tests/test_deployment_work_evidence.py tests/test_deployment_preflight.py
git commit -m "fix: classify deployment work by durable evidence"
```

### Task 3: Bind candidate restart compatibility and effective change class

**Files:**
- Create: `src/telegram_kol_research/deployment_change_surface.py`
- Create: `tests/test_deployment_change_surface.py`
- Modify: `src/telegram_kol_research/deployment_work_evidence.py`
- Modify: `src/telegram_kol_research/deployment_preflight.py`

**Step 1: Write RED tests for underdeclared changes**

Create two tiny Git histories in `tmp_path` and assert:

```python
safe = classify_change_surface(
    repository=repo,
    production_commit=base,
    candidate_commit=guard_only,
    requested_change_class="schema_compatible",
)
assert safe.effective_change_class == "schema_compatible"

unsafe = classify_change_surface(
    repository=repo,
    production_commit=base,
    candidate_commit=writer_change,
    requested_change_class="schema_compatible",
)
assert unsafe.effective_change_class == "execution_writer"
assert unsafe.underdeclared is True
```

Add a restart-compatibility test: when a database contains restart-safe
management residue, changing `strategy_management_reconciliation.py` must make
the residue blocking; changing only deployment guard files must not.

**Step 2: Run and verify RED**

```bash
uv run pytest -q tests/test_deployment_change_surface.py
```

Expected: FAIL because the module does not exist.

**Step 3: Implement a versioned surface registry**

Use `subprocess.run([...], check=True, capture_output=True)` with argument lists,
never a shell. Bind exact Git blob hashes for changed paths and for every
restart handler used by a present residue.

The registry must distinguish:

```python
DEPLOYMENT_GUARD_PATHS = frozenset({
    "src/telegram_kol_research/deployment_preflight.py",
    "src/telegram_kol_research/deployment_work_evidence.py",
    "src/telegram_kol_research/deployment_change_surface.py",
    "src/telegram_kol_research/deployment_preflight_cli.py",
    "deploy/telegram-kol-update",
    "scripts/bootstrap_server_updater.sh",
    "scripts/server_git_update.sh",
    "scripts/server_git_update.ps1",
})
```

Add exact writer and live-promotion path sets from the existing execution
architecture. A path absent from every set defaults to `code`; a changed
writer path upgrades to `execution_writer`; an authority activation path
upgrades to `live_promotion`. For broad files changed only to remove dormant
MiMo v2, bind the exact reviewed diff fingerprint and prove via the retirement
boundary test that no live authority is added. An unknown or missing Git object
is malformed/BLOCK.

Map each work adapter to its read-only restart handler files. If one of those
files differs and that adapter emits restart-safe/history rows, mark
`restart_compatibility_changed` and block non-writer deployment rather than
trusting the requested class.

**Step 4: Include bounded surface facts**

Return only versions, effective class, changed-path count, compatibility
boolean, and SHA-256 fingerprints. Do not serialize path names in the final
artifact.

**Step 5: Run tests and commit**

```bash
uv run pytest -q tests/test_deployment_change_surface.py \
  tests/test_deployment_work_evidence.py
git add src/telegram_kol_research/deployment_change_surface.py \
  src/telegram_kol_research/deployment_work_evidence.py \
  src/telegram_kol_research/deployment_preflight.py \
  tests/test_deployment_change_surface.py
git commit -m "feat: bind deployment change surface"
```

### Task 4: Introduce phase-bound artifact schema v2

**Files:**
- Modify: `src/telegram_kol_research/deployment_preflight.py:32-45,395-529`
- Create: `src/telegram_kol_research/deployment_preflight_cli.py`
- Create: `tests/test_deployment_preflight_cli.py`
- Modify: `tests/test_deployment_preflight.py`

**Step 1: Write RED artifact tests**

Test that a preliminary artifact includes:

```python
{
    "schema_version": 2,
    "phase": "preliminary",
    "production_commit": "a" * 40,
    "candidate_commit": "b" * 40,
    "requested_change_class": "schema_compatible",
    "effective_change_class": "schema_compatible",
    "preliminary_fingerprint": None,
}
```

Test that final creation requires a valid, unexpired preliminary artifact with
matching commits, class, policy version, surface fingerprint, and database
transition. Tampering or using a final artifact as the parent must raise a
specific `DeploymentPreflightInputError`.

Add the phase-drift test:

```python
final_facts = replace(
    preliminary_facts,
    work_classification_counts={
        "unknown_outcome": {"execution_order_legs": 1}
    },
)
artifact = build_final(...)
assert artifact["decision"] == "BLOCK"
assert "deployment_unknown_outcome" in artifact["reason_codes"]
```

**Step 2: Run and verify RED**

```bash
uv run pytest -q tests/test_deployment_preflight.py -k 'phase or preliminary or final'
```

Expected: FAIL against schema v1.

**Step 3: Implement schema v2**

Bump the artifact shape instead of accepting both contracts in the updater.
Keep canonical JSON, bounded size, atomic mode-0600 writes, TTL, and
constant-time fingerprint verification.

Expose explicit builders or one builder with mandatory phase arguments. The
final builder must include the preliminary artifact fingerprint and reject:

- mismatched production/candidate SHA;
- requested/effective class drift;
- policy/surface version drift;
- a new unsafe work classification;
- watermark regression;
- malformed backup/migration evidence.

Natural watermark growth between A and B is allowed before stop; the final
backup and migration copy must equal phase B exactly.

**Step 4: Add the standalone CLI**

Implement:

```text
python -m telegram_kol_research.deployment_preflight_cli collect
python -m telegram_kol_research.deployment_preflight_cli verify
```

Required collect options include repository path, production commit, candidate
commit, requested class, phase, output, database, snapshots, schema evidence,
and preliminary artifact for final phase. The CLI must emit exit codes
0 PASS, 2 WARN, 3 BLOCK, 4 malformed.

Keep the legacy Typer command out of the managed updater path so changing the
broad `cli.py` is not required for this repair. Document it as non-authoritative
until a later cleanup.

**Step 5: Run tests and commit**

```bash
uv run pytest -q tests/test_deployment_preflight.py \
  tests/test_deployment_preflight_cli.py tests/test_cli_smoke.py
git add src/telegram_kol_research/deployment_preflight.py \
  src/telegram_kol_research/deployment_preflight_cli.py \
  tests/test_deployment_preflight.py tests/test_deployment_preflight_cli.py
git commit -m "feat: bind two-phase deployment artifacts"
```

### Task 5: Make the updater non-mutating until phase B passes

**Files:**
- Modify: `deploy/telegram-kol-update:59-242`
- Modify: `scripts/bootstrap_server_updater.sh:20-53`
- Modify: `scripts/server_git_update.ps1:24-51`
- Modify: `tests/test_server_update_scripts.py`
- Create: `tests/test_server_updater_phases.py`

**Step 1: Write RED ordering tests**

Assert textual ordering and executable state-machine behavior:

```python
assert candidate_updater_extract < preliminary_preflight < service_stop
assert service_stop < final_schema_backup < final_preflight
assert final_verify < checkout < pip_install < updater_install < service_start
assert "/usr/local/bin/telegram-kol-update" not in bootstrap_before_execute
```

The phase test harness must fake `git`, `systemctl`, and the preflight CLI and
prove:

- preliminary BLOCK never calls stop;
- final BLOCK calls stop, then restarts the old service, but never calls
  checkout/install;
- final PASS performs mutations in the exact required order;
- stop timeout aborts before final collection;
- install/start failure restores previous SHA and editable package.

**Step 2: Run and verify RED**

```bash
uv run pytest -q tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py
```

Expected: FAIL because bootstrap currently installs the candidate updater before
preflight and both preflights use the same artifact contract.

**Step 3: Change bootstrap execution**

Keep the SHA-verified temporary candidate updater under `/run` and execute that
path directly. Do not install it to `/usr/local/bin` in the bootstrap script.
The candidate updater installs itself only after final artifact verification.

**Step 4: Implement phase A and B in the candidate updater**

- Capture `previous_commit` before staging and pass it as production commit.
- Run phase A without requiring schema backup/migration evidence.
- Stop the service and prove inactive.
- Create final backup and dry-run after stop.
- Run phase B with the preliminary artifact.
- Verify phase B immediately before checkout.
- Keep the existing exact remote SHA, lock, cleanup, rollback, and safe-path
  deletion checks.

Set `service_stopped=1` only after a successful stop command, and make cleanup
restart the old service on every pre-checkout phase-B failure.

**Step 5: Run shell and pytest checks**

```bash
bash -n deploy/telegram-kol-update
bash -n scripts/bootstrap_server_updater.sh
uv run pytest -q tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add deploy/telegram-kol-update scripts/bootstrap_server_updater.sh \
  scripts/server_git_update.ps1 tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py
git commit -m "fix: gate deployment in two bound phases"
```

### Task 6: Prove the race boundary and durable writer premise

**Files:**
- Create: `tests/test_deployment_writer_boundary.py`
- Modify only if a missing invariant is found: relevant existing fault-injection test file

**Step 1: Inventory every Deepcoin POST entry point**

Read `DeepcoinRestClient` completely and enumerate `place_order`,
`trigger_order`, position SL/TP mutations, regular-order cancellation,
trigger-order cancellation, replacement, and exact close paths. Map each call
site to its durable reservation/intent and existing fault-injection test.

The new test stores the expected write methods and fails if a new public POST
method appears without a declared durable owner.

**Step 2: Add the between-phase race RED tests**

Starting from an allowed phase-A artifact, insert each of:

- one `submitting` row;
- one `submit_unknown` row;
- one safe `submitted` row;
- one heartbeat-only `updated_at` change.

Assert final decisions are respectively BLOCK, BLOCK, allowed with WARN, and
unchanged.

**Step 3: Run the fault-injection gate**

```bash
uv run pytest -q tests/test_deployment_writer_boundary.py \
  tests/test_instruction_execution_fault_injection.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_mutation_gateway.py
```

Expected: PASS and no exchange-call fixture observes a call before its durable
pre-submit state.

If any existing production path lacks the invariant, stop. Record the exact
path and do not continue to Tasks 7-9.

**Step 4: Commit tests only**

```bash
git add tests/test_deployment_writer_boundary.py
git commit -m "test: prove deployment writer race boundary"
```

### Task 7: Update runbooks and retirement boundary

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`
- Modify: `docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history-design.md`
- Modify: `docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history.md`
- Modify: `tests/test_mimo_v2_retirement_boundary.py`
- Modify: `tests/test_server_update_scripts.py`

**Step 1: Write the documentation-boundary RED tests**

Assert the authoritative docs mention:

- the six work classifications;
- phase A versus phase B;
- unknown outcomes block regardless of age;
- restart-safe/history is WARN only for code/schema;
- candidate updater is not installed before phase B;
- push and deployment remain separate approvals.

Assert the retirement boundary still rejects all MiMo v2 runtime modules and
settings while allowing only the new deployment-gate files beyond the pre-v2
runtime tree.

**Step 2: Run RED**

```bash
uv run pytest -q tests/test_mimo_v2_retirement_boundary.py \
  tests/test_server_update_scripts.py -k 'documentation or retirement or phase'
```

Expected: FAIL until docs are updated.

**Step 3: Update documentation**

Replace `fresh_active_exchange_work` instructions and hard-coded timestamp
windows with the new reason codes and phase protocol. State explicitly that:

- no historical row is edited to permit deployment;
- a legitimate BLOCK is not overrideable;
- a schema rollback may restart around unchanged read-only residue;
- writer changes cannot;
- a PASS/WARN never authorizes an exchange write or MiMo v2 activation.

**Step 4: Run and commit**

```bash
uv run pytest -q tests/test_mimo_v2_retirement_boundary.py \
  tests/test_server_update_scripts.py
git add docs/runbook.md docs/server-deployment.md docs/migration-handoff.md \
  docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history-design.md \
  docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history.md \
  tests/test_mimo_v2_retirement_boundary.py tests/test_server_update_scripts.py
git commit -m "docs: explain evidence-based deployment gate"
```

### Task 8: Complete local verification and independent review

**Files:**
- Review: all changes from `7813150b7b33cd8ce3d90a6145889c6fef192dc7..HEAD`

**Step 1: Run focused tests**

```bash
uv run pytest -q tests/test_deployment_work_evidence.py \
  tests/test_deployment_change_surface.py \
  tests/test_deployment_preflight.py \
  tests/test_deployment_preflight_cli.py \
  tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py \
  tests/test_deployment_writer_boundary.py \
  tests/test_instruction_execution_fault_injection.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_mutation_gateway.py \
  tests/test_mimo_v2_retirement_boundary.py \
  tests/test_authoritative_recognition.py \
  tests/test_trading_settings.py
```

Expected: PASS.

**Step 2: Run static and full checks**

```bash
python -m compileall -q src tests
git diff --check 7813150b7b33cd8ce3d90a6145889c6fef192dc7..HEAD
uv run pytest -q
git status --short
```

Expected: compile and diff checks PASS, full suite PASS, clean worktree.

**Step 3: Request independent review**

Use `@requesting-code-review`. Review specifically for:

- a false pass for a real or unknown exchange write;
- another heartbeat field entering the safety fingerprint;
- state/adapter allowlist drift;
- candidate change-class underdeclaration;
- phase-A/phase-B race or artifact substitution;
- mutation before final authorization;
- rollback leaving the service stopped or candidate updater installed;
- Batch119-specific exceptions;
- MiMo v2 runtime accidentally retained or reintroduced.

Resolve every Critical and Important finding with RED then minimal GREEN. Re-run
the affected focused set and full suite. Do not request push approval until the
review verdict is Ready.

**Step 4: Record the reviewed SHA**

```bash
git rev-parse HEAD
git status --short --branch
```

Stop and request explicit push approval with the exact SHA and test/review
evidence.

### Task 9: Push, server shadow, and separate deployment approval

**Files:**
- No new source changes unless server validation reveals a reproducible defect.

**Step 1: Push only after explicit approval**

```bash
git push -u origin codex/deployment-preflight-evidence-gate
git ls-remote origin refs/heads/codex/deployment-preflight-evidence-gate
```

Expected: remote SHA equals the exact reviewed local SHA.

**Step 2: Stage beside production**

Create a mode-0700 candidate worktree under
`/opt/telegram-kol-candidates/deployment-gate-<SHA>`. Do not install it, stop the
service, change settings, or modify the database. Verify exact SHA and clean
tracked tree.

**Step 3: Run server focused tests**

Use the production virtual environment with candidate `PYTHONPATH` and run the
same focused deployment-gate test set. Expected: PASS with no notification or
exchange client invocation.

**Step 4: Run read-only shadow classification**

Use production `data/research.db` in SQLite read-only mode and candidate code.
Write only sanitized mode-0600 artifacts under `/run/telegram-kol`. Require:

```text
in_flight_write = 0
unknown_outcome = 0
Batch-119-shaped management facts = restart_safe_wait or historical_residue
policy-only schema_compatible evaluation = WARN
policy-only execution_writer evaluation = BLOCK
exact candidate effective class = execution_writer
exact candidate decision = BLOCK
database writes = 0
notifications = 0
exchange writes = 0
```

Repeat the shadow collection after at least one reconciliation heartbeat and
prove the safety fingerprint is unchanged even though `updated_at` changed.

The policy-only evaluations reuse the same sanitized fact snapshot without
claiming that the exact candidate belongs to both classes. The reviewed
candidate now includes the Task 6 terminal-entry cleanup
pre-submit ownership repair. Its exact production-to-candidate surface must
therefore resolve to `execution_writer`; requesting `schema_compatible` is
underdeclared and BLOCK. The two class results above remain a policy proof, but
the current combined candidate is expected to take the `execution_writer =
BLOCK` branch while restart-safe/history residue exists. Stop after recording
that shadow result. Do not request deployment approval for this SHA, split out
an unsafe gate-only candidate, or add a commit/Batch119 exception.

**Step 5: Re-prove production immutability**

Verify production SHA, active service, tracked-file count, database watermark,
settings, and MiMo v1 authority are unchanged. Remove disposable candidate
test artifacts and databases; retain only the reviewed sanitized shadow
summary if the runbook requires it.

Stop and request a separate deployment approval only for a future candidate
whose exact effective class receives an allowed Phase A and Phase B decision.
A successful shadow is not deployment authorization; the current combined
`execution_writer` candidate is expected to remain blocked.

**Step 6: Deploy only after that approval (not applicable to the current SHA)**

Run the reviewed helper only with the exact effective class for that future
candidate. Never request `schema_compatible` for a candidate classified as
`execution_writer`. Accept only phase-A and phase-B PASS/WARN artifacts. If
either is BLOCK or malformed, leave/restore the prior service and report the
exact reason.

After success verify:

```text
production SHA = reviewed candidate SHA
telegram-kol.service = active
tracked changes = 0
HTTP = healthy
MiMo v1 = only recognition path
non-v1 recognition runs = 0
audit/reference orphans = 0
deployment exchange writes = 0
historical replay = 0
notifications = 0
```

Do not enable MiMo v2 or perform any Batch 119 recovery.
