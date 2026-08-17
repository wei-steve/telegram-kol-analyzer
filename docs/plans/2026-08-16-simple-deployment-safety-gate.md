# Simple Deployment Safety Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the production deployment preflight with a small, automatic,
fail-closed gate that blocks only active exchange writes, genuinely unknown
exchange outcomes, invalid registered evidence, or queued work combined with a
changed writer surface.

**Architecture:** Build the candidate from production SHA `2274d90` and keep
the existing runtime writer and dormant MiMo v2 code byte-for-byte unchanged.
A flat, reviewed writer manifest produces one Git-object fingerprint. Explicit
read-only SQLite adapters reduce registered execution evidence to aggregate
`active_write`, `unknown_outcome`, `queued_work`, and `inactive` counts. A
standalone CLI produces a bounded Phase A artifact, then a directly bound Phase
B artifact after the service stops. The updater performs automatic schema
detection, backup/dry-run when required, and rollback on every mutation-side
failure.

**Tech Stack:** Python 3.12, SQLite, Git, argparse, Bash, PowerShell, pytest

---

## Fixed Safety Boundaries

- Work only in `codex/deployment-gate-simplification`, based on production
  `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`.
- Do not cherry-pick the terminal-entry repair or the MiMo v2 retirement diff.
- Do not add, remove, modify, enable, or replay MiMo v2 code or data.
- Do not edit production data, watermarks, execution policy, or deployment
  blockers to fit current row IDs or counts.
- Do not add manual change classes, operator overrides, age-based safety, a
  per-owner graph, or Batch-specific exceptions.
- Every behavior change starts with a failing test and is implemented with the
  smallest production change that makes it pass.
- A push, server shadow, and deployment each require their own explicit user
  approval. Completing this plan does not authorize any of them.

## Task 1: Encode the Four-Evidence Decision Policy

**Files:**

- Create: `src/telegram_kol_research/deployment_work_evidence.py`
- Create: `tests/test_deployment_work_evidence.py`

### Step 1: Write the RED decision matrix

Create table-driven tests for a pure `decide_deployment()` function. The
minimum public input is:

```python
@dataclass(frozen=True, slots=True)
class DeploymentEvidenceCounts:
    active_write: int = 0
    unknown_outcome: int = 0
    queued_work: int = 0
    inactive: int = 0
    invalid_evidence: int = 0


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    decision: Literal["PASS", "WARN", "BLOCK"]
    reason_codes: tuple[str, ...]
```

Cover these exact cases:

```python
@pytest.mark.parametrize(
    ("counts", "writer_changed", "expected"),
    [
        (DeploymentEvidenceCounts(active_write=1), False, "BLOCK"),
        (DeploymentEvidenceCounts(unknown_outcome=1), False, "BLOCK"),
        (DeploymentEvidenceCounts(invalid_evidence=1), False, "BLOCK"),
        (DeploymentEvidenceCounts(queued_work=1), True, "BLOCK"),
        (DeploymentEvidenceCounts(queued_work=1), False, "WARN"),
        (DeploymentEvidenceCounts(inactive=9), False, "PASS"),
    ],
)
```

Also prove negative counts, booleans used as counts, unknown fields, and
unbounded counts are invalid inputs. Do not include age or heartbeat fields.

Run:

```bash
pytest -q tests/test_deployment_work_evidence.py
```

Expected: FAIL because the module does not exist.

### Step 2: Implement the minimal pure policy

Use a fixed evaluation order:

1. `invalid_evidence`
2. `active_write`
3. `unknown_outcome`
4. `writer_changed and queued_work`
5. `queued_work`
6. no blocker or warning

Return stable reason codes and never accept an override argument.

### Step 3: Verify and commit

```bash
pytest -q tests/test_deployment_work_evidence.py
git diff --check
git add src/telegram_kol_research/deployment_work_evidence.py tests/test_deployment_work_evidence.py
git commit -m "feat: define simple deployment evidence policy"
```

## Task 2: Build One Automatic Writer Fingerprint

**Files:**

- Create: `src/telegram_kol_research/deployment_writer_surface.py`
- Create: `tests/test_deployment_writer_surface.py`

