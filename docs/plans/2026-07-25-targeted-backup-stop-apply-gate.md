# Targeted Backup Stop Apply Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Permit a reviewed single-position backup-stop repair when unrelated positions remain blocked, without weakening target-position safety checks.

**Architecture:** Preserve the full dry-run plan and its complete fingerprint. The CLI will reject only a conflict whose `pos_id` equals the requested `--pos-id`; the repair service will continue to rebuild the full plan, require one exact action, reserve one row, submit once, and verify the same exchange order before activation.

**Tech Stack:** Python, Typer, SQLAlchemy, pytest.

---

### Task 1: Scope the CLI conflict gate to the requested position

**Files:**

- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_backup_stop_repair.py`

**Step 1: Write the failing test.**

Create a plan containing one safe action for `pos-1` and one conflict for another position. Assert that the CLI-level guard permits applying `pos-1`, while a conflict for `pos-1` remains rejected.

**Step 2: Run the focused test and verify failure.**

```bash
uv run pytest -q tests/test_backup_stop_repair.py -k targeted_conflict
```

Expected: failure because the existing CLI rejects every nonempty conflict set.

**Step 3: Implement the smallest gate change.**

After validating `--pos-id` and fingerprint, compute conflicts whose `pos_id` equals the cleaned target. Refuse only when that filtered collection is nonempty. Keep printing all conflicts in dry-run output and do not change `apply_backup_stop_repair_plan`.

**Step 4: Run focused verification.**

```bash
uv run pytest -q tests/test_backup_stop_repair.py
```

Expected: pass, including the target-conflict refusal test.

**Step 5: Run static checks and commit.**

```bash
uv run python -m compileall -q src
git diff --check
git add src/telegram_kol_research/cli.py tests/test_backup_stop_repair.py
git commit -m "fix: scope backup repair conflicts to target position"
```

### Task 2: Deploy and perform one reviewed repair

**Files:**

- Update: `docs/migration-handoff.md` only after successful verification.

**Step 1: Push reviewed commit and deploy with `scripts/server_git_update.ps1` (or its exact SSH equivalent).**

**Step 2: Verify server SHA and active service.**

**Step 3: Generate a fresh dry-run, select the previously reviewed small candidate only, and use its new full-plan fingerprint.**

**Step 4: Run exactly one `repair-backup-stops --pos-id ... --apply --expected-fingerprint ...` command.**

**Step 5: Re-run the dry-run and audit. Stop for approval before every subsequent position.**
