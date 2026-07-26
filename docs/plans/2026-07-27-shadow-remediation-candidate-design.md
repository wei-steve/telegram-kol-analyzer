# Shadow Remediation Candidate Design

## Problem

`repair-position-management --apply` can identify a safe action for an old
shadow management batch, but it cannot promote the action when the original
signal candidate is already canonical. The remediation path reuses that source
candidate, so the normal planner finds and returns the original
`management_shadow_plan_only` batch instead of creating the required disabled
preflight batch.

## Design

Every approved remediation action gets a distinct `approved_remediation`
candidate. The candidate copies the reviewed source identity and normalized
management fields, uses a remediation-specific recognition generation, and is
bound to the approved action fingerprint.

The original candidate and shadow batch remain immutable audit evidence. The
new candidate lets the existing planner create a separate
`management_disabled_plan_only` batch. The existing remediation gates then
verify the batch target, predecessor signature, exchange snapshot fingerprint,
live management setting, and exact position set before promoting that batch to
live execution.

## Safety and verification

- Add a regression test with a canonical source candidate and an existing
  shadow batch.
- Assert the apply path creates a distinct remediation candidate and does not
  reuse or mutate the source candidate.
- Assert the action executes once through the normal management executor.
- Run the focused remediation and management test suites.
- Deploy through the reviewed Git branch, rerun a production dry-run, and apply
  each approved action with a freshly generated fingerprint.
