# Four- and Five-Stage Take-Profit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely preserve and execute one through five source take-profit targets for Deepcoin split positions, with deterministic allocation and fail-closed exact-position reconciliation.

**Architecture:** Add a pure `take_profit_plan` module as the sole source for target ordering, allocation selection, and step-aware quantity splitting. Make the draft builder, post-fill protection builder, and trigger TP convergence executor call it so no entry path can silently cap targets at three or calculate a conflicting quantity split.

**Tech Stack:** Python 3.12, Decimal, SQLAlchemy, FastAPI, pytest, Deepcoin REST client.

---

### Task 1: Add pure target and quantity planner

**Files:**
- Create: `src/telegram_kol_research/take_profit_plan.py`
- Create: `tests/test_take_profit_plan.py`

**Step 1: Write failing unit tests**

Cover a pure API such as `build_take_profit_plan(prices, side, configured_allocations, quantity=None, quantity_step=None, minimum_quantity=None)`:

```python
def test_four_targets_default_to_front_loaded_equal_remainder():
    plan = build_take_profit_plan(
        prices=[67500, 68500, 69500, 70500], side="long",
        configured_allocations=[50, 30, 20],
    )
    assert [(leg.price, leg.allocation_pct) for leg in plan.legs] == [
        ("67500", "40"), ("68500", "20"),
        ("69500", "20"), ("70500", "20"),
    ]

def test_five_short_targets_are_nearest_first_and_use_40_15_default():
    plan = build_take_profit_plan(
        prices=[64200, 65250, 64750, 65150, 63800], side="short",
        configured_allocations=[50, 30, 20],
    )
    assert [leg.price for leg in plan.legs] == ["65250", "65150", "64750", "64200", "63800"]
    assert [leg.allocation_pct for leg in plan.legs] == ["40", "15", "15", "15", "15"]
```

Also cover: one/two/three target legacy behavior; exact-length custom four/five allocation override; reject duplicate/non-positive/more-than-five prices; BTC integer `25` with 4-stage `40/20/20/20` -> `10/5/5/5`; ETH `2.4` with `0.1` step -> `1.0/0.5/0.5/0.4`; last leg receives the exact remainder; and an undersized position fails rather than dropping a target.

**Step 2: Run the new tests and verify RED**

Run: `uv run pytest -q tests/test_take_profit_plan.py`

Expected: import failure because the planner does not exist.

**Step 3: Implement the smallest planner**

Create immutable leg/result dataclasses and `TakeProfitPlanError`. Use `Decimal` only. Requirements:

- normalize and de-duplicate prices;
- order long ascending and short descending;
- limit to five only by rejecting the sixth target, never slicing;
- use a configured allocation list only when it has exactly `count` positive values and normalizes to 100;
- default to 100, 50/50, the valid configured 3-target setting or 40/30/30 fallback, 40/20/20/20, and 40/15/15/15/15;
- quantize every non-final quantity down to `quantity_step`, place the exact remaining stepped quantity in the final leg, and validate each leg against `minimum_quantity`.

**Step 4: Run focused tests and commit**

Run: `uv run pytest -q tests/test_take_profit_plan.py`

Expected: PASS.

```bash
git add src/telegram_kol_research/take_profit_plan.py tests/test_take_profit_plan.py
git commit -m "feat: plan up to five take-profit stages"
```

