# Runtime Agent Stale Context Claim Recovery Design

## Scope

Implement the next Phase 6 playbook,
`recover_stale_side_effect_free_claim`, for one exact worker family:
`context_resolution_attempt`. The handler may return an expired contextual
reanalysis claim to its existing safe queue. It must not rerun contextual
resolution itself, choose a strategy, alter a contextual decision, or reach any
trading mutation.

The handler remains dormant unless the existing Agent flag, action flag, and
exact playbook allowlist are all enabled. Unsupported source kinds and
incident types continue to fail closed as `executor_not_configured` or handler
failure.

## Considered Approaches

### 1. Reuse the worker's ordinary stale-claim takeover

The existing context worker can atomically replace a stale `running` claim
when it next polls. This is already safe, but it does not perform the named
playbook or produce action-specific verification evidence. Treating ordinary
worker polling as a successful Agent action would make the executor ledger
claim work it did not perform.

### 2. Release with a live database read and verify with a second live read

The Agent could compare-and-set the stale row back to `pending_reanalysis`,
then use the existing `get_worker_state` projection. This is simple, but the
normal context worker may reclaim the row between the action and verification.
The recovery would then be falsely frozen as a verification failure.

### 3. Atomic release with one-shot bounded verification proof

Use a dedicated coordinator in the Agent process. It revalidates the current
incident and target row, performs one exact compare-and-set release, and stores
a bounded in-memory proof of the committed transition. `get_worker_state`
consumes that proof exactly once for executor verification, then returns to its
ordinary passive projection.

This is the selected approach because it preserves the authoritative worker,
avoids a verification race, and proves only the transition actually committed
by this action.

## Safety Contract

The action is accepted only when all of these remain true at execution time:

- the runtime incident exists and its fingerprint matches the executor's
  expected fingerprint;
- the incident type is `context_worker_exhausted`;
- the source kind is exactly `context_resolution_attempt` and the source ID is
  a positive integer;
- the incident's bounded summary declares `claim_status: stale` and
  `claim_side_effect_class: none`;
- the source attempt exists, is exactly `running`, has a nonempty claim token,
  and its `claimed_at` is at least the authoritative five-minute
  `DEFAULT_STALE_AFTER` threshold old;
- no terminal message instruction, strategy-management batch, or
  strategy-revision batch exists for the attempt's raw message.

The update compares the exact target ID, `running` status, claim token, and
original `claimed_at`. It changes only:

- `status` to `pending_reanalysis`;
- `claim_token` and `claimed_at` to null;
- `next_attempt_at` and `updated_at` to the action time.

It preserves attempts, fingerprints, trigger evidence, prior errors,
decisions, and every business table. If another worker wins the race, the
compare-and-set affects zero rows and the action fails closed.

## Verification

After a successful commit, the coordinator stores at most 32 one-shot proofs,
keyed by incident ID. The proof contains only:

- `applicable: true`;
- `safe_queue_restored: true`;
- `claim_status: pending`;
- `business_write_owned: false`;
- the numeric context attempt ID.

`get_worker_state` consumes the proof and emits only
`incident:<id>` and `context-attempt:<id>` evidence references. A second call
returns the passive durable projection. No claim token, message text, prompt,
provider response, or contextual decision is exposed.

## Failure and Rollback

Invalid identity, incomplete proof, live claim, business-write ownership,
compare-and-set contention, or database failure returns no successful handler
signal. The existing executor freezes or refuses according to its durable
rules.

Rollback requires no schema change: clear the exact playbook allowlist and
action flag, then stop the dormant sidecar if necessary. The normal context
worker keeps its pre-existing stale-claim takeover behavior regardless of this
handler.

## Test and Canary Plan

Tests must first fail for:

- exact stale context claim recovery and one-shot verification;
- rejection of live, malformed, mismatched, unsupported, and
  business-write-owned targets;
- compare-and-set race loss;
- bounded proof retention;
- production handler injection and missing-handler refusal;
- unchanged authoritative worker behavior and all existing executor gates.

After review and local regressions, deploy with every Agent/action flag off.
Use a temporary database canary containing one synthetic stale context claim
and exactly one in-process action allowlist. Verify `action_verified`, prove
the production incident and business ledgers are unchanged, and keep the
production sidecar disabled.