### Step 1: Write RED Git-object tests

Test `classify_candidate_surface(repository, production_commit,
candidate_commit)` against temporary Git repositories. Require a result shaped
like:

```python
@dataclass(frozen=True, slots=True)
class CandidateSurface:
    manifest_version: int
    production_writer_fingerprint: str
    candidate_writer_fingerprint: str
    writer_changed: bool
    schema_changed: bool
    changed_path_count: int
```

Required RED cases:

- identical writer blobs in different commits produce identical fingerprints;
- one writer file change flips `writer_changed`;
- gate, updater, test, or documentation changes do not flip it;
- `db.py`, `models.py`, or any `migrations/**` diff sets `schema_changed`;
- schema detection cannot be downgraded by a caller argument;
- a missing Git object, non-commit object, path outside the repository, or
  malformed SHA fails closed;
- path order, file mode, deletion, rename, and missing-at-one-side cases are
  deterministic.

Run and confirm the import failure:

```bash
pytest -q tests/test_deployment_writer_surface.py
```

### Step 2: Add the flat reviewed manifest

Define one `WRITER_SURFACE_PATHS` set. Seed it from the already reviewed
execution, mutation-authority, and outcome-authority files, including at least:

```text
auto_trade_execution.py
deepcoin_client.py
deepcoin_execution_actions.py
entry_revision_executor.py
execution_bindings.py
instruction_execution_contracts.py
instruction_execution_entry_adapter.py
instruction_execution_management_adapter.py
instruction_execution_outcomes.py
instruction_execution_projection.py
instruction_execution_reconciliation.py
legacy_conditional_cancel.py
message_instruction_items.py
native_tpsl_migration.py
position_attribution.py
position_authority_lock.py
position_mutation_authority.py
position_mutation_gateway.py
position_mutation_intents.py
position_protection_legs.py
position_take_profit_orders.py
protection_attribution.py
protection_ledger.py
recovery_live_submit.py
recovery_live_submit_gate.py
recovery_order_confirmation.py
source_message_deletion.py
source_message_deletion_worker.py
strategy_management_batches.py
strategy_management_components.py
strategy_management_contracts.py
strategy_management_executor.py
strategy_management_market_decisions.py
strategy_management_market_policy.py
strategy_management_reconciliation.py
strategy_management_worker.py
strategy_revision_planner.py
terminal_entry_cleanup.py
trade_signals.py
trigger_backup_stop_executor.py
trigger_protection_intents.py
trigger_protection_rescue_worker.py
web_app.py
cli.py
```

Keep it flat: no table-to-owner mapping, restart subgraph, change-class rank, or
retirement exception. Hash `path + Git mode + blob identity/content marker` in
stable path order for both exact commits.

### Step 3: Add mutation-boundary inventory tests

Parse `DeepcoinRestClient` with `ast` and find every method that directly or
transitively reaches `_request("POST", ...)`. Scan project modules for calls to
those methods and assert every call-site file is in `WRITER_SURFACE_PATHS`.

Add explicit subset assertions for shared mutation locks, outcome interpreters,
state projectors, and worker claim modules so an indirect primitive cannot
silently disappear from the flat manifest. A synthetic new POST call site
outside the manifest must fail the test.

This test checks membership only. Do not restore the old per-call-site owner
and fault-test graph.

### Step 4: Verify and commit

```bash
pytest -q tests/test_deployment_writer_surface.py
git diff --check
git add src/telegram_kol_research/deployment_writer_surface.py tests/test_deployment_writer_surface.py
git commit -m "feat: fingerprint deployment writer surface"
```

## Task 3: Register Only Execution Evidence

**Files:**

- Modify: `src/telegram_kol_research/deployment_work_evidence.py`
- Modify: `tests/test_deployment_work_evidence.py`

### Step 1: Write RED registry and read-only tests

Build small SQLite fixtures and prove:

- `mimo_recognition_runs` and `mimo_recognition_attempts` are not registered
  execution work and never become invalid because they have status columns;
