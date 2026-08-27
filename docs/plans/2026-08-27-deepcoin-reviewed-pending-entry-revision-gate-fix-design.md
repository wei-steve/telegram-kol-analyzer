# Deepcoin Reviewed Pending Entry Revision Gate Fix Design

## Decision

Treat `succeeded`, `blocked`, `failed`, and `recovery_required` as terminal
revision batch states, but allow a terminal batch only when it has no claim
evidence and no ambiguous child write. Every non-terminal batch, any non-null
claim token or claim timestamp, a revision leg in `cancel_submitting` or
`submit_unknown`, and a replacement in `submit_reserved` or `submitted` blocks
the reviewed pending-entry cancellation planner.

## Why

Production contains three old `recovery_required` revision batches with no
claim token. Entry revision execution and planning already treat
`recovery_required` as a terminal/manual-attention state. The cancellation
candidate instead counted every such batch as active, so a safe read-only
dry-run could never reach the seven reviewed targets. Unlike a deployment
restart gate, however, this cancellation gate must also preserve the
unknown-outcome no-retry boundary: executor recovery clears the parent claim
but can intentionally leave an ambiguous child state behind.

## Safety boundaries

- Do not rewrite or terminalize historical revision rows.
- Block any claim evidence, including a half-present token or timestamp.
- Block ambiguous child writes even after the parent claim is cleared.
- Continue to block every non-terminal batch, including
  `submitting_replacements`, even without a child row.
- Continue to fail closed for active queue, order, management, mutation,
  protection, trade-signal, and worker-command authority.
- Make no change to cancellation writes, confirmation tokens, readback,
  ownership, fingerprints, history handling, or terminalization.

## Verification

Use TDD to prove that an unclaimed, child-clean `recovery_required` batch does
not block an otherwise clean plan; non-terminal and claimed batches do block;
and ambiguous cancellation/replacement children block even without a parent
claim. Run the reviewed-cancellation tests, adjacent active-write tests, the
relevant cancellation/entry/protection group, and one final full suite on the
completed production-code candidate.
