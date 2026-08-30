# Deepcoin Memory-Bounded Backup Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the whole-database in-memory reconciliation backup with a secure file-backed backup whose memory use remains below 1 GiB for the existing 814 MB production copy.

**Architecture:** Keep the existing source, parent, exclusive-path, inode, mode, integrity, and rollback gates. Open the exclusively created destination as a disk-backed SQLite database, copy with the online backup API in bounded page batches, force a standalone rollback-journal file, then verify and hash from disk without materializing the database in Python memory.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `os`, `stat`, `hashlib`, `resource` on Linux, pytest, SQLAlchemy session factory, systemd transient memory limits for the later production-copy proof.

---

### Task 1: Add the scale and disk-backed RED tests

**Files:**
- Modify: `tests/test_manual_pending_entry_reconciliation.py:2104-2264`
- Create: `tests/helpers/run_reconciliation_backup_under_limit.py`

**Step 1: Write a test helper that applies an address-space limit**

Create a Linux-only child-process helper that imports the candidate first,
applies `RLIMIT_AS`, runs the real `_create_verified_backup()`, and emits only
bounded JSON:

```python
from __future__ import annotations

import json
from pathlib import Path
import resource
import sys

from telegram_kol_research.manual_pending_entry_reconciliation import (
    _create_verified_backup,
)


def main() -> int:
    source = Path(sys.argv[1])
    backup = Path(sys.argv[2])
    limit_bytes = int(sys.argv[3])
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    _create_verified_backup(source, backup)
    print(json.dumps({"status": "complete", "size": backup.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 2: Write the failing Linux scale test**

In the parent test, create a file-backed SQLite fixture larger than 128 MiB by
inserting fixed-size `randomblob()` rows, close it, and invoke the helper with a
512 MiB `RLIMIT_AS`. Skip only when `sys.platform != "linux"` or `RLIMIT_AS` is
unavailable. Assert exit zero, output size at least source logical content, and
`quick_check=ok`.

The current `:memory:` plus serialization implementation must fail through
memory exhaustion or nonzero child exit. The test must not accept a skip on
Linux.

**Step 3: Write the cross-platform disk-backed behavior RED**

Replace `test_verified_backup_writes_the_exclusive_inode_without_path_reopen`
with a test that spies on `sqlite3.connect`, rejects every `":memory:"`
destination, and makes `_read_exact_descriptor()` raise if called. Require the
real backup to succeed and contain the seeded row. This proves the desired data
flow even on macOS where the strict resource limit is not portable.

**Step 4: Run the RED tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_manual_pending_entry_reconciliation.py::test_verified_backup_is_file_backed_without_whole_database_materialization \
  tests/test_manual_pending_entry_reconciliation.py::test_verified_backup_completes_under_bounded_address_space
```

Expected: the cross-platform test fails because the implementation opens
`":memory:"`; the Linux test fails under 512 MiB where executed.

**Step 5: Commit the RED tests**

```bash
git add -- tests/test_manual_pending_entry_reconciliation.py \
  tests/helpers/run_reconciliation_backup_under_limit.py
git diff --cached --name-only
git commit -m "test: reproduce reconciliation backup memory blowup"
```

### Task 2: Implement the minimal file-backed backup

**Files:**
- Modify: `src/telegram_kol_research/manual_pending_entry_reconciliation.py:786-990`
- Test: `tests/test_manual_pending_entry_reconciliation.py`

**Step 1: Preserve the existing pre-open guards**

Keep source `lstat`, safe-parent validation, parent descriptor identity,
existing-path refusal, exclusive destination creation, exact mode enforcement,
and cleanup ownership tracking unchanged.

**Step 2: Replace the in-memory destination**

After creating the exclusive descriptor, compare its inode to the destination
path, then use the protected path as a disk destination:

```python
destination = sqlite3.connect(
    f"file:{backup_path}?mode=rw",
    uri=True,
)
opened_destination = os.stat(
    backup_path.name,
    dir_fd=parent_descriptor,
    follow_symlinks=False,
)
if (
    opened_destination.st_dev != created_metadata.st_dev
    or opened_destination.st_ino != created_metadata.st_ino
):
    raise ValueError("backup_metadata_invalid")
source.execute("PRAGMA query_only=ON")
if source.execute("PRAGMA query_only").fetchone() != (1,):
    raise ValueError("backup_source_invalid")
source.backup(destination, pages=1024, sleep=0.01)
if destination.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
    raise ValueError("backup_metadata_invalid")
destination.commit()
```

Do not add an application retry loop. `pages` bounds each SQLite backup step;
the stdlib call remains one fail-closed operation.

**Step 3: Verify from disk**

Close the write destination, fsync the created descriptor and parent, validate
the final source/destination inode and metadata, then reopen with
`file:<path>?mode=ro`. Enable `query_only` and require `quick_check=ok`, zero
foreign-key rows, and `total_changes=0`.

Remove all uses of:

```python
sqlite3.connect(":memory:")
Connection.serialize()
Connection.deserialize()
bytearray(serialized)
bytes(payload_buffer)
_read_exact_descriptor(...)
```

Delete `_read_exact_descriptor()` after all callers are gone.

