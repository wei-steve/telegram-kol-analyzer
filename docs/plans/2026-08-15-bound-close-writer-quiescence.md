# Bound Close Writer Quiescence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the stopped-service reservation-recovery quiescence check distinguish fresh work from reviewed historical residue while preserving fail-closed handling of unknown states, malformed timestamps, durable Deepcoin operations, and every existing deployment gate.

**Architecture:** The dedicated aggregate helper will continue to mirror `deployment_preflight._WORK_SPECS` and read one query-only SQLite snapshot, but it will classify known active/unknown rows against the same ten-minute cutoff instead of treating every historical row as a live writer. The runbook will condition-poll that helper only after all reviewed services are stopped, with an absolute deadline and repeated unit/process/database checks before any Deepcoin capture becomes reachable. `deployment_preflight.py` is not modified.

**Tech Stack:** Python 3.12, SQLite, Bash/systemd runbook, pytest.

---

## Global constraints

- Work only on `codex/bound-close-reservation-recovery`.
- Use `@test-driven-development` for every behavior change.
- Do not modify `src/telegram_kol_research/deployment_preflight.py`.
- Do not add a database write, bootstrap, exchange write, notification, message
  replay, MiMo v2 path, force flag, ignore list, or age override.
- Keep the helper output aggregate-only and canonical; never emit a table name,
  state, timestamp, database id, path, provider row, or raw exception.
- Do not touch production, stop a service, run Deepcoin capture, deploy, or push
  until all local tasks and the final review are complete.
- The prior read-only production approval is consumed because the reviewed SHA
  will change. Stop after push and request the exact authorization again.

### Task 1: Add freshness-aware aggregate classification

**Files:**
- Modify: `scripts/check_bound_close_reservation_writer_quiescence.py:6-414`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py:425-864`

**Step 1: Extend the SQLite fixture with authoritative time columns**

Change `_writer_quiescence_database()` to create each work table with both the
state column and `spec.time_column`, using a fixed safe historical UTC value for
the target and any inserted safe rows. Derive the fixture from `_WORK_SPECS` so
that a future production time-column change fails the contract test.

Use a deterministic test clock:

```python
CHECKED_AT = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
CUTOFF = CHECKED_AT - timedelta(minutes=10)
```

Call the helper's Python function with `now=CHECKED_AT` for boundary tests; keep
the subprocess tests for canonical CLI output.

**Step 2: Write failing cutoff and fail-closed tests**

Add parametrized tests proving:

```python
@pytest.mark.parametrize(
    ("updated_at", "expected_status", "fresh", "historical"),
    [
        (CUTOFF - timedelta(microseconds=1), "ready", 0, 1),
        (CUTOFF, "refused", 1, 0),
        (CUTOFF + timedelta(microseconds=1), "refused", 1, 0),
    ],
)
def test_known_active_state_uses_exact_preflight_cutoff(...): ...
```

Cover one ordinary active state and one `unknown_states` value. Add separate
tests for:

- a future timestamp that is counted as fresh;
- NULL, non-text, malformed, and non-UTC-offset timestamp representations;
- an unrecognized future state both before and after the cutoff;
- a Deepcoin nonterminal/NULL/future state both before and after the cutoff;
- more than the bounded per-table inspection limit;
- exact integer counts (reject booleans/floats through internal result
  validation if such a seam exists).

Expected initial result: the old helper either refuses historical known states,
lacks time columns, or returns the old output schema.

**Step 3: Run the new tests to verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'writer_quiescence and (cutoff or timestamp or unrecognized or deepcoin or bounded)'
```

Expected: FAIL for the new freshness/output assertions.

**Step 4: Implement strict UTC parsing and bounded row classification**

In the helper, add closed constants:

```python
_ACTIVE_WINDOW = timedelta(minutes=10)
_MAX_INSPECTED_ROWS_PER_TABLE = 10_000
_OUTPUT_FIELDS = frozenset({
    "block_regardless_of_age_writer_count",
    "blocking_writer_count",
    "checked_table_count",
    "fresh_active_or_unknown_writer_count",
    "historical_active_or_unknown_residue_count",
    "missing_table_count",
    "schema_version",
    "status",
    "target_reservation_count",
    "unrecognized_or_null_state_count",
})
```

Add a private parser that accepts only a nonempty string in either the
repository's canonical naive SQLite datetime form (interpreted as UTC) or an
explicit UTC form. Reject nonzero offsets and malformed values. Normalize the
result to aware UTC but do not reject a future value: it remains fresh and
blocking. Keep the public CLI clock internal (`datetime.now(timezone.utc)`), but
allow `inspect_writer_quiescence(..., now=...)` for deterministic tests.

