# Deepcoin Triggered-Limit Lineage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Attribute a delayed-fill Deepcoin position to its exact stored Conditional entry leg through the generated regular limit order, without relying on generated-order `clOrdId` or crossing chat/strategy boundaries.

**Architecture:** Add a pure global lineage resolver that joins exact trigger history to regular order history using normalized trigger/create time and immutable economics, accepting only mutual-unique leg/order pairs. Feed only filled resolved regular orders into the existing position-attribution engine, where `regular ordId == posId` provides the final exact position identity; augment persisted evidence with the complete multi-hop chain. Keep all errors and ambiguity fail closed and preserve manual/terminal states.

**Tech Stack:** Python 3.14, SQLAlchemy, SQLite, Decimal, pytest, existing Deepcoin reconciliation snapshot and position-attribution engine.

---

### Task 1: Build a pure mutual-unique trigger-to-regular resolver

**Files:**
- Create: `src/telegram_kol_research/triggered_limit_lineage.py`
- Create: `tests/test_triggered_limit_lineage.py`

**Step 1: Write failing production-shape tests**

Create fixtures matching the verified Deepcoin shape:

```python
trigger = {
    "ordId": "1001124101153303",
    "instId": "BTC-USDT-SWAP",
    "side": "sell",
    "posSide": "short",
    "ordType": "Conditional",
    "sz": "7",
    "px": "65100",
    "triggerTime": "1784067708",
    "errorCode": "0",
}
regular = {
    "ordId": "1001124106836368",
    "clOrdId": "",
    "tag": "",
    "instId": "BTC-USDT-SWAP",
    "side": "sell",
    "posSide": "short",
    "ordType": "limit",
    "sz": "7",
    "px": "65100",
    "cTime": "1784067708000",
    "fillTime": "1784097189000",
    "state": "filled",
}
```

Cover:

- seconds trigger time matching millisecond regular creation time;
- empty generated `clOrdId` and `tag`;
- an eight-hour delayed fill still resolving because fill latency is unbounded;
- an unfilled regular order resolving lineage but not producing filled evidence;
- unsuccessful trigger, malformed time, incompatible instrument/side/size/price;
- two equal regular candidates remaining ambiguous;
- two legs from different chats/strategy instances competing for one candidate;
- one regular order claimed by two legs;
- stable results independent of input order.

Represent inputs with immutable dataclasses carrying `leg_id`, `binding_id`,
`chat_id`, `strategy_instance_id`, trigger order ID/client ID, instrument, side,
size, and normalized entry price. Return explicit resolved, ambiguous, and
unresolved collections; never select by list order.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_triggered_limit_lineage.py
```

Expected: FAIL because `triggered_limit_lineage` does not exist.

**Step 3: Implement normalized matching**

Implement Decimal-safe normalization and a timestamp helper that converts
seconds or milliseconds to milliseconds. Use a small creation-time precision
tolerance (at most 1,500 ms) only between `triggerTime` and regular-order
`cTime`; do not compare trigger time to fill/position creation time.

Build all compatible leg-to-regular edges, then accept only mutual-unique pairs:

```python
leg_best = unique_candidate_by_leg(edges)
order_best = unique_candidate_by_regular_order(edges)
accepted = edge where leg_best[leg_id] == edge == order_best[regular_order_id]
```

Do not use `clOrdId`, `tag`, fill latency, or cross-chat aliases as a fallback.
Return the exact trigger order ID, generated regular order ID, normalized
creation time, fill state/time, and matched economic fields for audit evidence.

**Step 4: Run GREEN and property tests**

```bash
.venv/bin/pytest -q tests/test_triggered_limit_lineage.py
```

Expected: PASS. Add a bounded permutation/property loop proving input-order
independence and absence of cross-pair selection.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/triggered_limit_lineage.py \
  tests/test_triggered_limit_lineage.py
git commit -m "feat: resolve triggered regular order lineage"
```

