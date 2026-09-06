# Management Unblock and Trigger Stop Attribution Design

## Goal

Allow a later, unequivocal full-exit instruction to proceed after an earlier
protection-only batch definitely failed and restored every touched position;
also record an exact trigger-entry stop loss when Deepcoin omits `posId` from
the pending TPSL response.

## Scope and constraints

- Never release a batch that has an unknown submission, a pending leg, or an
  un-restored mutation.
- Never associate a protection order from symbol, side, price, size, or time
  alone.
- The implementation may only adopt a stop-only order; staged take-profit
  convergence remains responsible for multi-target take profits.
- Existing historical and live repairs remain dry-run/fingerprint gated.

## Design

### Safe management successor

The management planner will inspect the existing `partial_failed` batch for
the target strategy before it creates a later full-exit batch. It may resolve
the former batch only when every management leg is terminal and safe: each
affected protection replacement is `restored` or `confirmed`, no close order
was submitted, and no leg is `planned`, `executing`, `submit_unknown`, or
`recovery_required`. The resolution and successor creation share the existing
serialized planning boundary, so no interval permits a competing action.

All other `partial_failed` states continue to hold the unique strategy lock
and the new full exit is refused as before.

### Exact stop-only trigger protection attribution

The trigger-intent adoption planner already has immutable parent-trigger
evidence and a verified entry leg with an exact `pos_id`. For a post-baseline
TPSL candidate that omits `posId`, it will accept only one candidate when all
of these match the saved parent request exactly: instrument, position side,
positive full position size, stop trigger price, and stop-only shape (no take
profit). The candidate must be unique among all post-baseline candidates and
must not be claimed by an existing ledger row or another intent.

The result writes a verified `stop_loss` ledger row with explicit
`trigger_protection_intent_stop_only` evidence. A candidate with a returned
but different `posId`, any competing candidate, or a non-matching shape still
refuses attribution. Multi-target convergence can then proceed only after
this verified stop is visible.

## Verification

Focused tests cover the exact safe-release predicate, retention of unsafe
locks, successful full-exit successor creation, stop-only omission adoption,
and rejection of ambiguity. Production verification is read-only: inspect the
management instruction result, exact management batch/legs, live positions,
pending TPSL orders, ledger rows, and staged take-profit convergence records.