- `position_backup_stop_orders.status = 'missing'` is valid inactive evidence;
- `source_message_deletion_exits.state = 'unbound'` with no target or claim is
  valid inactive evidence;
- an unrecognized state in a registered execution table increments only
  `invalid_evidence`;
- a random new table with a `status` or `state` column is ignored rather than
  generically treated as execution work;
- missing required columns, duplicate/conflicting required projections, and
  malformed registered values fail closed;
- collection opens `file:...?mode=ro`, sets `PRAGMA query_only=ON`, and cannot
  create or update a table;
- aggregate output contains no row ID, message text, symbol, order identifier,
  payload, or credential.

### Step 2: Implement explicit adapters

Create a bounded registry such as:

```python
@dataclass(frozen=True, slots=True)
class EvidenceAdapter:
    name: str
    required_tables: tuple[str, ...]
    collect: Callable[[sqlite3.Connection], EvidenceTally]
```

Each adapter must use explicit SQL and classify only known execution tables.
Table discovery may validate required schema but must never create a generic
adapter from a column name. Enforce a maximum count and query/result bounds.

Do not inspect `created_at` or `updated_at` to decide safety.

### Step 3: Verify and commit

```bash
pytest -q tests/test_deployment_work_evidence.py
git diff --check
git add src/telegram_kol_research/deployment_work_evidence.py tests/test_deployment_work_evidence.py
git commit -m "feat: collect registered deployment evidence"
```

## Task 4: Close Cross-Table Production Shapes

**Files:**

- Modify: `src/telegram_kol_research/deployment_work_evidence.py`
- Modify: `tests/test_deployment_work_evidence.py`

### Step 1: Add one RED test per audited predicate

Use synthetic IDs and dates, never copied production IDs or counts. Cover:

- a stale/unknown binding plus an `unknown` leg, with no active claim and no
  automatic mutation path, is inactive;
- an instruction item `unknown` without a contract is inactive;
- a contract or leg with `submit_unknown` and no terminal proof is
  `unknown_outcome`;
- management `recovery_required` excluded by the worker's actual claim
  predicate is inactive;
- the same management row with an attempted unknown child is
  `unknown_outcome`;
- protection recovery whose parent and entry evidence are closed is inactive;
- `partial_submission_failed` with complete verified active/terminal leg
  projection is inactive;
- the same summary with an incomplete or unknown attempted leg is
  `unknown_outcome`;
- durable `submitting`, `cancel_submitting`, and equivalent pre-submit claims
  are `active_write`;
- normal pending entry, ready management, and other worker-claimable rows are
  `queued_work`;
- terminal or permanently paused records are inactive;
- changing only `updated_at` never changes the classification or evidence
  fingerprint; changing durable claim/outcome evidence does.

Assert each fixture contributes to exactly one category.

### Step 2: Implement contextual SQL adapters

Mirror the production worker claim predicates and durable terminal-proof joins.
Keep the logic local to the affected evidence adapter. A textual state such as
`unknown` is never sufficient by itself; require attempted-write evidence and
the absence of a complete terminal proof.

The adapters return only aggregate counts plus a stable sanitized evidence
fingerprint. Do not expose the rows used to derive them.

### Step 3: Run focused runtime contract tests

```bash
pytest -q \
  tests/test_deployment_work_evidence.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_source_message_deletion.py
git diff --check
git add src/telegram_kol_research/deployment_work_evidence.py tests/test_deployment_work_evidence.py
git commit -m "fix: classify durable execution evidence contextually"
```

## Task 5: Replace the Artifact Contract with Direct Two-Phase Binding

**Files:**

- Modify: `src/telegram_kol_research/deployment_preflight.py`
- Rewrite focused cases in: `tests/test_deployment_preflight.py`

### Step 1: Write RED artifact tests

Define version 2 artifacts containing only:

```text
artifact_version, phase, production_commit, candidate_commit,
writer_manifest_version, production_writer_fingerprint,
candidate_writer_fingerprint, writer_changed, schema_changed,
evidence_counts, evidence_fingerprint, snapshot_status,
schema_verification, database_watermark, checked_at, expires_at,
parent_fingerprint (final only), decision, reason_codes, fingerprint
```

