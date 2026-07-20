# Trigger Child Position Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Verify and repair Deepcoin trigger-entry attribution when a long-waiting parent trigger order fills into a child order whose `ordId` becomes the live `posId`.

**Architecture:** Keep `execution_order_legs` as the position ownership authority. During read-only reconciliation, bridge a successful trigger-history row for the parent entry order to exactly one regular order-history child row with matching instrument, side, price, size, and exchange fill timestamp, then expose that child `ordId` as direct `pos_id` fill evidence for the parent leg. Do not infer ownership from group, symbol, side, or pending-leg similarity alone.

**Tech Stack:** Python, SQLAlchemy models, Deepcoin REST read APIs, pytest.

---

### Task 1: Reproduce The Delayed Position-Time Bug

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Reference: `src/telegram_kol_research/execution_bindings.py`

**Step 1: Write the failing test**

Add a reconciliation test with:

- one trigger entry leg with parent `order_id`;
- Deepcoin trigger history for that parent with `triggerTime` in seconds and `uTime` in milliseconds;
- Deepcoin order history child row with `ordId == live posId`, matching price and size, and `cTime == parent uTime`;
- live position `cTime` delayed by 47 seconds.

Expected result: the leg becomes `active`, `verified`, and receives the child `posId`.

**Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_execution_bindings.py::test_reconcile_links_delayed_live_position_through_trigger_child_order_history -q
```

Expected: FAIL because current attribution compares trigger evidence directly to live position time and does not bridge child order history.

### Task 2: Bridge Parent Trigger History To Child Order History

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Implement the minimal helper**

Inside snapshot fill-evidence construction:

1. Keep existing direct regular-order, trade-fill, and trigger-history evidence.
2. For each successful trigger-history row that matches exactly one entry leg, search `snapshot.order_history` for child filled rows where:
   - `instId` matches;
   - normalized side matches;
   - `fillSz`/`sz` matches trigger `sz`;
   - `fillPx`/`avgPx`/`px` matches trigger `px`;
   - order-history `cTime`/`fillTime`/`uTime` equals trigger `uTime`/`triggerTime` after seconds-to-milliseconds normalization.
3. Only when exactly one child row matches, append `FillEvidence` for the parent leg with `order_id` equal to parent trigger order id and `pos_id` equal to child `ordId`.

**Step 2: Run the focused test**

Run:

```bash
pytest tests/test_execution_bindings.py::test_reconcile_links_delayed_live_position_through_trigger_child_order_history -q
```

Expected: PASS.

### Task 3: Guard Against Ambiguous Child Rows

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py` only if needed

**Step 1: Add ambiguity regression**

Add a test with two order-history child rows matching the same parent trigger fill. Expected: no assignment and the live position remains unverified.

**Step 2: Run focused tests**

Run:

```bash
pytest tests/test_execution_bindings.py::test_reconcile_links_delayed_live_position_through_trigger_child_order_history tests/test_execution_bindings.py::test_reconcile_does_not_link_trigger_child_when_order_history_is_ambiguous -q
```

Expected: both PASS.

### Task 4: Verify The Relevant Suite

Run:

```bash
pytest tests/test_execution_bindings.py tests/test_position_attribution.py -q
```

Expected: PASS.

### Task 5: Record The Operational Pitfall

**Files:**
- Create: `/Users/steven/.codex/memories/extensions/ad_hoc/notes/<timestamp>-deepcoin-trigger-child-attribution.md`

Record:

- Deepcoin parent trigger history may use parent `ordId`, while child regular order `ordId` becomes live `posId`.
- Live position `cTime` can lag child order-history `cTime`; do not use live position time as the only hard link.
- `triggerTime` may be seconds while `uTime/cTime` are milliseconds; always use project timestamp normalization before comparison.
- Production repair remains dry-run first and apply only with exact fingerprint.
