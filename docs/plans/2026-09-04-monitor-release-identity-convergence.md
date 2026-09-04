# Monitor Release Identity Convergence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove monitor's second release identity source and prove the main-process import path during activation dry-run.

**Architecture:** Monitor base units inherit the generic release environment published by the scoped activator; the monitor env file retains only credentials and policy. The activator validates real systemd effective command/environment inputs and narrowly recognizes the audited legacy-to-canonical unit migration without relaxing unrelated runtime-support checks.

**Tech Stack:** Python 3.12, pytest, systemd unit files, Bash installer, immutable release activation.

---

### Task 1: Lock the missing dry-run proof with RED

**Files:**
- Modify: `tests/test_scoped_release_activation.py`

1. Add a split-source harness in which the generic candidate drop-in is prospective while the
   effective monitor main command still resolves an old monitor-specific path.
2. Assert dry-run raises a source-specific `ActivationError` before stop/start/reload and before
   authorization consumption.
3. Run the single test and record the expected `DID NOT RAISE` RED result.

### Task 2: Canonicalize the monitor unit and installer contract

**Files:**
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Modify: `deploy/systemd/telegram-kol-monitor-test-notification.service`
- Modify: `scripts/install_server_monitor.sh`
- Modify: `tests/test_server_monitor_installation.py`

1. Add failing static assertions that all three units directly invoke the virtualenv CLI and no
   monitor release key is emitted by the installer.
2. Remove the legacy command-local `PYTHONPATH` prefix and the three installer env writes.
3. Run the focused installation tests to GREEN.

### Task 3: Publish only the generic monitor identity

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

1. Add failing assertions that monitor drop-ins contain only generic release identity.
2. Remove the three monitor-specific assignments while retaining generic `PYTHONPATH`, commit,
   manifest, read-only release path, and ExecStartPre.
3. Run the focused render tests to GREEN.

### Task 4: Add effective main-process proof to dry-run

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

1. Extend the runtime protocol with a candidate monitor main-process proof.
2. Parse real `systemctl show` properties, validate the installed EnvironmentFile and effective
   command/environment, and overlay only the prospective generic candidate values.
3. Invoke the proof after rollback identity proof but before the active-write gate and dry-run
   return.
4. Cover matching sources, legacy conflict, missing Environment, missing/missing-file
   EnvironmentFile, and malformed ExecStart; assert zero service-control events.
5. Re-run the focused activation tests to GREEN.

### Task 5: Preserve rollback proof and support-digest safety

**Files:**
- Modify: `src/telegram_kol_research/scoped_release_activation.py`
- Modify: `tests/test_scoped_release_activation.py`

1. Update rollback proof to require generic identity and the canonical main command while keeping
   diagnostic freshness unchanged.
2. Add exact normalization for only the legacy monitor ExecStart prefix in the three named units.
3. Prove old/new monitor units compare compatible and any other command/sandbox/unit difference
   still fails.
4. Run all scoped activation tests.

### Task 6: Record deferred freeze-window loss

**Files:**
- Modify: `docs/known-issues-and-deferred-work.md`

1. Record raw 14795/14796, the 25-minute freeze window, zero attempts, and the lifecycle 1074
   management-message impact without including broader message content.
2. Record the future durable freeze-interval attribution and owner-notification direction; do not
   change stale thresholds or implement recovery.

### Task 7: Final verification and independent review

**Files:**
- Create: `docs/2026-09-04-monitor-release-identity-convergence-implementation.md`

1. Run focused tests for activation and monitor installation.
2. Run the complete pytest suite once on the frozen production-code candidate.
3. Commit code/tests/docs with explicit paths and record exact base/head SHAs.
4. Dispatch an independent reviewer against the exact base, specifically checking that identity,
   diagnostic freshness, runtime-support, authorization, freeze, and rollback gates were not
   weakened.
5. Resolve all P0/P1/P2 findings, rerun affected focused tests and the full suite if production
   code changes, then update the implementation record and push the reviewed commits.
