# Message Operation Incident Agent Design

## Status

Approved by the operator on 2026-08-08.

This document extends and corrects the scope of
`2026-07-28-runtime-incident-agent-design.md`. The original runtime continuity,
strategy-targeting, mutation-safety, rollout, and rollback invariants remain in
force. Where the documents differ on incident eligibility, investigation
scope, notification timing, or Codex handoff behavior, this document is the
new authority.

The original Runtime Incident Agent is not complete. The canonical status file
still names Phase 8R.3 as `in_progress`. Existing components must be reused and
advanced rather than replaced or treated as finished.

## Operator Goal

For every Telegram message that contains executable trading or strategy
management intent, detect when the system cannot carry out that intent
correctly, investigate the failure with broad but enforced read-only access,
notify the operator promptly, and produce a durable package the operator can
copy to Codex for independent verification and repair.

The Agent has no trading or production-mutation authority. Its investigation
authority should otherwise be as complete as practical.

## Current Gap

The deployed system proves individual safety mechanisms but does not yet meet
the operator goal:

- Agent eligibility is restricted by a narrow incident-type selector.
- Existing adapters see only failures that code already knows how to emit and
  can miss silent no-ops, false successes, or missing descendants.
- Some captured production failures are intentionally excluded from Agent
  diagnosis or Telegram delivery.
- The existing Codex handoff is reproducible data and a CLI output, not an
  actively persisted, operator-ready delivery artifact.
- A Telegram report can state that Codex handoff is required without actually
  giving the operator the complete copyable handoff.
- The proactive invariant scanner and its rollout remain unfinished.

These are rollout and architecture gaps, not evidence that the sidecar or
model provider is unhealthy.

## Selected Architecture

Use a deterministic, per-message outcome supervisor. It adds no model call to
the successful path. The AI Agent runs only after deterministic evidence shows
that an executable message did not reach a correct, verified outcome.

```text
raw Telegram message
        |
        v
existing recognition and contextual resolution
        |                         |
        |                         v
        |                 existing execution path
        v                         |
deterministic expectation         v
contract                  local and exchange evidence
        |                         |
        `----------+--------------'
                   v
          message outcome supervisor
             |                |
        verified success   violation/timeout
             |                |
       close without AI       +--> immediate Telegram alert
                              |
                              v
                    read-only AI investigation
                              |
                              v
                 diagnosis + copyable Codex handoff
