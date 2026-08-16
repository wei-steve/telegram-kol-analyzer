# Bound-Close and Batch 119 Joint Read-Only Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Break the recovery-order deadlock by proving the exact 29 bound-close reservations and exact Batch 119 false-submission incident together in one stopped-service read-only window, while keeping their apply windows and the ordinary deployment gate separate.

**Architecture:** Add a recovery-only joint material-authority classifier that tolerates only the audited Batch 119 retry-heartbeat timestamps while fingerprinting every business-authority field. Use it for two live admission snapshots, one post-stop recheck, four fresh exchange captures, and the bound-close apply pre/post checks. Reuse the existing bound-close and Batch 119 capture/classifier/apply authorities; do not modify ordinary deployment-preflight policy or create a new database schema.

**Tech Stack:** Python 3.12, SQLite query-only transactions, SQLAlchemy 2, Typer, pytest, Bash/systemd runbook, existing Deepcoin read-only clients and recovery comparators.

---

## Execution Rules

- Work only in `/Users/steven/Documents/telegram获取消息-deployment-gate-recovery-plan` on `codex/bound-close-reservation-recovery`.
- Read `AGENTS.md` before every production operation.
- Follow `docs/plans/2026-08-16-bound-close-batch119-joint-read-only-recovery-design.md` exactly.
- Use `@test-driven-development` for every production-code or runbook behavior change.
- Use `@requesting-code-review` after each task and for the complete range.
- Do not modify the ordinary semantics in
  `src/telegram_kol_research/deployment_preflight.py`.
- Do not add a migration, bootstrap production, hand-edit SQLite, replay a
  message, construct an exchange writer, send a notification, enable MiMo v2,
  push, deploy, stop production, capture production exchange evidence, or apply
  either recovery without the later explicit approval boundary.
- Keep raw database IDs, reservation refs, position/order/message IDs, provider
  rows, errors, timestamps, credentials, and capabilities out of operator
  projections and committed fixtures.
- Commit each task separately. Do not squash or rebase reviewed commits.

### Task 1: Extract the writer-quiescence engine without changing policy

**Files:**
- Create: `src/telegram_kol_research/bound_close_writer_quiescence.py`
- Modify: `scripts/check_bound_close_reservation_writer_quiescence.py`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Test: `tests/test_bound_close_writer_quiescence.py`

**Step 1: Write the failing library-contract tests**

Move the current helper fixtures into a new focused test module and add tests
that import a library function rather than execute the script:

```python
def test_writer_quiescence_library_matches_cli_projection(production_db):
    result = inspect_bound_close_writer_quiescence(production_db, now=NOW)
    assert result == run_helper(production_db, now=NOW).payload


def test_writer_quiescence_library_keeps_exact_closed_table_contract():
    assert set(WORK_TABLE_CONTRACT) == {
        spec.table for spec in deployment_preflight._WORK_SPECS
    }
```

Retain the existing matrices for every table's safe, active, unknown, NULL,
future, historical, missing-prior-schema, row-limit, timestamp, and target state.

**Step 2: Run the tests and observe RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'writer_quiescence'
```

Expected: collection fails because
`telegram_kol_research.bound_close_writer_quiescence` does not exist.

**Step 3: Extract the implementation mechanically**

Move the closed constants, timestamp validation, bounded row inspection,
known-prior-schema checks, and `inspect_writer_quiescence()` implementation from
the script into the new library. Rename the public entry point to:

```python
def inspect_bound_close_writer_quiescence(
    database_path: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]: ...
```

The library must continue to open `mode=ro`, enable and verify
`PRAGMA query_only=ON`, issue explicit `BEGIN`, use `LIMIT 10001`, and return the
same exact aggregate schema. The script becomes a thin argument/error/JSON/exit
wrapper. Do not change state sets, time columns, cutoff, counts, or readiness.

**Step 4: Run GREEN and adjacency checks**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_deployment_preflight.py
.venv/bin/python -m compileall -q \
  src/telegram_kol_research/bound_close_writer_quiescence.py \
  scripts/check_bound_close_reservation_writer_quiescence.py \
  tests/test_bound_close_writer_quiescence.py
git diff --check
```

