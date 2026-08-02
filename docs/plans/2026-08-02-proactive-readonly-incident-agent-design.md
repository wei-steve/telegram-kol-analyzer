# Proactive Read-Only Runtime Incident Agent Design

## Status

Approved on 2026-08-02.

This design extends the completed Phase 1–6 runtime incident Agent without
granting business-mutation authority. Original Phase 7 remains deferred and
unauthorized. It must not block this read-only enhancement, and nothing in this
design constitutes approval for Phase 7.

The non-negotiable rollout requirement is that implementation and deployment
must not interrupt Telegram intake, recognition, contextual resolution,
strategy execution, management, exits, protection, reconciliation, or the
production safety monitor.

## Goal

Make the runtime incident Agent useful before an operator has already found a
bug. It must proactively discover defined runtime and business-invariant
failures, collect bounded read-only evidence, produce an explicitly
hypothetical diagnosis, notify the operator through Telegram when Codex repair
is required, and continue read-only verification until the incident resolves.

## Authority Boundary

The enhanced Agent may:

- observe durable events and bounded health signals;
- run deterministic read-only invariant checks;
- read allowlisted, bounded, redacted evidence projections;
- compare coherent local and exchange snapshots;
- store incident, diagnosis, notification, and verification metadata;
- send Telegram incident, escalation, and recovery reports;
- produce a reproducible Codex handoff keyed by `incident_id`.

It may not:

- modify orders, positions, protection, leverage, margin, or strategy state;
- write source business rows or repair production data;
- restart, stop, or deploy any service;
- invoke Codex automatically;
- replace first-pass recognition or contextual strategy resolution;
- guess strategy, order, position, or lifecycle ownership;
- enable a Phase 7 action flag or playbook;
- import unchecked Deepcoin write clients or business-mutation gateways.

All Phase 7 action flags and action allowlists remain empty or false.

## Architectural Choice

Use a hybrid of event-driven intake and periodic read-only invariant scans.

Expanding only the existing event adapters would still miss silent logic bugs
where every process returns successfully but the resulting business state is
unsafe or inconsistent. Continuously giving raw logs to an LLM would be noisy,
expensive, difficult to bound, and prone to unsupported conclusions. The
hybrid design uses deterministic checks to decide that an incident exists and
uses AI only to explain gathered evidence.

```text
durable runtime failures -----+
                              |
periodic invariant scans -----+--> candidate observations
                              |          |
Agent/monitor health ---------+          v
                                  deterministic confirmation
                                             |
                                             v
                                      runtime_incidents
                                             |
                                             v
                                   bounded AI diagnosis
                                             |
                                      evidence validator
                                             |
                                             v
                                Telegram Codex handoff report
                                             |
                                             v
                                  continued read-only verification
```

The scanner and Agent remain outside the normal trading critical path. Their
failure must fail open with respect to normal production work.

## Detection Sources

### Existing durable failure adapters

Expand reviewed capture coverage beyond the currently enabled
`management_partial_failed` type. Candidate types include:

- provider retry exhaustion;
- contextual worker exhaustion;
- management `submit_unknown`, `partial_failed`, and `recovery_required`;
- severe protection incidents;
- production monitor adapter failure or incomplete audit;
- notification delivery failure.

Capture and delivery remain separate flags. New types first run capture-only
and are compared with their authoritative durable source rows.

### Proactive invariant scanner

The scanner evaluates versioned, deterministic rules over bounded coherent
snapshots. Each rule declares:

- stable rule and policy versions;
- exact evidence sources and stable identifiers;
- allowed normal-transition states;
- confirmation count and age threshold;
- severity and Codex-handoff policy;
- incident fingerprint inputs;
- recovery condition;
- maximum result size and scan cost.

The initial rule catalog covers:

1. A terminal lifecycle with an exchange position or live entry order.
2. An entered lifecycle whose exact exchange position is missing.
3. A persistent local-versus-exchange order-state mismatch.
4. Submit, cancel, close, or protection outcomes remaining unknown beyond their
   reviewed transition window.
