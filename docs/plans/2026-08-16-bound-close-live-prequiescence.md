# Bound Close Live Pre-Quiescence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move the ten-minute writer-aging wait before service shutdown, then require one immediate stopped-state recheck before any exchange capture.

**Architecture:** Keep the existing closed writer helper, strict projection validator, unit inventory, capture runner, and hard deadlines. Split the runbook into a live read-only admission poll with its own twelve-minute deadline and a stopped phase with an independent twelve-minute deadline and exactly one writer-helper invocation. A live result never grants capture authority; only the stopped recheck can reach the first fresh reader.

**Tech Stack:** Bash runbook, Python/pytest shell simulations, existing read-only SQLite writer-quiescence helper, systemd state stubs, GNU timeout.

---

### Task 1: Prove the live pre-quiescence boundary

**Files:**
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `docs/runbook.md`

**Step 1: Write the failing tests**

Add shell-simulation tests that extract the bound-close runbook block and prove:

```python
def test_live_prequiescence_refused_then_ready_never_stops_services(tmp_path):
    result, events = _simulate_live_prequiescence(
        tmp_path,
        helper_statuses=(2, 2, 0),
        sleep_jump=15,
    )
    assert result.returncode == 0
    assert events.read_text() == (
        "helper\nverify\nsleep\n"
        "helper\nverify\nsleep\n"
        "helper\nverify\nlive-ready\n"
    )
    assert "stop" not in events.read_text()


def test_live_prequiescence_timeout_never_stops_services(tmp_path):
    result, events = _simulate_live_prequiescence(
        tmp_path,
        helper_statuses=(2,),
        sleep_jump=720,
    )
    assert result.returncode != 0
    assert "stop" not in events.read_text()
```

Also assert the live deadline is computed once as `now + 720`, polling is
conditional at fifteen seconds, helper/projection/unit/SHA/database checks occur
before `QUIESCE_ATTEMPTED=1`, and malformed projection/helper status/process-scan
errors fail before any stop call.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'live_prequiescence'
```

Expected: FAIL because the current poll is located after service shutdown.

**Step 3: Implement the minimal live poll**

In `docs/runbook.md`:

- Introduce a closed `run_bound_close_live_prequiescence` function or marked
  block using the existing helper and strict projector.
- Compute one live deadline with `bound_close_now_epoch + 720`.
- Call `verify_all_local_identity_before_stop` before and after each helper
  invocation. It must check reviewed/production SHA, resolved database
  path/device/inode, unit inventory, and process-scan command integrity without
  requiring units to be inactive.
- Poll only a strictly validated `refused` projection; return immediately on
  exact `ready`.
- Run this block before `CAPTURE_RUNNER` construction and before
  `QUIESCE_ATTEMPTED=1`.
- Do not call Deepcoin, stop/reset/start units, write SQLite, or persist helper
  output outside the private temporary directory.

**Step 4: Run focused and full runbook tests**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py
```

Expected: PASS.

Extract the first Bash block and run `bash -n`; run `git diff --check`.

**Step 5: Commit**

```bash
git add docs/runbook.md tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: preflight reservation writers before stopping"
```

### Task 2: Make the stopped writer check single-shot and race closed

**Files:**
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `docs/runbook.md`

**Step 1: Write the failing tests**

Add a real shell simulation for the stopped phase:

```python
def test_post_stop_writer_race_restores_without_exchange_capture(tmp_path):
    result, events = _simulate_stopped_recheck(
        tmp_path,
        helper_status=2,
    )
    assert result.returncode != 0
    assert events.read_text().splitlines() == [
        "stop",
        "helper",
        "restore",
    ]
    assert "capture" not in events.read_text()


def test_post_stop_ready_invokes_helper_once_then_double_capture(tmp_path):
    result, events = _simulate_stopped_recheck(
        tmp_path,
        helper_status=0,
    )
    assert result.returncode == 0
    assert events.read_text().count("helper\n") == 1
    assert events.read_text().count("capture\n") == 1
    assert "sleep" not in events.read_text()
```

Assert the stopped deadline is computed only after all units are inactive, stays
`now + 720`, retains the 420-second capture admission reserve, and wraps the
entire private runner with `timeout --signal=KILL`.

**Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'post_stop_writer'
```

Expected: FAIL because the stopped phase still contains the aging loop.

**Step 3: Implement the minimal stopped recheck**

Replace the stopped `while` loop with one sequence:

```bash
verify_all_local_quiescence_and_identity
run_bound_close_writer_quiescence_helper "$WRITER_QUIESCENCE_RAW"
verify_all_local_quiescence_and_identity
project_bound_close_writer_quiescence_result ...
require_exact_ready_projection "$WRITER_QUIESCENCE_PROJECTION"
run_bound_close_double_capture_before_deadline
```

Requirements:

- `HELPER_STATUS=2`, malformed projection, identity drift, or any nonzero helper
  result exits through the existing trap immediately.
- There is no stopped-state sleep or retry.
- First exchange-reader construction remains unreachable until the exact ready
  projection and 420-second reserve both pass.
- Preserve the existing two fresh readers, independent 180-second deadlines,
  diagnostic redaction, 0600 files, process-group kill, and restoration logic.

**Step 4: Run affected and adjacent tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_deepcoin_client.py \
  tests/test_deployment_preflight.py
```

Expected: PASS. Confirm `src/telegram_kol_research/deployment_preflight.py` has
no diff.

**Step 5: Commit**

```bash
git add docs/runbook.md tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: recheck reservation writers after stopping"
```

### Task 3: Final safety review and push boundary

**Files:**
- Verify only; modify implementation files only for reviewed Critical/Important
  findings, with a new RED test first.

**Step 1: Request independent review**

Use `requesting-code-review` over the two implementation commits. Require review
of:

- live poll cannot stop units or reach exchange reads;
- stopped helper is single-shot;
- live readiness is not capture authority;
- post-stop races restore immediately;
- both twelve-minute deadlines are independent and absolute;
- gate, apply, replay, notification, deployment, and MiMo boundaries are
  unchanged.

Fix every Critical/Important with RED→GREEN and re-review to 0/0.

**Step 2: Run full verification**

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check <design-commit>..HEAD
```

Extract the runbook Bash block and run `bash -n`. Confirm clean worktree and no
diff to deployment-preflight production source.

**Step 3: Stop before push**

Report the exact reviewed SHA and validation totals. Do not push, deploy, stop
services, run exchange capture, apply recovery, or enable MiMo v2 without the
next explicit approval.