Expected: all pass and the real helper output remains byte-for-byte compatible.

**Step 5: Review and commit**

Review specifically for policy drift, missing tables/states, writable SQLite
paths, and error-detail leakage. Resolve every Critical/Important finding.

```bash
git add \
  src/telegram_kol_research/bound_close_writer_quiescence.py \
  scripts/check_bound_close_reservation_writer_quiescence.py \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "refactor: share bound close writer quiescence"
```

### Task 2: Add exact Batch 119 material authority

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Create: `src/telegram_kol_research/bound_close_batch119_joint_recovery.py`
- Test: `tests/test_bound_close_batch119_joint_recovery.py`
- Test: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write failing pure-authority tests**

Build a production-shaped Batch 119 false-submission fixture using the existing
test seed. Add tests for a frozen result such as:

```python
@dataclass(frozen=True)
class JointRecoveryMaterialAuthority:
    material_fingerprint: str
    reservation_count: int
    batch119_incident_count: int
    blocking_writer_count: int
    status: Literal["ready", "refused"]
    reason_code: str | None
```

Required RED cases:

- exact 29-reservation plus exact Batch 119 source is ready;
- changing only batch/leg retry-heartbeat `updated_at` preserves the material
  fingerprint;
- changing status, reason, mode, action, topology, request, response,
  client/exchange ID, snapshot, durable error, component evidence/attempt,
  mutation, binding, lifecycle, entry, ownership, deadline, progress,
  completion, or any non-audited timestamp changes the fingerprint or refuses;
- a different batch, second leg, changed reservation population/status,
  additional writer, NULL/future state, missing schema, or excessive rows
  refuses;
- malformed JSON/time/decimal and unknown fields refuse with a closed reason;
- the public result and every exception contain no raw identifiers or evidence.

**Step 2: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py \
  -k 'joint_recovery or material_authority'
```

Expected: import/API failures.

**Step 3: Extract one authoritative local Batch 119 payload builder**

In `composite_management_batch_recovery.py`, extract the local durable portion
of the existing Batch 119 planner into a private in-session function. Both the
normal planner and joint recovery must call this same function. Do not duplicate
the allowlist, false-submission topology, legacy-error check, identity checks,
or additional-active-work check.

The extracted payload retains the existing apply `source_fingerprint`
semantics. Add a separate recovery-admission normalization that removes only the
two explicitly audited retry-heartbeat `updated_at` values. It must not alter or
replace the apply source/evidence fingerprint.

**Step 4: Implement one coherent joint loader**

In the new module, load the writer facts, 29-reservation source population, and
Batch 119 local payload in one read-only SQLAlchemy/SQLite transaction. Use the
same closed table/state contract extracted in Task 1. Return only the frozen
aggregate authority and a private bounded canonical material payload. No raw
payload is serializable through the public API.

Support only closed phases:

```python
JOINT_DIAGNOSTIC = "joint_diagnostic"
BOUND_APPLY_PRE = "bound_apply_pre"
BOUND_APPLY_POST = "bound_apply_post"
```

- `joint_diagnostic` and `bound_apply_pre`: exact 29 active target reservations
  plus exact Batch 119 incident;
- `bound_apply_post`: all 29 target reservations confirmed and exact Batch 119
  incident unchanged.

No caller-provided batch ID, count, allowlist, ignored table, or age override is
accepted.

**Step 5: Run GREEN and full affected tests**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_bound_close_reservation_recovery.py
.venv/bin/python -m compileall -q src tests
git diff --check
```

**Step 6: Review and commit**

Review fingerprint exclusions field-by-field. Require a negative test for every
excluded/non-excluded timestamp and every raw-authority field.

