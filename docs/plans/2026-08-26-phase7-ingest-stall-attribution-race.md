# Phase 7 Ingest Stall Attribution Race Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve the exact pre-recovery ingest event-loop stack when a loop check-in races the watchdog's frame capture, without restoring stale-stack misattribution.

**Architecture:** Keep `LoopStallAttributor` as a passive watchdog thread. Linearize stall-episode ownership and frame freezing under its existing state lock, using a re-entrant lock only so injected defensive providers can still report a true same-thread recovery. Format and store only the frame obtained before a different loop thread can complete its recovery check-in.

**Tech Stack:** Python 3.12, `threading`, `sys._current_frames`, `traceback`, pytest.

---

### Task 1: Prove the recovery/frame race

**Files:**

- Modify: `tests/test_runtime_stall_attribution.py`

**Step 1: Write the failing test**

Add a deterministic event-driven test named
`test_concurrent_recovery_cannot_erase_pre_recovery_blocking_function`.
Its injected frame provider must pause after the watchdog has claimed the stall;
a separate thread then attempts `note_checkin()`. Assert that the check-in cannot
complete until frame capture is released, and that the retained stack contains
`synthetic_ingest_blocking_call` with no recovery-discard reason.

**Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_stall_attribution.py::test_concurrent_recovery_cannot_erase_pre_recovery_blocking_function \
  -vv
```

Expected: FAIL because current `poll_once()` releases `_lock` before
`_format_loop_stack()`, allowing the check-in to complete and erase the stack.

### Task 2: Freeze the pre-recovery frame atomically

**Files:**

- Modify: `src/telegram_kol_research/runtime_loop_health.py`
- Test: `tests/test_runtime_stall_attribution.py`

**Step 1: Implement the minimal fix**

Replace the attributor state lock with `threading.RLock`. In `poll_once()`, keep
that lock from the final stall/rate-limit decision through
`_format_loop_stack(thread_id)`, the generation comparison, and capture append.
The loop thread then cannot publish a recovery check-in between stall ownership
and frame freezing. Preserve the existing generation comparison so an injected
provider that truly checks in before returning a frame is still discarded.
Log the immutable `StallCapture` after releasing the lock.

**Step 2: Run GREEN**

Run the exact RED command again. Expected: PASS.

**Step 3: Run focused attribution and loop-health compatibility**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_stall_attribution.py \
  tests/test_runtime_loop_health.py \
  tests/test_web_app.py \
  -k 'stall or loop_health' -q
```

Expected: all selected tests pass, including the existing true-recovery stale-
stack discard test.

### Task 3: Verify the final local candidate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

**Step 1: Run static checks**

```bash
git diff --check
.venv/bin/python -m compileall -q \
  src/telegram_kol_research/runtime_loop_health.py \
  tests/test_runtime_stall_attribution.py
```

Expected: both exit zero.

**Step 2: Run one final complete suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: zero failures. Record exact pass/skip/warning counts and elapsed time.

**Step 3: Record the bounded result**

Update canonical status with the confirmed race, RED/GREEN evidence, exact
candidate commit, full-suite evidence, and the remaining production boundary:
this local change makes the next real stall attributable but does not claim the
unknown ingest blocking function has already been removed. Release ownership
and require separate push/deployment authorization before any production use.

**Step 4: Commit only explicit paths**

Stage only the source, test, this plan, and canonical status paths involved in
the corresponding checkpoint. Never use `git add -A`.
