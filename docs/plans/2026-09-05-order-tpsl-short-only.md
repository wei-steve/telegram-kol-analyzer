# ETH Short-only Ordinary Order Experiment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a one-shot user-run experiment that submits only one minimum-size ETH short ordinary limit order with attached TP/SL.

**Architecture:** Reuse the existing ordinary-order evidence collector and safety boundaries, but select only the short request before validation and submission. Install the reviewed scripts into a new server evidence directory so the earlier one-shot marker remains untouched.

**Tech Stack:** Python 3, pytest, Deepcoin REST API, SSH static installation.

---

### Task 1: Specify short-only request behavior

**Files:**
- Modify: `tests/test_deepcoin_order_tpsl_experiment.py`
- Modify: `scripts/deepcoin_order_tpsl_experiment.py`

**Step 1: Write the failing test**

Add a test requiring a short-only manifest to contain exactly one `sell`/`short` request at market reference plus 1, with TP minus 10 and SL plus 10.

**Step 2: Run test to verify it fails**

Run the focused pytest node and require failure because short-only selection is absent.

**Step 3: Implement the minimal code**

Add explicit short-only manifest validation and one-call submission. Keep the existing pair mode for evidence compatibility.

**Step 4: Run test to verify it passes**

Run the focused test, then all three Deepcoin experiment test files.

### Task 2: Add a separate one-shot command

**Files:**
- Modify: `tests/test_deepcoin_order_tpsl_experiment.py`
- Modify: `scripts/deepcoin_order_tpsl_experiment.py`

**Step 1: Write the failing test**

Require `--execute-order-tpsl-short` to select exactly one short request while no flag remains read-only.

**Step 2: Run test to verify it fails**

Require failure because the new CLI flag is absent.

**Step 3: Implement the minimal code**

Parameterize the live runner by mode, adjust accepted-count checks and terminal labels, and retain the existing no-retry, exact cleanup and position preflight behavior.

**Step 4: Run test to verify it passes**

Run focused and full experiment tests.

### Task 3: Install without executing

**Files:**
- Install: `/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-test-20260905/`

**Step 1: Validate syntax and hashes locally.**

**Step 2: Copy the three standalone scripts to a new mode-0700 server directory.**

**Step 3: Verify remote hashes and AST parsing only.**

**Step 4: Give the user the single explicit short-only command.**
