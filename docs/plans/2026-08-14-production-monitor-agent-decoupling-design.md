# Production Monitor And Runtime Agent Decoupling Design

## Status

Approved by the operator on 2026-08-14 after section-by-section review.

This design is local-only until a separate implementation plan is reviewed. It
does not authorize a production deployment, a Batch 119 apply, a stopped-state
capture, a database edit, a historical-message replay, or MiMo v2.

## Goal

Replace the current overloaded production monitor with a small deterministic
sentinel that supplies trustworthy facts to the existing Runtime Incident
system. Keep systemd execution status separate from production health, avoid
alerts caused by normal exchange/readback delay, and remove all superseded
monitor logic after a proven cutover.

The resulting flow is:

```text
read-only Deepcoin snapshot refresher
                |
                v
lightweight deterministic sentinel
                |
                v
versioned loopback incident bridge
                |
                v
durable runtime incident ledger
                |
                v
Runtime Incident AI Agent and deterministic notification
```

The monitor detects facts. The Runtime Incident system investigates and
reports them. Neither component joins the Telegram intake, recognition,
contextual-resolution, management, reconciliation, or exchange-mutation
critical path.

## Current Problems

The existing `telegram-kol-monitor.service` is a oneshot whose exit code mixes
two different questions:

1. Did the monitor program complete?
2. Did the monitor observe a healthy production system?

Any unhealthy observation therefore leaves the unit in systemd's `failed`
state even when the main trading service remains healthy and stable. That red
unit state has become an unreliable operational signal and can obstruct
otherwise safe procedures.

The current composite check also consumes a live-position cache that can be
older than its five-minute freshness requirement. Historically, that cache
was refreshed on startup when absent and on demand from the positions UI, so
an idle UI could make the monitor fail deterministically.

The monitor projection producer accepts the `composite` and `coverage`
adapter labels, while the production incident receiver has retained an older
allowlist and cardinality limit. A valid monitor capture can therefore receive
HTTP 422 and lose the durable incident handoff.

Finally, current checks contain independent fixed timing assumptions, including
five-minute snapshot/heartbeat freshness and a fifteen-minute composite stall
threshold. These values are not consistently bound to each operation's own
`execution_deadline_at` or to the temporal relationship between the local
state and the exchange snapshot. This can turn normal exchange eventual
consistency into a premature alert.

## Non-Goals And Safety Invariants

The redesign must not:

- place, cancel, close, retry, or compensate an exchange request;
- change trading settings or restart the main service;
- write the production trading database from the refresher or monitor;
- allow the Agent to replace deterministic strategy targeting or execution;
- replay a historical Telegram message;
- enable MiMo v2;
- weaken deployment preflight or any Batch 119 approval boundary;
- treat missing, stale, partial, or temporally incoherent evidence as healthy.

The independent refresher may be activated only with an exchange credential
whose read-only permission has been independently proven. A trading-capable
credential must not be mounted into the refresher unit. If a provably
read-only credential is unavailable, activation is blocked and the credential
boundary must be redesigned before rollout.

## Chosen Architecture

### Read-only snapshot refresher

A dedicated unprivileged oneshot runs on a two-minute timer. It calls only a
closed allowlist of Deepcoin read endpoints and has no exchange mutation client
surface. It receives only the minimum read-only authentication material and
does not mount the production database.

Each run produces a bounded, sanitized snapshot envelope containing:

- a unique monotonically ordered generation;
- request start and completion timestamps;
- a redacted account-scope fingerprint;
- pagination and source-completeness evidence;
- validation status, including duplicate-identity rejection;
- either a complete payload or a closed failure code.

The store retains only the latest three sealed generations and replaces its
manifest atomically. A failed refresh records failure; it never advances an
old payload's capture time or presents an old payload as new evidence.

The refresher is single-flight and bounded by request, response-size, page,
and wall-clock limits. Rate limiting, timeout, malformed data, incomplete
pagination, empty partial responses, clock skew, and overlapping invocation
all fail closed as unavailable evidence.

### Lightweight deterministic sentinel

The sentinel runs every five minutes and performs only bounded read-only
checks. It reads service availability, reviewed code identity, trading gates,
journald summaries, a coherent SQLite read snapshot, Runtime Incident channel
health, message-operation coverage, and the sealed exchange snapshot history.

