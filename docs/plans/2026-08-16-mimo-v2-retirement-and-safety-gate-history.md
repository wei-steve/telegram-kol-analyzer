# MiMo v2 Retirement Rollback Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the runtime repository tree to the exact pre-MiMo-v2 baseline while preserving production database audit history and an auditable safety-gate history, then deploy only through the unchanged fail-closed preflight.

**Architecture:** Create one forward rollback commit on top of production SHA `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`. A test-first static retirement boundary proves every MiMo v2 activation surface is gone; a compatibility test proves the restored code tolerates additive retired database objects. No database downgrade, Batch119 recovery, historical replay, exchange write, notification test, watermark change, or preflight bypass is allowed.

**Tech Stack:** Python 3.12, pytest, SQLite, SQLAlchemy, Git worktrees, PowerShell deployment helper, systemd.

---

### Task 1: Establish the retirement boundary with a failing test

**Files:**
- Create: `tests/test_mimo_v2_retirement_boundary.py`

**Step 1: Write the failing test**

Create a static architecture-boundary test with the complete forbidden runtime
surface:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_RUNTIME_PATHS = (
    "src/telegram_kol_research/mimo_contract_circuit.py",
    "src/telegram_kol_research/mimo_recognition_runs.py",
    "src/telegram_kol_research/mimo_v2_contract.py",
    "src/telegram_kol_research/mimo_v2_execution_adapter.py",
    "src/telegram_kol_research/mimo_v2_replay.py",
)
FORBIDDEN_RUNTIME_MARKERS = {
    "src/telegram_kol_research/authoritative_recognition.py": (
        "v2_live_adapter",
        "infer_mimo_authoritative_v2",
    ),
    "src/telegram_kol_research/cli.py": ("mimo-v2-replay",),
    "src/telegram_kol_research/trading_settings.py": (
        "mimo_contract_mode",
        "mimo_v2_activation_after_raw_message_id",
    ),
    "src/telegram_kol_research/web_app.py": ("mimo-v2", "v2_live_adapter"),
}


def test_mimo_v2_runtime_modules_are_retired():
    present = [path for path in FORBIDDEN_RUNTIME_PATHS if (ROOT / path).exists()]
    assert present == []


def test_mimo_v2_activation_surfaces_are_retired():
    found = []
    for relative_path, markers in FORBIDDEN_RUNTIME_MARKERS.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        found.extend(
            f"{relative_path}:{marker}" for marker in markers if marker in source
        )
    assert found == []
```

**Step 2: Run the test to verify RED**

Run:

```bash
uv run pytest tests/test_mimo_v2_retirement_boundary.py -q
```

Expected: both tests fail because the current production tree still contains
MiMo v2 modules and activation markers.

### Task 2: Restore the exact pre-v2 runtime tree

**Files:**
- Restore every path changed by `354c82c..2274d90` from commit `354c82c`.
- Preserve: `docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history-design.md`
- Preserve: `docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history.md`
- Preserve: `tests/test_mimo_v2_retirement_boundary.py`

**Step 1: Restore only the historical MiMo-v2 change set**

Run from the isolated worktree:

```bash
git diff --name-only -z 354c82c..2274d90 | \
  xargs -0 git restore --source=354c82c --staged --worktree --
```

This list is bounded to the exact 50 paths changed after the pre-v2 boundary;
the new retirement documents and boundary test are not in that list.

**Step 2: Verify the boundary test is GREEN**

Run:

```bash
uv run pytest tests/test_mimo_v2_retirement_boundary.py -q
```

Expected: `2 passed`.

**Step 3: Prove runtime equivalence**

Run:

```bash
git diff --name-only 354c82c -- \
  ':(exclude)docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history-design.md' \
  ':(exclude)docs/plans/2026-08-16-mimo-v2-retirement-and-safety-gate-history.md' \
  ':(exclude)tests/test_mimo_v2_retirement_boundary.py'
```

Expected: no output.

**Step 4: Commit the source retirement**

```bash
git add -A
git commit -m "revert: retire mimo v2 runtime"
```

### Task 3: Prove additive retired schema compatibility

**Files:**
- Modify: `tests/test_mimo_v2_retirement_boundary.py`

**Step 1: Add the compatibility regression test**

Add a test that creates a normal pre-v2 database, adds the retired tables and
column using SQLite DDL, then opens the database again through
`create_session_factory` and proves a normal `RawMessage` read still works.
The test must not drop or rewrite any table.

```python
import sqlite3

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