Required tests:

- preliminary and final builders recompute decisions from checked facts;
- final accepts exactly one preliminary parent fingerprint;
- final rejects a wrong parent, changed commit, changed writer surface, schema
  classification drift, or a database watermark that moves backward;
- verification recomputes the decision and reason codes instead of trusting
  serialized values;
- changing a real BLOCK to PASS and recomputing the ordinary SHA still fails;
- malformed counts, unknown keys, oversized values, NaN-like values, future
  times, expired artifacts, bad phases, and an unsupported version fail with
  input error;
- artifacts are deterministic, bounded, atomically replaced, and mode `0600`;
- no recursive artifact chain is accepted;
- unchanged-writer incomplete exchange snapshot is WARN, but changed-writer
  incomplete snapshot is BLOCK;
- `schema_changed=True` is BLOCK until backup `quick_check` and candidate
  migration dry-run evidence are both valid.

### Step 2: Implement the minimal artifact API

Expose explicit functions:

```python
build_preliminary_deployment_preflight_artifact(...)
build_final_deployment_preflight_artifact(..., preliminary_artifact=...)
verify_deployment_preflight_artifact(..., expected_phase=...)
read_deployment_preflight_artifact(path)
write_deployment_preflight_artifact(path, artifact)
```

Remove `DEPLOYMENT_CHANGE_CLASSES`, `change_class`, age cutoffs, and generic
state-table discovery. Use the pure policy from Task 1 and exact surface facts
from Task 2.

### Step 3: Verify and commit

```bash
pytest -q \
  tests/test_deployment_preflight.py \
  tests/test_deployment_work_evidence.py \
  tests/test_deployment_writer_surface.py
git diff --check
git add src/telegram_kol_research/deployment_preflight.py tests/test_deployment_preflight.py
git commit -m "feat: bind simple two-phase preflight artifacts"
```

## Task 6: Add a Standalone Gate CLI

**Files:**

- Create: `src/telegram_kol_research/deployment_preflight_cli.py`
- Create: `tests/test_deployment_preflight_cli.py`
- Modify if required: `pyproject.toml`

### Step 1: Write RED subprocess tests

Test the exact executable contract through `sys.executable -m
telegram_kol_research.deployment_preflight_cli`:

```text
surface       compute exact Git surface facts
collect       collect and write preliminary or final artifact
verify        verify an artifact against exact expected inputs
```

Require these stable return codes:

```text
0 PASS
2 WARN
3 BLOCK
4 invalid CLI, artifact, Git, database, or evidence input
```

Prove argparse errors, an invalid `--phase`, missing arguments, an unreadable
artifact, and an internal validation error all return 4, never 2. Prove stdout
and stderr contain only sanitized summaries.

### Step 2: Implement with argparse

Keep this entry point independent of the large application `cli.py` so the
gate-only candidate does not change a writer-sensitive module. Catch argparse
failures and domain input errors explicitly. Do not catch an invalid input and
convert it to WARN.

### Step 3: Verify and commit

```bash
pytest -q tests/test_deployment_preflight_cli.py
python -m compileall -q src/telegram_kol_research/deployment_preflight_cli.py
git diff --check
git add src/telegram_kol_research/deployment_preflight_cli.py tests/test_deployment_preflight_cli.py pyproject.toml
git commit -m "feat: add standalone deployment preflight cli"
```

If `pyproject.toml` needs no entry-point change, omit it from `git add`.

## Task 7: Simplify the Updater and Preserve Rollback

**Files:**

- Modify: `deploy/telegram-kol-update`
- Modify: `scripts/bootstrap_server_updater.sh`
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Modify: `tests/test_server_update_scripts.py`
- Create: `tests/test_server_updater_phases.py`

### Step 1: Write RED interface tests

Require every wrapper and the updater to accept the branch and exact expected
commit, but no `CHANGE_CLASS`, `ChangeClass`, live-promotion token, or operator
override. Assert bootstrap:

- fetches the exact candidate object;
- extracts the reviewed updater into a mode-0700 temporary directory;
- verifies its SHA-256;
- runs it without installing candidate code into production before Phase B.

