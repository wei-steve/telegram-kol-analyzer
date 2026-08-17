# Simple Gate Unknown Policy Design

> Superseded by 2026-08-17-minimal-deployment-gate-design.md.
> Retained only as historical context; do not execute this runbook.

## Context

The reviewed gate-only candidate at
`67de2296f5fc72e7ed814636d3e0c8396bbb1dd5` passed 482 focused server tests,
but its first production read-only Phase A returned `BLOCK` with one invalid
row and one unknown outcome. The shadow made no database, notification,
exchange, service, settings, or production-checkout changes.

Read-only diagnosis separated two causes:

- `source_message_deletion_exits.state = recovery_required` is a real state
  produced by the current source-deletion worker, but the evidence adapter does
  not recognize it. This is a classification defect.
- One strategy revision is durably `recovery_required` with reason
  `revision_cancel_outcome_unknown`; its affected entry legs remain pending and
  unassigned. No terminal proof exists. This is a genuine historical unknown,
  not malformed evidence.

There is no existing supported command that reconciles this revision without
designing a new exchange-readback and durable-state transition workflow. The
current executors intentionally freeze `submit_unknown` so that they cannot
repeat an ambiguous cancellation.

## Goals

- Keep the deployment decision table small and deterministic.
- Continue to block any active exchange write or malformed registered evidence.
- Continue to block unknown outcomes whenever the candidate changes the exact
  writer surface.
- Permit an unchanged-writer candidate to proceed only as an explicit `WARN`
  when historical unknown outcomes exist.
- Recognize the complete authoritative source-deletion state vocabulary without
  count-, timestamp-, row-, or environment-specific exceptions.
- Preserve the existing three approvals for push, read-only shadow, and
  deployment.

## Non-goals

- No manual database edits or historical replay.
- No operator override, force-pass flag, allowlist, current-count exception, or
  age-based exception.
- No exchange write, cancellation retry, or new revision reconciliation tool.
- No change to MiMo v1 authority, MiMo v2 watermarks, schema, execution policy,
  updater ordering, or rollback behavior.

## Considered Approaches

### 1. Writer-conditional unknown policy (selected)

Use the already reviewed exact writer fingerprint as the only condition for an
unknown outcome. Unknown plus changed writer is `BLOCK`; unknown plus unchanged
writer is `WARN`. This is the smallest policy that distinguishes a candidate
which can change retry/state-transition behavior from the gate-only candidate,
which cannot.

### 2. Add a second restart-compatibility fingerprint

This could classify restart handlers separately from exchange writers. It is
more precise, but it recreates the multi-surface compatibility machinery that
the simplified gate was designed to remove.

### 3. Build a revision reconciliation command

This retains an unconditional unknown `BLOCK`, but requires exact exchange
readback, identity proofs, durable state transitions, operator review, and its
own write-safety boundary. It is materially larger and riskier than the
gate-only deployment.

## Decision Matrix

The pure policy evaluates aggregate registered evidence in this order:

| Evidence | Writer unchanged | Writer changed |
|---|---:|---:|
| `invalid_evidence > 0` | BLOCK | BLOCK |
| `active_write > 0` | BLOCK | BLOCK |
| `unknown_outcome > 0` | WARN | BLOCK |
| `queued_work > 0` | WARN | BLOCK |
| none of the above | PASS | PASS |

When both unknown and queued work exist with an unchanged writer, the artifact
contains both stable WARN reason codes. There is no operator input to this
decision.

## Source-deletion Evidence Mapping

The adapter derives states only from the repository's authoritative producers:

- `unbound`, `succeeded`, `ignored`, `failed`, and `cancelled` are inactive.
- `pending` and legacy `waiting` are queued when their target/claim structure is
  internally valid.
- `closing_positions` and `reconciling` are queued orchestration states. Exact
  active or ambiguous exchange mutations remain represented by the registered
  management-batch/component adapters.
- `recovery_required` is an inactive, permanently paused state when it has no
  live claim. It is not silently retried.
- An unknown state, duplicate source event, broken target structure, one-sided
  claim, or inconsistent terminal/paused claim remains invalid.

The adapter will not special-case the currently observed row, its reason code,
its timestamp, or any production count.

## Artifact and Updater Behavior

Artifact construction and verification continue to recompute the decision from
the exact surface and evidence facts. A preliminary `WARN` has exit code 2 and
must verify as the same `WARN`. Phase B still binds to the independently saved
Phase A fingerprint and recollects all facts after the service stops. Any
writer drift, active write, malformed evidence, parent mismatch, or fact drift
fails closed.

## Testing and Review

Implementation follows strict RED/GREEN TDD:

1. Add pure decision tests for unknown with unchanged and changed writers, plus
   combined unknown/queued WARN reasons.
2. Add artifact and CLI tests proving collect/verify return stable WARN code 2
   for unchanged-writer unknown evidence and BLOCK code 3 when writer changes.
3. Add source-deletion tests for every authoritative state and malformed claim
   shape, including the production-shaped `recovery_required` row.
4. Preserve the future-change boundary tests against count, timestamp, and
   manual bypasses.
5. Run focused tests, the full repository suite, static checks, surface
   classification, and an independent Critical/Important review.
6. Require a new explicit push approval, then rerun the same server candidate
   test set and read-only Phase A. A passing shadow still requires a separate
  deployment approval.
