# Joint Recovery Historical Residue Hotfix Implementation Plan

> **Workflow:** Use the repository's `executing-plans` workflow task by task.

**Goal:** Let the recovery-only joint admission validate the exact Batch 119 incident while delegating global historical/fresh writer policy to its existing coherent writer check.

**Architecture:** Extract the exact Batch 119 incident-source validation from the ordinary loader without changing its checks. The ordinary loader keeps its existing global-exclusivity check; the joint loader calls the exact private source validator and then applies `_blocking_writer_count()` in the same query-only transaction. No public bypass, deployment-gate change, database write, or MiMo change is introduced.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite query-only transactions, pytest.

---

### Task 1: Reproduce the production historical-residue refusal

**Files:**
- Modify: `tests/test_bound_close_batch119_joint_recovery.py`
- Modify: `tests/test_composite_management_batch_recovery.py`

**Step 1: Write the failing joint regression**

Seed the exact joint incident plus known nonterminal management batch/component rows older than the writer freshness cutoff. Assert joint admission is `ready`, while changing either row to fresh or an unknown/NULL status remains `refused`.

**Step 2: Preserve ordinary Batch 119 behavior**

Add a regression proving the ordinary Batch 119 planner still refuses the same historical nonterminal management row as `additional_active_work_present`.

**Step 3: Run RED**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py \
  -k 'historical_nonterminal_management_residue'
```

Expected: the joint-ready assertion fails with `joint_material_invalid`; the ordinary-path assertion already passes.

### Task 2: Separate exact incident validation from global exclusivity

**Files:**
- Modify: `src/telegram_kol_research/composite_management_batch_recovery.py`
- Modify: `src/telegram_kol_research/bound_close_batch119_joint_recovery.py`

**Step 1: Extract the exact private source validator**

Move the existing Batch 119 identity, topology, false-submission, durable-evidence, instruction-population, and fingerprint checks into a private helper that does not perform the final global additional-work check.

**Step 2: Keep the ordinary wrapper strict**

Keep `_load_locked_recovery_source()` as the ordinary wrapper: call the exact helper, then reject when `_has_additional_active_database_work()` is true. Its observable behavior must not change.

**Step 3: Use the exact helper only inside joint admission**

In the same query-only transaction, pass the exact validated source into `_load_batch119_local_material_authority_in_session()`, then retain the existing `_blocking_writer_count()` check. No caller-provided allowlist or public option is added.

**Step 4: Run GREEN and adjacency tests**

```bash
.venv/bin/pytest -q \
  tests/test_bound_close_batch119_joint_recovery.py \
  tests/test_composite_management_batch_recovery.py \
  tests/test_bound_close_writer_quiescence.py \
  tests/test_deployment_preflight.py
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: all pass; production `deployment_preflight.py` remains unchanged.

### Task 3: Review and stop at the push boundary

Review the exact diff for a callable global-exclusivity bypass, fresh/unknown writer acceptance, query-only drift, raw evidence leakage, and ordinary planner regression. Resolve every Critical/Important finding, commit the reviewed hotfix, and stop for exact push approval. Do not retry production with a consumed diagnostic permit.