### Step 2: Write RED updater fault harness

Use fake `git`, `systemctl`, Python gate CLI, package install, and database
commands. Assert call order:

```text
fetch exact SHA
automatic surface/schema detection
schema backup + quick_check + migration dry-run only when schema_changed
Phase A collect + verify
record stop_attempted
stop + prove inactive
Phase B collect + verify against Phase A
checkout exact SHA
install
start + prove active
install/update durable updater last
```

Add fault cases for:

- Phase A BLOCK/invalid: no stop, checkout, install, or restart;
- schema backup, quick-check, or migration dry-run failure: no stop;
- stop returns non-zero after making the service inactive;
- stop times out and the systemd stop job completes later;
- TERM and INT during the foreground stop;
- Phase B BLOCK/invalid;
- final verifier return 4;
- checkout, install, candidate start, and health verification failure;
- rollback checkout/install/start failure;
- every attempted stop restores and verifies the original service unless the
  candidate has completed successfully;
- return code 2 continues only when it came from a successfully verified WARN
  artifact; CLI syntax errors return 4 and cannot bypass verification.

### Step 3: Implement the minimal updater flow

Set `stop_attempted=1` before invoking `timeout systemctl stop`. Cleanup must
use `stop_attempted` and the live unit state, not only the command return code.
Bind both phases to the same exact commits and writer fingerprints. Do not
install candidate Python or the candidate updater before final authorization.

### Step 4: Verify and commit

```bash
bash -n deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh \
  scripts/server_git_update.sh
pytest -q tests/test_server_update_scripts.py tests/test_server_updater_phases.py
git diff --check
git add deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh \
  scripts/server_git_update.sh \
  scripts/server_git_update.ps1 \
  tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py
git commit -m "feat: run automatic two-phase deployment gate"
```

## Task 8: Enforce the Gate-Only Boundary and Update Runbooks

**Files:**

- Create: `tests/test_simple_deployment_gate_boundary.py`
- Modify: `docs/server-deployment.md`
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`

### Step 1: Write the RED branch-boundary test

Against exact production commit `2274d90`, assert:

- the only changed runtime files are deployment gate modules;
- the only other changed files are updater scripts, tests, and documentation;
- production and candidate writer fingerprints are identical;
- no MiMo v2 runtime, prompt, migration, schema, replay, or activation file
  changed;
- no terminal-entry cleanup or other execution-writer file changed;
- no Batch number, production row ID, current count, or current timestamp is
  encoded as a gate exception;
- no `CHANGE_CLASS`, age cutoff, heartbeat cutoff, or manual BLOCK override
  remains in the updater/gate interface.

The test must fail if a future commit widens this candidate unnoticed.

### Step 2: Replace the old operator contract in documentation

Document one automatic flow and one decision table. Remove statements that
historical age is safe, that the operator chooses a class, or that bootstrap
installs candidate code before Phase B. Explain that queued work is:

- WARN when writer fingerprint is unchanged;
- BLOCK when writer fingerprint changes.

Document all four return codes, schema automation, Phase A/Phase B, rollback,
and the separate approvals for push, shadow, and deployment. Record that the
gate-only candidate leaves dormant MiMo v2 and MiMo v1 authority unchanged.

### Step 3: Verify and commit

```bash
pytest -q tests/test_simple_deployment_gate_boundary.py tests/test_server_update_scripts.py
rg -n "CHANGE_CLASS|ChangeClass|historical.*WARN|age cutoff" \
  deploy scripts docs/server-deployment.md docs/runbook.md docs/migration-handoff.md
git diff --check
git add tests/test_simple_deployment_gate_boundary.py \
  docs/server-deployment.md docs/runbook.md docs/migration-handoff.md
