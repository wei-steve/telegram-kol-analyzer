# Deepcoin Memory-Bounded Backup Repair Design

## Status

Approved on 2026-08-29 for local implementation only. This design does not
authorize push, stage, SSH mutation, production database mutation, activation,
Deepcoin writes, runtime restoration, replay, or entry thaw.

## Problem

The first production reconciliation attempt proved both fresh exchange plans
but was killed by the kernel before `BEGIN IMMEDIATE`. The production database
was 814,260,224 bytes. `_create_verified_backup()` copied the complete database
into SQLite `:memory:` and would then have created further whole-database
`serialize()`, `bytearray`, `bytes`, descriptor-read, and `deserialize()`
copies. The process reached about 1.69 GiB anonymous RSS on a 2 GiB host and was
OOM-killed while the disk destination was still zero bytes.

The production database size predates the failed transaction and remained
unchanged. Capacity retention is a separate workflow. This repair addresses
only the backup algorithm and its missing scale acceptance test.

## Goals

- Produce a transaction-consistent, standalone SQLite backup without holding a
  whole database image in application memory.
- Preserve the existing exact-source, safe-parent, exclusive-destination,
  root-owned `0600`, inode-continuity, integrity, and failure-cleanup contracts.
- Include committed WAL frames and leave a backup that opens independently of
  the source database and source WAL.
- Verify the backup from disk with `quick_check`, `foreign_key_check`, and the
  exact bounded before counts required by reconciliation.
- Prove an 814 MB production copy can be backed up under `MemoryMax=1GiB`.

## Non-Goals

- No pruning, archival, VACUUM, schema migration, or retention-policy change.
- No change to the seven canonical targets, exchange evidence, terminalization
  transaction, authority document, activator, runtime authority, or entry
  freeze semantics.
- No retry of the failed production executor or reuse of its zero-byte output.
- No activation of candidate
  `89a7dc66ea0c788f48be2e9841cec010cd8feeb1`.

## Considered Approaches

### A. Direct file-backed SQLite online backup

Create the destination exclusively inside the already validated owner-only
directory, open the same protected path as a file-backed SQLite destination,
copy in bounded page batches, force rollback-journal mode, close and fsync it,
then reopen it read-only for verification. Hash it with fixed-size reads.

This is the selected approach. It retains SQLite snapshot semantics and the
current Python dependency while making application memory independent of
database size.

### B. External `sqlite3 .backup`

This is naturally file-backed, but adds CLI/version/environment dependencies,
subprocess error translation, and a second implementation surface for path and
identity checks. It is unnecessary while Python exposes the same online backup
API.

### C. `VACUUM INTO`

This creates a standalone compact file but rewrites database layout, has larger
temporary I/O and time costs, and combines backup with compaction semantics.
Those are outside the minimal repair.

## Selected Backup Flow

1. Resolve and `lstat()` the exact source. Require the same regular non-symlink
   inode already bound to the session factory.
2. `lstat()` and open the destination parent with `O_DIRECTORY|O_NOFOLLOW`.
   Require the effective user to own it and reject group/world write bits.
3. Refuse every existing destination, including dangling symlinks. Create one
   regular destination inode with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0600`.
4. Because the parent is owner-controlled and not group/world-writable, open the
   exact created path as the SQLite disk destination. Immediately compare the
   path inode with the exclusive descriptor inode; any drift fails closed.
5. Open the source read-only, enable `query_only`, and verify the opened source
   inode. Use `Connection.backup()` with a bounded page batch and no in-memory
   database. The API may run to completion in one call, but both source and
   destination page caches stay bounded and the payload remains disk-backed.
6. On the disk destination, set `journal_mode=DELETE` after the online backup so
   the snapshot is standalone and does not depend on a source-side WAL. Close
   SQLite connections before final descriptor verification.
7. Fsync the destination and parent. Recheck source inode, destination inode,
   owner, exact `0600` mode, non-symlink status, and nontrivial file size.
8. Reopen the destination with `mode=ro`, enable `query_only`, and require
   `quick_check=ok`, zero foreign-key rows, `total_changes=0`, and the exact
   bounded table counts captured before backup.
9. Compute SHA-256 with fixed-size reads. No function may call `read_bytes()`,
   `serialize()`, `deserialize()`, or otherwise materialize the database in a
   Python bytes object.

## Failure Semantics

- Before a verified backup exists, every exception closes descriptors and
  connections and removes only the exact inode created by this invocation.
- A path/inode mismatch never unlinks an unrecognized replacement.
- Backup or verification failure occurs before `BEGIN IMMEDIATE`; all seven
  targets and the authority row remain unchanged and the runtime remains
  persistently inhibited.
- After a verified backup exists, later reconciliation failure preserves that
  backup exactly as the rollback boundary.
- Unknown remains terminal for that production attempt. No automatic retry is
  introduced.

## Testing

The first RED must expose the scale bug rather than merely ban implementation
names. Run the real backup in a child process against a file-backed fixture large
enough that the current whole-database-memory algorithm exceeds a deliberately
tight address-space/RSS boundary. Require successful completion, valid counts,
and a bounded peak memory for the repaired implementation.

Focused tests also preserve:

- exclusive path and exact `0600` mode;
- unsafe parent, existing path, dangling symlink, source/path ABA, and
  destination inode replacement refusal;
- uncheckpointed WAL commit inclusion and standalone reopen;
- `quick_check` and foreign-key failure cleanup;
- write failure cleanup without removing an unrecognized inode;
- terminalization rollback with the verified backup unchanged;
- streaming hash and absence of whole-file materialization.

After focused GREEN, run the complete affected reconciliation/CLI/activation
set, review the exact production-code diff, repair all P0/P1 findings, and run
one final full repository suite.

## Production-Scale Acceptance

Before a new production stage, use the existing root-owned read-only 814 MB
production copy, not the live database, and run the exact candidate backup path
inside a transient `MemoryMax=1GiB` boundary. Acceptance requires:

- process exits successfully without OOM or memory-limit violation;
- output is root-owned mode `0600` and independently readable;
- output SHA-256 is recorded;
- `quick_check=ok`, zero foreign-key issues, and before counts match;
- measured peak memory remains below the 1 GiB boundary;
- source copy hash and metadata remain unchanged.

Only after local tests, independent review, final full suite, this scale proof,
push, and a brand-new immutable stage may production reconciliation be
authorized again. The invalid zero-byte placeholder may be removed only in that
future production phase after exact owner, mode, inode, path, and size checks.

## Capacity Workflow Separation

Database retention and index growth are delegated to a separate capacity
analysis. Its findings must not change this repair, reuse this commit, prune the
production database during cutover, or weaken the exact backup and rollback
boundary.
