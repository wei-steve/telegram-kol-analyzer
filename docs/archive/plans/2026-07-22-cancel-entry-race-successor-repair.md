# Cancel-Entry Race Successor Repair Plan

**Goal:** When an exact deferred entry fills during cancellation, prove its
ownership from exchange evidence and automatically close it through a new,
auditable successor `full_exit` batch.

## Completed foundation

- `0ce06d2`: an entered strategy's `cancel_entry` now creates a `full_exit`
  management candidate.
- `a2cef20` and `f725135`: stable successor idempotency and atomic parent
  resolution / successor-batch persistence primitives.

## Remaining implementation

1. **Classify the cancellation response**
   - In `strategy_management_executor.py`, distinguish a definite exchange
     rejection after an exact pending-order match from unknown transport
     outcomes.
   - Persist `recovery_required/deferred_entry_cancel_race_detected` only for
     the definite-race candidate. Timeouts and ambiguous results remain the
     existing fail-closed path.
   - Tests: a definite rejection creates the race state and submits no close;
     a timeout does not create a race state.

2. **Reconcile and prove ownership**
   - In `strategy_management_worker.py`, process only the new race state.
   - Reuse `reconcile_deepcoin_execution_bindings` and accept a successor only
     when the formerly deferred leg has exactly one verified, authoritative
     `posId` (`UNIQUE_TRIGGER_FILL` or stronger evidence).
   - Tests: one-to-one trigger fill succeeds; ambiguous/missing evidence leaves
     the parent `recovery_required` and submits no exchange order.

3. **Create and execute successor**
   - Build `ManagementLegCreate` records exclusively from the verified live
     position economics and the parent contract snapshot.
   - Call `create_race_resolved_successor_batch`; then use the existing worker
     close execution path. The parent becomes `resolved` only in the same
     transaction that inserts the successor.
   - Tests: successor has a distinct idempotency fingerprint, exact `posId`,
     and only its close order is submitted. Duplicate worker ticks create no
     second successor.

4. **Verification and release**
   - Run executor, binding reconciliation, management worker, batch, and
     management reconciliation tests.
   - Run code review, push reviewed commits, deploy with
     `scripts/server_git_update.ps1`, verify service health and readonly
     exchange state. Do not replay the historical Telegram message.

## Safety invariants

- Never infer a position from symbol, side, price, or time alone.
- No successor on unknown API outcome or non-unique attribution.
- Original batches remain immutable exchange audit records; successors carry
  `race_resolved_successor_of` rather than overwriting the original target.
