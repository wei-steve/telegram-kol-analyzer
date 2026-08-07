# Management Safety-Gate Current-Evidence Recovery Design

## Problem

The Flyang BTC-long remediation exposed a liveness defect in the management
safety gates. A reviewed partial-take-profit action was rebuilt from one fresh,
complete Deepcoin snapshot and matched the exact live `posId`, binding, leg,
size, and existing protection. Planning nevertheless returned
`protection_recovery_required` solely because an immutable historical
`PositionProtectionIncident(protection_missing)` row existed.

The incident row described a real temporary replacement gap when it was
created, but the current exchange state is healthy: the exact position has a
verified primary stop, an independent backup stop, and a verified take profit.
`_protection_incident_requires_recovery()` currently treats history as current
state by testing only whether any matching incident row has ever existed. The
read-only convergence audit already proves that those are different facts, but
the management planner does not consume an equivalent current-health proof.

This creates the wrong failure mode: a guard intended to prevent unsafe writes
can indefinitely prevent an explicitly risk-reducing operation, even after the
underlying risk has disappeared. The attempted apply made no exchange write,
so the immediate incident is a false freeze rather than an unknown submission.

## Decision

Keep incident history immutable and keep all genuinely hard execution gates.
Replace the historical-row boolean with a fail-closed current-health decision
derived from the same coherent exchange snapshot already frozen by planning.

The gate has three outcomes:

- `healthy_current_evidence`: the exact live position has complete,
  conflict-free current protection evidence, so a historical protection
  incident does not freeze a risk-reducing management action;
- `recovery_required`: current evidence proves protection is missing,
  conflicting, or unhealthy;
- `evidence_insufficient`: pagination, ownership, identity, or exchange
  evidence is incomplete, so no write is authorized.

Historical incident rows are never updated or deleted. Each evaluation appends
a bounded `PositionProtectionHealthObservation` carrying the exact scope,
classification, evidence fingerprint, source incident IDs, and observation
time. This separates the audit trail from the current projection and makes the
planner's decision explainable after the fact.

## Safety boundary

Current evidence may neutralize only a recoverable historical protection
freeze. It must never bypass:

- exact-position, lifecycle, binding, or leg identity conflict;
- incomplete positions or pending-TPSL pagination;
- unowned or multiply owned native TPSL orders;
- submission whose exchange outcome is unknown;
- a batch or mutation intent that may already have submitted;
- changed position size, side, instrument, order set, or contract spec between
  review and execution;
- duplicate action or concurrent active management batch;
- missing or same-ID primary and backup stops;
- any risk-increasing instruction.

The healthy proof requires one complete reconciliation snapshot, the canonical
account-wide order-ownership index, the existing native TPSL matcher, the exact
live position, distinct visible primary and backup stops, and all position
protection orders attributable without conflict. The planner must reuse
`build_position_protection_audit()` and the convergence matching rules; it must
not invent a looser symbol/side/price matcher.

## Frozen decision evidence

One batch fingerprint binds all evidence that authorized planning:

- exchange snapshot fingerprint and per-instrument completeness;
- exact `posId`, venue, binding ID, execution-leg ID, instrument, side, and
  current size;
- canonical owned primary, backup, and take-profit order identities and their
  relevant price/size semantics;
- current-health classification and observation fingerprint;
- source message, recognition generation, requested fraction, contract spec,
  and planned close size;
- the intended post-close protection set.

Immediately before the first exchange mutation, the executor performs a fresh
complete read-back and recomputes the combined fingerprint. Any drift blocks
the action. Evidence that merely becomes healthier does not silently widen the
approved operation; it requires rebuilding and reviewing a new action.

## Retry and supersession semantics

A prior batch may be superseded only when it is demonstrably a zero-submission
preflight refusal:

- status is `blocked`;
- it has no durable management legs or mutation intents;
- no exchange request was attempted;
- its reason is explicitly recoverable, including historical
  `protection_recovery_required` now resolved by current evidence;
- the replacement keeps the same message/lifecycle/action idempotency identity
  and records the predecessor batch ID and new evidence fingerprint.

Any `submitted`, `submit_unknown`, `executing`, `reconciling`,
`recovery_required`, or otherwise ambiguous predecessor remains frozen for
read-only reconciliation. It is never replaced or blindly retried.

## Partial-close protection saga

Flyang's reviewed 50% take-profit originally had size six and an intended close
size of three, but the user has since closed the position manually. It is now a
historical regression fixture only and must never be replayed. For a future
equivalent live action, the operation must be a single durable saga rather than
an isolated market close:

1. Re-read the exact six-contract position and complete protection set.
2. If the venue will reject or implicitly resize oversize TPSL orders, cancel
   or replace only the exact owned take-profit/protection orders required by
   the existing composite-management policy.
3. Submit one idempotent reduce-only close for exactly three contracts.
4. Reconcile the close outcome; an unknown result stops the saga.
5. Confirm the exact remaining size is three.
6. Rebuild or resize the take profit to three contracts and verify it pending.
7. Preserve the requested stop at 64100 and its independent backup; do not move
   either to break-even because the source message did not authorize that.
8. Mark success only after the remaining position and complete primary,
   backup, and take-profit roles are verified from a fresh read-back.

The existing `partial_then_break_even` composite machinery provides the model
for cancellation, close confirmation, and protection convergence, but this
action remains `partial_take_profit`; protection maintenance must not change
the message's strategy semantics.

## Safety-gate divergence incident

Add a deterministic shadow rule for `management_safety_gate_divergence` when:

- a management action is refused only because of historical protection
  incidents; and
- the same complete exact-position evidence classifies current protection as
  healthy.

The observation includes only bounded IDs/fingerprints and reason codes. It
does not execute, select a strategy, or notify during the first rollout. After
shadow verification, repeated divergence may create one runtime incident
generation and one operator alert. This makes a malfunctioning guard visible
without allowing the Runtime Incident Agent to bypass it.

## Components

- `models.py` and the additive migration add the append-only health
  observation and optional predecessor/evidence references needed for safe
  supersession.
- `protection_health.py` owns the reusable exact current-health classifier.
- `strategy_management_planner.py` replaces the incident-exists boolean with
  the three-way classifier and persists the frozen evidence.
- `position_management_remediation.py` includes the current-health observation
  in action/chain fingerprints and promotes only an eligible zero-submission
  blocked batch.
- `strategy_management_executor.py` and reconciliation code enforce final
  fingerprint read-back and complete the partial-close protection saga.
- runtime incident observation/rule modules detect gate divergence in shadow.

## Rollout and rollback

1. Ship schema and current-health observation with planner behavior unchanged.
2. Run shadow comparison between the old historical gate and the new
   classifier; require no false-healthy result and no exchange writes.
3. Enable current-evidence recovery only for exact risk-reducing actions and
   only for explicitly eligible zero-submission predecessors.
4. Verify that the manually closed Flyang position produces no executable
   remediation action and that every earlier action/fingerprint is stale.
5. Enable the corrected gate only for new or still-live exact actions after a
   fresh plan and explicit review; never replay Flyang's historical message.

Every stage has an independent feature switch. Rollback disables use of the
new classification while retaining observations and history. Unknown exchange
outcomes are reconciled; rollback is never used as permission to retry.
