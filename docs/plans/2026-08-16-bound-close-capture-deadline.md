# Bound Close Capture Deadline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give every fresh bound-close exchange capture a 180-second absolute deadline so the bounded 29-target read graph can complete without weakening fail-closed classification or the deployment gate.

**Architecture:** Keep the existing single-use read-only reader, request graph, 1 MiB response cap, 64-target cap, POSIX interruption, and outer 12-minute stopped-service window. Change only the CLI-owned per-capture ceiling from 30 to 180 seconds, prove initial and post-apply captures each receive a fresh deadline, and document the two independent time bounds.

**Tech Stack:** Python 3.12, Typer, pytest, Bash runbook, Deepcoin read-only capability wrapper.

---

## Execution Rules

- Work only in `/Users/steven/Documents/telegram获取消息-deployment-gate-recovery-plan` on `codex/bound-close-reservation-recovery`.
- Use strict RED-GREEN TDD for every behavior change.
- Do not modify `deployment_preflight.py`, exchange request methods, response-size bounds, target limits, database code, Batch 119, monitor activation, or MiMo v2.
- Do not add retries, chunking, age inference, writers, notifications, or replay.
- Do not push, deploy, stop production services, call production Deepcoin, or reuse an earlier approval token.
- Resolve every Critical/Important review finding before final verification.

### Task 1: Bind CLI captures to the 180-second ceiling

**Files:**
- Modify: `tests/test_cli_smoke.py`
- Modify: `src/telegram_kol_research/cli.py:5256,5387-5395,5437-5443`

**Step 1: Write the failing initial-capture deadline test**

Add a test beside the existing bound-close ready/refused CLI tests. Use a real
temporary database path, monkeypatch the source/reader builders, and record the
`deadline_monotonic` passed to
`capture_and_seal_bound_close_reservation_recovery`. Freeze
`cli_module.time.monotonic()` at `100.0` and require:

```python
assert received_deadlines == [280.0]
```

Keep the existing canonical output and exit-status assertions.

**Step 2: Run the test and verify RED**

```bash
.venv/bin/pytest -q tests/test_cli_smoke.py \
  -k 'bound_close and capture_deadline and initial'
```

Expected: FAIL because the current deadline is `130.0`.

**Step 3: Write the failing post-apply deadline test**

Strengthen
`test_bound_close_recovery_cli_narrowly_recaptures_an_ambiguous_postapply`.
Make `time.monotonic()` return `100.0` for the initial capture and `200.0` for
the narrow post-apply recapture. Record both deadline arguments and require:

```python
assert initial_deadline == 280.0
assert postapply_deadline == 380.0
assert postapply_deadline != initial_deadline
```

Retain the assertions that the second reader is fresh and only the exact
ambiguous-commit path reaches recapture.

**Step 4: Run the post-apply test and verify RED**

```bash
.venv/bin/pytest -q \
  tests/test_cli_smoke.py::test_bound_close_recovery_cli_narrowly_recaptures_an_ambiguous_postapply
```

Expected: FAIL because the current deadlines are `130.0` and `230.0`.

**Step 5: Implement the minimal change**

In `src/telegram_kol_research/cli.py`, change only:

```python
_BOUND_CLOSE_RESERVATION_CAPTURE_TIMEOUT_SECONDS = 30.0
```

to:

```python
_BOUND_CLOSE_RESERVATION_CAPTURE_TIMEOUT_SECONDS = 180.0
```

Do not add configuration, formulas, sleeps, or retries. Both call sites must
continue to add the constant to a fresh monotonic reading.

**Step 6: Run the new tests and verify GREEN**

Run the commands from Steps 2 and 4. Expected: PASS.

**Step 7: Run the complete CLI smoke suite**

```bash
.venv/bin/pytest -q tests/test_cli_smoke.py
```

Expected: PASS. Invalid paths, apply authorization, same-path enforcement,
opaque authority, and canonical redacted errors must remain unchanged.

