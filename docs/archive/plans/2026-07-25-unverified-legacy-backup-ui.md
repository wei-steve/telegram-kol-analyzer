# Legacy Backup Verification UI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the positions page from presenting stale legacy generic backup-stop records as exchange-active protection, while preserving them for audit.

**Architecture:** Build one pure verifier that receives the live DeepCoin pending snapshot and a `PositionBackupStopOrder`. Only a native TPSL row matched by exact order ID and protection fields is exchange-verified. The Web row consumes that verdict; a separate, explicit database-only reconciliation command changes stale generic rows to `unverified_exchange` after a fresh, fingerprinted snapshot.

**Tech Stack:** Python 3, SQLAlchemy, FastAPI/Jinja positions panel, pytest, DeepCoin read-only API calls.

---

### Task 1: Make the positions view exchange-verification aware

**Files:**
- Modify: `src/telegram_kol_research/protection_snapshot.py`
- Modify: `src/telegram_kol_research/web_app.py:900-1060`
- Test: `tests/test_protection_snapshot.py`
- Test: `tests/test_web_app.py`

**Step 1: Write failing tests**

Add a legacy generic row with local status `active` but no matching pending exchange row. Assert the audit returns `legacy_generic` / `unverified_exchange`, and the positions row renders “交易所未验证（旧通用条件单）”, not `active`.

```python
assert row["backup_stop_status"] == "unverified_exchange"
assert row["backup_stop_state_text"] == "交易所未验证（旧通用条件单）"
```

**Step 2: Run RED**

Run: `uv run pytest tests/test_protection_snapshot.py tests/test_web_app.py -q`

Expected: FAIL because the Web page currently reads `PositionBackupStopOrder.status` directly.

**Step 3: Implement minimal verifier integration**

Use `build_position_protection_audit()` with the complete live position snapshot and pending TPSL rows. Map generic/missing local records to `unverified_exchange`; map verified native rows to `active`. Do not issue any write or exchange API call from rendering.

**Step 4: Run GREEN**

Run: `uv run pytest tests/test_protection_snapshot.py tests/test_web_app.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/protection_snapshot.py src/telegram_kol_research/web_app.py tests/test_protection_snapshot.py tests/test_web_app.py
git commit -m "fix: show only exchange-verified backup stops active"
```

### Task 2: Add explicit stale-legacy database reconciliation

**Files:**
- Create: `src/telegram_kol_research/legacy_backup_reconciliation.py`
- Create: `scripts/reconcile_legacy_backup_status.py`
- Test: `tests/test_legacy_backup_reconciliation.py`

**Step 1: Write failing reconciliation tests**

Cover a stale generic record becoming `unverified_exchange`; a pending/ambiguous row remaining unchanged; default CLI dry-run not writing; real reconciliation requiring `--execute --expected-fingerprint`.

```python
assert result.updated_pos_ids == ("1001124332530587",)
assert legacy.status == "unverified_exchange"
assert fake_client.write_calls == []
```

**Step 2: Run RED**

Run: `uv run pytest tests/test_legacy_backup_reconciliation.py -q`

Expected: FAIL because no reconciliation planner exists.

**Step 3: Implement a fingerprint-gated, database-only reconciler**

The planner reads current positions, pending trigger orders and history. It only selects rows with legacy generic request JSON whose exact `ordId` is absent from both exchange sources. The applier requires exact fingerprint, updates only the database status/evidence, and never calls `set_position_sltp`, `trigger_order`, or either cancel endpoint.

**Step 4: Run GREEN**

Run: `uv run pytest tests/test_legacy_backup_reconciliation.py tests/test_protection_snapshot.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/legacy_backup_reconciliation.py scripts/reconcile_legacy_backup_status.py tests/test_legacy_backup_reconciliation.py
git commit -m "fix: reconcile stale legacy backup statuses"
```

### Task 3: Server verification without exchange writes

**Files:**
- Modify: `docs/plans/2026-07-25-unverified-legacy-backup-ui-design.md`

**Step 1: Deploy reviewed commits and run dry-run**

Run on the server:

```bash
.venv/bin/python scripts/reconcile_legacy_backup_status.py --database-path data/research.db
```

Expected: exactly the stale legacy rows are planned; no exchange-write method is called.

**Step 2: Execute the database-only reconciliation with displayed fingerprint**

Run only after the dry-run output is reviewed:

```bash
.venv/bin/python scripts/reconcile_legacy_backup_status.py --database-path data/research.db --execute --expected-fingerprint <fingerprint>
```

Expected: stale records become `unverified_exchange`; no DeepCoin orders are created or cancelled.

**Step 3: Reload positions page and record outcome**

Confirm Web no longer labels legacy records active and App/Web agree on active native TPSL count. Add a concise implementation record to the design document.

**Step 4: Commit record**

```bash
git add docs/plans/2026-07-25-unverified-legacy-backup-ui-design.md
git commit -m "docs: record legacy backup reconciliation"
```
