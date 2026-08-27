# Deepcoin Contract Cache Task 12 Version-Aware Gates Design

## Problem

Task 12 currently requires the deployed production version to satisfy contracts
that are introduced by the candidate itself. In particular, it expects the real
cache to fail candidate inspection only on owner drift, requires the candidate
contract-spec health endpoint before deployment, treats bounded Deepcoin history
responses as incomplete deployment evidence, and hard-codes the observed refusal
count. This creates an upgrade loop: a safe old version cannot pass the gate that
would authorize installing its repair.

The fix is limited to the Deepcoin contract-cache ownership workflow. It does not
change the global deployment framework, runtime trading decisions, exchange-write
authority, or production monitor behavior.

## Decision

Split Task 12 evidence into three closed classes:

1. **Invariant pre-deploy gates** must pass on every version. These include the
   approved candidate SHA, production checkout identity, tracked-tree state,
   split-runtime topology, unique Telegram session owner, SQLite integrity, and
   complete zero counts for active exchange writes, claims, management work,
   worker commands, and revision claims.
2. **Recognized migratable legacy drift** may pass only when every observed
   difference is owned by the candidate's transactional updater and rollback.
3. **Post-deploy candidate gates** are evaluated only after the candidate is
   installed and while automatic entry remains frozen.

Anything outside these closed classes remains unknown and fails closed.

## Cache ownership classification

The pre-deploy helper inspection may report either a fully satisfied candidate
contract or the one recognized legacy cache state:

- the parent is the fixed production data directory;
- the target exists as one regular, single-link inode;
- the target owner is root;
- the target group and mode already match the runtime contract;
- the worker owner check fails;
- the explicit Agent deny ACL is either already satisfied or absent;
- no other ACL, type, link, directory-entry binding, or metadata error exists.

The root owner is the required legacy owner drift. An already satisfied Agent
deny ACL is preserved; an absent Agent deny ACL is the only migratable ACL drift.
The candidate helper changes the owner and, when needed, adds the ACL through the
validated descriptor, while the updater installs that helper within its tested
rollback boundary.
Unknown owners, unexpected groups or modes, symlinks, hardlinks, directories,
FIFOs, entry replacement, unreadable ACLs, duplicate ACLs, and any unclassified
error still stop Task 12.

After deployment, legacy classification is no longer accepted. The helper
`--check` must report the complete worker owner, runtime group, `0660`, regular
single-link target, and explicit Agent deny ACL contract.

## Capability-aware health gate

Before deployment, the production health endpoint is classified by capability:

- a present endpoint must return HTTP 200 and the exact validated schema;
- an HTTP 404 may be recorded as `legacy_capability_absent` only when production
  is still on the verified previous SHA, the recognized legacy monitor-env
  schema passes its closed validation, the same token succeeds against the exact
  worker port's authenticated monitor-capture health endpoint, loop health proves
  the worker runtime role, and the exact previous SHA's route inventory proves
  that the contract-spec route does not exist;
- authentication failures, timeouts, non-404 HTTP errors, malformed responses,
  or an endpoint that exists with an invalid schema remain blockers.

HTTP 404 alone is never absence proof because authentication and runtime-role
refusals are intentionally indistinguishable from route absence.

After deployment, `legacy_capability_absent` is forbidden. The candidate worker
health endpoint must return HTTP 200 with the exact schema before any automatic
entry restoration can be considered.

## Deepcoin evidence boundary

Task 12 continues to require complete current-account snapshots: positions,
pending regular orders, pending trigger/TPSL orders, and unique attribution for
any active rows. It also requires active local mutation state to be zero.

An otherwise valid history or fills response that reaches the venue's fixed
100-row window is recorded as bounded historical coverage, not as proof of full
account history and not as a cache-migration blocker. Any active row that needs
older evidence must still fail closed. Network errors, invalid schemas, ambiguous
active ownership, and incomplete current-account queries retain the existing
single-retry rule and remain blockers.

## Refusal and replay boundary

The refusal count is an observed freeze-time baseline, not a hard-coded number.
Task 12 records the exact set of `contract_spec_sync_unavailable` contracts and
proves every row is terminal `verified_refusal` with
`attempted_exchange_write=0`. The current observed count may therefore move from
15 to 16 without redefining history.

No baseline row is replayed, backfilled, resubmitted, or converted into a pending
instruction. New refusals before freeze are added to the baseline only after the
same terminal zero-write proof. Freeze and restore watermarks remain audit-only
future-signal boundaries.

## Updater and rollback boundary

The candidate updater remains responsible for the recognized migration:

- validate the legacy monitor env before checkout;
- preserve its original bytes and metadata;
- install the candidate monitor units, governed expectation, and cache helper in
  the existing transaction;
- restore the previous checkout, units, env, and timer state on failure;
- keep automatic entry frozen after any failed deployment attempt.

The version-aware Task 12 classification grants no deployment, freeze, restart,
settings write, database write, exchange write, replay, or notification authority.

## Verification

RED tests will first prove that the current documentation rejects the observed
root-owned cache with absent Agent ACL, incorrectly requires the candidate health
endpoint from the previous SHA, treats a valid 100-row history window as a cache
deployment blocker, and hard-codes the refusal total.

GREEN changes will update the implementation plan, runbook, canonical status,
and their static acceptance tests to encode the closed legacy classification and
strict post-deploy contract. Existing updater, descriptor/ACL, monitor redaction,
rollback, unknown-owner, and no-replay tests remain unchanged and must stay green.
