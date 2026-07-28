# Runtime Incident AI Agent Design

## Status

Approved in principle on 2026-07-28 with one additional non-negotiable
requirement: phased implementation must not interrupt the existing trading
system or cause a time-sensitive strategy, management instruction, or exit to
be missed.

This document is the authoritative architectural boundary. The implementation
plan may refine task details, but it must not weaken the invariants below.

## Goal

Add an event-driven, custom AI agent that investigates runtime failures,
notifies the operator through the existing Telegram system bot, and later gains
limited authority to execute approved recovery playbooks. The agent reduces
manual incident investigation without taking over strategy interpretation or
the normal trading path.

## Explicit Non-Goal

The agent does not own ambiguous message interpretation.

The existing flow already handles the case where first-pass AI cannot identify
the target strategy or encounters contextual ambiguity. It gathers multiple
sources of information and performs contextual strategy resolution. That flow
remains authoritative and independent.

The incident agent must not:

- participate in first-pass recognition;
- choose which strategy a message belongs to;
- replace or duplicate contextual multi-information resolution;
- rewrite recognition or contextual-resolution results;
- turn `unresolved` or `hold` into a target strategy;
- guess order, position, lifecycle, or strategy ownership;
- replay a trading instruction because an AI component failed;
- bypass the existing mutation gateway, idempotency, or reconciliation rules.

If the existing contextual resolver crashes, times out, or exhausts retries,
the incident agent may diagnose the technical failure. It may not produce a
replacement business decision.

## Architectural Choice

### Rejected: embed the agent in the recognition or execution critical path

This offers low latency but couples model availability and agent behavior to
time-sensitive trading. It creates unacceptable risk of blocking normal
messages or delaying exits.

### Rejected as the primary design: hourly Codex scheduled polling

Scheduled Codex remains useful as an optional audit fallback. It consumes usage
even when no incident exists, adds polling delay, and requires the local
machine and app for local project access.

### Selected: event-driven sidecar worker

The production system records a durable incident only when a defined runtime
failure occurs. A separate worker claims the incident and invokes the custom
agent. The normal listener, recognition, contextual resolution, execution, and
reconciliation flows do not wait for this worker.

```text
normal production flow
    |
    +-- succeeds ------------------------------> unchanged
    |
    +-- emits a structured runtime incident
            |
            v
       runtime_incidents
            |
            v
       incident agent worker
            |
            +-- read-only diagnosis
            +-- Telegram report
            +-- Codex handoff bundle
            `-- later: allowlisted recovery playbook