The existing daily management audit is separated from the five-minute path
and remains an independently scheduled low-frequency check. A heavyweight
audit cannot make the lightweight sentinel overrun or overlap its next run.

The sentinel does not call a model, diagnose a root cause, choose a playbook,
or execute recovery. It returns two independent top-level dimensions:

- `execution_status`: `COMPLETED` or `FAILED`;
- `observed_health`: `HEALTHY`, `UNHEALTHY`, or `UNKNOWN`.

Business/configuration anomalies produce `COMPLETED + UNHEALTHY`. Missing or
temporally incoherent evidence produces `COMPLETED + UNKNOWN`. Only inability
to initialize, validate essential configuration, or atomically persist a
completed result, plus an unhandled program failure, produces
`execution_status=FAILED`.

systemd receives a nonzero exit only for `execution_status=FAILED`. A business
anomaly, an unavailable observation source, an incident-channel error, or a
notification delivery failure does not turn a successfully completed oneshot
red. Those facts remain visible in structured state and journald.

### Shared incident contract

The monitor projection and main-service receiver import one versioned schema
authority. That authority owns adapter labels, maximum cardinalities, reason
codes, notification outcomes, size limits, and canonical serialization.
Producer and receiver must not maintain parallel allowlists.

Every submission carries a deterministic idempotency identifier derived from
the schema version, observation generation, and sanitized anomaly fingerprint.
If the main service commits an incident but its HTTP response is lost, the
monitor resubmits the same identifier and receives the original acceptance
result. A timeout alone must not cause a duplicate direct Telegram fallback.

The main service remains the only component that writes the runtime incident
ledger through its existing trusted writer. The monitor retains no SQLite
write mount.

### Runtime Incident Agent and notification ownership

The Runtime Incident system owns normal durable notification and AI
investigation. Once the bridge proves that an anomaly generation was accepted,
the monitor does not send a second normal alert.

The Agent may investigate bounded evidence, prepare a Codex handoff, and use
only its existing approved authority. The redesign grants no new action
authority, playbook allowlist, shell access, or exchange mutation permission.

The monitor keeps one narrow fallback notification path only for a proven
failure of the incident intake or notification channel. Fallback messages use
a fixed bounded template containing timestamps, closed reason codes, component
labels, and evidence status. They contain no model diagnosis, credentials,
payloads, identifiers that are not already allowlisted, or recovery advice.

Incident intake, deterministic notification, and Agent diagnosis have separate
service-level deadlines. Normal Agent queue time is not an incident-channel
failure. A fallback is eligible only after the applicable channel deadline and
an idempotent acceptance recheck fail.

## Temporal Consistency And False-Positive Control

### Settling lifecycle

One inconsistent observation is not automatically an incident. Each sanitized
candidate has a durable monitor-state record containing:

- reason code and fingerprint;
- first and last observation times;
- related local `last_progress_at` and `execution_deadline_at`;
- earliest confirmation time;
- distinct exchange snapshot generations observed;
- consecutive observation count;
- current `SETTLING`, `CONFIRMED`, or `RESOLVED` status;
- exact confirmation and resolution evidence classes.

Waiting occurs between timer invocations. The monitor never sleeps while
holding a process, database handle, HTTP connection, or systemd unit active.

### Single reason-policy authority

Every emitted reason code belongs to exactly one closed policy registry. Each
policy declares:

- immediate, settling, or evidence-unknown classification;
- authoritative business deadline source;
- minimum distinct complete snapshot count;
- temporal ordering required between local progress and exchange capture;
- notification and Agent eligibility;
- maximum decision latency when evidence remains available;
- exact resolution rule.

An unregistered reason code is `UNKNOWN`, blocks deployment, and is ineligible
for a guessed alert classification. Timing constants must not be scattered in
individual evaluators.

### Immediate facts

Persisted `submit_unknown`, `recovery_required`, proven duplicate submissions,
invalid durable identity, corrupt required schema/evidence, and other facts
that cannot be explained by exchange readback delay are confirmed immediately
when their complete authoritative source is read.

### Settling facts

New submissions, cancellations, partial fills, `awaiting_exchange`,
`pending_readback`, position-size convergence, and protection replacement
remain `SETTLING` until their own durable deadline. The monitor must use the
operation's `execution_deadline_at`; a missing required deadline is
`UNKNOWN`, not an invitation to invent a global timeout.