**Step 4: Preserve fail-closed artifact semantics**

Every exception before verified completion must close both SQLite connections
and all descriptors exactly once, but must not automatically unlink the
published destination. POSIX cannot atomically condition unlink on the created
inode, so preserving the failed artifact is required to avoid deleting a
replacement inode. Cleanup requires a later explicit identity check.

**Step 5: Run the two RED tests and verify GREEN**

Run the Task 1 command. Expected: both pass where the Linux test applies.

**Step 6: Commit production code**

```bash
git add -- src/telegram_kol_research/manual_pending_entry_reconciliation.py
git diff --cached --name-only
git commit -m "fix: bound reconciliation backup memory"
```

### Task 3: Repair and extend backup safety regression tests

**Files:**
- Modify: `tests/test_manual_pending_entry_reconciliation.py:2079-2264`

**Step 1: Adapt quick-check fault injection**

Change `test_verified_backup_removes_failed_quick_check_output` so its custom
connection factory targets the read-only backup reopen rather than
`":memory:"`. Require the self-created failed output to remain for explicit
later cleanup.

**Step 2: Add destination-path ABA coverage**

During the file-backed destination connect boundary, replace the path with a
different inode inside the test-controlled safe directory. Require
`backup_metadata_invalid`, require the unrecognized replacement to remain, and
prove no source mutation.

**Step 3: Prove WAL independence**

Extend the existing uncheckpointed-WAL test to close or move the source and its
WAL before opening the backup. Require the committed `wal-only` row and
`PRAGMA journal_mode=delete` from the standalone backup.

**Step 4: Prove forbidden whole-file paths are unused**

Monkeypatch any remaining compatibility helper or `Path.read_bytes` access in
the module to fail during backup. Compute any test digest with chunked reads.

**Step 5: Run the backup-focused set**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_manual_pending_entry_reconciliation.py -k 'verified_backup or terminalization_failure'
```

Expected: all selected tests pass.

**Step 6: Commit regression tests**

```bash
git add -- tests/test_manual_pending_entry_reconciliation.py
git diff --cached --name-only
git commit -m "test: harden disk-backed reconciliation backup"
```

### Task 4: Run affected verification and review the exact diff

**Files:**
- Modify if required by findings:
  `src/telegram_kol_research/manual_pending_entry_reconciliation.py`
- Modify if required by RED-first findings:
  `tests/test_manual_pending_entry_reconciliation.py`

**Step 1: Run static verification**

```bash
.venv/bin/python -m py_compile \
  src/telegram_kol_research/manual_pending_entry_reconciliation.py \
  tests/helpers/run_reconciliation_backup_under_limit.py
git diff --check
```

**Step 2: Run affected functional suites**

```bash
.venv/bin/python -m pytest -q \
  tests/test_manual_pending_entry_reconciliation.py \
  tests/test_cli_smoke.py \
  tests/test_scoped_release_activation.py \
  tests/test_deployment_activation_quiescence_check.py
```

Expected: all pass, with only existing documented skips/warnings.

**Step 3: Review the exact production-code base-to-HEAD diff**

Review from status commit
`9a688885ca9337cd57384ce8a98aa3617150661d` through HEAD. Prioritize:

- destination path/inode replacement races;
- cleanup deleting an unrecognized path;
- WAL-dependent or inconsistent backups;
- source or destination writes outside the exact boundary;
- hidden O(database-size) memory materialization;
- a backup accepted without integrity or mode proof.

**Step 4: Repair every P0/P1 finding with RED→GREEN**

For each finding, first add a focused failing test, run it to prove RED, make
the minimal implementation change, and rerun the focused test plus the affected
set. Commit explicit paths only.

### Task 5: Final local suite and handoff documentation

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Run one final full suite**

After the last production-code edit:

```bash
.venv/bin/python -m pytest -q
```

Expected: the complete repository suite passes. Do not rerun the full suite for
documentation-only edits.

**Step 2: Update the status document**

Record:

- rejected OOM candidate `89a7dc66...` remains inactive;
- RED and GREEN evidence;
- exact repair base and production-code SHAs;
- focused and final-suite results;
- code-review result;
- production remains `maintenance_stopped`;
- no push, production retry, database write, activation, replay, or thaw;
- next gate is production-copy scale proof under `MemoryMax=1GiB`.

**Step 3: Commit the status document**

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record memory-bounded backup repair"
```

### Task 6: Separately authorize the production-copy scale proof

**Files:** None in the repository unless status evidence is recorded afterward.

This task is not authorized by the local implementation plan. In its own
approved phase:

1. verify production still equals the recorded `maintenance_stopped` boundary;
2. verify the source is the existing root-owned read-only 814 MB evidence copy;
3. create a new evidence destination and transient `MemoryMax=1GiB` unit;
4. run the exact repaired backup implementation once;
5. record peak memory, exit status, output owner/mode/size/SHA-256,
   `quick_check`, foreign keys, counts, and unchanged source hash;
6. do not touch the live database, zero-byte placeholder, service state, stage,
   activation, Deepcoin, replay, or entry thaw.

Only a successful scale proof may authorize push, a brand-new immutable stage,
and a later fresh production cutover phase.
