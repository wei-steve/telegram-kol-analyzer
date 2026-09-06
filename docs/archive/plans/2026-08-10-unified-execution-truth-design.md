# Unified Execution Truth Design

## Problem

The production entry pipeline can currently finish an actionable instruction
without proving a corresponding exchange outcome.  The concrete failure on
Chen's message `#9974` crossed several independent modules:

1. MiMo correctly recognized a complete BTC long entry.
2. Adjacent-entry admission treated completed non-strategy messages whose
   fixed `strategy` schema contained only null values as unresolved actionable
   work.
3. The entry returned `deferred/adjacent_entry_context_pending`.
4. The instruction orchestrator did not recognize that new defer reason and
   defaulted the item to `succeeded`.
5. No `TradeSignal`, `ExecutionBinding`, or exchange order was created.
6. Price monitoring later set `StrategyLifecycle.lifecycle_status=entered`,
   which could be presented as a real holding despite the missing exchange
   evidence.

Production contains two confirmed rows with the exact stale-admission pattern:
the earlier group message `#4171` and Chen's `#9974`.  Chen's row now has a
separately operator-authorized manual market entry; the historical admission
row must not be replayed.

The incident exposed a broader architectural problem: recognition completion,
instruction completion, price lifecycle, exchange submission, and verified
position ownership use similar success words without sharing one terminal
truth contract.

## Goals

- Give every executable instruction one durable, explicit execution contract.
- Make verified exchange evidence or a verified refusal mandatory for success.
- Keep deferred work nonterminal and self-reconciling within a bounded deadline.
- Preserve multi-leg strategy economics during automatic and manual recovery.
- Separate price lifecycle from exchange execution state in every presentation.
- Detect silent stalls and contradictions without granting monitoring or the
  Runtime Incident Agent any trading authority.
- Replace the informal deployment safe-window checklist with a deterministic,
  change-class-aware preflight.
- Preserve current authoritative recognition, contextual targeting, exact
  `posId` ownership, unknown-outcome non-retry, and production history.

## Non-goals

- Do not replace MiMo or contextual multi-instruction targeting.
- Do not let a monitor, web view, lifecycle worker, or Runtime Incident Agent
  submit or mutate an exchange order.
- Do not bulk rewrite historical `succeeded` rows.
- Do not replay old Telegram messages or automatically compensate old missed
  entries.
- Do not delete legacy status columns during the initial rollout.
- Do not redesign Deepcoin position ownership or protection ledgers.

## Considered approaches

### Patch the current pipeline only

This is the smallest change, but it leaves duplicated status interpretation,
lost wakeups, misleading lifecycle presentation, and manual-recovery drift.
It is necessary as an immediate P0 but insufficient as the final architecture.

### Rewrite the complete execution pipeline

A new unified state machine would be conceptually clean, but replacing
recognition, targeting, entry, management, recovery, and lifecycle behavior at
once has unacceptable production and historical-compatibility risk.

### Add a watchdog only

A watchdog would shorten detection time but would not prevent deferred work
from being terminalized as success or keep manual recovery faithful to the
original plan.

### Selected approach: additive execution truth with compatibility adapters

Add one durable execution-contract layer after authoritative candidate
projection.  Existing execution writers remain in place and publish evidence
through adapters.  Roll out in shadow mode above a future watermark, switch
readers before writers, and retire duplicated legacy decisions one at a time.

## Authority boundaries

The new layer begins only after MiMo and contextual resolution have selected a
durable `MessageInstructionItem`.  It must never infer a target, reinterpret
raw Telegram text, or create an executable candidate.

Existing responsibilities remain:

- `MessageInstructionItem`: durable authoritative work identity and temporary
  compatibility status.
- `TradeSignal`: submission idempotency and unknown exchange outcome guard.
- `ExecutionBinding` and `ExecutionOrderLeg`: real order and position ownership.
- position protection and take-profit ledgers: exact protection ownership.
- `StrategyLifecycle`: strategy and price lifecycle only.
- `MessageOperationContract`: observation and incident supervision only.
- `ExecutionEvent`: durable business-event history.

The execution contract may constrain whether an instruction is terminal, but
it may not authorize a trade that the existing authoritative path did not
authorize.

## Data model

Add `instruction_execution_contracts`, with at most one row per active
`MessageInstructionItem` generation.  The row contains:

