# Historical Position Attribution Cleanup Design

## Problem

The production database contains historical Deepcoin entry legs that retain
duplicate or conflicted `pos_id` values after the underlying exchange positions
have disappeared. Current live-position authority remains fail closed, but the
historical rows have three operational effects:

- the partial unique index on `(venue, pos_id)` cannot be installed;
- nonterminal conflicted legs and `unknown` bindings remain recoverable-looking
  even though they do not authorize live operations; and
- some execution-backed lifecycles remain `entered` without a live position,
  which can pollute strategy holding counts.

The production audit on 2026-07-15 found 250 entry legs, 127 rows with a
`pos_id`, 112 distinct position IDs, and 9 duplicated position IDs spanning 24
rows. It also found 30 nonterminal conflicted legs across 25 bindings and 28
historical position IDs. None of those conflicted position IDs was present in
the current exchange snapshot. The current three live positions were uniquely
verified and are outside this cleanup scope.

## Goals

- Extend the existing fingerprinted repair workflow to express historical
  cleanup explicitly instead of using direct SQL or an untracked one-off
  script.
- Preserve immutable evidence before changing any historical ownership field.
- Clear only duplicate or stale ownership claims that current exchange and
  historical execution evidence prove are no longer authoritative.
- Derive leg, binding, and lifecycle terminal state together when the evidence
  proves one transition.
- Install the partial unique `(venue, pos_id)` index only after all duplicates
  are resolved.
- Keep current live positions and every exchange order mutation outside this
  workflow.

## Non-Goals

- Do not close, reduce, bind, or otherwise modify a live exchange position.
- Do not cancel or replace pending regular, trigger, or TPSL orders.
- Do not infer a historical owner from symbol, side, price proximity, or the
  absence of a live position alone.
- Do not convert research-only lifecycles without an execution binding into
  execution lifecycles.
- Do not bypass the planner, fingerprint, database backup, or review gates.

## Chosen Approach

Extend `repair-position-attribution` and its existing plan/apply model. The
planner receives one coherent Deepcoin snapshot plus local bindings, entry
legs, lifecycles, execution events, close reservations, and prior attribution
audits. It produces deterministic historical cleanup actions in the same
fingerprinted plan as current attribution repairs.

A one-off cleanup script was rejected because it would create a second source
of repair rules and weaker drift protection. Direct SQL was rejected because it
cannot revalidate exchange state, cannot preserve a structured evidence trail,
and can leave leg, binding, and lifecycle state inconsistent.

## Historical Cleanup Eligibility

A historical cleanup component is eligible only when all required exchange
evidence sources succeeded and every position ID in the component is absent
from:

- current nonzero positions;
- pending regular entry orders;
- pending trigger entry orders; and
- pending position-linked protection orders when Deepcoin exposes a usable
  position identity.

Absence from the current position snapshot is necessary but not sufficient.
At least one terminal evidence class must also exist:

1. a lifecycle already records `exited` or `cancelled` with an exit reason and
   timestamp;
2. a project execution event proves the exact close or cancellation;
3. a completed bound-position close reservation proves the exact position was
   closed; or
4. exchange order/fill history proves the originating entry is terminal and no
   live position or pending order remains.

Manual lifecycle terminal records remain valid evidence. A missing or failed
API source changes the component to `evidence_unavailable` and produces no
cleanup action.

An `entered` lifecycle without terminal evidence is never automatically exited.
Binding 23 / lifecycle 325, binding 96 / lifecycle 420, and binding 114 /
lifecycle 444 must therefore be evaluated individually. Insufficient evidence
leaves them unchanged and emits an unresolved cleanup conflict.

## Action Model

The planner may emit the following deterministic action types:

- `clear_redundant_historical_position`: clear a leg's non-authoritative
  `pos_id` after recording its old value, candidate component, and proof that it
  cannot authorize a live position.
- `terminalize_historical_entry_leg`: move a nonterminal leg to the evidence-
  proven terminal state and set a specific terminal reason.
- `close_historical_binding`: clear derived binding ownership and move the
  binding to `closed` only when all entry legs are terminal and no pending order
  remains.
