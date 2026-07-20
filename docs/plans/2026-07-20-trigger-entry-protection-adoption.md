# Trigger Entry Protection Adoption Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist a safe `posId` to TPSL `ordId` ownership record after a Deepcoin trigger entry fills, so later position-management batches can mutate only exact verified protection orders.

**Architecture:** Keep embedded TP/SL on trigger entries as the immediate exchange-side guard. After normal reconciliation has verified the exact trigger-entry leg and `posId`, adopt a pending TPSL order only when one globally unique row exactly matches the recorded entry request; write the existing protection ledger, never an exchange mutation. Rejections remain durable, observable, and fail closed. Historical repair shares the same matcher but requires an explicit fingerprinted apply.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, existing Deepcoin REST/read-only clients.

---

### Task 1: Make trigger-entry evidence claims truthful and strict

**Files:**
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`

**Step 1: Write failing tests for the exact candidate set**

Add fixtures with one verified trigger-limit entry leg and a pending TPSL row that
has no `posId`, but exactly matches the recorded `instId`, `posSide`, full
quantity, TP, and SL. Assert that the result is one combined action and that
the evidence says `trigger_entry_unique_expected_protection_shape`.

Add two refusals:

```python
def test_trigger_entry_repair_refuses_duplicate_expected_protection_shape(...):
    # Two distinct TPSL ordIds match the same entry request.
    assert plan.actions == ()
    assert plan.refusals[0].reason == "trigger_entry_tpsl_not_unique"


def test_trigger_entry_repair_does_not_claim_time_match_without_time_bound(...):
    plan = build_entry_protection_ledger_repair_plan(..., include_trigger_entries=True)
    assert plan.actions[0].evidence["match"] == (
        "trigger_entry_unique_expected_protection_shape"
    )
    assert "exchange_order_created_at" not in plan.actions[0].evidence
```

**Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py -k trigger_entry -q
```

Expected: the evidence-name assertion fails because the current implementation
claims `trigger_entry_unique_size_time_tpsl` without checking a time bound.

**Step 3: Implement the smallest matcher correction**

Rename the positive evidence to `trigger_entry_unique_expected_protection_shape`.
Do not include the pending row timestamp as positive match evidence unless a
future, documented and tested calibration rule is added. Keep the existing
mandatory checks for verified trigger leg, exact parent order/client order,
instrument, side, full TP/SL set, full size, and globally unique `ordId`.

**Step 4: Run the focused tests and confirm GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_entry_protection_ledger_repair.py src/telegram_kol_research/entry_protection_ledger_repair.py
git commit -m "fix: tighten trigger protection repair evidence"
```

### Task 2: Extract a reusable no-write trigger-protection adoption planner

**Files:**
- Modify: `tests/test_entry_protection_ledger_repair.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`

**Step 1: Write failing unit tests for one immutable adoption result**

Introduce a focused helper that accepts a verified trigger-entry leg, its
create-trigger-entry event, and an already-captured pending TPSL snapshot.
Assert that it returns either exactly one `EntryProtectionLedgerRepairAction`
or one explicit refusal. The helper must not call a Deepcoin client, mutate a
session, or query by symbol/side alone.

```python
result = plan_verified_trigger_entry_protection_adoption(
    session,
    entry_leg=entry_leg,
    event=entry_event,
    pending_tpsl_rows=pending_rows,
    existing_order_ids=set(),
)
assert result.action.pos_id == "pos-1"
assert result.action.order_id == "tpsl-1"
```

Add cases for missing `ordId`, a row containing a contradictory `posId`, a
partial-size row, and an already verified ledger order. Each must refuse or
return a no-op; none may fall back to group, KOL, message text, or price alone.

**Step 2: Run the new tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_entry_protection_ledger_repair.py -k adoption -q
```

Expected: FAIL because the reusable helper does not exist.

**Step 3: Refactor the existing repair path to use the helper**

Move the body of `_plan_trigger_entry_repair` into the helper. The CLI repair
planner becomes a thin adapter that obtains one pending snapshot per
instrument, invokes the helper, and preserves the existing fingerprinted
apply behavior.

