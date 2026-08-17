# Simple Gate Unknown Policy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow historical unknown outcomes to produce a verified WARN only when the exact deployment writer fingerprint is unchanged, while keeping active writes, malformed evidence, and changed-writer unknowns fail-closed.

**Architecture:** Keep the decision in the existing pure `decide_deployment` function and keep artifacts semantic rather than adding an override. Complete the source-deletion adapter from authoritative producer states, then let the existing artifact builder, verifier, CLI exit codes, and two-phase updater carry the new WARN without changing updater ordering or rollback.

**Tech Stack:** Python 3.12, dataclasses, SQLite read-only evidence collection, pytest, Git blob/mode boundary tests, Bash updater harnesses.

---

### Task 1: Make unknown outcomes conditional on the exact writer fingerprint

**Files:**
- Modify: `tests/test_deployment_work_evidence.py:13-84`
- Modify: `src/telegram_kol_research/deployment_work_evidence.py:87-113`

**Step 1: Write the failing pure-policy tests**

Change the existing unchanged-writer unknown case to expect WARN with
`unknown_outcome_with_unchanged_writer`. Add the symmetric changed-writer case
expecting BLOCK with `writer_changed_with_unknown_outcome`.

Add an exact combined-WARN test:

```python
def test_unchanged_writer_reports_unknown_and_queued_warn_reasons() -> None:
    result = decide_deployment(
        counts=DeploymentEvidenceCounts(unknown_outcome=1, queued_work=1),
        writer_changed=False,
    )
    assert result.decision == "WARN"
    assert result.reason_codes == (
        "unknown_outcome_with_unchanged_writer",
        "queued_work_with_unchanged_writer",
    )
```

Update the fixed blocking-order test to expect
`writer_changed_with_unknown_outcome` in the existing unknown position.

**Step 2: Run the tests to verify RED**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_work_evidence.py -k 'decision'
```

Expected: the old unconditional unknown BLOCK and old reason code fail the new
assertions.

**Step 3: Implement the minimal pure-policy change**

Use two ordered lists and no new inputs:

```python
blocking_reasons: list[str] = []
warning_reasons: list[str] = []
if counts.invalid_evidence:
    blocking_reasons.append("invalid_registered_evidence")
if counts.active_write:
    blocking_reasons.append("active_exchange_write")
if counts.unknown_outcome:
    if writer_changed:
        blocking_reasons.append("writer_changed_with_unknown_outcome")
    else:
        warning_reasons.append("unknown_outcome_with_unchanged_writer")
if counts.queued_work:
    if writer_changed:
        blocking_reasons.append("writer_changed_with_queued_work")
    else:
        warning_reasons.append("queued_work_with_unchanged_writer")
if blocking_reasons:
    return DeploymentDecision("BLOCK", tuple(blocking_reasons))
if warning_reasons:
    return DeploymentDecision("WARN", tuple(warning_reasons))
return DeploymentDecision("PASS", ())
```

Do not add time, count, row identity, operator, schema, or environment inputs.

**Step 4: Run the Step 2 command and verify GREEN**

Expected: all selected decision tests pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deployment_work_evidence.py \
  tests/test_deployment_work_evidence.py
git commit -m "fix: warn on unknown with unchanged writer"
```

### Task 2: Complete authoritative source-deletion state classification

**Files:**
- Modify: `tests/test_deployment_work_evidence.py:288-370`
- Modify: `src/telegram_kol_research/deployment_work_evidence.py:260-334`

**Step 1: Write production-shaped and authority-state RED tests**

Add parameterized tests for `closing_positions` and `reconciling` expecting
`queued_work`. Add a production-shaped `recovery_required` row with no claim
expecting `inactive`.

Add malformed claim-shape tests for one-sided token/time, empty token, and a
paused or terminal row retaining a live claim. Keep the existing invented-state
and duplicate-source-event tests.

Example:

```python
@pytest.mark.parametrize("state", ["closing_positions", "reconciling"])
def test_source_deletion_orchestration_states_are_queued(tmp_path, state):
    database = tmp_path / f"source-{state}.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_message_deletion_exits "
            "(id, source_event_id, raw_message_id, state) VALUES (1, 11, 22, ?)",
            (state,),
        )
    _assert_only_category(collect_deployment_evidence(database), "queued_work")
```

**Step 2: Run the tests to verify RED**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_work_evidence.py -k 'source_deletion or source_without'
```

Expected: the three authoritative states are currently invalid.

**Step 3: Implement the authoritative mapping**

Derive `no_claim`, a non-empty paired `live_claim`, and `claim_valid` once.
Classify:

- `unbound`: keep the existing all-NULL invariant, inactive;
- `succeeded`, `ignored`, `failed`, `cancelled`: require no claim, inactive;
- `pending`, `waiting`, `closing_positions`, `reconciling`: require a frozen
  target plus either no claim or a valid paired claim, queued;
- `recovery_required`: require no claim, inactive/permanently paused;
- everything else or a malformed claim: invalid.

Do not inspect the current production reason, timestamp, row ID, or count.

**Step 4: Run the full evidence tests**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_work_evidence.py
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deployment_work_evidence.py \
  tests/test_deployment_work_evidence.py
git commit -m "fix: classify source deletion recovery evidence"
```

### Task 3: Prove artifact, CLI, updater, and future-change boundaries

**Files:**
- Modify: `tests/test_deployment_preflight.py:350-390`
- Modify: `tests/test_deployment_preflight_cli.py:165-230`
- Modify: `tests/test_simple_deployment_gate_boundary.py:32-66`
- Test: `tests/test_server_updater_phases.py`

**Step 1: Write artifact semantic RED tests**

Add a preliminary artifact test where `unknown_outcome=1` and
`writer_changed=False`; artifact build and verify must both return WARN with
only `unknown_outcome_with_unchanged_writer`. Add the symmetric changed-writer
test expecting BLOCK and `writer_changed_with_unknown_outcome`.

Keep the existing facts/decision re-signing tests. Update expected valid
semantics only; never weaken the mismatch assertion.

**Step 2: Write CLI exit-code RED tests**

Extend the test input helper so `trade_signals.status` can be
`unknown_exchange_outcome`. Assert:

- unchanged writer: collect and verify both return 2 and report WARN;
- changed writer blob: collect returns 3 and reports BLOCK.

Exercise the real surface classifier, collector, artifact writer/reader, and
verifier; do not mock `decide_deployment`.

**Step 3: Run artifact and CLI tests to verify RED**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_deployment_preflight.py \
  tests/test_deployment_preflight_cli.py
```

Expected: new unchanged-writer WARN assertions fail before Task 1 GREEN.

**Step 4: Verify GREEN without production artifact/updater changes**

Run the Step 3 command after Tasks 1 and 2. Expected: pass through existing
semantic recomputation and exit-code mapping. If artifact or updater production
code needs a special case, stop and revise the design.

**Step 5: Refresh the frozen runtime blob and run boundary tests**

```bash
git hash-object src/telegram_kol_research/deployment_work_evidence.py
```

Update only that mode-100644 blob in `APPROVED_INTERFACE_ENTRIES`. Then run:

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
  tests/test_simple_deployment_gate_boundary.py \
  tests/test_server_updater_phases.py
```

Expected: all pass; synthetic count, timestamp, manual bypass, content, and
mode mutations remain rejected.

**Step 6: Commit**

```bash
git add tests/test_deployment_preflight.py \
  tests/test_deployment_preflight_cli.py \
  tests/test_simple_deployment_gate_boundary.py
git commit -m "test: bind conditional unknown gate policy"
```

### Task 4: Update the operator decision table atomically

