# Bound Close Legacy Monitor Transient-State Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the live bound-close writer-aging wait tolerate only the expected timer-driven legacy monitor oneshot transitions while preserving every other exact safety boundary.

**Architecture:** Add one closed legacy-monitor live-state verifier and keep the existing exact verifier for every other unit. The live runner uses the closed exception only while the paired timer remains installed and active; after timer freeze, the existing reset function deterministically converges only `failed`, `activating`, or `inactive` to the authorized inactive baseline.

**Tech Stack:** Bash runbook, Python/pytest shell simulations, systemd command stubs, existing GNU timeout process-group deadlines.

---

### Task 1: Define the closed live-state exception

**Files:**
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `docs/runbook.md`

**Step 1: Write the failing state-matrix tests**

Add table-driven tests around a real extracted
`verify_bound_close_legacy_monitor_live_state` function:

```python
@pytest.mark.parametrize(
    ("monitor_load", "monitor_state", "timer_load", "timer_state", "expected"),
    [
        ("loaded", "failed", "loaded", "active", 0),
        ("loaded", "activating", "loaded", "active", 0),
        ("loaded", "inactive", "loaded", "active", 0),
        ("loaded", "active", "loaded", "active", 1),
        ("loaded", "deactivating", "loaded", "active", 1),
        ("loaded", "unknown", "loaded", "active", 1),
        ("not-found", "failed", "loaded", "active", 1),
        ("loaded", "failed", "loaded", "inactive", 1),
        ("loaded", "failed", "not-found", "inactive", 1),
    ],
)
def test_legacy_monitor_live_state_exception_is_closed(...):
    ...
```

Also add a mixed-unit test proving:

```python
def test_live_unit_verifier_keeps_core_and_install_states_exact():
    assert legacy_monitor_timer_transition_is_accepted()
    assert core_service_runtime_drift_is_refused()
    assert any_unit_install_drift_is_refused()
```

Require the exception to be unavailable unless the original snapshots contain:

```text
telegram-kol-monitor.service = installed:(failed|inactive)
telegram-kol-monitor.timer   = installed:active
```

**Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'legacy_monitor_live_state_exception or live_unit_verifier_keeps'
```

Expected: FAIL because the closed verifier does not exist and the current live
verifier requires exact monitor runtime equality.

**Step 3: Implement the minimal verifier**

In `docs/runbook.md`, add:

```bash
verify_bound_close_legacy_monitor_live_state() {
  local monitor_load
  local monitor_state
  local timer_load
  local timer_state
  [ "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.service]}" = installed ]
  case "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}" in
    failed|inactive) ;;
    *) return 1 ;;
  esac
  [ "${ORIGINAL_UNIT_INSTALL_STATE[telegram-kol-monitor.timer]}" = installed ]
  [ "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.timer]}" = active ]
  monitor_load="$(systemctl show telegram-kol-monitor.service \
    --property=LoadState --value)"
  [ "$monitor_load" = loaded ] || return 1
  monitor_state="$(systemctl is-active telegram-kol-monitor.service || true)"
  case "$monitor_state" in
    failed|activating|inactive) ;;
    *) return 1 ;;
  esac
  timer_load="$(systemctl show telegram-kol-monitor.timer \
    --property=LoadState --value)"
  [ "$timer_load" = loaded ] || return 1
  timer_state="$(systemctl is-active telegram-kol-monitor.timer || true)"
  [ "$timer_state" = active ]
}
```

Add `verify_bound_close_unit_group_live_state` that calls the new verifier
only for the exact installed legacy monitor and delegates every other unit to
the existing exact original-state rules. Update
`verify_all_local_identity_before_stop` to call the live-state verifier.

Export both new functions in
`write_bound_close_live_prequiescence_runner`. Do not export them to the
stopped or capture runner.

**Step 4: Run focused and full runbook tests**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'legacy_monitor_live_state_exception or live_unit_verifier_keeps'
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py
```

Expected: PASS.

Extract the first bound-close Bash block and run `bash -n`; run
`git diff --check`.

**Step 5: Commit**

```bash
git add docs/runbook.md tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: allow closed legacy monitor live transitions"
```

### Task 2: Prove timer-freeze convergence and end-to-end live behavior

**Files:**
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `docs/runbook.md`

**Step 1: Write failing convergence and integration tests**

Extend the real reset-function matrix:

