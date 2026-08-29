# Deepcoin Maintenance Read Pacing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Pace manual-cleanup exact Deepcoin reads below the documented endpoint limits without retrying unknown outcomes or changing reconciliation evidence.

**Architecture:** Add an invocation-local endpoint pacer inside `manual_pending_entry_reconciliation`. Inject monotonic time and sleep for deterministic tests, use separate fills/history keys, and forward the dependencies through the apply path's mandatory fresh re-plan.

**Tech Stack:** Python 3.12, `time.monotonic`, `time.sleep`, pytest, SQLAlchemy, Typer.

---

### Task 1: Prove the burst and fail-closed contracts

**Files:**
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

**Step 1: Add a deterministic clock**

Add a fake monotonic clock that records sleeps and exact-read start times:

```python
class MaintenanceReadClock:
    def __init__(self):
        self.current = 0.0
        self.sleeps = []

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds
```

Have the test client record `clock()` when exact fills and exact history methods
start.

**Step 2: Write the canonical pacing RED test**

Seed all seven canonical targets, build a healthy plan with the fake clock and
sleeper, and assert:

```python
assert plan.status == "ready"
assert all(
    later - earlier >= pytest.approx(0.41)
    for earlier, later in pairwise(client.exact_fill_started_at)
)
assert all(
    later - earlier >= pytest.approx(0.41)
    for earlier, later in pairwise(client.exact_history_started_at)
)
```

Use direct pair assertions if `pytest.approx` cannot be used with `>=`.

**Step 3: Run the pacing test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_manual_pending_entry_reconciliation.py::test_manual_reconciliation_paces_all_canonical_exact_reads
```

Expected: FAIL because `build_manual_pending_entry_reconciliation_plan` does
not accept the injected monotonic clock/sleeper and exact reads are unpaced.

**Step 4: Write the no-retry RED test**

Configure the sixth exact fills call to raise a representative exception. Assert
the plan is blocked with `target_fill_query_incomplete`, exactly six fills calls
occurred, the seventh target was not called, history was not called and no write
method ran.

**Step 5: Run the no-retry test and verify its baseline contract**

Run the single test. It may already pass for call counts; the pacing assertions
must remain RED and establish the missing production behavior before code is
changed.

**Step 6: Commit the RED tests**

```bash
git add -- tests/test_manual_pending_entry_reconciliation.py
git commit -m "test: reproduce Deepcoin maintenance read burst"
```

### Task 2: Add the minimal maintenance endpoint pacer

**Files:**
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Test: `tests/test_manual_pending_entry_reconciliation.py`

**Step 1: Add timing dependencies and constants**

Import `time`, define a private `0.41` second exact-read interval and add
optional `read_monotonic`/`read_sleep` callables to the plan and apply functions.
Defaults must be resolved to `time.monotonic` and `time.sleep` inside the
function, not at import-time default arguments.

**Step 2: Implement the private pacer**

```python
class _MaintenanceExactReadPacer:
    def __init__(self, *, monotonic, sleep, interval_seconds=0.41):
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval_seconds = interval_seconds
        self._last_started_at = {}

    def wait(self, endpoint):
        now = self._monotonic()
        previous = self._last_started_at.get(endpoint)
        remaining = (
            self._interval_seconds
            if previous is None
            else self._interval_seconds - (now - previous)
        )
        if remaining > 0:
            self._sleep(remaining)
            now = self._monotonic()
        self._last_started_at[endpoint] = now
```

Do not add retry or persistent state.

**Step 3: Pace exact fills and history independently**

Create one pacer per plan. Call `wait("fills")` immediately before each exact
fills request and `wait("trigger_history")` immediately before each exact
trigger-history request. Pass the pacer into the two private evidence helpers.

**Step 4: Forward pacing dependencies through apply**

Pass the optional clock and sleeper into the fresh plan constructed by
`apply_manual_pending_entry_reconciliation`. Do not change CLI arguments,
fingerprints or database logic.

**Step 5: Run RED tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_manual_pending_entry_reconciliation.py::test_manual_reconciliation_paces_all_canonical_exact_reads \
  tests/test_manual_pending_entry_reconciliation.py::test_manual_reconciliation_does_not_retry_failed_exact_fill
```

Expected: both PASS.

**Step 6: Run the affected module**

```bash
.venv/bin/pytest -q tests/test_manual_pending_entry_reconciliation.py
```

Expected: PASS.

**Step 7: Commit the implementation**

```bash
git add -- \
  src/telegram_kol_research/manual_pending_entry_reconciliation.py \
  tests/test_manual_pending_entry_reconciliation.py
git commit -m "fix: pace Deepcoin maintenance evidence reads"
```

### Task 3: Verify integration and regression boundaries

**Files:**
- Test: `tests/test_deepcoin_client.py`
- Test: `tests/test_deepcoin_maintenance_evidence.py`
- Test: `tests/test_manual_pending_entry_reconciliation.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Run focused affected tests**

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_client.py \
  tests/test_deepcoin_maintenance_evidence.py \
  tests/test_manual_pending_entry_reconciliation.py \
  tests/test_cli_smoke.py
```

Expected: PASS with only documented existing skips/warnings.

**Step 2: Run one final full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS. If production code changes afterward, rerun affected tests and
one final full suite on the new candidate.

**Step 3: Run static checks**

```bash
git diff --check 98120385974870420c2be0abb3f297df3e8855ff HEAD
```

Expected: no output and exit 0.

### Task 4: Review and record the local repair

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Review the exact base-to-HEAD diff**

Review from base `98120385974870420c2be0abb3f297df3e8855ff` for correctness,
scope, no-retry semantics, test quality and accidental production/activation
changes. Fix every P0/P1 finding before proceeding. Because repository policy
does not authorize subagents by default, perform the review locally unless the
user explicitly requests delegation.

**Step 2: Update the status document**

Record the root cause, official rate contract, RED and GREEN evidence, focused
and final suite results, exact code commit, review outcome and the fact that no
push, SSH, production query/write, service control, stage, activate or thaw
occurred. Leave the production cutover `in_progress` and require a fresh later
attempt.

**Step 3: Commit only the status document**

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: record Deepcoin maintenance pacing repair"
```

Expected: only the status document is staged for this commit.

**Step 4: Verify final repository state**

```bash
git status --porcelain=v1
git log -4 --oneline
```

Expected: clean tree and the design, plan, code/test and status commits visible.