def test_pre_v2_runtime_ignores_additive_retired_mimo_schema(tmp_path):
    database_path = tmp_path / "retired-mimo-schema.db"
    factory = create_session_factory(database_path)
    with factory() as session:
        session.add(RawMessage(chat_id=1, message_id=1, text="baseline"))
        session.commit()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE message_evidence_versions "
            "ADD COLUMN mimo_recognition_run_id INTEGER"
        )
        connection.execute(
            "CREATE TABLE mimo_recognition_runs "
            "(id INTEGER PRIMARY KEY, run_kind TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE mimo_recognition_attempts "
            "(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE mimo_contract_circuit_state "
            "(id INTEGER PRIMARY KEY)"
        )
    reopened = create_session_factory(database_path)
    with reopened() as session:
        assert session.query(RawMessage).count() == 1
```

**Step 2: Run the focused tests**

```bash
uv run pytest \
  tests/test_mimo_v2_retirement_boundary.py \
  tests/test_db_migrations.py \
  tests/test_authoritative_recognition.py \
  tests/test_deployment_preflight.py -q
```

Expected: all pass.

**Step 3: Commit the compatibility proof**

```bash
git add tests/test_mimo_v2_retirement_boundary.py
git commit -m "test: prove retired mimo schema compatibility"
```

### Task 4: Complete local verification and independent review

**Files:**
- No production changes expected.

**Step 1: Run compile checks**

```bash
uv run python -m compileall -q src tests
git diff --check 2274d90..HEAD
```

Expected: both succeed with no output from `git diff --check`.

**Step 2: Run the full suite**

```bash
uv run pytest -q
```

Expected: all tests pass; no test may be skipped or weakened to accommodate the
rollback.

**Step 3: Request independent review**

Review the exact range `2274d90..HEAD` against the approved design. The reviewer
must check for Critical and Important defects, accidental removal of pre-v2
behavior, database-downgrade logic, preflight weakening, hidden MiMo v2
activation surfaces, and unrelated changes.

**Step 4: Resolve findings and re-run affected tests**

Do not proceed with any unresolved Critical or Important finding.

### Task 5: Stage and verify the exact candidate on the server

**Files:**
- No production-tree changes before preflight approval.

**Step 1: Push the reviewed branch**

```bash
git push -u origin codex/mimo-v2-retirement-rollback
```

Verify the remote SHA equals local `HEAD` exactly.

**Step 2: Create a private detached server candidate**

Fetch the exact branch into a private candidate directory under
`/opt/telegram-kol-candidates`, verify the exact SHA and clean worktree, and use
the production virtual environment only for dependencies. Do not install it or
change `/opt/telegram-kol-analyzer`.

**Step 3: Test against a disposable database copy**

Copy the production database into a private candidate artifact directory on
`/opt`, open it only with the candidate code, run the focused retirement,
migration, v1-authority, and deployment-preflight tests, then delete the
disposable database. Record zero production writes and zero exchange calls.

### Task 6: Deploy only through the unchanged preflight

**Files:**
- Use: `scripts/server_git_update.ps1`

**Step 1: Prove the deployment window**

Confirm the production SHA is still `2274d90`, the service is active, the
candidate SHA is exact, and no time-sensitive strategy operation is active.

**Step 2: Run the reviewed helper**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1 \
  -Branch codex/mimo-v2-retirement-rollback \
  -ExpectedCommit <reviewed-40-character-sha> \
  -ChangeClass code
```

The helper must stop on `BLOCK`, malformed evidence, SHA mismatch, failed
backup/migration verification, install failure, or unhealthy restart. Never
bypass or edit the preflight result.

**Step 3: Verify production if deployment succeeds**

Verify exact SHA, clean tracked files, active service, Web health, v1-only
recognition behavior, unchanged database audit counts, and no exchange writes,
notifications, or historical replays used for testing.

**Step 4: Stop safely if deployment is blocked**

Leave production unchanged at `2274d90`, preserve the reviewed branch, and
report the exact bounded preflight reason. Do not resume Batch119 recovery or
redesign the gate in this task.

