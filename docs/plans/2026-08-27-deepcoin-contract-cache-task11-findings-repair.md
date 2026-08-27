# Deepcoin Contract Cache Task 11 Findings Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the four exact-SHA Task 11 review findings without changing trading semantics, weakening fail-closed behavior, or authorizing any remote or production action.

**Architecture:** Preserve the approved worker-owned cache and atomic publication design. Restore the explicit Agent deny ACL on the candidate inode before atomic replacement, bind descriptor inspection to the current directory entry, make all installed monitor oneshots consume the governed auto-trade expectation, and use a distinct restore watermark so frozen-window messages remain terminal and unreplayed.

**Tech Stack:** Python 3.11, POSIX file descriptors and ACLs, Bash, systemd, pytest, existing governed deployment updater.

---

## Scope and authority

- Base: exact clean `49b8f40c9af0f38344724c84f39a7e065e5beabd` on
  `codex/phase0-deploy-integration` in the authoritative main workspace.
- Local code, tests, documentation, canonical status, and explicit-path local
  commits only.
- No push, SSH, deployment, restart, production/database/settings write,
  Telegram replay, manufactured traffic, or Deepcoin write.
- The 14 historical `contract_spec_sync_unavailable` refusals remain terminal.
- Keep `mkstemp -> fsync -> strict reload -> os.replace -> directory fsync`.

### Task 1: Preserve the explicit Agent deny ACL on every published inode

**Files:**

- Modify: `tests/test_deepcoin_contract_spec_cache.py`
- Modify: `src/telegram_kol_research/contract_cache_permissions.py`
- Modify: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`

**Step 1: Write RED**

Add a focused publication test proving the candidate inode receives the shared
fixed ACL helper after inherited ACL removal and before `os.replace()`. Assert
the descriptor is still open, mode is `0660`, and only the bounded Agent deny
ACL is applied.

**Step 2: Verify RED**

Run the single test and require failure because publication currently clears
the ACL without restoring `telegram-kol-agent:---`.

**Step 3: Implement minimal GREEN**

Expose one descriptor-only Agent deny helper from
`contract_cache_permissions.py`. Reuse it from both convergence and publication.
Publication continues clearing inherited ACLs first, then applies exact
`u:telegram-kol-agent:---,g::rw-,m::rw-` before strict reload and replacement.

**Step 4: Verify GREEN**

Run the focused publication and permission tests.

### Task 2: Fail closed when the descriptor is no longer the current entry

**Files:**

- Modify: `tests/test_contract_cache_permissions.py`
- Modify: `src/telegram_kol_research/contract_cache_permissions.py`

**Step 1: Write RED**

Add a deterministic race regression that replaces the directory entry during
the final ACL read. Require `verification_failed` and prove the replacement
inode is not reported as compliant. Cover both inspect and converge paths where
practical without relying on timing.

**Step 2: Verify RED**

Run the focused race test and require the current implementation to return a
false-positive compliant status.

**Step 3: Implement minimal GREEN**

After ACL inspection, re-`fstat()` the open descriptor and use
`os.stat(name, dir_fd=directory_fd, follow_symlinks=False)` to require the same
device/inode, a regular type, link count one, and final owner/group/mode. Any
missing, replaced, or malformed current entry maps to bounded
`verification_failed`/existing fail-closed categories.

**Step 4: Verify GREEN**

Run the complete permission test file, including unknown-owner and unsafe-target
regressions.

### Task 3: Govern frozen expectations for all monitor oneshots transactionally

**Files:**

- Modify: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Modify: `deploy/systemd/telegram-kol-monitor-test-notification.service`
- Modify: `deploy/telegram-kol-update`
- Modify: `tests/test_server_monitor_installation.py`
- Modify: `tests/test_minimal_server_updater.py`
- Modify: `tests/test_server_update_scripts.py`

**Step 1: Write RED**

Require all three monitor service units to consume exactly
`${TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION}`. Extend the updater harness
to prove main, diagnostic, and test-notification units are backed up, installed,
daemon-reloaded, and all restored on any unit-install/start/health failure.

**Step 2: Verify RED**

Run the exact static/unit/updater tests and require failure on the two hardcoded
oneshots and missing updater artifacts.

**Step 3: Implement minimal GREEN**

Replace the two hardcoded enabled flags. Extend the fixed-target monitor artifact
transaction to the three reviewed service paths only; no glob, operator path, or
new production mutation is introduced. Preserve root ownership/modes, secret
redaction, timer state, env rollback, and application rollback ordering.

**Step 4: Verify GREEN**

Run monitor installation and updater focused tests plus Bash syntax checks.

### Task 4: Make the restore watermark the only future-execution boundary

**Files:**

- Modify: `docs/runbooks/deepcoin-contract-cache-ownership-repair.md`
- Modify: `tests/test_server_update_scripts.py`

**Step 1: Write RED**

Require the runbook to record `freeze_raw_message_id` as a bounded freeze
evidence marker and a separate `restore_raw_message_id = MAX(raw_messages.id)`
immediately before enabling. Require explicit language that only IDs strictly
greater than `restore_raw_message_id` are future signals and every message at or
below it remains terminal with zero replay/zero backfill.

**Step 2: Verify RED**

Run the documentation contract test and require failure on the old
freeze-watermark wording.

**Step 3: Implement minimal GREEN**

Correct only the runbook boundary. Do not add replay code, queue mutation, or
trading behavior.

**Step 4: Verify GREEN**

Run the documentation contract tests and `git diff --check`.

### Task 5: Final local candidate verification and handoff

**Files:**

- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1:** Run all affected focused tests and static syntax checks.

**Step 2:** After the last production-code edit, run one final complete suite.

**Step 3:** Commit implementation/test/docs paths explicitly; never use
`git add -A`.

**Step 4:** Update canonical status with the old candidate rejection, new
candidate content SHA, test evidence, macOS/Linux skips, and all remaining
production authorization gates. Commit the status by explicit path only.

**Step 5:** Recheck exact HEAD, branch, clean tree, and changed path set. The next
step remains an independent read-only Task 11 review; push requires separate
authorization.