After the deadline, a local-versus-exchange mismatch is confirmable only from
complete exchange generations captured after the related local
`last_progress_at`. Cross-system position or protection mismatches normally
require two distinct consecutive complete generations. Re-reading one file or
two snapshots from before the local transition does not count.

With evidence continuously available, a settling candidate must be decided by
the first five-minute sentinel run that has all policy-required post-deadline
generations. If evidence remains incomplete, it remains `UNKNOWN`; a latency
target never authorizes guessing.

### Startup, deployment, and source warm-up

After main-service startup, exchange-dependent and supervisor-dependent facts
remain `UNKNOWN/STARTING` until the first successful reconciliation and the
relevant worker heartbeat are observed. A fixed elapsed startup delay alone
does not prove readiness.

An intentional deployment cannot be hidden by a broad maintenance flag. Unit,
expected-SHA, and schema changes must follow the separately approved rollout
ordering so the old active monitor never calls an unsupported new receiver or
vice versa.

### Recovery hysteresis

A confirmed anomaly resolves only from an authoritative durable terminal fact
or the policy-required number of distinct complete healthy generations. One
temporarily clean response cannot cause a recovery message. A later recurrence
after confirmed resolution creates a new generation rather than reopening an
old notification claim ambiguously.

## Other Race And Integrity Boundaries

- A fresh snapshot that predates local progress is unusable for comparison.
- An empty exchange list is authoritative only after successful complete
  pagination and account-scope validation.
- A service endpoint timeout, one generic ERROR log, or one exchange rate-limit
  response starts an unavailable/candidate observation; it does not by itself
  prove a business anomaly.
- SQLite facts are read from one coherent query-only transaction. A changing
  source that cannot be proven coherent is `UNKNOWN`.
- Future timestamps, material clock rollback, severe timer lateness, and
  out-of-order snapshot generations are `UNKNOWN` and recorded safely.
- Snapshot refresh, sentinel evaluation, incident submission, and fallback
  delivery each use independent single-flight/idempotency controls.
- State files contain only bounded sanitized facts and are written atomically
  with restrictive permissions.

## Deployment Gate Semantics

The redesign does not use `systemctl is-active telegram-kol-monitor.service`
as a production-health fact. A oneshot can be inactive after successful
completion.

Ordinary deployment preflight must instead verify all of the following:

- the sentinel timer is enabled and active;
- the latest structured result is within its freshness budget;
- its schema is supported and complete;
- `execution_status=COMPLETED`;
- `observed_health=HEALTHY`;
- no `SETTLING`, `STARTING`, `UNKNOWN`, or unsubmitted confirmed incident is
  present;
- required exchange/database evidence is complete;
- every existing independent deployment-gate fact still passes.

Missing, stale, malformed, incompatible, `UNHEALTHY`, or `UNKNOWN` monitor
evidence blocks deployment. A systemd exit code of zero never overrides those
facts.

The stopped read-only double capture, Batch 119 apply, and ordinary code
deployment remain three separate explicit approval boundaries. This design
does not cross any of them.

## State, Deduplication, And Fallback

The new state schema is versioned and has one owner. It records only the latest
completed run, bounded candidate generations, incident acceptance state,
fallback delivery state, and the low-frequency audit cursor.

Normal notification deduplication belongs to the Runtime Incident ledger.
Monitor-local deduplication applies only to channel-failure fallback. A
fallback fingerprint changes immediately when its bounded facts change and
otherwise follows a fixed retry policy. A failed fallback persists
`fallback_pending` for a later timer run and remains visible in journald; it
does not block the trading path.

## Old-Logic Removal

Migration uses two reviewed stages so production can compare the new shadow
facts before cutover, but old behavior is not a permanent compatibility path.

Stage one may retain only the minimum old code required for shadow comparison.
It must carry an explicit deletion inventory. After the new path is activated
and verified, stage two removes:

- the old `healthy=false` to nonzero-exit coupling;
- ordinary business-anomaly direct Telegram delivery from the monitor;
- superseded monitor notification, recovery, and deduplication branches;
- monitor dependence on UI-driven position-cache refresh;
- duplicate producer/receiver adapter allowlists;
- the legacy state schema and compatibility readers;
- obsolete CLI arguments, environment values, installer branches, and unit
  settings;