### Task 2: Integrate filled lineage into coherent reconciliation

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:280-650`
- Modify: `src/telegram_kol_research/position_attribution.py:1210-1310`
- Modify: `tests/test_execution_bindings.py`
- Modify: `tests/test_position_attribution.py`

**Step 1: Write failing reconciliation tests**

Cover the full production chain:

```text
leg.order_id = trigger ordId
trigger history triggerTime = T
regular order cTime = T, fillTime = T + 8h, ordId = P
live position posId = P, cTime = fillTime
```

Require:

- the leg becomes `active/verified` with `pos_id=P`;
- evidence records `triggered_regular_order_lineage`, trigger order ID, regular
  order ID, trigger/create timestamps, and empty generated client metadata;
- the binding and lifecycle derive active state only after the live position is
  present;
- unfilled regular order creates no owner;
- ambiguous regular candidates mark affected legs conflict and block the whole
  component;
- a snapshot order-history error yields `evidence_unavailable`;
- a `posId` already owned by another leg cannot be reassigned;
- terminal/manual-close legs and bindings remain terminal;
- different chats and strategy instances never exchange evidence;
- repeated reconciliation is idempotent;
- the fake client observes read methods only and no exchange write method.

**Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/test_execution_bindings.py \
  tests/test_position_attribution.py -k 'triggered_regular or delayed_fill'
```

Expected: FAIL because reconciliation still links trigger time directly to
position creation time.

**Step 3: Produce derived fill evidence once per snapshot**

In `_apply_reconcile_snapshot`, build lineage inputs for all nonterminal
`trigger_limit` entry legs and call the pure resolver with the snapshot's
trigger and regular order history. For each mutual-unique filled result, create
a derived `FillEvidence` whose regular `order_id` is the eventual exact
`posId`, whose leg identity is carried only after lineage has been proven, and
whose source is `triggered_regular_order`.

Do not mutate exchange rows and do not synthesize evidence for an unfilled
regular order. Merge lineage conflicts into the existing global conflict set.

Update position attribution so this prevalidated evidence may use
`regular order_id == position.pos_id` without the trigger-to-position five-second
rule. Preserve that rule for legacy direct `trigger_fill` evidence.

Before `_transition_leg_attribution`, augment the accepted evidence with the
complete lineage dictionary. Keep the existing ownership uniqueness check as a
second guard.

**Step 4: Run GREEN and adjacent tests**

```bash
.venv/bin/pytest -q tests/test_triggered_limit_lineage.py \
  tests/test_execution_bindings.py tests/test_position_attribution.py
.venv/bin/pytest -q tests/test_strategy_management_planner.py \
  tests/test_deepcoin_execution_actions.py tests/test_position_attribution_repair.py
```

Expected: PASS. Confirm current direct order, direct `posId`, legacy trigger,
manual bind, equivalent-permutation, and manual-close tests remain green.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/position_attribution.py \
  tests/test_execution_bindings.py tests/test_position_attribution.py
git commit -m "fix: attribute delayed triggered limit fills"
```

### Task 3: Document, verify, and hand back to strategy management

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/deepcoin-order-management.md`
- Modify: `.superpowers/sdd/progress.md`

**Step 1: Document the verified Deepcoin behavior**

Record that Conditional limit orders may generate regular orders with empty
`clOrdId/tag`, that the regular order can rest for hours after trigger, and that
safe ownership uses trigger-to-regular creation identity followed by
`regular ordId == posId`. Explicitly prohibit widening the direct
trigger-to-position time window and cross-chat inference.

**Step 2: Run full verification**

```bash
git diff --check
.venv/bin/python -m compileall -q src
.venv/bin/pytest -q
```

Expected: all tests pass with only the recorded pre-existing warnings.

**Step 3: Run an independent review**

Review the complete range from `b28f551` through the implementation head.
Require no Critical, Important, or Minor findings before continuing. Pay
special attention to global mutual uniqueness, terminal-state preservation,
read-only exchange behavior, timestamp units, and cross-chat isolation.

**Step 4: Record the checkpoint**

Append the reviewed commit range and test result to
`.superpowers/sdd/progress.md`. Do not deploy and do not apply historical
position ownership during this task.

**Step 5: Resume Task 7**

Regenerate the Task 7 brief from
`docs/plans/2026-07-15-strategy-management-batches.md` and continue the existing
subagent-driven implementation. Task 7 may consume only currently verified live
entry legs; it does not loosen attribution rules.