- `exit_historical_lifecycle`: move an execution-backed lifecycle to `exited`
  with an evidence-derived reason and timestamp only when the exact strategy
  transition is proven.
- `install_position_ownership_unique_index`: install
  `uq_execution_order_legs_venue_pos` only when a fresh duplicate check returns
  zero rows immediately before index creation.

Actions are ordered by component, then binding ID, leg ID, and action type. The
same evidence produces the same plan and fingerprint regardless of database or
API row order.

## Ownership Preservation Rules

When one historical leg has authoritative direct evidence such as
`order_id == pos_id`, an exact response `posId`, or an already reviewed
equivalent-permutation assignment, the planner may retain that historical
`pos_id` and clear only competing claims. Retention records historical
provenance; it does not grant live-operation authority to a terminal leg.

If no leg has unique authoritative evidence, every competing leg remains
`attribution_conflict` unless terminal evidence permits clearing the ownership
field from all candidates while preserving the original component in immutable
audit rows. The planner never selects a winner merely to satisfy the index.

Cross-binding duplicate components require stronger evidence than same-binding
duplicates. A component with an outside candidate edge, conflicting history,
or incomplete evidence is unresolved and blocks apply.

## Audit and Fingerprint

Every action includes:

- affected binding, leg, lifecycle, venue, and position IDs;
- prior and proposed states;
- exact evidence class and relevant order/event identifiers;
- the coherent exchange snapshot fingerprint;
- the database fingerprint; and
- the cleanup policy version.

Apply rebuilds the plan from fresh exchange and database evidence. A nonempty
plan requires `--expected-fingerprint`; any drift, API error, newly live
position, pending order, or unresolved conflict refuses the entire apply.
Historical values are written to immutable attribution audits before mutable
fields change, within the same database transaction.

## Failure Handling

- API error: no historical cleanup action; report `evidence_unavailable`.
- Position or order reappears: refuse cleanup for the entire component.
- Ambiguous historical owner: preserve conflict and report candidates.
- Missing terminal evidence: preserve leg, binding, and lifecycle state.
- Fingerprint drift: refuse apply without partial changes.
- Unique-index creation failure: roll back the transaction, including all
  cleanup actions in that apply.
- Unexpected live-position intersection: refuse the whole apply.

## Test Matrix

Tests must cover:

- same-binding duplicate legs with one exact direct owner;
- cross-binding duplicates with one exact direct owner;
- ambiguous cross-binding duplicates remaining unresolved;
- terminal manually closed legs clearing redundant ownership while retaining
  immutable audit evidence;
- closed/unassigned historical legs cleaning safely;
- nonterminal conflicts without terminal evidence remaining unchanged;
- entered lifecycles requiring exact terminal evidence;
- research-only lifecycles without execution bindings remaining untouched;
- pending regular, trigger, or position-linked protection orders blocking
  cleanup;
- API failure blocking all cleanup for the affected component;
- current live position IDs never appearing in cleanup actions;
- deterministic plans under shuffled inputs;
- stale fingerprint refusal;
- transaction rollback on unique-index failure;
- idempotent re-run producing zero actions after a successful apply; and
- database rejection of a new duplicate after the unique index is installed.

## Production Rollout

1. Implement locally with test-driven development.
2. Run focused repair, attribution, execution-action, database-bootstrap, and
   Web tests, then the full local suite.
3. Commit and push reviewed changes to `codex/deepcoin-auto-trading-v1`.
4. Verify the production global automatic-trading switch remains false.
5. Create a timestamped production database backup and record its size and
   SHA-256 before deployment or repair.
6. Deploy through the existing GitHub-to-server update helper.
7. Fetch a fresh coherent exchange snapshot and run the updated command without
   `--apply`.
8. Review every historical cleanup action, unresolved conflict, and the exact
   fingerprint. Confirm the three current live position IDs are absent from all
   cleanup actions.
9. Stop and request explicit operator approval for any nonzero production plan.
10. Apply only the unchanged reviewed fingerprint after approval.
11. Re-run dry-run, verify zero remaining actions, verify the unique index, and
    confirm current live attribution, Web holding counts, service health, and
    global automatic trading state.

This design authorizes implementation and deployment through the production
dry-run stage only. It does not pre-authorize a nonzero production apply.
