# Bound Close Capture Diagnostic Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make a refused stopped-service bound-close exchange capture emit one strictly redacted classification/reason summary after service restoration, without changing any safety decision or persisting raw evidence.

**Architecture:** Extend the existing strict output projector with a diagnostic-only mode that parses the same private capture and aggregates only closed classifications and reasons. Update the runbook to capture CLI exit status explicitly, gate the second capture on a valid ready result, and hold one validated bounded summary in shell memory until the EXIT restoration path finishes.

**Tech Stack:** Python 3.12, strict JSON parsing, Bash with `set -euo pipefail`, pytest, SQLite read-only recovery fixtures.

---

## Execution Rules

- Work only in `/Users/steven/Documents/telegram获取消息-deployment-gate-recovery-plan` on `codex/bound-close-reservation-recovery`.
- Start every behavior change with a focused failing test and observe the expected failure.
- Do not modify `deployment_preflight.py`, classifier policy, apply code, database schema, exchange client behavior, Batch 119, monitor activation, or MiMo v2.
- Do not push, deploy, stop production services, call production Deepcoin, or reuse an earlier approval token in this plan.
- Request independent review after the focused implementation and resolve every Critical/Important finding before the final full-suite checkpoint.

### Task 1: Add a closed capture-diagnostic projection

**Files:**
- Modify: `scripts/project_bound_close_reservation_recovery_output.py`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`

**Step 1: Write the failing valid-refusal projection test**

Add a fixture with one observation for every closed classification and several
repeated reason codes. Invoke the projector with `capture-diagnostic` and
require an exact object shaped like:

```python
assert payload == {
    "action_count": 0,
    "counts": {
        "active": 2,
        "proven_terminal": 1,
        "total": 5,
        "unknown": 2,
    },
    "database_writes": 0,
    "exchange_writes": 0,
    "history_replays": 0,
    "reason_counts": {
        "exact_close_and_position_terminal": 1,
        "exact_close_order_currently_pending": 2,
        "exchange_history_incomplete": 2,
    },
    "status": "refused",
}
```

Assert that the values conserve the observation population.

**Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'capture_diagnostic and valid_refusal'
```

Expected: FAIL because `capture-diagnostic` is not an accepted projector mode.

**Step 3: Implement the minimal diagnostic projection**

Import the closed reason sets and classification enum from the recovery module.
After `_parse_bound_close_reservation_dry_run_document(raw)` succeeds, aggregate
the parsed observations by exact `reason_code`. Return only the approved fields.
Do not deserialize the document a second time for semantic facts.

Use deterministic key ordering through the existing canonical `json.dumps`
call. Verify internally that:

```python
sum(reason_counts.values()) == len(parsed.plan.observations)
```

Build the classification counts from the same parsed observation tuple. Any
conservation mismatch raises `_ProjectionRefused`.

**Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Write failing redaction and malformed-input tests**

Add parameterized tests for:

- reservation, source and exchange fingerprints never appearing;
- confirmation token, capture identity and timestamps never appearing;
- duplicate JSON keys;
- unknown top-level fields;
- unknown classification or reason;
- exact-int/type violations;
- non-finite numbers, malformed UTF-8/JSON, empty input and oversized input.

Every invalid case must return exit 2 and exactly:

```text
{"status":"diagnostic_unavailable"}
```

No raw exception, input fragment, path or stderr is allowed.

**Step 6: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'capture_diagnostic'
```

Expected: at least one new security test FAILS before the invalid-output branch
is implemented.

**Step 7: Implement the fixed unavailable diagnostic**

Keep the existing `capture` and `apply-result` projections byte-for-byte
compatible. Only `capture-diagnostic` changes invalid output from the existing
generic refusal to the exact unavailable object. Catch only the existing closed
parser/projection exception set; never print exception text.

**Step 8: Run projector tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'projector or capture_diagnostic'
```

Expected: PASS.

**Step 9: Run static checks and commit Task 1**

Run:

```bash
.venv/bin/python -m py_compile \
  scripts/project_bound_close_reservation_recovery_output.py
git diff --check
```

Then commit only the two Task 1 files:

```bash
git add scripts/project_bound_close_reservation_recovery_output.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "feat: project bound close refusal diagnostics"
```

### Task 2: Make the runbook preserve only the safe diagnostic through cleanup

**Files:**
- Modify: `docs/runbook.md`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write the failing refused-first-capture shell test**

Extract the stopped-service Bash block with the existing test helper. Stub the
service/inventory/Git/database checks and the capture CLI so attempt one writes
a valid refused private document and returns exit 2. Stub the diagnostic
projector to emit the expected one-line safe summary.

Require the event order:

```text
capture-1
project-diagnostic-1
restore
print-diagnostic
```

Assert there is no `capture-2`, comparator, apply, notification, database write
or exchange write event, and that the temporary directory is absent afterward.

**Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'runbook and refused_capture and diagnostic'
```

Expected: FAIL because `set -e` exits before the projector and the EXIT handler
has no safe-summary handoff.

**Step 3: Implement bounded CLI-status capture and ready/refused matching**

In `run_bound_close_double_capture()`:

1. use `set +e` only around the exact CLI invocation;
2. record the exit status and immediately restore `set -e`;
3. chmod the private result 0600;
4. run `capture-diagnostic` on the same file;
5. read the already validated diagnostic into a bounded shell variable;
6. accept only exit 0 + diagnostic status ready, or exit 2 + diagnostic status
   refused; and
7. stop immediately after a refused result.

Do not inspect raw JSON with grep alone. Add a narrow Python or exact projector
validation step for the safe diagnostic schema and size before assigning it to
the shell variable.

**Step 4: Print only after restoration**

Initialize an empty `BOUND_CLOSE_SAFE_DIAGNOSTIC` before traps are installed.
In `finish_bound_close_reservation_window()`:

1. save the original status;
2. disable the EXIT trap;
3. run `restore_bound_close_reservation_units()`;
4. print the bounded safe diagnostic, if set, only after the restoration call;
5. preserve cleanup failure as nonzero; and
6. otherwise return the original nonzero refusal status.

Never print the private file or projector stderr.

**Step 5: Run the refused-path test and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 6: Write failing mismatch and failure-matrix tests**

Add parameterized shell tests for:

- exit 0 + refused document;
- exit 2 + ready document;
- exit 1, 3, 126, 127 and signal-style status;
- empty/malformed/oversized private output;
- projector exit 2;
- diagnostic schema mismatch;
- cleanup failure after a valid refused diagnostic.

All cases must skip capture two and comparator, run restoration, remain
nonzero, and expose at most the fixed unavailable diagnostic. A cleanup failure
must not be reported as a successful window.

**Step 7: Run the matrix and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'runbook and capture and (mismatch or malformed or cleanup)'
```

Expected: at least one matrix case FAILS until all status/schema combinations
are closed.

**Step 8: Implement the minimal fail-closed matrix**

Use exact shell case statements and the strict safe-diagnostic validator. Do
not add retry, sleep, persistence, per-reservation output, or a new approval
token.

**Step 9: Preserve the two-ready path**

Add or strengthen a test that two independent ready CLI results both run their
safe projection and then invoke the unchanged comparator exactly once. The
final comparator output must remain exactly `{"status":"stable"}` and the
window must restore services before returning success.

**Step 10: Run runbook and CLI tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py
```

Expected: PASS.

Extract the reviewed Bash block with the existing test helper and run:

```bash
bash -n /path/to/extracted-read-only-window.sh
```

Expected: PASS.

**Step 11: Update operator wording**

In the runbook, state explicitly that:

- the printed diagnostic appears only after the restoration attempt;
- it is counts-only and has no apply authority;
- a refused or unavailable diagnostic consumes the window approval and requires
  a new reviewed SHA/token before another production attempt; and
- raw capture files are deleted.

**Step 12: Run static checks and commit Task 2**

Run:

```bash
git diff --check
```

Then commit only the Task 2 files:

```bash
git add docs/runbook.md \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py
git commit -m "fix: report refused capture after restoration"
```

### Task 3: Adjacent regression, independent review and final local checkpoint

**Files:**
- Test only; production edits are allowed only for review findings proven by a
  new failing regression.

**Step 1: Run focused recovery and deployment-gate regression**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  tests/test_deployment_preflight.py
```

Expected: PASS. Confirm `src/telegram_kol_research/deployment_preflight.py` has
no diff.

**Step 2: Run security reachability checks**

Inspect the diff and tests to prove:

- no database write or bootstrap path was added;
- no exchange writer method is reachable;
- no notification or message replay path was added;
- no raw observation, identifier, token, fingerprint, timestamp, provider row,
  error, or path reaches final diagnostic stdout;
- the second capture remains unreachable after refusal; and
- diagnostic output grants no apply capability.

**Step 3: Request independent code review**

Use the requesting-code-review workflow against base `f808315` and the new
HEAD. Require explicit Critical/Important findings for projector closure,
shell status handling, restoration ordering, redaction and regression scope.

Do not continue until there are 0 Critical and 0 Important findings. Fix each
finding with a new RED-to-GREEN test and a narrow commit.

**Step 4: Run the complete local suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: PASS with only already documented environmental skips/warnings.

**Step 5: Run final hygiene checks**

Run:

```bash
.venv/bin/python -m compileall -q src tests scripts
git diff --check f808315..HEAD
git status --short
```

Expected: compile and diff checks pass; worktree is clean.

**Step 6: Stop before push or production**

Report:

- commit range;
- focused and full-suite results;
- independent review result;
- exact redacted diagnostic schema;
- confirmation that gate severity and production data paths did not change;
- confirmation that nothing was pushed, deployed or run against production.

Request separate approval before any push. A later production read-only window
requires the newly reviewed 40-character SHA and fresh exact tokens; prior
tokens are consumed and invalid.