- tests, fixtures, and documentation whose only purpose was deleted behavior.

Reusable deterministic fact evaluators may remain only when reachability and
ownership tests prove that they have one current responsibility. The fallback
notifier is a new channel-failure-only component, not a renamed copy of the old
ordinary alert route.

Rollback uses reviewed Git/unit versions, not dormant dead-code switches. The
project cannot be marked complete until the stage-two deletion passes review,
tests, static reachability checks, and production verification.

## Testing Strategy

Implementation follows test-driven development. Required local coverage
includes:

- read-only method allowlisting and the absence of mutation client surfaces;
- refusal to activate without a proven read-only credential boundary;
- atomic three-generation snapshot storage and bounded retention;
- pagination, identity, account-scope, empty-list, size, timeout, and
  rate-limit validation;
- independent `execution_status` and `observed_health` semantics;
- nonzero exit only when a run cannot initialize or persist completely;
- shared producer/receiver schema and acceptance of all registered adapters;
- idempotent lost-response resubmission without duplicate fallback;
- separate intake, notification, and Agent SLA handling;
- exact deadline boundary tests at one second before, at, and after expiry;
- exchange confirmation arriving immediately before and after evaluation;
- snapshots before and after local progress, repeated generation rejection,
  two-generation confirmation, and first-bad/second-good convergence;
- out-of-order/future snapshots, clock rollback, timer lateness, startup
  warm-up, and overlapping-unit refusal;
- confirmation and recovery hysteresis under alternating observations;
- immediate durable-terminal reason behavior;
- every emitted reason code appearing exactly once in the policy registry;
- fail-closed behavior for unknown policy/schema fields;
- `SETTLING`, `STARTING`, and `UNKNOWN` deployment blocking;
- static unit, mount, identity, credential, and database write-boundary checks;
- regression coverage for Telegram intake, recognition, contextual resolution,
  execution, reconciliation, Runtime Agent authority, historical-message
  immutability, MiMo v1, and the deployment gate;
- static/reachability proof that stage-two deletion leaves no dual evaluator,
  notification route, schema authority, or deprecated configuration.

Integration tests use fake clocks, delayed/readback simulations, incomplete
pagination, injected response loss, and temporary databases. They do not use
live trading, send Telegram messages, or call production Deepcoin mutation
endpoints.

## Rollout And Approval Boundaries

1. Implement and review locally with all new behavior dormant.
2. Stop at the ordinary code-deployment approval boundary.
3. After explicit approval and a proven safe window, deploy the code with the
   new refresher and sentinel disabled.
4. Run one credential/endpoint/read-only refresher canary and prove zero
   database and exchange mutation.
5. Run the new sentinel manually in no-notify shadow mode and compare its facts
   with the old path across settling and stable states.
6. Enable the snapshot timer, then the sentinel timer, only after the required
   evidence is complete. Keep normal notification ownership in the Runtime
   Incident system and enable only the narrow fallback route.
7. Verify main service, Telegram intake, reconciliation, Agent, structured
   monitor state, incident idempotency, timer history, and deployment preflight.
8. In a separately reviewed cleanup change and separately approved deployment,
   delete the old logic and prove no deprecated path remains.

No rollout step authorizes Batch 119 apply. Batch 119 remains paused until its
own approved stopped-state evidence and apply boundary are reached.

## Rollback

Rollback disables the new timers and restores an explicitly reviewed prior
unit/config version. It does not edit the trading database, replay messages,
change exchange state, enable MiMo v2, or restart trading logic unless a later
ordinary deployment approval explicitly covers that restart.

If neither the new nor the reviewed prior monitor can run safely, production
enters an explicit `monitoring_paused` state and deployment remains blocked.
The system must not manufacture a healthy monitor result to make rollback look
clean.

## Completion Criteria

The redesign is complete only when:

- the new refresher, sentinel, shared schema, settling policies, incident route,
  and fallback route pass local and server verification;
- production evidence shows no interruption or mutation outside the approved
  read-only scope;
- deployment preflight consumes the new structured state fail-closed;
- normal notifications have one owner;
- the stage-two cleanup has deleted every inventoried legacy path;
- independent review finds no Critical or Important issue;
- after the final separately approved production deployment, its exact SHA is
  recorded as the new baseline for any later MiMo v2 work, without enabling
  MiMo v2 during this rollout.
