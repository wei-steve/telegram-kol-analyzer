# Deepcoin Exact-Target History Repair Implementation Plan

> **For Codex:** Execute with test-driven development and request an independent
> exact-diff review before the final full-suite run.

**Goal:** Make manual-cleanup reconciliation depend only on complete flat-account
snapshots and per-canonical-order history/fill evidence.

**Architecture:** Preserve the shared maintenance evidence defaults, add an
explicit query selection used only by manual reconciliation, add one exact
trigger-history client endpoint, and bind exact-history observations into the
dry-run/apply plan fingerprint.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLite, pytest.

---

### Task 1: Establish RED evidence-profile tests

**Files:**
- Modify: `tests/test_deepcoin_maintenance_evidence.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

1. Add a test proving the default evidence profile still reads broad history
   and fills.
2. Add a manual-reconciliation test whose broad history/fills readers fail if
   called, while exact target history/fills remain empty.
3. Assert one exact history and one exact fills call per canonical target, with
   no retry.
4. Run the two test modules and verify RED for the missing profile and exact
   history behavior.

### Task 2: Establish RED exact-history semantics

**Files:**
- Modify: `tests/test_deepcoin_client.py`
- Modify: `tests/test_manual_pending_entry_reconciliation.py`

1. Require the REST client to request trigger history with exact `instId` and
   `ordId` query parameters.
2. Parameterize accepted zero-row and nonliteral-state results.
3. Parameterize malformed, exception, duplicate, identity-conflict,
   instrument-mismatch, active/executed/filled, and fill-quantity blockers.
4. Prove an exact-history content change changes the plan fingerprint.
5. Run the focused tests and verify RED.

### Task 3: Implement the minimum production change

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `src/telegram_kol_research/deepcoin_maintenance_evidence.py`
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py`

1. Add `list_trigger_order_history_by_order_id(inst_id, order_id)` to the
   client protocol and REST implementation.
2. Add an explicit immutable query selection to the maintenance evidence
   builder; keep its default selection unchanged.
3. Select only positions, regular, and pending queries in manual cleanup.
4. Read and validate exact history and exact fills once per reviewed target.
5. Include canonical exact-history evidence in the plan fingerprint.
6. Run affected tests to GREEN.

### Task 4: Review, final verification, and local handoff

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

1. Run `git diff --check` and focused tests.
2. Request an independent code review of the exact base-to-HEAD diff and repair
   every P0/P1 finding using a new RED→GREEN cycle.
3. Run one final full test suite after the last production-code edit.
4. Update the status document with the local proof, exact commit, and the still
   outstanding fresh production restage/cutover.
5. Stage explicit paths only, verify the staged path list, and create one local
   commit. Do not push or stage a production release.

