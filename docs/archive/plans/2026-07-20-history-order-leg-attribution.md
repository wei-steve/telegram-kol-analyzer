# History Order Leg Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the positions page history-order cards prefer verified entry-leg `pos_id` evidence before falling back to weak candidate attribution.

**Architecture:** Keep `execution_order_legs` as the strong ownership source for Deepcoin position identity. The Web exchange-order annotation now checks verified entry legs whose `pos_id` equals the rendered order ID, while preserving protection-ledger priority for TPSL rows and keeping candidate scoring as the final display-only fallback.

**Tech Stack:** FastAPI, Jinja templates, SQLAlchemy, SQLite, pytest.

---

### Task 1: Capture The Regression

**Files:**
- Test: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

Add a positions-panel test with:
- one history order whose `ordId` equals a verified `execution_order_legs.pos_id`;
- a binding whose `order_id` is a trigger parent, so the previous direct binding lookup cannot match;
- a wrong strategy candidate with a better price-score match.

Expected behavior: the card renders the verified leg group and does not display `可能归属`.

**Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_page_render.py::test_exchange_history_order_uses_verified_entry_leg_pos_id_before_candidates -q
```

Expected: FAIL before implementation because the order falls through to candidate scoring.

### Task 2: Prefer Verified Entry-Leg Position Evidence

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Add a verified leg lookup**

Inside `_attach_exchange_order_bindings`, query `ExecutionOrderLeg` joined to `ExecutionBinding` for:
- `venue == "deepcoin"`;
- `purpose == "entry"`;
- `attribution_status == "verified"`;
- `pos_id` in the rendered order/client IDs.

Build a `pos_id -> (leg, binding)` index only for rows whose existing `_persisted_position_attribution` renders as `state == "bound"`. Treat duplicate `pos_id` matches as ambiguous and do not assign them.

**Step 2: Preserve existing priority**

For every exchange order row:
- keep verified `position_protection_ledger` attribution first;
- keep direct `execution_bindings.order_id/client_order_id` attribution next;
- use verified entry-leg `pos_id` attribution only for non-TPSL rows with no prior strong match;
- fall through to candidate scoring only when no strong evidence exists.

**Step 3: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_web_page_render.py::test_exchange_history_order_uses_verified_entry_leg_pos_id_before_candidates \
  tests/test_web_page_render.py::test_exchange_current_tpsl_order_without_ledger_is_not_candidate_attributed \
  -q
```

Expected: PASS. The second test proves unverified TPSL rows still fail closed to `保护归属未验证`.

### Task 3: Broader Verification

**Files:**
- Verify: `tests/test_web_page_render.py`
- Verify: `tests/test_web_app.py`

**Step 1: Run related Web tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_page_render.py tests/test_web_app.py -q
```

Expected: PASS or only pre-existing unrelated failures. Do not deploy from local tests alone.

**Step 2: Production boundary**

This change is local until committed, pushed, and deployed through the existing GitHub -> server pull/restart workflow. Production verification after deployment should read `/positions-panel` and confirm the ETH history orders with verified leg `pos_id` render as verified ownership instead of weak `可能归属`.
