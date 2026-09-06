# Expiry Review Notification Dedup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist an independent expiry-review schedule so each due review sends once, and only an operator “continue waiting” action schedules one further review three hours later.

**Architecture:** Add `expiry_review_notified_at` and `expiry_review_next_at` to `StrategyLifecycle`, including idempotent SQLite compatibility migration and conservative legacy backfill. Claim each due notification with a conditional database update before calling Telegram, so repeated scans, restarts, concurrent monitors, and unrelated `management_action` changes cannot duplicate it.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, asyncio, pytest, Telegram Bot HTTP API.

---

### Task 1: Add persistent expiry-review fields and migrate existing databases

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Write the failing migration test**

Create a legacy `strategy_lifecycles` table without the new columns, insert one
`expiry_review_requested` row and one `expiry_review_continued` row, then call
`create_session_factory`. Assert:

- both new columns exist;
- the requested row uses `last_checked_at` as `expiry_review_notified_at`;
- the continued row uses `last_checked_at + 3 hours` as `expiry_review_next_at`;
- a second bootstrap leaves values unchanged.

**Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_db_bootstrap.py -k expiry_review -v
```

Expected: FAIL because the model and compatibility migration do not contain the
new fields.

**Step 3: Add minimal model and migration code**

Add nullable `DateTime` model fields:

```python
expiry_review_notified_at: Mapped[Optional[datetime]]
expiry_review_next_at: Mapped[Optional[datetime]]
```

Add both `ALTER TABLE` statements to `SQLITE_COMPAT_COLUMNS`. After column
creation, run an idempotent SQLite backfill:

```sql
UPDATE strategy_lifecycles
SET expiry_review_notified_at = COALESCE(last_checked_at, updated_at)
WHERE management_action = 'expiry_review_requested'
  AND expiry_review_notified_at IS NULL;

UPDATE strategy_lifecycles
SET expiry_review_next_at = datetime(COALESCE(last_checked_at, updated_at), '+3 hours')
WHERE management_action = 'expiry_review_continued'
  AND expiry_review_notified_at IS NULL
  AND expiry_review_next_at IS NULL;
```

**Step 4: Run migration tests**

Run:

```bash
pytest tests/test_db_bootstrap.py -k "expiry_review or database_bootstrap" -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_db_bootstrap.py
git commit -m "feat: persist expiry review notification state"
```

### Task 2: Make the lifecycle monitor claim each review once

**Files:**
- Modify: `src/telegram_kol_research/lifecycle_monitor.py`
- Test: `tests/test_lifecycle_monitor.py`

**Step 1: Write failing monitor tests**

Add focused tests proving:

- an initial overdue lifecycle sends once across repeated `run_once` calls and a
  newly constructed monitor;
- changing `management_action` after the first send does not reopen notification;
- an entered lifecycle with a pending second entry leg follows the same rule;
- an explicitly scheduled `expiry_review_next_at` sends once when due and clears
  the schedule;
- two monitors that encounter the same due row cannot both claim it;
- an entered lifecycle whose pending leg has resolved clears an outstanding
  schedule without sending.

**Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_lifecycle_monitor.py -k expiry_review -v
```

Expected: the new assertions fail because eligibility still depends on
`management_action`.

**Step 3: Implement minimal atomic claim logic**

Change review eligibility to:

```python
initial_due = (
    row.expiry_review_notified_at is None
    and row.expiry_review_next_at is None
    and now >= initial_expiry_at
)
continued_due = (
    row.expiry_review_next_at is not None
    and now >= row.expiry_review_next_at
)
```

Before adding a payload, issue a conditional `UPDATE` matching the corresponding
unclaimed state. Set `expiry_review_notified_at=now`,
`expiry_review_next_at=None`, and the existing display fields. Only a row count
of one may enqueue a Telegram payload.

Remove `management_action` from notification eligibility. Keep it only for
operator-visible status and review-reason text. When an entered lifecycle no
longer has pending entry legs, clear `expiry_review_next_at`.

**Step 4: Run monitor tests**

Run:

```bash
pytest tests/test_lifecycle_monitor.py -k expiry_review -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/lifecycle_monitor.py tests/test_lifecycle_monitor.py
git commit -m "fix: send each expiry review only once"
```

### Task 3: Schedule the next review only from “continue waiting”

**Files:**
- Modify: `src/telegram_kol_research/telegram_bot_commands.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write the failing callback test**

Extend the pending-entry and entered-pending-leg “continue waiting” tests to
assert:

```python
assert lifecycle.expiry_review_next_at == event_at + timedelta(hours=3)
```

Also prove other expiry decisions clear any outstanding
`expiry_review_next_at`.

**Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_system_operator_bot.py -k expiry -v
```

Expected: FAIL because callbacks do not yet manage the independent schedule.

**Step 3: Implement minimal callback scheduling**

For `EXPIRY_CONTINUE_COMMAND`, set:

```python
lifecycle.expiry_review_next_at = event_at + timedelta(hours=3)
```

For expire/cancel/keep decisions, set:

```python
lifecycle.expiry_review_next_at = None
```

Do not clear `expiry_review_notified_at`; it is durable evidence that the
initial notification already occurred.

**Step 4: Run callback tests**

Run:

```bash
pytest tests/test_system_operator_bot.py -k expiry -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/telegram_bot_commands.py tests/test_system_operator_bot.py
git commit -m "fix: schedule expiry review after operator continue"
```

### Task 4: Run regression checks and review the diff

**Files:**
- Review all modified files.

**Step 1: Run focused suites**

```bash
pytest tests/test_db_bootstrap.py tests/test_lifecycle_monitor.py tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 2: Run related execution-binding suite**

```bash
pytest tests/test_execution_bindings.py -q
```

Expected: PASS, including expiry-state clearing behavior for resolved legs.

**Step 3: Run formatting/static checks configured by the repository**

```bash
python -m compileall -q src tests
```

Expected: exit code 0.

**Step 4: Inspect scope**

```bash
git diff HEAD~3 --check
git status --short
```

Expected: no whitespace errors; unrelated pre-existing changes remain unstaged.

### Task 5: Push, deploy, and verify production

**Files:**
- Use: `scripts/server_git_update.ps1`
- Use: `scripts/codex_telegram_notify.py`

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: branch advances successfully.

**Step 2: Update production**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls the branch, reinstalls the editable package, restarts
`telegram-kol.service`, and reports active service state.

**Step 3: Verify production migration and service**

Confirm the deployed revision, active service, both new database columns, and
that existing `expiry_review_requested` rows were backfilled so they will not
be resent. Do not expose credentials or Telegram session values.

**Step 4: Send completion notification**

```bash
python3 "$(git rev-parse --show-toplevel)/scripts/codex_telegram_notify.py" \
  "已完成待入场超时复核单次通知修复并部署验证"
```

Expected: Telegram notification succeeds; if it fails, use the documented
macOS notification fallback and report the failure.
