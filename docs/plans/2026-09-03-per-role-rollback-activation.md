# Per-role Rollback Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow a fully scoped authority activation to start from independently verified split-runtime releases and restore each component to its exact pre-authorized release on failure.

**Architecture:** Add an activation-only rollback map bound by authorization v3, prove runtime and monitor identities per component, and publish rollback drop-ins per component. Preserve the v2 single-rollback path unchanged and bootstrap v3 through a hash-bound exact-commit control bundle. A dry-run executes every pre-mutation gate without consuming authorization or controlling services.

**Tech Stack:** Python 3.11+, dataclasses, canonical JSON, systemd/journald inspection, Bash, PowerShell, pytest.

---

### Task 1: Freeze the structural deadlock as RED tests

**Files:**
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `tests/test_deployment_action_plan.py`

**Steps:**

1. Add a split-runtime authority fixture whose Web rollback release differs from ingest/worker.
2. Add a test showing legacy single rollback rejects that fixture with `runtime identity proof failed`.
3. Add the desired per-role activation test using `rollback_releases`; it must initially fail because the field is unsupported.
4. Add manifest validation tests for exact keys, commit/digest formats, activation-only scope, and exact component coverage.
5. Run the exact new tests and record the expected RED failures before changing production code.

### Task 2: Add the typed manifest and canonical v3 authorization contract

**Files:**
- Modify: `src/telegram_kol_research/deployment_action_plan.py`
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_deployment_action_plan.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Add immutable typed rollback targets and canonical serialization.
2. Permit `rollback_releases` only for activation and require exact component coverage.
3. Keep all existing stage/activate fields exact while exempting only this activation-only field from `_same_declared_change()`.
4. Add authorization v3 validation for the rollback map, controller commit, and controller bundle SHA-256.
5. Retain the exact v2 field set and single rollback behavior.
6. Run manifest and authorization focused tests to GREEN.

### Task 3: Prove runtime roles against independent immutable releases

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Resolve and validate each distinct rollback release and its manifest-bound digest.
2. Compare the candidate runtime-support digest with every distinct rollback release.
3. Pass per-role expected releases into `prove_release_runtime()` without weakening any current identity predicate.
4. Add negative tests for arbitrary valid releases, manifest mismatch, live role mismatch, and corrupted trees.
5. Run the focused proof tests to GREEN.

### Task 4: Add read-only monitor rollback identity proof

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Extend `RuntimeAdapter` with a read-only monitor configured-identity/diagnostic method.
2. Parse effective Environment, FragmentPath, and DropInPaths for all monitor units and timer.
3. Validate root-owned regular unit/drop-in files and compute their maximum `st_mtime_ns`.
4. Parse the latest successful diagnostic payload without starting a unit.
5. Require matching commit/manifest, complete verified evidence, and diagnostic time strictly newer than every config file.
6. Add boundary tests for missing, mismatched, incomplete, equal-time, older, and fresh evidence.
7. Run monitor focused tests to GREEN.

### Task 5: Implement per-component activation rollback and fail-closed partial failure

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

**Steps:**

1. Publish candidate drop-ins exactly as today.
2. On failure, stop all declared units and publish each component's mapped rollback drop-in.
3. Start in the existing order and prove each role/monitor against its mapped target.
4. If any rollback step fails, best-effort stop all declared units and raise `rollback_failed`.
5. Preserve active-write, entry-freeze, restart, undeclared-process, and monitor gates.
6. Add success, rollback-complete, publication-failure, start-failure, and proof-failure tests.
7. Run activation and rollback focused tests to GREEN.

### Task 6: Add side-effect-free dry-run

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `deploy/telegram-kol-activate`
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `tests/test_server_update_scripts.py`

**Steps:**

1. Parse `ACTIVATION_DRY_RUN` strictly as `0` or `1`.
2. Execute every pre-mutation v2/v3 validation in dry-run.
3. Return canonical candidate/rollback/identity evidence before authorization consumption.
4. Assert zero stop/start/drop-in/daemon-reload/diagnostic-start calls.
5. Confirm live mode still revalidates and consumes authorization once.
6. Run dry-run and legacy live focused tests to GREEN.

### Task 7: Bootstrap v3 with an exact-commit control bundle

**Files:**
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Modify: `scripts/bootstrap_server_updater.sh`
- Modify: `deploy/telegram-kol-update`
- Modify: `tests/test_server_update_scripts.py`

**Steps:**

1. Detect the per-role manifest through the typed local action-plan parser.
2. Require an explicit controller commit independent of the runtime candidate, then archive only
   the required controller files from that exact reviewed controller commit.
3. Compute and transport the bundle SHA-256 and controller commit.
4. Verify hashes and safe archive entries before root-only `/run` extraction.
5. Run the controller with `python -B` and `PYTHONDONTWRITEBYTECODE=1`.
6. Bind controller commit and bundle hash into v3 authorization validation.
7. Keep the legacy single rollback dispatcher path unchanged.
8. Add Bash behavior/static tests and PowerShell static contract tests; run `bash -n` for all modified shell files.

### Task 8: Run focused safety regression and update documentation

**Files:**
- Modify: `tests/test_scoped_release_activation.py`
- Modify: `tests/test_deployment_action_plan.py`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `docs/known-issues-and-deferred-work.md`
- Create: `docs/2026-09-03-per-role-rollback-activation-implementation.md`

**Steps:**

1. Run all three focused test modules.
2. Run deployment-related tests covering authority scope, active writes, entry freeze, immutable content, monitor proof, authorization, and rollback.
3. Record the current production deployment deadlock and its scoped/authority structural cause in the known-issues document; mark it unresolved until the new activator is separately deployed.
4. Record RED evidence, focused results, compatibility behavior, dry-run guarantees, and non-production boundary in the implementation record.
5. Run `git diff --check` and inspect the exact changed-file set.

### Task 9: Full suite and independent review

**Files:**
- Modify only files required by review findings.

**Steps:**

1. Freeze the production-code candidate and record the exact base commit
   `358e8187ab4c0f1066501af23cedf624e7b3b032` and candidate commit.
2. Run the complete pytest suite once on the frozen candidate.
3. Dispatch an independent reviewer against the exact base/candidate range, emphasizing that no
   authority, identity, full-tree, active-write, entry-freeze, authorization, or rollback gate may
   be weakened.
4. Resolve every P0/P1/P2 finding and rerun affected focused tests plus the full suite if production
   code changes.
5. Commit implementation record and review conclusion, then push the reviewed commits to
   `codex/deepcoin-auto-trading-v1` without staging or activating a production release.