- instruction item, raw message, candidate, and optional strategy-instance IDs;
- intent kind;
- state and optimistic state version;
- bounded reason code and deadline;
- optional `TradeSignal` and `ExecutionBinding` references;
- whether any exchange write was attempted;
- terminal kind and completion scope;
- bounded evidence references;
- last-progress, verified, terminal, created, and updated timestamps.

Add `instruction_execution_transitions` as an immutable audit ledger containing
the contract, state version, previous and next states, reason code, bounded
evidence references, and timestamp.  No secrets, raw API payloads, free-form
Telegram messages, or credentials belong in either table.

The contract states are:

- `pending`
- `deferred`
- `submitting`
- `submit_unknown`
- `verified`
- `failed`
- `expired`

Legal transitions are:

```text
pending
  -> deferred
  -> submitting
  -> verified
  -> failed
  -> expired

deferred
  -> pending
  -> failed
  -> expired

submitting
  -> verified
  -> failed
  -> submit_unknown

submit_unknown
  -> verified
  -> failed
```

`verified`, `failed`, and `expired` are terminal.  `submit_unknown` is
non-retryable and may advance only through exact exchange readback.

`verified` uses a terminal kind:

- `verified_entry`
- `verified_management`
- `verified_cancel`
- `verified_exit`
- `verified_refusal`

Multi-leg execution remains leg-granular in `ExecutionOrderLeg`.  A contract
cannot terminate until every selected leg has a deterministic outcome.  When
some legs are verified and the remainder are confirmed absent, the contract
may be `verified` with `completion_scope=partial`; this must create an incident
and must not retry the absent legs automatically.

## Compatibility and historical data

During rollout, `MessageInstructionItem.status` remains a compatibility mirror.
All new state changes pass through one transition service that writes the new
contract and mirrors the old field in the same transaction where possible.
Unknown result values are invariant violations; they never default to
`succeeded`.

Historical rows are projected read-only:

- an exact binding plus verified exchange evidence is a verified execution;
- an explicit skip or safety refusal is a verified refusal;
- `succeeded + deferred`, price-entered without a binding, and other
  contradictions are `legacy_unproven` attention states;
- unknown exchange outcomes remain unknown and are never retried.

No production history is bulk migrated to a stronger truth state.  Chen's
`#9974` uses the separately authorized binding as its current execution proof.
The stale admission row is retained for audit.  Message `#4171` is reported for
manual review only.

## Execution flow

### Recognition and contract projection

MiMo persists evidence, candidate, and instruction item first.  A projector
then creates the contract.  It consumes structured authoritative output only
and cannot reinterpret text or alter target selection.

### Context assembly

Adjacent-entry admission returns a typed decision: `ready`, `deferred`, or
`refused`.  A deferred result must name exact blocker IDs, a bounded reason
code, a deadline, and a deterministic recheck condition.  All-null or
blank-only fixed strategy schemas are non-actionable placeholders.

Event-driven wakeup remains an optimization.  A periodic reconciler rechecks
all due deferred contracts so a lost completion event cannot leave an entry
pending forever.

### Immutable order draft

Before submission, persist a fingerprinted draft that freezes:

- original entry leg count and allocations;
- aggregate risk budget;
- order type and price per leg;
- stop-loss and take-profit plan;
- contract-spec evidence;
- stable per-leg client order IDs.

Operator recovery produces a revision of that draft.  An instruction such as
"market first leg" may change only the first leg's execution type and current
reference price.  It may not collapse a two-leg draft, reassign all risk to one
leg, or silently remove a remaining entry.

### Submission and readback

The contract transitions to `submitting` before the first exchange request.
The existing `TradeSignal` claim and Deepcoin writer remain responsible for
idempotency.  Every leg is read back by client order ID, order ID, and exact
`posId`.  Stop-loss and take-profit ownership are checked before the entry can
be presented as fully protected.

### Lifecycle and presentation

Price monitoring records price-touch facts and may maintain the analytical
lifecycle, but it does not verify execution.  Web and Telegram projections use
the contract, binding, legs, and exchange evidence to display one of:

- waiting for context;
- ready/pending submission;
- submitting;
- exchange outcome unknown;
- order verified;
- position verified;
- price touched but no exchange order;
- verified refusal;
- failed/expired;
- inconsistent state requiring attention.

## Error handling

- Failure before any exchange write may retry within the contract deadline.
- Timeout or disconnect after a possible write transitions to
  `submit_unknown`; no automatic retry is permitted.
- A crash after exchange acceptance is reconciled through the pre-persisted
  client order ID.
- Partial multi-leg execution preserves and protects confirmed legs, reports
  partial completion, and does not invent or retry missing legs.