**Step 4: Run the repair suite and adjacent planner suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_strategy_management_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_entry_protection_ledger_repair.py src/telegram_kol_research/entry_protection_ledger_repair.py
git commit -m "refactor: reuse trigger protection adoption matcher"
```

### Task 3: Adopt only verified trigger-entry protection during reconciliation

**Files:**
- Modify: `tests/test_execution_bindings.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/entry_protection_ledger_repair.py`

**Step 1: Write failing reconciliation tests**

Extend the existing fake Deepcoin reconciliation client with a pending TPSL
snapshot. Seed a trigger-limit entry, then let reconciliation establish an
exact verified `posId`. Assert that reconciliation creates the combined ledger
row only for the unique exact candidate.

Add a duplicate candidate test:

```python
result = reconcile_deepcoin_execution_bindings(...)
assert load_verified_ledger_rows(...) == []
assert result.protection_adoption_refused == 1
```

The result counter name may differ, but it must distinguish adopted, refused,
and unavailable snapshot states without treating a refusal as zero exposure.

**Step 2: Run the reconciliation tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_execution_bindings.py -k protection_adoption -q
```

Expected: FAIL because reconciliation does not invoke adoption.

**Step 3: Add the no-write-to-exchange adoption phase**

In `reconcile_deepcoin_execution_bindings`, reuse its bounded snapshot of
pending trigger orders after exact entry-leg attribution. For each eligible
verified trigger-limit leg, call the pure helper from Task 2 and upsert the
returned ledger evidence in the same database transaction. Do not call
`place_order`, `cancel_position_sltp`, `set_position_sltp`, or
`modify_position_sltp` from this phase.

Persist a bounded diagnostic/audit record for refusals using existing execution
event/audit infrastructure; do not emit Telegram message contents.

**Step 4: Run focused tests and the safety checks**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py -q
rg -n "place_order|cancel_position_sltp|set_position_sltp|modify_position_sltp" src/telegram_kol_research/execution_bindings.py
```

Expected: tests pass; the search returns no exchange-write call introduced by
the reconciliation adoption phase.

**Step 5: Commit**

```bash
git add tests/test_execution_bindings.py src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/entry_protection_ledger_repair.py
git commit -m "feat: adopt verified trigger entry protection"
```

### Task 4: Make adoption/refusal visible and preserve fail-closed management

**Files:**
- Modify: `tests/test_strategy_records.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html`
- Test: `tests/test_web_queries_messages.py`

**Step 1: Write failing read-model tests**

Assert that a strategy with no adopted ledger record and an ambiguous TPSL
candidate renders an actionable “保护单归属未验证” reason. Assert that a
successfully adopted record identifies only the exact `ordId` and does not
make any claim about other positions.

**Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_records.py -k protection -q
```

Expected: FAIL until the read model exposes adoption state.

**Step 3: Implement the minimal read-only presentation**

Expose the existing ledger evidence source and refusal code in the strategy
record/timeline. Do not add a one-click replay or an exchange mutation endpoint.

**Step 4: Run read-model and browser-smoke tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_records.py \
  tests/test_web_queries_messages.py \
  tests/test_web_page_render.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_strategy_records.py src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py src/telegram_kol_research/templates/_strategy_lifecycle_timeline.html
git commit -m "feat: show trigger protection adoption state"
```

### Task 5: Controlled production rollout

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`

**Step 1: Document the exact server-only audit command**

Document this read-only command and its expected safe outcomes:

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-entry-protection-ledger \
  --database-path data/research.db \
  --binding-id 158 \
  --pos-id 1001124227079271 \
  --include-trigger-entries
```

**Step 2: Document explicit fingerprinted application**

State that `--apply --expected-fingerprint <fingerprint>` is allowed only
after the dry-run shows precisely the reviewed action set. Do not apply a
historical repair after a manual protection change without a fresh snapshot and
operator review.

**Step 3: Run the full local suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

**Step 4: Commit, push, deploy, and verify on the server**

```bash
git add docs/runbook.md docs/server-deployment.md
git commit -m "docs: operate trigger protection adoption safely"
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

On the server, verify service health, run the read-only audit first, then
inspect a new trigger-entry lifecycle through `message -> entry leg -> posId ->
ledger ordId -> management preflight`. Never use a live management message as
a test fixture.
