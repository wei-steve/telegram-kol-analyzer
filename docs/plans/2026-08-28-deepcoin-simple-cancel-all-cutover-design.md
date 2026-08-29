# Deepcoin Simple Cancel-All Cutover Design

**Date:** 2026-08-28
**Status:** approved
**Risk:** L3 for local reconciliation; production service control and Deepcoin
operator actions remain separately authorized

## Objective

Use one explicit maintenance window to remove every open Deepcoin entry order,
reconcile the reviewed local pending-entry records, and switch to the normal
lease-aware runtime with entry admission still disabled.

The design deliberately removes the one-time bootstrap protocol. It accepts a
short service interruption because current operator intent is to clear all
entry orders, and it refuses the window if a fresh exchange snapshot finds any
position.

Historical observations are not acceptance evidence. The production window
must obtain a fresh, complete account snapshot immediately before cancellation
and again before local reconciliation.

## Selected Operator Flow

1. Fresh read-only evidence must show zero positions. Any position aborts the
   window; no additional protection handoff is attempted.
2. Stop and persistently inhibit the legacy trading runtime so it cannot create
   another entry.
3. The operator uses Deepcoin's own interface to cancel every open entry
   trigger. The repository performs no exchange-write loop and has no bulk
   cancellation API path.
4. Fresh read-only evidence must show zero positions, zero regular orders, and
   zero pending triggers for the governed instruments. Incomplete evidence is
   unknown and stops the window.
5. One local reconciliation command creates a verified SQLite backup, rechecks
   the exchange snapshot, terminalizes every canonical reviewed target in one
   transaction, records one event per target, and creates the missing idle
   entry authority row in that same transaction.
6. The ordinary scoped activator switches web, monitor, ingest, and worker to
   the staged release. Candidate and rollback both keep entry admission frozen.
7. Runtime identity and health are checked. Entry thaw remains a separate
   future action.

## Minimal State

Only operational state is retained:

```text
legacy_running -> maintenance_stopped -> candidate_entry_frozen
```

An incomplete exchange query, database uncertainty, or activation failure
leaves the system in `maintenance_stopped`. There is no separate bootstrap,
drain, handoff, or recovery state machine.

## Delete

- `bootstrap-control`, `seed-entry-authority`, and `drain-one` CLI commands.
- The immutable-control bootstrap module and tests.
- The three-action maintenance manifest and coordinator modules.
- The separate authority-seed tool.
- The reviewed per-order exchange-cancellation executor, confirmation-token
  flow, and seven-window orchestration.
- Obsolete authority owner kinds used only by those tools.

## Keep

- `REVIEWED_PENDING_ENTRY_TARGETS` as the only local target list.
- Fresh bounded Deepcoin read evidence and fail-closed unknown handling.
- Complete local intent, leg, binding, lifecycle, protection, convergence, and
  event terminalization.
- The runtime entry/revision authority used by normal workers.
- Ordinary stage and scoped activation, rollback, entry freeze, runtime
  identity, and version-aware monitoring.
- Explicit production authorization boundaries.

## Replace

- Replace three maintenance commands with one
  `finalize-cancelled-pending-entries` command. It never calls a Deepcoin write
  endpoint.
- Replace the separate L3 seed with creation of the missing canonical idle row
  inside the same stopped-runtime reconciliation transaction.
- Replace candidate self-validation after import with validation before import.
  Runtime processes receive `PYTHONDONTWRITEBYTECODE=1` and start Python with
  `-B`, so release contents do not change merely by loading code.

## Failure Boundaries

- A position, regular order, pending trigger, unowned local target, existing
  malformed authority row, or incomplete evidence refuses reconciliation.
- If backup creation or verification fails, the database is untouched.
- If any local target fails validation or any terminal update fails, the single
  transaction rolls back completely.
- If reconciliation commits but activation fails, rollback may start only the
  validated control release and must remain entry-frozen.
- No exchange write is automatically retried because this repository performs
  no exchange write in the simplified workflow.

## Acceptance

- CLI help contains only the single reconciliation action, not the three
  retired maintenance actions.
- A fake Deepcoin client that records calls proves the reconciliation path uses
  reads only.
- Any nonzero position or order count blocks before backup or database writes.
- Injected failure at every terminalization stage rolls back all seven targets
  and the authority seed.
- Successful apply yields zero nonterminal canonical targets, exactly one event
  per target, one valid idle authority row, a verified backup, and no exchange
  write call.
- Release activation drop-ins disable bytecode writes, and the activator itself
  invokes Python with `-B`.

## Authorization Boundary

This local implementation does not authorize push, stage, SSH, production
reads, service stop/mask/start, Deepcoin UI cancellation, database mutation,
activation, restart, rollback, or entry thaw.