git commit -m "docs: adopt simple deployment gate runbook"
```

Review every `rg` result; expected results are historical explanation only, not
an active operator instruction.

## Task 9: Complete Local Verification and Independent Review

**Files:** No intended production changes.

### Step 1: Run the focused safety suite

```bash
pytest -q \
  tests/test_deployment_work_evidence.py \
  tests/test_deployment_writer_surface.py \
  tests/test_deployment_preflight.py \
  tests/test_deployment_preflight_cli.py \
  tests/test_server_update_scripts.py \
  tests/test_server_updater_phases.py \
  tests/test_simple_deployment_gate_boundary.py \
  tests/test_instruction_execution_reconciliation.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_source_message_deletion.py \
  tests/test_mimo_recognition_v1.py \
  tests/test_mimo_recognition_config.py
```

If an exact MiMo v1 filename differs, select the existing v1/config test files
with `rg --files tests | rg 'mimo.*(v1|config)'` and record the command used.

### Step 2: Run static and full checks

```bash
python -m compileall -q src tests
bash -n deploy/telegram-kol-update scripts/bootstrap_server_updater.sh scripts/server_git_update.sh
git diff --check
pytest -q
git status --short
```

### Step 3: Request independent Critical/Important review

Use the `requesting-code-review` workflow against production `2274d90..HEAD`.
The reviewer must inspect, not merely count tests:

- false-PASS constructions for active and unknown exchange outcomes;
- false-BLOCK constructions for the audited inactive shapes;
- completeness of the flat writer manifest and mutation call-site test;
- semantic artifact recomputation and direct Phase A/B binding;
- updater pre-authorization mutation and rollback windows;
- gate-only and MiMo v1 boundaries.

Any Critical or Important finding requires a new RED test, minimal GREEN fix,
focused rerun, and another independent review. Do not weaken or skip a test.

### Step 4: Stop for push approval

Record the exact reviewed SHA, clean status, focused/full results, and reviewer
verdict. Do not push until the user explicitly approves that SHA.

## Task 10: Run Server Shadow Only After Explicit Push Approval

### Step 1: Push the exact reviewed commit

Push only `codex/deployment-gate-simplification`, then verify local and remote
SHA equality. Do not update production.

### Step 2: Stage a detached server candidate

Create a mode-0700 directory under `/opt/telegram-kol-candidates/` at the exact
reviewed SHA. Prove its worktree is clean. Use the production virtual
environment with candidate `PYTHONPATH`; do not install it.

### Step 3: Run focused server tests and read-only shadow

Run the Task 9 focused gate/updater tests. Then collect Phase A against the
production database with SQLite `mode=ro` and `query_only=ON`.

Acceptance is:

```text
writer_changed = false
active_write = 0
unknown_outcome = 0
invalid_evidence = 0
queued_work >= 0
decision = PASS or WARN
database writes = 0
notifications = 0
exchange mutation calls = 0
```

Before and after, verify production SHA, tracked tree, service state, MiMo v1
authority, settings, database file/watermarks, and notification/exchange-call
counters are unchanged. A transient Web timeout is reported but is not turned
into a gate blocker unless it prevents the required proof.

### Step 4: Stop for deployment approval

Shadow success authorizes only a separate deployment decision. Do not stop,
restart, checkout, install, or enable anything in production.

## Task 11: Deploy Only After a Separate Explicit Approval

Run the reviewed bootstrap/updater at the exact shadowed SHA with no manual
change class. Capture Phase A and Phase B artifacts and their direct binding.
If either phase is BLOCK or invalid, let rollback restore the existing service
and stop.

After a successful update, verify:

```text
production SHA = exact approved SHA
tracked worktree = clean
telegram-kol.service = active
application health = restored
MiMo v1 = authoritative
no MiMo v2 activation or replay
no database history edit
no notification or exchange test write
```

Retain mode-0600 sanitized artifacts according to the runbook. Do not deploy
the separate terminal-entry writer candidate in the same operation.

---

## Completion Definition

The implementation is complete only when:

1. the gate has exactly the approved automatic blockers and no manual class;
2. the gate-only diff has an unchanged writer fingerprint from production;
3. all RED/GREEN, focused, full-suite, shell, compile, and diff checks pass;
4. independent review reports zero Critical and zero Important findings;
5. a separately approved server shadow produces no writes, notifications, or
   exchange calls;
6. deployment, if separately approved, preserves MiMo v1 authority and does
   not include any other writer change.
