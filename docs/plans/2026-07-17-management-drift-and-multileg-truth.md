# Management Drift And Multi-Leg Truth Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Deepcoin multi-leg ownership and TPSL evidence exact, and surface legacy lifecycle-versus-exchange management drift without replaying historical instructions.

**Architecture:** Keep `execution_order_legs` as the live position authority and treat `closePosId` as exact TPSL identity. Project all verified leg IDs into strategy records, compare each live leg's exact protection with lifecycle expectations, and expose a read-only critical drift result when no confirmed management batch explains a mismatch.

**Tech Stack:** Python 3.12, SQLAlchemy 2, FastAPI/Jinja, pytest, SQLite, Deepcoin read-only REST evidence.

---

### Task 1: Attribute TPSL rows by `closePosId`

**Files:**
- Modify: `tests/test_protection_attribution.py`
- Modify: `src/telegram_kol_research/protection_attribution.py`

**Step 1: Write the failing test**

Add a test with two same-side ETH positions and two otherwise indistinguishable TPSL rows. Give each row a different `closePosId` and assert each position receives only its exact stop, target, and order ID.

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_protection_attribution.py::test_close_pos_id_is_exact_protection_identity -v`

Expected: FAIL because `closePosId` is ignored and both positions remain ambiguous.

**Step 3: Write minimal implementation**

Extend the exact position-ID lookup in `match_position_protection` to read `closePosId`, `close_pos_id`, and `closePositionId` after the existing position-ID variants. Do not change heuristic fallback behavior.

**Step 4: Run focused protection tests**

Run: `python -m pytest tests/test_protection_attribution.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_protection_attribution.py src/telegram_kol_research/protection_attribution.py
git commit -m "fix: bind protection by close position id"
```

### Task 2: Project verified entry-leg IDs into strategy records

**Files:**
- Modify: `tests/test_strategy_records.py`
- Modify: `src/telegram_kol_research/strategy_records.py`

**Step 1: Write the failing summary test**

Create one lifecycle whose binding compatibility value is `pos-a,pos-b` and whose two active entry legs are verified for `pos-a` and `pos-b`. Assert the summary exposes `pos_ids == ["pos-a", "pos-b"]` and does not treat the aggregate string as one position.

**Step 2: Run test to verify it fails**

Run the new test directly. Expected: FAIL because summaries expose only the aggregate `pos_id`.

**Step 3: Implement the minimal projection**

Load active verified entry legs with the existing batched evidence query, group their exact IDs by binding, and add a sorted `pos_ids` list to summaries/details. Preserve `pos_id` for compatibility.

**Step 4: Write the failing exchange-enrichment test**

Pass a multi-leg record and two exact exchange positions to `enrich_strategy_records_with_exchange`. Assert one lifecycle record is confirmed, contains both real positions, and produces no synthetic conflict rows.

**Step 5: Run test to verify it fails**

Expected: FAIL because enrichment compares the aggregate compatibility string.

**Step 6: Implement multi-leg enrichment**

Match every `pos_ids` value exactly, require one exchange row per ID, mark all matched IDs consumed, and store `real_positions`. Keep `real_position` only for a single-leg compatibility view.

**Step 7: Run strategy-record tests and commit**

Run: `python -m pytest tests/test_strategy_records.py -q`

Expected: PASS.

```bash
git add tests/test_strategy_records.py src/telegram_kol_research/strategy_records.py
git commit -m "fix: render verified multi-leg positions"
```

### Task 3: Fix strategy-detail multi-leg exchange evidence

**Files:**
- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`

**Step 1: Write the failing route test**

Build a lifecycle with two verified entry legs and a fake Deepcoin snapshot containing both positions. Request `/strategy-records/{id}` and assert the page reports confirmed ownership and renders both exact `posId`s.

**Step 2: Run test to verify it fails**

Expected: FAIL with `not_found` because the route compares one aggregate binding string.

**Step 3: Implement exact multi-leg detail matching**

Use the detail's verified `pos_ids`, require exactly one matching position for each ID, run ownership validation for each position, and return a `positions` list plus a compatibility `position` for single-leg records.

**Step 4: Render all exact positions**

Update the detail template to list each position's ID, size, protection state, stop, and take-profit evidence. Do not add mutation controls.

**Step 5: Run focused Web tests and commit**

Run the new test and the existing strategy-record route tests. Expected: PASS.

```bash
git add tests/test_web_app.py src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/strategy_record_detail.html
git commit -m "fix: show multi-leg exchange evidence"
```

### Task 4: Surface management execution drift

**Files:**
- Modify: `tests/test_strategy_records.py`
- Modify: `src/telegram_kol_research/strategy_records.py`
- Modify: `src/telegram_kol_research/templates/strategy_record_detail.html`

**Step 1: Write the failing drift test**

Create an entered lifecycle with a management message and expected stop `63,575.875`, an active verified leg whose exchange protection is `60,500`, and no confirmed management batch. Assert enrichment adds critical attention code `management_execution_drift` with expected and actual protection values.

**Step 2: Run test to verify it fails**

Expected: FAIL because only explicit exchange mismatch flags are currently recognized.

**Step 3: Implement fail-closed drift classification**

Compare lifecycle expected protection to exact verified exchange protection only when evidence is current. Require a management message reference and no confirmed batch that explains the state. Unknown, absent, ambiguous, or malformed evidence must remain separate and must not become a numeric mismatch.

**Step 4: Add template output**

Render expected/actual values and confirmation state in the read-only evidence section.

**Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_strategy_records.py tests/test_web_app.py -q`

Expected: PASS.

```bash
git add tests/test_strategy_records.py src/telegram_kol_research/strategy_records.py src/telegram_kol_research/templates/strategy_record_detail.html
git commit -m "fix: expose management execution drift"
```

### Task 5: Verify management confirmation ordering

**Files:**
- Modify only if coverage is missing: `tests/test_strategy_management_executor.py`
- Modify only if coverage is missing: `tests/test_strategy_management_reconciliation.py`

**Step 1: Audit existing tests**

Confirm tests prove submission failure leaves lifecycle unchanged and successful reconciliation applies lifecycle state only after exchange confirmation.

**Step 2: Add only missing failing coverage**

If either contract is missing, add the smallest integration test and verify it fails for the uncovered behavior. Do not duplicate existing tests.

**Step 3: Implement only if the new test identifies a real gap**

Keep changes inside the management batch executor/reconciliation modules.

**Step 4: Run management tests and commit if changed**

Run: `python -m pytest tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py -q`

Expected: PASS.

### Task 6: Full verification, documentation, integration, and deployment

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`

**Step 1: Run focused tests**

Run protection, strategy-record, Web, management executor, and reconciliation tests. Expected: PASS.

**Step 2: Run the complete suite**

Run: `python -m pytest -q`

Expected: at least the clean baseline of `1607 passed, 1 skipped`, with no new failures.

**Step 3: Review the complete diff**

Verify there is no exchange mutation, old-message replay, production database write, or unrelated main-worktree change.

**Step 4: Document and commit**

Record the authority, drift, deployment, and read-only verification contract in the two durable project documents.

**Step 5: Integrate and push**

Cherry-pick reviewed commits onto `codex/deepcoin-auto-trading-v1` without overwriting the main checkout's unrelated uncommitted work, then push GitHub.

**Step 6: Deploy through the approved helper**

Run: `./scripts/server_git_update.sh`

**Step 7: Verify production read-only**

Confirm deployed SHA, active service, SQLite `quick_check`, current trading settings, exact live position/TPSL evidence, zero newly submitted/cancelled exchange orders, and the corrected lifecycle `466` strategy record. Run focused server tests.