For each non-target, non-Deepcoin table, query only rows whose state is not in
the safe set with `LIMIT _MAX_INSPECTED_ROWS_PER_TABLE + 1`. Classify each row:

```python
if state is None or type(state) is not str or state not in known_work_states:
    unrecognized_or_null += 1
elif parsed_time >= cutoff:
    fresh += 1
else:
    historical += 1
```

`known_work_states` is the exact union of the spec's `active_states` and
`unknown_states`. Overflow and invalid timestamps raise a fixed
`_WriterQuiescenceError`; they must not produce a partial `ready` result.

For `deepcoin_execution_operations`, count every state outside the exact three
safe terminal states into `block_regardless_of_age_writer_count`, without any
age downgrade. For the target table, exclude only `_TARGET_STATES`, accept only
`confirmed` as safe, and count every other/NULL value as unrecognized.

Compute:

```python
blocking = fresh + unrecognized_or_null + block_regardless_of_age
status = "ready" if 0 < target_count <= 64 and blocking == 0 else "refused"
```

Return exactly `_OUTPUT_FIELDS` and keep `main()` exit codes `0=ready`,
`2=refused`, `1=error`.

**Step 5: Add the reviewed safe states and production-shaped regression**

Extend only these closed sets:

```python
"strategy_management_legs": {..., "restored"}
"position_backup_stop_orders": {..., "missing"}
"source_message_deletion_exits": {..., "unbound"}
```

Update the exhaustive safe-state matrix. Add one production-shaped aggregate
fixture with 513 historical known-work rows, 93 safe rows split 2/16/75, two
fresh rows, and 29 target rows. Assert it reports:

```python
{
    "fresh_active_or_unknown_writer_count": 2,
    "historical_active_or_unknown_residue_count": 513,
    "target_reservation_count": 29,
    "unrecognized_or_null_state_count": 0,
    "block_regardless_of_age_writer_count": 0,
    "blocking_writer_count": 2,
    "status": "refused",
}
```

Move the two fresh timestamps strictly before the cutoff and assert `ready`,
historical becomes 515, and no other count changes.

**Step 6: Run the complete helper suite and verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py
```

Expected: all tests pass, including existing schema, source-table, redaction,
target, and state-contract tests.

**Step 7: Commit Task 1**

```bash
git add \
  scripts/check_bound_close_reservation_writer_quiescence.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: distinguish historical recovery writer residue"
```

### Task 2: Make the stopped window condition-poll safely

**Files:**
- Modify: `docs/runbook.md:1489-1830`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py:260-424`

**Step 1: Write failing runbook structure tests**

Extract the read-only window Bash block as the existing tests do. Add assertions
that the block contains:

- a 12-minute absolute deadline computed once after all units are stopped;
- a short bounded polling interval;
- helper status capture without `set -e` skipping cleanup;
- `verify_bound_close_quiescence`, process absence, production SHA, and database
  identity checks immediately before and after every helper invocation;
- a loop that continues only for `refused`, exits on helper `error`, and reaches
  Deepcoin CLI only after an exact `ready` projection;
- no fixed unconditional ten-minute sleep;
- final aggregate output permissions `0600`;
- trap restoration on timeout, signal, malformed JSON, changing inventory, and
  helper failure.

Add a shell simulation test with stubbed helper outputs:

```text
refused (2 fresh) -> refused (1 fresh) -> ready -> capture
```

and separate simulations for timeout and helper error. Assert capture is called
exactly once only in the ready case and never in the other cases; assert restore
runs exactly once in all cases.

**Step 2: Run the runbook tests to verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'runbook and (quiescence or poll or timeout or restore)'
```

Expected: FAIL because the current runbook invokes the helper once and exits on
its refused code.

**Step 3: Implement the bounded polling block**

After the existing three unit-quiescence verification passes, define:

```bash
QUIESCENCE_DEADLINE_EPOCH="$(( $(date -u +%s) + 720 ))"
QUIESCENCE_POLL_SECONDS=15
```

Create a function that rechecks dynamic DB-stage inventory, all unit states,
`pgrep`, production SHA, and an initial `(device,inode,resolved path)` database
identity captured before stopping. Invoke the helper into a private temporary
file under `set +e`, record its exit status, restore `set -e`, chmod it, and
strictly parse only the documented aggregate schema using candidate Python.

Loop behavior:

```bash
while :; do
  verify_all_local_quiescence_and_identity
  run_aggregate_helper_without_errexit
  verify_all_local_quiescence_and_identity
  case "$HELPER_STATUS" in
    0) require_exact_ready_projection; break ;;
    2) require_exact_refused_projection ;;
    *) exit 1 ;;
  esac
  [ "$(date -u +%s)" -lt "$QUIESCENCE_DEADLINE_EPOCH" ] || exit 1
  sleep "$QUIESCENCE_POLL_SECONDS"