**Step 8: Commit Task 1**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_smoke.py
git commit -m "fix: extend bound close capture deadline"
```

### Task 2: Document and test the two deadline layers

**Files:**
- Modify: `docs/runbook.md:2257-2285`
- Modify: `tests/test_bound_close_reservation_dry_run_comparison.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write the failing runbook contract test**

Require the bound-close section to contain these operator contracts:

```python
assert "单次 capture 的绝对硬上限为 180 秒" in section
assert "完成即立即返回" in section
assert "停服窗口的 12 分钟绝对 deadline 不变" in section
assert "timeout 仍为 UNKNOWN" in section
```

Also assert that it does not authorize retrying a refused capture in the same
window.

**Step 2: Run the contract test and verify RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  -k 'bound_close and runbook and deadline'
```

Expected: FAIL because the 180-second wording is absent.

**Step 3: Add the minimal runbook wording**

After the existing 12-minute writer-quiescence explanation, state that each
fresh exchange capture has an independent 180-second absolute ceiling; work
returns immediately; the ceiling covers streaming, decoding, and normalization;
timeout remains `UNKNOWN / exchange_capture_timeout`; capture two is unreachable
after refusal; the outer 12-minute deadline is unchanged; and another attempt
requires a new reviewed SHA and fresh exact tokens.

Do not modify executable Bash, approval tokens, service lists, polling, or traps.

**Step 4: Run the complete runbook/CLI tests**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py
```

Expected: PASS.

**Step 5: Verify Bash and diff hygiene**

Extract the window-one Bash block with the existing closed test helper or
equivalent `awk` selection and run `bash -n`, then run `git diff --check`.
Expected: both pass and Task 2 has no executable Bash diff.

**Step 6: Commit Task 2**

```bash
git add docs/runbook.md \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py
git commit -m "docs: explain bound close capture deadline"
```

### Task 3: Adjacent regression, independent review, and final checkpoint

**Files:**
- Test only; production edits are allowed only for review findings proven by a
  focused failing regression.

**Step 1: Run the dedicated recovery and transport suites**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_deepcoin_client.py
```

Expected: PASS. Real blocking/slow-drip interruption, decode/parse deadlines,
signal/timer restoration, 1 MiB cap, one-shot scope, GET-only reachability, and
ordinary-client behavior must remain unchanged.

**Step 2: Run the complete adjacent safety suite**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_reservation_recovery.py \
  tests/test_bound_close_reservation_dry_run_comparison.py \
  tests/test_cli_smoke.py \
  tests/test_deepcoin_client.py \
  tests/test_deployment_preflight.py
```

Expected: PASS. Confirm `src/telegram_kol_research/deployment_preflight.py` has
no diff.

**Step 3: Audit security reachability**

Prove from the diff and tests that the only production-code behavior change is
the bound-close CLI constant; no writer/database/replay/notification/MiMo path
is added; the 64-target and 1 MiB caps remain; timeout still maps to
`UNKNOWN / exchange_capture_timeout`; initial/post-apply captures use distinct
readers and deadlines; and ordinary Deepcoin, Batch 119, monitor, and gate
semantics are unchanged.

**Step 4: Request independent code review**

Use `requesting-code-review` against design commit `16679bb` and the new HEAD.
Require explicit Critical/Important findings for deadline ownership,
post-apply recapture, interruption, response bounds, runbook wording, and gate
isolation. Fix every valid finding with RED-GREEN TDD. Do not continue until
there are zero Critical and zero Important findings.

**Step 5: Run the complete local suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS with only documented environmental skips/warnings.

**Step 6: Run final hygiene checks**

```bash
.venv/bin/python -m compileall -q src tests scripts
git diff --check 16679bb..HEAD
git status --short
```

Expected: compile and diff checks pass and the worktree is clean.

**Step 7: Stop before push or production**

Report the commit range, focused/full-suite results, review result, and exact
new HEAD. Confirm nothing was pushed, deployed, or run against production, and
request separate push approval. A later read-only window requires that reviewed
40-character SHA and a fresh pair of exact tokens.