```bash
git add \
  src/telegram_kol_research/composite_management_batch_recovery.py \
  src/telegram_kol_research/bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py
git commit -m "feat: classify joint recovery material authority"
```

### Task 3: Add strict joint-admission CLI and comparator

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Create: `scripts/compare_bound_close_batch119_joint_admissions.py`
- Create: `scripts/project_bound_close_batch119_joint_output.py`
- Modify: `tests/test_cli_smoke.py`
- Test: `tests/test_bound_close_batch119_joint_recovery.py`

**Step 1: Write failing CLI/parser tests**

Add a dormant command:

```text
inspect-bound-close-batch119-joint-recovery
  --database-path EXISTING_DB
  --phase joint_diagnostic|bound_apply_pre|bound_apply_post
```

Test exact-only arguments, existing regular non-symlink DB, mode=ro/query-only,
closed phase enum, canonical single-line JSON, exit `0` only for ready and `2`
for refusal, and fixed redaction for configuration/read failures.

Private admission documents may contain exact schema version, capture ID,
capture start/completion, phase, material fingerprint, counts, and status. They
must not contain raw evidence. The comparator must reject duplicate keys,
unknown/missing fields, bool-as-int, oversized bytes/tree/strings, malformed or
non-UTC times, repeated capture identity, non-increasing capture windows,
different phases, material drift, count drift, or any refused/error document.

The operator projector must output only:

```json
{"batch119_incident_count":1,"blocking_writer_count":0,"reservation_count":29,"schema_version":1,"status":"ready"}
```

and fixed `{"status":"refused"}` on every invalid input.

**Step 2: Run RED**

```bash
.venv/bin/pytest -q tests/test_cli_smoke.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  -k 'joint_recovery'
```

**Step 3: Implement the thin CLI, comparator, and projector**

The CLI calls only the Task 2 read-only loader. It has no apply, force, ignore,
row-selection, age, notify, exchange-client, or writer option. The comparator
and projector use strict JSON loaders with the same byte/tree/type discipline as
the existing bound-close and Batch 119 comparators.

**Step 4: Run GREEN**

```bash
.venv/bin/pytest -q tests/test_cli_smoke.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_batch119_dry_run_comparison.py
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```

**Step 5: Review and commit**

```bash
git add src/telegram_kol_research/cli.py \
  scripts/compare_bound_close_batch119_joint_admissions.py \
  scripts/project_bound_close_batch119_joint_output.py \
  tests/test_cli_smoke.py \
  tests/test_bound_close_batch119_joint_recovery.py
git commit -m "feat: add dormant joint recovery admission cli"
```

### Task 4: Implement the joint stopped-service read-only window

**Files:**
- Modify: `docs/runbook.md`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `tests/test_batch119_dry_run_comparison.py`

**Step 1: Write failing extracted-Bash and reachability tests**

Add tests proving the old two-token bound-close diagnostic cannot invoke the new
joint window. Require the exact new token:

```text
I_APPROVE_BOUND_CLOSE_BATCH119_ALL_DB_UNITS_STOPPED_JOINT_READ_ONLY_CAPTURE
```

and the existing exact legacy-monitor reset token. The approval block must state
that it authorizes no apply, exchange write, deployment, or MiMo activation.

Add real shell/stub tests for:

- two live joint admissions separated by the bounded poll interval;
- material/count drift before stop;
- unit/install/runtime, dynamic inventory, process, SHA, and DB identity drift;
- exact timer-first freeze and legacy-monitor convergence ordering;
- one post-stop joint admission with the same material fingerprint;
- four commands in exact order: Batch119-1, bound-close-1, Batch119-2,
  bound-close-2;
- four distinct CLI processes/readers and private 0600 outputs;
- first/second/third capture refusal or timeout makes later captures unreachable;
- both existing semantic comparators must return exact stable;
- no apply command or notification is reachable;
- all exit/signal/timeout paths restore original units and remove artifacts.

