# Deepcoin Reviewed Pending Entry Revision Gate Fix Design

## Decision

Treat `succeeded`, `blocked`, `failed`, and `recovery_required` as terminal
revision batch states. Every non-terminal batch and any non-null claim token or
claim timestamp remain global blockers. An ambiguous child in a terminal,
claim-free batch blocks only when its parent binding, execution leg, or order
identity overlaps the fixed reviewed target set.

## Why

Production contains three old `recovery_required` revision batches with no
claim token. Entry revision execution and planning already treat
`recovery_required` as a terminal/manual-attention state. The cancellation
candidate instead counted every such batch as active, so a safe read-only
dry-run could never reach the seven reviewed targets. Unlike a deployment
restart gate, however, this cancellation gate must also preserve the
unknown-outcome no-retry boundary: executor recovery clears the parent claim
but can intentionally leave an ambiguous child state behind. Production also
contains three old `submit_unknown` children for one order outside the fixed
seven and outside all reviewed bindings. That terminal history cannot be
confirmed or rewritten, but it has no authority to retry or mutate a reviewed
order and must not permanently block this closed-target operator tool.

## Safety boundaries

- Do not rewrite or terminalize historical revision rows.
- Block any claim evidence, including a half-present token or timestamp.
- Block ambiguous child writes after the parent claim is cleared whenever their
  binding, execution leg, or order identity overlaps a reviewed target.
- Ignore only terminal, claim-free ambiguous children that are disjoint from
  every reviewed binding, execution leg, and order ID.
- Continue to block every non-terminal batch, including
  `submitting_replacements`, even without a child row.
- Continue to fail closed for active queue, order, management, mutation,
  protection, trade-signal, and worker-command authority.
- Make no change to cancellation writes, confirmation tokens, readback,
  ownership, fingerprints, history handling, or terminalization.

## Verification

Use TDD to prove that terminal, claim-free ambiguous cancellation/replacement
children on an unrelated binding do not block; the same child states on a
reviewed binding, execution leg, or order do block; and non-terminal or claimed
batches remain global blockers. Run the reviewed-cancellation tests, adjacent
active-write tests, the relevant cancellation/entry/protection group, and one
final full suite on the completed production-code candidate.
