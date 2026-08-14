# Deployment Gate and Management-Batch Recovery Design

**Date:** 2026-08-14

**Status:** Approved by the operator on 2026-08-14

## Objective

Restore a truthful production deployment window without weakening the deployment
preflight. Resolve the false-active Batch 119 state from exact durable and
exchange evidence, audit batches 123, 127, and 129 independently, establish fresh
stable Deepcoin read evidence, and leave production ready to resume the MiMo v2
rollout with `mimo_contract_mode=v1`.

## Current Evidence

- Production is running commit `2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa`
  on `codex/deepcoin-auto-trading-v1`.
- The reviewed recovery candidate is
  `b27541f459ab89a18cb617e434f41b962d72b339`.
- The isolated MiMo candidate is
  `5702c343a46c89811edc082650330d4eacf39a8f` on
  `codex/mimo-v2-fast-deploy`.
- Production MiMo settings are `mimo_contract_mode=v1` and activation watermark
  `0`.
- Batch 119 was created on 2026-08-12 for `partial_then_break_even`. It remains
  `reconciling` with reason
  `management_close_pending_exchange_confirmation`.
- Leg 103 remains `submitted` with
  `management_close_order_not_found`.
- The worker updates the batch and leg repeatedly without changing the durable
  state. The deployment preflight uses a ten-minute `updated_at` window, so this
  false-progress loop is always classified as fresh active work.
- Batches 123, 127, and 129 are independently `recovery_required`. They are not
  silently equivalent to Batch 119 and must not be repaired as a group.
- The latest schema-compatible preflight also reported an exchange snapshot that
  was available and complete but not fresh or independently stable. That is a
  warning for this change class, but it must be corrected before declaring a
  clean release window.

## Root Cause

Batch 119 is a composite-management batch. Its first component failed closed on
an incomplete take-profit snapshot. The legacy management reconciler later took
ownership of the same batch, converted the leg to `submitted`, and converted the
batch to `reconciling` despite there being no durable close mutation intent,
request, response, client order ID, or exchange order ID.

On every subsequent reconciliation, the exact close order remains absent. The
legacy reconciler deliberately preserves the non-retryable pending state but
also refreshes `updated_at`. This makes a stuck incident look recently active to
the deployment preflight. The preflight is correctly failing closed; the defect
is the conflicting state-machine ownership and false liveness signal.

## Approaches Considered

### A. Evidence-backed recovery with unchanged preflight — selected

Use the already implemented, batch-119-only recovery authority at reviewed
commit `b27541f`. First run a stopped-service, read-only double capture from a
detached candidate worktree. In a later separately authorized window, apply only
the exact reviewed fingerprint. If the position is absent because an owned stop
closed it, the apply is local-only and performs zero exchange calls. Then create
a separate code-deployment window.

This preserves the normal deployment gate and the historical incident evidence.

### B. Wait for the record to age out

Rejected. The running worker refreshes `updated_at` without progress, so the row
does not age out. Stopping the service merely to cross the ten-minute threshold
would conceal unresolved authority rather than prove safety.

### C. Ignore Batch 119 in the deployment preflight

Rejected. A global exception would make an unresolved exchange state deployable
without proving it is the known false-active incident.

### D. Hand-edit terminal statuses

Rejected. Direct database edits would bypass the recovery fingerprint, locked
compare-and-swap checks, audit event, and exact natural-stop proof.

## Architecture

### 1. Separate diagnostic, apply, and deployment windows

The work is deliberately split into three operational windows:

1. **Diagnostic window:** stop all listed database/writer units, create private
   database copies, run two read-only exact-history captures, compare their
   semantic fingerprints, restore the original service state, and stop.
2. **Recovery apply window:** obtain new approval, stop the same units, back up
   the production database, recapture evidence directly against the production
   database, apply the exact fingerprint, verify state and exchange invariants,
   restore services, and stop.
3. **Code deployment window:** obtain a new stable preflight and deploy the
   reviewed recovery/governance code with every feature dormant. Do not combine
   this with the Batch 119 apply.

No approval or artifact crosses these windows. Every window obtains fresh
evidence.