```python
@pytest.mark.parametrize(
    ("original", "current", "expected_action", "expected_baseline"),
    [
        ("failed", "failed", "reset-failed", "inactive"),
        ("failed", "activating", "stop", "inactive"),
        ("inactive", "activating", "stop", "inactive"),
        ("inactive", "inactive", "", "inactive"),
        ("failed", "active", "refused", "failed"),
        ("failed", "deactivating", "refused", "failed"),
    ],
)
def test_timer_frozen_monitor_convergence_is_closed(...):
    ...
```

Add a shell simulation that runs at least three live helper rounds and changes
the monitor state between verifications:

```python
def test_live_wait_accepts_timer_driven_failed_activating_failed(tmp_path):
    result, events = _simulate_live_prequiescence(
        helper_statuses=(2, 2, 0),
        monitor_states=("failed", "activating", "failed", "failed"),
    )
    assert result.returncode == 0
    assert "stop" not in events.read_text()
```

Add counterexamples for timer runtime drift, monitor `active`, core service
drift, and install-state drift. Each must exit before any stop or exchange
capture.

**Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_bound_close_reservation_dry_run_comparison.py \
  -k 'timer_frozen_monitor_convergence or timer_driven_failed_activating'
```

Expected: FAIL because the reset function currently accepts `active` but does
not accept the approved `activating` state.

**Step 3: Implement the closed timer-frozen convergence**

Change only
`reset_bound_close_legacy_monitor_after_timer_freeze`:

```bash
case "$current_state" in
  activating)
    run_bound_close_external_command_before_deadline \
      sudo systemctl stop telegram-kol-monitor.service
    ORIGINAL_UNIT_STATE["telegram-kol-monitor.service"]=inactive
    ;;
  failed)
    run_bound_close_external_command_before_deadline \
      sudo systemctl reset-failed telegram-kol-monitor.service
    ORIGINAL_UNIT_STATE["telegram-kol-monitor.service"]=inactive
    ;;
  inactive)
    case "${ORIGINAL_UNIT_STATE[telegram-kol-monitor.service]}" in
      failed|inactive)
        ORIGINAL_UNIT_STATE["telegram-kol-monitor.service"]=inactive
        ;;
      *) return 1 ;;
    esac
    ;;
  *) return 1 ;;
esac
```

Keep the paired timer inactive verification before this function is called.
Keep the final exact monitor `inactive` check under the shared stopped
deadline. Do not add any start, retry, sleep, or new approval.

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

Expected: PASS. Confirm:

```bash
git diff --exit-code HEAD~1 -- src/telegram_kol_research/deployment_preflight.py
```

Extract the runbook Bash block and run `bash -n`; run
`git diff --check`.

**Step 5: Commit**

```bash
git add docs/runbook.md tests/test_bound_close_reservation_dry_run_comparison.py
git commit -m "fix: converge timer frozen legacy monitor"
```

### Task 3: Independent safety review and verification boundary

**Files:**
- Verify only; modify implementation files only for independently reviewed
  Critical/Important findings, with a new RED test first.

**Step 1: Request independent review**

Use `requesting-code-review` over Tasks 1–2. Require review of:

- the exception is reachable only for the exact installed legacy monitor;
- the paired timer remains exact `installed:active` during live waiting;
- every other unit retains exact install/runtime equality;
- `active`, `deactivating`, unknown, missing, and malformed monitor states
  fail before stopping;
- timer freeze precedes monitor convergence;
- only `failed`, `activating`, and `inactive` converge to inactive;
- legacy baseline mutation remains visible to the parent restoration trap;
- live readiness never grants capture authority;
- both twelve-minute process-group deadlines and stopped single-shot helper
  remain unchanged; and
- gate, apply, replay, notification, deployment, and MiMo boundaries remain
  unchanged.

Fix every Critical/Important with a new RED test, make it GREEN, and re-review
until the result is `0 Critical / 0 Important`.

**Step 2: Run full verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check 4b2578013c95574e4478728c4d3b46cd510d93ed..HEAD
```

Extract the bound-close runbook Bash block and run `bash -n`. Confirm:

- the worktree is clean;
- only the planned runbook/tests and plan documents changed;
- `src/telegram_kol_research/deployment_preflight.py` has no diff;
- no production, network, push, capture, apply, notification, or MiMo action
  occurred.

**Step 3: Stop before push**

Report the exact reviewed SHA and test totals. Do not push, deploy, run another
production read-only window, apply recovery, or enable MiMo v2 without new
explicit approvals.