- Deferred work that reaches its deadline expires and alerts; it is never
  submitted late.
- A presentation/evidence contradiction displays an abnormal state rather than
  a successful holding.

## Supervisors and incident handling

A periodic read-only supervisor detects:

- overdue deferred contracts;
- lost wakeups;
- stale submitting contracts;
- legacy succeeded items without terminal proof;
- price-entered lifecycles without bindings;
- mismatches among contract, TradeSignal, binding, legs, and protection;
- partial multi-leg completion.

The production monitor and Runtime Incident Agent consume these facts.  They
may alert, diagnose, or prepare a handoff, but may not transition an execution
contract into `submitting` or call an exchange mutation.

## Legacy-code impact audit

Before enabling the new layer, inventory every reader and writer of:

- `MessageInstructionItem.status`;
- dictionary results containing `status`, `reason`, or `submitted`;
- `StrategyLifecycle.lifecycle_status=entered`;
- `TradeSignal` terminal states;
- `ExecutionBinding` and `ExecutionOrderLeg` activity states;
- recovery, revision, cancellation, management, deletion, and protection
  workflows.

Each legacy path receives one classification:

- authoritative writer to adapt;
- compatibility mirror;
- presentation reader to replace;
- monitoring-only consumer;
- obsolete duplicate to retire after shadow comparison.

The audit must cover authoritative recognition, single- and multi-instruction
execution, legacy preambles, adjacent-entry v2, automatic entry, recovery,
entry revision, strategy management, source deletion, lifecycle monitoring,
web queries, Telegram notices, production monitoring, Runtime Incident Agent,
database migrations, and deployment scripts.

## Deployment preflight

Replace the informal safe-window judgment with a read-only
`deployment-preflight` command that returns deterministic JSON, an exit code,
and `PASS`, `WARN`, or `BLOCK`.

Preflight is change-class-aware:

- ordinary code fix: block only fresh active exchange or position mutations;
- compatible schema change: additionally require migration preflight and a
  successful backup;
- execution-writer change: additionally require a complete, stable read-only
  exchange/ownership snapshot;
- live mode promotion: additionally require reviewed shadow evidence and
  explicit operator approval.

Protected open positions, historical pending/failed/unknown rows, old monitor
baselines, ordinary recognition, and durable read-only analysis are warnings,
not indefinite blockers.

The command emits a short-lived fingerprint containing the expected commit,
change class, database watermark, and checked active-work facts.  The server
update helper must validate that fingerprint immediately before restart.

## Testing

Testing includes:

- exhaustive legal and illegal state transitions;
- an end-to-end invariant that every actionable instruction reaches verified
  exchange evidence or a verified refusal within its deadline;
- Chen, Miya, Sanjie, Feiyang, single-leg, two-leg, hybrid, half-size, add-entry,
  multi-instruction, management, cancellation, exit, and legacy-no-item replay
  fixtures;
- fault injection before submission, after possible submission, after exchange
  acceptance, between multi-leg submissions, during protection setup, after a
  lost wakeup, and across restart;
- additive migration and rollback testing against a sanitized production
  snapshot;
- server-side focused and regression suites with fake/read-only exchange
  adapters before any production activation.

## Rollout

1. Deploy the reviewed P0 adjacent-entry fix without replaying history.
2. Deploy additive contract tables and transition code dormant.
3. Enable future-only shadow dual-write above a fresh watermark.
4. Enable contradiction monitoring and binding-aware presentation.
5. Enforce the contract for new entry messages.
6. Extend enforcement to recovery and entry revision.
7. Extend enforcement to management, cancellation, exit, and deletion flows.
8. Retire duplicated legacy decisions only after a complete stable observation
   period with zero unexplained divergence.

Each phase has an independent disable switch.  Rollback turns off new reads or
enforcement but leaves additive tables and audit history in place.  Rollback
never replays a message or retries an unknown exchange request.  The P0 fix is
retained permanently.

## Success criteria

- A deferred instruction cannot be marked successful.
- Every future executable instruction obtains a bounded contract and terminal
  evidence or raises an incident.
- Lost wakeups self-reconcile before the execution deadline.
- No page or notification claims a holding from price lifecycle alone.
- Manual recovery preserves the original multi-leg and risk invariants.
- Existing authoritative recognition and exact-position ownership remain the
  only sources of targeting and mutation authority.
- Deployment decisions are deterministic, change-class-aware, and auditable.
- Existing production history remains intact and is never automatically
  replayed.