### 2. Candidate isolation

Candidate code runs from a detached worktree at the exact reviewed SHA. The
diagnostic phase uses the production virtual environment only for dependencies
and `PYTHONPATH` for candidate source. It never checks out the candidate in the
production directory and never bootstraps the production database.

The recovery command is allowlisted to batch 119. Its default is dry-run. Apply
requires an exact 64-character evidence fingerprint and the fixed authorization
contract. A stale fingerprint, changed durable population, incomplete exact
history, or identity conflict refuses before any mutation.

### 3. Exact state recovery

The recovery planner binds batch, lifecycle, binding, entry leg, position,
management leg, components, instruction population, mutation evidence,
protection ownership, and exact Deepcoin history.

For the observed absent-position case, it may terminalize local state only when
exact position history and exactly one verified owned stop prove the natural
close. This path constructs no writer and must report
`production_writes=0` and `exchange_calls=0` during dry-run. Apply writes only the
audited local recovery transition.

If the new capture instead shows a live position, uncertain close, manual close,
identity conflict, or incomplete pagination, recovery stops. No close, retry,
TPSL change, or protection replacement is inferred from the previous result.

### 4. Independent treatment of batches 123, 127, and 129

These batches receive separate read-only evidence bundles. The audit records
bounded statuses, reason codes, durable writer state, exact ownership, and
whether the batch is merely historical residue or requires active recovery.

No Batch 119 allowlist, fingerprint, or conclusion may authorize another batch.
If any of 123, 127, or 129 has an active/unknown writer, unprotected exposure, or
identity conflict, MiMo deployment remains blocked and a separate design is
required. Historical `recovery_required` rows may remain warnings only when
current exchange truth and durable ownership prove they cannot execute.

### 5. Deepcoin snapshot stability

The normal deployment helper consumes persisted read-only snapshots. Before the
code-deployment window, create two independent captures with distinct versions
and capture times. Both must be complete, fresh, and semantically identical for
positions and protection ownership. A repeated read of one cache file is not an
independent capture.

### 6. MiMo release continuity

Do not deploy `5702c34` after production has received the recovery code: its
older tree would remove the newly deployed safety fixes. The recovery session
must record the final deployed production SHA.

When returning to MiMo work, build the final MiMo release from that deployed
production SHA. If the deployed candidate already contains the reviewed MiMo
safe rebuild, the remaining MiMo work is verification, isolated replay, and
later activation—not redeploying the old isolated tree. MiMo remains `v1`
through every recovery and deployment step.

## Failure Handling

- Any service unit with an initial state other than exactly `active` or
  `inactive` aborts before backup.
- Any remaining local writer process aborts the stopped-service capture.
- Any durable active or unknown writer outside the exact incident aborts.
- Any mismatch between the two dry runs aborts and restores original service
  state.
- Any failed cleanup, worktree removal, service restoration, or SHA verification
  makes the operation fail.
- Before a possible exchange write, the verified database backup may be used for
  rollback. After a request might reach Deepcoin, never restore an older
  database; reconcile from exchange truth.
- The absent-position Batch 119 path must remain incapable of exchange writes.

## Verification

Local review must cover the complete `2274d90..b27541f` range, focused Batch 119
and Deepcoin authority suites, compileall, diff checks, and the full test suite.
Critical or Important findings stop the workflow.

Production verification is answer-first:

- exact candidate SHA and clean detached worktree;
- unchanged production SHA during diagnostic capture;
- stable double dry-run with `status=ready`;
- zero dry-run writes and calls;
- fresh apply fingerprint from a separately approved window;
- Batch 119 terminal or evidence-backed protected recovery state;
- batches 123/127/129 separately classified;
- no fresh active work, no unprotected positions, and stable exchange snapshots;
- active healthy service and unchanged `mimo_contract_mode=v1`.

## Completion Boundary

This design and its implementation plan authorize documentation and local
review only. They do not authorize stopping production, applying Batch 119
recovery, deploying `b27541f`, enabling any Deepcoin stage, or activating MiMo
v2. Each production window requires explicit approval in the new session.
