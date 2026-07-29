# Runtime Agent Production Audit Rerun Design

## Status

Approved by the existing Phase 6 rollout boundary and the canonical status
checkpoint. This turn implements only `rerun_production_audit`; every Agent,
shadow, and action flag remains off in persistent production configuration.

## Goal

Let the Runtime Incident Agent execute one bounded, read-only management audit
and verify that the audit produced a complete result. The handler must not
start system services, send notifications, alter business rows, or gain
exchange credentials.

## Approaches Considered

### Selected: run the existing private-snapshot audit in the sidecar process

The existing `_audit_management_batches_read_only` path reads a stable private
SQLite snapshot and does not require Deepcoin, Telegram, systemd, or business
write authority. A small coordinator runs that function once, projects only
bounded completion facts, and exposes the captured proof to the existing
`get_service_audit_state` verification tool exactly once.

This keeps the action within the sidecar's current read-only filesystem and
database permissions. It also avoids adding a privileged bridge.

### Rejected: start `telegram-kol-monitor-diagnostic.service`

The sidecar is deliberately denied access to systemd's control socket and has
no service-start authority. Adding sudo, Polkit, or system-bus access would
expand privilege for a read-only playbook and weaken the existing sandbox.

### Rejected: add a main-service loopback audit endpoint

The management audit is already database-only, so routing it through the Web
service would add an HTTP boundary, main-service workload, and another
credential-bearing component without providing a stronger proof.

## Components and Data Flow

1. `RuntimeAgentProductionAuditRefresh` receives an injected audit runner.
2. The action handler calls `rerun` with the exact incident and executor
   identity fields.
3. The runner executes the existing stable private-snapshot management audit
   with a bounded limit.
4. The coordinator validates and reduces the result to fixed completion facts:
   snapshot status, validation status, completeness, malformed-row count, and
   bounded alert-state counts.
5. The proof is held only in bounded process memory and keyed by incident ID.
6. The existing `get_service_audit_state` tool atomically consumes that proof
   and returns `audit_run_completed`, `complete`, and `monitor_error`.
7. The executor independently applies its existing playbook-specific
   verification rule and durably records the bounded proof in the recovery
   attempt.
8. A later audit-state query falls back to the existing passive monitor-state
   projection.

## Failure and Safety Behavior

- Invalid executor identity fails before running the audit.
- Runner exceptions fail the action and freeze the isolated incident through
  the existing executor behavior.
- An incomplete or malformed audit is still captured as a completed run, but
  verification returns `complete: false` with a generic bounded error code.
- The capture is consumed before verification processing, so it cannot verify
  twice or become a permanent live override.
- Captures are capped at 32; oldest entries are evicted.
- No raw batches, IDs, payloads, paths, provider errors, or audit command
  output enter the Runtime Agent tool result.
- Persistent Agent/action flags remain off; missing injection continues to
  produce `executor_not_configured`.

## Testing and Deployment

Tests cover valid complete results, historical abnormal counts, incomplete and
malformed results, runner exceptions, one-shot consumption, bounded capture
storage, production CLI wiring, passive fallback, and executor verification.
Critical Runtime Agent, listener, contextual-resolution, management, mutation,
and monitor regressions must pass.

Production deployment follows a fresh safe-window check with all Agent/action
flags off. The canary uses an isolated temporary database and the deployed
read-only production database audit. It may temporarily enable only the
in-process executor configuration for that isolated incident; it must not
persist flags, write the production incident ledger, or send Telegram.

