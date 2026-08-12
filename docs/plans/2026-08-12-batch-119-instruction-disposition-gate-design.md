# Batch 119 Instruction Disposition Gate Design

**Date:** 2026-08-12

**Status:** Approved

## Objective

Allow the dedicated batch 119 recovery planner to distinguish real instruction
work from legacy compatibility mirrors without rewriting historical rows or
weakening any global automatic-trading safety gate.

Production contains 186 non-retired instruction rows whose status is not
`succeeded` or `failed`. A read-only audit classified every row:

- one exact batch 119 source instruction is genuinely unresolved;
- 177 `submitted` rows have durable terminal business evidence;
- five `pending` rows have no current execution authority; and
- three historical `unknown` rows have terminal durable reconciliation but must
  remain frozen.

The current recovery predicate treats all 186 rows as active and also counts the
target batch's own instruction as additional work. This blocks the dedicated
recovery before any exchange call.

## Constraints

- Do not update, retire, replay, or backfill any historical instruction row.
- Do not redefine instruction terminality globally.
- Do not change MiMo settings, ordinary auto-trading, deployment preflight, or
  the normal message-instruction executor.
- Do not infer safety from age alone.
- Do not expose raw message text, strategy IDs, position IDs, order IDs, request
  payloads, or provider output.
- Any malformed, ambiguous, new, or drifting evidence must fail closed.
- The service remains stopped during the final locked apply boundary.

## Approaches Considered

### A. Batch-specific durable disposition attestation — selected

Classify every non-retired, non-`succeeded`/`failed` instruction from durable
database facts. Include a canonical digest and bounded counts in the recovery
source fingerprint. Recompute the same attestation after `BEGIN IMMEDIATE`
before any mutation.

This preserves historical truth and limits the exception to the already
allowlisted batch 119 command.

### B. Rewrite the 186 historical instruction statuses

Convert verified mirrors to `succeeded`, retire residue, and terminalize old
unknown rows. This is rejected because `submitted` intentionally records a
verified exchange write in the compatibility model. Bulk repair would change
historical semantics, could trigger summaries or monitors, and expands the
incident recovery into a general data migration.

### C. Treat `submitted` as terminal everywhere

Expand the global terminal-status allowlist. This is rejected because a raw
status cannot prove that a current or malformed instruction is safe. It would
weaken unrelated recovery and deployment paths.

## Architecture

Add a private, batch-119-only instruction population classifier inside
`composite_management_batch_recovery.py`. It returns an immutable payload with:

- schema version;
- exact population count;
- one count per approved disposition;
- a sorted per-row evidence digest; and
- no raw operational identifiers.

The approved dispositions are:

1. `target_incident_frozen`: exactly one instruction belonging to batch 119's
   raw message, lifecycle, strategy, and management action. It must remain
   `unknown`, contain a bounded `recovery_required` result, and link to the exact
   nonterminal target batch. It is the recovery subject, not additional work.
2. `verified_terminal_mirror`: an entry `submitted` mirror with an exact linked
   submitted trade signal and binding, or a management `submitted` mirror whose
   exact referenced batch is terminal and belongs to the same raw message.
3. `historical_residue_no_authority`: a `pending` row with no result, error,
   execution contract, management target, scheduled visibility retry, deadline,
   escalation, trade signal, or active descendant; its target lifecycle is
   absent or exited and any binding is absent or closed.
4. `historical_unknown_frozen`: an `unknown` row whose lifecycle and binding are
   closed and whose exact management or revision descendant is terminal. The row
   remains unchanged and cannot be replayed.

Any `executing` row, extra target-incident row, active descendant, scheduled
retry, unowned submitted payload, malformed JSON, mismatched identity, duplicate
link, or unknown disposition returns `additional_active_work_present`.

## Data Flow

During dry-run:

1. Load the exact batch, lifecycle, binding, leg, components, and protection
   evidence through the existing read-only session.
2. Classify the complete instruction population.
3. Add the canonical population payload to `_source_evidence_payload`.
4. Derive the existing source and recovery evidence fingerprints.

During apply:

1. Acquire `BEGIN IMMEDIATE` before the first source read.
2. Rebuild the complete instruction population payload from locked durable
   state.
3. Rebuild the source fingerprint and compare it to the approved plan.
4. Abort before every mutation on any population or evidence drift.

Resume authorization repeats the same classifier. It accepts only the same
bounded dispositions; progressed batch 119 component state does not authorize a
new or changed unrelated instruction.

## Security and Privacy

Each row contributes only canonical enums, booleans, timestamps or version
facts required for CAS, and SHA-256 references for row and linked-record
identity. Raw IDs are not emitted in serialized plan or audit output. JSON
payloads contribute validated bounded fields and fingerprints, never raw
contents.

## Testing

Tests use the real ORM and recovery planner/executor fixtures. TDD coverage must
prove:

- the audited four-class population becomes ready;
- the target instruction is required exactly once;
- verified entry and management mirrors are accepted only with exact durable
  links;
- the five pending-residue conditions are all required;
- historical unknown rows require terminal descendants and remain untouched;
- executing, scheduled, malformed, duplicate, active, and identity-drift rows
  fail closed;
- same-count content drift changes the source and evidence fingerprints;
- drift after dry-run conflicts under the write lock with zero mutations;
- normal unrelated recovery tests remain strict; and
- dry-run performs zero database or exchange writes.

## Deployment Boundary

Task 7A ends after reviewed commits are pushed. It does not deploy. Task 7 may
resume only from a new production read-only baseline and fresh exchange
snapshot. Live positions and pending orders are not closed to manufacture a
window; the dedicated window must separately prove that no writer is in flight
and every open position remains protected.
