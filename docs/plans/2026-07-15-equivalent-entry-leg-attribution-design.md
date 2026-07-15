# Equivalent Entry-Leg Attribution Design

## Problem

Deepcoin split-position trigger orders may create a new internal regular order
whose order ID becomes the position ID. The REST trigger-history response keeps
the parent trigger order ID, while fills and positions expose the child regular
order ID. Deepcoin does not expose a documented parent-trigger-to-child-order
link.

The project currently creates two 50% limit/trigger entry legs even when the
recognized entry is one price. If both legs have the same normalized price,
size, side, TP, SL, and trigger time, two resulting positions form a symmetric
two-by-two attribution graph. Their strategy ownership is certain, but their
entry-leg permutation is not observable from REST evidence.

## Goals

- Prevent new duplicate entry legs when the strategy contains one entry price.
- Merge range legs that become identical after exchange price/quantity
  normalization.
- Use distinct position protection only as supporting evidence when it creates a
  mutual-unique match; never alter trading prices merely to create identity.
- Recover exact operational authority for historical, economically equivalent
  permutations without pretending Deepcoin proved the parent-child mapping.
- Preserve fail-closed behavior for every non-equivalent or cross-strategy
  ambiguity.

## Entry-Leg Construction

### Single-price entry

An entry is single-price when the normalized low and high are equal. Limit,
trigger-limit, and market execution must produce one entry leg with 100% of the
risk budget. Existing near-market conversion may still select market execution,
but it must not affect the one-leg invariant.

### True range entry

A true range may produce two legs using the configured range style. After price
tick and quantity-step normalization, the builder must compare the final legs.
If the legs are economically identical, they are merged into one leg containing
the combined risk allocation and quantity. A range that collapses to one price
must therefore never submit duplicate orders.

The invariant is checked both in draft construction and immediately before live
submission so legacy or externally prepared drafts cannot bypass it.

## Protection Evidence

TP/SL evidence may distinguish entry legs only when all of the following hold:

- each position exposes its own protection values as direct position evidence;
- each candidate leg has persisted intended protection values;
- the normalized protection signatures differ between legs;
- the result is mutual-unique across all candidate legs and positions; and
- no later manual or automated protection mutation makes the entry-time values
  stale.

Equal TP/SL values add no identity information. The system must not create
artificial TP/SL differences for attribution.

## Equivalent-Permutation Recovery

A conflict component may be canonicalized only when:

- every candidate leg belongs to the same execution binding and strategy
  instance;
- the number of legs equals the number of live positions;
- no leg or position has an edge to a candidate outside the component;
- every leg has successful entry evidence and is nonterminal;
- symbol, side, venue, order kind, normalized entry/trigger price, requested
  quantity, stop loss, take-profit signature, margin mode, and position mode are
  identical;
- every live position has compatible size, entry price, creation time, and
  protection signature;
- there is no cancellation, manual-close, partial-fill, or API-evidence error;
  and
- no position already has another authoritative owner.

The canonical assignment sorts legs by stable leg ID and positions by numeric
position ID, then pairs them in order. It is deterministic and idempotent; it is
not random.

Persisted evidence uses a dedicated type such as
`equivalent_permutation_assignment` and records the whole component, the
equivalence fields, and the canonical ordering. The audit message must state
that binding ownership was proven while the parent-child permutation was
canonicalized.

Because every leg is economically equivalent and belongs to the same binding,
the resulting exact position records may pass the existing ownership authority
gate and resume normal binding-level close and TPSL management. Any later
evidence that breaks equivalence invalidates this authority and returns the
component to conflict.

## Historical Miya Snapshot

Before deployment, one observed snapshot showed Binding 126 with two successful
BTC long trigger legs and two then-live BTC long positions. In that historical
snapshot, both sides had identical normalized price, quantity, trigger time,
TP, SL, and strategy identity, with no outside candidates. It was the
production-shaped fixture for equivalent-permutation recovery; it is not a
statement of current exchange state or a production verification result.

On 2026-07-15 the operator manually closed the two unattributed positions
suspected to belong to Miya before this code was pushed or deployed. That action
invalidated the old repair plan, its fingerprint, and the expectation of exactly
two Miya assignment/apply actions. After deployment, fetch a fresh coherent
snapshot of positions, open TPSL orders, pending triggers, and relevant entry
history, then keep the audit read-only through a new repair dry run. A `posId`
absent from the new snapshot must never receive verified ownership or be sent a
close request. Zero actions is valid and authorizes no modification. Every
nonzero action, including terminal/manual transitions or stale
leg/binding/lifecycle cleanup, must be explicit in the fingerprinted plan and
separately reviewed before `--apply`; never bypass the planner by editing the
database directly.

## Failure Handling

- Missing or failed API evidence never authorizes canonicalization.
- Different quantities, prices, protection, strategy IDs, or terminal states
  keep the component conflicted.
- A component spanning multiple bindings is never canonicalized.
- Reconcile and repair must use the same eligibility function.
- Live mutation continues to require one authoritative persisted owner per
  position.

## Verification

- Builder tests for single-price market and limit entries producing one leg.
- Builder tests for true ranges producing two distinct legs.
- Tick/step normalization tests that merge collapsed legs and preserve total
  risk/quantity.
- Submission-gate tests rejecting duplicate equivalent legs in legacy drafts.
- Attribution tests for deterministic input-order-independent canonicalization.
- Negative tests for different bindings, unequal quantities/prices/TP/SL,
  cancellation, partial fill, API failure, and outside candidate edges.
- Authority tests proving canonical assignments can be managed and noneligible
  conflicts cannot.
- Repair tests covering dry-run, audited apply, idempotency, stale-plan refusal,
  and the production-shaped Miya incident.
- Local full suite followed by GitHub push and server deployment. Then capture a
  fresh production snapshot and dry run. Apply only separately reviewed nonzero
  actions without fingerprint drift; zero actions requires no apply. Restart
  and Web verification are conditional on the actual reviewed result, and this
  design does not claim that production has already been verified.