done
```

Do not duplicate the production classifier in Bash. The strict projection must
validate exact integer types, nonnegative bounded counts, exact field names,
`blocking_writer_count` arithmetic, and status/exit-code consistency. Only then
may the existing two-capture loop run.

Update the prose to explain that waiting does not declare exchange work
terminal: it merely lets known local writer markers cross the official
historical cutoff after all writer processes have been proven stopped. Target
reservations still require two fresh exchange captures.

**Step 4: Verify GREEN and shell syntax**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py
python3 -m compileall -q scripts tests
```

Extract the read-only window's Bash block with the existing test helper and run
`bash -n` against it. Expected: all tests and syntax checks pass.

**Step 5: Commit Task 2**

```bash
git add docs/runbook.md tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "docs: poll reservation writer quiescence safely"
```

### Task 3: Regress the deployment boundary

**Files:**
- Modify only if a missing regression is found:
  `tests/test_deployment_preflight.py`
- Test: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Test: `tests/test_deployment_preflight.py`

**Step 1: Add a deployment-preflight non-regression test if absent**

Prove the unchanged deployment preflight still:

- blocks fresh known work at and after the cutoff;
- reports old known active/unknown work as historical warning evidence;
- blocks every nonterminal/unknown `deepcoin_execution_operations` row
  regardless of age; and
- continues to report the five target reservation states as work until the
  dedicated recovery converges them.

The test must call the real `collect_deployment_preflight_facts()` and
`build_deployment_preflight_artifact()`; it must not restate expected logic in a
fake collector.

**Step 2: Run the focused gate regression**

```bash
.venv/bin/pytest -q \
  tests/test_deployment_preflight.py \
  tests/test_bound_close_reservation_dry_run_comparison.py
```

Expected: all tests pass and `src/telegram_kol_research/deployment_preflight.py`
has no diff.

**Step 3: Commit only if a test was added**

```bash
git add tests/test_deployment_preflight.py
git commit -m "test: preserve deployment gate during quiescence wait"
```

### Task 4: Full verification, independent review, and push

**Files:**
- Review all commits from `8fa493372a76bdb4945c3f5ec2bbc90264153de6..HEAD`
- Update no file unless review identifies a defect.

**Step 1: Run static and focused checks**

```bash
git diff --check 8fa493372a76bdb4945c3f5ec2bbc90264153de6..HEAD
python3 -m compileall -q src scripts tests
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_deployment_preflight.py \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_cli_smoke.py
```

Expected: all pass.

**Step 2: Run the full local suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with only already documented skips/warnings.

**Step 3: Use `@requesting-code-review`**

Request an independent review focused on:

- cutoff equality and future/malformed times;
- arbitrary unknown/NULL states never aging into safety;
- all-age Deepcoin blocking;
- exact target-only exclusion;
- bounded queries and aggregate redaction;
- polling timeout, process/unit/database race checks, and trap restoration;
- capture unreachability before exact readiness; and
- proof that deployment preflight is unchanged.

Fix every Critical or Important finding with RED-to-GREEN tests and repeat the
review until it reports 0 Critical / 0 Important.

**Step 4: Verify the final commit and clean worktree**

```bash
git status --short
git rev-parse HEAD
git log --oneline --decorate -5
```

Expected: clean worktree and one exact reviewed 40-character SHA.

**Step 5: Push the reviewed branch**

```bash
git push origin codex/bound-close-reservation-recovery
```

Verify the remote ref equals the reviewed local SHA. Do not deploy or run the
production helper in this task.

**Step 6: Stop at the production approval boundary**

Report the local/full-test and review results, the new reviewed SHA, and the
diagnostic conclusion in plain language. Explicitly state that the earlier
read-only authorization was consumed by the old SHA and request this exact
token again before any service stop or production capture:

```text
I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_READ_ONLY_DOUBLE_CAPTURE
```

Do not cross this boundary automatically.
