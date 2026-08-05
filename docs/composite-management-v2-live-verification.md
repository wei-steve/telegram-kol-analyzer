# Composite-management v2 live verification

## Scope and decision

This checklist verifies a dormant deployment only. It does not authorize live
management, automatic historical replay, or exchange writes. Miya and Sanjie
historical instructions remain excluded from automatic replay.

## Local evidence

- Reviewed branch: `codex/deepcoin-auto-trading-v1`
- Required focused suites: 742 passed on 2026-08-05
- Adjacent strategy-record and TP-consumption suites: 113 passed on 2026-08-05
- Python compile and whitespace checks: passed on 2026-08-05
- Independent Critical/Important review: no remaining findings after final
  review on 2026-08-05

## Pre-deployment safe-window evidence

Record timestamps and sanitized counts only:

- active management batches/components: two legacy `recovery_required` batches
  (17 and 22); v2 component table is not yet deployed
- active or unknown mutation intents: zero
- exact live-position ownership: pending server read
- pending TPSL ownership and protection incidents: pending server read
- listener/checkpoint/reconciliation health: main service active and zero error
  journal lines in the observed 15-minute window; exact live ownership and
  checkpoint evidence remains unproven
- production safety monitor result: `audit_abnormal` because the two legacy
  recovery batches remain actionable

Safe window decision on 2026-08-05: **not proven; deployment deferred**. The
monitor is non-healthy and exact live position/TPSL ownership was not completed.
No service restart or production mutation was performed.

If any management, close, protection, rescue, or time-sensitive strategy action
is in flight, stop before deployment and record the exact unresolved identity.

## Dormant deployment evidence

- reviewed feature commit SHA: `697d105`
- push target: `origin/codex/deepcoin-auto-trading-v1`
- server deployed SHA: pending
- schema/migration status: pending
- `management_execution_mode=disabled`: pending
- service restart and clean journal: pending
- exchange writes caused by verification: must remain `0`

## Shadow replay

Only immutable captured inputs and fake/read-only exchange adapters may be used.
Verify the Miya and Sanjie cases produce all required components, preserve the
50% close and stop-move clauses, consume/cancel first TP before close, converge
the remaining size, and create and verify both stops before old-stop cancel.
No historical Telegram row may be reclaimed or automatically replayed.

## Live enablement

Not approved by this document. A separate decision must review shadow evidence,
all monitor invariants, rollback/disable behavior, and the current exchange
state immediately before changing the mode.
