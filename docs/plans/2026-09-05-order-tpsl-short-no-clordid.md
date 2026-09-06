# ETH Short Order Without clOrdId Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prepare a one-shot ETH minimum-size short ordinary-order experiment that removes only `clOrdId` from the previously rejected request.

**Architecture:** Extend the existing experiment with an explicit `short_no_clordid` selection. Reuse the same quote, size, entry, TP/SL, no-retry, exact cleanup and evidence flow, while querying accepted orders only by exchange `ordId` because no client ID is sent.

**Tech Stack:** Python 3, pytest, Deepcoin REST API, SSH static installation.

---

### Task 1: Specify the single-variable manifest

**Files:**
- Modify: `tests/test_deepcoin_order_tpsl_experiment.py`
- Modify: `scripts/deepcoin_order_tpsl_experiment.py`

**Step 1:** Add a failing test requiring one short request with no `clOrdId` and unchanged price, size, TP and SL.

**Step 2:** Run the test and require failure because the selection is unsupported.

**Step 3:** Add the explicit manifest selection and validation.

**Step 4:** Run the focused test and require PASS.

### Task 2: Specify the one-shot CLI and readback

**Files:**
- Modify: `tests/test_deepcoin_order_tpsl_experiment.py`
- Modify: `scripts/deepcoin_order_tpsl_experiment.py`

**Step 1:** Add failing tests for `--execute-order-tpsl-short-no-clordid` and for an empty client-ID readback list.

**Step 2:** Run the tests and require failure for the missing flag or direct-key access.

**Step 3:** Route the new flag to `short_no_clordid`; retain accepted `ordId` observation and exact cancellation.

**Step 4:** Run all focused Deepcoin experiment tests.

### Task 3: Install a fresh one-shot package

**Files:**
- Install: `/var/lib/telegram-kol-cutover-evidence/eth-order-tpsl-short-no-clordid-test-20260905/`

**Step 1:** Verify local hashes and syntax.

**Step 2:** Create a new server directory so prior one-shot markers remain immutable.

**Step 3:** Copy scripts and verify remote hashes plus AST parsing without executing trading.

**Step 4:** Give the user one explicit command.