**Files:**
- Modify: `docs/server-deployment.md:155-180`
- Modify: `docs/runbook.md:1760-1855`
- Modify: `docs/migration-handoff.md`

**Step 1: Find every current unknown-policy statement**

```bash
rg -n 'unknown|unknown outcome|unknown_exchange|writer unchanged|writer changed' \
  docs/server-deployment.md docs/runbook.md docs/migration-handoff.md
```

**Step 2: Replace the decision table consistently**

Document only:

```text
active or invalid -> BLOCK
unknown or queued + changed writer -> BLOCK
unknown or queued + unchanged writer -> WARN
otherwise -> PASS
```

State that WARN is computed and artifact-bound, not an operator override.
Preserve all three approvals and prohibitions on database edits, historical
replay, notifications, and exchange writes.

**Step 3: Re-run Step 1 and inspect every match**

Expected: no contradiction still says all unknown outcomes unconditionally
BLOCK or that historical unknowns are silently ignored.

**Step 4: Commit**

```bash
git add docs/server-deployment.md docs/runbook.md docs/migration-handoff.md
git commit -m "docs: document conditional unknown deployment gate"
```

### Task 5: Complete local verification and independent review

**Files:** No intended production changes.

**Step 1: Run the focused safety suite**

```bash
../telegram获取消息/.venv/bin/python -m pytest -q \
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
  tests/test_authoritative_recognition.py \
  tests/test_trading_settings.py \
  tests/test_mimo_recognition_runs.py
```

Expected: all pass.

**Step 2: Run static, surface, and full checks**

```bash
../telegram获取消息/.venv/bin/python -m compileall -q src tests
bash -n deploy/telegram-kol-update scripts/bootstrap_server_updater.sh scripts/server_git_update.sh
git diff --check 2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa..HEAD
../telegram获取消息/.venv/bin/python -m pytest -q
git status --short
```

Run `classify_candidate_surface` against production `2274d90...` and the exact
candidate SHA. Expected: writer/schema unchanged, writer fingerprints equal,
and a clean worktree.

**Step 3: Request independent Critical/Important review**

Use `requesting-code-review`. The reviewer must construct and run:

- unchanged-writer unknown -> verified WARN;
- changed-writer unknown -> BLOCK;
- active or malformed evidence -> BLOCK regardless of writer;
- production-shaped source deletion `recovery_required` -> inactive;
- invented state or malformed claim -> invalid;
- no count/time/row/manual bypass;
- updater Phase A/B WARN binding and rollback/mutation boundaries unchanged.

Any Critical or Important finding requires a new RED, minimal GREEN, relevant
reruns, and another independent review.

**Step 4: Stop for explicit push approval**

Report the exact reviewed SHA, clean status, test results, and review verdict.
Do not push automatically.

### Task 6: Repeat the same server shadow after separate approval

**Files:** No intended repository changes.

**Step 1: Push only after explicit approval**

Push `codex/deployment-gate-simplification` and verify local, tracking, and
remote SHA equality. Stop for separate server-shadow approval.

**Step 2: Stage the exact SHA without installing it**

Create/reuse a detached clean mode-0700 candidate under
`/opt/telegram-kol-candidates/`. Use the production `.venv` plus candidate
`PYTHONPATH`; do not alter the production checkout or service.

**Step 3: Run the same focused tests and read-only Phase A**

Acceptance:

```text
writer_changed = false
active_write = 0
invalid_evidence = 0
unknown_outcome >= 0
decision = WARN
reason includes unknown_outcome_with_unchanged_writer
database writes = 0
notifications = 0
exchange/network mutation calls = 0
```

Verify production SHA, tracked status, service PID/InvocationID, MiMo v1 mode,
MiMo v2 watermark, database watermarks, settings hash, notification counters,
and execution counters before and after.

**Step 4: Stop for separate deployment approval**

A successful WARN shadow still does not authorize a production branch update,
service stop, checkout, install, restart, or deployment.