### Task 2: Preserve all targets in order drafts and settings

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:91-100,353-375,621-644`
- Modify: `src/telegram_kol_research/trading_settings.py:33,137-181`
- Modify: `src/telegram_kol_research/web_app.py` (trading-settings validation endpoint, if it enforces allocation shape)
- Modify: `src/telegram_kol_research/templates/index.html:357`
- Test: `tests/test_deepcoin_order_builder.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_web_app.py`

**Step 1: Add failing draft/settings tests**

Add long and short four/five-target draft tests that assert every source target remains in `take_profit_legs`, in the correct exit order, with the selected allocation. Add a setting round-trip test for `40,20,20,20` and `40,15,15,15,15`, and rejection tests for zero, more than five, or malformed allocation entries.

**Step 2: Run RED tests**

Run: `uv run pytest -q tests/test_deepcoin_order_builder.py tests/test_trading_settings.py tests/test_web_app.py -k 'take_profit or allocation'`

Expected: failures proving the builder retains only three targets and settings lack the new limit checks.

**Step 3: Wire the shared planner into draft construction**

- Replace `_order_take_profit_prices(... )[:3]` and `_normalize_take_profit_allocations` with the pure planner.
- Store all returned legs in `take_profit_legs`, retaining price, allocation, and `market_on_trigger` metadata.
- Keep existing one/two/three target output byte-for-byte compatible when their configured allocations are valid.
- Make settings accept one through five positive allocation entries; the UI label should describe that a matching count overrides defaults, and that 4/5 stages default to 40% TP1 with equal remainder.

**Step 4: Run focused checks and commit**

Run: `uv run pytest -q tests/test_deepcoin_order_builder.py tests/test_trading_settings.py tests/test_web_app.py`

Expected: PASS.

```bash
git add src/telegram_kol_research/deepcoin_order_builder.py src/telegram_kol_research/trading_settings.py src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/index.html tests/test_deepcoin_order_builder.py tests/test_trading_settings.py tests/test_web_app.py
git commit -m "feat: retain four and five take-profit targets"
```

### Task 3: Use exact step-aware quantities after every fill

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py:827-852,1720-1778`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Add failing post-fill payload tests**

Create four- and five-stage split-position drafts with a verified `posId`. Assert generated `set-position-sltp` payloads include one full-position stop plus every TP target, their `sz` sum exactly to the position, and ETH decimal-step allocation is precise. Add an undersized-position case that raises `RecoveryLiveSubmitError` before any payload is returned.

**Step 2: Run RED test**

Run: `uv run pytest -q tests/test_recovery_live_submit.py -k 'four_stage or five_stage or undersized'`

Expected: failure due duplicated float splitter or missing target support.

**Step 3: Delegate quantity work to the shared planner**

Replace `_split_quantity_by_allocations` use with `take_profit_plan` quantity allocation. Pass the draft's verified `quantity_step` and contract minimum. Preserve stop-first payload ordering and the requirement for exact `posId` in split mode.

**Step 4: Run tests and commit**

Run: `uv run pytest -q tests/test_recovery_live_submit.py`

Expected: PASS.

```bash
git add src/telegram_kol_research/recovery_live_submit.py tests/test_recovery_live_submit.py
git commit -m "fix: allocate post-fill take profits by contract step"
```

### Task 4: Execute and record 4/5-stage trigger TP convergence

**Files:**
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py:240-385`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py:101-120` (only if target-count validation is needed)
- Test: `tests/test_trigger_take_profit_convergence_executor.py`
- Test: `tests/test_trigger_take_profit_convergence.py`

**Step 1: Add failing convergence tests**

Extend the ready-convergence helper with contract step/minimum metadata. Assert that a four-stage BTC plan produces four `set_position_sltp` payloads with `10/5/5/5`, and that a five-stage ETH plan produces five decimal quantities whose sum is the exact live position. Assert all submitted order IDs are persisted in `PositionTakeProfitOrder`.

Add safety tests for: a position too small for all stages (blocked before exchange call); a response missing an order ID (frozen after exactly one call); and a pending TP appearing between plan and submit (conflicted with no cancellation/retry).

