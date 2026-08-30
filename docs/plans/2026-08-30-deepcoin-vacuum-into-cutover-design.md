# Deepcoin `VACUUM INTO` Cutover Simplification Design

## Goal

Finish the already-approved contract-cache ownership cutover without adding a
new protocol. Replace the nonconverging incremental SQLite backup mechanism
with one `VACUUM INTO` operation, then reuse the existing reconciliation,
deployment, activation and verification path.

## Selected approach

`_create_verified_backup()` keeps its existing public contract and the minimum
safety envelope around it:

- validate and bind the source file and root-owned non-writable destination
  directory;
- create the destination exclusively as mode `0600`;
- open the source with `mode=ro`, bind its inode, use `query_only=1` while
  verifying the expected critical-table counts, then turn `query_only` off only
  for `VACUUM INTO` while `mode=ro` continues to forbid source writes;
- run exactly one parameterized `VACUUM INTO` against the pre-created empty
  destination;
- bind and verify the resulting destination inode, `quick_check`, foreign keys,
  critical-table counts, mode, ownership, size and streaming SHA-256;
- retain the existing post-backup `BEGIN IMMEDIATE` count and target recheck
  before any reconciliation mutation.

No page batching, progress callback, retry, snapshot-pinning state machine or
nonconvergence guard remains.

## Alternatives rejected

- Continue repairing `sqlite3_backup`: rejected because production is already
  stopped and the incremental API is the source of the repeated failure.
- Plain file copy: rejected because a WAL-backed SQLite source can require
  sidecar handling and does not directly produce the verified standalone
  database expected by the existing cutover.

## Failure and rollback

Any missing or inconsistent evidence remains fail-closed. A failed published
backup artifact is retained for exact operator review. Database terminalization
remains one transaction; failure rolls it back while retaining the verified
backup. Activation failure uses the already-reviewed maintenance-stopped
rollback boundary. This design does not change trading decisions or exchange
write semantics.

## Verification

Use TDD for the implementation switch, run the affected reconciliation and
deployment tests, run one final repository suite, review the exact diff, push
the exact reviewed SHA, then perform fresh production preflight, immutable
stage, reconciliation, activation and risk-adaptive production observation.
