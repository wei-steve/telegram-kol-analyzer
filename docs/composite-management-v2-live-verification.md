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
- exact live-position ownership: seven open positions; every position has one
  unique verified entry strategy, complete protection readback, no ownership
  conflict, and no unowned protection order in the 2026-08-05 read-only audit
- pending TPSL ownership and protection incidents: 24 pending trigger/TPSL
  orders were read; current-position protection audits were complete and
  protected, while five positions retain the pre-existing
  `backup_stop_blocked` freeze reason and therefore remain excluded from backup
  stop repair
- listener/checkpoint/reconciliation health: main service active and zero error
  journal lines in the observed 15-minute window; exact live ownership and
  checkpoint evidence remains unproven
- production safety monitor result: `audit_abnormal` because the two legacy
  recovery batches remain actionable

The first safe-window decision on 2026-08-05 was **not proven**, so deployment
was deferred. After the cumulative-history fix passed review, a fresh gate found
zero running recognition, active management, active/unknown mutations, active
rescues, new two-minute messages, and new two-minute execution events. The
reviewed branch was then deployed dormant. Separate fresh zero-count gates were
required before applying batch 17 and batch 22; both exact fingerprinted
recoveries succeeded without any exchange write.

If any management, close, protection, rescue, or time-sensitive strategy action
is in flight, stop before deployment and record the exact unresolved identity.

## Dormant deployment evidence

- reviewed feature commit SHA: `697d105`
- push target: `origin/codex/deepcoin-auto-trading-v1`
- server deployed SHA: `665448fd522338777020916dba83ded29dc34456`
- schema/migration status: service startup and focused server tests passed
- `composite_management_v2_mode=disabled`: confirmed after deployment;
  existing `management_execution_mode=live` was unchanged
- service restart and clean journal: service active; zero warning journal lines
  since the controlled restart
- exchange writes caused by verification and history recovery: confirmed `0`

Post-deployment evidence:

- focused server tests: 94 passed
- cumulative-history recovery tests: 18 passed locally; independent review had
  no remaining Critical/Important findings
- batches 17 and 22: `succeeded`; three legs: `confirmed`
- post-recovery management audit: zero actionable batches, zero blocked,
  `partial_failed`, `recovery_required`, and `submit_unknown`
- monitor diagnostic: healthy with no reason codes; timer active and enabled
- execution-event delta: exactly two `management_history_recovery` events and
  no other event after the pre-recovery baseline

## Shadow replay

Only immutable captured inputs and fake/read-only exchange adapters may be used.
Verify the Miya and Sanjie cases produce all required components, preserve the
50% close and stop-move clauses, consume/cancel first TP before close, converge
the remaining size, and create and verify both stops before old-stop cancel.
No historical Telegram row may be reclaimed or automatically replayed.

## Live enablement

Live enablement was approved by the operator on 2026-08-06 after the separate
review required above.

Before enablement, the exact Miya and Sanjie recognition and shadow admission
selection passed twice with zero real exchange adapters: 8 focused tests passed
locally and the same 8 passed on the deployed server. The broader local
contract, gate, planner, executor, and fault-injection selection passed 346
tests. Repeated contract serialization and fingerprint tests remained stable.

The pre-enable live audit found one additional observability defect: composite
backup stops existed as verified `position_protection_ledger` rows, while the
position audit still preferred an older terminal backup-stop table row and a
stale take-profit row. Commits `1c27d44` and `0e64398` made current ledger state
authoritative, excluded terminal rows from current protection, and made a
terminal ledger TP suppress a stale active TP row with the same order ID. The
focused local regression and adjacent safety selection passed 478 tests; the
deployed focused selection passed 13 tests. Independent review found no
remaining Critical or Important issue.

The final safe-window gate immediately before enablement reported zero running
recognition, active management batches/components, active or unknown position
mutations, active protection rescues, two-minute raw messages, and two-minute
execution events. Database and loopback API readback then both reported
`composite_management_v2_mode=live` and effective mode `live`. Activation
created no management batch, component, mutation, or execution event. The main
service and monitor timer remained active, and the bounded no-notify production
safety diagnostic completed healthy with `monitor_error=null` and no reason
codes.

Rollback remains an atomic setting change back to `disabled`. Admission can be
disabled without abandoning reconciliation for an already submitted v2
component. No historical message or synthetic real trade was replayed during
activation; production verification continues only from naturally arriving
messages.
