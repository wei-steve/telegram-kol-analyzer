# Position Protection Ledger Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a durable protection ledger so management messages can use verified TPSL ownership evidence instead of repeatedly blocking on ambiguous Deepcoin pending rows.

**Architecture:** Add a local ledger table and repository helpers, feed ledger-backed current TPSL rows into protection attribution only when a fresh exchange snapshot confirms the same order IDs, then expose specific unresolved reason codes. Existing position ownership and TPSL cancel/replace rechecks remain authoritative.

**Tech Stack:** Python, SQLAlchemy models, SQLite bootstrap/backfill, pytest, existing Deepcoin read-only/action clients.

---

### Task 1: Ledger Schema And Repository

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Create: `src/telegram_kol_research/protection_ledger.py`
- Test: `tests/test_protection_ledger.py`

**Step 1: Write failing schema/repository tests**

Create tests that:
- bootstrap a new SQLite DB and assert `position_protection_ledger` exists;
- upsert a ledger row for one `execution_order_leg_id`, `pos_id`, and `order_id`;
- update the same row without duplicating it;
- reject empty `order_id` by returning no persisted row.

**Step 2: Run tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/test_protection_ledger.py -q
```

Expected: fail because the module/table does not exist.

**Step 3: Implement minimal schema and helper**

Add `PositionProtectionLedger` with fields:
- `id`
- `venue`
- `execution_binding_id`
- `execution_order_leg_id`
- `strategy_instance_id`
- `pos_id`
- `instrument_id`
- `side`
- `order_id`
- `purpose`
- `trigger_price`
- `size_text`
- `status`
- `evidence_source`
- `evidence_json`
- `first_seen_at`
- `last_seen_at`
- `last_verified_at`
- `created_at`
- `updated_at`

Add unique index `(venue, order_id)` and useful indexes for `pos_id`, leg, binding, and status.

Add `upsert_protection_ledger_row(session, ...)` and `list_verified_ledger_rows_for_positions(session, pos_ids)`.

**Step 4: Run tests to verify pass**

Run:

```bash
./.venv/bin/python -m pytest tests/test_protection_ledger.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py src/telegram_kol_research/protection_ledger.py tests/test_protection_ledger.py
git commit -m "feat: add position protection ledger"
```

### Task 2: Ledger-Backed Protection Attribution

**Files:**
- Modify: `src/telegram_kol_research/protection_attribution.py`
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_protection_ledger.py`

**Step 1: Write failing tests**

Add tests that prove:
- a fresh pending TPSL row with no `posId` can become `verified` when a ledger row ties its exact order ID to the target leg/posId and price/purpose still match;
- ledger evidence is rejected when order ID is absent from current pending rows;
- ledger evidence is rejected when another live position can plausibly claim the same unscoped row;
- Miya-501-like two-position snapshot no longer collapses to generic `target_protection_not_verified`; it either verifies exact ledger-backed rows or records a specific unresolved reason.

**Step 2: Run tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/test_protection_ledger.py tests/test_strategy_management_planner.py -q
```

Expected: new tests fail because planner does not use ledger rows.

**Step 3: Implement minimal attribution integration**

Add an optional `ledger_rows_by_pos_id` input to `match_position_protection` or a small wrapper used by the planner. The wrapper should:
- call existing attribution first;
- if a position is `absent` or `present_but_ambiguous`, inspect verified ledger rows for that posId;
- find exact order IDs in the fresh pending TPSL snapshot;
- verify instrument, side, purpose, trigger price, and compatible size;
- return `verified` with `evidence.match = "ledger_confirmed_current_order"` only when every ledger row is current and globally unique.

**Step 4: Run tests to verify pass**

Run:

```bash
./.venv/bin/python -m pytest tests/test_protection_ledger.py tests/test_strategy_management_planner.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protection_attribution.py src/telegram_kol_research/strategy_management_planner.py tests/test_protection_ledger.py tests/test_strategy_management_planner.py
git commit -m "feat: use ledger-backed protection evidence"
```

### Task 3: Record Ledger Rows From TPSL Writes

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Write failing tests**

Add tests that successful entry protection and management protection replacement persist ledger rows with returned order IDs, posId, purpose, trigger price, and size.

**Step 2: Run tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/test_deepcoin_execution_actions.py tests/test_strategy_management_executor.py -q
```

Expected: new ledger assertions fail.

**Step 3: Implement minimal write hooks**

After confirmed `set_position_sltp` / replacement responses, snapshot the old/new TPSL rows and upsert ledger rows. Mark replaced/cancelled old rows `inactive` only after exchange confirmation or reconciliation proof.

**Step 4: Run tests to verify pass**

Run:

```bash
./.venv/bin/python -m pytest tests/test_deepcoin_execution_actions.py tests/test_strategy_management_executor.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/strategy_management_executor.py tests/test_deepcoin_execution_actions.py tests/test_strategy_management_executor.py
git commit -m "feat: record protection ledger from tpsl writes"
```

### Task 4: Specific Reason Codes And Unresolved Visibility

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_records.py`

**Step 1: Write failing tests**

Assert planner returns specific reason codes for ambiguous assignment, missing current order ID, price/size mismatch, and evidence unavailable. Assert strategy records surface blocked management batches as attention items with group/message IDs and reason.

**Step 2: Run tests to verify they fail**

Run:

```bash
./.venv/bin/python -m pytest tests/test_strategy_management_planner.py tests/test_strategy_records.py -q
```

Expected: fail on new reason/visibility assertions.

**Step 3: Implement minimal reason mapping**

Return specific reason codes from the planner based on `PositionProtection.evidence` / status. Add read-only projection for unresolved management batches without replaying them.

**Step 4: Run tests to verify pass**

Run:

```bash
./.venv/bin/python -m pytest tests/test_strategy_management_planner.py tests/test_strategy_records.py -q
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_records.py tests/test_strategy_management_planner.py tests/test_strategy_records.py
git commit -m "feat: surface actionable management blockers"
```

### Task 5: Server Deploy And Read-Only Repair Audit

**Files:**
- Modify if needed: `docs/migration-handoff.md`

**Step 1: Run full focused tests locally**

Run:

```bash
./.venv/bin/python -m pytest tests/test_protection_ledger.py tests/test_strategy_management_planner.py tests/test_deepcoin_execution_actions.py tests/test_strategy_management_executor.py tests/test_strategy_records.py -q
```

Expected: pass.

**Step 2: Push and deploy**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

Expected: server pulls latest commit, reinstalls editable package, and `telegram-kol.service` is active.

**Step 3: Verify server schema and safety**

Run server quick checks:
- `PRAGMA quick_check`
- focused pytest for new ledger tests
- read-only query for unresolved management batches
- no new non-management exchange write events during verification

**Step 4: Optional controlled ledger backfill**

Only after tests pass, run a read-only Deepcoin snapshot plus local DB ledger backfill. It may write ledger evidence rows only; it must not create trade signals, management batches, orders, or exchange writes.