**Step 2: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_batch119_dry_run_comparison.py \
  -k 'joint or stopped_service or deadline'
```

**Step 3: Implement one shared stopped-phase deadline**

Update the runbook to create the deadline before any stop and place timer stop,
verification, monitor convergence, service/socket stop, process/identity checks,
post-stop admission, all four captures, both comparisons, final identity checks,
and cleanup under that same deadline.

Use direct process-group `KILL` at the absolute deadline. Before Batch119-1,
require at least:

```text
2 * 180 seconds bound-close capture budget
+ 2 * 120 seconds Batch119 outer capture budget
+ 60 seconds local projection/comparison/cleanup reserve
= 660 seconds
```

If the remaining shared budget is below 660 seconds, restore and refuse without
starting a capture. Each capture normally returns immediately when complete; the
limits are hard maxima, not fixed sleeps.

Use fresh database copies only where the existing Batch 119 dry-run requires
them. Never bootstrap or write production. All output documents remain in the
0700 recovery directory and are removed by the trap.

**Step 4: Run GREEN and Bash checks**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_cli_smoke.py
# Extract the complete joint recovery Bash block and syntax-check it.
bash -n /tmp/extracted-joint-recovery-window.sh
git diff --check
```

The test must create the extracted file with pytest's temporary directory; do
not add generated shell files to the repository.

**Step 5: Review and commit**

```bash
git add docs/runbook.md \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_batch119_dry_run_comparison.py
git commit -m "docs: add joint stopped recovery capture"
```

### Task 5: Close the bound-close apply side of the dependency

**Files:**
- Modify: `docs/runbook.md`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `tests/test_bound_close_batch119_joint_recovery.py`
- Test: `tests/test_bound_close_reservation_recovery.py`

**Step 1: Write failing apply-boundary tests**

Using production-shaped fixtures, prove the existing bound-close apply window is
blocked by exact Batch 119 before this change. Then require:

- a fresh `bound_apply_pre` joint admission after all units stop;
- a fresh bound-close reader/capture and existing exact apply authorization;
- exact reservation-only database writes and audit;
- a `bound_apply_post` joint admission before success;
- all 29 reservations confirmed;
- identical Batch 119 material fingerprint before and after;
- no Batch 119 database write or exchange writer;
- no reuse of the joint diagnostic permit, documents, capture IDs, or material
  authority object.

Add counterexamples for Batch 119 drift before apply, during the transaction
seam, after apply, and during post-commit verification. Any drift must refuse or
report a closed unresolved outcome without a blind retry.

**Step 2: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'joint or batch119_unchanged or bound_apply'
```

**Step 3: Update only the recovery runbook/wiring needed**

Use the joint CLI's closed `bound_apply_pre` and `bound_apply_post` phases around
the existing bound-close apply command. Do not modify the apply SQL allowlist or
expand its mutation authority. Keep backup, transaction deadline, trigger and
authorizer defense, post-commit read-only verification, and redacted result
projection unchanged.

If the current apply implementation needs a private pre/post material
fingerprint parameter, add only an exact typed capability issued by the Task 2
loader; never accept a caller-provided JSON payload or fingerprint as authority.

**Step 4: Run GREEN and adjacency checks**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_composite_management_batch_recovery.py
git diff --check
```

**Step 5: Review and commit**

```bash
git add docs/runbook.md \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: preserve batch 119 across reservation apply"
```

### Task 6: Prove the Batch 119 apply remains second and isolated

**Files:**
- Modify: `docs/runbook.md`
- Modify: `tests/test_batch119_dry_run_comparison.py`
- Modify: `tests/test_composite_management_batch_recovery.py`
- Modify: `tests/test_deployment_preflight.py`

**Step 1: Write failing sequence and isolation tests**

Add tests proving Batch 119 apply refuses while any target reservation is not
confirmed. After bound-close convergence, require:

- a fresh Batch 119 source load and exchange capture;
- the existing Batch 119 apply tokens and stopped-service authority;
- no reuse of joint-diagnostic or bound-apply artifacts;
- no mutation to the confirmed reservation rows;
- exact Batch 119-only mutation/audit behavior;
- ordinary deployment preflight remains BLOCK after only the first recovery and
  removes both incident facts only after the second recovery.

**Step 2: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_deployment_preflight.py \
  -k 'bound_close or batch119 or joint_recovery'
```

**Step 3: Add exact runbook pre/post proofs**

Before Batch 119 capture/apply, query-only verify the approved reservation
population is confirmed and fingerprint it. After apply, reload the same rows
and require the identical confirmed fingerprint. Keep the existing Batch 119
recovery code, allowlist, exchange-writer rules, backup, authorization, and
postchecks unchanged unless a test proves a missing capability boundary.

Do not add an ordinary deployment-preflight exception. Production
`deployment_preflight.py` should normally have no diff; the test file changes
only to prove the existing gate transition.

**Step 4: Run GREEN**

```bash
.venv/bin/pytest -q \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_deployment_preflight.py \
  tests/test_bound_close_reservation_recovery.py
git diff --check
```

**Step 5: Review and commit**

```bash
git add docs/runbook.md \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_deployment_preflight.py
git commit -m "test: enforce joint recovery apply order"
```

### Task 7: Final review, full verification, and push boundary

**Files:**
- Review: complete design-to-HEAD range
- No new production files expected

**Step 1: Run focused security and recovery suites**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_batch119_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  tests/test_deepcoin_client.py \
  tests/test_deepcoin_snapshot_authority.py \
  tests/test_deployment_preflight.py
```

Expected: PASS with no network.

**Step 2: Run repository-wide verification**

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/pytest -q
git diff --check
git status --short --branch
```

Extract every modified production runbook Bash block and run `bash -n`. Prove
`src/telegram_kol_research/deployment_preflight.py` has no diff from the reviewed
base unless an independently reviewed correctness defect explicitly requires a
change; never change it merely to unblock recovery.

**Step 3: Independent Critical/Important review**

Use `@requesting-code-review` on the complete range. Review:

- exact Batch 119 incident binding and material-fingerprint exclusions;
- no alternative batch/reservation/population acceptance;
- one coherent read-only local snapshot;
- strict JSON/type/size/time bounds and redaction;
- fresh-reader and no-capability-reuse guarantees;
- shared absolute deadline and all cleanup paths;
- zero-write joint diagnostic;
- separate apply tokens, artifacts, backups, and mutations;
- no writer/notification/replay/bootstrap/deploy/MiMo reachability;
- ordinary gate unchanged and still fail-closed.

Resolve every Critical and Important finding under RED/GREEN and repeat Steps
1-3 until review reports zero Critical and zero Important.

**Step 4: Commit any review-only fixes and stop**

Confirm the worktree is clean and report the exact reviewed SHA, test counts,
review result, and production boundaries. Do not push automatically.

Stop and request exact push approval. After an approved push, stop again and
request the new joint diagnostic token plus the legacy-monitor reset token. Do
not infer either authorization from any previous failed or consumed window.

## Production Sequence After Local Completion

These are approval boundaries, not implementation steps authorized by this
plan:

1. push the exact reviewed recovery SHA;
2. obtain
   `I_APPROVE_BOUND_CLOSE_BATCH119_ALL_DB_UNITS_STOPPED_JOINT_READ_ONLY_CAPTURE`
   plus the exact legacy-monitor reset token;
3. run the joint stopped-service read-only window and stop;
4. obtain fresh bound-close apply authorization, apply reservations only, and
   stop;
5. obtain fresh Batch 119 stopped apply authorization, apply Batch 119 only,
   and stop;
6. gather fresh stable monitor evidence and run ordinary deployment preflight;
7. obtain ordinary deployment approval, deploy, and record the final production
   SHA as the later MiMo v2 baseline.
