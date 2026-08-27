# Deepcoin Reviewed Pending Entry Revision Gate Fix Design

## Decision

Align the reviewed pending-entry cancellation planner with the repository's
existing deployment active-write definition. A revision batch blocks planning
only while its batch status is `submitting_replacements`. Claimed revision
children remain separate blockers when a cancellation leg is
`cancel_submitting` or a replacement is `submit_reserved` and the parent has a
non-null claim token and claim timestamp.

## Why

Production contains three old `recovery_required` revision batches with no
claim token. Entry revision execution and planning already treat
`recovery_required` as a terminal/manual-attention state, and the authoritative
deployment active-write check does not count it as an active exchange write.
The cancellation candidate instead counted every revision batch outside
`succeeded` and `blocked`, so a safe read-only dry-run could never reach the
seven reviewed targets.

## Safety boundaries

- Do not rewrite or terminalize historical revision rows.
- Do not weaken the claimed-child checks.
- Continue to block `submitting_replacements` even without a child row.
- Continue to fail closed for active queue, order, management, mutation,
  protection, trade-signal, and worker-command authority.
- Make no change to cancellation writes, confirmation tokens, readback,
  ownership, fingerprints, history handling, or terminalization.

## Verification

Use TDD to prove that an unrelated unclaimed `recovery_required` batch does not
block an otherwise clean plan and that `submitting_replacements` still blocks
all actions. Run the reviewed-cancellation tests, adjacent active-write tests,
the relevant cancellation/entry/protection group, and one final full suite on
the completed production-code candidate.