**Step 2: Run RED tests**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py tests/test_trigger_take_profit_convergence.py -k 'four or five or step or undersized'`

Expected: failures because `_allocate_sizes` only rounds whole units and cannot validate contract steps.

**Step 3: Replace local integer allocation**

- Route normalized desired targets and verified live position quantity through `take_profit_plan`.
- Obtain contract step/minimum from the same verified contract specification source used by the entry draft; explicitly block if unavailable.
- Preserve exact-pos, exact-side, split-mode, primary-stop verification, durable reservation, immediate per-order persistence, and freeze-on-unknown-response behavior.
- Do not add automatic cancellation of existing TP rows in this initial-trigger path; retain its current conflict behavior. A management replacement remains an explicitly authorized workflow.

**Step 4: Run focused tests and commit**

Run: `uv run pytest -q tests/test_trigger_take_profit_convergence_executor.py tests/test_trigger_take_profit_convergence.py tests/test_execution_bindings.py`

Expected: PASS.

```bash
git add src/telegram_kol_research/trigger_take_profit_convergence_executor.py src/telegram_kol_research/trigger_take_profit_convergence.py tests/test_trigger_take_profit_convergence_executor.py tests/test_trigger_take_profit_convergence.py tests/test_execution_bindings.py
git commit -m "feat: converge up to five exact-position take profits"
```

### Task 5: Add a separately authorized safe TP replacement workflow

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:145-280`
- Modify: `src/telegram_kol_research/models.py` (only if a durable TP replacement reservation model is absent)
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Add failing management-path tests**

Cover a verified position with an old combined TP/SL order. Assert the action first creates and re-reads a replacement primary stop, cancels only the verified old protection row, then creates all replacement TP legs. Assert it aborts before cancellation when backup/primary stop verification fails, and freezes after any uncertain exchange response.

**Step 2: Run RED test**

Run: `uv run pytest -q tests/test_deepcoin_execution_actions.py -k 'multi_stage_take_profit_replacement'`

Expected: failure because the current adjustment path is scalar TP/SL oriented.

**Step 3: Implement explicit replacement action**

Add a distinct management action (for example `replace_position_take_profits`) that accepts a complete immutable target plan. It must require verified exact ownership, a current exchange preflight, an active backup stop, a durable reservation, stop-first replacement for any combined TP/SL order, exact cancellation verification, per-stage TP creation/persistence, and final read-back. It must never run from the normal trigger-entry reconciliation loop.

**Step 4: Run tests and commit**

Run: `uv run pytest -q tests/test_deepcoin_execution_actions.py tests/test_position_take_profit_orders.py`

Expected: PASS.

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/models.py tests/test_deepcoin_execution_actions.py tests/test_position_take_profit_orders.py
git commit -m "feat: replace staged take profits with verified protection"
```

### Task 6: Full regression, review, and controlled rollout

**Files:**
- Modify: `docs/plans/2026-07-24-multi-stage-take-profit-design.md` (only if tests reveal a design clarification)
- Modify: `docs/plans/2026-07-24-multi-stage-take-profit.md` (record deviations only)

**Step 1: Run local full suite**

Run: `uv run pytest -q`

Expected: PASS.

**Step 2: Inspect scope and review safety invariants**

Run: `git diff --check` and `git status --short`

Verify user changes (`uv.lock` and unrelated untracked inspection artifacts) are not staged. Review that no path can submit a target count greater than five, omit a target silently, operate without exact `posId`, or cancel a stop-bearing order before a replacement stop is confirmed.

**Step 3: Commit documentation if changed and push**

```bash
git add docs/plans/2026-07-24-multi-stage-take-profit-design.md docs/plans/2026-07-24-multi-stage-take-profit.md
git commit -m "docs: record multi-stage take-profit rollout"
git push origin codex/deepcoin-auto-trading-v1
```

**Step 4: Production verification without new orders**

Use `scripts/server_git_update.ps1` after push. On the server, verify service health, run read-only draft/ledger/pending-order parity checks, and inspect logs for validation/freeze incidents. Do not test by creating, cancelling, or modifying a real order. Enable the new behavior only for future trigger entries after this read-only verification and explicit authorization.