5. Multiple local owners for one exact exchange order or position.
6. An active position without verified primary protection.
7. Missing backup protection when primary protection has failed.
8. Protection direction, quantity, or exact `posId` mismatch.
9. Confirmed TP1 fill without a terminal break-even convergence outcome.
10. Partial close without converged remaining protection quantity.
11. A persisted Telegram message whose recognition exceeds its lease and
    recovery window.
12. Recognition execution ownership stuck in a nonterminal running state.
13. A high-priority deletion, close, stop update, or management message without
    a terminal processing outcome.
14. Main service, listener, worker, safety monitor, notification link, or Agent
    health failure.
15. A contradiction where another monitor reports failures while the incident
    ledger remains silent.

No rule may infer a strategy target or convert a contextual `unresolved` or
`hold` outcome into an incident unless an independent technical worker failure
has occurred.

## Severity and Confirmation

- **Critical:** confirmed exchange exposure after local terminal state,
  unprotected active position, or loss of the main listener/service. Notify on
  the first complete coherent snapshot.
- **High:** persistent unknown write outcome, TP1 break-even failure, entry
  exposure conflict, or protection mismatch. Require two coherent observations
  unless the authoritative source is already terminal.
- **Medium:** exhausted AI work, repeated notification failure, incomplete
  monitor execution, or stuck background ownership. Apply a reviewed count or
  time threshold.
- **Low:** configuration drift, heartbeat degradation, or a silent intake
  contradiction. Aggregate unless severity increases.

An incomplete exchange snapshot never proves absence, mismatch, or recovery.
It produces an explicit evidence-insufficient result instead.

## Incident State Machine and Deduplication

Candidate observations use a stable fingerprint based on rule version and
stable strategy, lifecycle, binding, leg, order, position, job, or service
identifiers. Dynamic text and timestamps are excluded from the fingerprint.

```text
observing -> open -> diagnosing -> notified -> verifying -> resolved
     |                                             |            |
     +-----------------> resolved <----------------+            +-> open
```

- `observing` holds unconfirmed transition-window evidence.
- `open` means deterministic confirmation completed.
- `diagnosing` is a bounded Agent lease.
- `notified` means a durable notification result exists.
- `verifying` keeps checking the same recovery predicate without executing a
  recovery action.
- `resolved` means the deterministic recovery condition is true.
- A resolved fingerprint may reopen as a new generation.

Unchanged evidence does not send repeated Telegram messages. Notify again only
when severity rises, material evidence changes, a reviewed escalation deadline
passes, or the incident resolves. Telegram retains the stable event ID as the
deduplication marker.

## Diagnosis Contract

The deterministic rule establishes the incident. The model only explains a
bounded evidence bundle and returns a closed structured object containing:

- incident type and severity;
- confirmed facts copied from evidence references;
- diagnosis hypothesis and confidence;
- missing or incomplete evidence;
- current impact and containment;
- remaining risk;
- `codex_handoff_required`;
- bounded recommended code areas to inspect;
- attempted read-only queries.

Facts and hypotheses remain separate. A validator rejects unknown fields,
unsupported certainty, nonexistent evidence references, invented ownership,
business-action recommendations, sensitive material, and unbounded output.
When validation fails, the system discards the AI diagnosis and sends only the
deterministic facts.

## Telegram and Codex Handoff

Telegram reports contain:

- incident ID, severity, object identity, and current state;
- confirmed facts;
- the Agent diagnosis explicitly labeled as a hypothesis;
- confidence and missing evidence;
- current containment and remaining risk;
- `自动操作: 无`;
- whether Codex is required;
- a copyable instruction using the stable `incident_id`.

The durable handoff bundle contains a bounded timeline, stable IDs, expected
versus observed state, redacted evidence, read-only tool calls, hypothesis,
confidence, excluded causes, suggested reproduction entry points, and the
deterministic recovery condition. Codex must independently verify the bundle;
the Agent diagnosis is never treated as fact.