```

## Runtime Continuity Invariants

Each phase must satisfy all of the following before production enablement:

1. **No critical-path dependency.** Agent failure, provider failure, queue
   backlog, or database claim failure cannot block the normal production path.
2. **Dormant by default.** New workers and actions ship behind explicit feature
   flags that default off.
3. **Shadow before authority.** Every new incident class and every new
   playbook runs read-only or shadow-only before it can mutate state.
4. **No deployment during an unverified active operation.** Before restart,
   verify that no recognition/execution/management transition is currently in
   a time-sensitive in-flight state. If that cannot be proven, defer server
   deployment without marking the phase complete.
5. **Durable intake before restart.** Existing raw-message persistence and
   reconciliation remain enabled so Telegram messages received around a
   restart can be recovered.
6. **Bounded restart.** Runtime changes use the existing reviewed deployment
   helper and perform immediate service, listener, checkpoint, backlog, and
   safety-monitor verification.
7. **Immediate disable path.** Agent flags can be turned off without disabling
   Telegram intake, first-pass AI, contextual resolution, execution, or
   reconciliation.
8. **No schema-dependent cutover.** Additive schema changes must be compatible
   with the currently running code. Old code must continue operating until the
   new worker is enabled.
9. **One phase per conversation.** A phase cannot silently acquire permissions
   planned for a later phase.
10. **Server evidence required.** Runtime phases remain `in_progress` until
    server verification is complete. Documentation-only phases are exempt from
    service restart.

## Incident Scope

Initial runtime incidents are technical and execution-state failures:

- model/provider call failure or retry exhaustion;
- background worker crash, stale claim, or exhausted retry;
- production monitor adapter failure or incomplete audit;
- `submit_unknown`;
- `partial_failed`;
- `recovery_required`;
- severe protection incident;
- service/configuration drift;
- notification delivery failure.

Normal contextual outcomes are not runtime incidents:

- `unresolved`;
- `hold`;
- multiple valid strategy candidates;
- a normal request for later contextual reanalysis.

A contextual worker crash can create a technical incident, but its unresolved
business payload remains owned by the existing resolver.

## Components

### Runtime incident ledger

An additive `runtime_incidents` table provides:

- stable incident ID;
- incident type, source kind, and source record ID;
- redacted state fingerprint;
- severity;
- first/last occurrence and repeat count;
- lifecycle state;
- claim token and claim timestamps;
- diagnosis and evidence references;
- notification state;
- selected playbook and recovery state;
- exact feature/prompt/tool policy versions.

The ledger stores references and bounded redacted summaries, not secrets,
complete provider responses, raw tracebacks, or unbounded logs.

### Incident adapters

Small adapters convert existing durable failure states into incident records.
They must be best-effort and fail open with respect to the normal production
path: an incident-recording failure is logged and monitored but does not alter
the original business transition.

### Agent worker

The worker:

1. claims one incident generation;
2. loads a bounded redacted incident summary;
3. calls only policy-allowed tools;
4. stores structured diagnosis;
5. optionally selects an allowlisted playbook;
6. verifies completion or records escalation;
7. schedules a Telegram report.

The loop has maximum step, token, wall-clock, and repeated-tool limits.

### Read-only tools

The first tool set includes:

- incident summary;
- relevant lifecycle/binding/order-leg state;
- relevant worker/job state;
- redacted service and audit state;
- bounded journal summary;
- coherent read-only exchange snapshot;
- local-versus-exchange comparison;
- prior attempts for the same fingerprint.

These tools do not expose arbitrary SQL, arbitrary shell, credentials, or raw
Deepcoin write methods.

### Recovery playbooks

Playbooks are versioned deterministic functions. The agent selects a playbook;
it does not generate arbitrary recovery code or exchange requests.

Every playbook declares:

- permitted incident types;
- prerequisites and refusal reasons;
- side-effect class;
- idempotency key;
- maximum attempts;
- pre-execution state fingerprint;
- verification query;
- terminal success and escalation conditions.

### Telegram reporting

The existing system operator bot reports:

- incident ID and type;
- impact and current containment;
- evidence-based diagnosis;
- actions attempted;
- verification result;
- remaining risk;
- whether Codex repair is required.

Notifications use fixed labels, bounded values, redaction, and durable
at-most-once claims.

### Codex handoff bundle

When code repair is needed, the system stores a durable bundle containing:

- incident timeline and stable record IDs;
- expected versus observed state;
- bounded redacted evidence;
- agent tool calls and results;
- diagnosis clearly labeled as a hypothesis;
- attempted playbooks;
- reproduction hints;
- likely files and tests;
- explicit prohibited actions.

The Telegram report includes a ready-to-use request that tells Codex to verify,
not trust, the agent diagnosis.

## Authority Phases

1. **Observe:** collect incidents and notify without AI.
2. **Diagnose:** AI uses read-only tools and prepares a Codex handoff.
3. **Shadow:** AI selects a playbook; policy evaluates it; nothing executes.
4. **Low-risk recovery:** execute only side-effect-free or operationally
   reversible playbooks.
5. **Bounded business recovery:** optional, separately approved, and always
   routed through existing deterministic planners and mutation gateways.

The agent never receives open-ended production shell access or unrestricted
exchange credentials.

## Error Handling

- Incident capture failure never changes the source transaction.
- Agent/provider failure leaves the incident pending or escalated.
- A stale claim can be reclaimed only through a token-checked transition.
- Same-fingerprint analysis is reused rather than repeated.
- Unknown tool output fails closed and is excluded from prompts and reports.
- A recovery verification mismatch stops the loop and escalates.
- `submit_unknown` remains frozen; the agent may investigate but never retries
  the unknown write.
- Automatic action budget exhaustion disables further action for that incident
  generation.

## Testing Strategy

Each runtime phase requires:

- focused unit tests written before implementation;
- database migration and compatibility tests;
- concurrency/idempotency tests;
- redaction and resource-bound tests;
- an architectural boundary test preventing imports/calls into strategy
  targeting and contextual-resolution application paths;
- regression tests for listener, recognition, contextual resolution,
  management execution, and production monitoring;
- server-side read-only verification;
- controlled feature-flag enablement only after baseline comparison.

Live trading is never used as a test fixture.

## Cross-Conversation Control

The exact user phrase `请执行自定义ai agent的下一步实施` is a durable project
command. `AGENTS.md` routes it to the design, implementation plan, status, and
runbook.

The status file is the only authority for what to do next:

- `planned`: start `current_phase`;
- `in_progress`: resume `current_phase`;
- `completed`: advance exactly once to the next planned phase;
- `blocked`: do not change scope; record the blocker and request the missing
  authority or external state.

Chat memory is informative only. It cannot override the committed design or
status.