```

The supervisor is outside the recognition and execution critical path. Its
failure must not delay or change normal trading behavior.

## Deterministic Expectation Contract

One source message owns one contract with one or more instruction items. Items
cover at least new entry, add entry, take profit, stop loss, cancel, exit, and
other strategy-management operations.

The contract is built only from existing authoritative records. It does not
call a new model, reinterpret a message, choose a strategy, or repair an
unresolved target. It records:

- source message and reply relationship;
- whether existing processing found executable intent;
- authoritative target and instruction identifiers, when available;
- expected deterministic descendants and terminal states;
- operation-specific deadline;
- observed durable descendants, terminal states, and readback evidence;
- final verified, violated, superseded, or duplicate disposition.

An executable message that ends in `hold`, `unresolved`, refusal, or no action
still gets a contract violation even if no target was resolved. The Agent may
investigate why resolution failed but may not supply a replacement business
decision.

A multi-instruction message is verified only when every instruction item is
verified. The successful path closes the contract without any new AI call.

## Violation Rules

The supervisor creates an incident when any of the following is true:

- recognition or contextual resolution crashes, times out, or exhausts retry;
- executable intent terminates as `hold`, `unresolved`, refused, or no-op;
- required management, component, mutation-intent, or execution descendants
  are absent;
- any instruction item is failed, partial, unknown, stranded, or exhausted;
- a local success lacks the required exchange acknowledgement or readback;
- local and exchange direction, size, price, stop, take-profit, order, or
  position evidence disagrees with the authoritative plan;
- processing does not reach an operation-specific terminal state in time;
- restart, lease recovery, idempotency, or deduplication skips required work;
- later reconciliation disproves an earlier success.

Only ordinary non-action conversation, a proven idempotent duplicate whose
original result remains valid, or a proven superseded instruction with a
verified replacement may close without investigation.

An intentional safety refusal is still reported and investigated, but is
classified as an expected safety refusal rather than a code defect when the
evidence supports that conclusion.

## Broad Read-Only Investigation Authority

The Agent may investigate:

- original messages, replies, and media-derived evidence;
- recognition, contextual resolution, strategy management, execution,
  reconciliation, protection, and worker state;
- relevant production database records and their history;
- bounded service health, structured logs, and journal evidence;
- deployed source, Git revision, non-secret configuration state, and tests;
- Deepcoin balances, positions, orders, fills, history, and instrument rules
  through read-only credentials;
- Telegram evidence and notification delivery state;
- related incidents, prior diagnoses, and repair outcomes.

This is not an unrestricted production shell. A read-only investigation broker
provides broad declarative queries and an isolated analysis workspace. It is
extensible by evidence category rather than gated by incident type.

## Enforced Isolation

Safety must be structural, not prompt-based:

- run under a dedicated unprivileged operating-system identity;
- expose production data through read-only snapshots, projections, or enforced
  query-only connections rather than a writable production database;
- use a separate exchange key with trading disabled at the exchange;
- mount code, configuration state, and logs read-only, while never mounting
  credential files;
- permit temporary analysis files only inside an isolated private workspace;
- allow network access only to reviewed read-only evidence endpoints and the
  model provider;
- keep Telegram Bot credentials in a separate deterministic dispatcher;
- reject and audit file writes, database writes, service control, exchange
  mutations, credential reads, and unapproved egress;
- bound rows, bytes, time ranges, model turns, tokens, wall time, and retries.

Raw operational data needed for diagnosis may enter the model context. Tokens,
API secrets, signing material, and credentials may not.

## Two-Stage Telegram Notification

### Stage 1: immediate deterministic alert

Every violated source message receives an alert that includes:

- incident and source-message identifiers;
- source time and bounded original content;
- recognized executable intent;
- failed, refused, unresolved, no-op, mismatched, or timed-out checkpoint;
- currently known impact;
- explicit notice that read-only AI investigation is in progress.

Fingerprint deduplication must never suppress this per-message first alert.

### Stage 2: diagnosis or explicit investigation failure

The second notification includes:

- original message, reply chain, and instruction items;
- expected-versus-observed outcome comparison;
- processing timeline and durable evidence;
- relevant database, log, deployed-code, and exchange readback evidence;
- diagnosis hypothesis, confidence, missing evidence, and affected scope;
- classification such as code defect, configuration problem, external failure,
  expected safety refusal, or undetermined;
- likely files and focused tests;
- an explicit statement that the Agent performed no production mutation;
- a self-contained prompt that can be copied directly to Codex.

If investigation times out, the provider fails, or evidence remains
unavailable, the second notification still reports the deterministic evidence,
failure reason, and remaining investigation steps. Silence is not a terminal
state.

The default investigation budget is 120 seconds. A timeout notification is
sent at the boundary; at most one bounded supplemental investigation may
continue afterward.

## Durable Codex Handoff

The handoff is a durable artifact, not an in-memory worker result or Boolean
flag. It contains:

- incident, message, instruction, strategy, lifecycle, and execution IDs;
- original message and reply evidence;
- expected and observed states;
- bounded evidence references and a timeline;
- Agent queries, hypothesis, confidence, and missing evidence;
- likely code and test locations;
- prohibited actions and safety constraints;
- a ready-to-copy Codex prompt requiring independent verification.

Telegram includes the complete bounded copyable prompt. Oversized evidence is
sent as a redacted JSON document and remains retrievable by stable handoff ID.

The operator remains the dispatch authority: this design does not
automatically create a Codex task. It guarantees that the operator receives
everything needed to copy the incident into Codex.

## Deduplication

Every affected source message gets Stage 1. Incidents with the same verified
fingerprint may share one Agent investigation and Stage 2 diagnosis. The
incident retains the complete affected-message list.

Send an updated Stage 2 notification when affected-message count grows,
severity increases, material evidence changes, or the diagnosis changes.
Repeated status alone does not justify permanent notification suppression.

## Failure Handling

- Capture, supervision, Agent, and notification failures never alter the
  source business transition.
- Stage 1 does not depend on the Agent or model provider.
- Missing evidence is recorded explicitly and retried within a bounded budget.
- Incident, investigation, and delivery claims are durable and recoverable
  after restart.
- A denied write, service-control, credential, or egress attempt creates a
  high-priority security incident.
- Agent budget exhaustion produces an operator-visible terminal notification.
- The supervisor has an independently monitored heartbeat; failure to inspect
  messages is itself an incident.
- Telegram delivery failures retain durable retry state and produce a separate
  high-priority incident visible to independent monitoring.

## Relationship to Existing Runtime Agent Work

Reuse the existing runtime incident ledger, sidecar worker, structured
diagnosis contract, redaction checks, read-only evidence projections,
notification outbox, policy versions, and rollback mechanisms where they meet
this design.

Do not:

- reset or skip the unfinished Phase 8R.3 status;
- replace existing strategy targeting or contextual resolution;
- duplicate existing incident rows or evidence tools without migration;
- broaden trading, recovery-playbook, service-control, or business-mutation
  authority;
- mark the original Agent implementation complete merely because this design
  and its plan exist.

The implementation plan must begin with a gap inventory that maps every
approved requirement to existing, partial, missing, or conflicting code. It
must then resume or revise the current phase in the canonical status file
rather than inventing an unrelated parallel rollout.

## Rollout

1. Add compatible contract and handoff persistence while dormant.
2. Run the deterministic supervisor shadow-only on natural messages.
3. Compare contract outcomes with existing durable records and recent
   redacted failure fixtures.
4. After a fresh safe-window check, enable Stage 1 for all new contract
   violations above a recorded watermark; do not use an incident-type Agent
   allowlist as the final eligibility boundary.
5. Enable broad read-only Agent investigation for all new message-operation
   incidents.
6. Enable durable Stage 2 handoffs and coverage monitoring.

Each runtime step preserves the existing one-phase-per-turn rule, dormant or
shadow-first introduction, tested disable path, and server-side verification.
Historical incidents are read-only regression evidence and are not replayed or
backfilled into Telegram.

## Verification

Tests must cover:

- every supported instruction class and every correct and incorrect terminal
  state;
- multi-instruction aggregation, reply context, duplicates, and supersession;
- silent no-op, missing descendant, false success, partial result, unknown
  result, timeout, restart, and reconciliation-disproved success;
- normal conversation with no contract and successful messages with no new
  model call;
- Stage 1 per-message delivery and Stage 2 diagnosis/failure/timeout delivery;
- fingerprint reuse without suppressing affected-message alerts;
- provider, database evidence, journal, exchange, Telegram, and process failure
  injection;
- denied SQL/file/service/exchange/credential/egress mutations;
- redaction, resource bounds, concurrency, leases, recovery, and rollback;
- recent real failures as redacted regression fixtures;
- architecture boundaries protecting strategy targeting, contextual
  resolution, and every production mutation path.

Live trading is never a test fixture.

## Acceptance Criteria

- successful messages add zero new AI calls;
- 100% of recognized executable messages receive a durable supervision
  contract;
- 100% of test contract violations receive Stage 1;
- 100% of incidents reach a diagnosed, investigation-failed, or timed-out
  operator-visible terminal state;
- 100% of terminal investigations have a reproducible Codex handoff;
- zero unauthorized production or exchange writes succeed;
- zero notification or investigation items disappear without a durable
  terminal state;
- the main listener, recognition, contextual resolution, management,
  execution, reconciliation, and protection flows remain independent and
  continuous.