## Failure Handling and Self-Observation

- Provider unavailable: send a deterministic fact-only incident report.
- Invalid or fabricated AI evidence: reject the diagnosis and report facts.
- Telegram failure: retain a bounded durable retry and make the independent
  monitor report notification-path failure.
- Agent crash or stuck claim: recover the lease and emit an Agent-health
  observation.
- Incomplete exchange data: refuse a state conclusion and retry read-only
  observation later.
- Scanner failure: preserve the normal production path and emit a scanner
  health observation through an independent path.
- Sustained Agent idle state while other monitors report abnormalities: raise a
  silent-intake contradiction.

The monitor notification configuration and state-file readability defects must
be repaired before relying on the new notification path. The Agent must not be
its own only watchdog.

## Roadmap and Production Continuity

Phase 1–6 remains authoritative and deployed. Phase 7 is recorded as deferred,
unauthorized, and non-blocking. The new read-only work is tracked as
`Phase 8R — Proactive Read-Only Incident Detection`.

Phase 8R is divided into separately reviewed rollout steps, with at most one
runtime step implemented per user turn:

1. Repair monitoring observability and add independent heartbeats.
2. Expand durable capture types in capture-only mode.
3. Deploy the invariant scanner dormant, then shadow-only.
4. Canary one deterministic notification rule at a time.
5. Enable bounded AI diagnosis and Codex handoff for confirmed rules.
6. Expand the rule catalog and continuous-quality metrics.

Every runtime change ships dormant by default. Before any restart, prove a safe
window with zero time-sensitive recognition, execution, management, exit,
protection, or reconciliation work in flight. Deploy from reviewed Git commits,
restart only through the existing bounded procedure, and verify listener
continuity, message checkpoints, reconciliation, management, protection, and
the safety monitor immediately afterward. If a safe window cannot be proven,
finish local work and defer deployment without marking the step complete.

Each layer has an independent rollback:

- disable the proactive scanner;
- disable one rule;
- disable AI diagnosis while retaining deterministic incidents;
- disable new Telegram delivery while retaining the ledger;
- stop the Agent sidecar without stopping the main service;
- leave every Phase 7 action flag and allowlist disabled.

## Testing

Tests must cover:

- normal, abnormal, recovery, and allowed-transition states for every rule;
- coherent snapshot requirements and incomplete-snapshot refusal;
- stable fingerprints, generations, dedupe, escalation, and recovery;
- AI provider outage and fact-only notification fallback;
- fabricated-reference and unsafe-recommendation rejection;
- Telegram retry, exhaustion, and independent notification-health detection;
- Agent crash, claim expiry, scanner failure, idle contradiction, and watchdog
  independence;
- architectural prohibition of business mutation, strategy targeting, and
  unchecked exchange writes;
- critical recognition, contextual resolution, management, exit, protection,
  listener, and reconciliation regressions.

Confirmed production incidents become bounded redacted fixtures. Production
verification uses real read-only state only; it must not create a position,
order, strategy message, or business mutation as a test fixture.

New rules run shadow-only for a recommended 48 hours. A shorter critical-rule
window requires complete historical replay, focused regression coverage, and a
separately proven production read-only canary.

## Acceptance Criteria

- Reviewed historical examples of the recent failures are detected by replay.
- Critical risk enters the ledger within two minutes of a complete snapshot.
- High risk confirms within two observations or ten minutes.
- Every notification contains a stable incident ID and valid evidence
  references.
- Provider failure does not suppress deterministic alerting.
- Unchanged evidence creates no duplicate Telegram report.
- Codex can reconstruct the bounded incident timeline from `incident_id`.
- Scanner and Agent perform zero exchange, business-state, and source-row
  writes.
- Disabling the scanner or Agent leaves the normal system unchanged.
- Agent, scanner, monitor, and Telegram notification health have an independent
  observation path.
- Production deployment and rollback pass all continuity gates without missing
  a Telegram message or interrupting a time-sensitive operation.
